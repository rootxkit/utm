"""`IngestStore` over TimescaleDB plus the file archive.

Split by what each medium is good at:

- **The datagrams go to files** (`gateway/archive.py`). Opaque bytes nobody
  queries by content, read back as a time range after an incident.
- **The index goes to the database**: which segment covers which hour and
  which sequence range, what has been acknowledged, which holes are permanent,
  and what happened.

Object storage is a later implementation of `RawArchive`, not a migration,
because no row here holds a datagram.

## Dedupe is bounded per epoch, never by time

A time window would be wrong. A relay offline for two hours and replaying its
backlog is the design working as intended (`relay-v1.md` §12, §10), and a
window would reject exactly the case the queue exists for.

Per `(station_id, epoch)` the state is one `highest_contiguous_seq` and a short
list of permanent gaps. That is constant-size however old the replay is, and it
is what `resume_from_seq` needs anyway - the dedupe state and the resume point
are the same number, so they cannot drift apart.

Anything at or below the watermark is a duplicate and is dropped. That is
sound because relay-v1 §10 guarantees in-order delivery per epoch and §5 makes
the server authoritative about where to resume, so a record below the watermark
can only be a retransmission.

## The failure mode of dropping a closed epoch

An epoch is closed when its station declares a different one. Closed epochs are
retained for `epoch_retention_days` and then dropped.

**If a relay reconnects under an epoch that has been dropped, it is treated as
new: `resume_from_seq` is 0 and the station resends whatever it still holds.**
That duplicates data in the archive rather than losing it. This is the right
direction to fail, and it is written down here because the opposite - keeping
the watermark and quietly discarding the resend - looks identical in every log
and silently loses a flight.

A relay is only in that position if it held an unacknowledged queue for longer
than the retention period, which is itself worth an event.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from common import get_logger
from gateway.archive import (
    ArchiveError,
    RawArchive,
    SegmentWrite,
    group_by_hour,
    segment_relative_path,
)
from gateway.ingest_store import StoreError
from gateway.relay_messages import Gap
from gateway.relay_records import Record
from gateway.stage_timing import StageTimings, shared_timings
from gateway.station_state import LinkState, LossEvent

_log = get_logger(__name__)

# The watermark for an epoch that has stored nothing. Not 0, which would claim
# record zero had arrived.
EMPTY_WATERMARK: Final = -1

_epochs = sa.table(
    "relay_epochs",
    sa.column("station_id", sa.Text),
    sa.column("epoch", sa.Text),
    sa.column("highest_contiguous_seq", sa.BigInteger),
    sa.column("opened_at", sa.DateTime(timezone=True)),
    sa.column("last_seen_at", sa.DateTime(timezone=True)),
    sa.column("closed_at", sa.DateTime(timezone=True)),
)

_gaps = sa.table(
    "relay_epoch_gaps",
    sa.column("station_id", sa.Text),
    sa.column("epoch", sa.Text),
    sa.column("from_seq", sa.BigInteger),
    sa.column("to_seq", sa.BigInteger),
    sa.column("reason", sa.Text),
)

_segments = sa.table(
    "archive_segments",
    sa.column("station_id", sa.Text),
    sa.column("epoch", sa.Text),
    sa.column("relative_path", sa.Text),
    sa.column("hour_start", sa.DateTime(timezone=True)),
    sa.column("first_seq", sa.BigInteger),
    sa.column("last_seq", sa.BigInteger),
    sa.column("first_recv_utc_ns", sa.BigInteger),
    sa.column("last_recv_utc_ns", sa.BigInteger),
    sa.column("record_count", sa.Integer),
    sa.column("compressed_bytes", sa.BigInteger),
    sa.column("uncompressed_bytes", sa.BigInteger),
)

_events = sa.table(
    "ingest_events",
    sa.column("station_id", sa.Text),
    sa.column("epoch", sa.Text),
    sa.column("event_type", sa.Text),
    sa.column("payload", JSONB),
)


@dataclass
class TimescaleIngestStore:
    """The production `IngestStore`."""

    engine: AsyncEngine
    archive: RawArchive
    epoch_retention_days: int = 30
    timings: StageTimings = field(default_factory=shared_timings)

    async def resume_from_seq(self, station_id: str, epoch: str) -> int:
        """protocol §5. From durable state, so a restart answers the same."""
        watermark = await self._watermark(station_id, epoch)
        return watermark + 1

    async def store_records(
        self, station_id: str, epoch: str, records: list[Record]
    ) -> int:
        """Archive, index and advance the watermark. Durable before returning.

        Order matters and is the point of the method: the datagrams reach disk
        and `fsync` returns *before* the transaction that advances the
        watermark commits. The reverse order would let a crash between the two
        leave the Gateway believing it holds records that are not there, and it
        would already have acknowledged them.
        """
        await self._ensure_epoch(station_id, epoch)
        watermark = await self._watermark(station_id, epoch)

        fresh = [record for record in records if record.seq > watermark]
        if not fresh:
            # Everything was a retransmission. Still refresh liveness so a
            # station that is only resending does not look idle.
            await self._touch(station_id, epoch)
            return watermark

        if fresh[0].seq != watermark + 1:
            # §10 promises in-order delivery and §5 makes us authoritative
            # about where to resume, so this cannot happen against a
            # conforming relay. The records are archived anyway - never
            # discard telemetry - but the watermark does not move, which
            # leaves resume_from_seq asking for the missing range on the next
            # connection.
            await self._record_event(
                station_id,
                epoch,
                "sequence_discontinuity",
                {
                    "expected_seq": watermark + 1,
                    "received_seq": fresh[0].seq,
                    "missing_count": fresh[0].seq - (watermark + 1),
                },
            )
            await self._archive_and_index(station_id, epoch, fresh, advance_to=None)
            return watermark

        advanced = fresh[-1].seq
        await self._archive_and_index(station_id, epoch, fresh, advance_to=advanced)
        return await self._extend_through_gaps(station_id, epoch, advanced)

    async def record_gap(self, station_id: str, epoch: str, gap: Gap) -> None:
        """protocol §11. Permanent, and it advances the resume point."""
        await self._ensure_epoch(station_id, epoch)
        try:
            async with self.engine.begin() as connection:
                await connection.execute(
                    pg_insert(_gaps)
                    .values(
                        station_id=station_id,
                        epoch=epoch,
                        from_seq=gap.from_seq,
                        to_seq=gap.to_seq,
                        reason=gap.reason,
                    )
                    # A gap re-reported on a later reconnect is the same hole,
                    # not a second one.
                    .on_conflict_do_nothing(constraint="relay_epoch_gaps_unique")
                )
        except SQLAlchemyError as error:
            raise StoreError(f"could not record gap: {error}") from error

        await self._record_event(
            station_id,
            epoch,
            "gap",
            {
                "from_seq": gap.from_seq,
                "to_seq": gap.to_seq,
                "missing_count": gap.missing_count,
                "reason": gap.reason,
            },
        )

        watermark = await self._watermark(station_id, epoch)
        await self._extend_through_gaps(station_id, epoch, watermark)

    async def record_loss(self, station_id: str, epoch: str, loss: LossEvent) -> None:
        await self._record_event(
            station_id,
            epoch,
            f"loss.{loss.kind.value}",
            {
                "from_seq": loss.from_seq,
                "to_seq": loss.to_seq,
                "datagram_count": loss.datagram_count,
                "detail": loss.detail,
            },
        )

    async def record_link_state(
        self, station_id: str, state: LinkState, *, at_utc_ns: int
    ) -> None:
        await self._record_event(
            station_id,
            None,
            "link_state",
            {"state": state.value, "station_utc_ns": at_utc_ns},
        )

    # --- epoch lifecycle ---------------------------------------------------

    async def open_epoch(self, station_id: str, epoch: str) -> None:
        """Declare this epoch current and close every other one for the station.

        protocol §4: the epoch changes only when the relay's queue database is
        created, so a different epoch means the previous one will never be
        continued.
        """
        await self._ensure_epoch(station_id, epoch)
        try:
            async with self.engine.begin() as connection:
                result = await connection.execute(
                    sa.update(_epochs)
                    .where(
                        _epochs.c.station_id == station_id,
                        _epochs.c.epoch != epoch,
                        _epochs.c.closed_at.is_(None),
                    )
                    .values(closed_at=_now())
                    .returning(_epochs.c.epoch)
                )
                closed = [row.epoch for row in result]
        except SQLAlchemyError as error:
            raise StoreError(f"could not open epoch: {error}") from error

        for previous in closed:
            await self._record_event(
                station_id, previous, "epoch_closed", {"superseded_by": epoch}
            )

    async def purge_closed_epochs(self) -> int:
        """Drop epochs closed longer ago than the retention period.

        Not on the ingest path. Read the module docstring before changing the
        retention period: a relay reconnecting under a dropped epoch resends
        its backlog, which duplicates rather than loses, and that asymmetry is
        the reason this is safe at all.
        """
        cutoff = sa.text(f"now() - interval '{int(self.epoch_retention_days)} days'")
        try:
            async with self.engine.begin() as connection:
                result = await connection.execute(
                    sa.delete(_epochs)
                    .where(
                        _epochs.c.closed_at.is_not(None),
                        _epochs.c.closed_at < cutoff,
                    )
                    .returning(_epochs.c.epoch)
                )
                return len(list(result))
        except SQLAlchemyError as error:
            raise StoreError(f"could not purge closed epochs: {error}") from error

    # --- internals ---------------------------------------------------------

    async def _archive_and_index(
        self,
        station_id: str,
        epoch: str,
        records: list[Record],
        *,
        advance_to: int | None,
    ) -> None:
        """Append to the archive, then index and advance in one transaction.

        Idempotent, because at-least-once delivery (§10) means the same batch
        arrives again after any failure between the file and the ack, and the
        relay keeps resending it until it is acknowledged. Three defences, in
        the order they apply:

        1. Hour groups whose exact index row `(path, first_seq, last_seq)`
           already exists are not appended again. This is the recovery path
           for a station left with the index written and the watermark not:
           before S-05 those were two transactions, and a crash between them
           left the next resend raising on `archive_segments_unique` on every
           attempt, for ever, appending the same bytes to the segment each
           time. The rows are there, so only the watermark is missing.
        2. The index insert is `ON CONFLICT DO NOTHING` on that constraint, so
           a row that exists anyway - a resend re-batched across an hour
           boundary, say - is not an error.
        3. The index rows and the watermark commit together. A crash can now
           only leave an unindexed frame at the end of a segment, which the
           next resend appends once more; `read_segment` reads across that
           duplicate (contiguity is not required), and the index says which
           bytes it vouches for.

        Files first, fsync included, then the index. A crash between them
        leaves an unindexed frame, which is recoverable. The opposite order
        leaves an index entry for bytes that do not exist, which is not.
        """
        already = await self._indexed_ranges(
            station_id, epoch, records[0].seq, records[-1].seq
        )
        pending: list[Record] = []
        skipped = 0
        for hour, group in group_by_hour(records):
            key = (
                segment_relative_path(station_id, epoch, hour),
                group[0].seq,
                group[-1].seq,
            )
            if key in already:
                skipped += len(group)
            else:
                pending.extend(group)
        if skipped:
            _log.info(
                "segments already indexed; advancing the watermark only",
                extra={
                    "station_id": station_id,
                    "epoch": epoch,
                    "records_skipped": skipped,
                    "advance_to": advance_to,
                },
            )

        writes: list[SegmentWrite] = []
        if pending:
            # On a worker thread: compression and `fsync` are blocking, and
            # run on the event loop they were time in which no other station
            # was served, so one station's fsync stalled every other
            # station's acks (S-04).
            try:
                with self.timings.measure("store.archive"):
                    writes = await asyncio.to_thread(
                        self.archive.append, station_id, epoch, pending
                    )
            except ArchiveError as error:
                raise StoreError(f"archive write failed: {error}") from error

        try:
            async with self.engine.begin() as connection:
                if writes:
                    await connection.execute(
                        pg_insert(_segments).on_conflict_do_nothing(
                            constraint="archive_segments_unique"
                        ),
                        [
                            {
                                "station_id": station_id,
                                "epoch": epoch,
                                "relative_path": write.relative_path,
                                "hour_start": write.hour_start,
                                "first_seq": write.first_seq,
                                "last_seq": write.last_seq,
                                "first_recv_utc_ns": write.first_recv_utc_ns,
                                "last_recv_utc_ns": write.last_recv_utc_ns,
                                "record_count": write.record_count,
                                "compressed_bytes": write.compressed_bytes,
                                "uncompressed_bytes": write.uncompressed_bytes,
                            }
                            for write in writes
                        ],
                    )
                if advance_to is not None:
                    await connection.execute(
                        _advance_watermark(station_id, epoch, advance_to)
                    )
        except SQLAlchemyError as error:
            raise StoreError(f"could not index archive segments: {error}") from error

    async def _indexed_ranges(
        self, station_id: str, epoch: str, first_seq: int, last_seq: int
    ) -> set[tuple[str, int, int]]:
        """Index rows already covering part of `[first_seq, last_seq]`."""
        try:
            async with self.engine.connect() as connection:
                rows = (
                    await connection.execute(
                        sa.select(
                            _segments.c.relative_path,
                            _segments.c.first_seq,
                            _segments.c.last_seq,
                        ).where(
                            _segments.c.station_id == station_id,
                            _segments.c.epoch == epoch,
                            _segments.c.first_seq >= first_seq,
                            _segments.c.last_seq <= last_seq,
                        )
                    )
                ).all()
        except SQLAlchemyError as error:
            raise StoreError(f"could not read the archive index: {error}") from error
        return {
            (row.relative_path, int(row.first_seq), int(row.last_seq)) for row in rows
        }

    async def _watermark(self, station_id: str, epoch: str) -> int:
        try:
            async with self.engine.connect() as connection:
                found = await connection.scalar(
                    sa.select(_epochs.c.highest_contiguous_seq).where(
                        _epochs.c.station_id == station_id,
                        _epochs.c.epoch == epoch,
                    )
                )
        except SQLAlchemyError as error:
            raise StoreError(f"could not read the watermark: {error}") from error
        return EMPTY_WATERMARK if found is None else int(found)

    async def _set_watermark(self, station_id: str, epoch: str, seq: int) -> None:
        try:
            async with self.engine.begin() as connection:
                await connection.execute(_advance_watermark(station_id, epoch, seq))
        except SQLAlchemyError as error:
            raise StoreError(f"could not advance the watermark: {error}") from error

    async def _extend_through_gaps(
        self, station_id: str, epoch: str, watermark: int
    ) -> int:
        """Walk the watermark forward across any gap that begins just past it.

        protocol §11: a recorded gap's sequence numbers are permanently absent
        and count as satisfied. Without this the watermark sticks at the hole
        for the life of the epoch, every reconnect asks for records the relay
        cannot supply, and the relay answers with the same gap for ever.
        """
        try:
            async with self.engine.connect() as connection:
                rows = (
                    await connection.execute(
                        sa.select(_gaps.c.from_seq, _gaps.c.to_seq)
                        .where(
                            _gaps.c.station_id == station_id,
                            _gaps.c.epoch == epoch,
                        )
                        .order_by(_gaps.c.from_seq)
                    )
                ).all()
        except SQLAlchemyError as error:
            raise StoreError(f"could not read gaps: {error}") from error

        extended = watermark
        moved = True
        while moved:
            moved = False
            for from_seq, to_seq in rows:
                if from_seq <= extended + 1 < to_seq:
                    # to_seq is exclusive, so the last missing record is
                    # to_seq - 1 and the watermark becomes exactly that.
                    extended = to_seq - 1
                    moved = True

        if extended != watermark:
            await self._set_watermark(station_id, epoch, extended)
        return extended

    async def _ensure_epoch(self, station_id: str, epoch: str) -> None:
        try:
            async with self.engine.begin() as connection:
                await connection.execute(
                    pg_insert(_epochs)
                    .values(
                        station_id=station_id,
                        epoch=epoch,
                        highest_contiguous_seq=EMPTY_WATERMARK,
                        last_seen_at=_now(),
                    )
                    .on_conflict_do_nothing(index_elements=["station_id", "epoch"])
                )
        except SQLAlchemyError as error:
            raise StoreError(f"could not register the epoch: {error}") from error

    async def _touch(self, station_id: str, epoch: str) -> None:
        try:
            async with self.engine.begin() as connection:
                await connection.execute(
                    sa.update(_epochs)
                    .where(
                        _epochs.c.station_id == station_id,
                        _epochs.c.epoch == epoch,
                    )
                    .values(last_seen_at=_now())
                )
        except SQLAlchemyError as error:
            raise StoreError(f"could not refresh the epoch: {error}") from error

    async def _record_event(
        self,
        station_id: str,
        epoch: str | None,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        try:
            async with self.engine.begin() as connection:
                await connection.execute(
                    sa.insert(_events).values(
                        station_id=station_id,
                        epoch=epoch,
                        event_type=event_type,
                        payload=payload,
                    )
                )
        except SQLAlchemyError as error:
            raise StoreError(f"could not record {event_type}: {error}") from error


def _advance_watermark(station_id: str, epoch: str, seq: int) -> sa.Update:
    return (
        sa.update(_epochs)
        .where(
            _epochs.c.station_id == station_id,
            _epochs.c.epoch == epoch,
            # Never move backwards. Two connections from one station - a
            # reconnect racing a half-closed session - must not let a stale
            # value undo progress.
            _epochs.c.highest_contiguous_seq < seq,
        )
        .values(highest_contiguous_seq=seq, last_seen_at=_now())
    )


def _now() -> datetime:
    return datetime.now(tz=UTC)

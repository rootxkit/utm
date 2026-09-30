"""Archive retention: bounded by policy, never by disk exhaustion.

Two rules, in this order:

1. **Age.** Segments stored longer ago than `telemetry_retention_days` are
   deleted. That setting is shared with P1-04's `drone_state` retention and
   must stay shared: the archive must not outlive the telemetry it explains,
   nor the telemetry the archive. Half a record is worse than none, because
   it reads as complete.

   Age is measured on `stored_at`, the Gateway's clock, never on the hour
   the records claim to belong to. `hour_start` comes from `recv_utc_ns`,
   the ground PC's wall clock, which relay-v1 §9 says may be wrong; keyed on
   that, a station whose clock was years behind had freshly acknowledged
   telemetry unlinked at the next sweep.

2. **Size ceiling, per station.** If a station is over its ceiling after the
   age sweep, the oldest whole segments go first, oldest epoch first, until it
   is under. This is the bound that applies when a station produces more than
   expected before the period expires.

**Whole segments only.** A partially deleted hour is a hole with nothing
recording that it is a hole.

**Every deletion writes an `ingest_events` row.** This is not decoration and
not optional. When the relay hits its queue cap it reports a `gap` and the
Gateway records it — the relay has somewhere to report to. The Gateway has
nobody downstream, so that row is the entire audit trail for data this system
destroyed on purpose. Without it, "there was never any telemetry here" and "we
deleted it in March" are the same answer.

The index row survives deletion, marked. A time-range query can then still say
"3600 records were here and were deleted on this date for this reason", which
is a different statement from "nothing was ever recorded".

## Holds

A hold exempts an epoch from both rules, so an investigation outlives the
policy without anybody editing the policy — editing the retention period to
protect one flight is how a fleet keeps everything for a year by accident.

Every hold expires, and records who set it, when, and why. An indefinite hold
becomes permanent by neglect, which is the failure the flag exists to prevent.
`holds_expiring_within` drives the warning that has to arrive before the date,
not after it.

**An expired hold deletes nothing by itself.** It returns the epoch to the
normal rules, which then apply as they would have anyway. Expiry that triggered
deletion would let a forgotten date destroy evidence.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Protocol

import sqlalchemy as sa
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from common import get_logger
from gateway.archive import RawArchive
from gateway.ingest_store import StoreError

_log = get_logger(__name__)

BYTES_PER_GIB: Final = 1024**3

# How far ahead a hold's expiry starts being warned about. Long enough that
# whoever set it can renew or release it deliberately rather than discovering
# the lapse afterwards.
HOLD_WARNING_DAYS: Final = 14

_epochs = sa.table(
    "relay_epochs",
    sa.column("station_id", sa.Text),
    sa.column("epoch", sa.Text),
    sa.column("hold_until", sa.DateTime(timezone=True)),
    sa.column("hold_set_by", sa.Text),
    sa.column("hold_set_at", sa.DateTime(timezone=True)),
    sa.column("hold_reason", sa.Text),
)

_segments = sa.table(
    "archive_segments",
    sa.column("id", sa.BigInteger),
    sa.column("station_id", sa.Text),
    sa.column("epoch", sa.Text),
    sa.column("relative_path", sa.Text),
    sa.column("hour_start", sa.DateTime(timezone=True)),
    sa.column("record_count", sa.Integer),
    sa.column("compressed_bytes", sa.BigInteger),
    # Server time, set by the database when the row was indexed.
    sa.column("stored_at", sa.DateTime(timezone=True)),
    sa.column("deleted_at", sa.DateTime(timezone=True)),
    sa.column("deleted_reason", sa.Text),
)

_events = sa.table(
    "ingest_events",
    sa.column("station_id", sa.Text),
    sa.column("epoch", sa.Text),
    sa.column("event_type", sa.Text),
    sa.column("payload", sa.JSON),
)


# id, station_id, epoch, relative_path, hour_start, record_count, compressed_bytes
_SegmentRow = sa.Row[tuple[int, str, str, str, datetime, int, int]]


def _by_path(rows: list[_SegmentRow]) -> list[list[_SegmentRow]]:
    """Group index rows by the file they describe, keeping first-seen order."""
    groups: dict[str, list[_SegmentRow]] = {}
    for row in rows:
        groups.setdefault(row.relative_path, []).append(row)
    return list(groups.values())


@dataclass(frozen=True, slots=True)
class Hold:
    """An epoch exempt from retention, and the date that exemption lapses."""

    station_id: str
    epoch: str
    hold_until: datetime
    set_by: str
    set_at: datetime
    reason: str

    def days_remaining(self, *, now: datetime | None = None) -> int:
        moment = now if now is not None else datetime.now(tz=UTC)
        return (self.hold_until - moment).days


@dataclass(frozen=True, slots=True)
class SweepResult:
    """What one retention run removed, per reason."""

    deleted_by_age: int = 0
    deleted_by_ceiling: int = 0
    bytes_reclaimed: int = 0
    records_destroyed: int = 0
    skipped_held: int = 0
    # Segments the index listed but whose file was already gone. Expected
    # during crash recovery - the sweep is restartable precisely because this
    # is tolerated - and a sign the index and the disk disagree at any other
    # time. Never silent: see `_report_missing_files`.
    already_missing: int = 0

    @property
    def deleted_total(self) -> int:
        return self.deleted_by_age + self.deleted_by_ceiling


@dataclass
class ArchiveRetention:
    """Applies the retention policy. Never on the ingest path."""

    engine: AsyncEngine
    archive: RawArchive
    retention_days: int
    max_bytes_per_station: int

    async def sweep(
        self, *, now: datetime | None = None, only_station: str | None = None
    ) -> SweepResult:
        """Apply both rules and return what was removed.

        `only_station` bounds the sweep to one station. Production sweeps
        everything, but a caller working on one station's data - a test, or an
        operator repairing a single station - should not be able to reach the
        rest of the fleet.

        That is not hypothetical. These tests ran unscoped against a shared
        development database and applied a 12 KiB ceiling, meant for a few
        synthetic segments, to a live station holding 3.3 MB from a hardware
        run: 14,288 index rows across successive runs were marked deleted
        while their files sat untouched on disk. Nothing failed, because
        deleting a segment whose file is already gone is deliberately not an
        error - the sweep has to be re-runnable after a crash.
        """
        moment = now if now is not None else datetime.now(tz=UTC)
        by_age = await self._sweep_by_age(moment, only_station)
        by_ceiling = await self._sweep_by_ceiling(moment, only_station)
        result = SweepResult(
            deleted_by_age=by_age.deleted_by_age,
            deleted_by_ceiling=by_ceiling.deleted_by_ceiling,
            bytes_reclaimed=by_age.bytes_reclaimed + by_ceiling.bytes_reclaimed,
            records_destroyed=by_age.records_destroyed + by_ceiling.records_destroyed,
            skipped_held=by_age.skipped_held + by_ceiling.skipped_held,
            already_missing=by_age.already_missing + by_ceiling.already_missing,
        )
        if result.already_missing:
            await self._report_missing_files(result, only_station)
        return result

    async def _report_missing_files(
        self, result: SweepResult, only_station: str | None
    ) -> None:
        """Record that the index and the disk disagreed.

        Deleting a segment whose file is already gone is deliberately not an
        error - retention has to be re-runnable after a crash, and on the
        second run every file it removed is already absent. That tolerance is
        correct and it is also what let 14,288 index rows be marked deleted
        against a live station without anything failing.

        So the tolerance stays and the silence goes. In crash recovery a
        non-zero count is expected and this row is a footnote. In an ordinary
        sweep it means segments the index still listed are not on disk, which
        is either a bug or data loss, and it should be visible on the first
        sweep rather than the ten-thousandth.
        """
        _log.warning(
            "archive index and disk disagree",
            extra={
                "already_missing": result.already_missing,
                "deleted_total": result.deleted_total,
                "station_id": only_station,
                "archive_root": str(self.archive.root),
            },
        )
        await self._record_event(
            only_station or "*",
            None,
            "retention.missing_files",
            {
                "already_missing": result.already_missing,
                "deleted_total": result.deleted_total,
                "archive_root": str(self.archive.root),
                "note": (
                    "segments the index listed were not on disk. Expected "
                    "during crash recovery; otherwise the index and the "
                    "archive disagree."
                ),
            },
        )

    # --- holds -------------------------------------------------------------

    async def place_hold(
        self,
        station_id: str,
        epoch: str,
        *,
        until: datetime,
        set_by: str,
        reason: str,
        now: datetime | None = None,
    ) -> None:
        """Exempt an epoch from retention until a date.

        All four fields are required, and the database enforces that a hold is
        all-or-nothing. A hold with no owner and no reason is one nobody can
        evaluate when the date arrives, so it gets renewed for ever by whoever
        is unwilling to be the person who deleted it.
        """
        moment = now if now is not None else datetime.now(tz=UTC)
        if until <= moment:
            raise ValueError(
                f"a hold must expire in the future: until={until.isoformat()} "
                f"is not after {moment.isoformat()}"
            )
        if not set_by.strip():
            raise ValueError("a hold must name who set it")
        if not reason.strip():
            raise ValueError("a hold must say why")

        try:
            async with self.engine.begin() as connection:
                updated = await connection.execute(
                    sa.update(_epochs)
                    .where(
                        _epochs.c.station_id == station_id,
                        _epochs.c.epoch == epoch,
                    )
                    .values(
                        hold_until=until,
                        hold_set_by=set_by,
                        hold_set_at=moment,
                        hold_reason=reason,
                    )
                )
        except SQLAlchemyError as error:
            raise StoreError(f"could not place hold: {error}") from error

        if updated.rowcount == 0:
            raise StoreError(
                f"no such epoch to hold: station={station_id} epoch={epoch}"
            )
        await self._record_event(
            station_id,
            epoch,
            "retention.hold_placed",
            {
                "hold_until": until.isoformat(),
                "set_by": set_by,
                "reason": reason,
            },
        )

    async def release_hold(self, station_id: str, epoch: str) -> None:
        """Return an epoch to the normal rules immediately."""
        try:
            async with self.engine.begin() as connection:
                await connection.execute(
                    sa.update(_epochs)
                    .where(
                        _epochs.c.station_id == station_id,
                        _epochs.c.epoch == epoch,
                    )
                    .values(
                        hold_until=None,
                        hold_set_by=None,
                        hold_set_at=None,
                        hold_reason=None,
                    )
                )
        except SQLAlchemyError as error:
            raise StoreError(f"could not release hold: {error}") from error
        await self._record_event(station_id, epoch, "retention.hold_released", {})

    async def holds_expiring_within(
        self, days: int = HOLD_WARNING_DAYS, *, now: datetime | None = None
    ) -> list[Hold]:
        """Holds whose date is approaching, for the periodic warning.

        The warning has to arrive *before* the date. A hold that lapses
        unnoticed is indistinguishable from one that was never needed, and the
        evidence is then subject to the normal rules without anyone deciding
        that it should be.
        """
        moment = now if now is not None else datetime.now(tz=UTC)
        horizon = moment + timedelta(days=days)
        try:
            async with self.engine.connect() as connection:
                rows = (
                    await connection.execute(
                        sa.select(
                            _epochs.c.station_id,
                            _epochs.c.epoch,
                            _epochs.c.hold_until,
                            _epochs.c.hold_set_by,
                            _epochs.c.hold_set_at,
                            _epochs.c.hold_reason,
                        )
                        .where(
                            _epochs.c.hold_until.is_not(None),
                            _epochs.c.hold_until <= horizon,
                        )
                        .order_by(_epochs.c.hold_until)
                    )
                ).all()
        except SQLAlchemyError as error:
            raise StoreError(f"could not read holds: {error}") from error

        return [
            Hold(
                station_id=row.station_id,
                epoch=row.epoch,
                hold_until=row.hold_until,
                set_by=row.hold_set_by,
                set_at=row.hold_set_at,
                reason=row.hold_reason,
            )
            for row in rows
        ]

    # --- the two rules -----------------------------------------------------

    async def _sweep_by_age(
        self, now: datetime, only_station: str | None = None
    ) -> SweepResult:
        cutoff = now - timedelta(days=self.retention_days)
        candidates = await self._live_segments(
            older_than=cutoff, station_id=only_station
        )
        deleted, reclaimed, records, held, missing = await self._delete_all(
            candidates, reason="age", now=now
        )
        return SweepResult(
            deleted_by_age=deleted,
            bytes_reclaimed=reclaimed,
            records_destroyed=records,
            skipped_held=held,
            already_missing=missing,
        )

    async def _sweep_by_ceiling(
        self, now: datetime, only_station: str | None = None
    ) -> SweepResult:
        deleted = reclaimed = records = held = missing = 0

        for station_id, total_bytes in await self._station_sizes(only_station):
            if total_bytes <= self.max_bytes_per_station:
                continue

            over_by = total_bytes - self.max_bytes_per_station
            # Oldest stored first. Whole files: every row of a path goes
            # together, because the file is one unit on disk.
            for rows in _by_path(await self._live_segments(station_id=station_id)):
                if over_by <= 0:
                    break
                if await self._is_held(rows[0].station_id, rows[0].epoch, now):
                    held += len(rows)
                    continue
                freed, count, existed = await self._delete_path(rows, reason="ceiling")
                deleted += len(rows)
                reclaimed += freed
                records += count
                missing += 0 if existed else 1
                over_by -= sum(row.compressed_bytes for row in rows)

        return SweepResult(
            deleted_by_ceiling=deleted,
            bytes_reclaimed=reclaimed,
            records_destroyed=records,
            skipped_held=held,
            already_missing=missing,
        )

    # --- internals ---------------------------------------------------------

    async def _live_segments(
        self,
        *,
        older_than: datetime | None = None,
        station_id: str | None = None,
    ) -> list[_SegmentRow]:
        query = (
            sa.select(
                _segments.c.id,
                _segments.c.station_id,
                _segments.c.epoch,
                _segments.c.relative_path,
                _segments.c.hour_start,
                _segments.c.record_count,
                _segments.c.compressed_bytes,
            )
            .where(_segments.c.deleted_at.is_(None))
            # Oldest by the Gateway's clock first, so the ceiling too takes
            # what was stored longest ago rather than what a station's clock
            # claims is oldest.
            .order_by(_segments.c.stored_at, _segments.c.id)
        )
        if older_than is not None:
            # Age on the Gateway's clock, and only whole files: a path with
            # any live row stored inside the window stays, because deleting
            # the file would take that row's bytes with it, unmarked.
            recently_stored = sa.select(_segments.c.relative_path).where(
                _segments.c.deleted_at.is_(None),
                _segments.c.stored_at >= older_than,
            )
            query = query.where(
                _segments.c.stored_at < older_than,
                _segments.c.relative_path.not_in(recently_stored),
            )
        if station_id is not None:
            query = query.where(_segments.c.station_id == station_id)

        try:
            async with self.engine.connect() as connection:
                return list((await connection.execute(query)).all())
        except SQLAlchemyError as error:
            raise StoreError(f"could not list segments: {error}") from error

    async def _station_sizes(
        self, only_station: str | None = None
    ) -> list[tuple[str, int]]:
        query = (
            sa.select(
                _segments.c.station_id,
                sa.func.sum(_segments.c.compressed_bytes),
            )
            .where(_segments.c.deleted_at.is_(None))
            .group_by(_segments.c.station_id)
        )
        if only_station is not None:
            query = query.where(_segments.c.station_id == only_station)
        try:
            async with self.engine.connect() as connection:
                rows = (await connection.execute(query)).all()
        except SQLAlchemyError as error:
            raise StoreError(f"could not measure stations: {error}") from error
        return [(row[0], int(row[1] or 0)) for row in rows]

    async def _is_held(self, station_id: str, epoch: str, now: datetime) -> bool:
        try:
            async with self.engine.connect() as connection:
                held_until = await connection.scalar(
                    sa.select(_epochs.c.hold_until).where(
                        _epochs.c.station_id == station_id,
                        _epochs.c.epoch == epoch,
                    )
                )
        except SQLAlchemyError as error:
            raise StoreError(f"could not check hold: {error}") from error
        # An expired hold is not a hold. It does not delete anything by itself;
        # it simply stops exempting, and the ordinary rules resume.
        return held_until is not None and held_until > now

    async def _delete_all(
        self,
        segments: list[_SegmentRow],
        *,
        reason: str,
        now: datetime,
    ) -> tuple[int, int, int, int, int]:
        deleted = reclaimed = records = held = missing = 0
        for rows in _by_path(segments):
            if await self._is_held(rows[0].station_id, rows[0].epoch, now):
                held += len(rows)
                continue
            freed, count, existed = await self._delete_path(rows, reason=reason)
            deleted += len(rows)
            reclaimed += freed
            records += count
            missing += 0 if existed else 1
        return deleted, reclaimed, records, held, missing

    async def _delete_path(
        self, rows: list[_SegmentRow], *, reason: str
    ) -> tuple[int, int, bool]:
        """Delete one file, mark every index row of it, record the event.

        In that order. File first: a crash after deleting but before marking
        leaves index rows for bytes that are gone, which the next sweep
        retries harmlessly. The reverse leaves a file nothing points at,
        which nothing will ever clean up.

        One file, all its rows at once. A segment has one index row per
        batch appended to it, so unlinking on the first row and then
        visiting the rest counted every later row as `already_missing`: each
        sweep logged "index and disk disagree" and wrote a
        `retention.missing_files` event for an archive that was fine, and a
        warning that is always false is one nobody reads.
        """
        first = rows[0]
        # Under the station lock, so the unlink cannot race an append to
        # the same hour file from a session of this station.
        async with self.archive.station_lock(first.station_id):
            # Off the event loop: a stat and an unlink on a slow or remote
            # disk are time in which no station is served.
            existed = await asyncio.to_thread(
                (self.archive.root / first.relative_path).exists
            )
            freed = await asyncio.to_thread(
                self.archive.delete_segment, first.relative_path
            )

            try:
                async with self.engine.begin() as connection:
                    await connection.execute(
                        sa.update(_segments)
                        .where(_segments.c.id.in_([row.id for row in rows]))
                        .values(deleted_at=sa.func.now(), deleted_reason=reason)
                    )
            except SQLAlchemyError as error:
                raise StoreError(f"could not mark segment deleted: {error}") from error

        record_count = sum(row.record_count for row in rows)
        await self._record_event(
            first.station_id,
            first.epoch,
            f"retention.deleted.{reason}",
            {
                "relative_path": first.relative_path,
                "hour_start": first.hour_start.isoformat(),
                "record_count": record_count,
                "index_rows": len(rows),
                "bytes_reclaimed": freed,
            },
        )
        return freed, record_count, existed

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


# --- running it ------------------------------------------------------------
#
# Everything above had no production caller (S-07). `sweep` and
# `purge_closed_epochs` were designed, tested against a real database, and
# never scheduled, so the archive was bounded by policy on paper and by the
# disk in practice - exactly the failure the module docstring opens with.


class Sweeper(Protocol):
    """What the schedule needs from `ArchiveRetention`."""

    async def sweep(
        self, *, now: datetime | None = None, only_station: str | None = None
    ) -> SweepResult: ...

    async def holds_expiring_within(
        self, days: int = HOLD_WARNING_DAYS, *, now: datetime | None = None
    ) -> list[Hold]: ...


class EpochPurger(Protocol):
    """What the schedule needs from the ingest store."""

    async def purge_closed_epochs(self) -> int: ...


@dataclass
class RetentionSchedule:
    """Runs the retention pass on a timer, and keeps running when one fails.

    A pass that fails - the database away for a minute - is logged and
    retried at the next interval. Nothing here is on the ingest path, and a
    sweep that stopped for good after one bad pass would return the archive
    to being bounded by the disk, silently.
    """

    retention: Sweeper
    store: EpochPurger
    interval_s: float
    # Production sweeps every station. A caller working on one station's
    # data - a test - must not be able to reach the rest; see `sweep`.
    only_station: str | None = None
    passes: int = field(default=0, init=False)
    failures: int = field(default=0, init=False)

    async def run_until(self, stop: asyncio.Event) -> None:
        """One pass now, then one per interval, until `stop` is set."""
        while not stop.is_set():
            await self.run_once()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), self.interval_s)

    async def run_once(self) -> None:
        try:
            result = await self.retention.sweep(only_station=self.only_station)
            purged = await self.store.purge_closed_epochs()
            expiring = await self.retention.holds_expiring_within()
        except Exception as error:
            # Anything: a StoreError or ArchiveError from the sweep, but
            # also an OSError from a stat on a disk that has gone away. A
            # pass that stops the schedule for good returns the archive to
            # being bounded by the disk, silently, which is the one outcome
            # this task exists to prevent.
            self.failures += 1
            _log.error(
                "retention pass failed; retrying at the next interval",
                extra={"error": repr(error), "interval_s": self.interval_s},
            )
            return
        finally:
            self.passes += 1

        _log.info(
            "retention pass complete",
            extra={
                "deleted_by_age": result.deleted_by_age,
                "deleted_by_ceiling": result.deleted_by_ceiling,
                "bytes_reclaimed": result.bytes_reclaimed,
                "records_destroyed": result.records_destroyed,
                "skipped_held": result.skipped_held,
                "already_missing": result.already_missing,
                "epochs_purged": purged,
                "station_id": self.only_station,
            },
        )
        for hold in expiring:
            # The warning that has to arrive before the date, not after it.
            _log.warning(
                "retention hold expires soon",
                extra={
                    "station_id": hold.station_id,
                    "epoch": hold.epoch,
                    "hold_until": hold.hold_until.isoformat(),
                    "days_remaining": hold.days_remaining(),
                    "set_by": hold.set_by,
                    "reason": hold.reason,
                },
            )

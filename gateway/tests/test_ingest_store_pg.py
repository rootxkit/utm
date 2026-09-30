"""`TimescaleIngestStore` against a real TimescaleDB.

Marked `postgres`: these need a database, so they are excluded from the default
run the way `sitl` tests are. They are not optional - CI runs them with a
service container - but a developer with no stack up should not see failures
for a dependency they were never told to start.

A real database rather than a fake, because everything these tests are about is
in the database: the conflict clause that makes a re-reported gap one row, the
guard that stops a watermark moving backwards, the constraints that catch an
epoch of the wrong shape. A fake would agree with whatever the code did.
"""

from __future__ import annotations

import dataclasses
import threading
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine

from gateway.archive import RawArchive, SegmentWrite
from gateway.ingest_store_pg import EMPTY_WATERMARK, TimescaleIngestStore
from gateway.relay_messages import Gap
from gateway.relay_records import Record
from gateway.station_state import LinkState, LossEvent, LossKind

pytestmark = pytest.mark.postgres

EPOCH = "9f2c1b7d4e6a58039ab1c2d3e4f50617"
OTHER_EPOCH = "00112233445566778899aabbccddeeff"
BASE_NS = 1_790_000_000_000_000_000


@pytest.fixture
async def station(
    engine: AsyncEngine, request: pytest.FixtureRequest
) -> AsyncIterator[str]:
    """A station id unique to this test, cleaned up afterwards.

    Tests share one database, so they must not share a station. Deriving the id
    from the node id means they can also run in any order, and in parallel.
    """
    name = f"test-{abs(hash(request.node.nodeid)) % 10**12}"
    try:
        yield name
    finally:
        async with engine.begin() as connection:
            for table in ("archive_segments", "ingest_events", "relay_epochs"):
                # relay_epoch_gaps cascades from relay_epochs.
                await connection.execute(
                    sa.text(f"DELETE FROM {table} WHERE station_id = :s"),
                    {"s": name},
                )


@pytest.fixture
def store(engine: AsyncEngine, archive_root: Path) -> TimescaleIngestStore:
    """Rooted at the guarded temporary path, never a real archive."""
    return TimescaleIngestStore(engine=engine, archive=RawArchive(root=archive_root))


def records(first_seq: int, count: int) -> list[Record]:
    return [
        Record(
            seq=first_seq + n,
            recv_utc_ns=BASE_NS + n * 1_000_000,
            datagram=bytes([(first_seq + n) % 256]) * 20,
        )
        for n in range(count)
    ]


async def events_of(engine: AsyncEngine, station: str) -> list[str]:
    async with engine.connect() as connection:
        rows = await connection.execute(
            sa.text(
                "SELECT event_type FROM ingest_events WHERE station_id = :s ORDER BY id"
            ),
            {"s": station},
        )
        return [row.event_type for row in rows]


# --- resume point ----------------------------------------------------------


async def test_an_unknown_epoch_resumes_from_zero(
    store: TimescaleIngestStore, station: str
) -> None:
    assert await store.resume_from_seq(station, EPOCH) == 0


async def test_the_resume_point_follows_what_was_stored(
    store: TimescaleIngestStore, station: str
) -> None:
    await store.store_records(station, EPOCH, records(0, 100))
    assert await store.resume_from_seq(station, EPOCH) == 100


async def test_the_resume_point_survives_a_new_store_object(
    store: TimescaleIngestStore, engine: AsyncEngine, archive_root: Path, station: str
) -> None:
    """§4.2: a restarted Gateway must answer the same number.

    Nothing may be cached in the process: this is what a restart looks like.
    """
    await store.store_records(station, EPOCH, records(0, 42))

    restarted = TimescaleIngestStore(
        engine=engine, archive=RawArchive(root=archive_root)
    )

    assert await restarted.resume_from_seq(station, EPOCH) == 42


async def test_epochs_are_independent(
    store: TimescaleIngestStore, station: str
) -> None:
    """§4: a recreated queue restarts seq at 0 and must not be deduped away."""
    await store.store_records(station, EPOCH, records(0, 100))
    assert await store.resume_from_seq(station, OTHER_EPOCH) == 0


# --- dedupe ----------------------------------------------------------------


async def test_a_retransmission_is_dropped(
    store: TimescaleIngestStore, station: str
) -> None:
    await store.store_records(station, EPOCH, records(0, 50))

    watermark = await store.store_records(station, EPOCH, records(0, 50))

    assert watermark == 49
    async with store.engine.connect() as connection:
        stored = await connection.scalar(
            sa.text(
                "SELECT sum(record_count) FROM archive_segments "
                "WHERE station_id = :s AND epoch = :e"
            ),
            {"s": station, "e": EPOCH},
        )
    # 50 stored once. A second copy would mean the archive doubles every time
    # an ack goes missing.
    assert stored == 50


async def test_a_partially_overlapping_batch_stores_only_the_new_part(
    store: TimescaleIngestStore, station: str
) -> None:
    await store.store_records(station, EPOCH, records(0, 50))

    watermark = await store.store_records(station, EPOCH, records(40, 30))

    assert watermark == 69
    stored = store.archive.read_segment(_only_segment_path(store, station))
    assert [record.seq for record in stored] == list(range(70))


def _only_segment_path(store: TimescaleIngestStore, station: str) -> str:
    root = store.archive.root / station / EPOCH
    found = sorted(root.rglob("*.zst"))
    assert len(found) == 1, f"expected one segment, found {found}"
    return str(found[0].relative_to(store.archive.root)).replace("\\", "/")


async def test_the_watermark_never_moves_backwards(
    store: TimescaleIngestStore, station: str
) -> None:
    """Two connections from one station must not let a stale value undo work.

    A reconnect racing a half-closed session is the realistic case.
    """
    await store.store_records(station, EPOCH, records(0, 100))
    await store._set_watermark(station, EPOCH, 10)
    assert await store.resume_from_seq(station, EPOCH) == 100


# --- gaps ------------------------------------------------------------------


async def test_a_gap_advances_the_resume_point(
    store: TimescaleIngestStore, station: str
) -> None:
    """§11, and the arithmetic that keeps a hole from being permanent.

    to_seq is exclusive: a gap over [50, 80) means 79 is the last missing
    record, so the watermark becomes 79 and the next wanted seq is 80.
    """
    await store.store_records(station, EPOCH, records(0, 50))

    await store.record_gap(
        station, EPOCH, Gap(epoch=EPOCH, from_seq=50, to_seq=80, reason="queue_cap")
    )

    assert await store.resume_from_seq(station, EPOCH) == 80


async def test_the_same_gap_reported_twice_is_one_row(
    store: TimescaleIngestStore, station: str
) -> None:
    await store.store_records(station, EPOCH, records(0, 50))
    gap = Gap(epoch=EPOCH, from_seq=50, to_seq=80, reason="queue_cap")

    await store.record_gap(station, EPOCH, gap)
    await store.record_gap(station, EPOCH, gap)

    async with store.engine.connect() as connection:
        count = await connection.scalar(
            sa.text(
                "SELECT count(*) FROM relay_epoch_gaps "
                "WHERE station_id = :s AND epoch = :e"
            ),
            {"s": station, "e": EPOCH},
        )
    assert count == 1


async def test_records_after_a_gap_continue_the_watermark(
    store: TimescaleIngestStore, station: str
) -> None:
    await store.store_records(station, EPOCH, records(0, 50))
    await store.record_gap(
        station, EPOCH, Gap(epoch=EPOCH, from_seq=50, to_seq=80, reason="queue_cap")
    )

    watermark = await store.store_records(station, EPOCH, records(80, 10))

    assert watermark == 89
    assert await store.resume_from_seq(station, EPOCH) == 90


async def test_a_gap_arriving_before_any_records_still_advances(
    store: TimescaleIngestStore, station: str
) -> None:
    """The cap can destroy everything the relay held, including record zero."""
    await store.record_gap(
        station, EPOCH, Gap(epoch=EPOCH, from_seq=0, to_seq=30, reason="queue_cap")
    )
    assert await store.resume_from_seq(station, EPOCH) == 30


async def test_consecutive_gaps_chain(
    store: TimescaleIngestStore, station: str
) -> None:
    await store.record_gap(
        station, EPOCH, Gap(epoch=EPOCH, from_seq=0, to_seq=30, reason="queue_cap")
    )
    await store.record_gap(
        station, EPOCH, Gap(epoch=EPOCH, from_seq=30, to_seq=45, reason="queue_cap")
    )
    assert await store.resume_from_seq(station, EPOCH) == 45


# --- non-conforming input --------------------------------------------------


async def test_a_sequence_discontinuity_is_archived_but_not_acknowledged(
    store: TimescaleIngestStore, engine: AsyncEngine, station: str
) -> None:
    """A conforming relay cannot do this, so it is recorded rather than trusted.

    The records are archived - never discard telemetry - but the watermark
    stays put, so the next `resume_from_seq` still asks for the missing range
    and nothing is acknowledged that was not received.
    """
    await store.store_records(station, EPOCH, records(0, 10))

    watermark = await store.store_records(station, EPOCH, records(50, 5))

    assert watermark == 9
    assert await store.resume_from_seq(station, EPOCH) == 10
    assert "sequence_discontinuity" in await events_of(engine, station)
    # Archived all the same.
    async with engine.connect() as connection:
        total = await connection.scalar(
            sa.text(
                "SELECT sum(record_count) FROM archive_segments "
                "WHERE station_id = :s AND epoch = :e"
            ),
            {"s": station, "e": EPOCH},
        )
    assert total == 15


async def test_an_epoch_of_the_wrong_shape_is_refused_by_the_database(
    store: TimescaleIngestStore, station: str
) -> None:
    """The constraint, not the application, is what enforces this."""
    from gateway.ingest_store import StoreError

    with pytest.raises(StoreError):
        await store.store_records(station, "NOT-AN-EPOCH", records(0, 1))


# --- epoch lifecycle -------------------------------------------------------


async def test_declaring_a_new_epoch_closes_the_previous_one(
    store: TimescaleIngestStore, engine: AsyncEngine, station: str
) -> None:
    await store.open_epoch(station, EPOCH)
    await store.store_records(station, EPOCH, records(0, 5))

    await store.open_epoch(station, OTHER_EPOCH)

    async with engine.connect() as connection:
        closed = await connection.scalar(
            sa.text(
                "SELECT closed_at FROM relay_epochs "
                "WHERE station_id = :s AND epoch = :e"
            ),
            {"s": station, "e": EPOCH},
        )
        still_open = await connection.scalar(
            sa.text(
                "SELECT closed_at FROM relay_epochs "
                "WHERE station_id = :s AND epoch = :e"
            ),
            {"s": station, "e": OTHER_EPOCH},
        )

    assert closed is not None
    assert still_open is None
    assert "epoch_closed" in await events_of(engine, station)


async def test_a_closed_epoch_keeps_its_watermark_until_it_is_purged(
    store: TimescaleIngestStore, station: str
) -> None:
    """Closing is not dropping.

    A station that declares a new epoch and then reconnects under the old one
    still gets the right resume point, right up until retention removes it.
    """
    await store.store_records(station, EPOCH, records(0, 25))
    await store.open_epoch(station, OTHER_EPOCH)

    assert await store.resume_from_seq(station, EPOCH) == 25


async def test_purging_only_removes_epochs_past_retention(
    store: TimescaleIngestStore, engine: AsyncEngine, station: str
) -> None:
    """The paired presence/absence test for retention.

    A just-closed epoch survives; the same epoch backdated past the window does
    not, and its resume point goes back to 0 - which resends rather than loses,
    the direction this is allowed to fail in.
    """
    await store.store_records(station, EPOCH, records(0, 25))
    await store.open_epoch(station, OTHER_EPOCH)

    assert await store.purge_closed_epochs() == 0
    assert await store.resume_from_seq(station, EPOCH) == 25

    async with engine.begin() as connection:
        await connection.execute(
            sa.text(
                "UPDATE relay_epochs SET closed_at = now() - interval '400 days' "
                "WHERE station_id = :s AND epoch = :e"
            ),
            {"s": station, "e": EPOCH},
        )

    assert await store.purge_closed_epochs() == 1
    assert await store.resume_from_seq(station, EPOCH) == 0


# --- events ----------------------------------------------------------------


async def test_a_loss_is_recorded_with_its_kind(
    store: TimescaleIngestStore, engine: AsyncEngine, station: str
) -> None:
    await store.record_loss(
        station,
        EPOCH,
        LossEvent(kind=LossKind.INTAKE_DROP, datagram_count=40, detail="dropped"),
    )
    assert "loss.intake_drop" in await events_of(engine, station)


async def test_a_link_state_change_is_recorded(
    store: TimescaleIngestStore, engine: AsyncEngine, station: str
) -> None:
    await store.record_link_state(station, LinkState.UNREACHABLE, at_utc_ns=BASE_NS)
    assert "link_state" in await events_of(engine, station)


async def test_the_empty_watermark_is_minus_one_not_zero() -> None:
    """0 would claim record zero had arrived."""
    assert EMPTY_WATERMARK == -1


# --- the archive write stays off the event loop (S-04) ---------------------


class ThreadRecordingArchive(RawArchive):
    """Notes which thread `append` ran on."""

    def __init__(self, root: Path) -> None:
        super().__init__(root=root)
        self.append_threads: list[threading.Thread] = []

    def append(
        self, station_id: str, epoch: str, records: list[Record]
    ) -> list[SegmentWrite]:
        self.append_threads.append(threading.current_thread())
        return super().append(station_id, epoch, records)


async def test_the_archive_append_runs_off_the_event_loop_thread(
    engine: AsyncEngine, archive_root: Path, station: str
) -> None:
    """Compression and fsync block; on the loop they stall every station."""
    archive = ThreadRecordingArchive(archive_root)
    store = TimescaleIngestStore(engine=engine, archive=archive)

    await store.store_records(station, EPOCH, records(0, 10))

    assert len(archive.append_threads) == 1
    assert archive.append_threads[0] is not threading.current_thread()
    # And it still stored: the thread hop must not lose the write.
    assert await store.resume_from_seq(station, EPOCH) == 10


# --- a resend after a crash must not wedge the station (S-05) --------------


async def _rewind_watermark(engine: AsyncEngine, station: str, seq: int) -> None:
    """What a crash between the old two transactions left behind.

    Before S-05 the segment rows and the watermark were committed separately,
    so a crash after the first left the index full and the watermark short.
    `_set_watermark` refuses to move backwards, so this goes round it.
    """
    async with engine.begin() as connection:
        await connection.execute(
            sa.text(
                "UPDATE relay_epochs SET highest_contiguous_seq = :seq "
                "WHERE station_id = :s AND epoch = :e"
            ),
            {"seq": seq, "s": station, "e": EPOCH},
        )


async def _index_rows(engine: AsyncEngine, station: str) -> int:
    async with engine.connect() as connection:
        return int(
            await connection.scalar(
                sa.text(
                    "SELECT count(*) FROM archive_segments "
                    "WHERE station_id = :s AND epoch = :e"
                ),
                {"s": station, "e": EPOCH},
            )
            or 0
        )


async def test_a_resend_after_a_crash_between_index_and_watermark_recovers(
    store: TimescaleIngestStore, engine: AsyncEngine, station: str
) -> None:
    """The station that was stuck for ever.

    Segments indexed, watermark not advanced, and the relay resending the
    same batch on every reconnect. Before S-05 the insert raised on
    `archive_segments_unique`, the session closed, the relay reconnected and
    resent, and every attempt appended the same bytes to the segment.
    """
    await store.store_records(station, EPOCH, records(0, 50))
    await _rewind_watermark(engine, station, EMPTY_WATERMARK)
    assert await store.resume_from_seq(station, EPOCH) == 0

    watermark = await store.store_records(station, EPOCH, records(0, 50))

    assert watermark == 49
    assert await store.resume_from_seq(station, EPOCH) == 50
    assert await _index_rows(engine, station) == 1
    # The archive holds each record once: the resend was not appended.
    stored = store.archive.read_segment(_only_segment_path(store, station))
    assert [record.seq for record in stored] == list(range(50))


async def test_a_resend_recovers_when_only_part_of_it_is_indexed(
    store: TimescaleIngestStore, engine: AsyncEngine, station: str
) -> None:
    """Two batches indexed, the watermark left before the second, one resend."""
    await store.store_records(station, EPOCH, records(0, 30))
    await store.store_records(station, EPOCH, records(30, 20))
    await _rewind_watermark(engine, station, 29)

    watermark = await store.store_records(station, EPOCH, records(30, 20))

    assert watermark == 49
    assert await _index_rows(engine, station) == 2
    stored = store.archive.read_segment(_only_segment_path(store, station))
    assert [record.seq for record in stored] == list(range(50))


async def test_an_index_conflict_the_precheck_missed_is_not_an_error(
    engine: AsyncEngine, archive_root: Path, station: str
) -> None:
    """The conflict clause itself, with the precheck taken away.

    The precheck skips the append, so on its own it never lets the insert
    reach the constraint. This exercises the belt with the braces removed: the
    row exists, the insert must do nothing, and the watermark must still move.
    """

    class BlindStore(TimescaleIngestStore):
        async def _indexed_ranges(
            self, station_id: str, epoch: str, first_seq: int, last_seq: int
        ) -> set[tuple[str, int, int]]:
            return set()

    store = BlindStore(engine=engine, archive=RawArchive(root=archive_root))
    await store.store_records(station, EPOCH, records(0, 50))
    await _rewind_watermark(engine, station, EMPTY_WATERMARK)

    watermark = await store.store_records(station, EPOCH, records(0, 50))

    assert watermark == 49
    assert await _index_rows(engine, station) == 1


async def test_the_index_and_the_watermark_commit_together(
    engine: AsyncEngine, archive_root: Path, station: str
) -> None:
    """A failed index insert must leave the watermark where it was.

    Separate transactions would recreate the stuck state this section exists
    to remove. The insert is made to fail by a segment row that violates the
    `archive_segments_non_empty` check constraint.
    """
    from gateway.ingest_store import StoreError

    class BrokenArchive(RawArchive):
        def append(
            self, station_id: str, epoch: str, records: list[Record]
        ) -> list[SegmentWrite]:
            return [
                dataclasses.replace(write, record_count=0)
                for write in super().append(station_id, epoch, records)
            ]

    store = TimescaleIngestStore(
        engine=engine, archive=BrokenArchive(root=archive_root)
    )
    with pytest.raises(StoreError, match="index archive segments"):
        await store.store_records(station, EPOCH, records(0, 10))

    assert await store.resume_from_seq(station, EPOCH) == 0
    assert await _index_rows(engine, station) == 0

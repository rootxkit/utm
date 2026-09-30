"""Archive retention, against a real TimescaleDB.

**Every sweep here is scoped with `only_station`.** These tests share a
database with whatever else is using it, and they set a 12 KiB ceiling meant
for a few synthetic segments. Run unscoped, that ceiling was applied to a live
station holding a hardware run - 14,288 index rows marked deleted across
successive runs, while the files sat untouched on disk, and nothing failed
because deleting an already-missing segment is deliberately not an error.

A test that can reach data it did not create will eventually delete some.

Every "this is not deleted" assertion is paired with one that makes the
deletion happen. A retention sweep that silently did nothing would satisfy
every absence assertion here, and would also be the most dangerous possible
bug: the archive grows until the disk decides the policy instead.
"""

from __future__ import annotations

import asyncio
import secrets
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine

from gateway.archive import RawArchive
from gateway.ingest_store import StoreError
from gateway.ingest_store_pg import TimescaleIngestStore
from gateway.relay_records import Record
from gateway.retention import ArchiveRetention, listing_query

pytestmark = pytest.mark.postgres

EPOCH = "9f2c1b7d4e6a58039ab1c2d3e4f50617"
OTHER_EPOCH = "00112233445566778899aabbccddeeff"

NOW = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)
RETENTION_DAYS = 90


@pytest.fixture
async def station(
    engine: AsyncEngine, request: pytest.FixtureRequest
) -> AsyncIterator[str]:
    name = f"ret-{abs(hash(request.node.nodeid)) % 10**12}"
    try:
        yield name
    finally:
        async with engine.begin() as connection:
            for table in ("archive_segments", "ingest_events", "relay_epochs"):
                await connection.execute(
                    sa.text(f"DELETE FROM {table} WHERE station_id = :s"),
                    {"s": name},
                )


@pytest.fixture
def archive(archive_root: Path) -> RawArchive:
    """Rooted at the guarded temporary path, never a real archive."""
    return RawArchive(root=archive_root)


@pytest.fixture
def store(engine: AsyncEngine, archive: RawArchive) -> TimescaleIngestStore:
    return TimescaleIngestStore(engine=engine, archive=archive)


@pytest.fixture
def retention(engine: AsyncEngine, archive: RawArchive) -> ArchiveRetention:
    return ArchiveRetention(
        engine=engine,
        archive=archive,
        retention_days=RETENTION_DAYS,
        # A small ceiling, so the size rule can be exercised without writing
        # gigabytes - but comfortably larger than one segment. A ceiling below
        # the size of a single segment is satisfiable only by deleting every
        # segment, which is correct behaviour and a useless test: it cannot
        # tell "oldest first" from "all of them".
        max_bytes_per_station=12_000,
    )


def records_at(when: datetime, first_seq: int, count: int) -> list[Record]:
    """Records in one hour, with incompressible payloads.

    The datagrams are random because the size ceiling is measured on *stored*
    bytes. An earlier version used a repeated byte, which zstd shrank to
    almost nothing, so six hours of traffic sat under a 4 KiB ceiling and the
    ceiling tests silently exercised nothing. Real MAVLink is not random, but
    it is not a single repeated byte either, and a test for a size rule must
    not depend on the compressor's mood.
    """
    base_ns = int(when.timestamp()) * 1_000_000_000
    return [
        Record(
            seq=first_seq + n,
            recv_utc_ns=base_ns + n * 1_000_000,
            datagram=secrets.token_bytes(256),
        )
        for n in range(count)
    ]


async def live_segments(engine: AsyncEngine, station: str) -> int:
    async with engine.connect() as connection:
        return int(
            await connection.scalar(
                sa.text(
                    "SELECT count(*) FROM archive_segments "
                    "WHERE station_id = :s AND deleted_at IS NULL"
                ),
                {"s": station},
            )
            or 0
        )


async def backdate(engine: AsyncEngine, station: str, when: datetime) -> None:
    """Make a station's segments look as if they were stored at `when`.

    Age is keyed on `stored_at`, the Gateway's clock, which the store sets to
    now; a test that wants a segment past retention has to move it.
    """
    async with engine.begin() as connection:
        await connection.execute(
            sa.text("UPDATE archive_segments SET stored_at = :w WHERE station_id = :s"),
            {"w": when, "s": station},
        )


async def events_of(engine: AsyncEngine, station: str) -> list[str]:
    async with engine.connect() as connection:
        rows = await connection.execute(
            sa.text(
                "SELECT event_type FROM ingest_events WHERE station_id = :s ORDER BY id"
            ),
            {"s": station},
        )
        return [row.event_type for row in rows]


# --- rule 1: age -----------------------------------------------------------


async def test_a_segment_past_retention_is_deleted(
    store: TimescaleIngestStore,
    retention: ArchiveRetention,
    engine: AsyncEngine,
    archive: RawArchive,
    station: str,
) -> None:
    old = NOW - timedelta(days=RETENTION_DAYS + 1)
    await store.store_records(station, EPOCH, records_at(old, 0, 10))
    await backdate(store.engine, station, old)
    assert await live_segments(engine, station) == 1

    result = await retention.sweep(now=NOW, only_station=station)

    assert result.deleted_by_age == 1
    assert result.records_destroyed == 10
    assert result.bytes_reclaimed > 0
    assert await live_segments(engine, station) == 0
    assert not list(archive.root.rglob("*.zst"))


async def test_a_segment_within_retention_is_kept(
    store: TimescaleIngestStore,
    retention: ArchiveRetention,
    engine: AsyncEngine,
    station: str,
) -> None:
    """The paired presence test for rule 1.

    A sweep that deleted everything would pass the test above, and a sweep that
    deleted nothing would pass this one. Both are needed to say the boundary is
    in the right place.
    """
    recent = NOW - timedelta(days=RETENTION_DAYS - 1)
    await store.store_records(station, EPOCH, records_at(recent, 0, 10))

    result = await retention.sweep(now=NOW, only_station=station)

    assert result.deleted_total == 0
    assert await live_segments(engine, station) == 1


async def test_deleting_a_segment_records_an_event(
    store: TimescaleIngestStore,
    retention: ArchiveRetention,
    engine: AsyncEngine,
    station: str,
) -> None:
    """The Gateway has nobody downstream, so this row is the audit trail.

    Without it, "there was never any telemetry here" and "we deleted it in
    March" are the same answer.
    """
    old = NOW - timedelta(days=RETENTION_DAYS + 1)
    await store.store_records(station, EPOCH, records_at(old, 0, 10))
    await backdate(store.engine, station, old)

    await retention.sweep(now=NOW, only_station=station)

    assert "retention.deleted.age" in await events_of(engine, station)


async def test_the_index_row_survives_and_says_what_was_there(
    store: TimescaleIngestStore,
    retention: ArchiveRetention,
    engine: AsyncEngine,
    station: str,
) -> None:
    """ "3600 records were here and were deleted" is not "nothing was here"."""
    old = NOW - timedelta(days=RETENTION_DAYS + 1)
    await store.store_records(station, EPOCH, records_at(old, 0, 10))
    await backdate(store.engine, station, old)

    await retention.sweep(now=NOW, only_station=station)

    async with engine.connect() as connection:
        row = (
            await connection.execute(
                sa.text(
                    "SELECT record_count, deleted_reason FROM archive_segments "
                    "WHERE station_id = :s"
                ),
                {"s": station},
            )
        ).one()

    assert row.record_count == 10
    assert row.deleted_reason == "age"


# --- rule 2: the size ceiling ----------------------------------------------


async def test_the_ceiling_deletes_oldest_first(
    store: TimescaleIngestStore,
    retention: ArchiveRetention,
    engine: AsyncEngine,
    station: str,
) -> None:
    """Well inside the retention period, so only the ceiling can act."""
    base = NOW - timedelta(days=1)
    for hour in range(6):
        await store.store_records(
            station, EPOCH, records_at(base + timedelta(hours=hour), hour * 20, 20)
        )

    before = await live_segments(engine, station)
    result = await retention.sweep(now=NOW, only_station=station)

    assert result.deleted_by_age == 0
    assert result.deleted_by_ceiling > 0
    assert await live_segments(engine, station) < before

    # What survives is the newest.
    async with engine.connect() as connection:
        remaining = (
            await connection.execute(
                sa.text(
                    "SELECT hour_start FROM archive_segments WHERE station_id = :s "
                    "AND deleted_at IS NULL ORDER BY hour_start"
                ),
                {"s": station},
            )
        ).all()
        removed = (
            await connection.execute(
                sa.text(
                    "SELECT hour_start FROM archive_segments WHERE station_id = :s "
                    "AND deleted_at IS NOT NULL ORDER BY hour_start"
                ),
                {"s": station},
            )
        ).all()

    assert removed, "nothing was removed, so the ordering proves nothing"
    assert max(row.hour_start for row in removed) <= min(
        row.hour_start for row in remaining
    )


async def test_a_station_under_the_ceiling_is_untouched(
    store: TimescaleIngestStore,
    retention: ArchiveRetention,
    engine: AsyncEngine,
    station: str,
) -> None:
    await store.store_records(
        station, EPOCH, records_at(NOW - timedelta(hours=1), 0, 1)
    )
    result = await retention.sweep(now=NOW, only_station=station)
    assert result.deleted_total == 0
    assert await live_segments(engine, station) == 1


async def test_the_ceiling_event_names_the_ceiling(
    store: TimescaleIngestStore,
    retention: ArchiveRetention,
    engine: AsyncEngine,
    station: str,
) -> None:
    """Age and ceiling deletions must be distinguishable after the fact.

    They mean different things: one is policy working, the other is a station
    producing more than anyone budgeted for.
    """
    base = NOW - timedelta(days=1)
    for hour in range(6):
        await store.store_records(
            station, EPOCH, records_at(base + timedelta(hours=hour), hour * 20, 20)
        )

    await retention.sweep(now=NOW, only_station=station)

    assert "retention.deleted.ceiling" in await events_of(engine, station)


# --- holds -----------------------------------------------------------------


async def test_a_hold_exempts_an_epoch_from_age(
    store: TimescaleIngestStore,
    retention: ArchiveRetention,
    engine: AsyncEngine,
    station: str,
) -> None:
    old = NOW - timedelta(days=RETENTION_DAYS + 1)
    await store.store_records(station, EPOCH, records_at(old, 0, 10))
    await backdate(store.engine, station, old)
    await retention.place_hold(
        station,
        EPOCH,
        until=NOW + timedelta(days=30),
        set_by="safety-officer",
        reason="CAA occurrence 2026-114",
        now=NOW,
    )

    result = await retention.sweep(now=NOW, only_station=station)

    assert result.deleted_total == 0
    assert result.skipped_held == 1
    assert await live_segments(engine, station) == 1


async def test_an_expired_hold_stops_exempting(
    store: TimescaleIngestStore,
    retention: ArchiveRetention,
    engine: AsyncEngine,
    station: str,
) -> None:
    """The paired test. An expired hold does not delete anything by itself.

    It returns the epoch to the ordinary rules, which then apply as they would
    have anyway - here, the age rule that was always going to remove this.
    """
    old = NOW - timedelta(days=RETENTION_DAYS + 1)
    await store.store_records(station, EPOCH, records_at(old, 0, 10))
    await backdate(store.engine, station, old)
    await retention.place_hold(
        station,
        EPOCH,
        until=NOW + timedelta(days=5),
        set_by="safety-officer",
        reason="CAA occurrence 2026-114",
        now=NOW,
    )

    # Still held.
    assert (await retention.sweep(now=NOW, only_station=station)).deleted_total == 0
    # Past the date.
    result = await retention.sweep(now=NOW + timedelta(days=6), only_station=station)

    assert result.deleted_by_age == 1
    assert await live_segments(engine, station) == 0


async def test_a_hold_must_expire_in_the_future(
    store: TimescaleIngestStore, retention: ArchiveRetention, station: str
) -> None:
    await store.store_records(station, EPOCH, records_at(NOW, 0, 1))
    with pytest.raises(ValueError, match="must expire in the future"):
        await retention.place_hold(
            station,
            EPOCH,
            until=NOW - timedelta(days=1),
            set_by="someone",
            reason="because",
            now=NOW,
        )


@pytest.mark.parametrize(
    ("set_by", "reason", "message"),
    [("", "why", "who set it"), ("who", "  ", "say why")],
)
async def test_a_hold_must_name_an_owner_and_a_reason(
    store: TimescaleIngestStore,
    retention: ArchiveRetention,
    station: str,
    set_by: str,
    reason: str,
    message: str,
) -> None:
    """A hold nobody can evaluate gets renewed for ever by default."""
    await store.store_records(station, EPOCH, records_at(NOW, 0, 1))
    with pytest.raises(ValueError, match=message):
        await retention.place_hold(
            station,
            EPOCH,
            until=NOW + timedelta(days=30),
            set_by=set_by,
            reason=reason,
            now=NOW,
        )


async def test_holding_an_unknown_epoch_is_an_error(
    retention: ArchiveRetention, station: str
) -> None:
    """Silently succeeding would leave evidence unprotected and look fine."""
    with pytest.raises(StoreError, match="no such epoch"):
        await retention.place_hold(
            station,
            EPOCH,
            until=NOW + timedelta(days=30),
            set_by="safety-officer",
            reason="typo in the epoch",
            now=NOW,
        )


async def test_a_released_hold_stops_exempting_immediately(
    store: TimescaleIngestStore,
    retention: ArchiveRetention,
    engine: AsyncEngine,
    station: str,
) -> None:
    old = NOW - timedelta(days=RETENTION_DAYS + 1)
    await store.store_records(station, EPOCH, records_at(old, 0, 10))
    await backdate(store.engine, station, old)
    await retention.place_hold(
        station,
        EPOCH,
        until=NOW + timedelta(days=300),
        set_by="safety-officer",
        reason="closed early",
        now=NOW,
    )
    await retention.release_hold(station, EPOCH)

    assert (await retention.sweep(now=NOW, only_station=station)).deleted_by_age == 1


async def test_a_hold_exempts_only_its_own_epoch(
    store: TimescaleIngestStore,
    retention: ArchiveRetention,
    engine: AsyncEngine,
    station: str,
) -> None:
    old = NOW - timedelta(days=RETENTION_DAYS + 1)
    await store.store_records(station, EPOCH, records_at(old, 0, 10))
    await store.store_records(station, OTHER_EPOCH, records_at(old, 0, 10))
    await backdate(store.engine, station, old)
    await retention.place_hold(
        station,
        EPOCH,
        until=NOW + timedelta(days=30),
        set_by="safety-officer",
        reason="one flight only",
        now=NOW,
    )

    result = await retention.sweep(now=NOW, only_station=station)

    assert result.deleted_by_age == 1
    assert result.skipped_held == 1
    assert await live_segments(engine, station) == 1


# --- the warning that has to arrive before the date ------------------------


async def test_a_hold_nearing_its_date_is_reported(
    store: TimescaleIngestStore, retention: ArchiveRetention, station: str
) -> None:
    await store.store_records(station, EPOCH, records_at(NOW, 0, 1))
    await retention.place_hold(
        station,
        EPOCH,
        until=NOW + timedelta(days=7),
        set_by="safety-officer",
        reason="CAA occurrence 2026-114",
        now=NOW,
    )

    expiring = await retention.holds_expiring_within(14, now=NOW)

    mine = [hold for hold in expiring if hold.station_id == station]
    assert len(mine) == 1
    assert mine[0].set_by == "safety-officer"
    assert mine[0].reason == "CAA occurrence 2026-114"
    assert mine[0].days_remaining(now=NOW) == 7


async def test_a_hold_far_from_its_date_is_not_reported(
    store: TimescaleIngestStore, retention: ArchiveRetention, station: str
) -> None:
    """The paired absence test: a warning on every hold is a warning on none."""
    await store.store_records(station, EPOCH, records_at(NOW, 0, 1))
    await retention.place_hold(
        station,
        EPOCH,
        until=NOW + timedelta(days=90),
        set_by="safety-officer",
        reason="long investigation",
        now=NOW,
    )

    expiring = await retention.holds_expiring_within(14, now=NOW)

    assert [hold for hold in expiring if hold.station_id == station] == []


async def test_placing_and_releasing_a_hold_are_both_recorded(
    store: TimescaleIngestStore,
    retention: ArchiveRetention,
    engine: AsyncEngine,
    station: str,
) -> None:
    await store.store_records(station, EPOCH, records_at(NOW, 0, 1))
    await retention.place_hold(
        station,
        EPOCH,
        until=NOW + timedelta(days=30),
        set_by="safety-officer",
        reason="CAA occurrence 2026-114",
        now=NOW,
    )
    await retention.release_hold(station, EPOCH)

    events = await events_of(engine, station)
    assert "retention.hold_placed" in events
    assert "retention.hold_released" in events


async def test_an_incomplete_hold_is_refused_by_the_database(
    store: TimescaleIngestStore, engine: AsyncEngine, station: str
) -> None:
    """The constraint, not the application, is the thing that guarantees it."""
    await store.store_records(station, EPOCH, records_at(NOW, 0, 1))
    with pytest.raises(Exception, match="relay_epochs_hold_is_complete"):
        async with engine.begin() as connection:
            await connection.execute(
                sa.text(
                    "UPDATE relay_epochs SET hold_until = now() + interval '1 day' "
                    "WHERE station_id = :s AND epoch = :e"
                ),
                {"s": station, "e": EPOCH},
            )


# --- the index and the disk disagreeing ------------------------------------


async def test_a_segment_whose_file_is_gone_is_counted_and_reported(
    store: TimescaleIngestStore,
    retention: ArchiveRetention,
    engine: AsyncEngine,
    archive: RawArchive,
    station: str,
) -> None:
    """The tolerance stays; the silence goes.

    Deleting an already-absent segment must not fail - retention has to be
    re-runnable after a crash. But it must be visible, because that same
    tolerance is what let 14,288 index rows be marked deleted against a live
    station with nothing failing.
    """
    old = NOW - timedelta(days=RETENTION_DAYS + 1)
    await store.store_records(station, EPOCH, records_at(old, 0, 10))
    await backdate(store.engine, station, old)

    # The file vanishes behind the index's back.
    for segment in archive.root.rglob("*.zst"):
        segment.unlink()

    result = await retention.sweep(now=NOW, only_station=station)

    assert result.already_missing == 1
    assert result.deleted_by_age == 1
    assert "retention.missing_files" in await events_of(engine, station)


async def test_an_ordinary_sweep_reports_nothing_missing(
    store: TimescaleIngestStore,
    retention: ArchiveRetention,
    engine: AsyncEngine,
    station: str,
) -> None:
    """The paired absence test.

    A counter that always fired would make the warning worthless, which is how
    a real signal becomes something operators filter out.
    """
    old = NOW - timedelta(days=RETENTION_DAYS + 1)
    await store.store_records(station, EPOCH, records_at(old, 0, 10))
    await backdate(store.engine, station, old)

    result = await retention.sweep(now=NOW, only_station=station)

    assert result.deleted_by_age == 1
    assert result.already_missing == 0
    assert "retention.missing_files" not in await events_of(engine, station)


async def test_re_running_a_sweep_is_still_safe(
    store: TimescaleIngestStore,
    retention: ArchiveRetention,
    station: str,
) -> None:
    """Restartability is the property the tolerance exists for.

    A second sweep must not raise; it simply finds nothing left to do, because
    the first marked the index.
    """
    old = NOW - timedelta(days=RETENTION_DAYS + 1)
    await store.store_records(station, EPOCH, records_at(old, 0, 10))
    await backdate(store.engine, station, old)

    first = await retention.sweep(now=NOW, only_station=station)
    second = await retention.sweep(now=NOW, only_station=station)

    assert first.deleted_by_age == 1
    assert second.deleted_total == 0
    assert second.already_missing == 0


# --- the schedule, against the real thing (S-07) ---------------------------


async def test_the_scheduled_pass_deletes_a_segment_past_retention(
    store: TimescaleIngestStore,
    retention: ArchiveRetention,
    engine: AsyncEngine,
    archive: RawArchive,
    station: str,
) -> None:
    """Retention had no production caller. This drives the same schedule
    `python -m gateway` starts, scoped to this test's station, and watches
    it delete: a pass that ran and removed nothing would look identical to
    one that never ran."""
    from gateway.retention import RetentionSchedule

    old = datetime.now(tz=UTC) - timedelta(days=RETENTION_DAYS + 1)
    await store.store_records(station, EPOCH, records_at(old, 0, 10))
    await backdate(store.engine, station, old)
    assert await live_segments(engine, station) == 1

    schedule = RetentionSchedule(
        retention=retention, store=store, interval_s=0.05, only_station=station
    )
    stop = asyncio.Event()
    task = asyncio.create_task(schedule.run_until(stop))
    try:
        started = time.monotonic()
        while await live_segments(engine, station) and time.monotonic() - started < 5:
            await asyncio.sleep(0.02)
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=2.0)

    assert schedule.passes >= 1
    assert schedule.failures == 0
    assert await live_segments(engine, station) == 0
    assert not list(archive.root.rglob("*.zst"))
    assert "retention.deleted.age" in await events_of(engine, station)


# --- age is the Gateway's clock, not the station's (S-07) -------------------


async def test_a_segment_stored_recently_survives_an_ancient_station_clock(
    store: TimescaleIngestStore,
    retention: ArchiveRetention,
    engine: AsyncEngine,
    station: str,
) -> None:
    """relay-v1 §9: `recv_utc_ns` may be wrong. A ground PC whose clock is
    years behind must not have freshly acknowledged telemetry deleted at the
    next sweep. Stored now, filed under 1999: kept."""
    ancient = datetime(1999, 1, 1, tzinfo=UTC)
    await store.store_records(station, EPOCH, records_at(ancient, 0, 10))

    result = await retention.sweep(now=datetime.now(tz=UTC), only_station=station)

    assert result.deleted_total == 0
    assert await live_segments(engine, station) == 1


async def test_a_segment_stored_long_ago_is_deleted_whatever_its_hour_says(
    store: TimescaleIngestStore,
    retention: ArchiveRetention,
    engine: AsyncEngine,
    station: str,
) -> None:
    """The presence half: an hour that claims to be today, stored long ago."""
    await store.store_records(station, EPOCH, records_at(NOW, 0, 10))
    await backdate(store.engine, station, NOW - timedelta(days=RETENTION_DAYS + 1))

    result = await retention.sweep(now=NOW, only_station=station)

    assert result.deleted_by_age == 1
    assert await live_segments(engine, station) == 0


async def test_a_file_with_a_recently_stored_row_is_kept_whole(
    store: TimescaleIngestStore,
    retention: ArchiveRetention,
    engine: AsyncEngine,
    station: str,
) -> None:
    """Whole files only. Two index rows share one hour file; if one of them
    was stored inside the window, deleting the file would take its bytes
    with it, unmarked. Neither row is touched."""
    await store.store_records(station, EPOCH, records_at(NOW, 0, 10))
    await backdate(store.engine, station, NOW - timedelta(days=RETENTION_DAYS + 1))
    await store.store_records(station, EPOCH, records_at(NOW, 10, 10))
    assert await live_segments(engine, station) == 2

    result = await retention.sweep(now=NOW, only_station=station)

    assert result.deleted_total == 0
    assert await live_segments(engine, station) == 2


# --- one file, many index rows (S-07) --------------------------------------


async def test_a_file_with_many_index_rows_is_deleted_once_and_reports_nothing_missing(
    store: TimescaleIngestStore,
    retention: ArchiveRetention,
    engine: AsyncEngine,
    archive: RawArchive,
    station: str,
) -> None:
    """Every batch appended to an hour adds an index row. Unlinking on the
    first row and then visiting the rest counted each later row as missing,
    so every sweep of an ordinary archive cried "index and disk disagree"."""
    old = NOW - timedelta(days=RETENTION_DAYS + 1)
    for n in range(4):
        await store.store_records(station, EPOCH, records_at(old, n * 5, 5))
    await backdate(store.engine, station, old)
    assert await live_segments(engine, station) == 4
    assert len(list(archive.root.rglob("*.zst"))) == 1

    result = await retention.sweep(now=NOW, only_station=station)

    assert result.deleted_by_age == 4
    assert result.records_destroyed == 20
    assert result.already_missing == 0
    assert await live_segments(engine, station) == 0
    assert not list(archive.root.rglob("*.zst"))
    events = await events_of(engine, station)
    assert events.count("retention.deleted.age") == 1
    assert "retention.missing_files" not in events


async def test_a_file_with_many_index_rows_that_is_really_gone_is_reported_once(
    store: TimescaleIngestStore,
    retention: ArchiveRetention,
    engine: AsyncEngine,
    archive: RawArchive,
    station: str,
) -> None:
    """The presence half: a file that really is missing is still reported,
    and counted once, not once per row."""
    old = NOW - timedelta(days=RETENTION_DAYS + 1)
    for n in range(4):
        await store.store_records(station, EPOCH, records_at(old, n * 5, 5))
    await backdate(store.engine, station, old)
    for segment in archive.root.rglob("*.zst"):
        segment.unlink()

    result = await retention.sweep(now=NOW, only_station=station)

    assert result.deleted_by_age == 4
    assert result.already_missing == 1
    assert "retention.missing_files" in await events_of(engine, station)


# --- the listing is paged and indexed (S-07) -------------------------------


async def test_the_listing_uses_the_retention_index(
    store: TimescaleIngestStore, engine: AsyncEngine, station: str
) -> None:
    """The unscoped age listing, as production runs it, is shaped for the
    two partial indexes of migration 0008. With sequential scans disabled
    the planner must serve both the candidate scan and the newer-row probe
    from them; if it cannot, the plan says so. EXPLAIN only: nothing here
    reaches another station's rows."""

    await store.store_records(station, EPOCH, records_at(NOW, 0, 5))
    query = listing_query(station_id=None, stored_before=NOW, after=(NOW, ""), limit=10)
    sql = str(
        query.compile(dialect=engine.dialect, compile_kwargs={"literal_binds": True})
    )
    async with engine.begin() as connection:
        await connection.execute(sa.text("SET LOCAL enable_seqscan = off"))
        rows = await connection.execute(sa.text(f"EXPLAIN {sql}"))
        plan = "\n".join(str(row[0]) for row in rows)

    # Which index serves each side is a cost call the planner makes
    # differently at five rows and at five million - in one run it took the
    # unique constraint's index, which also orders by relative_path - so no
    # index name is asserted. What is: the newer-row check is an anti-join,
    # and with sequential scans off every side is served from an index, so
    # the query has a shape the indexes of migration 0008 can serve.
    assert "Anti Join" in plan, plan
    assert "Seq Scan" not in plan, plan
    assert "Index" in plan, plan


async def test_a_path_with_many_index_rows_is_listed_once_and_deleted_whole(
    store: TimescaleIngestStore,
    retention: ArchiveRetention,
    engine: AsyncEngine,
    archive: RawArchive,
    station: str,
) -> None:
    """One row per batch, 120 batches into one hour: one path, one unlink,
    every row marked, and the totals add up."""
    old = NOW - timedelta(days=RETENTION_DAYS + 1)
    for n in range(120):
        await store.store_records(station, EPOCH, records_at(old, n * 2, 2))
    await backdate(store.engine, station, old)
    assert await live_segments(engine, station) == 120

    result = await retention.sweep(now=NOW, only_station=station)

    assert result.deleted_by_age == 120
    assert result.records_destroyed == 240
    assert result.already_missing == 0
    assert await live_segments(engine, station) == 0
    assert not list(archive.root.rglob("*.zst"))
    assert (await events_of(engine, station)).count("retention.deleted.age") == 1


async def test_the_age_sweep_pages_through_more_paths_than_one_listing(
    store: TimescaleIngestStore,
    engine: AsyncEngine,
    archive: RawArchive,
    station: str,
) -> None:
    """Five hours past retention, listed two paths at a time: every page is
    visited and every file goes. A keyset that skipped ties or stopped after
    the first page would leave files behind with no error."""
    retention = ArchiveRetention(
        engine=engine,
        archive=archive,
        retention_days=RETENTION_DAYS,
        max_bytes_per_station=10**9,
        listing_page=2,
    )
    old = NOW - timedelta(days=RETENTION_DAYS + 1)
    for hour in range(5):
        await store.store_records(
            station, EPOCH, records_at(old + timedelta(hours=hour), hour * 3, 3)
        )
    # All backdated to the same instant, so the keyset must break ties.
    await backdate(store.engine, station, old)

    result = await retention.sweep(now=NOW, only_station=station)

    assert result.deleted_by_age == 5
    assert await live_segments(engine, station) == 0
    assert not list(archive.root.rglob("*.zst"))


async def test_the_ceiling_pages_until_the_station_is_under_it(
    store: TimescaleIngestStore,
    engine: AsyncEngine,
    archive: RawArchive,
    station: str,
) -> None:
    """The presence half for the ceiling's paging: more over-ceiling paths
    than one page holds, and it keeps going until the station fits."""
    retention = ArchiveRetention(
        engine=engine,
        archive=archive,
        retention_days=RETENTION_DAYS,
        max_bytes_per_station=12_000,
        listing_page=2,
    )
    base = NOW - timedelta(days=1)
    for hour in range(8):
        await store.store_records(
            station, EPOCH, records_at(base + timedelta(hours=hour), hour * 20, 20)
        )

    result = await retention.sweep(now=NOW, only_station=station)

    assert result.deleted_by_ceiling > 2, "stopped after the first page"
    async with engine.connect() as connection:
        remaining = await connection.scalar(
            sa.text(
                "SELECT coalesce(sum(compressed_bytes), 0) FROM archive_segments "
                "WHERE station_id = :s AND deleted_at IS NULL"
            ),
            {"s": station},
        )
    assert int(remaining) <= 12_000


# --- an append between the listing and the lock (S-07) ---------------------


class AppendsAfterListing(ArchiveRetention):
    """Appends a batch to the first listed path right after listing it.

    What a session does, minutes into a large sweep: the listing is stale,
    and the file it named now holds acknowledged bytes the listing never
    saw.
    """

    def __init__(self, *args: Any, store: TimescaleIngestStore, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.store = store
        self.appended: list[str] = []

    async def _list_paths(
        self,
        *,
        station_id: str | None,
        stored_before: datetime | None,
        after: tuple[datetime, str] | None,
    ) -> list[Any]:
        page = await super()._list_paths(
            station_id=station_id, stored_before=stored_before, after=after
        )
        if page and not self.appended:
            first = page[0]
            await self.store.store_records(
                first.station_id, first.epoch, records_at(first.hour_start, 100, 5)
            )
            self.appended.append(first.relative_path)
        return page


async def test_a_file_appended_to_after_listing_survives_the_age_sweep(
    store: TimescaleIngestStore,
    engine: AsyncEngine,
    archive: RawArchive,
    station: str,
) -> None:
    old = NOW - timedelta(days=RETENTION_DAYS + 1)
    await store.store_records(station, EPOCH, records_at(old, 0, 10))
    await backdate(store.engine, station, old)
    retention = AppendsAfterListing(
        engine=engine,
        archive=archive,
        retention_days=RETENTION_DAYS,
        max_bytes_per_station=10**9,
        store=store,
    )

    result = await retention.sweep(now=NOW, only_station=station)

    assert retention.appended, "the append never happened, so this proves nothing"
    assert result.deleted_total == 0
    assert result.skipped_appended == 1
    assert await live_segments(engine, station) == 2
    # The file is there, and both the old batch and the new one read back.
    path = retention.appended[0]
    stored = archive.read_segment(path)
    assert [record.seq for record in stored] == list(range(10)) + list(range(100, 105))

    # Presence: once the new row is old too, the file goes.
    await backdate(store.engine, station, old)
    later = await ArchiveRetention(
        engine=engine,
        archive=archive,
        retention_days=RETENTION_DAYS,
        max_bytes_per_station=10**9,
    ).sweep(now=NOW, only_station=station)
    assert later.deleted_by_age == 2
    assert await live_segments(engine, station) == 0


async def test_a_file_appended_to_after_listing_survives_the_ceiling_sweep(
    store: TimescaleIngestStore,
    engine: AsyncEngine,
    archive: RawArchive,
    station: str,
) -> None:
    base = NOW - timedelta(days=1)
    for hour in range(3):
        await store.store_records(
            station, EPOCH, records_at(base + timedelta(hours=hour), hour * 20, 20)
        )
    retention = AppendsAfterListing(
        engine=engine,
        archive=archive,
        retention_days=RETENTION_DAYS,
        # Under everything, so the ceiling wants the oldest path gone.
        max_bytes_per_station=12_000,
        store=store,
    )

    result = await retention.sweep(now=NOW, only_station=station)

    assert retention.appended
    assert result.skipped_appended == 1
    appended = retention.appended[0]
    stored = archive.read_segment(appended)
    assert list(range(100, 105)) == [r.seq for r in stored if r.seq >= 100]
    async with engine.connect() as connection:
        live = await connection.scalar(
            sa.text(
                "SELECT count(*) FROM archive_segments WHERE relative_path = :p "
                "AND deleted_at IS NULL"
            ),
            {"p": appended},
        )
    assert live == 2
    # The ceiling still acted, on the paths that were not touched.
    assert result.deleted_by_ceiling >= 1

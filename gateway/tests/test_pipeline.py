"""The ingest pipeline: stored records in, drone_state rows out.

The behaviour worth testing here is not the conversion - every step has its own
tests - but the pipeline's obligations to the transport above it:

- it never raises, because a conversion fault is not a transport fault and must
  not stall a station;
- an unbound source produces no row and is announced once, not once per
  datagram;
- a GCS is never announced at all.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

import pytest
from pymavlink.dialects.v20 import ardupilotmega as mavlink

from gateway.binding import NOT_ASSIGNED, UNBOUND, UNCLASSIFIED, Resolution
from gateway.drone_state import DroneStateRow, timestamp_from_recv_utc_ns
from gateway.parsing import SourceId
from gateway.pipeline import IngestPipeline, StationPipelines
from gateway.rate_limit import RateLimiter
from gateway.relay_records import Record

STATION = "tbilisi-base-1"
EPOCH = "9f2c1b7d4e6a58039ab1c2d3e4f50617"
DRONE = UUID("0b63df96-30ba-41ba-b3fe-7647edb2b0ee")
NOON_NS = int(datetime(2026, 9, 24, 12, 0, tzinfo=UTC).timestamp()) * 1_000_000_000


class FakeResolver:
    """Resolves everything to one drone, or to nothing."""

    def __init__(
        self,
        *,
        drone_id: UUID | None = DRONE,
        labels: dict[UUID, str] | None = None,
        labels_fail: bool = False,
        rebound_at: datetime | None = None,
        rebound_to: UUID | None = None,
    ) -> None:
        self.drone_id = drone_id
        self.unclaimed: list[Resolution] = []
        self.labels = labels if labels is not None else {DRONE: "SITL-01"}
        self.labels_fail = labels_fail
        # A SYSID reassigned at `rebound_at`: records captured from then on
        # belong to `rebound_to`. Mirrors BindingResolver's validity ranges.
        self.rebound_at = rebound_at
        self.rebound_to = rebound_to
        self.batch_calls = 0
        self.suppressed: list[int] = []
        # Every vehicle resolves as bound on another station (P1-07).
        self.not_assigned = False

    async def labels_for(self, drone_ids: set[UUID]) -> dict[UUID, str]:
        if self.labels_fail:
            raise RuntimeError("known_drones is unreachable")
        return {
            drone_id: self.labels[drone_id]
            for drone_id in drone_ids
            if drone_id in self.labels
        }

    async def resolve(
        self, station_id: str, source: Any, *, at: datetime
    ) -> Resolution:
        if self.not_assigned and source.kind.value == "vehicle":
            return Resolution(source.source_id, None, NOT_ASSIGNED)
        if self.drone_id is None:
            reason = UNCLASSIFIED if source.kind.value == "unclassified" else UNBOUND
            return Resolution(source.source_id, None, reason)
        if self.rebound_at is not None and at >= self.rebound_at:
            return Resolution(source.source_id, self.rebound_to)
        return Resolution(source.source_id, self.drone_id)

    async def resolve_batch(
        self, station_id: str, records: list[tuple[Any, datetime]]
    ) -> list[Resolution]:
        self.batch_calls += 1
        return [
            await self.resolve(station_id, source, at=moment)
            for source, moment in records
        ]

    async def record_unclaimed(
        self,
        station_id: str,
        epoch: str | None,
        resolution: Resolution,
        *,
        suppressed: int = 0,
    ) -> None:
        self.unclaimed.append(resolution)
        self.suppressed.append(suppressed)


class FakeWriter:
    def __init__(self, *, fail: bool = False) -> None:
        self.written: list[DroneStateRow] = []
        self.fail = fail

    async def write(self, rows: list[DroneStateRow]) -> int:
        if self.fail:
            raise RuntimeError("the database is gone")
        self.written.extend(rows)
        return len(rows)


class FakePublisher:
    def __init__(self) -> None:
        self.rows: list[DroneStateRow] = []
        self.labels: dict[UUID, str] = {}
        self.links: dict[UUID, dict[str, Any]] = {}
        self.firmware: dict[UUID, dict[str, Any]] = {}
        self.unclaimed: list[SourceId] = []
        self.rx_ts: list[datetime | None] = []
        self.backlog: list[bool] = []

    async def publish_rows(
        self,
        rows: list[DroneStateRow],
        labels: dict[UUID, str] | None = None,
        links: dict[UUID, dict[str, Any]] | None = None,
        firmware: dict[UUID, dict[str, Any]] | None = None,
        *,
        rx_ts: datetime | None = None,
        backlog: list[bool] | None = None,
    ) -> None:
        self.rows.extend(rows)
        self.labels = labels or {}
        self.links = links or {}
        self.firmware = firmware or {}
        self.rx_ts.extend([rx_ts] * len(rows))
        self.backlog.extend(backlog if backlog is not None else [False] * len(rows))

    async def publish_unclaimed(
        self, station_id: str, resolution: Resolution, source_id: SourceId
    ) -> None:
        self.unclaimed.append(source_id)


def build(
    resolver: FakeResolver | None = None,
    writer: FakeWriter | None = None,
    publisher: FakePublisher | None = None,
) -> tuple[IngestPipeline, FakeResolver, FakeWriter, FakePublisher]:
    resolver = resolver or FakeResolver()
    writer = writer or FakeWriter()
    publisher = publisher or FakePublisher()
    pipeline = IngestPipeline(
        station_id=STATION,
        resolver=cast(Any, resolver),
        writer=cast(Any, writer),
        publisher=cast(Any, publisher),
    )
    return pipeline, resolver, writer, publisher


def link(sysid: int = 1, compid: int = 1) -> mavlink.MAVLink:
    built = mavlink.MAVLink(None, srcSystem=sysid, srcComponent=compid)
    built.signing.sign_outgoing = False
    return built


def record(seq: int, datagram: bytes, *, offset_ns: int = 0) -> Record:
    return Record(seq=seq, recv_utc_ns=NOON_NS + offset_ns, datagram=datagram)


def heartbeat(sysid: int = 1, compid: int = 1, *, gcs: bool = False) -> bytes:
    sender = link(sysid, compid)
    return bytes(
        sender.heartbeat_encode(
            mavlink.MAV_TYPE_GCS if gcs else mavlink.MAV_TYPE_QUADROTOR,
            mavlink.MAV_AUTOPILOT_INVALID
            if gcs
            else mavlink.MAV_AUTOPILOT_ARDUPILOTMEGA,
            1,
            4,
            mavlink.MAV_STATE_ACTIVE,
        ).pack(sender)
    )


def position(sysid: int = 1, compid: int = 1) -> bytes:
    sender = link(sysid, compid)
    return bytes(
        sender.global_position_int_encode(
            0, 417151000, 448271000, 450000, 60000, 1000, -250, 150, 9000
        ).pack(sender)
    )


# --- the happy path --------------------------------------------------------


async def test_a_heartbeat_then_a_position_produces_one_row() -> None:
    pipeline, _, writer, publisher = build()

    rows = await pipeline.process(
        EPOCH, [record(0, heartbeat()), record(1, position(), offset_ns=300_000_000)]
    )

    assert len(rows) == 1
    assert rows[0].drone_id == DRONE
    assert rows[0].station_id == STATION
    assert rows[0].lat_deg == pytest.approx(41.7151)
    assert writer.written == rows
    assert publisher.rows == rows


async def test_the_row_timestamp_is_the_records_capture_time() -> None:
    """Not ingest time. A replayed backlog belongs where it happened."""
    pipeline, _, _, _ = build()

    rows = await pipeline.process(
        EPOCH, [record(0, heartbeat()), record(1, position())]
    )

    assert rows[0].ts == datetime(2026, 9, 24, 12, 0, tzinfo=UTC)


async def test_rows_are_published_with_the_gateways_receive_time() -> None:
    """S-11. `rx_ts` is the Gateway's clock at the batch, not the relay's
    capture time, and without a session every record is live."""
    pipeline, _, _, publisher = build()
    received = datetime(2026, 9, 24, 12, 0, 5, tzinfo=UTC)
    pipeline.wall = lambda: received

    rows = await pipeline.process(
        EPOCH, [record(0, heartbeat()), record(1, position(), offset_ns=300_000_000)]
    )

    assert len(rows) == 1
    assert publisher.rx_ts == [received]
    assert publisher.backlog == [False]


async def test_records_up_to_newest_seq_held_are_backlog_and_the_rest_live() -> None:
    """S-11. The session's `hello` declared `newest_seq_held` = 3: seq 0-3
    were on the relay's disk before the connection; seq 4 onward were
    captured while it was up. One batch straddles the boundary."""
    pipeline, _, _, publisher = build()

    rows = await pipeline.process(
        EPOCH,
        [record(0, heartbeat())] + [record(seq, position()) for seq in range(1, 7)],
        newest_seq_held=3,
    )

    assert len(rows) == 6
    assert publisher.backlog == [True, True, True, False, False, False]


async def test_messages_that_are_not_on_the_hot_path_emit_no_row() -> None:
    """A row is emitted on position and nothing else."""
    pipeline, _, writer, _ = build()

    rows = await pipeline.process(EPOCH, [record(0, heartbeat())])

    assert rows == []
    assert writer.written == []


# --- unbound sources -------------------------------------------------------


async def test_an_unbound_source_produces_no_row() -> None:
    """§7: archived, never written, never auto-registered."""
    pipeline, _, writer, _ = build(resolver=FakeResolver(drone_id=None))

    rows = await pipeline.process(
        EPOCH, [record(0, heartbeat()), record(1, position())]
    )

    assert rows == []
    assert writer.written == []


async def test_an_unbound_source_is_announced_once_not_once_per_datagram() -> None:
    """An unbound aircraft at 84 Hz would otherwise emit 84 events a second.

    That is how a genuinely useful signal becomes something operators filter
    out, so it is announced per address.
    """
    pipeline, resolver, _, publisher = build(resolver=FakeResolver(drone_id=None))

    for seq in range(20):
        await pipeline.process(EPOCH, [record(seq, position())])

    assert len(publisher.unclaimed) == 1
    assert len(resolver.unclaimed) == 1
    assert publisher.unclaimed[0] == SourceId(sysid=1, compid=1)


async def test_two_unbound_addresses_are_announced_separately() -> None:
    """Per address, so a second aircraft is not hidden by the first."""
    pipeline, _, _, publisher = build(resolver=FakeResolver(drone_id=None))

    await pipeline.process(
        EPOCH,
        [record(0, position(sysid=1)), record(1, position(sysid=2))],
    )

    assert set(publisher.unclaimed) == {SourceId(1, 1), SourceId(2, 1)}


async def test_a_ground_station_is_never_announced_as_unclaimed() -> None:
    """QGroundControl's own heartbeat is not an aircraft nobody bound.

    Announcing it would bury the case that matters, which arrives at the same
    rate on the same link.
    """
    pipeline, _, _, publisher = build(resolver=FakeResolver(drone_id=None))

    await pipeline.process(
        EPOCH, [record(0, heartbeat(sysid=255, compid=190, gcs=True))]
    )

    assert publisher.unclaimed == []


async def test_forgetting_an_address_allows_it_to_be_announced_again() -> None:
    """A binding that is later removed must be reported again.

    Silence after a binding disappears would read as everything being fine.
    """
    pipeline, _, _, publisher = build(resolver=FakeResolver(drone_id=None))
    await pipeline.process(EPOCH, [record(0, position())])

    pipeline.forget_unclaimed(SourceId(1, 1))
    await pipeline.process(EPOCH, [record(1, position())])

    assert len(publisher.unclaimed) == 2


# --- the pipeline never raises into the transport --------------------------


async def test_a_datagram_that_is_not_mavlink_does_not_raise() -> None:
    """The transport already stored it; the conversion is what fails.

    A datagram that is not MAVLink is exactly what relay-v1 §6 promises to
    forward unchanged, so it must reach here and be survivable.
    """
    pipeline, _, writer, _ = build()

    rows = await pipeline.process(EPOCH, [record(0, b"GET / HTTP/1.1\r\n\r\n")])

    assert rows == []
    assert writer.written == []


async def test_a_writer_failure_does_not_raise() -> None:
    """A failure here loses a derived view, not the flight record.

    The archive and the index are already written, so this must not propagate
    back into the transport and stall a station.
    """
    pipeline, _, _, publisher = build(writer=FakeWriter(fail=True))

    rows = await pipeline.process(
        EPOCH, [record(0, heartbeat()), record(1, position())]
    )

    # The rows were assembled and returned; only the write failed.
    assert len(rows) == 1
    assert publisher.rows == []


async def test_a_resolver_failure_does_not_raise() -> None:
    class ExplodingResolver(FakeResolver):
        async def resolve(
            self, station_id: str, source: Any, *, at: datetime
        ) -> Resolution:
            raise RuntimeError("the database is gone")

    pipeline, _, writer, _ = build(resolver=ExplodingResolver())

    rows = await pipeline.process(EPOCH, [record(0, position())])

    assert rows == []
    assert writer.written == []


async def test_a_failed_batch_does_not_poison_the_next_one() -> None:
    """The presence half of the resolver failure: once the database is back,
    the next batch converts normally."""

    class FlakyResolver(FakeResolver):
        def __init__(self) -> None:
            super().__init__()
            self.fail_next = True

        async def resolve_batch(
            self, station_id: str, records: list[tuple[Any, datetime]]
        ) -> list[Resolution]:
            if self.fail_next:
                self.fail_next = False
                raise RuntimeError("the database is gone")
            return await super().resolve_batch(station_id, records)

    pipeline, _, writer, _ = build(resolver=FlakyResolver())

    assert await pipeline.process(EPOCH, [record(0, heartbeat())]) == []
    rows = await pipeline.process(EPOCH, [record(1, position())])

    assert len(rows) == 1
    assert writer.written == rows


async def test_bad_frames_are_counted() -> None:
    """Corruption reaches somewhere it can be seen rather than vanishing."""
    pipeline, _, _, _ = build()
    damaged = bytearray(heartbeat())
    damaged[6] ^= 0xFF

    await pipeline.process(EPOCH, [record(0, bytes(damaged))])

    assert pipeline._bad_frames >= 1


# --- per-station isolation -------------------------------------------------


async def test_each_station_gets_its_own_pipeline() -> None:
    """§8: two stations relaying one vehicle are independent observations.

    A shared accumulator would blend two links into a state neither saw.
    """
    resolver, writer, publisher = FakeResolver(), FakeWriter(), FakePublisher()
    pipelines = StationPipelines(
        resolver=cast(Any, resolver),
        writer=cast(Any, writer),
        publisher=cast(Any, publisher),
    )

    await pipelines.process("alpha", EPOCH, [record(0, heartbeat())])
    await pipelines.process("bravo", EPOCH, [record(0, position())])

    assert set(pipelines.pipelines) == {"alpha", "bravo"}
    # bravo never saw the heartbeat, so its row carries no mode.
    assert len(writer.written) == 1
    assert writer.written[0].station_id == "bravo"
    assert writer.written[0].mode is None


async def test_the_same_station_reuses_its_pipeline() -> None:
    """State accumulates across batches; a new pipeline per batch would lose
    everything learned from the messages before it."""
    resolver, writer, publisher = FakeResolver(), FakeWriter(), FakePublisher()
    pipelines = StationPipelines(
        resolver=cast(Any, resolver),
        writer=cast(Any, writer),
        publisher=cast(Any, publisher),
    )

    await pipelines.process(STATION, EPOCH, [record(0, heartbeat())])
    await pipelines.process(STATION, EPOCH, [record(1, position())])

    assert len(pipelines.pipelines) == 1
    assert writer.written[0].mode == "GUIDED"


# --- the registry label travels with the row -------------------------------
#
# The console listed ten aircraft as `9e1e607e`, `ba7f9168`, `685def77` and so
# on. Every one of them was correct and none of them was usable: with ten SITL
# vehicles in a row on the map there was no way to tell which entry was which
# airframe, which is also how a heading was compared against the wrong vehicle.


async def test_the_label_is_published_with_the_row() -> None:
    pipeline, _, _, publisher = build()

    await pipeline.process(
        EPOCH, [record(0, heartbeat()), record(1, position(), offset_ns=300_000_000)]
    )

    assert publisher.labels == {DRONE: "SITL-01"}


async def test_a_row_is_still_published_when_the_label_is_unknown() -> None:
    """An unregistered label must cost the name, not the position."""
    pipeline, _, _, publisher = build(resolver=FakeResolver(labels={}))

    rows = await pipeline.process(
        EPOCH, [record(0, heartbeat()), record(1, position(), offset_ns=300_000_000)]
    )

    assert publisher.rows == rows
    assert publisher.labels == {}


async def test_a_row_is_still_published_when_the_label_lookup_fails() -> None:
    """The paired failure test: the registry falling over is cosmetic.

    Written because the first version of this change put the lookup inside the
    handler that already wrapped the write and the publish, so a resolver
    without the method took the telemetry down with it - live positions lost to
    a missing display name.
    """
    pipeline, _, writer, publisher = build(resolver=FakeResolver(labels_fail=True))

    rows = await pipeline.process(
        EPOCH, [record(0, heartbeat()), record(1, position(), offset_ns=300_000_000)]
    )

    assert rows
    assert writer.written == rows
    assert publisher.rows == rows
    assert publisher.labels == {}


# --- bindings are read per batch (P1-13) -----------------------------------
#
# P1-10 measured one binding query per MAVLink message as 96% of the time the
# Gateway spent storing a batch. These pin the fix and, more importantly, what
# the fix must not change: which binding applies to which message.


async def test_a_batch_is_resolved_with_one_call_not_one_per_message() -> None:
    pipeline, resolver, _, _ = build()
    batch = [record(0, heartbeat())] + [
        record(n, position(), offset_ns=n * 250_000_000) for n in range(1, 50)
    ]

    rows = await pipeline.process(EPOCH, batch)

    assert resolver.batch_calls == 1
    assert len(rows) == 49


async def test_a_batch_crossing_a_rebinding_splits_between_two_drones() -> None:
    """A replayed backlog straddling a SYSID reassignment. Every record must
    resolve against the binding in force when it was *captured*, so one batch
    legitimately yields two drone_ids for one address."""
    successor = UUID("5d2c6a9e-8f41-4b7a-9c3e-1a2b3c4d5e6f")
    cutover = datetime(2026, 9, 24, 12, 0, 2, tzinfo=UTC)
    resolver = FakeResolver(rebound_at=cutover, rebound_to=successor)
    pipeline, _, _, _ = build(resolver=resolver)
    batch = [record(0, heartbeat())] + [
        record(n, position(), offset_ns=n * 500_000_000) for n in range(1, 9)
    ]

    rows = await pipeline.process(EPOCH, batch)

    assert resolver.batch_calls == 1
    before = [row for row in rows if row.ts < cutover]
    after = [row for row in rows if row.ts >= cutover]
    assert before and after
    assert {row.drone_id for row in before} == {DRONE}
    assert {row.drone_id for row in after} == {successor}


async def test_a_heartbeat_later_in_the_batch_does_not_reclassify_an_earlier_message() -> (
    None
):
    """Classification is per message, as it was when messages were resolved
    one at a time. A position that arrives before its source's first HEARTBEAT
    is unclassified at that moment and produces no row, even though the
    HEARTBEAT is in the same batch."""

    class ClassifyingResolver(FakeResolver):
        # BindingResolver's rule, which the plain fake skips: a source that
        # has not sent a HEARTBEAT never resolves, bound or not.
        async def resolve(
            self, station_id: str, source: Any, *, at: datetime
        ) -> Resolution:
            if source.kind.value == "unclassified":
                return Resolution(source.source_id, None, UNCLASSIFIED)
            return await super().resolve(station_id, source, at=at)

    pipeline, _, _, _ = build(resolver=ClassifyingResolver())

    rows = await pipeline.process(
        EPOCH,
        [
            record(0, position()),
            record(1, heartbeat(), offset_ns=100_000_000),
            record(2, position(), offset_ns=300_000_000),
        ],
    )

    assert len(rows) == 1
    assert rows[0].ts == datetime(2026, 9, 24, 12, 0, 0, 300_000, tzinfo=UTC)


# --- sources refused by station policy (P1-07) -----------------------------


class Clock:
    def __init__(self) -> None:
        self.now_s = 0.0

    def __call__(self) -> float:
        return self.now_s


def a_station_presenting_someone_elses_aircraft() -> tuple[
    IngestPipeline, FakeResolver, FakeWriter, FakePublisher, Clock
]:
    resolver = FakeResolver()
    resolver.not_assigned = True
    pipeline, _, writer, publisher = build(resolver=resolver)
    clock = Clock()
    pipeline.rejections = RateLimiter(interval_s=60.0, clock=clock)
    return pipeline, resolver, writer, publisher, clock


async def test_a_rejected_source_produces_no_row_and_is_reported() -> None:
    pipeline, resolver, writer, publisher, _ = (
        a_station_presenting_someone_elses_aircraft()
    )

    rows = await pipeline.process(
        EPOCH, [record(0, heartbeat()), record(1, position())]
    )

    assert rows == []
    assert writer.written == []
    assert [r.unclaimed_reason for r in resolver.unclaimed] == [NOT_ASSIGNED]
    assert publisher.unclaimed == [SourceId(sysid=1, compid=1)]


async def test_a_continuing_rejection_is_reported_again_with_its_count() -> None:
    """Unlike an unclaimed source, a refusal must not go quiet after its first
    report - and it must not flood either."""
    pipeline, resolver, _, _, clock = a_station_presenting_someone_elses_aircraft()
    batch = [record(0, heartbeat())] + [record(n, position()) for n in range(1, 10)]

    await pipeline.process(EPOCH, batch)
    assert resolver.suppressed == [0]

    clock.now_s += 30.0
    await pipeline.process(EPOCH, batch)
    assert resolver.suppressed == [0], "reported again inside the interval"

    clock.now_s += 31.0
    await pipeline.process(EPOCH, batch)
    # 9 more in the first batch, 10 in the second, then this one reported.
    assert resolver.suppressed == [0, 19]


async def test_an_unclaimed_source_is_still_announced_only_once() -> None:
    """The rejection path must not change the unclaimed one: an aircraft being
    set up is announced once, as before."""
    pipeline, resolver, _, _ = build(resolver=FakeResolver(drone_id=None))

    for _ in range(3):
        await pipeline.process(EPOCH, [record(0, heartbeat())])

    assert len(resolver.unclaimed) == 1
    assert resolver.suppressed == [0]


# --- live state (P1-05) ----------------------------------------------------


class FakeLiveState:
    def __init__(self, *, fail: bool = False) -> None:
        self.updates: list[list[DroneStateRow]] = []
        self.fail = fail

    async def update(
        self, rows: list[DroneStateRow], labels: dict[UUID, str] | None = None
    ) -> dict[UUID, int]:
        if self.fail:
            raise ConnectionError("redis is gone")
        self.updates.append(list(rows))
        return {row.drone_id: 1 for row in rows}


def with_live_state(
    live: FakeLiveState, writer: FakeWriter | None = None
) -> tuple[IngestPipeline, FakeWriter, FakePublisher]:
    pipeline, _, writer, publisher = build(writer=writer)
    pipeline.live_state = cast(Any, live)
    return pipeline, writer, publisher


async def test_rows_reach_live_state() -> None:
    live = FakeLiveState()
    pipeline, _, _ = with_live_state(live)

    rows = await pipeline.process(
        EPOCH, [record(0, heartbeat()), record(1, position())]
    )

    assert live.updates == [rows]


async def test_a_redis_failure_costs_neither_the_insert_nor_the_publish() -> None:
    pipeline, writer, publisher = with_live_state(FakeLiveState(fail=True))

    rows = await pipeline.process(
        EPOCH, [record(0, heartbeat()), record(1, position())]
    )

    assert writer.written == rows
    assert publisher.rows == rows


async def test_a_failed_insert_does_not_make_every_drone_look_link_lost() -> None:
    """Live state is derived from the same rows, not from the hypertable. A
    database hiccup must not flip the whole fleet to lost."""
    live = FakeLiveState()
    pipeline, _, _ = with_live_state(live, writer=FakeWriter(fail=True))

    rows = await pipeline.process(
        EPOCH, [record(0, heartbeat()), record(1, position())]
    )

    assert live.updates == [rows]


async def test_a_batch_with_no_rows_touches_no_live_state() -> None:
    live = FakeLiveState()
    pipeline, _, _ = with_live_state(live)

    await pipeline.process(EPOCH, [record(0, heartbeat())])

    assert live.updates == []


# --- firmware (P1-11) --------------------------------------------------------


class FakeFirmware:
    """Records what it is shown, and answers `latest` from that."""

    def __init__(self, *, fail: bool = False) -> None:
        self.observed: list[tuple[UUID, str, datetime, Any]] = []
        self.fail = fail

    async def observe(
        self, drone_id: UUID, station_id: str, observed_at: datetime, firmware: Any
    ) -> bool:
        if self.fail:
            raise RuntimeError("drone_firmware is unreachable")
        self.observed.append((drone_id, station_id, observed_at, firmware))
        return True

    async def latest(self, drone_id: UUID) -> Any:
        if self.fail:
            raise RuntimeError("drone_firmware is unreachable")
        for seen, _, _, firmware in reversed(self.observed):
            if seen == drone_id:
                return firmware
        return None


def with_firmware(
    registry: FakeFirmware, resolver: FakeResolver | None = None
) -> tuple[IngestPipeline, FakeWriter, FakePublisher]:
    pipeline, _, writer, publisher = build(resolver=resolver)
    pipeline.firmware = cast(Any, registry)
    return pipeline, writer, publisher


def autopilot_version(sysid: int = 1, compid: int = 1) -> bytes:
    sender = link(sysid, compid)
    return bytes(
        sender.autopilot_version_encode(
            capabilities=0,
            flight_sw_version=(4 << 24) | (8 << 16) | mavlink.FIRMWARE_VERSION_TYPE_DEV,
            middleware_sw_version=0,
            os_sw_version=0,
            board_version=0,
            flight_custom_version=list(b"66c89850"),
            middleware_custom_version=[0] * 8,
            os_custom_version=[0] * 8,
            vendor_id=0,
            product_id=0,
            uid=0,
        ).pack(sender)
    )


async def test_a_bound_drones_version_is_recorded_and_published() -> None:
    registry = FakeFirmware()
    pipeline, _, publisher = with_firmware(registry)

    await pipeline.process(
        EPOCH,
        [
            record(0, heartbeat()),
            record(1, autopilot_version(), offset_ns=100_000_000),
            record(2, position(), offset_ns=200_000_000),
        ],
    )

    assert len(registry.observed) == 1
    drone_id, station_id, observed_at, firmware = registry.observed[0]
    assert (drone_id, station_id) == (DRONE, STATION)
    # Capture time, not ingest time, like every other record.
    assert observed_at == timestamp_from_recv_utc_ns(NOON_NS + 100_000_000)
    assert firmware.version == "4.8.0-dev"
    assert publisher.firmware == {
        DRONE: {"version": "4.8.0-dev", "git_hash": "66c89850"}
    }


async def test_a_drone_with_no_recorded_version_is_published_without_one() -> None:
    """Absent, so the console shows unknown - not a blank that reads as fine."""
    pipeline, _, publisher = with_firmware(FakeFirmware())

    rows = await pipeline.process(
        EPOCH, [record(0, heartbeat()), record(1, position())]
    )

    assert publisher.rows == rows
    assert publisher.firmware == {}


async def test_an_unbound_sources_version_is_not_recorded() -> None:
    """A version is attributed to an airframe or not at all."""
    registry = FakeFirmware()
    pipeline, _, _ = with_firmware(registry, resolver=FakeResolver(drone_id=None))

    await pipeline.process(
        EPOCH, [record(0, heartbeat()), record(1, autopilot_version())]
    )

    assert registry.observed == []


async def test_a_firmware_store_failure_costs_neither_the_insert_nor_the_publish() -> (
    None
):
    pipeline, writer, publisher = with_firmware(FakeFirmware(fail=True))

    rows = await pipeline.process(
        EPOCH,
        [
            record(0, heartbeat()),
            record(1, autopilot_version()),
            record(2, position()),
        ],
    )

    assert len(rows) == 1
    assert writer.written == rows
    assert publisher.rows == rows
    assert publisher.firmware == {}

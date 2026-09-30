"""The service's obligations: publish and audit each transition once, and keep
going when either fails."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest

from airspace.cpa import SeparationPolicy, local_offset_m
from airspace.monitor import AirspaceMonitor, Alert, ClearReason
from airspace.service import AirspaceService, run_ticker
from common.terrain import TerrainFileError, cell_name

A = UUID(int=1)
B = UUID(int=2)
LAT0 = 41.7151
LON0 = 44.8271
POLICY = SeparationPolicy(
    t_cpa_max_s=60, d_horizontal_min_m=60, d_vertical_min_m=20, neighbour_radius_m=800
)


def payload(
    drone_id: UUID, north_m: float, vn: float, armed: bool = True, at_s: float = 0.0
) -> bytes:
    n1, _ = local_offset_m(LAT0, LON0, LAT0 + 0.001, LON0)
    return json.dumps(
        {
            "drone_id": str(drone_id),
            "label": f"D{drone_id.int}",
            "ts": datetime.fromtimestamp(at_s, tz=UTC).isoformat(),
            "lat_deg": LAT0 + 0.001 * north_m / n1,
            "lon_deg": LON0,
            "alt_amsl_m": 550.0,
            "vx_ms": vn,
            "vy_ms": 0.0,
            "vz_ms": 0.0,
            "armed": armed,
        }
    ).encode()


class RecordingBus:
    def __init__(self, *, fail: bool = False) -> None:
        self.sent: list[tuple[str, dict[str, Any]]] = []
        self.fail = fail

    async def publish(self, subject: str, payload: bytes) -> None:
        if self.fail:
            raise ConnectionError("bus is gone")
        self.sent.append((subject, json.loads(payload)))


class RecordingAudit:
    def __init__(
        self, *, fail: bool = False, gate: asyncio.Event | None = None
    ) -> None:
        self.rows: list[tuple[str, str]] = []
        self.fail = fail
        # When set, every write waits for it: a slow database.
        self.gate = gate

    async def record(
        self, alert: Alert, state: str, *, reason: ClearReason | None = None
    ) -> None:
        if self.gate is not None:
            await self.gate.wait()
        if self.fail:
            raise ConnectionError("database is gone")
        self.rows.append((alert.key, state if reason is None else f"{state}:{reason}"))


class Clock:
    def __init__(self) -> None:
        self.now_s = 0.0

    def __call__(self) -> float:
        return self.now_s


def service(
    bus: RecordingBus, audit: RecordingAudit | None = None
) -> tuple[AirspaceService, Clock]:
    clock = Clock()
    return (
        AirspaceService(
            monitor=AirspaceMonitor(policy=POLICY, stale_after_s=15.0),
            bus=bus,
            audit=audit,
            clock=clock,
        ),
        clock,
    )


async def test_a_conflict_is_published_and_audited_once() -> None:
    bus, audit = RecordingBus(), RecordingAudit()
    svc, _ = service(bus, audit)

    await svc.on_telemetry(payload(A, 0, 10))
    await svc.on_telemetry(payload(B, 500, -10))
    await svc.on_telemetry(payload(A, 0, 10))

    assert len(bus.sent) == 1
    subject, body = bus.sent[0]
    assert subject.startswith("alert.conflict:")
    assert body["state"] == "raised"
    assert body["severity"] == "critical"
    await svc.flush_audit()
    assert audit.rows == [(body["key"], "raised")]


async def test_the_tick_clears_what_went_silent_and_says_so() -> None:
    bus, audit = RecordingBus(), RecordingAudit()
    svc, clock = service(bus, audit)
    await svc.on_telemetry(payload(A, 0, 10))
    await svc.on_telemetry(payload(B, 500, -10))

    clock.now_s = 20.0
    await svc.on_tick()

    assert [body["state"] for _, body in bus.sent] == ["raised", "cleared"]
    assert "reason" not in bus.sent[0][1]
    assert bus.sent[1][1]["reason"] == "stale"
    key = bus.sent[0][1]["key"]
    await svc.flush_audit()
    assert audit.rows == [(key, "raised"), (key, "cleared:stale")]


async def test_a_clear_shown_by_telemetry_is_published_as_resolved() -> None:
    bus = RecordingBus()
    svc, clock = service(bus)
    await svc.on_telemetry(payload(A, 0, 10))
    await svc.on_telemetry(payload(B, 500, -10))
    for at_s in (1.0, 3.0, 5.0):
        clock.now_s = at_s
        await svc.on_telemetry(payload(B, 500 + 10 * at_s, 10, at_s=at_s))

    assert [body["state"] for _, body in bus.sent] == ["raised", "cleared"]
    assert bus.sent[1][1]["reason"] == "resolved"


async def test_a_bus_failure_does_not_stop_the_audit_or_the_monitor() -> None:
    audit = RecordingAudit()
    svc, _ = service(RecordingBus(fail=True), audit)

    await svc.on_telemetry(payload(A, 0, 10))
    await svc.on_telemetry(payload(B, 500, -10))

    await svc.flush_audit()
    assert len(audit.rows) == 1
    assert len(svc.monitor.active) == 1


async def test_an_audit_failure_does_not_stop_the_publish_or_the_writer(
    caplog: pytest.LogCaptureFixture,
) -> None:
    bus = RecordingBus()
    audit = RecordingAudit(fail=True)
    svc, clock = service(bus, audit)

    await svc.on_telemetry(payload(A, 0, 10))
    await svc.on_telemetry(payload(B, 500, -10))
    await svc.flush_audit()
    assert len(bus.sent) == 1
    assert [r.step for r in _delivery_errors(caplog)] == ["audit"]

    # The writer survived the failure: the next row is written.
    audit.fail = False
    clock.now_s = 20.0
    await svc.on_tick()
    await svc.flush_audit()
    assert [state for _, state in audit.rows] == ["cleared:stale"]


def _delivery_errors(caplog: pytest.LogCaptureFixture) -> list[Any]:
    return [r for r in caplog.records if r.getMessage().startswith("could not deliver")]


async def test_the_audit_is_off_the_telemetry_path_and_written_in_order() -> None:
    """S-13. With the database stalled, telemetry is still evaluated and
    published; the rows are written, in order, once it answers."""
    gate = asyncio.Event()
    bus, audit = RecordingBus(), RecordingAudit(gate=gate)
    svc, clock = service(bus, audit)

    await svc.on_telemetry(payload(A, 0, 10))
    await svc.on_telemetry(payload(B, 500, -10))
    clock.now_s = 20.0
    await svc.on_tick()

    assert [body["state"] for _, body in bus.sent] == ["raised", "cleared"]
    assert audit.rows == []
    assert svc.audit_pending >= 1

    gate.set()
    await asyncio.wait_for(svc.flush_audit(), timeout=5.0)
    key = bus.sent[0][1]["key"]
    assert audit.rows == [(key, "raised"), (key, "cleared:stale")]
    assert svc.audit_overflow == 0
    await svc.close()


async def test_an_overflowing_audit_queue_is_counted_and_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The presence pair: a queue of one, a stalled database, and three
    transitions. What does not fit is dropped, and says so."""
    gate = asyncio.Event()
    bus, audit = RecordingBus(), RecordingAudit(gate=gate)
    clock = Clock()
    svc = AirspaceService(
        monitor=AirspaceMonitor(policy=POLICY, stale_after_s=15.0, clear_after_s=1.0),
        bus=bus,
        audit=audit,
        clock=clock,
        audit_queue_size=1,
    )
    await svc.on_telemetry(payload(A, 0, 10))
    await svc.on_telemetry(payload(B, 500, -10))
    for at_s in (1.0, 3.0):  # B turns away: the conflict resolves.
        clock.now_s = at_s
        await svc.on_telemetry(payload(B, 500 + 10 * at_s, 10, at_s=at_s))
    clock.now_s = 4.0
    await svc.on_telemetry(payload(B, 450, -10, at_s=4.0))  # and comes back.

    assert [body["state"] for _, body in bus.sent] == ["raised", "cleared", "raised"]
    assert svc.audit_overflow >= 1
    dropped: list[Any] = [
        r for r in caplog.records if r.getMessage().startswith("audit row dropped")
    ]
    assert len(dropped) == svc.audit_overflow
    assert dropped[0].audit_queue_size == 1
    assert dropped[0].key == bus.sent[0][1]["key"]

    gate.set()
    await asyncio.wait_for(svc.flush_audit(), timeout=5.0)
    assert len(audit.rows) + svc.audit_overflow == 3
    await svc.close()


class RecordingTiles:
    """A tile cache that notes which thread each read ran on."""

    def __init__(self, *, fail: bool = False) -> None:
        self.loaded: set[str] = set()
        self.load_threads: list[str] = []
        self.fail = fail

    def is_loaded(self, lat_deg: float, lon_deg: float) -> bool:
        return cell_name(lat_deg, lon_deg) in self.loaded

    def load(self, lat_deg: float, lon_deg: float) -> None:
        self.load_threads.append(threading.current_thread().name)
        if self.fail:
            raise TerrainFileError("index lists N41E044 but N41E044.pgm: missing")
        self.loaded.add(cell_name(lat_deg, lon_deg))


async def test_the_terrain_tile_is_read_once_off_the_loop_before_observing() -> None:
    """S-13. The first message over a cell reads its tile in a worker
    thread; later ones over the same cell read nothing."""
    tiles = RecordingTiles()
    bus = RecordingBus()
    svc, _ = service(bus)
    svc.tiles = tiles

    await svc.on_telemetry(payload(A, 0, 10))
    await svc.on_telemetry(payload(B, 500, -10))
    await svc.on_telemetry(payload(A, 10, 10, at_s=1.0))

    assert len(tiles.load_threads) == 1
    assert tiles.load_threads[0] != threading.main_thread().name
    assert tiles.loaded == {cell_name(LAT0, LON0)}
    assert len(bus.sent) == 1


async def test_a_tile_that_cannot_be_read_is_logged_and_the_message_still_counts(
    caplog: pytest.LogCaptureFixture,
) -> None:
    tiles = RecordingTiles(fail=True)
    bus = RecordingBus()
    svc, _ = service(bus)
    svc.tiles = tiles

    await svc.on_telemetry(payload(A, 0, 10))
    await svc.on_telemetry(payload(B, 500, -10))

    assert len(tiles.load_threads) == 2, "tried again: nothing was cached"
    failures: list[Any] = [
        r for r in caplog.records if r.getMessage() == "could not load the terrain tile"
    ]
    assert [r.drone_id for r in failures] == [str(A), str(B)]
    assert all(r.exc_info for r in failures)
    assert len(bus.sent) == 1


async def test_the_tick_logs_the_running_totals_on_its_cadence(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """B1. The counters are only useful if someone can see them: one status
    line per `status_every_s`, with the rejected totals."""
    caplog.set_level(logging.INFO, logger="airspace.service")
    bus = RecordingBus()
    svc, clock = service(bus)
    svc.status_every_s = 10.0
    await svc.on_telemetry(payload(A, 0, 10))
    await svc.on_telemetry(payload(B, 500, -10))
    # A backlog message: the station's offset is 0, this one is 30 s late.
    await svc.on_telemetry(payload(A, 0, 10, at_s=-30.0))

    for clock.now_s in (1.0, 5.0, 12.0):
        await svc.on_tick()

    lines: list[Any] = [
        r for r in caplog.records if r.getMessage() == "airspace monitor status"
    ]
    assert len(lines) == 2, "at the first tick and 10 s later, not every tick"
    assert lines[-1].rejected_backlog == 1
    assert lines[-1].active_alerts == 1
    assert lines[-1].tracked == 2
    assert svc.status()["rejected_backlog"] == 1


async def test_close_writes_what_is_queued() -> None:
    bus, audit = RecordingBus(), RecordingAudit()
    svc, _ = service(bus, audit)
    await svc.on_telemetry(payload(A, 0, 10))
    await svc.on_telemetry(payload(B, 500, -10))

    await svc.close()

    assert len(audit.rows) == 1
    assert svc._audit_writer is None


async def test_a_garbled_message_is_skipped_and_the_next_one_counts() -> None:
    bus = RecordingBus()
    svc, _ = service(bus)

    await svc.on_telemetry(b"not json")
    await svc.on_telemetry(b'{"no": "drone_id"}')
    await svc.on_telemetry(payload(A, 0, 10))
    await svc.on_telemetry(payload(B, 500, -10))

    assert len(bus.sent) == 1


async def test_a_non_finite_position_is_logged_and_the_next_message_counts(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """S-12: `inf` used to reach math.floor in the grid as OverflowError,
    which nothing caught."""
    bus = RecordingBus()
    svc, _ = service(bus)
    broken = json.loads(payload(A, 0, 10))
    broken["lat_deg"] = float("inf")

    await svc.on_telemetry(json.dumps(broken).encode())
    await svc.on_telemetry(payload(A, 0, 10))
    await svc.on_telemetry(payload(B, 500, -10))

    assert [r.getMessage() for r in caplog.records if r.levelname == "WARNING"] == [
        "unusable telemetry message"
    ]
    assert len(bus.sent) == 1


class FlakyService(AirspaceService):
    """Fails its first tick, then behaves."""

    ticks = 0

    async def on_tick(self) -> None:
        self.ticks += 1
        if self.ticks == 1:
            raise RuntimeError("tick blew up")
        await super().on_tick()


async def test_the_ticker_outlives_a_failing_tick_and_a_failing_refresh(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """S-13. The ticker clears stale alerts; if it died the alerts would show
    for ever. Its first tick raises, its first refresh raises, and it goes on
    to tick, refresh and clear the stale pair."""
    bus = RecordingBus()
    clock = Clock()
    svc = FlakyService(
        monitor=AirspaceMonitor(policy=POLICY, stale_after_s=15.0), bus=bus, clock=clock
    )
    await svc.on_telemetry(payload(A, 0, 10))
    await svc.on_telemetry(payload(B, 500, -10))
    clock.now_s = 20.0

    refreshes = 0
    stop = asyncio.Event()

    async def refresh() -> None:
        nonlocal refreshes
        refreshes += 1
        if refreshes == 1:
            raise ConnectionError("database is gone")
        if refreshes == 3:
            stop.set()

    await asyncio.wait_for(
        run_ticker(
            svc, stop=stop, tick_s=0.001, refresh_every_s=0.002, refresh=refresh
        ),
        timeout=5.0,
    )

    assert svc.ticks >= 3 and refreshes == 3
    assert [body["state"] for _, body in bus.sent] == ["raised", "cleared"]
    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert [r.getMessage().split(";")[0] for r in errors] == [
        "tick failed",
        "could not reload the zones, the policy or the height limit",
    ]
    assert all(r.exc_info for r in errors)


async def test_an_active_alert_is_refreshed_on_each_tick_but_not_audited() -> None:
    """The numbers move as the pair closes; the console must see them move,
    and the audit log must not fill with them."""
    bus, audit = RecordingBus(), RecordingAudit()
    svc, clock = service(bus, audit)
    await svc.on_telemetry(payload(A, 0, 10))
    await svc.on_telemetry(payload(B, 500, -10))

    clock.now_s = 1.0
    await svc.on_tick()

    assert [body["state"] for _, body in bus.sent] == ["raised", "active"]
    await svc.flush_audit()
    assert audit.rows == [(bus.sent[0][1]["key"], "raised")]

"""The service's obligations: publish and audit each transition once, and keep
going when either fails."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest

from airspace.cpa import SeparationPolicy, local_offset_m
from airspace.monitor import AirspaceMonitor, Alert
from airspace.service import AirspaceService

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
    def __init__(self, *, fail: bool = False) -> None:
        self.rows: list[tuple[str, str]] = []
        self.fail = fail

    async def record(self, alert: Alert, state: str) -> None:
        if self.fail:
            raise ConnectionError("database is gone")
        self.rows.append((alert.key, state))


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
    assert audit.rows == [(body["key"], "raised")]


async def test_the_tick_clears_what_went_silent() -> None:
    bus = RecordingBus()
    svc, clock = service(bus)
    await svc.on_telemetry(payload(A, 0, 10))
    await svc.on_telemetry(payload(B, 500, -10))

    clock.now_s = 20.0
    await svc.on_tick()

    assert [body["state"] for _, body in bus.sent] == ["raised", "cleared"]


async def test_a_bus_failure_does_not_stop_the_audit_or_the_monitor() -> None:
    audit = RecordingAudit()
    svc, _ = service(RecordingBus(fail=True), audit)

    await svc.on_telemetry(payload(A, 0, 10))
    await svc.on_telemetry(payload(B, 500, -10))

    assert len(audit.rows) == 1
    assert len(svc.monitor.active) == 1


async def test_an_audit_failure_does_not_stop_the_publish() -> None:
    bus = RecordingBus()
    svc, _ = service(bus, RecordingAudit(fail=True))

    await svc.on_telemetry(payload(A, 0, 10))
    await svc.on_telemetry(payload(B, 500, -10))

    assert len(bus.sent) == 1


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
    assert audit.rows == [(bus.sent[0][1]["key"], "raised")]

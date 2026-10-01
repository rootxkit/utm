"""U-15 in the airspace monitor: a source switched off is not judged.

A relay aircraft (A, on station gs-1) and a Remote ID aircraft (B, heard by
receiver rx-1) converge head on. Switching Remote ID off clears the conflict
as `source_disabled` and drops only B; while it is off, B's messages are
counted and not judged; switching it on again raises the conflict from B's
next message. Switching station gs-1 off drops only A.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine

from airspace.cpa import SeparationPolicy, local_offset_m
from airspace.monitor import AirspaceMonitor, ClearReason, conflict_key
from airspace.service import AirspaceService, EventsAuditLog
from airspace.tests.test_service import Clock, RecordingAudit, RecordingBus
from airspace.tests.zone_helpers import zone as make_zone
from common.sources import RELAY, REMOTE_ID, Control, SourceControlState

A = UUID(int=1)
B = UUID(int=2)
LAT0 = 41.7151
LON0 = 44.8271
POLICY = SeparationPolicy(
    t_cpa_max_s=60, d_horizontal_min_m=60, d_vertical_min_m=20, neighbour_radius_m=800
)


@dataclass
class Switches:
    state: SourceControlState = field(default_factory=SourceControlState)

    def set(self, source_type: str, instance_id: str | None, *, enabled: bool) -> None:
        kept = [
            c
            for c in self.state.controls
            if (c.source_type, c.instance_id) != (source_type, instance_id)
        ]
        kept.append(
            Control(
                source_type=source_type,
                instance_id=instance_id,
                enabled=enabled,
                reason="test",
                changed_by="test-admin",
                changed_at="2026-10-01T12:00:00+00:00",
            )
        )
        self.state = SourceControlState(
            version=self.state.version + 1, controls=tuple(kept)
        )

    def enabled(self, source_type: str, instance_id: str | None) -> bool:
        return self.state.enabled(source_type, instance_id)


def message(
    drone_id: UUID, north_m: float, vn: float, *, at_s: float = 0.0, remote_id: bool
) -> dict[str, Any]:
    n1, _ = local_offset_m(LAT0, LON0, LAT0 + 0.001, LON0)
    at = datetime.fromtimestamp(at_s, tz=UTC).isoformat()
    body: dict[str, Any] = {
        "drone_id": str(drone_id),
        "label": f"D{drone_id.int}",
        "ts": at,
        "rx_ts": at,
        "captured_at": at,
        "backlog": False,
        "lat_deg": LAT0 + 0.001 * north_m / n1,
        "lon_deg": LON0,
        "alt_amsl_m": 550.0,
        "vx_ms": vn,
        "vy_ms": 0.0,
        "vz_ms": 0.0,
    }
    if remote_id:
        return {
            **body,
            "source": "remote_id",
            "station_id": "rx-1",
            "armed": None,
            "airborne": True,
        }
    return {**body, "station_id": "gs-1", "armed": True}


def relay_a(at_s: float = 0.0) -> dict[str, Any]:
    return message(A, 0, 10, at_s=at_s, remote_id=False)


def rid_b(at_s: float = 0.0) -> dict[str, Any]:
    return message(B, 500, -10, at_s=at_s, remote_id=True)


def monitor(switches: Switches) -> AirspaceMonitor:
    return AirspaceMonitor(policy=POLICY, source_enabled=switches.enabled)


def converging(m: AirspaceMonitor, at_s: float = 0.0) -> None:
    m.observe(relay_a(at_s), now_s=at_s)
    m.observe(rid_b(at_s), now_s=at_s)


def test_switching_remote_id_off_clears_its_alerts_as_source_disabled() -> None:
    switches = Switches()
    m = monitor(switches)
    converging(m)
    assert [a.key for a in m.active] == [conflict_key(A, B)]

    switches.set(REMOTE_ID, None, enabled=False)
    change = m.apply_sources(now_s=1.0)

    assert change.raised == []
    assert [(c.alert.key, c.reason) for c in change.cleared] == [
        (conflict_key(A, B), ClearReason.SOURCE_DISABLED)
    ]
    assert m.active == []
    # Only the Remote ID aircraft is gone.
    assert m.tracked == 1
    assert m.dropped_source_disabled == 1


def test_while_switched_off_its_messages_are_counted_and_not_judged() -> None:
    switches = Switches()
    switches.set(REMOTE_ID, None, enabled=False)
    m = monitor(switches)

    for at_s in (0.0, 1.0, 2.0):
        change_a = m.observe(relay_a(at_s), now_s=at_s)
        change_b = m.observe(rid_b(at_s), now_s=at_s)
        assert change_a.raised == change_b.raised == []

    assert m.active == []
    assert m.rejected_source_disabled == 3
    assert m.tracked == 1


def test_switching_it_back_on_judges_its_next_message() -> None:
    switches = Switches()
    m = monitor(switches)
    converging(m)
    switches.set(REMOTE_ID, None, enabled=False)
    m.apply_sources(now_s=1.0)

    switches.set(REMOTE_ID, None, enabled=True)
    assert m.apply_sources(now_s=1.5).cleared == []
    m.observe(relay_a(2.0), now_s=2.0)
    raised = m.observe(rid_b(2.0), now_s=2.0).raised

    assert [a.key for a in raised] == [conflict_key(A, B)]
    assert m.tracked == 2


def test_switching_one_station_off_drops_only_its_aircraft() -> None:
    switches = Switches()
    m = monitor(switches)
    converging(m)

    switches.set(RELAY, "gs-1", enabled=False)
    change = m.apply_sources(now_s=1.0)

    assert [c.reason for c in change.cleared] == [ClearReason.SOURCE_DISABLED]
    assert m.tracked == 1
    # B is still judged: its messages are evaluated, not refused.
    m.observe(rid_b(2.0), now_s=2.0)
    assert m.rejected_source_disabled == 0


def test_another_station_of_the_same_type_is_not_touched() -> None:
    switches = Switches()
    switches.set(RELAY, "gs-2", enabled=False)
    m = monitor(switches)
    converging(m)

    assert m.apply_sources(now_s=1.0).cleared == []
    assert m.tracked == 2
    assert len(m.active) == 1


def test_a_disabled_message_without_notice_drops_its_own_track() -> None:
    """The switch reached the monitor's state but nothing called
    `apply_sources` yet: the aircraft's next message does it."""
    switches = Switches()
    m = monitor(switches)
    converging(m)
    switches.set(REMOTE_ID, "rx-1", enabled=False)

    change = m.observe(rid_b(1.0), now_s=1.0)

    assert [c.reason for c in change.cleared] == [ClearReason.SOURCE_DISABLED]
    assert m.tracked == 1


def test_a_disabled_source_does_not_drop_a_track_from_another_source() -> None:
    """One aircraft heard both ways (U-16): its relay track stands while its
    Remote ID broadcasts are refused."""
    switches = Switches()
    switches.set(REMOTE_ID, None, enabled=False)
    m = monitor(switches)
    m.observe(relay_a(0.0), now_s=0.0)

    same_aircraft_by_rid = {**message(A, 0, 10, remote_id=True), "station_id": "rx-1"}
    change = m.observe(same_aircraft_by_rid, now_s=0.5)

    assert change.cleared == []
    assert m.tracked == 1
    assert m.rejected_source_disabled == 1


def test_a_zone_alert_is_cleared_as_source_disabled_too() -> None:
    switches = Switches()
    square = {
        "type": "Polygon",
        "coordinates": [
            [
                [LON0 - 0.01, LAT0 - 0.01],
                [LON0 + 0.01, LAT0 - 0.01],
                [LON0 + 0.01, LAT0 + 0.01],
                [LON0 - 0.01, LAT0 + 0.01],
                [LON0 - 0.01, LAT0 - 0.01],
            ]
        ],
    }
    # A PROHIBITED zone (no-fly before U-03), from the ground up.
    zone = make_zone(
        zone_id=uuid4(),
        name="no-fly test",
        restriction="PROHIBITED",
        coordinates=square["coordinates"],
    )
    m = AirspaceMonitor(policy=POLICY, zones=[zone], source_enabled=switches.enabled)
    raised = m.observe(rid_b(0.0), now_s=0.0).raised
    assert len(raised) == 1

    switches.set(REMOTE_ID, None, enabled=False)
    cleared = m.apply_sources(now_s=1.0).cleared

    assert [(c.alert.kind.value, c.reason) for c in cleared] == [
        ("zone", ClearReason.SOURCE_DISABLED)
    ]


# --- the service: published and audited as source_disabled ---------------------


async def test_the_service_publishes_and_audits_the_clear_as_source_disabled() -> None:
    switches = Switches()
    bus, audit = RecordingBus(), RecordingAudit()
    clock = Clock()
    svc = AirspaceService(monitor=monitor(switches), bus=bus, audit=audit, clock=clock)
    await svc.on_telemetry(json.dumps(relay_a()).encode())
    await svc.on_telemetry(json.dumps(rid_b()).encode())

    switches.set(REMOTE_ID, None, enabled=False)
    clock.now_s = 1.0
    await svc.on_sources_changed()

    states = [(body["state"], body.get("reason")) for _, body in bus.sent]
    assert states == [("raised", None), ("cleared", "source_disabled")]
    await svc.flush_audit()
    key = conflict_key(A, B)
    assert audit.rows == [(key, "raised"), (key, "cleared:source_disabled")]
    assert svc.status()["dropped_source_disabled"] == 1


async def test_the_tick_applies_a_switch_whose_notice_was_missed() -> None:
    switches = Switches()
    bus = RecordingBus()
    clock = Clock()
    svc = AirspaceService(monitor=monitor(switches), bus=bus, clock=clock)
    await svc.on_telemetry(json.dumps(relay_a()).encode())
    await svc.on_telemetry(json.dumps(rid_b()).encode())

    switches.set(REMOTE_ID, None, enabled=False)
    clock.now_s = 1.0
    await svc.on_tick()

    cleared = [body for _, body in bus.sent if body["state"] == "cleared"]
    assert [body["reason"] for body in cleared] == ["source_disabled"]


@pytest.mark.postgres
async def test_the_audit_row_says_source_disabled(
    relational_engine: AsyncEngine,
) -> None:
    switches = Switches()
    m = monitor(switches)
    a, b = uuid4(), uuid4()
    m.observe(message(a, 0, 10, remote_id=False), now_s=0.0)
    m.observe(message(b, 500, -10, remote_id=True), now_s=0.0)
    switches.set(REMOTE_ID, None, enabled=False)
    [cleared] = m.apply_sources(now_s=1.0).cleared

    at = datetime(2026, 10, 1, 12, 0, 1, tzinfo=UTC)
    await EventsAuditLog(relational_engine).record(
        cleared.alert, "cleared", at=at, reason=cleared.reason
    )
    async with relational_engine.connect() as connection:
        rows = (
            await connection.execute(
                sa.text(
                    "SELECT event_type, payload FROM events WHERE entity_id IN (:a, :b)"
                ),
                {"a": str(a), "b": str(b)},
            )
        ).all()
    assert len(rows) == 2
    assert {row.payload["reason"] for row in rows} == {"source_disabled"}
    assert {row.event_type for row in rows} == {"airspace_alert_cleared"}

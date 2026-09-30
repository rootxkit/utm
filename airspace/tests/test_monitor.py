"""From telemetry messages to alerts: raised once, cleared with hysteresis,
and never for aircraft on the ground."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest

from airspace.cpa import SeparationPolicy, local_offset_m
from airspace.monitor import AirspaceMonitor, AlertKind, Severity, conflict_key
from airspace.zones import zone_from_geojson

A = UUID(int=1)
B = UUID(int=2)
LAT0 = 41.7151
LON0 = 44.8271
POLICY = SeparationPolicy(
    t_cpa_max_s=60, d_horizontal_min_m=60, d_vertical_min_m=20, neighbour_radius_m=800
)


def message(
    drone_id: UUID,
    north_m: float,
    *,
    vn: float = 0.0,
    armed: bool | None = True,
    alt_amsl_m: float = 550.0,
    label: str | None = None,
    at_s: float = 0.0,
) -> dict[str, Any]:
    """A message captured at `at_s` (epoch seconds), as the Gateway's `ts`."""
    n1, _ = local_offset_m(LAT0, LON0, LAT0 + 0.001, LON0)
    return {
        "drone_id": str(drone_id),
        "label": label or f"D{drone_id.int}",
        "ts": datetime.fromtimestamp(at_s, tz=UTC).isoformat(),
        "lat_deg": LAT0 + 0.001 * north_m / n1,
        "lon_deg": LON0,
        "alt_amsl_m": alt_amsl_m,
        "vx_ms": vn,
        "vy_ms": 0.0,
        "vz_ms": 0.0,
        "armed": armed,
    }


def head_on(monitor: AirspaceMonitor, *, now_s: float) -> list[Any]:
    """A at 0 heading north, B 500 m north heading south: CPA in 25 s."""
    monitor.observe(message(A, 0, vn=10, at_s=now_s), now_s=now_s)
    return monitor.observe(message(B, 500, vn=-10, at_s=now_s), now_s=now_s).raised


def test_a_head_on_pair_raises_one_critical_conflict_naming_both() -> None:
    monitor = AirspaceMonitor(policy=POLICY)
    raised = head_on(monitor, now_s=0.0)

    assert len(raised) == 1
    alert = raised[0]
    assert alert.kind is AlertKind.CONFLICT
    assert alert.severity is Severity.CRITICAL
    assert set(alert.drone_ids) == {A, B}
    assert set(alert.labels) == {"D1", "D2"}
    assert alert.detail["t_cpa_s"] == 25.0


def test_the_same_conflict_is_not_raised_again_every_tick() -> None:
    monitor = AirspaceMonitor(policy=POLICY)
    head_on(monitor, now_s=0.0)
    again = head_on(monitor, now_s=1.0)
    assert again == []
    assert len(monitor.active) == 1


def test_the_alert_is_the_same_whichever_aircraft_reported_last() -> None:
    """Both aircraft's messages describe the pair identically."""
    monitor = AirspaceMonitor(policy=POLICY)
    head_on(monitor, now_s=0.0)
    first = monitor.active[0]
    monitor.observe(message(A, 0, vn=10, at_s=1.0), now_s=1.0)
    second = monitor.active[0]
    assert first.key == second.key == conflict_key(A, B)
    assert first.drone_ids == second.drone_ids


def test_a_resolved_conflict_clears_only_after_the_hysteresis() -> None:
    monitor = AirspaceMonitor(policy=POLICY, clear_after_s=3.0)
    head_on(monitor, now_s=0.0)

    # B turns away: diverging and 500 m apart, no longer a conflict.
    at_1 = monitor.observe(message(B, 500, vn=10, at_s=1.0), now_s=1.0)
    at_3 = monitor.observe(message(B, 510, vn=10, at_s=3.0), now_s=3.0)
    at_4 = monitor.observe(message(B, 520, vn=10, at_s=3.5), now_s=3.5)

    assert at_1.cleared == [] and at_3.cleared == []
    assert [alert.key for alert in at_4.cleared] == [conflict_key(A, B)]
    assert monitor.active == []


def test_silence_does_not_clear_a_conflict_before_the_aircraft_are_stale() -> None:
    """The paired case: no message shows the pair apart, so it stays raised."""
    monitor = AirspaceMonitor(policy=POLICY, clear_after_s=3.0, stale_after_s=15.0)
    head_on(monitor, now_s=0.0)
    assert monitor.tick(now_s=10.0).cleared == []
    assert len(monitor.active) == 1


def test_a_pair_on_the_ground_raises_nothing() -> None:
    """The paired absence: the same geometry, disarmed."""
    monitor = AirspaceMonitor(policy=POLICY)
    monitor.observe(message(A, 0, vn=10, armed=False), now_s=0.0)
    change = monitor.observe(message(B, 20, vn=-10, armed=False), now_s=0.0)
    assert change.raised == []


def test_unknown_armed_state_is_not_treated_as_flying() -> None:
    monitor = AirspaceMonitor(policy=POLICY)
    monitor.observe(message(A, 0, armed=None), now_s=0.0)
    change = monitor.observe(message(B, 20, armed=True), now_s=0.0)
    assert change.raised == []


def test_disarming_clears_the_conflict() -> None:
    monitor = AirspaceMonitor(policy=POLICY)
    head_on(monitor, now_s=0.0)
    change = monitor.observe(message(B, 500, armed=False, at_s=1.0), now_s=1.0)
    assert [alert.key for alert in change.cleared] == [conflict_key(A, B)]


def test_an_aircraft_that_goes_silent_is_dropped_and_its_alerts_cleared() -> None:
    """The end of a condition can be the absence of telemetry; tick() sees it."""
    monitor = AirspaceMonitor(policy=POLICY, stale_after_s=15.0)
    head_on(monitor, now_s=0.0)

    assert monitor.tick(now_s=10.0).cleared == []
    cleared = monitor.tick(now_s=16.0).cleared
    assert [alert.key for alert in cleared] == [conflict_key(A, B)]
    assert len(monitor.index) == 0


def test_a_message_without_velocity_places_nothing() -> None:
    monitor = AirspaceMonitor(policy=POLICY)
    incomplete = message(A, 0)
    incomplete["vx_ms"] = None
    monitor.observe(incomplete, now_s=0.0)
    assert len(monitor.index) == 0


def zone(kind: str) -> Any:
    square = [
        [LON0 - 0.01, LAT0 - 0.01],
        [LON0 + 0.01, LAT0 - 0.01],
        [LON0 + 0.01, LAT0 + 0.01],
        [LON0 - 0.01, LAT0 + 0.01],
        [LON0 - 0.01, LAT0 - 0.01],
    ]
    return zone_from_geojson(
        zone_id=UUID(int=77),
        name="Parliament",
        zone_type=kind,
        geojson=json.dumps({"type": "Polygon", "coordinates": [square]}),
        min_alt_amsl_m=None,
        max_alt_amsl_m=None,
    )


def test_entering_a_no_fly_zone_is_critical_and_names_the_zone() -> None:
    monitor = AirspaceMonitor(policy=POLICY, zones=[zone("no_fly")])
    raised = monitor.observe(message(A, 0), now_s=0.0).raised
    assert len(raised) == 1
    assert raised[0].kind is AlertKind.ZONE
    assert raised[0].severity is Severity.CRITICAL
    assert raised[0].detail["zone_name"] == "Parliament"


def test_a_restricted_zone_is_a_warning() -> None:
    monitor = AirspaceMonitor(policy=POLICY, zones=[zone("restricted")])
    raised = monitor.observe(message(A, 0), now_s=0.0).raised
    assert raised[0].severity is Severity.WARNING


def test_outside_the_zone_raises_nothing_and_leaving_clears() -> None:
    monitor = AirspaceMonitor(policy=POLICY, zones=[zone("no_fly")], clear_after_s=3.0)
    far = 5_000.0
    assert monitor.observe(message(A, far), now_s=0.0).raised == []

    monitor.observe(message(A, 0, at_s=1.0), now_s=1.0)
    monitor.observe(message(A, far, at_s=2.0), now_s=2.0)
    cleared = monitor.observe(message(A, far, at_s=5.0), now_s=5.0).cleared
    assert len(cleared) == 1


def test_the_alert_serialises_for_the_bus() -> None:
    monitor = AirspaceMonitor(policy=POLICY)
    alert = head_on(monitor, now_s=0.0)[0]
    encoded = json.loads(json.dumps(alert.as_dict()))
    assert encoded["kind"] == "conflict"
    assert encoded["severity"] == "critical"
    assert sorted(encoded["drone_ids"]) == sorted([str(A), str(B)])


# --- S-11: time is the capture time -----------------------------------------


def not_evaluated(caplog: pytest.LogCaptureFixture) -> list[Any]:
    return [r for r in caplog.records if r.getMessage() == "telemetry not evaluated"]


def test_a_message_without_a_capture_time_is_refused() -> None:
    monitor = AirspaceMonitor(policy=POLICY)
    without = message(A, 0, vn=10)
    del without["ts"]
    with pytest.raises(ValueError, match="ts"):
        monitor.observe(without, now_s=0.0)


@pytest.mark.parametrize("stale_first", [True, False], ids=["A-stale", "B-stale"])
def test_a_neighbours_5_s_old_sample_gives_the_same_cpa_as_fresh_ones(
    stale_first: bool,
) -> None:
    """Head-on, meeting in 25 s. The stale aircraft's sample was taken 5 s
    earlier, 50 m back down its track; used as it stands it would say 27.5 s."""
    fresh = AirspaceMonitor(policy=POLICY)
    expected = head_on(fresh, now_s=5.0)[0].detail

    monitor = AirspaceMonitor(policy=POLICY)
    if stale_first:
        monitor.observe(message(A, -50, vn=10, at_s=0.0), now_s=0.0)
        raised = monitor.observe(message(B, 500, vn=-10, at_s=5.0), now_s=5.0).raised
    else:
        monitor.observe(message(B, 550, vn=-10, at_s=0.0), now_s=0.0)
        raised = monitor.observe(message(A, 0, vn=10, at_s=5.0), now_s=5.0).raised

    assert len(raised) == 1
    assert raised[0].detail["t_cpa_s"] == expected["t_cpa_s"] == 25.0
    assert raised[0].detail["d_horizontal_now_m"] == expected["d_horizontal_now_m"]


@pytest.mark.parametrize("stale_first", [True, False], ids=["B-first", "A-first"])
def test_a_conflict_that_exists_only_in_a_stale_sample_is_not_alerted(
    stale_first: bool,
) -> None:
    """A hovers. B's 5 s old sample has it 15 m south, heading north at
    20 m/s: taken as current that is a hit in 0.75 s, but by A's capture time
    B is 85 m past A and opening."""
    monitor = AirspaceMonitor(policy=POLICY)
    if stale_first:
        monitor.observe(message(B, -15, vn=20, at_s=0.0), now_s=0.0)
        change = monitor.observe(message(A, 0, at_s=5.0), now_s=5.0)
    else:
        monitor.observe(message(A, 0, at_s=5.0), now_s=5.0)
        change = monitor.observe(message(B, -15, vn=20, at_s=0.0), now_s=5.0)

    assert change.raised == []
    assert monitor.active == []


def test_the_same_geometry_with_fresh_samples_is_alerted() -> None:
    """The presence pair of the test above: 15 m apart and closing now."""
    monitor = AirspaceMonitor(policy=POLICY)
    monitor.observe(message(A, 0, at_s=5.0), now_s=5.0)
    raised = monitor.observe(message(B, -15, vn=20, at_s=5.0), now_s=5.0).raised
    assert [alert.kind for alert in raised] == [AlertKind.CONFLICT]
    assert raised[0].detail["t_cpa_s"] < 1.0


@pytest.mark.parametrize(("age_s", "alerts"), [(10.0, 1), (10.5, 0)])
def test_a_neighbour_older_than_the_maximum_age_is_left_out(
    age_s: float, alerts: int
) -> None:
    monitor = AirspaceMonitor(policy=POLICY, neighbour_max_age_s=10.0)
    monitor.observe(message(A, 0, vn=10, at_s=0.0), now_s=0.0)
    raised = monitor.observe(message(B, 500, vn=-10, at_s=age_s), now_s=age_s).raised
    assert len(raised) == alerts


@pytest.mark.parametrize("now_s", [100.0, -100.0], ids=["backlog", "clock-ahead"])
def test_telemetry_that_is_not_live_raises_nothing_and_is_counted(
    now_s: float, caplog: pytest.LogCaptureFixture
) -> None:
    """A replayed backlog, or a ground station whose clock is off by more
    than the live window either way: the pair is not evaluated."""
    monitor = AirspaceMonitor(policy=POLICY, live_max_age_s=10.0)
    monitor.observe(message(A, 0, vn=10, at_s=0.0), now_s=now_s)
    change = monitor.observe(message(B, 500, vn=-10, at_s=0.0), now_s=now_s)

    assert change.raised == []
    assert len(monitor.index) == 0
    assert monitor.rejected == 2
    assert [r.reason for r in not_evaluated(caplog)] == ["not live", "not live"]


def test_the_same_telemetry_within_the_live_window_is_evaluated() -> None:
    monitor = AirspaceMonitor(policy=POLICY, live_max_age_s=10.0)
    monitor.observe(message(A, 0, vn=10, at_s=0.0), now_s=9.0)
    raised = monitor.observe(message(B, 500, vn=-10, at_s=0.0), now_s=9.0).raised
    assert len(raised) == 1
    assert monitor.rejected == 0


def test_a_sample_older_than_the_one_held_is_ignored(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Out of order: the later message must not move the aircraft back."""
    monitor = AirspaceMonitor(policy=POLICY)
    monitor.observe(message(A, 0, at_s=5.0), now_s=5.0)
    monitor.observe(message(A, 500, at_s=2.0), now_s=5.0)
    monitor.observe(message(A, 500, at_s=2.5), now_s=5.0)

    held = monitor.index.track(A)
    assert held is not None and held.captured_at_s == 5.0
    assert monitor.rejected == 2
    assert len(not_evaluated(caplog)) == 1, "one line per aircraft per run"

    monitor.observe(message(A, 0, at_s=6.0), now_s=6.0)
    monitor.observe(message(A, 500, at_s=2.0), now_s=6.0)
    assert len(not_evaluated(caplog)) == 2

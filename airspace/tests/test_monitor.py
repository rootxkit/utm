"""From telemetry messages to alerts: raised once, cleared with hysteresis,
and never for aircraft on the ground."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest

from airspace.cpa import SeparationPolicy, closest_approach, local_offset_m
from airspace.monitor import (
    AirspaceMonitor,
    AlertKind,
    ClearReason,
    Severity,
    conflict_key,
)
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
    station: str = "gs-1",
    rx_at_s: float | None = None,
    backlog: bool = False,
    with_rx: bool = True,
    captured_at_s: float | None = None,
) -> dict[str, Any]:
    """A message as the Gateway publishes it: captured at `at_s` (epoch
    seconds) on `station`'s clock (`ts`), received by the Gateway at
    `rx_at_s` (`rx_ts`, the same instant unless said otherwise), flagged
    `backlog` by the Gateway. `with_rx` False leaves `rx_ts` out, as an
    older Gateway would."""
    n1, _ = local_offset_m(LAT0, LON0, LAT0 + 0.001, LON0)
    rx_s = at_s if rx_at_s is None else rx_at_s
    return {
        "drone_id": str(drone_id),
        "label": label or f"D{drone_id.int}",
        "ts": datetime.fromtimestamp(at_s, tz=UTC).isoformat(),
        **(
            {"rx_ts": datetime.fromtimestamp(rx_s, tz=UTC).isoformat()}
            if with_rx
            else {}
        ),
        **(
            {"captured_at": datetime.fromtimestamp(captured_at_s, tz=UTC).isoformat()}
            if captured_at_s is not None
            else {}
        ),
        "backlog": backlog,
        "station_id": station,
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
    assert [(c.alert.key, c.reason) for c in at_4.cleared] == [
        (conflict_key(A, B), ClearReason.RESOLVED)
    ]
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
    assert [(c.alert.key, c.reason) for c in change.cleared] == [
        (conflict_key(A, B), ClearReason.STALE)
    ]


def test_an_aircraft_that_goes_silent_is_dropped_and_its_alerts_cleared() -> None:
    """The end of a condition can be the absence of telemetry; tick() sees it."""
    monitor = AirspaceMonitor(policy=POLICY, stale_after_s=15.0)
    head_on(monitor, now_s=0.0)

    assert monitor.tick(now_s=10.0).cleared == []
    cleared = monitor.tick(now_s=16.0).cleared
    assert [(c.alert.key, c.reason) for c in cleared] == [
        (conflict_key(A, B), ClearReason.STALE)
    ]
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


# --- S-13: the policy can change while running -----------------------------


def test_a_re_read_policy_that_is_the_same_changes_nothing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="airspace.monitor")
    monitor = AirspaceMonitor(policy=POLICY)
    index = monitor.index
    assert monitor.update_policy(SeparationPolicy(60, 60, 20, 800)) is False
    assert monitor.index is index
    assert [r for r in caplog.records if "policy" in r.getMessage()] == []


def test_a_new_neighbour_radius_rebuilds_the_index_with_its_aircraft(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Head-on at 500 m is a conflict at the 800 m radius. Re-read with a
    400 m radius the pair are not neighbours; back at 800 m they are again,
    without either aircraft having to report."""
    caplog.set_level(logging.INFO, logger="airspace.monitor")
    monitor = AirspaceMonitor(policy=POLICY)
    head_on(monitor, now_s=0.0)
    assert len(monitor.index.neighbours(A)) == 1

    narrow = SeparationPolicy(60, 60, 20, neighbour_radius_m=400)
    assert monitor.update_policy(narrow) is True
    assert monitor.index.radius_m == 400 and len(monitor.index) == 2
    assert monitor.index.neighbours(A) == []

    assert monitor.update_policy(POLICY) is True
    assert len(monitor.index.neighbours(A)) == 1
    changes: list[Any] = [r for r in caplog.records if "policy" in r.getMessage()]
    assert [c.after["neighbour_radius_m"] for c in changes] == [400, 800]


def test_a_new_threshold_applies_to_the_next_message() -> None:
    """A pair 500 m apart and closing, meeting in 25 s: not a conflict once
    the lead time is 20 s, and one again at 60 s."""
    monitor = AirspaceMonitor(policy=SeparationPolicy(20, 60, 20, 800))
    head_on(monitor, now_s=0.0)
    assert monitor.active == []
    monitor.update_policy(POLICY)
    head_on(monitor, now_s=1.0)
    assert [alert.kind for alert in monitor.active] == [AlertKind.CONFLICT]


# --- S-12: a number that is not a number -----------------------------------


@pytest.mark.parametrize("value", [float("inf"), float("-inf"), float("nan")])
@pytest.mark.parametrize("name", ["lat_deg", "lon_deg", "alt_amsl_m", "vx_ms"])
def test_a_non_finite_number_is_refused_before_it_reaches_the_grid(
    name: str, value: float
) -> None:
    monitor = AirspaceMonitor(policy=POLICY)
    monitor.observe(message(B, 100), now_s=0.0)
    bad = message(A, 0)
    bad[name] = value
    with pytest.raises(ValueError, match=name):
        monitor.observe(bad, now_s=0.0)
    assert len(monitor.index) == 1


# --- S-11: time is the capture time -----------------------------------------


def not_evaluated(caplog: pytest.LogCaptureFixture) -> list[Any]:
    return [r for r in caplog.records if r.getMessage() == "telemetry not evaluated"]


def test_a_message_without_a_capture_time_is_evaluated_at_arrival_and_counted(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No producer omits `ts`; if one did, dropping its aircraft would cost
    alerts, so it is placed at its `rx_ts` all the same and the count says
    so (it only loses ordering within its source)."""
    monitor = AirspaceMonitor(policy=POLICY)
    for north_m, vn in ((0, 10), (10, 10)):
        without = message(A, north_m, vn=vn, at_s=5.0 + north_m / 10)
        del without["ts"]
        monitor.observe(without, now_s=5.0 + north_m / 10)
    raised = monitor.observe(message(B, 510, vn=-10, at_s=6.0), now_s=6.0).raised

    assert [alert.kind for alert in raised] == [AlertKind.CONFLICT]
    assert raised[0].detail["t_cpa_s"] == 25.0
    assert monitor.without_capture_time == 2
    warnings = [r for r in caplog.records if "missing a time field" in r.getMessage()]
    assert len(warnings) == 1, "once per aircraft"


def test_a_ts_that_is_not_a_timestamp_is_refused() -> None:
    monitor = AirspaceMonitor(policy=POLICY)
    with pytest.raises(ValueError, match="ts"):
        monitor.observe({**message(A, 0), "ts": 12345}, now_s=0.0)
    with pytest.raises(ValueError):
        monitor.observe({**message(A, 0), "ts": "yesterday"}, now_s=0.0)


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


@pytest.mark.parametrize(
    "skew_s", [60.0, -3600.0, 0.0], ids=["behind-1min", "ahead-1h", "true"]
)
def test_a_station_clock_off_by_any_amount_still_raises_the_conflict(
    skew_s: float,
) -> None:
    """B1. The station's `ts` reads `wall - skew`: a minute slow, an hour
    fast, or right. The track is placed at the Gateway's `rx_ts`, so its
    aircraft alert all the same, at the right CPA, and nothing is rejected."""
    monitor = AirspaceMonitor(policy=POLICY, live_max_age_s=10.0)
    for wall_s in (100.0, 101.0):
        north_m = 10 * (wall_s - 100.0)
        monitor.observe(
            message(A, north_m, vn=10, at_s=wall_s - skew_s, rx_at_s=wall_s),
            now_s=wall_s,
        )
    raised = monitor.observe(
        message(B, 510, vn=-10, at_s=101.0 - skew_s, rx_at_s=101.0), now_s=101.0
    ).raised

    assert [alert.kind for alert in raised] == [AlertKind.CONFLICT]
    assert raised[0].detail["t_cpa_s"] == 25.0
    assert monitor.rejected == 0


def test_two_stations_with_different_skews_are_compared_at_the_gateways_clock() -> None:
    """A on a station a minute slow, B on one that is right, both received
    by the Gateway at the same instant: the CPA is 25 s, not a minute of
    advance, and no station's skew had to be guessed."""
    monitor = AirspaceMonitor(policy=POLICY)
    monitor.observe(
        message(A, 0, vn=10, at_s=40.0, rx_at_s=100.0, station="slow"), now_s=100.0
    )
    raised = monitor.observe(
        message(B, 500, vn=-10, at_s=100.0, rx_at_s=100.0, station="right"),
        now_s=100.0,
    ).raised
    assert len(raised) == 1
    assert raised[0].detail["t_cpa_s"] == 25.0
    assert raised[0].detail["d_horizontal_now_m"] == 500.0


def replay(
    monitor: AirspaceMonitor, *, captured_wall_s: range, rx_s: float, speed: float = 1.0
) -> None:
    """B's positions from an outage, flagged `backlog` by the Gateway,
    delivered in one burst at `rx_s`. `speed` is how much faster than real
    time the relay drains; the flag makes it irrelevant."""
    for n, captured_s in enumerate(captured_wall_s):
        monitor.observe(
            message(
                B,
                100,
                vn=10,
                at_s=float(captured_s),
                rx_at_s=rx_s + n / speed,
                backlog=True,
            ),
            now_s=rx_s + n / speed,
        )


def test_a_relay_backlog_raises_nothing_and_live_records_alert_again(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """B1. After an outage the relay replays 36 s of B's positions, each
    flagged `backlog` by the Gateway (relay-v1 §5): none is evaluated, B is
    not even placed, and the reason is logged once. The first live record
    after them is evaluated and, head-on with A, raises the conflict."""
    monitor = AirspaceMonitor(policy=POLICY, live_max_age_s=10.0)
    for wall_s in (0.0, 1.0, 2.0):
        monitor.observe(message(A, 0, vn=10, at_s=wall_s), now_s=wall_s)

    replay(monitor, captured_wall_s=range(3, 39), rx_s=45.0, speed=36.0)
    assert monitor.rejected_backlog == 36
    assert monitor.index.track(B) is None
    logged = not_evaluated(caplog)
    assert [(r.reason, r.station_id) for r in logged] == [("backlog", "gs-1")]

    for wall_s in (44.0, 45.0, 46.0):
        monitor.observe(message(A, 0, vn=10, at_s=wall_s), now_s=wall_s)
    raised = monitor.observe(message(B, 500, vn=-10, at_s=46.0), now_s=46.0).raised
    assert [alert.kind for alert in raised] == [AlertKind.CONFLICT]
    assert monitor.rejected_backlog == 36


def test_review_scenario_1_a_source_first_heard_mid_replay_raises_nothing() -> None:
    """The monitor restarts while a relay is 30 min into a replay. Every
    replayed record is flagged by the Gateway, so a monitor with no history
    of the source evaluates none of them; the same records unflagged (the
    presence pair) would be evaluated."""
    fresh = AirspaceMonitor(policy=POLICY)
    fresh.observe(message(A, 0, vn=10, at_s=1800.0), now_s=1800.0)
    replay(fresh, captured_wall_s=range(0, 1800, 60), rx_s=1800.0, speed=10.0)
    assert fresh.rejected_backlog == 30 and fresh.active == []

    unflagged = AirspaceMonitor(policy=POLICY)
    unflagged.observe(message(A, 0, vn=10, at_s=1800.0), now_s=1800.0)
    unflagged.observe(message(B, 500, vn=-10, at_s=1800.0), now_s=1800.0)
    assert len(unflagged.active) == 1


def test_review_scenario_2_a_slow_replay_after_a_live_warm_up_raises_nothing() -> None:
    """A live warm-up, then a replay draining at only 1.5x real time for
    long enough that any estimator would have absorbed it. The flag does
    not estimate, so no replayed position is evaluated."""
    monitor = AirspaceMonitor(policy=POLICY, live_max_age_s=10.0)
    for wall_s in range(0, 60):
        monitor.observe(message(A, 0, vn=10, at_s=float(wall_s)), now_s=float(wall_s))
    replay(monitor, captured_wall_s=range(60, 960), rx_s=960.0, speed=1.5)
    assert monitor.rejected_backlog == 900
    assert monitor.index.track(B) is None and monitor.active == []


def test_review_scenario_3_one_future_stamped_ts_changes_nothing_after_it() -> None:
    """A station sends one record stamped an hour ahead, then sane ones.
    Nothing is pinned: the sane records are received later, so they are not
    out of order, and the pair alerts."""
    monitor = AirspaceMonitor(policy=POLICY)
    monitor.observe(message(A, 0, vn=10, at_s=3600.0, rx_at_s=0.0), now_s=0.0)
    for wall_s in (1.0, 2.0):
        monitor.observe(message(A, 10 * wall_s, vn=10, at_s=wall_s), now_s=wall_s)
    raised = monitor.observe(message(B, 520, vn=-10, at_s=2.0), now_s=2.0).raised
    assert [alert.kind for alert in raised] == [AlertKind.CONFLICT]
    assert monitor.rejected == 0
    held = monitor.index.track(A)
    assert held is not None and held.captured_at_s == 2.0


def test_a_gateway_that_is_behind_yields_late_alerts_not_none() -> None:
    """ADR-002: the Gateway is 47.6 s behind the relay, so `ts` is 47.6 s
    older than `rx_ts`, and one large frame carries both aircraft: A's
    sample captured 5 s before B's, under one `rx_ts`. Each is placed at
    its `captured_at`, so A is advanced 5 s and the pair alerts at 25 s,
    not at the 27.5 s that treating the frame as one instant would give."""
    monitor = AirspaceMonitor(policy=POLICY, live_max_age_s=10.0)
    lag_s = 47.6
    rx_s = 101.0
    monitor.observe(
        message(A, 0, vn=10, at_s=96.0 - lag_s, rx_at_s=rx_s, captured_at_s=96.0),
        now_s=rx_s,
    )
    raised = monitor.observe(
        message(B, 550, vn=-10, at_s=101.0 - lag_s, rx_at_s=rx_s, captured_at_s=101.0),
        now_s=rx_s,
    ).raised
    assert [alert.kind for alert in raised] == [AlertKind.CONFLICT]
    assert raised[0].detail["t_cpa_s"] == 25.0
    assert raised[0].detail["d_horizontal_now_m"] == 500.0
    assert monitor.rejected == 0


def test_placement_prefers_captured_at_and_lateness_uses_rx_ts() -> None:
    """A row placed 8 s behind its batch's `rx_ts` is not late: the batch
    reached us at once. The late window judges `rx_ts`; the position sits
    at `captured_at`."""
    monitor = AirspaceMonitor(policy=POLICY, live_max_age_s=10.0)
    monitor.observe(
        message(A, 0, at_s=100.0, rx_at_s=108.0, captured_at_s=100.0), now_s=108.5
    )
    held = monitor.index.track(A)
    assert held is not None and held.captured_at_s == 100.0
    assert monitor.rejected_late == 0
    # The same row reaching us 11 s after its batch was received is late.
    monitor.observe(
        message(A, 0, at_s=101.0, rx_at_s=109.0, captured_at_s=101.0), now_s=120.0
    )
    assert monitor.rejected_late == 1


@pytest.mark.parametrize(("behind_s", "alerts"), [(9.0, 1), (10.5, 0)])
def test_the_live_window_applies_to_the_gateway_to_monitor_leg_only(
    behind_s: float, alerts: int, caplog: pytest.LogCaptureFixture
) -> None:
    """`wall - rx_ts` beyond `live_max_age_s` is this service being behind;
    those messages are counted as late and not evaluated."""
    monitor = AirspaceMonitor(policy=POLICY, live_max_age_s=10.0)
    monitor.observe(message(A, 0, vn=10, at_s=100.0), now_s=100.0)
    raised = monitor.observe(
        message(B, 500, vn=-10, at_s=100.0), now_s=100.0 + behind_s
    ).raised
    assert len(raised) == alerts
    assert monitor.rejected_late == (0 if alerts else 1)
    assert [r.reason for r in not_evaluated(caplog)] == ([] if alerts else ["late"])


def test_a_message_without_rx_ts_is_placed_at_its_arrival_and_counted(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An older Gateway, or a test fixture: evaluated, never dropped."""
    monitor = AirspaceMonitor(policy=POLICY)
    monitor.observe(message(A, 0, vn=10, at_s=-500.0, with_rx=False), now_s=5.0)
    raised = monitor.observe(message(B, 500, vn=-10, at_s=5.0), now_s=5.0).raised

    assert [alert.kind for alert in raised] == [AlertKind.CONFLICT]
    assert raised[0].detail["t_cpa_s"] == 25.0, "placed at arrival, not at ts"
    assert monitor.without_receive_time == 1 and monitor.rejected == 0
    warnings: list[Any] = [
        r for r in caplog.records if "missing a time field" in r.getMessage()
    ]
    assert [r.missing for r in warnings] == [["rx_ts"]]


def test_messages_without_ts_do_not_disturb_ordering() -> None:
    monitor = AirspaceMonitor(policy=POLICY)
    for wall_s in (0.0, 1.0):
        without = message(A, 10 * wall_s, vn=10, at_s=wall_s)
        del without["ts"]
        monitor.observe(without, now_s=wall_s)
    monitor.observe(message(A, 20, vn=10, at_s=2.0), now_s=2.0)
    monitor.observe(message(A, 30, vn=10, at_s=3.0), now_s=3.0)
    assert monitor.rejected == 0 and monitor.without_capture_time == 2


def test_per_source_state_is_bounded_however_many_station_ids_appear() -> None:
    monitor = AirspaceMonitor(policy=POLICY, source_state_max=100)
    for n in range(5000):
        monitor.observe(
            message(A, 0, at_s=float(n), station=f"station-{n}"), now_s=float(n)
        )
    assert len(monitor._last_by_source_s) == 100
    assert monitor.rejected == 0
    with pytest.raises(ValueError, match="source_state_max"):
        AirspaceMonitor(policy=POLICY, source_state_max=0)


def test_two_sources_for_one_aircraft_are_not_ordered_against_each_other() -> None:
    """Relay and Remote ID both report the same aircraft; the relay's clock
    is a minute fast. Neither source's samples are rejected as older than
    the other's, so a resuming source does not go stale and re-raise."""
    monitor = AirspaceMonitor(policy=POLICY)
    monitor.observe(message(A, 0, at_s=60.0, rx_at_s=0.0, station="relay"), now_s=0.0)
    monitor.observe(message(A, 0, at_s=61.0, rx_at_s=1.0, station="relay"), now_s=1.0)
    monitor.observe(message(A, 0, at_s=1.5, station="rid"), now_s=1.5)
    monitor.observe(message(A, 0, at_s=62.0, rx_at_s=2.0, station="relay"), now_s=2.0)
    monitor.observe(message(A, 0, at_s=2.5, station="rid"), now_s=2.5)

    assert monitor.rejected == 0
    held = monitor.index.track(A)
    assert held is not None and held.source == "rid"
    assert held.captured_at_s == pytest.approx(2.5)
    # Within one source the order still holds: an older record received no
    # later than the last one (the same batch) is out of order.
    monitor.observe(message(A, 500, at_s=61.5, rx_at_s=2.0, station="relay"), now_s=3.0)
    assert monitor.rejected_out_of_order == 1
    held = monitor.index.track(A)
    assert held is not None and held.source == "rid"


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


# --- inside the minimum is a conflict, whatever t_cpa says -------------------


def test_a_hovering_pair_inside_the_minimum_stays_alerted() -> None:
    """Found in SITL: 29.9 m apart, hovering, velocity noise. The alert must
    be raised and must not clear as resolved while they stay there."""
    monitor = AirspaceMonitor(policy=POLICY, clear_after_s=3.0)
    cleared = []
    for t in range(0, 30):
        noise = 0.01 if t % 2 else -0.01
        for sample in (
            message(A, 0, vn=noise, at_s=float(t)),
            message(B, 29.9, vn=-noise, at_s=float(t)),
        ):
            cleared.extend(monitor.observe(sample, now_s=float(t)).cleared)
    assert [alert.kind for alert in monitor.active] == [AlertKind.CONFLICT]
    assert cleared == []


def test_a_hovering_pair_outside_the_minimum_raises_nothing() -> None:
    monitor = AirspaceMonitor(policy=POLICY)
    for t in range(0, 10):
        monitor.observe(message(A, 0, vn=0.01, at_s=float(t)), now_s=float(t))
        monitor.observe(message(B, 80, vn=-0.01, at_s=float(t)), now_s=float(t))
    assert monitor.active == []


def test_a_diverging_pair_clears_resolved_only_once_past_the_minimum() -> None:
    """B opens from A at 5 m/s: 40 m at t=0, 60 m at t=4. Inside until
    t=3 (55 m), shown clear from t=4, resolved once the hysteresis has
    passed since it was last shown inside: at t=7."""
    monitor = AirspaceMonitor(policy=POLICY, clear_after_s=3.0)
    monitor.observe(message(A, 0, at_s=0.0), now_s=0.0)
    changes = []
    for t in range(0, 9):
        changes.append(
            monitor.observe(message(B, 40 + 5 * t, vn=5, at_s=float(t)), now_s=float(t))
        )
    assert [alert.kind for alert in changes[0].raised] == [AlertKind.CONFLICT]
    cleared_at = [t for t, change in enumerate(changes) if change.cleared]
    assert cleared_at == [7], "last inside at t=3, shown clear from t=4"
    assert [c.reason for c in changes[7].cleared] == [ClearReason.RESOLVED]


# --- B2: a silent neighbour is not evidence ---------------------------------


def test_a_silent_neighbours_conflict_ends_stale_never_resolved() -> None:
    """Head-on at t=0. B goes silent; A reports every second. Once B's
    sample is too old to advance, A's messages cannot judge the pair, so
    the alert is neither refreshed nor shown clear. It ends when B is
    dropped as stale, with that reason, and not a second before."""
    monitor = AirspaceMonitor(
        policy=POLICY, stale_after_s=15.0, clear_after_s=3.0, neighbour_max_age_s=10.0
    )
    head_on(monitor, now_s=0.0)
    for t in range(1, 16):
        change = monitor.observe(
            message(A, 10 * t, vn=10, at_s=float(t)), now_s=float(t)
        )
        assert change.cleared == [], f"cleared at t={t}"
    assert len(monitor.active) == 1

    cleared = monitor.observe(message(A, 160, vn=10, at_s=16.0), now_s=16.0).cleared
    assert [(c.alert.key, c.reason) for c in cleared] == [
        (conflict_key(A, B), ClearReason.STALE)
    ]


def test_a_neighbour_that_keeps_reporting_can_still_resolve() -> None:
    """The presence pair: B reports too, diverging, and it resolves."""
    monitor = AirspaceMonitor(policy=POLICY, clear_after_s=3.0)
    head_on(monitor, now_s=0.0)
    cleared = []
    for t in (1, 2, 3, 4, 5):
        for sample in (
            message(A, 10 * t, vn=10, at_s=float(t)),
            message(B, 500 + 10 * t, vn=10, at_s=float(t)),
        ):
            cleared.extend(monitor.observe(sample, now_s=float(t)).cleared)
    assert [c.reason for c in cleared] == [ClearReason.RESOLVED]


def test_evidence_of_resolution_outranks_going_stale() -> None:
    """Both hold at once: B has shown the pair apart for longer than the
    hysteresis, and A has just gone stale. The clear says resolved."""
    monitor = AirspaceMonitor(policy=POLICY, stale_after_s=3.0, clear_after_s=3.0)
    head_on(monitor, now_s=0.0)
    monitor.observe(message(B, 510, vn=10, at_s=1.0), now_s=1.0)
    cleared = monitor.observe(message(B, 540, vn=10, at_s=3.5), now_s=3.5).cleared
    assert [c.reason for c in cleared] == [ClearReason.RESOLVED]
    assert monitor.tracked == 1, "A was dropped as stale in the same step"


def test_one_neighbours_failure_does_not_lose_the_others(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nit 7. Three aircraft; the CPA for any pair with C raises. The A-B
    conflict is still raised, and the pairs with C are not judged."""
    c_id = UUID(int=3)

    def flaky(a: Any, b: Any) -> Any:
        if c_id in (a.drone_id, b.drone_id):
            raise ZeroDivisionError("bad pair")
        return closest_approach(a, b)

    monkeypatch.setattr("airspace.monitor.closest_approach", flaky)
    monitor = AirspaceMonitor(policy=POLICY)
    monitor.observe(message(c_id, 20), now_s=0.0)
    monitor.observe(message(B, 500, vn=-10), now_s=0.0)
    raised = monitor.observe(message(A, 0, vn=10), now_s=0.0).raised

    assert [set(alert.drone_ids) for alert in raised] == [{A, B}]
    assert monitor.check_failures == 2, "B-C on B's message, A-C on A's"
    failures = [r for r in caplog.records if "pair is not judged" in r.getMessage()]
    assert len(failures) == 2 and all(r.exc_info for r in failures)


# --- one Remote ID transmitter under two ids (S-32) ---------------------------


def broadcast(drone_id: UUID, north_m: float, transmitter: str, **kw: Any) -> Any:
    """A Remote ID observation, as gateway/remote_id.py publishes it."""
    return {
        **message(drone_id, north_m, armed=None, **kw),
        "source": "remote_id",
        "airborne": True,
        "remote_id": {"transmitter": transmitter, "identified": True},
    }


def test_two_ids_of_one_transmitter_are_not_a_conflict() -> None:
    """Unidentified, then identified: one radio, never a pair."""
    monitor = AirspaceMonitor(policy=POLICY)

    monitor.observe(broadcast(A, 0, "02:55:16:00:00:01", vn=1.0), now_s=0.0)
    change = monitor.observe(
        broadcast(B, 1, "02:55:16:00:00:01", vn=1.0, at_s=0.5), now_s=0.5
    )

    assert change.raised == []


def test_two_transmitters_in_the_same_place_are_a_conflict() -> None:
    """The presence half: the same geometry, two radios."""
    monitor = AirspaceMonitor(policy=POLICY)

    monitor.observe(broadcast(A, 0, "02:55:16:00:00:01", vn=1.0), now_s=0.0)
    change = monitor.observe(
        broadcast(B, 1, "02:55:16:00:00:02", vn=1.0, at_s=0.5), now_s=0.5
    )

    assert [alert.kind for alert in change.raised] == [AlertKind.CONFLICT]


def test_a_transmitter_is_never_matched_against_mavlink() -> None:
    """A MAVLink track has no transmitter, so the rule cannot hide one."""
    monitor = AirspaceMonitor(policy=POLICY)

    monitor.observe(message(A, 0, vn=1.0), now_s=0.0)
    change = monitor.observe(
        broadcast(B, 1, "02:55:16:00:00:01", vn=1.0, at_s=0.5), now_s=0.5
    )

    assert [alert.kind for alert in change.raised] == [AlertKind.CONFLICT]

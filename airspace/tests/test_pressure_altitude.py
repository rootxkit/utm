"""A pressure altitude does not place an aircraft vertically. S-33.

The Gateway falls back to a Remote ID broadcast's pressure altitude when its
geodetic altitude is missing or poor, and says so in `alt_source`. Pressure
altitude is referenced to 1013.25 hPa, not the local QNH: about 8 m per hPa
off, some 160 m on a 20 hPa day, against a 20 m vertical minimum. So such a
track's vertical position is unknown: a conflict is judged on the horizontal
alone. Zone altitude bands are widened by the pressure uncertainty, and the
height limit is judged with it taken off; either alert is then a warning.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

import pytest

from airspace.cpa import closest_approach
from airspace.monitor import (
    AirspaceMonitor,
    AlertKind,
    Severity,
    track_from_telemetry,
)
from airspace.tests.test_height_limit import LIMIT_M, Slope
from airspace.tests.test_monitor import LAT0, LON0, POLICY, A, B, message
from airspace.tests.zone_helpers import square
from airspace.tests.zone_helpers import zone as make_zone
from airspace.zones import Zone

GEODETIC, PRESSURE = "geodetic", "pressure"


def at(
    drone_id: UUID, north_m: float, alt_amsl_m: float, source: str, **kw: Any
) -> dict[str, Any]:
    return {
        **message(drone_id, north_m, alt_amsl_m=alt_amsl_m, **kw),
        "alt_source": source,
    }


def converging(monitor: AirspaceMonitor, source_a: str, source_b: str) -> list[Any]:
    """Head-on, CPA in 25 s horizontally, 100 m apart vertically."""
    monitor.observe(at(A, 0, 500.0, source_a, vn=10), now_s=0.0)
    return monitor.observe(at(B, 500, 600.0, source_b, vn=-10), now_s=0.0).raised


# --- conflicts -----------------------------------------------------------------


def test_two_pressure_tracks_100_m_apart_vertically_still_conflict() -> None:
    raised = converging(AirspaceMonitor(policy=POLICY), PRESSURE, PRESSURE)

    assert [alert.kind for alert in raised] == [AlertKind.CONFLICT]
    detail = raised[0].detail
    assert detail["vertical_separation_known"] is False
    assert detail["d_alt_at_cpa_m"] is None
    assert detail["d_cpa_horizontal_m"] == pytest.approx(0.0, abs=0.5)


def test_the_same_pair_on_geodetic_altitudes_does_not() -> None:
    raised = converging(AirspaceMonitor(policy=POLICY), GEODETIC, GEODETIC)

    assert raised == []


def test_one_pressure_track_makes_the_pair_unknown() -> None:
    raised = converging(AirspaceMonitor(policy=POLICY), GEODETIC, PRESSURE)

    assert [alert.detail["vertical_separation_known"] for alert in raised] == [False]


def test_a_known_vertical_separation_is_said_to_be_known() -> None:
    monitor = AirspaceMonitor(policy=POLICY)
    monitor.observe(at(A, 0, 500.0, GEODETIC, vn=10), now_s=0.0)

    raised = monitor.observe(at(B, 500, 505.0, GEODETIC, vn=-10), now_s=0.0).raised

    assert raised[0].detail["vertical_separation_known"] is True
    assert raised[0].detail["d_alt_at_cpa_m"] == 5.0


def test_horizontally_clear_pressure_tracks_do_not_conflict() -> None:
    """Unknown vertically is not a conflict by itself."""
    monitor = AirspaceMonitor(policy=POLICY)
    monitor.observe(at(A, 0, 500.0, PRESSURE), now_s=0.0)

    raised = monitor.observe(at(B, 500, 500.0, PRESSURE), now_s=0.0).raised

    assert raised == []


def test_the_track_carries_the_flag_through_the_cpa() -> None:
    a = track_from_telemetry(at(A, 0, 500.0, PRESSURE, vn=10), arrived_at_s=0.0)
    b = track_from_telemetry(at(B, 500, 600.0, GEODETIC, vn=-10), arrived_at_s=1.0)
    absent = track_from_telemetry(message(B, 500, vn=-10), arrived_at_s=1.0)

    assert a is not None and b is not None and absent is not None
    assert (a.vertical_known, b.vertical_known, absent.vertical_known) == (
        False,
        True,
        True,
    )
    # `a` is advanced to `b`'s time, and keeps its flag.
    assert closest_approach(a, b).vertical_known is False


# --- zones and the height limit ------------------------------------------------


def zone(min_alt_amsl_m: float | None, max_alt_amsl_m: float | None) -> Zone:
    """A PROHIBITED zone (no-fly before U-03) with an AMSL band."""
    return make_zone(
        zone_id=UUID(int=78),
        name="Band",
        restriction="PROHIBITED",
        coordinates=square(LAT0, LON0),
        lower=None if min_alt_amsl_m is None else (min_alt_amsl_m, "AMSL"),
        upper=None if max_alt_amsl_m is None else (max_alt_amsl_m, "AMSL"),
    )


def test_a_pressure_track_inside_a_no_fly_band_stays_critical() -> None:
    """Indicated inside the band itself: the zone's own severity, flagged."""
    monitor = AirspaceMonitor(policy=POLICY, zones=[zone(400.0, 600.0)])

    raised = monitor.observe(at(A, 0, 500.0, PRESSURE), now_s=0.0).raised

    assert [alert.kind for alert in raised] == [AlertKind.ZONE]
    assert raised[0].severity is Severity.CRITICAL
    assert raised[0].detail["vertical_known"] is False
    assert raised[0].detail["within_band"] is True
    assert raised[0].detail["pressure_uncertainty_m"] == 250.0
    assert monitor.vertical_unknown == 1


def test_inside_only_the_widened_band_is_a_warning() -> None:
    monitor = AirspaceMonitor(policy=POLICY, zones=[zone(400.0, 600.0)])

    raised = monitor.observe(at(A, 0, 800.0, PRESSURE), now_s=0.0).raised

    assert raised[0].severity is Severity.WARNING
    assert raised[0].detail["within_band"] is False
    assert raised[0].detail["vertical_known"] is False


def test_a_severity_change_is_raised_again_not_changed_in_place() -> None:
    """Critical inside the band, then only inside the widened band: the
    warning is a transition the bus and audit log carry."""
    monitor = AirspaceMonitor(policy=POLICY, zones=[zone(400.0, 600.0)])
    first = monitor.observe(at(A, 0, 500.0, GEODETIC), now_s=0.0).raised

    changed = monitor.observe(at(A, 0, 800.0, PRESSURE, at_s=1.0), now_s=1.0).raised
    same = monitor.observe(at(A, 0, 810.0, PRESSURE, at_s=2.0), now_s=2.0).raised
    back = monitor.observe(at(A, 0, 500.0, PRESSURE, at_s=3.0), now_s=3.0).raised

    assert [a.severity for a in first] == [Severity.CRITICAL]
    assert [a.severity for a in changed] == [Severity.WARNING]
    assert changed[0].key == first[0].key
    # The same severity again is a refresh, not a transition.
    assert same == []
    assert [a.severity for a in back] == [Severity.CRITICAL]
    assert [a.severity for a in monitor.active] == [Severity.CRITICAL]


@pytest.mark.parametrize(
    ("alt_amsl_m", "source", "alerts"),
    [
        # 200 m over the band's top: inside it widened by 250 m.
        (800.0, PRESSURE, 1),
        (800.0, GEODETIC, 0),
        # Beyond the margin, below and above.
        (100.0, PRESSURE, 0),
        (900.0, PRESSURE, 0),
    ],
)
def test_a_band_is_widened_by_the_pressure_uncertainty(
    alt_amsl_m: float, source: str, alerts: int
) -> None:
    monitor = AirspaceMonitor(policy=POLICY, zones=[zone(400.0, 600.0)])

    raised = monitor.observe(at(A, 0, alt_amsl_m, source), now_s=0.0).raised

    assert len(raised) == alerts


def test_the_margin_is_configurable() -> None:
    monitor = AirspaceMonitor(
        policy=POLICY, zones=[zone(400.0, 600.0)], pressure_uncertainty_m=100.0
    )

    raised = monitor.observe(at(A, 0, 800.0, PRESSURE), now_s=0.0).raised

    assert raised == []


def test_a_geodetic_track_in_a_no_fly_band_stays_critical() -> None:
    monitor = AirspaceMonitor(policy=POLICY, zones=[zone(400.0, 600.0)])

    raised = monitor.observe(at(A, 0, 500.0, GEODETIC), now_s=0.0).raised

    assert raised[0].severity is Severity.CRITICAL
    assert "vertical_known" not in raised[0].detail


def test_a_zone_without_altitude_limits_is_judged_as_for_anyone() -> None:
    monitor = AirspaceMonitor(policy=POLICY, zones=[zone(None, None)])

    raised = monitor.observe(at(A, 0, 500.0, PRESSURE), now_s=0.0).raised

    assert raised[0].severity is Severity.CRITICAL
    assert "vertical_known" not in raised[0].detail


def test_leaving_the_widened_band_on_pressure_clears() -> None:
    monitor = AirspaceMonitor(
        policy=POLICY, zones=[zone(400.0, 600.0)], clear_after_s=3.0
    )
    monitor.observe(at(A, 0, 500.0, PRESSURE), now_s=0.0)

    cleared = []
    for t_s in range(1, 6):
        cleared += monitor.observe(
            at(A, 0, 1000.0, PRESSURE, at_s=float(t_s)), now_s=float(t_s)
        ).cleared

    assert [c.alert.kind for c in cleared] == [AlertKind.ZONE]


def height_monitor() -> AirspaceMonitor:
    return AirspaceMonitor(policy=POLICY, terrain=Slope(), max_height_agl_m=LIMIT_M)


@pytest.mark.parametrize(
    ("alt_amsl_m", "alerts"),
    [
        # Ground 500 m, limit 120 m: indicated 200 m and 150 m up.
        (700.0, 1),
        (650.0, 1),
        # Indicated 100 m up: under the limit.
        (600.0, 0),
    ],
)
def test_on_pressure_the_height_limit_is_judged_as_indicated(
    alt_amsl_m: float, alerts: int
) -> None:
    raised = height_monitor().observe(at(A, 0, alt_amsl_m, PRESSURE), now_s=0.0).raised

    assert len(raised) == alerts
    for alert in raised:
        assert alert.kind is AlertKind.HEIGHT
        assert alert.severity is Severity.WARNING
        assert alert.detail["vertical_known"] is False
        assert alert.detail["pressure_uncertainty_m"] == 250.0


def test_a_geodetic_height_alert_is_not_flagged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="airspace.monitor")
    geodetic, pressure = height_monitor(), height_monitor()

    known = geodetic.observe(at(A, 0, 650.0, GEODETIC), now_s=0.0).raised
    for t_s in (0.0, 1.0):
        pressure.observe(at(A, 0, 650.0, PRESSURE, at_s=t_s), now_s=t_s)

    assert "vertical_known" not in known[0].detail
    assert pressure.vertical_unknown == 2
    logged = [r for r in caplog.records if "pressure altitude" in r.getMessage()]
    assert len(logged) == 1, "once per aircraft, not per message"

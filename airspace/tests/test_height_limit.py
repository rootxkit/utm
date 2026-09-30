"""Height above ground against the limit. P5-19.

The monitor computes the height from the AMSL altitude and the ground under
the aircraft, never from telemetry's height above home.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest

from airspace.monitor import AirspaceMonitor, AlertKind, Severity, height_key
from airspace.tests.test_monitor import LAT0, POLICY, message
from common.terrain import Elevation

A = UUID(int=1)
LIMIT_M = 120.0
METRES_PER_DEG_LAT = 111_132.0


class Slope:
    """Ground at `base_m`, falling `fall_per_m` metres per metre north of
    LAT0. Unknown north of `known_to_m`."""

    def __init__(
        self, base_m: float = 500.0, fall_per_m: float = 0.0, known_to_m: float = 1e9
    ) -> None:
        self.base_m = base_m
        self.fall_per_m = fall_per_m
        self.known_to_m = known_to_m

    def north_m(self, lat_deg: float) -> float:
        return (lat_deg - LAT0) * METRES_PER_DEG_LAT

    def elevation(self, lat_deg: float, lon_deg: float) -> Elevation | None:
        north_m = self.north_m(lat_deg)
        if north_m > self.known_to_m:
            return None
        return Elevation(
            elevation_m=self.base_m - self.fall_per_m * north_m,
            dataset="COP-DEM GLO-30",
            spacing_m=30.9,
        )


def monitor(terrain: Any = None, limit: float | None = LIMIT_M) -> AirspaceMonitor:
    return AirspaceMonitor(
        policy=POLICY,
        terrain=Slope() if terrain is None else terrain,
        max_height_agl_m=limit,
    )


def test_above_the_limit_raises_one_warning_with_the_numbers() -> None:
    m = monitor()

    raised = m.observe(message(A, 0, alt_amsl_m=650.0), now_s=0.0).raised

    assert len(raised) == 1
    alert = raised[0]
    assert alert.kind is AlertKind.HEIGHT
    assert alert.severity is Severity.WARNING
    assert alert.key == height_key(A)
    assert alert.drone_ids == (A,)
    assert alert.detail == {
        "height_agl_m": 150.0,
        "max_height_agl_m": LIMIT_M,
        "alt_amsl_m": 650.0,
        "ground_elevation_m": 500.0,
        "dataset": "COP-DEM GLO-30",
    }
    assert m.observe(message(A, 0, alt_amsl_m=655.0, at_s=1.0), now_s=1.0).raised == []
    assert m.active[0].detail["height_agl_m"] == 155.0


@pytest.mark.parametrize(("alt_amsl_m", "alerts"), [(619.0, 0), (620.0, 0), (620.5, 1)])
def test_the_limit_itself_is_allowed(alt_amsl_m: float, alerts: int) -> None:
    m = monitor()
    assert len(m.observe(message(A, 0, alt_amsl_m=alt_amsl_m), now_s=0.0).raised) == (
        alerts
    )


def test_over_falling_ground_the_alert_comes_where_the_ground_drops_away() -> None:
    """Constant AMSL, heading north over ground falling 0.1 m per metre: 99.5 m
    above ground at the start, so the limit is crossed 205 m north and the
    first sample past it is 210 m. Neither the altitude nor the height above
    home changes; only the ground does."""
    m = monitor(Slope(base_m=500.0, fall_per_m=0.1))
    first_alert_m = None
    for step, north_m in enumerate(range(0, 400, 10)):
        sample = message(A, north_m, alt_amsl_m=599.5, vn=10, at_s=step)
        change = m.observe(sample, now_s=step)
        if change.raised and first_alert_m is None:
            first_alert_m = north_m

    assert first_alert_m == 210


def test_it_clears_once_below_the_limit_for_longer_than_the_hysteresis() -> None:
    m = monitor()
    m.observe(message(A, 0, alt_amsl_m=650.0), now_s=0.0)

    assert m.observe(message(A, 0, alt_amsl_m=600.0, at_s=1.0), now_s=1.0).cleared == []
    assert m.observe(message(A, 0, alt_amsl_m=600.0, at_s=3.0), now_s=3.0).cleared == []
    cleared = m.observe(message(A, 0, alt_amsl_m=600.0, at_s=4.5), now_s=4.5).cleared
    assert [alert.key for alert in cleared] == [height_key(A)]


def test_unknown_ground_is_not_evaluated_rather_than_taken_as_zero() -> None:
    m = monitor(Slope(known_to_m=100.0))

    assert m.observe(message(A, 500, alt_amsl_m=5000.0), now_s=0.0).raised == []
    raised = m.observe(message(A, 0, alt_amsl_m=5000.0, at_s=1.0), now_s=1.0).raised
    assert [alert.kind for alert in raised] == [AlertKind.HEIGHT]


def test_without_terrain_or_without_a_limit_nothing_is_evaluated() -> None:
    no_terrain = AirspaceMonitor(policy=POLICY, max_height_agl_m=LIMIT_M)
    no_limit = monitor(limit=None)

    for m in (no_terrain, no_limit):
        assert m.observe(message(A, 0, alt_amsl_m=5000.0), now_s=0.0).raised == []


def test_an_aircraft_on_the_ground_is_not_evaluated() -> None:
    """Disarmed at a base on a hilltop above a valley is not 'too high'."""
    m = monitor()
    assert (
        m.observe(message(A, 0, alt_amsl_m=650.0, armed=False), now_s=0.0).raised == []
    )


def test_a_remote_id_aircraft_declared_airborne_is_evaluated() -> None:
    m = monitor()
    broadcast = {**message(A, 0, alt_amsl_m=650.0, armed=None), "airborne": True}

    raised = m.observe(broadcast, now_s=0.0).raised

    assert [alert.kind for alert in raised] == [AlertKind.HEIGHT]

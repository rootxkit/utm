"""Closest point of approach. P5-07's criterion: head-on, crossing, overtaking,
parallel, and the zero-relative-velocity case, plus the two decisions the
formula alone does not make (diverging pairs, vertical separation)."""

from __future__ import annotations

import math
from uuid import UUID

import pytest

from airspace.cpa import (
    SeparationPolicy,
    Track,
    closest_approach,
    horizontal_distance_m,
    local_offset_m,
)

A = UUID(int=1)
B = UUID(int=2)
LAT0 = 41.7151
LON0 = 44.8271
# Stage 0 values from ARCHITECTURE.md §6.2, as the migration seeds them.
POLICY = SeparationPolicy(
    t_cpa_max_s=60, d_horizontal_min_m=60, d_vertical_min_m=20, neighbour_radius_m=800
)


def at(
    drone_id: UUID,
    north_m: float,
    east_m: float,
    *,
    vn: float = 0.0,
    ve: float = 0.0,
    vd: float = 0.0,
    alt_amsl_m: float = 550.0,
) -> Track:
    """A track at a metric offset from the origin, so tests speak in metres."""
    # Invert the local projection numerically (it is linear at this scale).
    n1, _ = local_offset_m(LAT0, LON0, LAT0 + 0.001, LON0)
    _, e1 = local_offset_m(LAT0, LON0, LAT0, LON0 + 0.001)
    return Track(
        drone_id=drone_id,
        lat_deg=LAT0 + 0.001 * north_m / n1,
        lon_deg=LON0 + 0.001 * east_m / e1,
        alt_amsl_m=alt_amsl_m,
        vn_ms=vn,
        ve_ms=ve,
        vd_ms=vd,
    )


def test_the_projection_matches_a_known_distance() -> None:
    """0.01 deg of latitude at 41.7 N is 1,110.7 m on WGS84."""
    north_m, east_m = local_offset_m(LAT0, LON0, LAT0 + 0.01, LON0)
    assert north_m == pytest.approx(1110.7, abs=1.0)
    assert east_m == pytest.approx(0.0, abs=1e-6)


def test_head_on() -> None:
    """1 km apart, each at 10 m/s towards the other: meet in 50 s, at 0 m."""
    a = at(A, 0, 0, vn=10)
    b = at(B, 1000, 0, vn=-10)
    approach = closest_approach(a, b)
    assert approach.t_cpa_s == pytest.approx(50.0, rel=1e-3)
    assert approach.d_cpa_horizontal_m == pytest.approx(0.0, abs=0.5)
    assert POLICY.is_conflict(approach)


def test_crossing_at_right_angles() -> None:
    """A heads north, B heads west, both reach the same point in 30 s."""
    a = at(A, -300, 0, vn=10)
    b = at(B, 0, 300, ve=-10)
    approach = closest_approach(a, b)
    assert approach.t_cpa_s == pytest.approx(30.0, rel=1e-3)
    assert approach.d_cpa_horizontal_m == pytest.approx(0.0, abs=0.5)


def test_crossing_that_misses() -> None:
    """Same geometry, B 20 s late: they pass 200/sqrt(2) = 141 m apart."""
    a = at(A, -300, 0, vn=10)
    b = at(B, 0, 500, ve=-10)
    approach = closest_approach(a, b)
    assert approach.d_cpa_horizontal_m == pytest.approx(200 / math.sqrt(2), rel=1e-3)
    assert not POLICY.is_conflict(approach)


def test_overtaking() -> None:
    """Same track, B 200 m behind and 5 m/s faster: caught up in 40 s."""
    a = at(A, 200, 0, vn=10)
    b = at(B, 0, 0, vn=15)
    approach = closest_approach(a, b)
    assert approach.t_cpa_s == pytest.approx(40.0, rel=1e-3)
    assert approach.d_cpa_horizontal_m == pytest.approx(0.0, abs=0.5)


def test_parallel_at_the_same_speed_is_a_constant_distance() -> None:
    """Zero relative velocity: every time is equally close, so t_cpa is now."""
    a = at(A, 0, 0, vn=10)
    b = at(B, 0, 45, vn=10)
    approach = closest_approach(a, b)
    assert approach.t_cpa_s == 0.0
    assert approach.d_cpa_horizontal_m == pytest.approx(45.0, rel=1e-3)
    # 45 m apart and staying so is a conflict under a 60 m minimum.
    assert POLICY.is_conflict(approach)


def test_both_hovering_is_the_same_degenerate_case() -> None:
    approach = closest_approach(at(A, 0, 0), at(B, 30, 40))
    assert approach.t_cpa_s == 0.0
    assert approach.d_cpa_horizontal_m == pytest.approx(50.0, rel=1e-3)


def test_a_diverging_pair_is_judged_on_where_it_is_now() -> None:
    """The approach is in the past; the closest from now on is now."""
    a = at(A, 0, 0, vn=-10)
    b = at(B, 100, 0, vn=10)
    approach = closest_approach(a, b)
    assert approach.t_cpa_s == 0.0
    assert approach.d_cpa_horizontal_m == pytest.approx(100.0, rel=1e-3)
    assert not POLICY.is_conflict(approach)


def test_a_diverging_pair_that_is_already_too_close_is_still_a_conflict() -> None:
    """The paired presence: diverging is not a pass if they are 10 m apart."""
    a = at(A, 0, 0, vn=-1)
    b = at(B, 10, 0, vn=1)
    assert POLICY.is_conflict(closest_approach(a, b))


def test_vertical_separation_at_cpa_prevents_the_alert() -> None:
    """Head-on horizontally, but 50 m apart in altitude: no conflict."""
    a = at(A, 0, 0, vn=10, alt_amsl_m=550)
    b = at(B, 1000, 0, vn=-10, alt_amsl_m=600)
    approach = closest_approach(a, b)
    assert approach.d_alt_at_cpa_m == pytest.approx(50.0)
    assert not POLICY.is_conflict(approach)


def test_a_climb_that_closes_the_vertical_gap_is_a_conflict() -> None:
    """Down is positive: A climbing at 1 m/s (vd = -1) closes 50 m in 50 s."""
    a = at(A, 0, 0, vn=10, vd=-1.0, alt_amsl_m=550)
    b = at(B, 1000, 0, vn=-10, alt_amsl_m=600)
    approach = closest_approach(a, b)
    assert approach.d_alt_at_cpa_m == pytest.approx(0.0, abs=0.1)
    assert POLICY.is_conflict(approach)


def test_a_conflict_too_far_ahead_is_not_alerted_yet() -> None:
    """Meeting in 100 s is beyond the 60 s window."""
    a = at(A, 0, 0, vn=5)
    b = at(B, 1000, 0, vn=-5)
    approach = closest_approach(a, b)
    assert approach.t_cpa_s == pytest.approx(100.0, rel=1e-3)
    assert not POLICY.is_conflict(approach)


def test_the_result_does_not_depend_on_the_order_of_the_pair() -> None:
    a = at(A, -300, 0, vn=10)
    b = at(B, 0, 500, ve=-10)
    one, other = closest_approach(a, b), closest_approach(b, a)
    assert one.t_cpa_s == pytest.approx(other.t_cpa_s)
    assert one.d_cpa_horizontal_m == pytest.approx(other.d_cpa_horizontal_m, abs=1e-6)


def test_horizontal_distance_agrees_with_the_approach_now() -> None:
    a, b = at(A, 0, 0), at(B, 300, 400)
    assert horizontal_distance_m(a, b) == pytest.approx(500.0, rel=1e-3)

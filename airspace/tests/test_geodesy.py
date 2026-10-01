"""Vincenty's inverse on WGS-84, pinned against values published elsewhere."""

from __future__ import annotations

import math

import pytest

from airspace.geodesy import A_M, distance_m


def dms(degrees: float, minutes: float, seconds: float) -> float:
    sign = -1 if degrees < 0 else 1
    return sign * (abs(degrees) + minutes / 60 + seconds / 3600)


def test_flinders_peak_to_buninyong() -> None:
    """Geoscience Australia's worked example of Vincenty's inverse (GRS80,
    which differs from WGS-84 in the flattening's 10th digit): 54 972.271 m."""
    flinders = (dms(-37, 57, 3.72030), dms(144, 25, 29.52440))
    buninyong = (dms(-37, 39, 10.15610), dms(143, 55, 35.38390))
    assert distance_m(*flinders, *buninyong) == pytest.approx(54_972.271, abs=0.001)


def test_one_degree_of_longitude_on_the_equator_is_the_semi_major_axis_arc() -> None:
    assert distance_m(0.0, 0.0, 0.0, 1.0) == pytest.approx(
        A_M * math.pi / 180, abs=1e-6
    )


def test_the_quarter_meridian() -> None:
    """Equator to pole on WGS-84: 10 001 965.729 m (the meridian arc)."""
    assert distance_m(0.0, 0.0, 90.0, 0.0) == pytest.approx(10_001_965.729, abs=0.001)


def test_the_same_point_is_zero_and_the_distance_is_symmetric() -> None:
    assert distance_m(41.7, 44.8, 41.7, 44.8) == 0.0
    there = distance_m(41.7151, 44.8271, 41.7239, 44.8401)
    back = distance_m(41.7239, 44.8401, 41.7151, 44.8271)
    assert there == pytest.approx(back, abs=1e-9)


def test_it_differs_from_the_sphere_where_a_zone_edge_would_notice() -> None:
    """1 km north at Tbilisi: the sphere is off by about 3 m, the ellipsoid
    is what PostGIS's geography buffer draws."""
    from airspace.zones import great_circle_m

    lat2 = 41.7151 + 1000 / 111_053.0
    ellipsoid = distance_m(41.7151, 44.8271, lat2, 44.8271)
    sphere = great_circle_m(41.7151, 44.8271, lat2, 44.8271)
    assert ellipsoid == pytest.approx(1000.0, abs=1.0)
    assert abs(ellipsoid - sphere) > 1.0

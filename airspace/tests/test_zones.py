"""Zone geometry, in flight: inside a polygon or a circle, and its limits."""

from __future__ import annotations

from typing import Any

import pytest

from airspace.ed269 import Restriction, VerticalReference
from airspace.tests.zone_helpers import geozone, square, zone
from airspace.zones import geozone_from_row, great_circle_m, unjudgeable

LAT, LON = 41.71, 44.81
HOLE = [
    [44.805, 41.705],
    [44.815, 41.705],
    [44.815, 41.715],
    [44.805, 41.715],
    [44.805, 41.705],
]


def test_inside_and_outside_a_polygon() -> None:
    z = zone(coordinates=square(LAT, LON))
    assert z.contains_horizontally(41.71, 44.81)
    assert not z.contains_horizontally(41.73, 44.81)
    assert not z.contains_horizontally(41.71, 44.83)


def test_a_hole_is_outside() -> None:
    z = zone(coordinates=[*square(LAT, LON), HOLE])
    assert not z.contains_horizontally(41.71, 44.81)
    assert z.contains_horizontally(41.702, 44.802)


def test_a_circle_is_judged_by_distance_not_by_a_polygon() -> None:
    z = zone(circle=(LAT, LON, 500))
    north_m = 499.0
    inside_lat = LAT + north_m / 111_195.0
    assert great_circle_m(LAT, LON, inside_lat, LON) == pytest.approx(north_m, rel=1e-3)
    assert z.contains_horizontally(inside_lat, LON)
    assert not z.contains_horizontally(LAT + 501.0 / 111_195.0, LON)


def test_a_circle_in_feet_is_converted_to_metres() -> None:
    z = zone(circle=(LAT, LON, 1000), uom="FT")
    assert z.circle is not None
    assert z.circle.radius_m == pytest.approx(304.8)
    assert not z.contains_horizontally(LAT + 320.0 / 111_195.0, LON)


def test_limits_keep_their_reference_and_become_metres() -> None:
    z = zone(
        coordinates=square(LAT, LON),
        lower=(100, "AGL"),
        upper=(2000, "AMSL"),
        uom="FT",
    )
    assert z.lower is not None and z.upper is not None
    assert z.lower.value_m == pytest.approx(30.48)
    assert z.upper.value_m == pytest.approx(609.6)
    assert (z.lower.reference, z.upper.reference) == (
        VerticalReference.AGL,
        VerticalReference.AMSL,
    )
    assert z.references == {VerticalReference.AGL, VerticalReference.AMSL}


def test_a_missing_limit_is_unbounded() -> None:
    z = zone(coordinates=square(LAT, LON), upper=(700, "AMSL"))
    assert z.lower is None
    assert z.references == {VerticalReference.AMSL}


def test_the_restriction_is_read() -> None:
    z = zone(coordinates=square(LAT, LON), restriction="CONDITIONAL")
    assert z.restriction is Restriction.CONDITIONAL


def test_zones_that_cannot_be_judged_are_named_by_what_they_need() -> None:
    zones = [
        zone(identifier="AGL1", coordinates=square(LAT, LON), upper=(120, "AGL")),
        zone(identifier="HAE1", coordinates=square(LAT, LON), upper=(600, "WGS84")),
        zone(identifier="MSL1", coordinates=square(LAT, LON), upper=(600, "AMSL")),
    ]
    assert unjudgeable(zones, terrain=False, geoid=False) == {
        "TERRAIN_DIR": ["AGL1"],
        "GEOID_PATH": ["HAE1"],
    }
    assert unjudgeable(zones, terrain=True, geoid=True) == {}


def _row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "identifier": "R1",
        "country": "GEO",
        "name": None,
        "ed269_type": "COMMON",
        "restriction": "PROHIBITED",
        "reason": None,
        "message": None,
        "zone_authority": [],
        "applicability": [{"permanent": "YES"}],
        "uom_dimensions": "M",
        "lower_limit": None,
        "lower_reference": "AMSL",
        "upper_limit": 120.0,
        "upper_reference": "AGL",
        "circle_lon_deg": None,
        "circle_lat_deg": None,
        "circle_radius": None,
        "ed269_extra": {},
        "geojson": '{"type":"Polygon","coordinates":'
        + str(square(LAT, LON)).replace(" ", "")
        + "}",
    }
    row.update(overrides)
    return row


def test_a_stored_row_is_the_zone_that_was_stored() -> None:
    stored = geozone(
        identifier="R1",
        name=None,
        coordinates=square(LAT, LON),
        upper=(120, "AGL"),
    )
    assert geozone_from_row(_row()) == stored


def test_a_stored_circle_is_read_from_its_centre_and_radius_not_geom() -> None:
    stored = geozone(
        identifier="R1", name=None, circle=(LAT, LON, 250), upper=(120, "AGL")
    )
    row = _row(circle_lon_deg=LON, circle_lat_deg=LAT, circle_radius=250.0)
    assert geozone_from_row(row) == stored


def test_a_stored_non_polygon_is_refused() -> None:
    with pytest.raises(ValueError, match="not a Polygon"):
        geozone_from_row(_row(geojson='{"type":"Point","coordinates":[44.8,41.7]}'))

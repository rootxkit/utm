"""Zone containment, in flight."""

from __future__ import annotations

import json
from uuid import UUID

import pytest

from airspace.zones import Zone, ZoneType, zone_from_geojson

SQUARE = [
    [44.80, 41.70],
    [44.82, 41.70],
    [44.82, 41.72],
    [44.80, 41.72],
    [44.80, 41.70],
]
HOLE = [
    [44.805, 41.705],
    [44.815, 41.705],
    [44.815, 41.715],
    [44.805, 41.715],
    [44.805, 41.705],
]


def zone(
    rings: list[list[list[float]]],
    *,
    min_alt: float | None = None,
    max_alt: float | None = None,
    kind: str = "no_fly",
) -> Zone:
    return zone_from_geojson(
        zone_id=UUID(int=9),
        name="test zone",
        zone_type=kind,
        geojson=json.dumps({"type": "Polygon", "coordinates": rings}),
        min_alt_amsl_m=min_alt,
        max_alt_amsl_m=max_alt,
    )


def test_inside_and_outside() -> None:
    z = zone([SQUARE])
    assert z.contains(41.71, 44.81, 500)
    assert not z.contains(41.73, 44.81, 500)
    assert not z.contains(41.71, 44.83, 500)


def test_a_hole_is_outside() -> None:
    z = zone([SQUARE, HOLE])
    assert not z.contains(41.71, 44.81, 500)
    assert z.contains(41.702, 44.802, 500)


def test_the_altitude_band_bounds_the_zone() -> None:
    z = zone([SQUARE], min_alt=500, max_alt=700)
    assert z.contains(41.71, 44.81, 600)
    assert not z.contains(41.71, 44.81, 450)
    assert not z.contains(41.71, 44.81, 750)


def test_a_missing_bound_is_unbounded() -> None:
    z = zone([SQUARE], max_alt=700)
    assert z.contains(41.71, 44.81, -50)


def test_the_type_is_read() -> None:
    assert zone([SQUARE], kind="restricted").type is ZoneType.RESTRICTED


def test_a_non_polygon_is_refused() -> None:
    with pytest.raises(ValueError, match="not a Polygon"):
        zone_from_geojson(
            zone_id=UUID(int=9),
            name="line",
            zone_type="no_fly",
            geojson=json.dumps({"type": "LineString", "coordinates": SQUARE}),
            min_alt_amsl_m=None,
            max_alt_amsl_m=None,
        )


# --- bounds above the ground and times in force (P5-18) ---------------------------


def agl_zone(**changes: object) -> Zone:
    from airspace.ed269 import parse
    from airspace.tests.ed269_files import features, zone

    [imported] = parse(features(zone(**changes))).zones  # type: ignore[arg-type]
    return zone_from_geojson(
        zone_id=UUID(int=9),
        name=imported.name,
        zone_type=imported.zone_type,
        geojson=imported.geojson(),
        min_alt_amsl_m=imported.min_alt_amsl_m,
        max_alt_amsl_m=imported.max_alt_amsl_m,
        min_height_agl_m=imported.min_height_agl_m,
        max_height_agl_m=imported.max_height_agl_m,
        applicability=(
            None if imported.applicability is None else list(imported.applicability)
        ),
        external_id=imported.external_id,
    )


def test_a_ground_to_120_m_zone_is_checked_above_the_ground() -> None:
    from airspace.tests.ed269_files import LAT, LON

    z = agl_zone()
    assert z.contains(LAT, LON, alt_amsl_m=700.0, height_agl_m=100.0)
    assert not z.contains(LAT, LON, alt_amsl_m=700.0, height_agl_m=130.0)


def test_with_the_ground_unknown_a_height_bound_cannot_rule_the_aircraft_out() -> None:
    from airspace.tests.ed269_files import LAT, LON

    assert agl_zone().contains(LAT, LON, alt_amsl_m=5000.0, height_agl_m=None)


def test_a_zone_outside_its_times_is_not_in_force() -> None:
    from datetime import UTC, datetime

    z = agl_zone(
        applicability=[
            {
                "permanent": "NO",
                "startDateTime": "2026-10-01T00:00:00Z",
                "endDateTime": "2026-10-02T00:00:00Z",
            }
        ]
    )
    assert not z.in_force(datetime(2026, 9, 30, tzinfo=UTC))
    assert z.in_force(datetime(2026, 10, 1, 12, tzinfo=UTC))

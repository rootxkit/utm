"""An authority's ED-269 file, read into zones or refused whole. P5-18."""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from typing import Any

import pytest

from airspace.ed269 import Ed269Error, in_force, parse
from airspace.tests.ed269_files import LAT, LON, features, square, volume, zone, zone_list


def test_both_published_shapes_are_read() -> None:
    first = parse(features(zone()))
    second = parse(zone_list(zone()))

    assert first.version == "Test zones v1"
    assert second.version == "2026-09-30T08:00:00Z"
    assert first.zones[0].comparable() == second.zones[0].comparable()


def test_a_prohibited_zone_is_no_fly_with_its_message_and_band() -> None:
    [z] = parse(features(zone())).zones

    assert (z.external_id, z.zone_type, z.restriction) == ("TEST01", "no_fly", "PROHIBITED")
    assert z.message == "Test zone, not a real restriction"
    assert z.reason == ("AIR_TRAFFIC",)
    assert (z.min_height_agl_m, z.max_height_agl_m) == (0.0, 120.0)
    assert (z.min_alt_amsl_m, z.max_alt_amsl_m) == (None, None)
    assert z.applicability is None
    assert z.rings[0][0] == (LON - 0.01, LAT - 0.01)


@pytest.mark.parametrize(
    ("restriction", "zone_type"),
    [("PROHIBITED", "no_fly"), ("REQ_AUTHORISATION", "restricted"), ("CONDITIONAL", "restricted")],
)
def test_restrictions_map_to_zone_types(restriction: str, zone_type: str) -> None:
    assert parse(features(zone(restriction=restriction))).zones[0].zone_type == zone_type


def test_no_restriction_is_skipped_and_said_to_be() -> None:
    parsed = parse(features(zone("OPEN1", restriction="NO_RESTRICTION"), zone("TEST02")))
    assert [z.external_id for z in parsed.zones] == ["TEST02"]
    assert parsed.skipped == (("OPEN1", "NO_RESTRICTION"),)


def test_each_volume_is_a_zone_and_references_are_kept_apart() -> None:
    parsed = parse(
        features(
            zone(
                geometry=[
                    volume(),
                    volume(
                        lowerLimit=200,
                        lowerVerticalReference="AMSL",
                        upperLimit="1000",
                        upperVerticalReference="AMSL",
                        uomDimensions="FT",
                    ),
                ]
            )
        )
    )
    first, second = parsed.zones
    assert (first.external_id, second.external_id) == ("TEST01#1", "TEST01#2")
    assert second.min_alt_amsl_m == pytest.approx(200 * 0.3048)
    assert second.max_alt_amsl_m == pytest.approx(304.8)
    assert (second.min_height_agl_m, second.max_height_agl_m) == (None, None)


def test_a_mixed_band_ground_to_an_altitude_is_kept_as_published() -> None:
    [z] = parse(
        features(zone(geometry=[volume(upperLimit=900, upperVerticalReference="AMSL")]))
    ).zones
    assert (z.min_height_agl_m, z.max_alt_amsl_m, z.max_height_agl_m) == (0.0, 900.0, None)


def test_a_missing_limit_is_unbounded() -> None:
    [z] = parse(features(zone(geometry=[volume(upperLimit=None)]))).zones
    assert z.max_height_agl_m is None


def distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6_371_008.8 * math.asin(math.sqrt(a))


def test_a_circle_becomes_a_polygon_on_the_circle() -> None:
    circle = {"type": "Circle", "center": [LON, LAT], "radius": 1000}
    [z] = parse(features(zone(geometry=[volume(horizontalProjection=circle)]))).zones

    [ring] = z.rings
    assert len(ring) == 65 and ring[0] == ring[-1]
    for lon, lat in ring:
        assert distance_m(LAT, LON, lat, lon) == pytest.approx(1000.0, abs=0.01)


def test_a_circle_in_feet_is_measured_in_feet() -> None:
    circle = {"type": "Circle", "center": [LON, LAT], "radius": 1000}
    [z] = parse(
        features(zone(geometry=[volume(horizontalProjection=circle, uomDimensions="FT")]))
    ).zones
    lon, lat = z.rings[0][0]
    assert distance_m(LAT, LON, lat, lon) == pytest.approx(304.8, abs=0.01)


def test_an_open_ring_is_closed_and_holes_are_kept() -> None:
    outer = square(half_deg=0.02)[0][:-1]
    hole = square(half_deg=0.005)[0]
    [z] = parse(
        features(
            zone(
                geometry=[
                    volume(horizontalProjection={"type": "Polygon", "coordinates": [outer, hole]})
                ]
            )
        )
    ).zones
    assert z.rings[0][0] == z.rings[0][-1]
    assert len(z.rings) == 2


def test_bounds_are_reported_longitude_first() -> None:
    assert parse(features(zone())).bounds() == pytest.approx(
        (LON - 0.01, LAT - 0.01, LON + 0.01, LAT + 0.01)
    )


@pytest.mark.parametrize(
    ("document", "complaint"),
    [
        (b"nope", "not JSON"),
        (b"[]", "not a JSON object"),
        (b"{}", "no 'features'"),
        (features(zone(identifier="")), "no identifier"),
        (features(zone(), zone()), "appears twice"),
        (features(zone(restriction="MAYBE")), "restriction"),
        (features(zone(geometry=[])), "no geometry"),
        (features(zone(geometry=[volume(lowerVerticalReference="WGS84")])), "only AGL and AMSL"),
        (features(zone(geometry=[volume(uomDimensions="NM")])), "unit"),
        (features(zone(geometry=[volume(upperLimit="high")])), "upperLimit"),
        (
            features(zone(geometry=[volume(horizontalProjection={"type": "Line"})])),
            "horizontalProjection type",
        ),
        (
            features(
                zone(geometry=[volume(horizontalProjection={"type": "Circle", "center": [LON, LAT], "radius": 0})])
            ),
            "radius > 0",
        ),
        (
            features(
                zone(
                    geometry=[
                        volume(
                            horizontalProjection={
                                "type": "Polygon",
                                "coordinates": [[[LAT, 200.0], [1, 1], [2, 2]]],
                            }
                        )
                    ]
                )
            ),
            "outside",
        ),
        (
            features(
                zone(geometry=[volume(horizontalProjection={"type": "Polygon", "coordinates": [[[1, 1], [1, 1]]]})])
            ),
            "three distinct",
        ),
        (features(zone(reason="AIR_TRAFFIC")), "reason"),
        (features(zone(applicability=[{"permanent": "NO", "startDateTime": "2026-09-30T10:00:00"}])), "no time zone"),
        (
            features(
                zone(applicability=[{"permanent": "NO", "schedule": [{"day": ["FUNDAY"], "startTime": "10:00Z", "endTime": "11:00Z"}]}])
            ),
            "days",
        ),
        (
            features(
                zone(applicability=[{"permanent": "NO", "schedule": [{"day": ["MON"], "startTime": "10:00", "endTime": "11:00Z"}]}])
            ),
            "no time zone",
        ),
    ],
    ids=[
        "not-json", "not-object", "no-list", "no-identifier", "duplicate", "restriction",
        "no-geometry", "wgs84", "unit", "limit", "projection", "radius", "range",
        "degenerate", "reason", "date-zone", "day", "time-zone",
    ],
)
def test_a_bad_file_is_refused_whole(document: bytes, complaint: str) -> None:
    with pytest.raises(Ed269Error, match=complaint):
        parse(document)


def at(text: str) -> datetime:
    return datetime.fromisoformat(text).astimezone(UTC)


def applicability(*periods: dict[str, Any]) -> list[dict[str, Any]] | None:
    parsed = parse(features(zone(applicability=list(periods)))).zones[0].applicability
    return None if parsed is None else json.loads(json.dumps(list(parsed)))


def test_permanent_is_always_in_force() -> None:
    assert applicability({"permanent": "YES"}) is None
    assert in_force(None, at("2030-01-01T00:00:00+00:00"))


def test_a_date_window_is_in_force_only_inside_it() -> None:
    periods = applicability(
        {
            "permanent": "NO",
            "startDateTime": "2026-10-01T00:00:00Z",
            "endDateTime": "2026-10-05T00:00:00Z",
        }
    )
    assert not in_force(periods, at("2026-09-30T23:59:59+00:00"))
    assert in_force(periods, at("2026-10-02T12:00:00+00:00"))
    assert not in_force(periods, at("2026-10-05T00:00:01+00:00"))


def test_a_weekly_schedule_in_utc_with_an_offset_moved_to_utc() -> None:
    periods = applicability(
        {
            "permanent": "NO",
            "schedule": [
                {"day": ["SAT", "SUN"], "startTime": "21:00:00.00+04:00", "endTime": "23:59:00Z"},
            ],
        }
    )
    assert periods is not None and periods[0]["schedule"][0]["start"] == "17:00:00"
    # 2026-10-03 is a Saturday.
    assert in_force(periods, at("2026-10-03T17:30:00+00:00"))
    assert not in_force(periods, at("2026-10-03T16:59:00+00:00"))
    assert not in_force(periods, at("2026-10-02T18:00:00+00:00"))


def test_a_schedule_across_midnight_counts_both_sides() -> None:
    periods = applicability(
        {
            "permanent": "NO",
            "schedule": [{"day": ["ANY"], "startTime": "22:00Z", "endTime": "06:00Z"}],
        }
    )
    assert in_force(periods, at("2026-10-03T23:00:00+00:00"))
    assert in_force(periods, at("2026-10-03T05:00:00+00:00"))
    assert not in_force(periods, at("2026-10-03T12:00:00+00:00"))


def test_an_empty_placeholder_schedule_is_no_schedule() -> None:
    """As the InterUSS sample writes it: one dailyPeriod of nulls."""
    periods = applicability(
        {
            "permanent": "YES",
            "startDateTime": None,
            "endDateTime": None,
            "dailyPeriod": [{"day": [], "startTime": None, "endTime": None}],
        }
    )
    assert periods is None


def test_a_time_that_crosses_midnight_in_utc_is_refused() -> None:
    with pytest.raises(Ed269Error, match="crosses midnight"):
        applicability(
            {
                "permanent": "NO",
                "schedule": [{"day": ["MON"], "startTime": "02:00+04:00", "endTime": "05:00Z"}],
            }
        )

"""ED-269 reading, refusing and writing. U-03.

The valid fixture exercises every field the module stores; each refusal
below names the field and the reason, as an importer's report must.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from airspace import ed269
from airspace.ed269 import (
    Circle,
    Ed269Error,
    Polygon,
    Restriction,
    Uom,
    VerticalReference,
    applies,
    document,
    parse,
    parse_applicability,
    parse_zone,
)

FIXTURE = Path(__file__).parent / "fixtures" / "ed269_valid.json"


def fixture_bytes() -> bytes:
    return FIXTURE.read_bytes()


def fixture_json() -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(fixture_bytes())
    return loaded


def encoded(value: dict[str, Any]) -> bytes:
    return json.dumps(value).encode()


# --- round trip ------------------------------------------------------------------


def test_the_valid_file_round_trips_unchanged() -> None:
    parsed = parse(fixture_bytes())
    written = document(parsed.zones, title=parsed.title, description=parsed.description)
    # Through text, as an export is served, and compared as JSON.
    assert json.loads(json.dumps(written)) == fixture_json()


def test_what_is_read_is_what_was_published() -> None:
    zones = {zone.identifier: zone for zone in parse(fixture_bytes()).zones}
    square, circle, night, info = (zones[f"TST00{n}"] for n in range(1, 5))

    assert square.restriction is Restriction.PROHIBITED
    assert isinstance(square.volume.projection, Polygon)
    assert len(square.volume.projection.rings) == 2
    assert square.volume.lower_reference is VerticalReference.AGL
    assert (square.volume.lower_m, square.volume.upper_m) == (0.0, 120.0)

    assert isinstance(circle.volume.projection, Circle)
    assert circle.volume.uom is Uom.FT
    assert circle.volume.radius_m == pytest.approx(1640 * 0.3048)
    assert circle.volume.upper_m == pytest.approx(2500.5 * 0.3048)
    assert circle.extra["extendedProperties"] == {"source": "test", "revision": 3}

    assert night.name is None
    assert night.volume.lower_limit is None
    assert night.volume.upper_reference is VerticalReference.WGS84
    assert info.reason is None
    assert info.zone_authority == ({"purpose": "INFORMATION"},)


def test_the_list_wrapper_and_a_byte_order_mark_are_read() -> None:
    features = fixture_json()["features"]
    wrapped = {
        "formatVersion": "1.0.0",
        "createdAt": "2026-10-01T00:00:00Z",
        "UASZoneList": features,
    }
    parsed = parse(b"\xef\xbb\xbf" + encoded(wrapped))
    assert [z.identifier for z in parsed.zones] == [f["identifier"] for f in features]
    assert parsed.title is None


def test_null_optional_fields_read_as_absent_and_export_absent() -> None:
    feature = copy.deepcopy(fixture_json()["features"][0])
    feature["name"] = None
    feature["message"] = None
    feature["geometry"][0]["upperLimit"] = None
    feature["zoneAuthority"][0]["phone"] = None
    zone = parse_zone(feature)
    out = ed269.feature(zone)
    assert "name" not in out and "message" not in out
    assert "upperLimit" not in out["geometry"][0]
    assert "phone" not in out["zoneAuthority"][0]
    # Read again, it is the same zone: the round trip is stable.
    assert parse_zone(out) == zone


# --- refusals, each with its field and reason ------------------------------------

Mutation = Callable[[dict[str, Any]], None]


def _set(path: list[str | int], value: Any) -> Mutation:
    def mutate(feature: dict[str, Any]) -> None:
        target: Any = feature
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value

    return mutate


def _delete(key: str) -> Mutation:
    def mutate(feature: dict[str, Any]) -> None:
        del feature[key]

    return mutate


VOLUME: list[str | int] = ["geometry", 0]
PROJECTION: list[str | int] = [*VOLUME, "horizontalProjection"]
PERIOD: list[str | int] = ["applicability", 0]

INVALID: list[tuple[str, Mutation, str, str]] = [
    ("no identifier", _delete("identifier"), "identifier", "missing"),
    ("long identifier", _set(["identifier"], "TOOLONG1"), "identifier", "at most 7"),
    ("bad country", _set(["country"], "GE"), "country", "ISO 3166-1 alpha-3"),
    ("lower-case country", _set(["country"], "geo"), "country", "ISO 3166-1"),
    ("no type", _delete("type"), "type", "missing"),
    ("bad restriction", _set(["restriction"], "FORBIDDEN"), "restriction", "not one"),
    (
        "American spelling",
        _set(["restriction"], "REQ_AUTHORIZATION"),
        "restriction",
        "spells it REQ_AUTHORISATION",
    ),
    ("bad reason", _set(["reason"], ["WEATHER"]), "reason[0]", "not one of"),
    ("repeated reason", _set(["reason"], ["NOISE", "NOISE"]), "reason[1]", "twice"),
    ("long message", _set(["message"], "x" * 201), "message", "at most 200"),
    ("unknown field", _set(["colour"], "red"), "colour", "unknown field"),
    ("no authority list", _delete("zoneAuthority"), "zoneAuthority", "missing"),
    (
        "bad purpose",
        _set(["zoneAuthority", 0, "purpose"], "AUTHORISATION"),
        "zoneAuthority[0].purpose",
        "not one of",
    ),
    (
        "unknown authority field",
        _set(["zoneAuthority", 0, "fax"], "1"),
        "zoneAuthority[0].fax",
        "unknown field",
    ),
    ("no applicability", _delete("applicability"), "applicability", "missing"),
    ("empty applicability", _set(["applicability"], []), "applicability", "at least"),
    (
        "permanent with an end",
        _set([*PERIOD, "endDateTime"], "2027-01-01T00:00:00Z"),
        "applicability[0].endDateTime",
        "permanent YES",
    ),
    (
        "bad permanent",
        _set([*PERIOD, "permanent"], "Yes"),
        "applicability[0].permanent",
        "not one of",
    ),
    (
        "not permanent and never",
        _set(PERIOD, {"permanent": "NO"}),
        "applicability[0]",
        "to say when",
    ),
    (
        "naive date",
        _set(PERIOD, {"permanent": "NO", "startDateTime": "2026-01-01T00:00:00"}),
        "applicability[0].startDateTime",
        "no offset",
    ),
    (
        "end before start",
        _set(
            PERIOD,
            {
                "permanent": "NO",
                "startDateTime": "2026-02-01T00:00:00Z",
                "endDateTime": "2026-01-01T00:00:00Z",
            },
        ),
        "applicability[0].endDateTime",
        "not after",
    ),
    (
        "schedule without offset",
        _set(
            PERIOD,
            {
                "permanent": "NO",
                "schedule": [
                    {"day": ["MON"], "startTime": "08:00", "endTime": "09:00"}
                ],
            },
        ),
        "applicability[0].schedule[0].startTime",
        "with an offset",
    ),
    (
        "schedule with two offsets",
        _set(
            PERIOD,
            {
                "permanent": "NO",
                "schedule": [
                    {"day": ["MON"], "startTime": "08:00Z", "endTime": "09:00+04:00"}
                ],
            },
        ),
        "applicability[0].schedule[0].endTime",
        "different offset",
    ),
    (
        "bad day",
        _set(
            PERIOD,
            {
                "permanent": "NO",
                "schedule": [
                    {"day": ["MONDAY"], "startTime": "08:00Z", "endTime": "09:00Z"}
                ],
            },
        ),
        "applicability[0].schedule[0].day[0]",
        "not one of",
    ),
    ("no geometry", _delete("geometry"), "geometry", "missing"),
    (
        "two volumes",
        lambda f: f["geometry"].append(copy.deepcopy(f["geometry"][0])),
        "geometry",
        "one volume per zone",
    ),
    ("bad unit", _set([*VOLUME, "uomDimensions"], "KM"), "uomDimensions", "not one"),
    (
        "bad reference",
        _set([*VOLUME, "upperVerticalReference"], "FL"),
        "upperVerticalReference",
        "not one of",
    ),
    (
        "limit as a string",
        _set([*VOLUME, "lowerLimit"], "0"),
        "geometry[0].lowerLimit",
        "must be a number",
    ),
    (
        "upper not above lower",
        _set([*VOLUME, "upperLimit"], 0),
        "geometry[0].upperLimit",
        "not above",
    ),
    (
        "unclosed ring",
        _set(
            [*PROJECTION, "coordinates"],
            [[[44.8, 41.7], [44.82, 41.7], [44.82, 41.72], [44.8, 41.72]]],
        ),
        "horizontalProjection.coordinates[0]",
        "not closed",
    ),
    (
        "latitude out of range",
        _set(
            [*PROJECTION, "coordinates"],
            [[[44.8, 91.7], [44.82, 41.7], [44.82, 41.72], [44.8, 91.7]]],
        ),
        "horizontalProjection.coordinates[0][0]",
        "outside",
    ),
    (
        "unknown shape",
        _set([*PROJECTION, "type"], "Ellipse"),
        "horizontalProjection.type",
        "not Polygon or Circle",
    ),
    (
        "circle without radius",
        _set(PROJECTION, {"type": "Circle", "center": [44.8, 41.7]}),
        "horizontalProjection.radius",
        "above 0",
    ),
    (
        "circle with coordinates",
        _set(
            PROJECTION,
            {"type": "Circle", "center": [44.8, 41.7], "radius": 5, "coordinates": []},
        ),
        "horizontalProjection.coordinates",
        "not a field of a Circle",
    ),
    ("bad region", _set(["region"], "7"), "region", "must be an integer"),
    (
        "bad exemption",
        _set(["regulationExemption"], "MAYBE"),
        "regulationExemption",
        "not one of",
    ),
]


@pytest.mark.parametrize(
    ("mutation", "field", "reason"),
    [case[1:] for case in INVALID],
    ids=[case[0] for case in INVALID],
)
def test_an_invalid_zone_is_refused_with_the_field_and_the_reason(
    mutation: Mutation, field: str, reason: str
) -> None:
    feature = copy.deepcopy(fixture_json()["features"][0])
    mutation(feature)
    with pytest.raises(Ed269Error) as refused:
        parse(encoded({"features": [feature]}))
    problems = refused.value.problems
    assert any(p.field.endswith(field) and reason in p.reason for p in problems), (
        problems
    )
    # The path names the zone, so a report on a long file can be followed.
    assert all(p.field.startswith("features[0]") for p in problems), problems


def test_the_same_zone_is_accepted_without_the_mutation() -> None:
    """The presence pair of the refusals above: the base feature is valid."""
    feature = fixture_json()["features"][0]
    assert parse(encoded({"features": [feature]})).zones[0].identifier == "TST001"


@pytest.mark.parametrize(
    ("data", "field", "reason"),
    [
        (b"{not json", "$", "not JSON"),
        (b"\xff\xfe", "$", "not UTF-8"),
        (b"[]", "$", "not a JSON object"),
        (b'{"zones": []}', "features", "missing"),
        (b'{"features": {}}', "features", "list of zones"),
        (b'{"features": [], "author": "x"}', "author", "unknown field"),
    ],
)
def test_an_invalid_document_is_refused_with_a_named_reason(
    data: bytes, field: str, reason: str
) -> None:
    with pytest.raises(Ed269Error) as refused:
        parse(data)
    assert any(p.field == field and reason in p.reason for p in refused.value.problems)


def test_a_repeated_identifier_names_both_places() -> None:
    feature = fixture_json()["features"][0]
    with pytest.raises(Ed269Error) as refused:
        parse(encoded({"features": [feature, feature]}))
    (problem,) = refused.value.problems
    assert problem.field == "features[1].identifier"
    assert "features[0]" in problem.reason


def test_every_problem_in_a_file_is_reported_not_only_the_first() -> None:
    features = copy.deepcopy(fixture_json()["features"])
    features[0]["country"] = "XX"
    features[2]["restriction"] = "NONE"
    with pytest.raises(Ed269Error) as refused:
        parse(encoded({"features": features}))
    fields = {p.field for p in refused.value.problems}
    assert fields == {"features[0].country", "features[2].restriction"}
    assert "features[0].country" in str(refused.value)


def test_a_huge_report_is_capped_and_counts_the_rest() -> None:
    feature = fixture_json()["features"][0]
    features = []
    for n in range(ed269.MAX_PROBLEMS + 5):
        broken = copy.deepcopy(feature)
        broken["identifier"] = f"Z{n:05d}"
        broken["country"] = "X"
        features.append(broken)
    with pytest.raises(Ed269Error) as refused:
        parse(encoded({"features": features}))
    assert len(refused.value.problems) == ed269.MAX_PROBLEMS
    assert refused.value.more == 5
    assert "and 5 more" in str(refused.value)


# --- applicability -------------------------------------------------------------------


def at(text: str) -> datetime:
    return datetime.fromisoformat(text)


def periods(*raw: dict[str, Any]) -> tuple[ed269.Period, ...]:
    return parse_applicability(list(raw))


def test_permanent_applies_at_any_time() -> None:
    assert applies(periods({"permanent": "YES"}), at("1999-01-01T00:00:00Z"))


def test_a_date_window_applies_inside_and_not_outside() -> None:
    window = periods(
        {
            "permanent": "NO",
            "startDateTime": "2026-10-01T07:00:00Z",
            "endDateTime": "2026-10-01T10:00:00+02:00",
        }
    )
    assert not applies(window, at("2026-10-01T06:59:59Z"))
    assert applies(window, at("2026-10-01T07:00:00Z"))
    assert applies(window, at("2026-10-01T08:00:00Z"))
    # 10:00+02:00 is 08:00Z: the end is converted, not read as UTC.
    assert not applies(window, at("2026-10-01T08:00:01Z"))


def test_a_weekly_schedule_applies_only_on_its_days_and_hours() -> None:
    weekdays_nine_to_five = periods(
        {
            "permanent": "NO",
            "schedule": [
                {"day": ["MON", "WED"], "startTime": "09:00Z", "endTime": "17:00Z"}
            ],
        }
    )
    # 2026-10-05 is a Monday.
    assert applies(weekdays_nine_to_five, at("2026-10-05T12:00:00Z"))
    assert not applies(weekdays_nine_to_five, at("2026-10-05T17:00:01Z"))
    assert not applies(weekdays_nine_to_five, at("2026-10-06T12:00:00Z"))
    assert applies(weekdays_nine_to_five, at("2026-10-07T09:00:00Z"))


def test_a_night_period_runs_past_midnight_utc_into_the_next_day() -> None:
    night = periods(
        {
            "permanent": "NO",
            "schedule": [{"day": ["FRI"], "startTime": "22:00Z", "endTime": "02:00Z"}],
        }
    )
    # Friday 2026-10-09 22:00Z to Saturday 02:00Z.
    assert not applies(night, at("2026-10-09T21:59:59Z"))
    assert applies(night, at("2026-10-09T23:59:59Z"))
    assert applies(night, at("2026-10-10T00:00:00Z"))
    assert applies(night, at("2026-10-10T02:00:00Z"))
    assert not applies(night, at("2026-10-10T02:00:01Z"))
    # Friday's own early hours belong to Thursday's night, which is not listed.
    assert not applies(night, at("2026-10-09T01:00:00Z"))


def test_a_schedule_in_an_offset_is_judged_on_that_offsets_day() -> None:
    tbilisi_morning = periods(
        {
            "permanent": "NO",
            "schedule": [
                {"day": ["MON"], "startTime": "02:00+04:00", "endTime": "03:00+04:00"}
            ],
        }
    )
    # Monday 02:30 in Tbilisi is Sunday 22:30 UTC.
    assert applies(tbilisi_morning, at("2026-10-04T22:30:00Z"))
    assert not applies(tbilisi_morning, at("2026-10-05T22:30:00Z"))


def test_a_schedule_is_bounded_by_its_dates() -> None:
    bounded = periods(
        {
            "permanent": "NO",
            "startDateTime": "2026-10-06T00:00:00Z",
            "schedule": [{"day": ["ANY"], "startTime": "00:00Z", "endTime": "23:59Z"}],
        }
    )
    assert not applies(bounded, at("2026-10-05T12:00:00Z"))
    assert applies(bounded, at("2026-10-06T12:00:00Z"))


def test_any_of_several_periods_applies() -> None:
    two = periods(
        {"permanent": "NO", "endDateTime": "2026-01-01T00:00:00Z"},
        {"permanent": "NO", "startDateTime": "2027-01-01T00:00:00Z"},
    )
    assert applies(two, at("2025-06-01T00:00:00Z"))
    assert not applies(two, at("2026-06-01T00:00:00Z"))
    assert applies(two, at("2027-06-01T00:00:00Z"))


def test_a_naive_time_cannot_be_evaluated() -> None:
    with pytest.raises(ValueError, match="aware"):
        applies(periods({"permanent": "YES"}), datetime(2026, 1, 1))  # noqa: DTZ001
    assert applies(periods({"permanent": "YES"}), datetime(2026, 1, 1, tzinfo=UTC))

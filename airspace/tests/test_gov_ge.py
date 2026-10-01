"""airspace.gov.ge's zone files to ED-269, on a fixture in their format. U-03.

Never against the live site: the fixture was written in the format seen on
2026-10-01, with invented zones.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path
from typing import Any

import pytest

from airspace.ed269 import Circle, Ed269Error, Polygon, Restriction, parse
from airspace.gov_ge import Rules, parse_circle_radii, parse_points, to_ed269

FIXTURES = Path(__file__).parent / "fixtures"


def points() -> str:
    return (FIXTURES / "gov_ge_points.js").read_text(encoding="utf-8")


def page() -> str:
    return (FIXTURES / "gov_ge_page.html").read_text(encoding="utf-8")


def rules(**changes: Any) -> Rules:
    raw = tomllib.loads((FIXTURES / "gov_ge_rules.toml").read_text(encoding="utf-8"))
    raw.update(changes)
    return Rules.from_mapping(raw)


def test_the_shapes_are_read_with_latitude_and_longitude_swapped() -> None:
    shapes = {shape.name: shape for shape in parse_points(points())}
    assert set(shapes) == {"TSTA_CTR", "TSTB_CTR", "UGT01_EPR"}
    assert shapes["TSTA_CTR"].center == (44.8, 41.7)
    assert shapes["TSTA_CTR"].kind == "CTR"
    ring = shapes["TSTB_CTR"].ring
    assert ring is not None and ring[0] == (44.7, 41.8)
    # An open ring on the site is closed here.
    unclosed = shapes["UGT01_EPR"].ring
    assert unclosed is not None and unclosed[0] == unclosed[-1]
    assert shapes["UGT01_EPR"].kind == "EPR"


def test_circle_radii_come_from_the_page() -> None:
    assert parse_circle_radii(page()) == {"TSTA_CTR": 5556.0}


def test_the_fixture_converts_to_a_valid_ed269_document() -> None:
    document = to_ed269(parse_points(points()), parse_circle_radii(page()), rules())
    zones = {z.identifier: z for z in parse(json.dumps(document).encode()).zones}

    assert set(zones) == {"TSTACTR", "TSTBCTR", "UGT01"}
    circle = zones["TSTACTR"]
    assert isinstance(circle.volume.projection, Circle)
    assert circle.volume.radius_m == 5556.0
    assert circle.restriction is Restriction.REQ_AUTHORISATION
    assert circle.zone_authority == (
        {"name": "Test authority", "purpose": "AUTHORIZATION"},
    )
    restricted = zones["UGT01"]
    assert isinstance(restricted.volume.projection, Polygon)
    assert restricted.restriction is Restriction.PROHIBITED
    # A 3000 ft ceiling, as the rules give it.
    assert restricted.volume.upper_m == pytest.approx(914.4)
    assert restricted.extra["extendedProperties"] == {
        "source": "airspace.gov.ge",
        "kind": "EPR",
    }


def test_a_kind_without_a_rule_is_refused_by_name() -> None:
    raw = tomllib.loads((FIXTURES / "gov_ge_rules.toml").read_text(encoding="utf-8"))
    del raw["kinds"]["EPR"]
    with pytest.raises(Ed269Error) as refused:
        to_ed269(
            parse_points(points()), parse_circle_radii(page()), Rules.from_mapping(raw)
        )
    assert [p.field for p in refused.value.problems] == ["UGT01_EPR"]
    assert "'EPR' has no rule" in refused.value.problems[0].reason


def test_a_circle_without_a_radius_and_a_long_identifier_are_refused() -> None:
    with pytest.raises(Ed269Error) as refused:
        to_ed269(parse_points(points()), {}, rules(identifiers={}))
    reasons = {p.field: p.reason for p in refused.value.problems}
    assert "no radius" in reasons["TSTA_CTR"]
    assert "longer than 7" in reasons["UGT01_EPR"]


def test_a_rule_that_is_not_ed269_is_refused_by_the_strict_reader() -> None:
    raw = tomllib.loads((FIXTURES / "gov_ge_rules.toml").read_text(encoding="utf-8"))
    raw["kinds"]["CTR"]["restriction"] = "RESTRICTED"
    with pytest.raises(Ed269Error) as refused:
        to_ed269(
            parse_points(points()), parse_circle_radii(page()), Rules.from_mapping(raw)
        )
    assert any(p.field.endswith(".restriction") for p in refused.value.problems)


def test_a_variable_that_is_not_coordinates_is_refused_by_name() -> None:
    with pytest.raises(Ed269Error) as refused:
        parse_points('var BAD_CTR_points = [[41.7, "x"]];\nvar ODD_CTR_point = [1];')
    assert {p.field for p in refused.value.problems} == {"BAD_CTR", "ODD_CTR"}


def test_a_missing_rule_key_is_named() -> None:
    raw = tomllib.loads((FIXTURES / "gov_ge_rules.toml").read_text(encoding="utf-8"))
    del raw["kinds"]["CTR"]["applicability"]
    with pytest.raises(KeyError, match="applicability"):
        Rules.from_mapping(raw)

"""Zones from airspace.gov.ge, the GCAA's drone map, as ED-269. U-03.

## What the site serves (checked read-only on 2026-10-01)

`https://airspace.gov.ge/` is a Leaflet page. It has no API, no GeoJSON or
ED-269 download, no WMS/WFS and no ArcGIS service. Its zones are JavaScript
variables in a static file, `/Airspace/leaflet/zone/points.js`:

    var UGTB_CTR_points = [[41.875, 44.9083], ...];   // a polygon
    var UGKO_CTR_point = [42.1766667, 42.4825];        // a circle's centre

- coordinates are `[latitude, longitude]`, the other way round from
  GeoJSON and ED-269;
- a circle's radius is not in that file but in the page itself, in the
  `L.circle(UGKO_CTR_point, {radius: 11112, ...})` call;
- the kind of zone is only the variable name's suffix (CTR, TMA, FIZ, ATZ,
  EPR for restricted areas, MIL, EPP), and its name only a popup string;
- there are **no vertical limits and no times** anywhere.

So nothing on the site says what restriction a zone carries, between which
heights, or when. Those decide alerts, and CLAUDE.md forbids guessing them,
so this importer takes them from a rules file the authority provides
(`Rules`): per kind, the restriction, limits, reasons and message, and the
authority to name. A kind the rules do not cover refuses the conversion by
name. The geometry is taken from the site's files as they are.

## How it is used

`tools/gov_ge_zones.py` converts saved copies of `points.js` and the page
into an ED-269 document, which is then imported like any other: through
`POST /airspace/zones/import`, with a dry run first. Nothing here fetches
the live site; the tests run on a fixture written in its format.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from airspace.ed269 import IDENTIFIER_MAX, Ed269Error, Problem, parse

# `var NAME_points = [...];` and `var NAME_point = [...];`
_VARIABLE = re.compile(
    r"var\s+(?P<name>[A-Za-z0-9_]+?)_(?P<form>points|point)\s*=\s*(?P<value>\[.*?\])\s*;",
    re.DOTALL,
)
# `L.circle(NAME_point, { ... radius: 11112 ... })`
_CIRCLE = re.compile(
    r"L\.circle\(\s*(?P<name>[A-Za-z0-9_]+?)_point\s*,\s*\{[^}]*?radius\s*:\s*"
    r"(?P<radius>[0-9]+(?:\.[0-9]+)?)",
    re.DOTALL,
)


@dataclass(frozen=True, slots=True)
class Shape:
    """One zone as the site draws it. Coordinates are (lon_deg, lat_deg)."""

    name: str
    # The variable name's suffix: CTR, TMA, EPR, MIL...
    kind: str
    ring: tuple[tuple[float, float], ...] | None = None
    center: tuple[float, float] | None = None


def parse_points(text: str) -> list[Shape]:
    """The zones in a `points.js`. Raises Ed269Error naming a variable that
    is not a list of [lat, lon] pairs."""
    shapes: list[Shape] = []
    problems: list[Problem] = []
    for match in _VARIABLE.finditer(text):
        name = match["name"]
        try:
            value = json.loads(match["value"])
        except json.JSONDecodeError:
            problems.append(Problem(name, "is not a list of numbers"))
            continue
        kind = name.rsplit("_", 1)[-1] if "_" in name else name
        if match["form"] == "point":
            if not _is_pair(value):
                problems.append(Problem(name, "a point must be [lat, lon]"))
                continue
            shapes.append(Shape(name, kind, center=(value[1], value[0])))
            continue
        if not isinstance(value, list) or not all(_is_pair(p) for p in value):
            problems.append(Problem(name, "a polygon must be a list of [lat, lon]"))
            continue
        ring = [(float(p[1]), float(p[0])) for p in value]
        if ring and ring[0] != ring[-1]:
            ring.append(ring[0])
        shapes.append(Shape(name, kind, ring=tuple(ring)))
    if problems:
        raise Ed269Error(problems)
    return shapes


def _is_pair(value: Any) -> bool:
    return (
        isinstance(value, list)
        and len(value) == 2
        and all(isinstance(v, int | float) and not isinstance(v, bool) for v in value)
    )


def parse_circle_radii(page: str) -> dict[str, float]:
    """Each circle's radius in metres, from the page's `L.circle` calls."""
    return {m["name"]: float(m["radius"]) for m in _CIRCLE.finditer(page)}


@dataclass(frozen=True)
class KindRule:
    """What the authority says a kind of zone is. Every value goes into the
    ED-269 zone as given; nothing here has a default, since each decides
    alerts. A missing limit is unbounded, as in ED-269."""

    restriction: str
    uom: str
    lower_reference: str
    upper_reference: str
    applicability: list[dict[str, Any]]
    lower_limit: float | None = None
    upper_limit: float | None = None
    reason: list[str] | None = None
    message: str | None = None


@dataclass(frozen=True)
class Rules:
    country: str
    authority: dict[str, str]
    kinds: dict[str, KindRule]
    # ED-269 identifiers are at most 7 characters; site names often longer.
    identifiers: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> Rules:
        """From the rules file (TOML), e.g.

            country = "GEO"
            [authority]
            name = "GCAA"
            purpose = "AUTHORIZATION"
            [kinds.CTR]
            restriction = "REQ_AUTHORISATION"
            uom = "M"
            lower_reference = "AGL"
            lower_limit = 0
            upper_reference = "AGL"
            upper_limit = 120
            applicability = [{ permanent = "YES" }]
            reason = ["AIR_TRAFFIC"]
            [identifiers]
            UGR01_EPR = "UGR01"

        Raises KeyError naming a required key that is missing.
        """
        kinds: dict[str, KindRule] = {}
        for kind, rule in dict(raw.get("kinds", {})).items():
            kinds[str(kind)] = KindRule(
                restriction=str(rule["restriction"]),
                uom=str(rule["uom"]),
                lower_reference=str(rule["lower_reference"]),
                upper_reference=str(rule["upper_reference"]),
                applicability=[dict(p) for p in rule["applicability"]],
                lower_limit=_optional_float(rule.get("lower_limit")),
                upper_limit=_optional_float(rule.get("upper_limit")),
                reason=None
                if "reason" not in rule
                else [str(r) for r in rule["reason"]],
                message=None if "message" not in rule else str(rule["message"]),
            )
        return cls(
            country=str(raw["country"]),
            authority={
                str(k): str(v) for k, v in dict(raw.get("authority", {})).items()
            },
            kinds=kinds,
            identifiers={
                str(k): str(v) for k, v in dict(raw.get("identifiers", {})).items()
            },
        )


def _optional_float(value: Any) -> float | None:
    return None if value is None else float(value)


def to_ed269(
    shapes: list[Shape], radii: Mapping[str, float], rules: Rules
) -> dict[str, Any]:
    """An ED-269 document of the shapes, checked by the strict reader.
    Raises Ed269Error naming each shape that cannot be converted: a kind
    with no rule, a circle with no radius, an identifier too long."""
    problems: list[Problem] = []
    features: list[dict[str, Any]] = []
    for shape in shapes:
        rule = rules.kinds.get(shape.kind)
        if rule is None:
            problems.append(
                Problem(
                    shape.name, f"kind {shape.kind!r} has no rule in the rules file"
                )
            )
            continue
        identifier = rules.identifiers.get(shape.name, shape.name.replace("_", ""))
        if len(identifier) > IDENTIFIER_MAX:
            problems.append(
                Problem(
                    shape.name,
                    f"identifier {identifier!r} is longer than {IDENTIFIER_MAX}; "
                    "give one under [identifiers] in the rules file",
                )
            )
            continue
        if shape.center is not None:
            radius = radii.get(shape.name)
            if radius is None:
                problems.append(
                    Problem(shape.name, "a circle with no radius in the page")
                )
                continue
            projection: dict[str, Any] = {
                "type": "Circle",
                "center": list(shape.center),
                # The page gives metres; ED-269 gives the radius in the
                # volume's unit.
                "radius": radius if rule.uom == "M" else radius / 0.3048,
            }
        else:
            assert shape.ring is not None
            projection = {
                "type": "Polygon",
                "coordinates": [[list(p) for p in shape.ring]],
            }
        volume: dict[str, Any] = {
            "uomDimensions": rule.uom,
            "lowerVerticalReference": rule.lower_reference,
            "upperVerticalReference": rule.upper_reference,
            "horizontalProjection": projection,
        }
        if rule.lower_limit is not None:
            volume["lowerLimit"] = rule.lower_limit
        if rule.upper_limit is not None:
            volume["upperLimit"] = rule.upper_limit
        feature: dict[str, Any] = {
            "identifier": identifier,
            "country": rules.country,
            "name": shape.name,
            "type": "COMMON",
            "restriction": rule.restriction,
            "applicability": rule.applicability,
            "zoneAuthority": [rules.authority] if rules.authority else [],
            "geometry": [volume],
            "extendedProperties": {"source": "airspace.gov.ge", "kind": shape.kind},
        }
        if rule.reason is not None:
            feature["reason"] = rule.reason
        if rule.message is not None:
            feature["message"] = rule.message
        features.append(feature)
    if problems:
        raise Ed269Error(problems)
    document = {"title": "airspace.gov.ge zones", "features": features}
    # The same strict reader an import uses: refuse here, not at the import.
    parse(json.dumps(document).encode())
    return document

"""Reading a EUROCAE ED-269 geographical zones file. P5-18.

Authorities publish UAS geographical zones (EU 2019/947 Art. 15) as ED-269
JSON: a list of zones, each with a restriction, reasons, when it applies,
and one or more airspace volumes, each a polygon or a circle between two
limits. This turns a file into zones the airspace monitor checks, or
refuses it with the reason. It does not touch a database
(`tools/import_zones.py` does).

## Both published shapes

The zones are under `features` in the files states publish (Luxembourg,
Switzerland, the ED-269 examples), and under `UASZoneList` with
`formatVersion` and `createdAt` in others. Both are read.

## What becomes what

| ED-269 | Here |
|---|---|
| `restriction` PROHIBITED | `no_fly`: critical alert |
| REQ_AUTHORISATION, CONDITIONAL | `restricted`: warning |
| NO_RESTRICTION | not imported, listed as skipped |

Each volume of a zone is one zone here, `identifier#n` when there are
several. Coordinates are GeoJSON order, longitude first, which is what the
published files use (the standard's own prose says "lat, lng" in places;
the files do not). The import reports the bounding box of what it read, so
a file with the order the other way round is seen at once.

A circle becomes a 64-sided polygon inscribed in it. Every vertex is on the
circle and a chord is at most 0.12 % of the radius inside it, 1.2 m for a
1 km circle. The published circle stays in `source_record`.

Limits keep their own reference. AMSL bounds become `min_alt_amsl_m` and
`max_alt_amsl_m`; AGL bounds become `min_height_agl_m` and
`max_height_agl_m`, checked against the DEM. Feet become metres. Any other
reference is refused, not guessed.

## A file is imported whole or not at all

One malformed zone refuses the file. Half of an authority's zones is worse
than none: it looks complete.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from typing import Any

FORMAT = "ED-269"
FEET_M = 0.3048
CIRCLE_SIDES = 64
EARTH_RADIUS_M = 6_371_008.8

_RESTRICTIONS = {
    "PROHIBITED": "no_fly",
    "REQ_AUTHORISATION": "restricted",
    "CONDITIONAL": "restricted",
}
SKIPPED_RESTRICTION = "NO_RESTRICTION"
_DAYS = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")
_TIME = re.compile(
    r"^(?P<h>\d{2}):(?P<m>\d{2})(?::(?P<s>\d{2})(?:\.\d+)?)?"
    r"(?P<tz>Z|[+-]\d{2}:?\d{2})?$"
)

Ring = tuple[tuple[float, float], ...]


class Ed269Error(ValueError):
    """A file that cannot be imported, and where."""


@dataclass(frozen=True)
class ImportedZone:
    external_id: str
    name: str
    zone_type: str
    restriction: str
    message: str | None
    reason: tuple[str, ...]
    # Exterior first, then holes; (lon_deg, lat_deg), closed.
    rings: tuple[Ring, ...]
    min_alt_amsl_m: float | None
    max_alt_amsl_m: float | None
    min_height_agl_m: float | None
    max_height_agl_m: float | None
    # None: always in force. Otherwise periods, any of which puts it in force.
    applicability: tuple[dict[str, Any], ...] | None
    source_record: dict[str, Any] = field(compare=False)

    def geojson(self) -> str:
        return json.dumps(
            {"type": "Polygon", "coordinates": [list(map(list, r)) for r in self.rings]}
        )

    def comparable(self) -> dict[str, Any]:
        """What a re-import compares to decide whether a zone changed."""
        return {
            "name": self.name,
            "zone_type": self.zone_type,
            "restriction": self.restriction,
            "message": self.message,
            "reason": list(self.reason),
            "rings": [[list(p) for p in r] for r in self.rings],
            "min_alt_amsl_m": self.min_alt_amsl_m,
            "max_alt_amsl_m": self.max_alt_amsl_m,
            "min_height_agl_m": self.min_height_agl_m,
            "max_height_agl_m": self.max_height_agl_m,
            "applicability": (
                None if self.applicability is None else list(self.applicability)
            ),
        }


@dataclass(frozen=True)
class Ed269File:
    version: str | None
    zones: tuple[ImportedZone, ...]
    # (identifier, why), for zones read but deliberately not imported.
    skipped: tuple[tuple[str, str], ...]

    def bounds(self) -> tuple[float, float, float, float] | None:
        """min_lon, min_lat, max_lon, max_lat of everything imported."""
        points = [p for z in self.zones for r in z.rings for p in r]
        if not points:
            return None
        lons = [p[0] for p in points]
        lats = [p[1] for p in points]
        return min(lons), min(lats), max(lons), max(lats)


def parse(data: bytes) -> Ed269File:
    try:
        document = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise Ed269Error(f"not JSON: {error}") from error
    if not isinstance(document, dict):
        raise Ed269Error("not a JSON object")
    features = document.get("features", document.get("UASZoneList"))
    if not isinstance(features, list):
        raise Ed269Error("no 'features' or 'UASZoneList' list")
    version = document.get("createdAt") or document.get("title")
    zones: list[ImportedZone] = []
    skipped: list[tuple[str, str]] = []
    seen: set[str] = set()
    for index, feature in enumerate(features):
        where = f"zone {index + 1}"
        if not isinstance(feature, dict):
            raise Ed269Error(f"{where}: not an object")
        identifier = feature.get("identifier")
        if not isinstance(identifier, str) or not identifier.strip():
            raise Ed269Error(f"{where}: no identifier")
        where = f"zone {identifier!r}"
        if identifier in seen:
            raise Ed269Error(f"{where}: identifier appears twice")
        seen.add(identifier)
        restriction = feature.get("restriction")
        if restriction == SKIPPED_RESTRICTION:
            skipped.append((identifier, "NO_RESTRICTION"))
            continue
        if restriction not in _RESTRICTIONS:
            raise Ed269Error(f"{where}: restriction {restriction!r}")
        zones.extend(_zone(feature, identifier, restriction, where))
    return Ed269File(
        version=None if version is None else str(version),
        zones=tuple(zones),
        skipped=tuple(skipped),
    )


def _zone(
    feature: dict[str, Any], identifier: str, restriction: str, where: str
) -> list[ImportedZone]:
    volumes = feature.get("geometry")
    if not isinstance(volumes, list) or not volumes:
        raise Ed269Error(f"{where}: no geometry")
    reason = feature.get("reason") or []
    if not isinstance(reason, list) or not all(isinstance(r, str) for r in reason):
        raise Ed269Error(f"{where}: reason is not a list of strings")
    applicability = _applicability(feature.get("applicability"), where)
    message = feature.get("message")
    name = feature.get("name") or identifier
    zones = []
    for number, volume in enumerate(volumes, start=1):
        here = f"{where} volume {number}"
        if not isinstance(volume, dict):
            raise Ed269Error(f"{here}: not an object")
        scale = _unit(volume.get("uomDimensions"), here)
        lower = _limit(volume, "lower", scale, here)
        upper = _limit(volume, "upper", scale, here)
        zones.append(
            ImportedZone(
                external_id=identifier if len(volumes) == 1 else f"{identifier}#{number}",
                name=str(name),
                zone_type=_RESTRICTIONS[restriction],
                restriction=restriction,
                message=None if message is None else str(message),
                reason=tuple(reason),
                rings=_rings(volume.get("horizontalProjection"), scale, here),
                min_alt_amsl_m=lower[0] if lower[1] == "AMSL" else None,
                max_alt_amsl_m=upper[0] if upper[1] == "AMSL" else None,
                min_height_agl_m=lower[0] if lower[1] == "AGL" else None,
                max_height_agl_m=upper[0] if upper[1] == "AGL" else None,
                applicability=applicability,
                source_record=feature,
            )
        )
    return zones


def _unit(value: Any, where: str) -> float:
    unit = str(value or "M").upper()
    if unit == "M":
        return 1.0
    if unit == "FT":
        return FEET_M
    raise Ed269Error(f"{where}: unit {value!r}")


def _limit(
    volume: dict[str, Any], which: str, scale: float, where: str
) -> tuple[float | None, str | None]:
    raw = volume.get(f"{which}Limit")
    reference = volume.get(f"{which}VerticalReference")
    if raw is None or raw == "":
        return None, None
    try:
        value = float(raw) * scale
    except (TypeError, ValueError) as error:
        raise Ed269Error(f"{where}: {which}Limit {raw!r}") from error
    if not math.isfinite(value):
        raise Ed269Error(f"{where}: {which}Limit {raw!r}")
    if reference not in ("AGL", "AMSL"):
        raise Ed269Error(
            f"{where}: {which}VerticalReference {reference!r}; only AGL and AMSL "
            "are read"
        )
    return value, reference


def _rings(projection: Any, scale: float, where: str) -> tuple[Ring, ...]:
    if not isinstance(projection, dict):
        raise Ed269Error(f"{where}: no horizontalProjection")
    kind = projection.get("type")
    if kind == "Circle":
        center = projection.get("center")
        radius = projection.get("radius")
        if (
            not isinstance(center, list)
            or len(center) != 2
            or not isinstance(radius, int | float)
            or isinstance(radius, bool)
            or radius <= 0
        ):
            raise Ed269Error(f"{where}: a circle needs a center and a radius > 0")
        lon, lat = _point(center, where)
        return (_circle(lat, lon, float(radius) * scale),)
    if kind == "Polygon":
        coordinates = projection.get("coordinates")
        if not isinstance(coordinates, list) or not coordinates:
            raise Ed269Error(f"{where}: a polygon with no coordinates")
        return tuple(_ring(ring, where) for ring in coordinates)
    raise Ed269Error(f"{where}: horizontalProjection type {kind!r}")


def _point(raw: Any, where: str) -> tuple[float, float]:
    if not isinstance(raw, list) or len(raw) < 2:
        raise Ed269Error(f"{where}: a point is not [lon, lat]")
    try:
        lon, lat = float(raw[0]), float(raw[1])
    except (TypeError, ValueError) as error:
        raise Ed269Error(f"{where}: a point is not numeric") from error
    if not (-180.0 <= lon <= 180.0 and -90.0 <= lat <= 90.0):
        raise Ed269Error(f"{where}: point {raw} is outside [lon, lat] ranges")
    return lon, lat


def _ring(raw: Any, where: str) -> Ring:
    if not isinstance(raw, list):
        raise Ed269Error(f"{where}: a ring is not a list")
    points = [_point(p, where) for p in raw]
    if points and points[0] != points[-1]:
        points.append(points[0])
    if len(set(points)) < 3:
        raise Ed269Error(f"{where}: a ring needs at least three distinct points")
    return tuple(points)


def _circle(lat_deg: float, lon_deg: float, radius_m: float) -> Ring:
    """Vertices on the circle, by the spherical direct geodesic."""
    distance = radius_m / EARTH_RADIUS_M
    lat1 = math.radians(lat_deg)
    lon1 = math.radians(lon_deg)
    points = []
    for k in range(CIRCLE_SIDES):
        bearing = 2 * math.pi * k / CIRCLE_SIDES
        lat2 = math.asin(
            math.sin(lat1) * math.cos(distance)
            + math.cos(lat1) * math.sin(distance) * math.cos(bearing)
        )
        lon2 = lon1 + math.atan2(
            math.sin(bearing) * math.sin(distance) * math.cos(lat1),
            math.cos(distance) - math.sin(lat1) * math.sin(lat2),
        )
        points.append((math.degrees(lon2), math.degrees(lat2)))
    points.append(points[0])
    return tuple(points)


# --- when a zone is in force ---------------------------------------------------


def _applicability(raw: Any, where: str) -> tuple[dict[str, Any], ...] | None:
    if raw is None or raw == []:
        return None
    if not isinstance(raw, list):
        raise Ed269Error(f"{where}: applicability is not a list")
    periods = []
    for period in raw:
        if not isinstance(period, dict):
            raise Ed269Error(f"{where}: an applicability period is not an object")
        start = _datetime(period.get("startDateTime"), where)
        end = _datetime(period.get("endDateTime"), where)
        schedule = _schedule(
            period.get("schedule", period.get("dailyPeriod")), where
        )
        permanent = period.get("permanent") == "YES"
        if permanent and start is None and end is None and schedule is None:
            # In force at all times: nothing else can narrow it.
            return None
        periods.append(
            {
                "start": None if start is None else start.isoformat(),
                "end": None if end is None else end.isoformat(),
                "schedule": schedule,
            }
        )
    return tuple(periods)


def _datetime(raw: Any, where: str) -> datetime | None:
    if raw is None or raw == "":
        return None
    try:
        value = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError as error:
        raise Ed269Error(f"{where}: date {raw!r}") from error
    if value.tzinfo is None:
        raise Ed269Error(f"{where}: date {raw!r} has no time zone")
    return value.astimezone(UTC)


def _schedule(raw: Any, where: str) -> list[dict[str, Any]] | None:
    """Weekly periods in UTC, as {days, start, end} with times "HH:MM:SS"."""
    if raw is None:
        return None
    if not isinstance(raw, list):
        raise Ed269Error(f"{where}: schedule is not a list")
    periods = []
    for daily in raw:
        if not isinstance(daily, dict):
            raise Ed269Error(f"{where}: a schedule entry is not an object")
        days = daily.get("day") or []
        start, end = daily.get("startTime"), daily.get("endTime")
        if not days and start is None and end is None:
            # An empty placeholder, as some files write for "no schedule".
            continue
        if not isinstance(days, list) or not all(
            d in _DAYS or d == "ANY" for d in days
        ):
            raise Ed269Error(f"{where}: schedule days {days!r}")
        periods.append(
            {
                "days": list(_DAYS) if not days or "ANY" in days else list(days),
                "start": _utc_time(start, where),
                "end": _utc_time(end, where),
            }
        )
    return periods or None


def _utc_time(raw: Any, where: str) -> str:
    match = _TIME.match(str(raw)) if raw is not None else None
    if match is None:
        raise Ed269Error(f"{where}: time {raw!r}")
    hours, minutes = int(match["h"]), int(match["m"])
    seconds = int(match["s"] or 0)
    zone = match["tz"]
    if zone is None:
        raise Ed269Error(f"{where}: time {raw!r} has no time zone")
    offset = timedelta()
    if zone != "Z":
        sign = 1 if zone[0] == "+" else -1
        digits = zone[1:].replace(":", "")
        offset = sign * timedelta(hours=int(digits[:2]), minutes=int(digits[2:]))
    # A time of day moved to UTC. Crossing midnight moves the day too; the
    # published files seen use Z, so that case is refused, not half-handled.
    local = datetime.combine(datetime(2000, 1, 3).date(), time(hours, minutes, seconds))
    utc = local - offset
    if utc.date() != local.date():
        raise Ed269Error(
            f"{where}: time {raw!r} crosses midnight in UTC; not supported"
        )
    return utc.strftime("%H:%M:%S")


def in_force(applicability: list[dict[str, Any]] | None, at: datetime) -> bool:
    """Whether a zone with this stored applicability is in force at `at`."""
    if applicability is None:
        return True
    at = at.astimezone(UTC)
    for period in applicability:
        if period["start"] is not None and at < datetime.fromisoformat(period["start"]):
            continue
        if period["end"] is not None and at > datetime.fromisoformat(period["end"]):
            continue
        schedule = period.get("schedule")
        if not schedule:
            return True
        day = _DAYS[at.weekday()]
        clock = at.strftime("%H:%M:%S")
        for weekly in schedule:
            if day not in weekly["days"]:
                continue
            start, end = weekly["start"], weekly["end"]
            # 22:00 to 06:00 runs past midnight: counted on the day it starts
            # and the morning after, as the listed day's night.
            if start <= end and start <= clock <= end:
                return True
            if start > end and (clock >= start or clock <= end):
                return True
    return False

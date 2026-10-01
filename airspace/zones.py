"""Is an aircraft inside a geographical zone that applies now? In flight. P5-15, U-03.

No route is checked against zones before a flight; the system never sees
one. This checks where an aircraft *is*, on every telemetry tick, which is
what a supervisor watching the air needs whether or not anyone planned the
flight.

Zones are ED-269 zones (`airspace/ed269.py`) stored in `airspace_zones`
(migration 0007_geo_awareness). Each has one volume: a polygon or a circle,
between a lower and an upper limit, each limit with its own reference.

## Horizontally

A polygon is tested by ray casting on longitude and latitude, treating
edges as straight in degrees. For zones a few kilometres across, the
difference from a geodesic edge is metres at most, well under GPS error at
the boundary; a zone the size of a country would need the database's own
`ST_Contains` on `geography`. Interior rings (holes) are honoured.

A circle is tested exactly, by the geodesic distance from its centre on the
WGS-84 ellipsoid (`airspace/geodesy.py`), which is what PostGIS's buffer on
`geography` draws, not against the inscribed polygon `geom` holds. Every
shape is first tested against its bounding box.

## Vertically, each limit in its own reference

A limit is compared with the aircraft's height in the same reference, never
converted into another with a single number (CLAUDE.md: AMSL and AGL are
never mixed):

- AMSL: the aircraft's AMSL altitude.
- AGL: the aircraft's AMSL altitude less the ground under it (the DEM, as the
  height limit uses it, P5-19).
- WGS84: the limit is height above the ellipsoid, made AMSL with the geoid
  undulation under the aircraft (`common/geoid.py`).

Where the ground or the geoid is unknown, that limit cannot be judged. The
monitor then reports the zone **not evaluated** for that aircraft rather
than guessing (`AirspaceMonitor`). A limit that can be judged and excludes
the aircraft still decides: above an AMSL ceiling is outside, whatever an
AGL floor would have said.

A missing limit is unbounded: no lower limit is from the ground (or below
it), no upper limit is unlimited.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine

from airspace.ed269 import (
    Circle,
    GeoZone,
    Period,
    Polygon,
    Reason,
    Restriction,
    Uom,
    VerticalReference,
    Volume,
    applies,
)
from airspace.geodesy import distance_m

Ring = tuple[tuple[float, float], ...]

EARTH_RADIUS_M = 6_371_008.8


class RowType(StrEnum):
    """What an `airspace_zones` row is. Only a geozone restricts."""

    GEOZONE = "geozone"
    CORRIDOR = "corridor"
    BASE = "base"


class Undulation(Protocol):
    """`common.geoid.GeoidGrid`, or anything that answers like it."""

    def undulation_m(self, lat_deg: float, lon_deg: float) -> float: ...


@dataclass(frozen=True, slots=True)
class Limit:
    value_m: float
    reference: VerticalReference


@dataclass(frozen=True, slots=True)
class CircleM:
    lat_deg: float
    lon_deg: float
    radius_m: float


@dataclass(frozen=True)
class Zone:
    """A zone as the monitor checks it: metres, parsed periods."""

    zone_id: UUID
    identifier: str
    name: str | None
    restriction: Restriction
    # (lon_deg, lat_deg) pairs, GeoJSON order. Empty for a circle.
    exterior: Ring
    holes: tuple[Ring, ...]
    circle: CircleM | None
    lower: Limit | None
    upper: Limit | None
    periods: tuple[Period, ...]
    message: str | None = None
    reason: tuple[str, ...] = ()

    def applies_at(self, at: datetime) -> bool:
        return applies(self.periods, at)

    def __post_init__(self) -> None:
        # The bounding box, checked before the ray casting: a track far
        # from a zone of thousands of vertices costs four comparisons.
        if self.circle is None:
            lons = [p[0] for p in self.exterior]
            lats = [p[1] for p in self.exterior]
            box = (min(lons), min(lats), max(lons), max(lats))
        else:
            # Degrees of latitude are at least 110.5 km on WGS-84; of
            # longitude, that times cos(lat). The box is a little generous,
            # never short.
            c = self.circle
            d_lat = c.radius_m / 110_500.0
            d_lon = d_lat / max(
                math.cos(math.radians(min(abs(c.lat_deg) + d_lat, 89.9))), 1e-6
            )
            box = (
                c.lon_deg - d_lon,
                c.lat_deg - d_lat,
                c.lon_deg + d_lon,
                c.lat_deg + d_lat,
            )
        object.__setattr__(self, "bbox", box)

    # (min_lon, min_lat, max_lon, max_lat), set from the shape.
    bbox: tuple[float, float, float, float] = field(init=False, compare=False)

    def contains_horizontally(self, lat_deg: float, lon_deg: float) -> bool:
        min_lon, min_lat, max_lon, max_lat = self.bbox
        if not (min_lon <= lon_deg <= max_lon and min_lat <= lat_deg <= max_lat):
            return False
        if self.circle is not None:
            # On the ellipsoid, as PostGIS's geography buffer draws it.
            return (
                distance_m(lat_deg, lon_deg, self.circle.lat_deg, self.circle.lon_deg)
                <= self.circle.radius_m
            )
        if not _in_ring(self.exterior, lon_deg, lat_deg):
            return False
        return not any(_in_ring(hole, lon_deg, lat_deg) for hole in self.holes)

    @property
    def references(self) -> frozenset[VerticalReference]:
        return frozenset(
            limit.reference for limit in (self.lower, self.upper) if limit is not None
        )


def great_circle_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Haversine distance on a sphere of the Earth's mean radius."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = phi2 - phi1
    d_lambda = math.radians(lon2 - lon1)
    a = (
        math.sin(d_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    )
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


def _in_ring(ring: Ring, x: float, y: float) -> bool:
    """Even-odd ray casting. A point exactly on an edge may go either way,
    which at a zone boundary is below what any position fix can resolve."""
    inside = False
    count = len(ring)
    for i in range(count):
        x1, y1 = ring[i]
        x2, y2 = ring[(i + 1) % count]
        if (y1 > y) != (y2 > y):
            crossing_x = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if x < crossing_x:
                inside = not inside
    return inside


def monitored_zone(zone_id: UUID, geozone: GeoZone) -> Zone:
    """The monitor's view of a stored ED-269 zone."""
    volume = geozone.volume
    projection = volume.projection
    circle = None
    exterior: Ring = ()
    holes: tuple[Ring, ...] = ()
    if isinstance(projection, Circle):
        radius_m = volume.radius_m
        assert radius_m is not None
        circle = CircleM(
            lat_deg=projection.center_lat_deg,
            lon_deg=projection.center_lon_deg,
            radius_m=radius_m,
        )
    else:
        exterior, holes = projection.rings[0], projection.rings[1:]
    lower_m, upper_m = volume.lower_m, volume.upper_m
    return Zone(
        zone_id=zone_id,
        identifier=geozone.identifier,
        name=geozone.name,
        restriction=geozone.restriction,
        exterior=exterior,
        holes=holes,
        circle=circle,
        lower=None if lower_m is None else Limit(lower_m, volume.lower_reference),
        upper=None if upper_m is None else Limit(upper_m, volume.upper_reference),
        periods=geozone.periods(),
        message=geozone.message,
        reason=tuple(r.value for r in geozone.reason or ()),
    )


def needs_terrain(zone: Zone) -> bool:
    """Whether judging the zone vertically needs the DEM: an AGL ceiling, or
    an AGL floor above the ground. A floor at or below it is met by any
    airborne aircraft."""
    return any(
        limit is not None
        and limit.reference is VerticalReference.AGL
        and (not is_lower or limit.value_m > 0)
        for limit, is_lower in ((zone.lower, True), (zone.upper, False))
    )


def unjudgeable(
    zones: list[Zone], *, terrain: bool, geoid: bool
) -> dict[str, list[str]]:
    """Identifiers of zones with a limit that cannot be judged anywhere, by
    what is missing: AGL limits that need the ground (`needs_terrain`)
    without terrain, WGS84 limits without the geoid. Such a zone is never evaluated vertically, so the service says
    so when it loads them rather than only when an aircraft is inside."""
    missing: dict[str, list[str]] = {}
    for zone in zones:
        if not terrain and needs_terrain(zone):
            missing.setdefault("TERRAIN_DIR", []).append(zone.identifier)
        if not geoid and VerticalReference.WGS84 in zone.references:
            missing.setdefault("GEOID_PATH", []).append(zone.identifier)
    return missing


# --- from the database ------------------------------------------------------------

ZONE_COLUMNS = """
    id, type, identifier, country, name, ed269_type, restriction, reason,
    message, zone_authority, applicability, uom_dimensions, lower_limit,
    lower_reference, upper_limit, upper_reference,
    ST_X(circle_center) AS circle_lon_deg, ST_Y(circle_center) AS circle_lat_deg,
    circle_radius, ed269_extra, ST_AsGeoJSON(geom, 15) AS geojson,
    created_at, updated_at
"""


def geozone_from_row(row: Mapping[Any, Any]) -> GeoZone:
    """The ED-269 zone a row stores. `row` has `ZONE_COLUMNS`.

    The stored values were validated on the way in (`api/zones.py`), and
    the table's constraints hold the rest, so this does not validate again.
    For a circle the published centre and radius are used, never the
    polygon `geom` holds for drawing.
    """
    projection: Polygon | Circle
    if row["circle_radius"] is not None:
        projection = Circle(
            center_lon_deg=float(row["circle_lon_deg"]),
            center_lat_deg=float(row["circle_lat_deg"]),
            radius=float(row["circle_radius"]),
        )
    else:
        geometry = json.loads(row["geojson"])
        if geometry.get("type") != "Polygon":
            raise ValueError(
                f"zone {row['identifier']!r} is a {geometry.get('type')}, not a Polygon"
            )
        projection = Polygon(
            rings=tuple(
                tuple((float(p[0]), float(p[1])) for p in ring)
                for ring in geometry["coordinates"]
            )
        )
    reason = row["reason"]
    return GeoZone(
        identifier=row["identifier"],
        country=row["country"],
        name=row["name"],
        type=row["ed269_type"],
        restriction=Restriction(row["restriction"]),
        reason=None if reason is None else tuple(Reason(r) for r in reason),
        message=row["message"],
        zone_authority=tuple(dict(a) for a in row["zone_authority"]),
        applicability=tuple(dict(p) for p in row["applicability"]),
        volume=Volume(
            uom=Uom(row["uom_dimensions"]),
            lower_limit=_optional_float(row["lower_limit"]),
            lower_reference=VerticalReference(row["lower_reference"]),
            upper_limit=_optional_float(row["upper_limit"]),
            upper_reference=VerticalReference(row["upper_reference"]),
            projection=projection,
        ),
        extra=dict(row["ed269_extra"]),
    )


def _optional_float(value: Any) -> float | None:
    return None if value is None else float(value)


# Corridors and bases are NO_RESTRICTION by constraint, so this is every
# geozone that can raise an alert.
_ZONES = sa.text(
    f"SELECT {ZONE_COLUMNS} FROM airspace_zones "
    "WHERE restriction <> 'NO_RESTRICTION' ORDER BY identifier"
)


async def load_zones(engine: AsyncEngine) -> list[Zone]:
    async with engine.connect() as connection:
        rows = (await connection.execute(_ZONES)).mappings().all()
    return [monitored_zone(row["id"], geozone_from_row(row)) for row in rows]

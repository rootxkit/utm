"""Is an aircraft inside a no-fly or restricted zone? In flight, not at planning.

P5-05 checks a *route* against zones before release. This checks where an
aircraft *is*, on every telemetry tick: the monitoring half, which is what a
supervisor watching the air needs whether or not anyone planned the flight.

Zones come from `airspace_zones` in the relational database (P2-01): a WGS84
polygon, an optional AMSL band and, for zones imported from an authority's
ED-269 file (P5-18), an optional band above the ground and the times it is in
force. A missing bound is unbounded, so a zone with none is a column from the
ground up, at all times.

A bound above the ground is checked against the DEM (P5-00). Where the ground
is unknown, the aircraft is taken to be inside that bound: a zone that cannot
be ruled out is alerted, the safe side for a no-fly zone.

The containment test is ray casting on longitude and latitude, treating edges
as straight in degrees. For zones a few kilometres across, the difference from
a geodesic edge is metres at most, well under GPS error at the boundary; a
zone the size of a country would need the database's own `ST_Contains` on
`geography`. Interior rings (holes) are honoured.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine

from airspace.ed269 import in_force

Ring = tuple[tuple[float, float], ...]


class ZoneType(StrEnum):
    NO_FLY = "no_fly"
    RESTRICTED = "restricted"


@dataclass(frozen=True, slots=True)
class Zone:
    zone_id: UUID
    name: str
    type: ZoneType
    # (lon_deg, lat_deg) pairs, GeoJSON order.
    exterior: Ring
    holes: tuple[Ring, ...]
    min_alt_amsl_m: float | None
    max_alt_amsl_m: float | None
    min_height_agl_m: float | None = None
    max_height_agl_m: float | None = None
    # None: always in force (ed269.in_force).
    applicability: list[dict[str, Any]] | None = field(default=None, compare=False)
    # From an authority's file: its identifier and what it says.
    external_id: str | None = None
    restriction: str | None = None
    message: str | None = None

    def in_force(self, at: datetime) -> bool:
        return in_force(self.applicability, at)

    def contains(
        self,
        lat_deg: float,
        lon_deg: float,
        alt_amsl_m: float,
        height_agl_m: float | None = None,
    ) -> bool:
        """`height_agl_m` None: the ground is unknown, and a bound above the
        ground cannot rule the aircraft out."""
        if self.min_alt_amsl_m is not None and alt_amsl_m < self.min_alt_amsl_m:
            return False
        if self.max_alt_amsl_m is not None and alt_amsl_m > self.max_alt_amsl_m:
            return False
        if height_agl_m is not None:
            if self.min_height_agl_m is not None and height_agl_m < self.min_height_agl_m:
                return False
            if self.max_height_agl_m is not None and height_agl_m > self.max_height_agl_m:
                return False
        if not _in_ring(self.exterior, lon_deg, lat_deg):
            return False
        return not any(_in_ring(hole, lon_deg, lat_deg) for hole in self.holes)


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


def zone_from_geojson(
    *,
    zone_id: UUID,
    name: str,
    zone_type: str,
    geojson: str,
    min_alt_amsl_m: float | None,
    max_alt_amsl_m: float | None,
    min_height_agl_m: float | None = None,
    max_height_agl_m: float | None = None,
    applicability: list[dict[str, Any]] | None = None,
    external_id: str | None = None,
    restriction: str | None = None,
    message: str | None = None,
) -> Zone:
    geometry: dict[str, Any] = json.loads(geojson)
    if geometry.get("type") != "Polygon":
        raise ValueError(f"zone {name!r} is a {geometry.get('type')}, not a Polygon")
    rings = [
        tuple((float(point[0]), float(point[1])) for point in ring)
        for ring in geometry["coordinates"]
    ]
    return Zone(
        zone_id=zone_id,
        name=name,
        type=ZoneType(zone_type),
        exterior=rings[0],
        holes=tuple(rings[1:]),
        min_alt_amsl_m=min_alt_amsl_m,
        max_alt_amsl_m=max_alt_amsl_m,
        min_height_agl_m=min_height_agl_m,
        max_height_agl_m=max_height_agl_m,
        applicability=applicability,
        external_id=external_id,
        restriction=restriction,
        message=message,
    )


_ZONES = sa.text(
    """
    SELECT id, name, type, ST_AsGeoJSON(geom) AS geojson,
           min_alt_amsl_m, max_alt_amsl_m, min_height_agl_m, max_height_agl_m,
           applicability, external_id, restriction, message
    FROM airspace_zones
    WHERE type IN ('no_fly', 'restricted')
    ORDER BY name
    """
)


async def load_zones(engine: AsyncEngine) -> list[Zone]:
    async with engine.connect() as connection:
        rows = (await connection.execute(_ZONES)).all()
    return [
        zone_from_geojson(
            zone_id=row.id,
            name=row.name,
            zone_type=row.type,
            geojson=row.geojson,
            min_alt_amsl_m=row.min_alt_amsl_m,
            max_alt_amsl_m=row.max_alt_amsl_m,
            min_height_agl_m=row.min_height_agl_m,
            max_height_agl_m=row.max_height_agl_m,
            applicability=row.applicability,
            external_id=row.external_id,
            restriction=row.restriction,
            message=row.message,
        )
        for row in rows
    ]

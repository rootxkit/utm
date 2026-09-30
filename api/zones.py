"""Airspace zones for the operator's map. P6-01, P5-18.

Read-only, and every type: the airspace monitor alerts on `no_fly` and
`restricted` (P5-15), but an operator also needs to see corridors and bases
drawn. Geometry is GeoJSON straight from PostGIS, in WGS84 (SRID 4326), so
the map draws exactly what the monitor checks against. A zone imported from an
authority's file carries its source, identifier, restriction and message, and
whether it is in force now.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine

from airspace.ed269 import in_force

_ZONES = sa.text(
    """
    SELECT z.id, z.name, z.type, z.min_alt_amsl_m, z.max_alt_amsl_m,
           z.min_height_agl_m, z.max_height_agl_m, z.external_id, z.restriction,
           z.message, z.applicability, s.name AS source,
           ST_AsGeoJSON(z.geom) AS geojson
    FROM airspace_zones z LEFT JOIN zone_sources s ON s.id = z.source_id
    ORDER BY z.type, z.name
    """
)


@dataclass
class ZoneReader:
    engine: AsyncEngine
    wall: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))

    async def zones(self) -> list[dict[str, Any]]:
        now = self.wall()
        async with self.engine.connect() as connection:
            rows = (await connection.execute(_ZONES)).all()
        return [
            {
                "id": row.id,
                "name": row.name,
                "type": row.type,
                "min_alt_amsl_m": row.min_alt_amsl_m,
                "max_alt_amsl_m": row.max_alt_amsl_m,
                "min_height_agl_m": row.min_height_agl_m,
                "max_height_agl_m": row.max_height_agl_m,
                "source": row.source,
                "external_id": row.external_id,
                "restriction": row.restriction,
                "message": row.message,
                "in_force": in_force(row.applicability, now),
                "geometry": json.loads(row.geojson),
            }
            for row in rows
        ]

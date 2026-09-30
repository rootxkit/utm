"""Airspace zones for the operator's map. P6-01.

Read-only, and every type: the airspace monitor alerts on `no_fly` and
`restricted` (P5-15), but an operator also needs to see corridors and bases
drawn. Geometry is GeoJSON straight from PostGIS, in WGS84 (SRID 4326), so
the map draws exactly what the monitor checks against.
"""

import json
from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine

_ZONES = sa.text(
    """
    SELECT id, name, type, min_alt_amsl_m, max_alt_amsl_m,
           ST_AsGeoJSON(geom) AS geojson
    FROM airspace_zones
    ORDER BY type, name
    """
)


@dataclass
class ZoneReader:
    engine: AsyncEngine

    async def zones(self) -> list[dict[str, Any]]:
        async with self.engine.connect() as connection:
            rows = (await connection.execute(_ZONES)).all()
        return [
            {
                "id": row.id,
                "name": row.name,
                "type": row.type,
                "min_alt_amsl_m": row.min_alt_amsl_m,
                "max_alt_amsl_m": row.max_alt_amsl_m,
                "geometry": json.loads(row.geojson),
            }
            for row in rows
        ]

"""ED-269 documents for tests, in both published shapes. P5-18.

Written here, not copied: the zones are invented, around Tbilisi.
"""

from __future__ import annotations

import json
from typing import Any

LAT, LON = 41.7151, 44.8271


def square(lon: float = LON, lat: float = LAT, half_deg: float = 0.01) -> list[list[list[float]]]:
    return [
        [
            [lon - half_deg, lat - half_deg],
            [lon + half_deg, lat - half_deg],
            [lon + half_deg, lat + half_deg],
            [lon - half_deg, lat + half_deg],
            [lon - half_deg, lat - half_deg],
        ]
    ]


def volume(**changes: Any) -> dict[str, Any]:
    return {
        "uomDimensions": "M",
        "lowerLimit": 0,
        "lowerVerticalReference": "AGL",
        "upperLimit": 120,
        "upperVerticalReference": "AGL",
        "horizontalProjection": {"type": "Polygon", "coordinates": square()},
        **changes,
    }


def zone(identifier: str = "TEST01", **changes: Any) -> dict[str, Any]:
    return {
        "identifier": identifier,
        "country": "GEO",
        "name": f"Test zone {identifier}",
        "type": "COMMON",
        "restriction": "PROHIBITED",
        "reason": ["AIR_TRAFFIC"],
        "message": "Test zone, not a real restriction",
        "applicability": [{"permanent": "YES"}],
        "zoneAuthority": [{"name": "Test authority"}],
        "geometry": [volume()],
        **changes,
    }


def features(*zones: dict[str, Any], title: str = "Test zones v1") -> bytes:
    return json.dumps({"title": title, "description": "tests", "features": list(zones)}).encode()


def zone_list(*zones: dict[str, Any], created_at: str = "2026-09-30T08:00:00Z") -> bytes:
    return json.dumps(
        {"formatVersion": "1.0.0", "createdAt": created_at, "UASZoneList": list(zones)}
    ).encode()

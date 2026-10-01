"""Zones for tests, built through the ED-269 reader so they are ones it accepts."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from airspace.ed269 import GeoZone, parse_zone
from airspace.zones import Zone, monitored_zone

PERMANENT = [{"permanent": "YES"}]


def square(
    lat_deg: float, lon_deg: float, half_deg: float = 0.01
) -> list[list[list[float]]]:
    return [
        [
            [lon_deg - half_deg, lat_deg - half_deg],
            [lon_deg + half_deg, lat_deg - half_deg],
            [lon_deg + half_deg, lat_deg + half_deg],
            [lon_deg - half_deg, lat_deg + half_deg],
            [lon_deg - half_deg, lat_deg - half_deg],
        ]
    ]


def feature(
    *,
    identifier: str = "T1",
    restriction: str = "PROHIBITED",
    coordinates: list[list[list[float]]] | None = None,
    circle: tuple[float, float, float] | None = None,
    lower: tuple[float, str] | None = None,
    upper: tuple[float, str] | None = None,
    uom: str = "M",
    applicability: list[dict[str, Any]] | None = None,
    name: str | None = "Test zone",
    **extra: Any,
) -> dict[str, Any]:
    """An ED-269 feature. `circle` is (lat_deg, lon_deg, radius); `lower`
    and `upper` are (value, reference); absent means unbounded."""
    if circle is not None:
        projection: dict[str, Any] = {
            "type": "Circle",
            "center": [circle[1], circle[0]],
            "radius": circle[2],
        }
    else:
        assert coordinates is not None
        projection = {"type": "Polygon", "coordinates": coordinates}
    volume: dict[str, Any] = {
        "uomDimensions": uom,
        "lowerVerticalReference": "AMSL" if lower is None else lower[1],
        "upperVerticalReference": "AMSL" if upper is None else upper[1],
        "horizontalProjection": projection,
    }
    if lower is not None:
        volume["lowerLimit"] = lower[0]
    if upper is not None:
        volume["upperLimit"] = upper[0]
    out: dict[str, Any] = {
        "identifier": identifier,
        "country": "GEO",
        "type": "COMMON",
        "restriction": restriction,
        "applicability": PERMANENT if applicability is None else applicability,
        "zoneAuthority": [],
        "geometry": [volume],
        **extra,
    }
    if name is not None:
        out["name"] = name
    return out


def geozone(**kwargs: Any) -> GeoZone:
    return parse_zone(feature(**kwargs))


def zone(zone_id: UUID = UUID(int=77), **kwargs: Any) -> Zone:
    return monitored_zone(zone_id, geozone(**kwargs))

"""Geographical zones: read, written by the authority, imported and exported
in ED-269. P6-01, U-03.

A zone is an ED-269 `UASZoneVersion` (`airspace/ed269.py`) stored in
`airspace_zones` (migration 0006_geo_awareness). Every zone written here,
by the console's editor or by an import, goes through the same strict
reader, `airspace.ed269.parse_zone`, so what is stored is always something
the export can write back unchanged and the monitor can evaluate.

## Every change is two writes, or none

The row and its `events` row are written in one transaction
(`api.registry.audit`), as the fleet registry does: `zone_created`,
`zone_updated` (with the zone before and after) and `zone_deleted` (with the
zone as it was), against `airspace_zone` and the zone's id. An import is one
transaction for the whole file, with one event per zone it changed and one
`zones_imported` event for the file (its SHA-256, and what it added and
changed).

## An import adds and replaces, by identifier

A zone in the file whose identifier is new is created; one whose identifier
exists is replaced if anything in it differs, and left alone otherwise.
Zones absent from the file are kept: a file is not taken to be the whole
airspace, since a zone drawn in the console must not vanish because an
authority's file did not mention it. A dry run reads, validates and
compares, and writes nothing.

## Only geozones

Corridors and bases (`type`) are listed for drawing but are not ED-269 zones
of an authority: they are not exported, and an import or an edit that would
overwrite one by its identifier is refused.

## The monitor picks a change up within its refresh

`python -m airspace` re-reads zones every 60 s (`ZONE_REFRESH_S`), so a
zone created, edited or deleted here alerts, or stops alerting, within a
minute.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from airspace.ed269 import (
    Circle,
    Ed269Document,
    GeoZone,
    Problem,
    applies,
    document,
    feature,
)
from airspace.zones import ZONE_COLUMNS, RowType, geozone_from_row
from api.actors import Actor
from api.registry import ConflictError, NotFoundError, audit, refused

ENTITY = "airspace_zone"
IMPORT_ENTITY = "airspace_zone_import"


def _now() -> datetime:
    return datetime.now(UTC)


class ZoneImportRefusedError(ConflictError):
    """An import that is valid ED-269 but would overwrite what it must not."""

    def __init__(self, problems: list[Problem]) -> None:
        super().__init__(
            "; ".join(f"{p.field}: {p.reason}" for p in problems),
            code="zone_import_refused",
        )
        self.problems = problems


@dataclass
class ImportReport:
    sha256: str
    dry_run: bool
    zones: int
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "sha256": self.sha256,
            "dry_run": self.dry_run,
            "zones": self.zones,
            "created": self.created,
            "updated": self.updated,
            "unchanged": self.unchanged,
        }


_SELECT = f"SELECT {ZONE_COLUMNS} FROM airspace_zones"

# A polygon as published; a circle as the polygon PostGIS inscribes in it on
# `geography` (64 sides), with the published centre and radius kept beside.
_POLYGON_GEOM = "ST_SetSRID(ST_GeomFromGeoJSON(:geojson), 4326)"
_CIRCLE_GEOM = (
    "ST_Buffer(ST_SetSRID(ST_MakePoint(:center_lon, :center_lat), 4326)"
    "::geography, :radius_m, 'quad_segs=16')::geometry"
)
_CENTER = "ST_SetSRID(ST_MakePoint(:center_lon, :center_lat), 4326)"


def _insert_sql(geozone: GeoZone) -> sa.TextClause:
    circle = isinstance(geozone.volume.projection, Circle)
    return sa.text(
        f"""
        INSERT INTO airspace_zones
          (type, name, geom, identifier, country, ed269_type, restriction,
           reason, message, zone_authority, applicability, uom_dimensions,
           lower_limit, lower_reference, upper_limit, upper_reference,
           circle_center, circle_radius, ed269_extra)
        VALUES
          ('geozone', :name, {_CIRCLE_GEOM if circle else _POLYGON_GEOM},
           :identifier, :country, :ed269_type, :restriction, :reason, :message,
           CAST(:zone_authority AS jsonb), CAST(:applicability AS jsonb),
           :uom_dimensions, :lower_limit, :lower_reference, :upper_limit,
           :upper_reference, {_CENTER if circle else "NULL"}, :circle_radius,
           CAST(:ed269_extra AS jsonb))
        RETURNING id
        """
    )


def _update_sql(geozone: GeoZone) -> sa.TextClause:
    circle = isinstance(geozone.volume.projection, Circle)
    return sa.text(
        f"""
        UPDATE airspace_zones SET
            name = :name,
            geom = {_CIRCLE_GEOM if circle else _POLYGON_GEOM},
            identifier = :identifier, country = :country,
            ed269_type = :ed269_type, restriction = :restriction,
            reason = :reason, message = :message,
            zone_authority = CAST(:zone_authority AS jsonb),
            applicability = CAST(:applicability AS jsonb),
            uom_dimensions = :uom_dimensions,
            lower_limit = :lower_limit, lower_reference = :lower_reference,
            upper_limit = :upper_limit, upper_reference = :upper_reference,
            circle_center = {_CENTER if circle else "NULL"},
            circle_radius = :circle_radius,
            ed269_extra = CAST(:ed269_extra AS jsonb),
            updated_at = now()
        WHERE id = :id
        """
    )


def _params(geozone: GeoZone) -> dict[str, Any]:
    volume = geozone.volume
    projection = volume.projection
    params: dict[str, Any] = {
        "name": geozone.name,
        "identifier": geozone.identifier,
        "country": geozone.country,
        "ed269_type": geozone.type,
        "restriction": geozone.restriction.value,
        "reason": None
        if geozone.reason is None
        else [reason.value for reason in geozone.reason],
        "message": geozone.message,
        "zone_authority": json.dumps(list(geozone.zone_authority)),
        "applicability": json.dumps(list(geozone.applicability)),
        "uom_dimensions": volume.uom.value,
        "lower_limit": volume.lower_limit,
        "lower_reference": volume.lower_reference.value,
        "upper_limit": volume.upper_limit,
        "upper_reference": volume.upper_reference.value,
        "ed269_extra": json.dumps(geozone.extra),
        "circle_radius": None,
    }
    if isinstance(projection, Circle):
        params.update(
            center_lon=projection.center_lon_deg,
            center_lat=projection.center_lat_deg,
            radius_m=volume.radius_m,
            circle_radius=projection.radius,
        )
    else:
        params["geojson"] = json.dumps(
            {
                "type": "Polygon",
                "coordinates": [[list(p) for p in ring] for ring in projection.rings],
            }
        )
    return params


@dataclass
class ZoneStore:
    engine: AsyncEngine
    # When "active now" is judged; a test sets it.
    wall: Callable[[], datetime] = _now

    # --- reading ------------------------------------------------------------------

    async def list_zones(self) -> list[dict[str, Any]]:
        """Every row, geozones, corridors and bases, for drawing."""
        async with self.engine.connect() as connection:
            rows = (
                (
                    await connection.execute(
                        sa.text(f"{_SELECT} ORDER BY type, identifier")
                    )
                )
                .mappings()
                .all()
            )
        now = self.wall()
        return [self._out(row, now) for row in rows]

    async def get(self, zone_id: UUID) -> dict[str, Any]:
        async with self.engine.connect() as connection:
            row = await self._row(connection, zone_id)
        return self._out(row, self.wall())

    def _out(self, row: Any, now: datetime) -> dict[str, Any]:
        geozone = geozone_from_row(row)
        return {
            "id": row["id"],
            "type": row["type"],
            "active_now": applies(geozone.periods(), now),
            "feature": feature(geozone),
            # What to draw: the polygon, or the one inscribed in a circle.
            "geometry": json.loads(row["geojson"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    async def _row(
        self, connection: AsyncConnection, zone_id: UUID, *, lock: bool = False
    ) -> Any:
        row = (
            (
                await connection.execute(
                    sa.text(
                        f"{_SELECT} WHERE id = :id" + (" FOR UPDATE" if lock else "")
                    ),
                    {"id": str(zone_id)},
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise NotFoundError(f"no zone {zone_id}")
        return row

    async def export(self) -> dict[str, Any]:
        """Every geozone as one ED-269 document, by identifier."""
        async with self.engine.connect() as connection:
            rows = (
                (
                    await connection.execute(
                        sa.text(f"{_SELECT} WHERE type = 'geozone' ORDER BY identifier")
                    )
                )
                .mappings()
                .all()
            )
        exported_at = self.wall().astimezone(UTC).isoformat(timespec="seconds")
        return document(
            (geozone_from_row(row) for row in rows),
            title=f"UAS geographical zones, exported {exported_at}",
        )

    # --- writing ------------------------------------------------------------------

    async def create(self, geozone: GeoZone, *, actor: Actor) -> dict[str, Any]:
        try:
            async with self.engine.begin() as connection:
                zone_id = await self._insert(connection, geozone)
                await audit(
                    connection,
                    ENTITY,
                    zone_id,
                    "zone_created",
                    {"source": "api", "feature": feature(geozone)},
                    actor=actor,
                )
        except IntegrityError as error:
            raise refused("zone", geozone.identifier, error) from error
        return await self.get(zone_id)

    async def update(
        self, zone_id: UUID, geozone: GeoZone, *, actor: Actor
    ) -> dict[str, Any]:
        """Replace the zone. Nothing that differs: nothing written."""
        try:
            async with self.engine.begin() as connection:
                row = await self._row(connection, zone_id, lock=True)
                self._refuse_non_geozone(row)
                before = geozone_from_row(row)
                if before != geozone:
                    await connection.execute(
                        _update_sql(geozone), {**_params(geozone), "id": str(zone_id)}
                    )
                    await audit(
                        connection,
                        ENTITY,
                        zone_id,
                        "zone_updated",
                        {
                            "source": "api",
                            "before": feature(before),
                            "after": feature(geozone),
                        },
                        actor=actor,
                    )
        except IntegrityError as error:
            raise refused("zone", geozone.identifier, error) from error
        return await self.get(zone_id)

    async def delete(self, zone_id: UUID, *, actor: Actor) -> None:
        async with self.engine.begin() as connection:
            row = await self._row(connection, zone_id, lock=True)
            self._refuse_non_geozone(row)
            await connection.execute(
                sa.text("DELETE FROM airspace_zones WHERE id = :id"),
                {"id": str(zone_id)},
            )
            await audit(
                connection,
                ENTITY,
                zone_id,
                "zone_deleted",
                {"feature": feature(geozone_from_row(row))},
                actor=actor,
            )

    @staticmethod
    def _refuse_non_geozone(row: Any) -> None:
        if row["type"] != RowType.GEOZONE:
            raise ConflictError(
                f"zone {row['identifier']!r} is a {row['type']}, not a geozone; "
                "it is not edited here",
                code="not_a_geozone",
            )

    async def _insert(self, connection: AsyncConnection, geozone: GeoZone) -> UUID:
        zone_id: UUID = (
            await connection.execute(_insert_sql(geozone), _params(geozone))
        ).scalar_one()
        return zone_id

    async def import_document(
        self,
        parsed: Ed269Document,
        data: bytes,
        *,
        actor: Actor,
        dry_run: bool,
    ) -> ImportReport:
        """Add and replace the file's zones by identifier, in one transaction.
        A dry run compares and writes nothing. Raises ZoneImportRefusedError
        when a zone would overwrite a corridor or a base."""
        report = ImportReport(
            sha256=hashlib.sha256(data).hexdigest(),
            dry_run=dry_run,
            zones=len(parsed.zones),
        )
        try:
            async with self.engine.begin() as connection:
                existing = {
                    row["identifier"]: row
                    for row in (
                        await connection.execute(
                            sa.text(
                                f"{_SELECT} WHERE identifier = ANY(:identifiers) "
                                "FOR UPDATE"
                            ),
                            {"identifiers": [z.identifier for z in parsed.zones]},
                        )
                    )
                    .mappings()
                    .all()
                }
                problems = [
                    Problem(
                        f"features[{index}].identifier",
                        f"{zone.identifier!r} is a {existing[zone.identifier]['type']} "
                        "here, not a geozone; an import does not overwrite it",
                    )
                    for index, zone in enumerate(parsed.zones)
                    if zone.identifier in existing
                    and existing[zone.identifier]["type"] != RowType.GEOZONE
                ]
                if problems:
                    raise ZoneImportRefusedError(problems)
                changes: list[tuple[str, UUID | None, GeoZone, GeoZone | None]] = []
                for zone in parsed.zones:
                    row = existing.get(zone.identifier)
                    if row is None:
                        report.created.append(zone.identifier)
                        changes.append(("zone_created", None, zone, None))
                        continue
                    stored = geozone_from_row(row)
                    if stored == zone:
                        report.unchanged.append(zone.identifier)
                    else:
                        report.updated.append(zone.identifier)
                        changes.append(("zone_updated", row["id"], zone, stored))
                if dry_run:
                    # Leaving the block commits a transaction that only read.
                    return report
                for event, zone_id, zone, before in changes:
                    payload: dict[str, Any] = {
                        "source": "ed269_import",
                        "import_sha256": report.sha256,
                    }
                    if zone_id is None:
                        zone_id = await self._insert(connection, zone)
                        payload["feature"] = feature(zone)
                    else:
                        await connection.execute(
                            _update_sql(zone), {**_params(zone), "id": str(zone_id)}
                        )
                        assert before is not None
                        payload["before"] = feature(before)
                        payload["after"] = feature(zone)
                    await audit(
                        connection, ENTITY, zone_id, event, payload, actor=actor
                    )
                await connection.execute(
                    sa.text(
                        "INSERT INTO events (actor_type, actor_id, entity_type, "
                        " entity_id, event_type, payload) "
                        "VALUES (:actor_type, :actor_id, :entity, :sha, "
                        " 'zones_imported', CAST(:payload AS jsonb))"
                    ),
                    {
                        "actor_type": actor.actor_type,
                        "actor_id": actor.actor_id,
                        "entity": IMPORT_ENTITY,
                        "sha": report.sha256,
                        "payload": json.dumps(
                            {**report.as_dict(), "title": parsed.title}
                        ),
                    },
                )
        except IntegrityError as error:
            raise refused("zone import", report.sha256[:12], error) from error
        return report

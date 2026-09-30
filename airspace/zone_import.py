"""Importing an authority's ED-269 zones into `airspace_zones`. P5-18.

A source is an authority's published file, known by a name ("GCAA", say).
Importing a new version of it replaces that source's zones in one
transaction, so the monitor never sees half of each. Hand-drawn zones and
other sources' zones are untouched. The airspace monitor re-reads zones every
minute, so an import is in force within a minute and needs no restart.

The change is written to `events`: which zones were added, removed or
changed, by identifier, with the file's version, its SHA-256 and who
imported it. A zone "changed" when its published record changed. Importing
the file already current changes nothing and says so.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine

from airspace.ed269 import FORMAT, Ed269File, ImportedZone, parse


@dataclass
class ImportReport:
    source: str
    version: str | None
    sha256: str
    imported: bool
    zones: int
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    unchanged: int = 0
    skipped: list[tuple[str, str]] = field(default_factory=list)
    bounds: tuple[float, float, float, float] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "version": self.version,
            "sha256": self.sha256,
            "zones": self.zones,
            "added": self.added,
            "removed": self.removed,
            "changed": self.changed,
            "unchanged": self.unchanged,
            "skipped": [{"identifier": i, "why": w} for i, w in self.skipped],
        }


_CURRENT = sa.text(
    "SELECT id, sha256 FROM zone_sources WHERE name = :name AND current FOR UPDATE"
)
_EXISTING = sa.text(
    """
    SELECT external_id, source_record FROM airspace_zones
    WHERE source_id IN (SELECT id FROM zone_sources WHERE name = :name)
    """
)
_RETIRE = sa.text("UPDATE zone_sources SET current = false WHERE name = :name AND current")
_DELETE = sa.text(
    """
    DELETE FROM airspace_zones
    WHERE source_id IN (SELECT id FROM zone_sources WHERE name = :name)
    """
)
_SOURCE = sa.text(
    """
    INSERT INTO zone_sources
      (name, format, version, file_name, sha256, zone_count, imported_by, current)
    VALUES (:name, :format, :version, :file_name, :sha256, :zone_count, :by, true)
    RETURNING id
    """
)
_ZONE = sa.text(
    """
    INSERT INTO airspace_zones
      (name, type, geom, min_alt_amsl_m, max_alt_amsl_m, min_height_agl_m,
       max_height_agl_m, source_id, external_id, restriction, message, reason,
       applicability, source_record)
    VALUES
      (:name, :type, ST_SetSRID(ST_GeomFromGeoJSON(:geojson), 4326),
       :min_alt_amsl_m, :max_alt_amsl_m, :min_height_agl_m, :max_height_agl_m,
       :source_id, :external_id, :restriction, :message, :reason,
       CAST(:applicability AS jsonb), CAST(:source_record AS jsonb))
    """
)
_EVENT = sa.text(
    """
    INSERT INTO events (actor_type, actor_id, entity_type, entity_id, event_type,
                        payload)
    VALUES ('operator', :by, 'zone_source', :name, 'zones_imported',
            CAST(:payload AS jsonb))
    """
)


def _diff(
    existing: dict[str, dict[str, Any]], zones: tuple[ImportedZone, ...]
) -> tuple[list[str], list[str], list[str], int]:
    new = {zone.external_id: zone.source_record for zone in zones}
    added = sorted(set(new) - set(existing))
    removed = sorted(set(existing) - set(new))
    changed = sorted(
        key for key in set(new) & set(existing) if new[key] != existing[key]
    )
    unchanged = len(set(new) & set(existing)) - len(changed)
    return added, removed, changed, unchanged


async def import_zones(
    engine: AsyncEngine,
    data: bytes,
    *,
    source: str,
    file_name: str,
    by: str,
    dry_run: bool = False,
) -> ImportReport:
    """Raises `Ed269Error` for a file that cannot be imported; nothing changes."""
    parsed: Ed269File = parse(data)
    sha256 = hashlib.sha256(data).hexdigest()
    report = ImportReport(
        source=source,
        version=parsed.version,
        sha256=sha256,
        imported=False,
        zones=len(parsed.zones),
        skipped=list(parsed.skipped),
        bounds=parsed.bounds(),
    )
    async with engine.begin() as connection:
        current = (await connection.execute(_CURRENT, {"name": source})).one_or_none()
        if current is not None and current.sha256 == sha256:
            report.unchanged = len(parsed.zones)
            return report
        existing = {
            str(row.external_id): row.source_record
            for row in (await connection.execute(_EXISTING, {"name": source})).all()
        }
        report.added, report.removed, report.changed, report.unchanged = _diff(
            existing, parsed.zones
        )
        if dry_run:
            # Nothing written: leaving the block commits only the reads.
            return report
        await connection.execute(_RETIRE, {"name": source})
        await connection.execute(_DELETE, {"name": source})
        source_id = (
            await connection.execute(
                _SOURCE,
                {
                    "name": source,
                    "format": FORMAT,
                    "version": parsed.version,
                    "file_name": file_name,
                    "sha256": sha256,
                    "zone_count": len(parsed.zones),
                    "by": by,
                },
            )
        ).scalar_one()
        if parsed.zones:
            await connection.execute(
                _ZONE, [_row(zone, source_id) for zone in parsed.zones]
            )
        await connection.execute(
            _EVENT,
            {
                "by": by,
                "name": source,
                "payload": json.dumps(
                    {**report.as_dict(), "file_name": file_name, "format": FORMAT}
                ),
            },
        )
    report.imported = True
    return report


def _row(zone: ImportedZone, source_id: object) -> dict[str, Any]:
    return {
        "name": zone.name,
        "type": zone.zone_type,
        "geojson": zone.geojson(),
        "min_alt_amsl_m": zone.min_alt_amsl_m,
        "max_alt_amsl_m": zone.max_alt_amsl_m,
        "min_height_agl_m": zone.min_height_agl_m,
        "max_height_agl_m": zone.max_height_agl_m,
        "source_id": source_id,
        "external_id": zone.external_id,
        "restriction": zone.restriction,
        "message": zone.message,
        "reason": list(zone.reason),
        "applicability": (
            None if zone.applicability is None else json.dumps(list(zone.applicability))
        ),
        "source_record": json.dumps(zone.source_record),
    }

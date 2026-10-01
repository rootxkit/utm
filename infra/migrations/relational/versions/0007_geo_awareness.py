"""airspace_zones follows EUROCAE ED-269: restriction, limits with their reference

Revision ID: 0007_geo_awareness
Revises: 0006_source_controls
Create Date: 2026-10-01

U-03. A zone becomes an ED-269 `UASZoneVersion` with one airspace volume
(`airspace/ed269.py` says where the field names come from and why one
volume). The table is evolved in place, not shadowed by a second zone
table: the console, the monitor and the GiST index keep reading `geom`.

## What each zone gains

- `identifier` (unique, at most 7 characters), `country` (ISO alpha-3),
  `ed269_type` (ED-269's `type`, "COMMON" in every file seen; the column
  `type` already means something else, below), `restriction`, `reason`,
  `message`, `zone_authority` and `applicability` (both as published, JSON
  lists), and `ed269_extra`: the published fields this system carries but
  does not interpret (`restrictionConditions`, `region`, ...), so an export
  gives back what was imported.
- **Vertical limits with their reference.** `lower_limit` and `upper_limit`
  as published, in `uom_dimensions` (M or FT), each with its own reference:
  AGL, AMSL or WGS84 (height above the ellipsoid). A NULL limit is
  unbounded: from the surface, or unlimited. AGL is checked against the
  terrain and WGS84 through the geoid, never converted with one number here.
- **A circle stays a circle.** `circle_center` and `circle_radius` (in
  `uom_dimensions`) are the published circle, which the monitor checks
  exactly; `geom` holds a polygon inscribed in it (the API writes it with
  PostGIS's buffer on `geography`) for drawing and the index.
- `name` becomes optional, as in ED-269.
- `updated_at`.

## `type` now says what kind of row it is

`type` was `no_fly`, `restricted`, `corridor` or `base`: a restriction and
a purpose in one column. The restriction moves to `restriction`, and `type`
becomes `geozone`, `corridor` or `base`. Only a geozone can restrict: a
corridor or a base is `NO_RESTRICTION` by constraint, drawn on the map and
never alerted on, exactly as before.

`airspace_policy.conditional_zone_severity` (info or warning, seeded
warning) is the alert a CONDITIONAL zone raises: policy, like the other
thresholds in that row.

## How existing zones are mapped

Safe on a populated table: the columns are added nullable, filled by one
UPDATE each, and only then made NOT NULL. Every existing zone alerts exactly
as it did:

| before | restriction | type | alert |
|---|---|---|---|
| no_fly | PROHIBITED | geozone | critical, as before |
| restricted | REQ_AUTHORISATION | geozone | warning, as before |
| corridor, base | NO_RESTRICTION | unchanged | none, as before |

- `min_alt_amsl_m` and `max_alt_amsl_m` become `lower_limit` and
  `upper_limit` in metres AMSL; a NULL stays NULL (unbounded), so a zone
  that was a column from the ground up still is, without needing terrain.
- `applicability` is permanent (`[{"permanent": "YES"}]`): an old zone
  applied at all times.
- `identifier` is `Z` and a six-digit number in order of creation
  (`Z000001`...): old zones had none, and ED-269 requires one.
- `country` is `GEO`: this is Georgia's national system and old zones are
  Georgian. Edit a zone through the API to change it.
- `ed269_type` is `COMMON`, `zone_authority` an empty list, `reason` NULL.

## What a downgrade discards

The downgrade restores the old columns and types. It loses what the old
model cannot hold, always towards more alerting, never less, except for
the first item:

- Zones with `NO_RESTRICTION` are **deleted**: the old model has no
  geozone that does not alert. Corridors and bases are kept.
- CONDITIONAL becomes `restricted` (a warning, whatever the policy said).
- AGL and WGS84 limits are dropped, so the zone extends to the ground or
  without a ceiling; AMSL limits in feet are converted to metres.
- Applicability is dropped: every zone applies at all times.
- A circle stays as its inscribed polygon.
- Identifiers, countries, reasons, messages, authorities, extra fields and
  `conditional_zone_severity` are dropped; a zone without a name is named
  after its identifier first.

Every change made through the API is in `events` with the zone as it was,
so what the downgrade discards is in the audit log, not lost from it.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0007_geo_awareness"
down_revision: str | None = "0006_source_controls"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RESTRICTIONS = ("PROHIBITED", "REQ_AUTHORISATION", "CONDITIONAL", "NO_RESTRICTION")
REFERENCES = ("AGL", "AMSL", "WGS84")
UNITS = ("M", "FT")
ROW_TYPES = ("geozone", "corridor", "base")
OLD_ROW_TYPES = ("no_fly", "restricted", "corridor", "base")


def _in(column: str, values: Sequence[str]) -> str:
    return f"{column} IN ({', '.join(repr(value) for value in values)})"


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE airspace_zones
            ADD COLUMN identifier TEXT,
            ADD COLUMN country TEXT,
            ADD COLUMN ed269_type TEXT,
            ADD COLUMN restriction TEXT,
            ADD COLUMN reason TEXT[],
            ADD COLUMN message TEXT,
            ADD COLUMN zone_authority JSONB,
            ADD COLUMN applicability JSONB,
            ADD COLUMN uom_dimensions TEXT,
            ADD COLUMN lower_limit DOUBLE PRECISION,
            ADD COLUMN lower_reference TEXT,
            ADD COLUMN upper_limit DOUBLE PRECISION,
            ADD COLUMN upper_reference TEXT,
            ADD COLUMN circle_center geometry(Point, 4326),
            ADD COLUMN circle_radius DOUBLE PRECISION,
            ADD COLUMN ed269_extra JSONB NOT NULL DEFAULT '{}'::jsonb,
            ADD COLUMN updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        """
    )
    op.execute(
        """
        WITH numbered AS (
            SELECT id, row_number() OVER (ORDER BY created_at, id) AS n
            FROM airspace_zones
        )
        UPDATE airspace_zones z
        SET identifier = 'Z' || lpad(numbered.n::text, 6, '0')
        FROM numbered WHERE numbered.id = z.id
        """
    )
    op.execute("ALTER TABLE airspace_zones DROP CONSTRAINT airspace_zones_type_known")
    op.execute(
        """
        UPDATE airspace_zones SET
            country = 'GEO',
            ed269_type = 'COMMON',
            restriction = CASE type
                WHEN 'no_fly' THEN 'PROHIBITED'
                WHEN 'restricted' THEN 'REQ_AUTHORISATION'
                ELSE 'NO_RESTRICTION' END,
            type = CASE WHEN type IN ('no_fly', 'restricted') THEN 'geozone'
                        ELSE type END,
            zone_authority = '[]'::jsonb,
            applicability = '[{"permanent": "YES"}]'::jsonb,
            uom_dimensions = 'M',
            lower_limit = min_alt_amsl_m,
            lower_reference = 'AMSL',
            upper_limit = max_alt_amsl_m,
            upper_reference = 'AMSL'
        """
    )
    op.execute(
        """
        ALTER TABLE airspace_zones
            ALTER COLUMN identifier SET NOT NULL,
            ALTER COLUMN country SET NOT NULL,
            ALTER COLUMN ed269_type SET NOT NULL,
            ALTER COLUMN restriction SET NOT NULL,
            ALTER COLUMN zone_authority SET NOT NULL,
            ALTER COLUMN applicability SET NOT NULL,
            ALTER COLUMN uom_dimensions SET NOT NULL,
            ALTER COLUMN lower_reference SET NOT NULL,
            ALTER COLUMN upper_reference SET NOT NULL,
            ALTER COLUMN name DROP NOT NULL,
            DROP CONSTRAINT airspace_zones_altitude_band,
            DROP COLUMN min_alt_amsl_m,
            DROP COLUMN max_alt_amsl_m
        """
    )
    checks = {
        "airspace_zones_type_known": _in("type", ROW_TYPES),
        "airspace_zones_only_geozones_restrict": (
            "type = 'geozone' OR restriction = 'NO_RESTRICTION'"
        ),
        "airspace_zones_restriction_known": _in("restriction", RESTRICTIONS),
        "airspace_zones_identifier_length": ("char_length(identifier) BETWEEN 1 AND 7"),
        "airspace_zones_country_alpha3": "country ~ '^[A-Z]{3}$'",
        "airspace_zones_uom_known": _in("uom_dimensions", UNITS),
        "airspace_zones_references_known": (
            f"{_in('lower_reference', REFERENCES)} "
            f"AND {_in('upper_reference', REFERENCES)}"
        ),
        "airspace_zones_vertical_band": (
            "lower_limit IS NULL OR upper_limit IS NULL "
            "OR lower_reference <> upper_reference OR lower_limit < upper_limit"
        ),
        "airspace_zones_circle": (
            "(circle_center IS NULL) = (circle_radius IS NULL) "
            "AND (circle_radius IS NULL OR circle_radius > 0)"
        ),
        "airspace_zones_published_lists": (
            "jsonb_typeof(zone_authority) = 'array' "
            "AND jsonb_typeof(applicability) = 'array' "
            "AND jsonb_array_length(applicability) >= 1 "
            "AND jsonb_typeof(ed269_extra) = 'object'"
        ),
    }
    for name, condition in checks.items():
        op.execute(
            f"ALTER TABLE airspace_zones ADD CONSTRAINT {name} CHECK ({condition})"
        )
    op.execute(
        "CREATE UNIQUE INDEX airspace_zones_identifier ON airspace_zones (identifier)"
    )
    # ARCHITECTURE.md §5: GiST on every geometry column.
    op.execute(
        "CREATE INDEX airspace_zones_circle_center_gist "
        "ON airspace_zones USING gist (circle_center)"
    )

    op.execute(
        """
        ALTER TABLE airspace_policy
            ADD COLUMN conditional_zone_severity TEXT NOT NULL DEFAULT 'warning',
            ADD CONSTRAINT airspace_policy_conditional_zone_severity_known
                CHECK (conditional_zone_severity IN ('info', 'warning'))
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE airspace_policy
            DROP CONSTRAINT airspace_policy_conditional_zone_severity_known,
            DROP COLUMN conditional_zone_severity
        """
    )
    op.execute("DROP INDEX airspace_zones_circle_center_gist")
    op.execute("DROP INDEX airspace_zones_identifier")
    for name in (
        "airspace_zones_type_known",
        "airspace_zones_only_geozones_restrict",
        "airspace_zones_restriction_known",
        "airspace_zones_identifier_length",
        "airspace_zones_country_alpha3",
        "airspace_zones_uom_known",
        "airspace_zones_references_known",
        "airspace_zones_vertical_band",
        "airspace_zones_circle",
        "airspace_zones_published_lists",
    ):
        op.execute(f"ALTER TABLE airspace_zones DROP CONSTRAINT {name}")
    op.execute(
        """
        ALTER TABLE airspace_zones
            ADD COLUMN min_alt_amsl_m DOUBLE PRECISION,
            ADD COLUMN max_alt_amsl_m DOUBLE PRECISION
        """
    )
    op.execute(
        "DELETE FROM airspace_zones "
        "WHERE type = 'geozone' AND restriction = 'NO_RESTRICTION'"
    )
    op.execute(
        """
        UPDATE airspace_zones SET
            min_alt_amsl_m = CASE WHEN lower_reference = 'AMSL' THEN
                lower_limit * CASE uom_dimensions WHEN 'FT' THEN 0.3048 ELSE 1 END
                END,
            max_alt_amsl_m = CASE WHEN upper_reference = 'AMSL' THEN
                upper_limit * CASE uom_dimensions WHEN 'FT' THEN 0.3048 ELSE 1 END
                END,
            type = CASE WHEN type <> 'geozone' THEN type
                        WHEN restriction = 'PROHIBITED' THEN 'no_fly'
                        ELSE 'restricted' END,
            name = coalesce(name, identifier)
        """
    )
    op.execute(
        f"""
        ALTER TABLE airspace_zones
            ALTER COLUMN name SET NOT NULL,
            ADD CONSTRAINT airspace_zones_type_known CHECK ({_in("type", OLD_ROW_TYPES)}),
            ADD CONSTRAINT airspace_zones_altitude_band CHECK (
                min_alt_amsl_m IS NULL OR max_alt_amsl_m IS NULL
                OR min_alt_amsl_m < max_alt_amsl_m),
            DROP COLUMN identifier,
            DROP COLUMN country,
            DROP COLUMN ed269_type,
            DROP COLUMN restriction,
            DROP COLUMN reason,
            DROP COLUMN message,
            DROP COLUMN zone_authority,
            DROP COLUMN applicability,
            DROP COLUMN uom_dimensions,
            DROP COLUMN lower_limit,
            DROP COLUMN lower_reference,
            DROP COLUMN upper_limit,
            DROP COLUMN upper_reference,
            DROP COLUMN circle_center,
            DROP COLUMN circle_radius,
            DROP COLUMN ed269_extra,
            DROP COLUMN updated_at
        """
    )

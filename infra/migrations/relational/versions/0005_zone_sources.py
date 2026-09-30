"""zone_sources, and airspace_zones that remember where they came from

Revision ID: 0005_zone_sources
Revises: 0004_height_limit
Create Date: 2026-09-30

P5-18. Zones published by an authority in EUROCAE ED-269 are imported by
`tools/import_zones.py` rather than drawn by hand.

## zone_sources

One row per imported version of a published file: which authority's source
(`name`), the file's own version (its `createdAt` or title), its SHA-256,
who imported it and when. Re-importing a source replaces its zones; the old
version's row stays, so the history of what the monitor was checking against
is kept. The change itself, which zones were added, removed or changed, is
written to `events`.

## airspace_zones, new columns

All nullable: a zone drawn by hand has no source.

- `source_id`, `external_id`: the source version and the zone's ED-269
  `identifier`, with the volume's index when the zone has several volumes.
- `restriction`, `message`, `reason`: as published, for the operator reading
  the alert.
- `min_height_agl_m`, `max_height_agl_m`: ED-269 gives each bound its own
  reference, and many zones are "from the ground to 120 m above it". An AGL
  bound is kept as AGL and checked against the DEM (P5-00), never converted
  with one ground height.
- `applicability`: when the zone is in force, as published (permanent, a
  date range, weekly periods). NULL is always.
- `source_record`: the ED-269 feature exactly as published, so a circle
  that became a polygon, or any field not modelled here, can be checked
  against the original.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID

revision: str = "0005_zone_sources"
down_revision: str | None = "0004_height_limit"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "zone_sources",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("format", sa.Text(), nullable=False),
        sa.Column("version", sa.Text(), nullable=True),
        sa.Column("file_name", sa.Text(), nullable=False),
        sa.Column("sha256", sa.Text(), nullable=False),
        sa.Column("zone_count", sa.Integer(), nullable=False),
        sa.Column("imported_by", sa.Text(), nullable=False),
        sa.Column(
            "imported_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        # The version whose zones are in airspace_zones now. One per source.
        sa.Column("current", sa.Boolean(), nullable=False),
        sa.CheckConstraint("format IN ('ED-269')", name="zone_sources_format_known"),
    )
    op.create_index(
        "zone_sources_one_current",
        "zone_sources",
        ["name"],
        unique=True,
        postgresql_where=sa.text("current"),
    )

    op.add_column(
        "airspace_zones",
        sa.Column(
            "source_id",
            UUID(as_uuid=True),
            sa.ForeignKey("zone_sources.id", ondelete="RESTRICT"),
            nullable=True,
        ),
    )
    op.add_column("airspace_zones", sa.Column("external_id", sa.Text(), nullable=True))
    op.add_column("airspace_zones", sa.Column("restriction", sa.Text(), nullable=True))
    op.add_column("airspace_zones", sa.Column("message", sa.Text(), nullable=True))
    op.add_column(
        "airspace_zones", sa.Column("reason", ARRAY(sa.Text()), nullable=True)
    )
    op.add_column(
        "airspace_zones", sa.Column("min_height_agl_m", sa.Float(), nullable=True)
    )
    op.add_column(
        "airspace_zones", sa.Column("max_height_agl_m", sa.Float(), nullable=True)
    )
    op.add_column("airspace_zones", sa.Column("applicability", JSONB(), nullable=True))
    op.add_column("airspace_zones", sa.Column("source_record", JSONB(), nullable=True))
    op.create_check_constraint(
        "airspace_zones_height_band",
        "airspace_zones",
        "min_height_agl_m IS NULL OR max_height_agl_m IS NULL "
        "OR min_height_agl_m < max_height_agl_m",
    )
    op.create_check_constraint(
        "airspace_zones_sourced_have_an_id",
        "airspace_zones",
        "source_id IS NULL OR external_id IS NOT NULL",
    )
    op.create_index(
        "airspace_zones_source_external_id",
        "airspace_zones",
        ["source_id", "external_id"],
        unique=True,
        postgresql_where=sa.text("source_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("airspace_zones_source_external_id", table_name="airspace_zones")
    op.drop_constraint(
        "airspace_zones_sourced_have_an_id", "airspace_zones", type_="check"
    )
    op.drop_constraint("airspace_zones_height_band", "airspace_zones", type_="check")
    for column in (
        "source_record",
        "applicability",
        "max_height_agl_m",
        "min_height_agl_m",
        "reason",
        "message",
        "restriction",
        "external_id",
        "source_id",
    ):
        op.drop_column("airspace_zones", column)
    op.drop_index("zone_sources_one_current", table_name="zone_sources")
    op.drop_table("zone_sources")

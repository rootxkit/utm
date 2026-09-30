"""fleet: bases, pilots, drones, airspace zones and the audit log

Revision ID: 0001_fleet
Revises:
Create Date: 2026-09-28

P2-01, the fleet part of `ARCHITECTURE.md` §4. The delivery tables of the
earlier design were never created here, and have since left the design
(P-01).

## Departures from §4, each on purpose

- **`drones.status` is not a column.** P2-05 requires status to be derived
  from telemetry freshness, not set by hand, so a stored status could only
  ever disagree with the truth. What a person does set is stored instead:
  `in_maintenance` and `retired_at`. The design's partial index on
  `status = 'IDLE'` goes with it.
- **`drones.sysid` is not a column.** Which SYSID an airframe transmits as, on
  which station, from when, is `source_bindings` in the telemetry database
  (P1-06). A second copy here would be a second answer.
- **`drones.firmware_version` is not a column.** The observed history is
  `drone_firmware` in the telemetry database (P1-11).
- **`drones.label`** is added. It is what the telemetry projection
  `known_drones` and every console show; a pilot does not know a UUID.
- **Altitudes are named for their datum** (CLAUDE.md): `min_alt_amsl_m`.
- **`events` is append-only in the database**, not by convention: a trigger
  refuses UPDATE and DELETE, and TRUNCATE is refused too. An audit log that
  can be edited by anyone with the application's credentials is a log of
  what the last editor wanted it to say.

Every geometry is SRID 4326 with a GiST index. Distances are computed on
`geography` at query time (CLAUDE.md), so the columns stay `geometry`.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "0001_fleet"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PILOT_STATUSES = ("AVAILABLE", "ON_DUTY", "OFF_DUTY")
ZONE_TYPES = ("no_fly", "restricted", "corridor", "base")


class Geometry(sa.types.UserDefinedType[Any]):
    """A PostGIS column type, spelled in SQL.

    SQLAlchemy has no PostGIS type without GeoAlchemy2, and a column type is
    not worth a dependency.
    """

    cache_ok = True

    def __init__(self, kind: str) -> None:
        self.kind = kind

    def get_col_spec(self, **kw: Any) -> str:
        return f"geometry({self.kind}, 4326)"


def _uuid_pk() -> sa.Column[Any]:
    return sa.Column(
        "id",
        UUID(as_uuid=True),
        primary_key=True,
        server_default=sa.text("gen_random_uuid()"),
    )


def _created_at() -> sa.Column[Any]:
    return sa.Column(
        "created_at",
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.text("now()"),
    )


def _in(column: str, values: Sequence[str]) -> str:
    quoted = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({quoted})"


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS postgis")

    op.create_table(
        "bases",
        _uuid_pk(),
        sa.Column("name", sa.Text(), nullable=False, unique=True),
        sa.Column("geom", Geometry("Point"), nullable=False),
        sa.Column("elevation_amsl_m", sa.Float(), nullable=True),
        sa.Column("capacity", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("charging_slots", sa.Integer(), nullable=False, server_default="0"),
        _created_at(),
        sa.CheckConstraint("capacity >= 0", name="bases_capacity_non_negative"),
        sa.CheckConstraint(
            "charging_slots >= 0", name="bases_charging_slots_non_negative"
        ),
    )
    op.execute("CREATE INDEX bases_geom_gist ON bases USING gist (geom)")

    op.create_table(
        "pilots",
        _uuid_pk(),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("license_ref", sa.Text(), nullable=True, unique=True),
        sa.Column(
            "status", sa.Text(), nullable=False, server_default=PILOT_STATUSES[2]
        ),
        sa.Column(
            "max_concurrent_drones", sa.Integer(), nullable=False, server_default="1"
        ),
        _created_at(),
        sa.CheckConstraint(_in("status", PILOT_STATUSES), name="pilots_status_known"),
        sa.CheckConstraint(
            "max_concurrent_drones >= 1", name="pilots_max_concurrent_positive"
        ),
    )

    op.create_table(
        "drones",
        _uuid_pk(),
        sa.Column("serial", sa.Text(), nullable=False, unique=True),
        sa.Column("label", sa.Text(), nullable=False, unique=True),
        sa.Column("model", sa.Text(), nullable=True),
        # Airframe parameters. Nullable: a monitored aircraft may have none on
        # record, and nothing may assume a value for one. Positive when
        # present.
        sa.Column("max_payload_g", sa.Integer(), nullable=True),
        sa.Column("max_range_m", sa.Float(), nullable=True),
        sa.Column("battery_capacity_wh", sa.Float(), nullable=True),
        sa.Column("cruise_speed_ms", sa.Float(), nullable=True),
        sa.Column("avg_power_w", sa.Float(), nullable=True),
        sa.Column(
            "home_base_id",
            UUID(as_uuid=True),
            sa.ForeignKey("bases.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column(
            "current_pilot_id",
            UUID(as_uuid=True),
            sa.ForeignKey("pilots.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column(
            "in_maintenance", sa.Boolean(), nullable=False, server_default="false"
        ),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        _created_at(),
        *[
            sa.CheckConstraint(
                f"{column} IS NULL OR {column} > 0", name=f"drones_{column}_positive"
            )
            for column in (
                "max_payload_g",
                "max_range_m",
                "battery_capacity_wh",
                "cruise_speed_ms",
                "avg_power_w",
            )
        ],
    )

    op.create_table(
        "airspace_zones",
        _uuid_pk(),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("geom", Geometry("Polygon"), nullable=False),
        sa.Column("min_alt_amsl_m", sa.Float(), nullable=True),
        sa.Column("max_alt_amsl_m", sa.Float(), nullable=True),
        _created_at(),
        sa.CheckConstraint(_in("type", ZONE_TYPES), name="airspace_zones_type_known"),
        sa.CheckConstraint(
            "min_alt_amsl_m IS NULL OR max_alt_amsl_m IS NULL "
            "OR min_alt_amsl_m < max_alt_amsl_m",
            name="airspace_zones_altitude_band",
        ),
    )
    op.execute(
        "CREATE INDEX airspace_zones_geom_gist ON airspace_zones USING gist (geom)"
    )

    op.create_table(
        "events",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column(
            "ts",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("actor_type", sa.Text(), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=True),
        sa.Column("entity_type", sa.Text(), nullable=False),
        sa.Column("entity_id", sa.Text(), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column(
            "payload", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
    )
    op.create_index("events_by_entity", "events", ["entity_type", "entity_id", "ts"])
    op.create_index("events_by_ts", "events", ["ts"])
    op.execute(
        """
        CREATE FUNCTION events_append_only() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'events is append-only: % refused', TG_OP
                USING ERRCODE = 'insufficient_privilege';
        END;
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER events_no_update_or_delete BEFORE UPDATE OR DELETE ON events "
        "FOR EACH ROW EXECUTE FUNCTION events_append_only()"
    )
    op.execute(
        "CREATE TRIGGER events_no_truncate BEFORE TRUNCATE ON events "
        "FOR EACH STATEMENT EXECUTE FUNCTION events_append_only()"
    )


def downgrade() -> None:
    op.drop_table("events")
    op.execute("DROP FUNCTION events_append_only()")
    op.drop_table("airspace_zones")
    op.drop_table("drones")
    op.drop_table("pilots")
    op.drop_table("bases")

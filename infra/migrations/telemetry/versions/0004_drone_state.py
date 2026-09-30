"""drone_state hypertable: the live telemetry the console and airspace read

Revision ID: 0004_drone_state
Revises: 0003_source_bindings
Create Date: 2026-09-23

`ARCHITECTURE.md` §4. One row per vehicle per telemetry tick, in SI, with a
`drone_id` resolved through `source_bindings` at the record's own timestamp.

## Altitudes

`alt_amsl_m` and `alt_above_home_m`, named exactly as the converter produces
them. **There is no `alt_agl_m`**, and it is absent rather than nullable:
nothing in the telemetry carries height above ground, and a nullable column
that is always null invites someone to fill it from `relative_alt`, which is
"Altitude above home". See `docs/specs/p1-02-gateway-ingest.md` §10 and P5-00.

## Every measurement is nullable, and that is not laxity

MAVLink says "unknown" in-band, and §6.4 forbids depending on any message
arriving at any rate. A missing value is a missing value: `NULL` is how that
is recorded, and it is a different fact from zero. `battery_remaining = -1`
means the autopilot does not estimate it, and storing that as 0% would put an
aircraft into a failsafe decision on a number nobody measured.

`drone_id` and `ts` are the exceptions. A row with no identity or no time is
not a degraded observation, it is an unusable one.

## Chunking and retention

7-day chunks and a retention policy driven by `telemetry_retention_days`,
which is shared with the raw archive so neither outlives the other. P1-04 owns
tuning the batching that writes these; this migration owns the shape.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0004_drone_state"
down_revision: str | None = "0003_source_bindings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 7-day chunks, per TASKS.md P1-04.
CHUNK_INTERVAL = "7 days"


def upgrade() -> None:
    op.create_table(
        "drone_state",
        # Resolved through source_bindings at the record's timestamp, never a
        # raw SYSID. No foreign key to known_drones: a hypertable's chunks make
        # one expensive, and the binding resolution has already checked it.
        sa.Column("drone_id", UUID(as_uuid=True), nullable=False),
        # The record's own capture time, from the relay's recv_utc_ns. Not the
        # time it was ingested: a replayed backlog belongs where it happened.
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        # Which station delivered it. §8 accepts two stations relaying one
        # vehicle, and the archive keeps both copies as independent
        # observations, so the row says which link it came over.
        sa.Column("station_id", sa.Text(), nullable=False),
        sa.Column(
            "geom",
            sa.Text(),  # replaced below; PostGIS type added by raw SQL
            nullable=True,
        ),
        sa.Column("alt_amsl_m", sa.Double(), nullable=True),
        # Above the HOME point, not above ground. The name says so because
        # CLAUDE.md requires the datum in the field name, and because this is
        # the field somebody will otherwise read as AGL.
        sa.Column("alt_above_home_m", sa.Double(), nullable=True),
        sa.Column("heading_deg", sa.Double(), nullable=True),
        # Ground velocity, north/east/down. Down is positive, as MAVLink sends
        # it; a sign flip here would make every climb read as a descent.
        sa.Column("vx_ms", sa.Double(), nullable=True),
        sa.Column("vy_ms", sa.Double(), nullable=True),
        sa.Column("vz_ms", sa.Double(), nullable=True),
        # Percent and energy are separate quantities, not two views of one
        # (CLAUDE.md). Percent depends on a discharge curve the autopilot
        # chose; watt-hours are what P4-03's reserve check needs.
        sa.Column("batt_pct", sa.Double(), nullable=True),
        sa.Column("batt_voltage_v", sa.Double(), nullable=True),
        sa.Column("batt_consumed_wh", sa.Double(), nullable=True),
        sa.Column("mode", sa.Text(), nullable=True),
        sa.Column("armed", sa.Boolean(), nullable=True),
        sa.Column("gps_fix_type", sa.SmallInteger(), nullable=True),
        sa.Column("sat_count", sa.SmallInteger(), nullable=True),
        sa.Column("groundspeed_ms", sa.Double(), nullable=True),
        sa.Column("climb_ms", sa.Double(), nullable=True),
        sa.CheckConstraint(
            "batt_pct IS NULL OR (batt_pct >= 0 AND batt_pct <= 100)",
            name="drone_state_batt_pct_is_a_percentage",
        ),
        # 0-359 inclusive of neither 360 nor a sentinel that slipped through:
        # UINT16_MAX in cdeg is 655.35, which this rejects outright rather
        # than storing as a heading no compass produces.
        sa.CheckConstraint(
            "heading_deg IS NULL OR (heading_deg >= 0 AND heading_deg < 360)",
            name="drone_state_heading_is_a_bearing",
        ),
    )

    # PostGIS geometry, SRID 4326, per CLAUDE.md. Added as raw SQL so the
    # column carries its real type rather than the placeholder above.
    op.execute("ALTER TABLE drone_state DROP COLUMN geom")
    op.execute("ALTER TABLE drone_state ADD COLUMN geom geometry(Point, 4326)")

    op.execute(
        f"SELECT create_hypertable('drone_state', 'ts', "
        f"chunk_time_interval => INTERVAL '{CHUNK_INTERVAL}')"
    )

    # The query the console makes constantly: this drone, most recent first.
    op.create_index(
        "drone_state_by_drone_ts",
        "drone_state",
        ["drone_id", sa.text("ts DESC")],
    )
    op.execute("CREATE INDEX drone_state_geom_gist ON drone_state USING gist (geom)")

    # Two stations relaying one vehicle produce two rows for one instant
    # (spec §8), and they are not duplicates - they are independent
    # observations over different links. What must not happen is one station
    # delivering the same record twice after a reconnect.
    op.create_index(
        "drone_state_unique_observation",
        "drone_state",
        ["drone_id", "ts", "station_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_table("drone_state")

"""remote_id_observations hypertable: Remote ID tracks, kept for replay

Revision ID: 0006_remote_id_observations
Revises: 0005_drone_firmware
Create Date: 2026-09-30

P1-15. A Remote ID aircraft on the map was, until this table, on the map
only: when it left, nothing said where it had been. For a monitoring system
that is the one question an incident asks.

## Not drone_state

`drone_state` rows are MAVLink telemetry of aircraft bound through
`source_bindings`, and everything reading it assumes that. A broadcast is a
claim by an unauthenticated transmitter about itself, heard by a receiver we
may not operate. Keeping the two apart keeps that difference visible in every
query rather than in a flag somebody forgets to filter on.

## What a row is

One observation the ingest completed: the aircraft's broadcast identity, the
position and height it claimed, and which receiver heard which transmitter.
`ts` is where the observation was placed in time, the `captured_at` it was
published with: the broadcast's own capture time when that is plausible, or
the ingest's clock when the frame arrived (S-27, gateway/remote_id.py).
Receivers are not trusted to keep time; the module's GPS clock is, within
limits. (This paragraph was revised after the migration ran; it describes,
it does not change, the schema.)

Heights are kept as broadcast and as converted, never merged:

- `alt_hae_m` is the claim, height above the WGS-84 ellipsoid;
- `alt_amsl_m` is that minus the geoid, and `geoid_model` says which model.
  It is NULL where no geoid was configured.

`payload` is the frame that completed the observation, exactly as received.
Remote ID has no raw archive of its own, and it lets the decode be checked
later.

## Keys

`(aircraft_id, ts, receiver_id)` is unique: two receivers hearing one
broadcast are two observations, as two stations are for `drone_state`.
7-day chunks, as `drone_state`.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0006_remote_id_observations"
down_revision: str | None = "0005_drone_firmware"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CHUNK_INTERVAL = "7 days"


def upgrade() -> None:
    op.create_table(
        "remote_id_observations",
        # uuid5 of the broadcast identity (gateway/remote_id.py), never a
        # registered drone_id: nothing here has been matched to our fleet.
        sa.Column("aircraft_id", UUID(as_uuid=True), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("receiver_id", sa.Text(), nullable=False),
        sa.Column("transmitter", sa.Text(), nullable=False),
        sa.Column("ua_id", sa.Text(), nullable=False),
        # Open Drone ID enumerations, stored as broadcast.
        sa.Column("id_type", sa.SmallInteger(), nullable=False),
        sa.Column("ua_type", sa.SmallInteger(), nullable=True),
        sa.Column("status", sa.SmallInteger(), nullable=True),
        sa.Column("alt_hae_m", sa.Double(), nullable=True),
        sa.Column("alt_amsl_m", sa.Double(), nullable=True),
        sa.Column("geoid_model", sa.Text(), nullable=True),
        # Only when the broadcast's height is over the take-off point.
        sa.Column("alt_above_takeoff_m", sa.Double(), nullable=True),
        sa.Column("track_deg", sa.Double(), nullable=True),
        # North, east, down, as the airspace monitor used them.
        sa.Column("vx_ms", sa.Double(), nullable=True),
        sa.Column("vy_ms", sa.Double(), nullable=True),
        sa.Column("vz_ms", sa.Double(), nullable=True),
        sa.Column("groundspeed_ms", sa.Double(), nullable=True),
        sa.Column("climb_ms", sa.Double(), nullable=True),
        sa.Column("operator_id", sa.Text(), nullable=True),
        sa.Column("rssi_dbm", sa.Double(), nullable=True),
        sa.Column("payload", sa.LargeBinary(), nullable=False),
        sa.CheckConstraint(
            "track_deg IS NULL OR (track_deg >= 0 AND track_deg < 360)",
            name="remote_id_observations_track_is_a_bearing",
        ),
        sa.CheckConstraint(
            "alt_amsl_m IS NULL OR geoid_model IS NOT NULL",
            name="remote_id_observations_amsl_names_its_geoid",
        ),
    )
    # PostGIS geometry, SRID 4326 (CLAUDE.md), as drone_state does it.
    op.execute(
        "ALTER TABLE remote_id_observations ADD COLUMN geom geometry(Point, 4326)"
    )
    op.execute(
        "ALTER TABLE remote_id_observations "
        "ADD COLUMN operator_geom geometry(Point, 4326)"
    )
    op.execute(
        f"SELECT create_hypertable('remote_id_observations', 'ts', "
        f"chunk_time_interval => INTERVAL '{CHUNK_INTERVAL}')"
    )
    op.create_index(
        "remote_id_observations_by_aircraft_ts",
        "remote_id_observations",
        ["aircraft_id", sa.text("ts DESC")],
    )
    op.execute(
        "CREATE INDEX remote_id_observations_geom_gist "
        "ON remote_id_observations USING gist (geom)"
    )
    op.create_index(
        "remote_id_observations_unique",
        "remote_id_observations",
        ["aircraft_id", "ts", "receiver_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_table("remote_id_observations")

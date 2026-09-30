"""drone_firmware: flight software per airframe, as a history

Revision ID: 0005_drone_firmware
Revises: 0004_drone_state
Create Date: 2026-09-28

P1-11. `AUTOPILOT_VERSION` as the Gateway observed it, one row each time a
drone's reported firmware **changes**. The table is a history, not a current
value. An incident investigation needs to know what an airframe ran in a
given flight, not only what it runs now.

The Gateway observes the message and never requests it. Stage 0 is
receive-only, and QGC requests the version on every connect, so a relay
attached before QGC connects sees the reply.

## Columns

- `flight_sw_version` is stored raw, alongside `version`, its decoded text.
  The raw value is the evidence. The text is a convenience that a decoding rule
  could get wrong.
- `git_hash` is `flight_custom_version`: 8 bytes, in ArduPilot the first eight
  characters of the git hash.
- `uid` is text because it is a `uint64`, and PostgreSQL's `bigint` is signed.

`observed_at` is the capture time of the first record showing this version.
`drone_id` references `known_drones`, the same projection bindings use. A
version cannot be attributed to an airframe the telemetry database was never
told about.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0005_drone_firmware"
down_revision: str | None = "0004_drone_state"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "drone_firmware",
        sa.Column(
            "drone_id",
            UUID(as_uuid=True),
            sa.ForeignKey("known_drones.drone_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("station_id", sa.Text(), nullable=False),
        sa.Column("flight_sw_version", sa.BigInteger(), nullable=False),
        sa.Column("version", sa.Text(), nullable=False),
        sa.Column("git_hash", sa.Text(), nullable=True),
        sa.Column("os_sw_version", sa.BigInteger(), nullable=True),
        sa.Column("board_version", sa.BigInteger(), nullable=True),
        sa.Column("vendor_id", sa.Integer(), nullable=True),
        sa.Column("product_id", sa.Integer(), nullable=True),
        sa.Column("uid", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("drone_id", "observed_at"),
    )


def downgrade() -> None:
    op.drop_table("drone_firmware")

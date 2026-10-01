"""The registry facts U-02 resolves a track against, projected for the Gateway

Revision ID: 0009_uas_identity_projection
Revises: 0008_archive_retention_index
Create Date: 2026-10-01

U-02: every track is resolved to registered, registered but suspended,
unknown operator or unidentified, by its serial and the operator
registration number it broadcasts. The facts that takes live in the
relational registry (U-01), which the Gateway must never reach (CLAUDE.md).
They are projected here, by the API, the way `known_drones.serial` already
is (0007), so every adapter reads them from the database it already holds.

## What is projected

- `known_drones.registration_status`: the UAS's own status (active,
  suspended, revoked), NULL for a fleet aircraft registered before U-01 or
  projected by `tools/register_aircraft.py`. A reader takes NULL as active:
  it is what the relational column defaults to.
- `known_drones.uas_operator_id`: the operator that owns it, NULL for a
  fleet aircraft with none.
- `known_uas_operators`: one row per UAS operator, its registration number
  and status. Unique on the upper-cased number, as the relational table is
  (U-01 compares registration numbers case-insensitively).

No contact details, no legal name: nothing a Remote ID resolver does not
need. No foreign key from `known_drones.uas_operator_id` to
`known_uas_operators`: it is a projection, written in whatever order the
API's resynchronisation reaches the rows, and a dangling id reads as an
operator the registry does not know, which is what it is.

The API writes these inside the relational transaction that changes the
registry, and re-projects the whole registry at start and periodically, so
a lost write is repaired (`api/uas_registry.py`, `sync_projection`).

## What a downgrade discards

The two columns and the table. They are a projection, rebuilt by the API's
next resynchronisation after an upgrade.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0009_uas_identity_projection"
down_revision: str | None = "0008_archive_retention_index"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STATUSES = "('active', 'suspended', 'revoked')"


def upgrade() -> None:
    op.add_column(
        "known_drones", sa.Column("registration_status", sa.Text(), nullable=True)
    )
    op.create_check_constraint(
        "known_drones_registration_status_valid",
        "known_drones",
        f"registration_status IS NULL OR registration_status IN {_STATUSES}",
    )
    op.add_column(
        "known_drones",
        sa.Column("uas_operator_id", UUID(as_uuid=True), nullable=True),
    )
    op.create_table(
        "known_uas_operators",
        sa.Column("operator_id", UUID(as_uuid=True), primary_key=True),
        sa.Column("registration_number", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column(
            "projected_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            f"status IN {_STATUSES}", name="known_uas_operators_status_valid"
        ),
    )
    op.create_index(
        "known_uas_operators_number_unique",
        "known_uas_operators",
        [sa.text("upper(registration_number)")],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "known_uas_operators_number_unique", table_name="known_uas_operators"
    )
    op.drop_table("known_uas_operators")
    op.drop_column("known_drones", "uas_operator_id")
    op.drop_constraint(
        "known_drones_registration_status_valid", "known_drones", type_="check"
    )
    op.drop_column("known_drones", "registration_status")

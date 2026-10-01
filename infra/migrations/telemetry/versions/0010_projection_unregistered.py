"""known_drones.registration_status may say 'unregistered'

Revision ID: 0010_projection_unregistered
Revises: 0009_uas_identity_projection
Create Date: 2026-10-01

U-02 review. `known_drones` can hold aircraft the relational registry does
not: rows written by `tools/register_aircraft.py` before the API existed,
or left by a registry that was restored without them. A NULL status reads
as active, which would call such an aircraft registered. The API's full
re-projection now marks every row with no relational `drones` row
`unregistered`, and the resolver reads that as `unknown_operator` with
reason `not_in_registry`.

A downgrade turns 'unregistered' back into NULL, which the code before this
revision reads as active.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0010_projection_unregistered"
down_revision: str | None = "0009_uas_identity_projection"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NAME = "known_drones_registration_status_valid"


def upgrade() -> None:
    op.drop_constraint(_NAME, "known_drones", type_="check")
    op.create_check_constraint(
        _NAME,
        "known_drones",
        "registration_status IS NULL OR registration_status IN "
        "('active', 'suspended', 'revoked', 'unregistered')",
    )


def downgrade() -> None:
    op.execute(
        "UPDATE known_drones SET registration_status = NULL "
        "WHERE registration_status = 'unregistered'"
    )
    op.drop_constraint(_NAME, "known_drones", type_="check")
    op.create_check_constraint(
        _NAME,
        "known_drones",
        "registration_status IS NULL OR registration_status IN "
        "('active', 'suspended', 'revoked')",
    )

"""airspace_policy: separation thresholds, one audited row

Revision ID: 0002_airspace_policy
Revises: 0001_fleet
Create Date: 2026-09-29

P5-07. The thresholds `ARCHITECTURE.md` §6.2 alerts on. Policy, not code: an
operator changes them, and the change is a row update the API records in
`events`, not a deploy.

The seeded values are §6.2's: `t_cpa < 60 s` rather than the 30 s an
automated response would allow, because a person executes the advice in QGC
and needs the reaction time; the system never commands, so that stays true.
60 m horizontal, 20 m vertical, 800 m neighbour radius.

The radius bounds how early a conflict can be seen: a pair closing at
`v` m/s enters it `800 / v` seconds before they meet. Two aircraft at 15 m/s
head-on close at 30 m/s, so they are first compared about 27 s out, inside the
60 s window. Raising `t_cpa_max_s` without the radius changes nothing for fast
pairs.

`id = 1` is enforced: there is one policy, and a second row would make "which
one applies" a question.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_airspace_policy"
down_revision: str | None = "0001_fleet"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "airspace_policy",
        sa.Column("id", sa.SmallInteger(), primary_key=True),
        sa.Column("t_cpa_max_s", sa.Float(), nullable=False),
        sa.Column("d_horizontal_min_m", sa.Float(), nullable=False),
        sa.Column("d_vertical_min_m", sa.Float(), nullable=False),
        sa.Column("neighbour_radius_m", sa.Float(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint("id = 1", name="airspace_policy_single_row"),
        sa.CheckConstraint(
            "t_cpa_max_s > 0 AND d_horizontal_min_m > 0 AND d_vertical_min_m > 0",
            name="airspace_policy_positive",
        ),
        sa.CheckConstraint(
            "neighbour_radius_m >= d_horizontal_min_m",
            name="airspace_policy_radius_covers_minimum",
        ),
    )
    op.execute(
        "INSERT INTO airspace_policy "
        "(id, t_cpa_max_s, d_horizontal_min_m, d_vertical_min_m, neighbour_radius_m) "
        "VALUES (1, 60, 60, 20, 800)"
    )


def downgrade() -> None:
    op.drop_table("airspace_policy")

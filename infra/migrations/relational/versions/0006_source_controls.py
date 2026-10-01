"""source_controls: which position sources are switched off, and by whom

Revision ID: 0006_source_controls
Revises: 0005_uas_registry
Create Date: 2026-10-01

U-15 (`ARCHITECTURE.md` §2.1). Every source can be switched off without a
deploy, by type or by instance. This table holds the current switch for
each; the history is in `events` (entity_type `source`), written in the
same transaction by the API, which is the only writer.

- `source_type` is open text, constrained to a lower-case identifier rather
  than an enum: network Remote ID providers (U-02), ADS-B feeds (U-07) and
  sensors (U-14) are further values, not a schema change.
- `instance_id` is one station, receiver, provider or feed, or `*` for the
  whole type. Never NULL, so the primary key is an ordinary one.
- A source with no row is enabled, unless the API runs with
  `SOURCES_DEFAULT_DENY` (`common/sources.py`).
- `reason` is required: a switch nobody can explain later is the one that
  is undone by the next person to notice it.

The Gateway never reads this table (CLAUDE.md). It reads the state the API
publishes on NATS (`common/sources.py`).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_source_controls"
down_revision: str | None = "0005_uas_registry"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "source_controls",
        sa.Column("source_type", sa.Text(), nullable=False),
        sa.Column("instance_id", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("changed_by", sa.Text(), nullable=False),
        sa.Column(
            "changed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("source_type", "instance_id"),
        sa.CheckConstraint(
            "source_type ~ '^[a-z][a-z0-9_]{0,31}$'",
            name="source_controls_type_shape",
        ),
        sa.CheckConstraint(
            "instance_id = '*' OR instance_id ~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$'",
            name="source_controls_instance_shape",
        ),
        sa.CheckConstraint(
            "length(btrim(reason)) > 0", name="source_controls_reason_given"
        ),
        sa.CheckConstraint(
            "length(btrim(changed_by)) > 0", name="source_controls_changed_by_given"
        ),
    )


def downgrade() -> None:
    op.drop_table("source_controls")

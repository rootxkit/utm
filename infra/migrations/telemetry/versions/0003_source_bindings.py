"""source bindings: (station_id, sysid, compid) -> drone_id, with validity

Revision ID: 0003_source_bindings
Revises: 0002_archive_retention
Create Date: 2026-09-23

`docs/specs/p1-02-gateway-ingest.md` §7. A SYSID is a flight-time address, not
an identity: it is set by a parameter, reused across airframes, and two
aircraft that never fly together may share one for years. `drone_state` stores
a `drone_id`, and it must never be inferred from an address.

## The exclusion constraint is the point

Two overlapping bindings for one `(station_id, sysid, compid)` would make
resolution ambiguous, and an ambiguous resolution attributes one airframe's
flight to another. The constraint makes that **impossible** rather than
unlikely: it is a PostgreSQL exclusion constraint over a `tstzrange`, so the
database refuses the second insert. Application code cannot be the guard here -
two concurrent writers would both check, both find nothing, and both insert.

`btree_gist` is what lets the equality parts of the key sit in a GiST index
alongside the range. It is created by `infra/initdb/timescale/01-extensions.sql`.

## Why `known_drones` exists

The fleet registry lives in the *relational* database (`ARCHITECTURE.md` §5),
and the Gateway never connects to it. A foreign key cannot cross databases, so
a binding to a drone that does not exist could only be caught by application
code - which is exactly what "a constraint violation, not a silent skip" rules
out.

`known_drones` is a **projection** of the registry: the identities the
telemetry database is permitted to attribute telemetry to, and nothing else. It
is not a copy of `drones`. Keeping it in step is the API's job when it
registers or retires an aircraft, and that synchronisation does not exist yet -
it is the cost of the Gateway's isolation, and it is a real cost, not a free
one.

Deletion is `RESTRICT`: an aircraft that has flown cannot be erased from under
its own history. Retirement is the soft path, and it blocks *new* bindings
while leaving old ones resolvable, because reading a two-year-old flight is
exactly when the airframe is most likely to be long retired.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import TSTZRANGE, UUID

revision: str = "0003_source_bindings"
down_revision: str | None = "0002_archive_retention"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "known_drones",
        # UUID because ARCHITECTURE.md §6.2's deconfliction advice is ordered
        # by drone_id ("lower drone_id maintains course"), so the identity has
        # to be totally ordered, and because the relational registry generates
        # them with uuid-ossp.
        sa.Column("drone_id", UUID(as_uuid=True), primary_key=True),
        # For a human reading an ingest_events row. Not authoritative: the
        # relational registry is.
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column(
            "registered_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        # Retired, not deleted. A retired drone's telemetry still resolves;
        # only new bindings are refused.
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_table(
        "source_bindings",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("station_id", sa.Text(), nullable=False),
        sa.Column("sysid", sa.Integer(), nullable=False),
        sa.Column("compid", sa.Integer(), nullable=False),
        sa.Column(
            "drone_id",
            UUID(as_uuid=True),
            sa.ForeignKey("known_drones.drone_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        # The single source of truth for validity. Storing bound_from and
        # bound_until separately as well would give two representations that
        # can disagree, and the exclusion constraint can only be built on one
        # of them. Queries read `lower(valid)` and `upper(valid)`.
        #
        # Default bounds are '[)': inclusive lower, exclusive upper. A record
        # exactly at the instant a binding ends belongs to the NEW binding.
        # That choice is arbitrary but it must be made once and pinned, because
        # the alternative is one record attributed to two airframes or none.
        sa.Column("valid", TSTZRANGE(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column("note", sa.Text(), nullable=False, server_default=sa.text("''")),
        # MAVLink addresses are u8. 0 is reserved for broadcast, so a binding
        # for SYSID 0 would be binding "everyone".
        sa.CheckConstraint(
            "sysid BETWEEN 1 AND 255", name="source_bindings_sysid_in_range"
        ),
        sa.CheckConstraint(
            "compid BETWEEN 1 AND 255", name="source_bindings_compid_in_range"
        ),
        sa.CheckConstraint(
            "NOT isempty(valid)", name="source_bindings_validity_non_empty"
        ),
    )

    # The whole reason this table can be trusted. Two overlapping bindings for
    # one address are refused by the database, not merely avoided by whoever
    # wrote the insert.
    op.execute(
        """
        ALTER TABLE source_bindings
        ADD CONSTRAINT source_bindings_no_overlap
        EXCLUDE USING gist (
            station_id WITH =,
            sysid WITH =,
            compid WITH =,
            valid WITH &&
        )
        """
    )

    # Resolution asks: which binding covers this address at this instant.
    op.create_index(
        "source_bindings_by_address",
        "source_bindings",
        ["station_id", "sysid", "compid"],
    )
    # "What has this drone been bound to" - for an investigation working
    # backwards from an aircraft rather than forwards from a station.
    op.create_index("source_bindings_by_drone", "source_bindings", ["drone_id"])


def downgrade() -> None:
    op.drop_index("source_bindings_by_drone", table_name="source_bindings")
    op.drop_index("source_bindings_by_address", table_name="source_bindings")
    op.drop_table("source_bindings")
    op.drop_table("known_drones")

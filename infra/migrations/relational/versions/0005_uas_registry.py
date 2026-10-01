"""UAS operators, remote pilots and UAS: the operator registry of 2019/947 Art. 14

Revision ID: 0005_uas_registry
Revises: 0004_height_limit
Create Date: 2026-10-01

U-01. The registry the regulation describes: operators with a registration
number, the remote pilots who fly for them with their competencies, and the
aircraft (UAS) they own, with serial, class label and MTOM.

## `uas_operators` is new, and is not `operators`

`operators` (0003) are the people who sign in to this console. A UAS
operator is a natural or legal person registered with the authority, who
never signs in here and may never have heard of this system. The two share a
word and nothing else, so they share no table.

`registration_number` is unique case-insensitively (an index on its upper
case), stored as given, and limited to letters and digits in the database.
Its exact shape is checked by the API against a pattern in configuration
(`UAS_OPERATOR_REGISTRATION_PATTERN`), because Georgia's format is not
confirmed yet; a schema change for a regex would be the wrong cost. The EU
number's three secret characters (after the hyphen) are refused rather than
stored.

`status` is active, suspended or revoked. Revoked is final. `valid_until` is
the instant the registration stops being valid; NULL means none recorded.
`source` says where the current values came from: typed in (`manual`) or a
`uas.gov.ge` export (`import`, `tools/import_uas_registry.py`).

## Remote pilots are `pilots`, and UAS are `drones`: extended, not duplicated

Both tables were built for our own fleet (0001). They are extended here
rather than shadowed by `remote_pilots` and `uas`, for one reason each:

- **A serial number names one aircraft, whoever owns it.** Remote ID
  matching (P1-15, and U-02 after it) resolves a broadcast serial through
  `known_drones`, the telemetry database's projection of `drones`, which is
  keyed by `drones.id` and unique on serial. A separate `uas` table would
  need a second projection path into the same `known_drones`, and the same
  serial could then be registered twice, once in each table, with nothing
  in either database to refuse it. One table keeps one uniqueness
  constraint and the projection the registry already writes.
- **A person who flies is one person.** A remote pilot of a third-party
  operator and a pilot of our fleet have the same attributes (a name, a
  certificate reference) and the same competencies. Two tables would make
  the same person two records the moment they did both.

So `drones` gains the owning operator, the class label, MTOM and a
registration status, and `pilots` gains the operator they fly for and a
registration status. Our fleet's rows keep working unchanged: every new
column is nullable or defaulted, and a drone with no operator is a fleet
aircraft registered before U-01.

`registration_status` is named so, not `status`, because both tables already
have a status that means something else: `pilots.status` is duty
(AVAILABLE, ON_DUTY, OFF_DUTY), and a drone's status is derived from
telemetry and never stored (0001). Suspension is a regulatory state, set by
the authority, and independent of both. It is also independent of
`drones.retired_at`: a suspended aircraft stays in `known_drones`, so its
broadcasts are still recognised as that aircraft - and shown as suspended
by U-02 - rather than becoming an unknown aircraft.

`class_label` is C0-C6 or NULL. NULL means no class label: a legacy or
privately built aircraft, or a fleet aircraft registered before U-01 whose
label was never recorded. MTOM is in grams, like `max_payload_g`.

## `pilot_competencies`

One row per pilot and competency (A1/A3, A2, STS-01, STS-02), with the
certificate reference and its expiry. Recording a competency again replaces
its row; the history is in `events`.

## What a downgrade discards

Everything U-01 registered, except its audit trail: `uas_operators` and
`pilot_competencies` are dropped, and `drones` and `pilots` lose their
operator, class label, MTOM and registration status. Third-party UAS and
remote pilots stay as rows in `drones` and `pilots`, where they can no longer
be told from the fleet, and UAS stay in `known_drones`. `events` keeps every
row about them. Export the registry first (`/uas`) if it is to be restored.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0005_uas_registry"
down_revision: str | None = "0004_height_limit"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

REGISTRATION_STATUSES = ("active", "suspended", "revoked")
OPERATOR_TYPES = ("natural_person", "legal_person")
SOURCES = ("manual", "import")
CLASS_LABELS = ("C0", "C1", "C2", "C3", "C4", "C5", "C6")
COMPETENCIES = ("A1_A3", "A2", "STS_01", "STS_02")


def _in(column: str, values: Sequence[str]) -> str:
    quoted = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({quoted})"


def _timestamp(name: str, *, nullable: bool = False) -> sa.Column[object]:
    return sa.Column(
        name,
        sa.DateTime(timezone=True),
        nullable=nullable,
        server_default=None if nullable else sa.text("now()"),
    )


def _operator_fk() -> sa.Column[object]:
    return sa.Column(
        "uas_operator_id",
        UUID(as_uuid=True),
        sa.ForeignKey("uas_operators.id", ondelete="RESTRICT"),
        nullable=True,
    )


def _registration_status() -> sa.Column[object]:
    return sa.Column(
        "registration_status",
        sa.Text(),
        nullable=False,
        server_default=REGISTRATION_STATUSES[0],
    )


def upgrade() -> None:
    op.create_table(
        "uas_operators",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("registration_number", sa.Text(), nullable=False),
        sa.Column("legal_name", sa.Text(), nullable=False),
        sa.Column("operator_type", sa.Text(), nullable=False),
        sa.Column("contact_email", sa.Text(), nullable=True),
        sa.Column("contact_phone", sa.Text(), nullable=True),
        sa.Column("postal_address", sa.Text(), nullable=True),
        sa.Column(
            "status", sa.Text(), nullable=False, server_default=REGISTRATION_STATUSES[0]
        ),
        _timestamp("valid_until", nullable=True),
        sa.Column("source", sa.Text(), nullable=False, server_default=SOURCES[0]),
        _timestamp("created_at"),
        _timestamp("updated_at"),
        sa.CheckConstraint(
            "registration_number ~ '^[A-Za-z0-9]+$'",
            name="uas_operators_registration_number_alphanumeric",
        ),
        sa.CheckConstraint(
            "length(btrim(legal_name)) > 0", name="uas_operators_legal_name_present"
        ),
        sa.CheckConstraint(
            _in("operator_type", OPERATOR_TYPES), name="uas_operators_type_known"
        ),
        sa.CheckConstraint(
            _in("status", REGISTRATION_STATUSES), name="uas_operators_status_known"
        ),
        sa.CheckConstraint(_in("source", SOURCES), name="uas_operators_source_known"),
    )
    op.execute(
        "CREATE UNIQUE INDEX uas_operators_registration_number_unique "
        "ON uas_operators (upper(registration_number))"
    )

    op.add_column("pilots", _operator_fk())
    op.add_column("pilots", _registration_status())
    op.create_check_constraint(
        "pilots_registration_status_known",
        "pilots",
        _in("registration_status", REGISTRATION_STATUSES),
    )
    op.create_index("pilots_by_uas_operator", "pilots", ["uas_operator_id"])

    op.create_table(
        "pilot_competencies",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column(
            "pilot_id",
            UUID(as_uuid=True),
            sa.ForeignKey("pilots.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("competency", sa.Text(), nullable=False),
        sa.Column("certificate_ref", sa.Text(), nullable=True),
        _timestamp("valid_until", nullable=True),
        _timestamp("recorded_at"),
        sa.UniqueConstraint(
            "pilot_id", "competency", name="pilot_competencies_one_per_pilot"
        ),
        sa.CheckConstraint(
            _in("competency", COMPETENCIES), name="pilot_competencies_known"
        ),
    )

    op.add_column("drones", _operator_fk())
    op.add_column("drones", sa.Column("class_label", sa.Text(), nullable=True))
    op.add_column("drones", sa.Column("mtom_g", sa.Integer(), nullable=True))
    op.add_column("drones", _registration_status())
    op.create_check_constraint(
        "drones_class_label_known",
        "drones",
        f"class_label IS NULL OR {_in('class_label', CLASS_LABELS)}",
    )
    op.create_check_constraint(
        "drones_mtom_g_positive", "drones", "mtom_g IS NULL OR mtom_g > 0"
    )
    op.create_check_constraint(
        "drones_registration_status_known",
        "drones",
        _in("registration_status", REGISTRATION_STATUSES),
    )
    op.create_index("drones_by_uas_operator", "drones", ["uas_operator_id"])


def downgrade() -> None:
    op.drop_index("drones_by_uas_operator", table_name="drones")
    for constraint in (
        "drones_registration_status_known",
        "drones_mtom_g_positive",
        "drones_class_label_known",
    ):
        op.drop_constraint(constraint, "drones", type_="check")
    for column in ("registration_status", "mtom_g", "class_label", "uas_operator_id"):
        op.drop_column("drones", column)

    op.drop_table("pilot_competencies")

    op.drop_index("pilots_by_uas_operator", table_name="pilots")
    op.drop_constraint("pilots_registration_status_known", "pilots", type_="check")
    op.drop_column("pilots", "registration_status")
    op.drop_column("pilots", "uas_operator_id")

    op.drop_table("uas_operators")

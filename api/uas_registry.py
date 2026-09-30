"""The UAS operator registry of 2019/947 Art. 14: operators, remote pilots, UAS. U-01.

Operators are `uas_operators`. Remote pilots are rows of `pilots` and UAS are
rows of `drones`, the tables the fleet registry (`api.registry`) already
owns, extended by migration 0005_uas_registry, which says why they are not
separate tables. So a UAS is audited as a `drone` and a remote pilot as a
`pilot`: one aircraft, one history.

## The rules the fleet registry already keeps

- **Every change is two writes, or none**: the row and its `events` row, in
  one transaction (`api.registry.audit`).
- **A registered UAS is projected into `known_drones`** in the telemetry
  database, inside the relational transaction, exactly as
  `FleetRegistry.register_drone` does, so Remote ID matching recognises its
  serial (P1-15).
- **A refusal carries a stable code**, never the database's own words
  (`api.registry.refused`).

## Status

Operators, remote pilots and UAS each have a registration status: active,
suspended or revoked. Suspending and reactivating go back and forth; revoking
is final, and a revoked record is refused every further transition. A change
to the status one already has is refused too (`already_suspended`), so every
status event in the log is a change.

Statuses are independent: suspending an operator does not rewrite its
aircraft's rows. A UAS is reported with its operator's status alongside its
own, and deciding what the pair means for a track is U-02's job, not this
module's.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from api.actors import SYSTEM, Actor
from api.config import DEFAULT_REGISTRATION_PATTERN
from api.registry import (
    ConflictError,
    InvalidError,
    NotFoundError,
    TelemetryProjection,
    audit,
    refused,
)
from common import get_logger
from common.uas_identity import (
    ClassLabel,
    is_cta2063,
    normalize_registration_number,
    normalize_serial,
    registration_number_problem,
    serial_problem,
)

_log = get_logger(__name__)

MAX_PAGE = 500


class RegistrationStatus(StrEnum):
    ACTIVE = "active"
    SUSPENDED = "suspended"
    REVOKED = "revoked"


class OperatorType(StrEnum):
    NATURAL_PERSON = "natural_person"
    LEGAL_PERSON = "legal_person"


class RecordSource(StrEnum):
    """Where a record's current values came from."""

    MANUAL = "manual"
    IMPORT = "import"


class Competency(StrEnum):
    """Remote pilot competencies of 2019/947: open category A1/A3 and A2,
    and the standard scenarios of the specific category."""

    A1_A3 = "A1_A3"
    A2 = "A2"
    STS_01 = "STS_01"
    STS_02 = "STS_02"


# The event a transition into each status is recorded as.
_STATUS_EVENTS = {
    RegistrationStatus.ACTIVE: "reactivated",
    RegistrationStatus.SUSPENDED: "suspended",
    RegistrationStatus.REVOKED: "revoked",
}

OPERATOR_FIELDS = frozenset(
    {
        "legal_name",
        "operator_type",
        "contact_email",
        "contact_phone",
        "postal_address",
        "valid_until",
    }
)
UAS_FIELDS = frozenset({"class_label", "mtom_g", "model", "uas_operator_id"})

_OPERATOR_COLUMNS = (
    "id, registration_number, legal_name, operator_type, contact_email, "
    "contact_phone, postal_address, status, valid_until, source, created_at, "
    "updated_at"
)
_UAS_SELECT = (
    "SELECT d.id, d.serial, d.label, d.model, d.class_label, d.mtom_g, "
    "d.uas_operator_id, o.registration_number AS operator_registration_number, "
    "o.status AS operator_status, d.registration_status, d.retired_at, "
    "d.created_at "
    "FROM drones d LEFT JOIN uas_operators o ON o.id = d.uas_operator_id"
)
_PILOT_SELECT = (
    "SELECT p.id, p.name, p.license_ref, p.uas_operator_id, "
    "o.registration_number AS operator_registration_number, "
    "o.status AS operator_status, p.registration_status, p.created_at "
    "FROM pilots p LEFT JOIN uas_operators o ON o.id = p.uas_operator_id"
)


@dataclass(frozen=True, slots=True)
class CompetencyRecord:
    competency: Competency
    certificate_ref: str | None = None
    valid_until: datetime | None = None


def check_transition(
    entity: str, name: str, current: str, target: RegistrationStatus
) -> None:
    """Refuse a status change that is no change, or that leaves `revoked`."""
    if current == target:
        raise ConflictError(
            f"{entity} {name} is already {target}", code=f"already_{target}"
        )
    if current == RegistrationStatus.REVOKED:
        raise ConflictError(
            f"{entity} {name} is revoked, which is final", code="revoked"
        )


def _aware(name: str, value: datetime | None) -> datetime | None:
    """A time with no zone is refused rather than guessed (CLAUDE.md: UTC)."""
    if value is not None and value.tzinfo is None:
        raise InvalidError(
            f"{name} must carry a zone, e.g. a trailing Z", code="naive_time"
        )
    return value


def _like(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _row(row: sa.Row[Any]) -> dict[str, Any]:
    return dict(row._mapping)


def _changes(current: Mapping[str, Any], wanted: Mapping[str, Any]) -> dict[str, Any]:
    """The fields of `wanted` that differ from `current`, as {field: {from, to}}."""
    return {
        name: {"from": current[name], "to": value}
        for name, value in wanted.items()
        if current[name] != value
    }


def _uas_out(row: dict[str, Any]) -> dict[str, Any]:
    row["serial_cta2063"] = is_cta2063(row["serial"])
    return row


@dataclass
class UasRegistry:
    engine: AsyncEngine
    projection: TelemetryProjection
    registration_pattern: re.Pattern[str] = field(
        default_factory=lambda: re.compile(DEFAULT_REGISTRATION_PATTERN)
    )

    # --- operators --------------------------------------------------------------

    def valid_registration_number(self, value: str) -> str:
        number = normalize_registration_number(value)
        problem = registration_number_problem(number, self.registration_pattern)
        if problem is not None:
            raise InvalidError(
                f"registration number {number!r} refused: {problem}",
                code="invalid_registration_number",
            )
        return number

    async def create_operator(
        self,
        *,
        registration_number: str,
        legal_name: str,
        operator_type: OperatorType,
        contact_email: str | None = None,
        contact_phone: str | None = None,
        postal_address: str | None = None,
        valid_until: datetime | None = None,
        status: RegistrationStatus = RegistrationStatus.ACTIVE,
        source: RecordSource = RecordSource.MANUAL,
        actor: Actor = SYSTEM,
    ) -> dict[str, Any]:
        number = self.valid_registration_number(registration_number)
        if not legal_name.strip():
            raise InvalidError("a legal name is required", code="invalid_value")
        try:
            async with self.engine.begin() as connection:
                created = (
                    await connection.execute(
                        sa.text(
                            "INSERT INTO uas_operators (registration_number, "
                            " legal_name, operator_type, contact_email, contact_phone, "
                            " postal_address, valid_until, status, source) "
                            "VALUES (:number, :legal_name, :operator_type, :email, "
                            " :phone, :address, :valid_until, :status, :source) "
                            f"RETURNING {_OPERATOR_COLUMNS}"
                        ),
                        {
                            "number": number,
                            "legal_name": legal_name.strip(),
                            "operator_type": OperatorType(operator_type).value,
                            "email": contact_email,
                            "phone": contact_phone,
                            "address": postal_address,
                            "valid_until": _aware("valid_until", valid_until),
                            "status": RegistrationStatus(status).value,
                            "source": RecordSource(source).value,
                        },
                    )
                ).one()
                operator = _row(created)
                await audit(
                    connection,
                    "uas_operator",
                    operator["id"],
                    "registered",
                    operator,
                    actor=actor,
                )
        except IntegrityError as error:
            raise refused("UAS operator", number, error) from error
        _log.info(
            "UAS operator registered",
            extra={"uas_operator_id": str(operator["id"]), "registration": number},
        )
        return operator

    async def find_operator(self, registration_number: str) -> dict[str, Any] | None:
        """By registration number, case-insensitively; None when unknown."""
        number = normalize_registration_number(registration_number)
        async with self.engine.connect() as connection:
            found = (
                await connection.execute(
                    sa.text(
                        f"SELECT {_OPERATOR_COLUMNS} FROM uas_operators "
                        "WHERE upper(registration_number) = upper(:number)"
                    ),
                    {"number": number},
                )
            ).one_or_none()
        return None if found is None else _row(found)

    async def operator_by_registration(
        self, registration_number: str
    ) -> dict[str, Any]:
        found = await self.find_operator(registration_number)
        if found is None:
            raise NotFoundError(
                f"no UAS operator with registration number {registration_number!r}"
            )
        return found

    async def get_operator(self, operator_id: UUID) -> dict[str, Any]:
        async with self.engine.connect() as connection:
            found = (
                await connection.execute(
                    sa.text(
                        f"SELECT {_OPERATOR_COLUMNS} FROM uas_operators WHERE id = :id"
                    ),
                    {"id": str(operator_id)},
                )
            ).one_or_none()
        if found is None:
            raise NotFoundError(f"no UAS operator {operator_id}")
        return _row(found)

    async def list_operators(
        self,
        *,
        status: RegistrationStatus | None = None,
        source: RecordSource | None = None,
        query: str | None = None,
        limit: int = MAX_PAGE,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Filtered; `query` matches the registration number or the legal name."""
        clauses: list[str] = []
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if status is not None:
            clauses.append("status = :status")
            params["status"] = status.value
        if source is not None:
            clauses.append("source = :source")
            params["source"] = source.value
        if query:
            clauses.append("(registration_number ILIKE :q OR legal_name ILIKE :q)")
            params["q"] = _like(query.strip())
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        async with self.engine.connect() as connection:
            rows = await connection.execute(
                sa.text(
                    f"SELECT {_OPERATOR_COLUMNS} FROM uas_operators {where} "
                    "ORDER BY registration_number LIMIT :limit OFFSET :offset"
                ),
                params,
            )
            return [_row(row) for row in rows]

    async def update_operator(
        self,
        operator_id: UUID,
        changes: Mapping[str, Any],
        *,
        source: RecordSource = RecordSource.MANUAL,
        actor: Actor = SYSTEM,
    ) -> dict[str, Any]:
        """Change some of `OPERATOR_FIELDS`. Nothing different: nothing written.

        The registration number is the operator's identity and never changes;
        a different number is a different registration.
        """
        unknown = set(changes) - OPERATOR_FIELDS
        if unknown:
            raise InvalidError(
                f"cannot change {sorted(unknown)} of a UAS operator",
                code="not_changeable",
            )
        wanted = dict(changes)
        if "legal_name" in wanted:
            name = (wanted["legal_name"] or "").strip()
            if not name:
                raise InvalidError("a legal name is required", code="invalid_value")
            wanted["legal_name"] = name
        if "operator_type" in wanted:
            if wanted["operator_type"] is None:
                raise InvalidError("an operator type is required", code="invalid_value")
            wanted["operator_type"] = OperatorType(wanted["operator_type"]).value
        if "valid_until" in wanted:
            _aware("valid_until", wanted["valid_until"])
        try:
            async with self.engine.begin() as connection:
                current = await self._lock_operator(connection, operator_id)
                diff = _changes(current, wanted)
                if not diff:
                    return current
                assignments = ", ".join(f"{name} = :{name}" for name in diff)
                updated = (
                    await connection.execute(
                        sa.text(
                            f"UPDATE uas_operators SET {assignments}, "
                            "source = :source, updated_at = now() WHERE id = :id "
                            f"RETURNING {_OPERATOR_COLUMNS}"
                        ),
                        {
                            **{name: wanted[name] for name in diff},
                            "source": RecordSource(source).value,
                            "id": str(operator_id),
                        },
                    )
                ).one()
                await audit(
                    connection,
                    "uas_operator",
                    operator_id,
                    "updated",
                    {"changes": diff, "source": source},
                    actor=actor,
                )
                return _row(updated)
        except IntegrityError as error:
            raise refused("UAS operator", str(operator_id), error) from error

    async def set_operator_status(
        self,
        operator_id: UUID,
        status: RegistrationStatus,
        *,
        reason: str | None = None,
        actor: Actor = SYSTEM,
    ) -> dict[str, Any]:
        async with self.engine.begin() as connection:
            current = await self._lock_operator(connection, operator_id)
            check_transition(
                "UAS operator",
                current["registration_number"],
                current["status"],
                status,
            )
            updated = (
                await connection.execute(
                    sa.text(
                        "UPDATE uas_operators SET status = :status, "
                        "updated_at = now() WHERE id = :id "
                        f"RETURNING {_OPERATOR_COLUMNS}"
                    ),
                    {"status": status.value, "id": str(operator_id)},
                )
            ).one()
            await audit(
                connection,
                "uas_operator",
                operator_id,
                _STATUS_EVENTS[status],
                {"from": current["status"], "to": status, "reason": reason},
                actor=actor,
            )
        _log.info(
            "UAS operator status changed",
            extra={"uas_operator_id": str(operator_id), "status": status.value},
        )
        return _row(updated)

    async def _lock_operator(
        self, connection: AsyncConnection, operator_id: UUID
    ) -> dict[str, Any]:
        found = (
            await connection.execute(
                sa.text(
                    f"SELECT {_OPERATOR_COLUMNS} FROM uas_operators "
                    "WHERE id = :id FOR UPDATE"
                ),
                {"id": str(operator_id)},
            )
        ).one_or_none()
        if found is None:
            raise NotFoundError(f"no UAS operator {operator_id}")
        return _row(found)

    async def _refuse_revoked_operator(
        self, connection: AsyncConnection, operator_id: UUID | None
    ) -> None:
        """Nothing new is attached to a revoked operator. An unknown one is
        left to the foreign key, which refuses it as `unknown_reference`."""
        if operator_id is None:
            return
        status = (
            await connection.execute(
                sa.text("SELECT status FROM uas_operators WHERE id = :id"),
                {"id": str(operator_id)},
            )
        ).scalar_one_or_none()
        if status == RegistrationStatus.REVOKED:
            raise ConflictError(
                f"UAS operator {operator_id} is revoked", code="operator_revoked"
            )

    # --- remote pilots ------------------------------------------------------------

    async def create_remote_pilot(
        self,
        *,
        name: str,
        license_ref: str | None,
        uas_operator_id: UUID,
        competencies: Sequence[CompetencyRecord] = (),
        actor: Actor = SYSTEM,
    ) -> dict[str, Any]:
        if not name.strip():
            raise InvalidError("a name is required", code="invalid_value")
        for record in competencies:
            _aware("valid_until", record.valid_until)
        try:
            async with self.engine.begin() as connection:
                await self._refuse_revoked_operator(connection, uas_operator_id)
                pilot_id: UUID = (
                    await connection.execute(
                        sa.text(
                            "INSERT INTO pilots (name, license_ref, uas_operator_id) "
                            "VALUES (:name, :license_ref, :operator) RETURNING id"
                        ),
                        {
                            "name": name.strip(),
                            "license_ref": license_ref,
                            "operator": str(uas_operator_id),
                        },
                    )
                ).scalar_one()
                for record in competencies:
                    await self._record_competency(connection, pilot_id, record)
                pilot = await self._pilot(connection, pilot_id)
                await audit(
                    connection, "pilot", pilot_id, "registered", pilot, actor=actor
                )
        except IntegrityError as error:
            raise refused("remote pilot", name, error) from error
        return pilot

    async def get_remote_pilot(self, pilot_id: UUID) -> dict[str, Any]:
        async with self.engine.connect() as connection:
            return await self._pilot(connection, pilot_id)

    async def list_remote_pilots(
        self,
        *,
        uas_operator_id: UUID | None = None,
        status: RegistrationStatus | None = None,
        query: str | None = None,
        limit: int = MAX_PAGE,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Filtered; `query` matches the name or the certificate reference."""
        clauses: list[str] = []
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if uas_operator_id is not None:
            clauses.append("p.uas_operator_id = :operator")
            params["operator"] = str(uas_operator_id)
        if status is not None:
            clauses.append("p.registration_status = :status")
            params["status"] = status.value
        if query:
            clauses.append("(p.name ILIKE :q OR p.license_ref ILIKE :q)")
            params["q"] = _like(query.strip())
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        async with self.engine.connect() as connection:
            rows = await connection.execute(
                sa.text(
                    f"{_PILOT_SELECT} {where} ORDER BY p.name, p.id "
                    "LIMIT :limit OFFSET :offset"
                ),
                params,
            )
            pilots = [_row(row) for row in rows]
            await self._attach_competencies(connection, pilots)
        return pilots

    async def record_competency(
        self, pilot_id: UUID, record: CompetencyRecord, *, actor: Actor = SYSTEM
    ) -> dict[str, Any]:
        """Record or replace one competency. The previous one is in `events`."""
        _aware("valid_until", record.valid_until)
        async with self.engine.begin() as connection:
            await self._lock_pilot(connection, pilot_id)
            await self._record_competency(connection, pilot_id, record)
            await audit(
                connection,
                "pilot",
                pilot_id,
                "competency_recorded",
                {
                    "competency": record.competency,
                    "certificate_ref": record.certificate_ref,
                    "valid_until": record.valid_until,
                },
                actor=actor,
            )
            return await self._pilot(connection, pilot_id)

    async def set_pilot_status(
        self,
        pilot_id: UUID,
        status: RegistrationStatus,
        *,
        reason: str | None = None,
        actor: Actor = SYSTEM,
    ) -> dict[str, Any]:
        async with self.engine.begin() as connection:
            current = await self._lock_pilot(connection, pilot_id)
            check_transition(
                "remote pilot", current["name"], current["registration_status"], status
            )
            await connection.execute(
                sa.text(
                    "UPDATE pilots SET registration_status = :status WHERE id = :id"
                ),
                {"status": status.value, "id": str(pilot_id)},
            )
            await audit(
                connection,
                "pilot",
                pilot_id,
                _STATUS_EVENTS[status],
                {
                    "from": current["registration_status"],
                    "to": status,
                    "reason": reason,
                },
                actor=actor,
            )
            return await self._pilot(connection, pilot_id)

    async def _lock_pilot(
        self, connection: AsyncConnection, pilot_id: UUID
    ) -> dict[str, Any]:
        found = (
            await connection.execute(
                sa.text(
                    "SELECT name, registration_status FROM pilots "
                    "WHERE id = :id FOR UPDATE"
                ),
                {"id": str(pilot_id)},
            )
        ).one_or_none()
        if found is None:
            raise NotFoundError(f"no pilot {pilot_id}")
        return _row(found)

    async def _record_competency(
        self, connection: AsyncConnection, pilot_id: UUID, record: CompetencyRecord
    ) -> None:
        await connection.execute(
            sa.text(
                "INSERT INTO pilot_competencies "
                "(pilot_id, competency, certificate_ref, valid_until) "
                "VALUES (:pilot, :competency, :certificate, :valid_until) "
                "ON CONFLICT (pilot_id, competency) DO UPDATE SET "
                "certificate_ref = EXCLUDED.certificate_ref, "
                "valid_until = EXCLUDED.valid_until, recorded_at = now()"
            ),
            {
                "pilot": str(pilot_id),
                "competency": Competency(record.competency).value,
                "certificate": record.certificate_ref,
                "valid_until": record.valid_until,
            },
        )

    async def _pilot(
        self, connection: AsyncConnection, pilot_id: UUID
    ) -> dict[str, Any]:
        found = (
            await connection.execute(
                sa.text(f"{_PILOT_SELECT} WHERE p.id = :id"), {"id": str(pilot_id)}
            )
        ).one_or_none()
        if found is None:
            raise NotFoundError(f"no pilot {pilot_id}")
        pilot = _row(found)
        await self._attach_competencies(connection, [pilot])
        return pilot

    async def _attach_competencies(
        self, connection: AsyncConnection, pilots: list[dict[str, Any]]
    ) -> None:
        by_pilot: dict[UUID, list[dict[str, Any]]] = {p["id"]: [] for p in pilots}
        if by_pilot:
            rows = await connection.execute(
                sa.text(
                    "SELECT pilot_id, competency, certificate_ref, valid_until, "
                    "recorded_at FROM pilot_competencies "
                    "WHERE pilot_id = ANY(:ids) ORDER BY competency"
                ),
                {"ids": list(by_pilot)},
            )
            for row in rows:
                record = _row(row)
                by_pilot[record.pop("pilot_id")].append(record)
        for pilot in pilots:
            pilot["competencies"] = by_pilot[pilot["id"]]

    # --- UAS ----------------------------------------------------------------------

    def valid_serial(self, serial: str, class_label: ClassLabel | None) -> str:
        normalized = normalize_serial(serial)
        problem = serial_problem(normalized, class_label)
        if problem is not None:
            raise InvalidError(
                f"serial {normalized!r} refused: {problem}", code="invalid_serial"
            )
        return normalized

    async def register_uas(
        self,
        *,
        serial: str,
        class_label: ClassLabel | None,
        mtom_g: int,
        uas_operator_id: UUID,
        model: str | None = None,
        label: str | None = None,
        status: RegistrationStatus = RegistrationStatus.ACTIVE,
        actor: Actor = SYSTEM,
    ) -> dict[str, Any]:
        """Register a UAS and project it into `known_drones`, like a fleet drone.

        `label` is what consoles show; the serial when not given.
        """
        label_class = None if class_label is None else ClassLabel(class_label)
        normalized = self.valid_serial(serial, label_class)
        shown = (label or "").strip() or normalized
        if mtom_g <= 0:
            raise InvalidError("MTOM must be positive", code="invalid_value")
        try:
            async with self.engine.begin() as connection:
                await self._refuse_revoked_operator(connection, uas_operator_id)
                drone_id: UUID = (
                    await connection.execute(
                        sa.text(
                            "INSERT INTO drones (serial, label, model, class_label, "
                            " mtom_g, uas_operator_id, registration_status) "
                            "VALUES (:serial, :label, :model, :class_label, :mtom_g, "
                            " :operator, :status) RETURNING id"
                        ),
                        {
                            "serial": normalized,
                            "label": shown,
                            "model": model,
                            "class_label": None
                            if label_class is None
                            else label_class.value,
                            "mtom_g": mtom_g,
                            "operator": str(uas_operator_id),
                            "status": RegistrationStatus(status).value,
                        },
                    )
                ).scalar_one()
                uas = await self._uas(connection, drone_id)
                await audit(
                    connection, "drone", drone_id, "registered", uas, actor=actor
                )
                # Last, and inside the transaction: see `api.registry`.
                await self.projection.register_drone(drone_id, shown, serial=normalized)
        except IntegrityError as error:
            raise refused("UAS", normalized, error) from error
        _log.info(
            "UAS registered",
            extra={"drone_id": str(drone_id), "uas_operator_id": str(uas_operator_id)},
        )
        return uas

    async def get_uas(self, drone_id: UUID) -> dict[str, Any]:
        async with self.engine.connect() as connection:
            return await self._uas(connection, drone_id)

    async def find_uas(self, serial: str) -> dict[str, Any] | None:
        """By serial. An exact match wins; otherwise a match ignoring case,
        when there is exactly one. None when there is none."""
        normalized = normalize_serial(serial)
        async with self.engine.connect() as connection:
            rows = (
                await connection.execute(
                    sa.text(
                        f"{_UAS_SELECT} WHERE upper(d.serial) = upper(:serial) "
                        "ORDER BY (d.serial = :serial) DESC LIMIT 2"
                    ),
                    {"serial": normalized},
                )
            ).all()
        if not rows:
            return None
        first = _row(rows[0])
        if first["serial"] == normalized or len(rows) == 1:
            return _uas_out(first)
        return None

    async def uas_by_serial(self, serial: str) -> dict[str, Any]:
        found = await self.find_uas(serial)
        if found is None:
            raise NotFoundError(f"no UAS with serial {serial!r}")
        return found

    async def list_uas(
        self,
        *,
        uas_operator_id: UUID | None = None,
        status: RegistrationStatus | None = None,
        class_label: ClassLabel | None = None,
        query: str | None = None,
        include_fleet: bool = False,
        limit: int = MAX_PAGE,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Filtered; `query` matches serial, label or model.

        Fleet aircraft with no operator are left out unless `include_fleet`.
        """
        clauses: list[str] = []
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if not include_fleet:
            clauses.append("d.uas_operator_id IS NOT NULL")
        if uas_operator_id is not None:
            clauses.append("d.uas_operator_id = :operator")
            params["operator"] = str(uas_operator_id)
        if status is not None:
            clauses.append("d.registration_status = :status")
            params["status"] = status.value
        if class_label is not None:
            clauses.append("d.class_label = :class_label")
            params["class_label"] = class_label.value
        if query:
            clauses.append(
                "(d.serial ILIKE :q OR d.label ILIKE :q OR d.model ILIKE :q)"
            )
            params["q"] = _like(query.strip())
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        async with self.engine.connect() as connection:
            rows = await connection.execute(
                sa.text(
                    f"{_UAS_SELECT} {where} ORDER BY d.serial "
                    "LIMIT :limit OFFSET :offset"
                ),
                params,
            )
            return [_uas_out(_row(row)) for row in rows]

    async def update_uas(
        self,
        drone_id: UUID,
        changes: Mapping[str, Any],
        *,
        actor: Actor = SYSTEM,
    ) -> dict[str, Any]:
        """Change some of `UAS_FIELDS`. Nothing different: nothing written.

        The serial is the aircraft's identity and never changes. A new class
        label is checked against it, as at registration.
        """
        unknown = set(changes) - UAS_FIELDS
        if unknown:
            raise InvalidError(
                f"cannot change {sorted(unknown)} of a UAS", code="not_changeable"
            )
        wanted = dict(changes)
        if "mtom_g" in wanted and (wanted["mtom_g"] is None or wanted["mtom_g"] <= 0):
            raise InvalidError("MTOM must be positive", code="invalid_value")
        if "uas_operator_id" in wanted and wanted["uas_operator_id"] is None:
            raise InvalidError(
                "a UAS cannot be left without an operator", code="invalid_value"
            )
        if wanted.get("class_label") is not None:
            wanted["class_label"] = ClassLabel(wanted["class_label"]).value
        try:
            async with self.engine.begin() as connection:
                current = await self._lock_uas(connection, drone_id)
                diff = _changes(current, wanted)
                if not diff:
                    return await self._uas(connection, drone_id)
                if "class_label" in diff:
                    new_class = wanted["class_label"]
                    self.valid_serial(
                        current["serial"],
                        None if new_class is None else ClassLabel(new_class),
                    )
                if "uas_operator_id" in diff:
                    await self._refuse_revoked_operator(
                        connection, wanted["uas_operator_id"]
                    )
                assignments = ", ".join(f"{name} = :{name}" for name in diff)
                await connection.execute(
                    sa.text(f"UPDATE drones SET {assignments} WHERE id = :id"),
                    {
                        **{
                            name: (str(value) if isinstance(value, UUID) else value)
                            for name, value in ((n, wanted[n]) for n in diff)
                        },
                        "id": str(drone_id),
                    },
                )
                await audit(
                    connection,
                    "drone",
                    drone_id,
                    "updated",
                    {"changes": diff},
                    actor=actor,
                )
                return await self._uas(connection, drone_id)
        except IntegrityError as error:
            raise refused("UAS", str(drone_id), error) from error

    async def set_uas_status(
        self,
        drone_id: UUID,
        status: RegistrationStatus,
        *,
        reason: str | None = None,
        actor: Actor = SYSTEM,
    ) -> dict[str, Any]:
        """Suspend, reactivate or revoke. `known_drones` is left as it is, so
        a suspended aircraft's broadcasts are still recognised as it
        (migration 0005_uas_registry)."""
        async with self.engine.begin() as connection:
            current = await self._lock_uas(connection, drone_id)
            check_transition(
                "UAS", current["serial"], current["registration_status"], status
            )
            await connection.execute(
                sa.text(
                    "UPDATE drones SET registration_status = :status WHERE id = :id"
                ),
                {"status": status.value, "id": str(drone_id)},
            )
            await audit(
                connection,
                "drone",
                drone_id,
                _STATUS_EVENTS[status],
                {
                    "from": current["registration_status"],
                    "to": status,
                    "reason": reason,
                },
                actor=actor,
            )
            uas = await self._uas(connection, drone_id)
        _log.info(
            "UAS status changed",
            extra={"drone_id": str(drone_id), "status": status.value},
        )
        return uas

    async def _lock_uas(
        self, connection: AsyncConnection, drone_id: UUID
    ) -> dict[str, Any]:
        found = (
            await connection.execute(
                sa.text(
                    "SELECT serial, class_label, mtom_g, model, uas_operator_id, "
                    "registration_status FROM drones WHERE id = :id FOR UPDATE"
                ),
                {"id": str(drone_id)},
            )
        ).one_or_none()
        if found is None:
            raise NotFoundError(f"no UAS {drone_id}")
        return _row(found)

    async def _uas(self, connection: AsyncConnection, drone_id: UUID) -> dict[str, Any]:
        found = (
            await connection.execute(
                sa.text(f"{_UAS_SELECT} WHERE d.id = :id"), {"id": str(drone_id)}
            )
        ).one_or_none()
        if found is None:
            raise NotFoundError(f"no UAS {drone_id}")
        return _uas_out(_row(found))

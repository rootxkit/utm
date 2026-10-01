"""The registry facts identification needs, in the telemetry database. U-02.

The UAS operator registry (U-01) lives in the relational database, which no
adapter may reach (CLAUDE.md). What resolving a track takes from it is small
and is projected here by the API, the one writer of the registry:

- per aircraft (`known_drones`): its serial, label, registration status and
  owning operator;
- per operator (`known_uas_operators`): its registration number and status.

Telemetry migration `0009_uas_identity_projection` says why these columns
and why no foreign key.

## Why the telemetry database, and not a NATS bucket like U-15

U-15 publishes the source switches as one key of a JetStream bucket. That
fits a small state that must act within a round trip. The registry is
neither: it grows with every aircraft registered in the country, past the
size one bucket value can hold (NATS' 1 MB default payload), and a status
change applied within a few seconds is enough. The telemetry database is
already every adapter's read path for identity (`known_drones` for serials
and bindings), the API already writes `known_drones` inside the
relational transaction that changes the registry, and a projection there
can be joined by replay and by U-12's evidence packs.

## Write side (the API)

`IdentityProjection` updates one operator or one aircraft as it changes,
inside the relational transaction, and `replace_all` re-projects the whole
registry, which the API runs at start and periodically
(`api/uas_registry.py`). A lost write is therefore repaired within one
resynchronisation interval.

## Read side (the adapters)

`load_snapshot` reads everything into a `RegistrySnapshot`, and
`RegistryFollower` re-reads it every `REGISTRY_REFRESH_S` (5 s by default),
so a registry change reaches every resolver within that plus the API's
transaction. A failed read keeps the snapshot held: a database hiccup must
not turn every registered aircraft unknown. A retired aircraft is left
out: its serial no longer names a registered aircraft.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from common import get_logger
from common.uas_identity import (
    RegistrationStatus,
    normalize_serial,
    public_registration_number,
)

_log = get_logger(__name__)

DEFAULT_REFRESH_S = 5.0


def operator_key(registration_number: str) -> str:
    """How a registration number is compared: its public part (the EU
    secret suffix stripped), upper case (U-01)."""
    return public_registration_number(registration_number).upper()


@dataclass(frozen=True, slots=True)
class UasFacts:
    drone_id: UUID
    label: str
    serial: str | None
    # NULL in the projection (a fleet aircraft from before U-01) reads as
    # active: what the relational column defaults to.
    registration_status: RegistrationStatus
    uas_operator_id: UUID | None
    # False for a `known_drones` row the relational registry has no aircraft
    # for (`unregistered` in the projection, migration 0010).
    in_registry: bool = True


# What the projection says of a `known_drones` row with no relational
# `drones` row (migration 0010).
UNREGISTERED = "unregistered"


@dataclass(frozen=True, slots=True)
class OperatorFacts:
    operator_id: UUID
    registration_number: str
    status: RegistrationStatus


@dataclass(frozen=True)
class RegistrySnapshot:
    """The registry as one read of the projection saw it."""

    uas: tuple[UasFacts, ...] = ()
    operators: tuple[OperatorFacts, ...] = ()
    by_serial: dict[str, UasFacts] = field(init=False, repr=False, compare=False)
    by_serial_folded: dict[str, list[UasFacts]] = field(
        init=False, repr=False, compare=False
    )
    by_drone_id: dict[UUID, UasFacts] = field(init=False, repr=False, compare=False)
    operators_by_id: dict[UUID, OperatorFacts] = field(
        init=False, repr=False, compare=False
    )
    operators_by_key: dict[str, OperatorFacts] = field(
        init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        by_serial: dict[str, UasFacts] = {}
        folded: dict[str, list[UasFacts]] = {}
        for facts in self.uas:
            if facts.serial:
                by_serial[facts.serial] = facts
                folded.setdefault(facts.serial.upper(), []).append(facts)
        object.__setattr__(self, "by_serial", by_serial)
        object.__setattr__(self, "by_serial_folded", folded)
        object.__setattr__(self, "by_drone_id", {f.drone_id: f for f in self.uas})
        object.__setattr__(
            self, "operators_by_id", {o.operator_id: o for o in self.operators}
        )
        object.__setattr__(
            self,
            "operators_by_key",
            {operator_key(o.registration_number): o for o in self.operators},
        )

    def find_uas(self, serial: str) -> UasFacts | None:
        """By serial, as U-01's `find_uas` does: an exact match wins, else a
        match ignoring case when there is exactly one."""
        normalized = normalize_serial(serial)
        exact = self.by_serial.get(normalized)
        if exact is not None:
            return exact
        candidates = self.by_serial_folded.get(normalized.upper(), [])
        return candidates[0] if len(candidates) == 1 else None

    def find_operator(self, registration_number: str) -> OperatorFacts | None:
        return self.operators_by_key.get(operator_key(registration_number))


def _status(value: object) -> RegistrationStatus:
    return (
        RegistrationStatus.ACTIVE if value is None else RegistrationStatus(str(value))
    )


_UAS = sa.text(
    "SELECT drone_id, label, serial, registration_status, uas_operator_id "
    "FROM known_drones WHERE retired_at IS NULL"
)
_OPERATORS = sa.text(
    "SELECT operator_id, registration_number, status FROM known_uas_operators"
)


async def load_snapshot(engine: AsyncEngine) -> RegistrySnapshot:
    async with engine.connect() as connection:
        uas_rows = (await connection.execute(_UAS)).all()
        operator_rows = (await connection.execute(_OPERATORS)).all()
    return RegistrySnapshot(
        uas=tuple(
            UasFacts(
                drone_id=row.drone_id,
                label=str(row.label),
                serial=None if row.serial is None else str(row.serial),
                registration_status=_status(
                    None
                    if row.registration_status == UNREGISTERED
                    else row.registration_status
                ),
                uas_operator_id=row.uas_operator_id,
                in_registry=row.registration_status != UNREGISTERED,
            )
            for row in uas_rows
        ),
        operators=tuple(
            OperatorFacts(
                operator_id=row.operator_id,
                registration_number=str(row.registration_number),
                status=_status(row.status),
            )
            for row in operator_rows
        ),
    )


@dataclass
class RegistryFollower:
    """Keeps a `RegistrySnapshot` current in one adapter process."""

    engine: AsyncEngine
    refresh_s: float = DEFAULT_REFRESH_S
    clock_s: Callable[[], float] = time.monotonic
    snapshot: RegistrySnapshot = field(default_factory=RegistrySnapshot)
    # Called with each new snapshot, e.g. to update the fleet serials.
    on_change: Callable[[RegistrySnapshot], None] | None = None
    # False until a read succeeds: every serial is unknown until then.
    loaded: bool = field(default=False, init=False)
    reads: int = field(default=0, init=False)
    read_failures: int = field(default=0, init=False)
    # When the snapshot held was read, on `clock_s`; None before any.
    read_at_s: float | None = field(default=None, init=False)

    async def refresh(self) -> bool:
        """Read the projection. False when it could not be read; the
        snapshot held is kept, and the failure logged."""
        try:
            snapshot = await load_snapshot(self.engine)
        except (SQLAlchemyError, OSError) as error:
            self.read_failures += 1
            _log.error(
                "could not read the registry projection; keeping what is held",
                extra={
                    "error": repr(error),
                    "read_failures": self.read_failures,
                    "loaded": self.loaded,
                },
            )
            return False
        self.snapshot = snapshot
        self.loaded = True
        self.reads += 1
        self.read_at_s = self.clock_s()
        if self.on_change is not None:
            self.on_change(snapshot)
        return True

    async def run(self, stop: asyncio.Event) -> None:
        """Re-read every `refresh_s` until `stop`."""
        while not stop.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), self.refresh_s)
            if stop.is_set():
                return
            await self.refresh()

    def status(self) -> dict[str, int]:
        age_s = None if self.read_at_s is None else self.clock_s() - self.read_at_s
        return {
            "registry_loaded": int(self.loaded),
            "registry_uas": len(self.snapshot.uas),
            "registry_operators": len(self.snapshot.operators),
            "registry_read_failures": self.read_failures,
            "registry_age_s": -1 if age_s is None else round(age_s),
        }


# --- write side (the API) -----------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProjectedUas:
    drone_id: UUID
    registration_status: str
    uas_operator_id: UUID | None


@dataclass(frozen=True, slots=True)
class ProjectedOperator:
    operator_id: UUID
    registration_number: str
    status: str


@dataclass
class IdentityProjection:
    """Writes the projection. The API is its only caller."""

    engine: AsyncEngine

    async def project_operator(self, operator: ProjectedOperator) -> None:
        async with self.engine.begin() as connection:
            await self._upsert_operators(connection, [operator])

    async def project_uas(self, uas: ProjectedUas) -> None:
        """The aircraft's status and owner. Its `known_drones` row is made
        by `BindingResolver.register_drone`; one not there yet is left for
        the next `replace_all`."""
        async with self.engine.begin() as connection:
            await self._update_uas(connection, [uas])

    async def replace_all(
        self,
        operators: Iterable[ProjectedOperator],
        uas: Iterable[ProjectedUas],
    ) -> tuple[int, int]:
        """Make the projection say exactly what the registry says. Returns
        (operator rows written, aircraft rows changed). Operators no longer
        in the registry are deleted. Aircraft rows are updated, never
        deleted (`known_drones` is the fleet projection too, and bindings
        and history refer to it): a row with no aircraft in `uas` is marked
        `unregistered`, never left to read as registered."""
        operator_list = list(operators)
        uas_list = list(uas)
        async with self.engine.begin() as connection:
            await connection.execute(
                sa.text(
                    "DELETE FROM known_uas_operators "
                    "WHERE NOT (operator_id = ANY(:ids))"
                ),
                {"ids": [o.operator_id for o in operator_list]},
            )
            await self._upsert_operators(connection, operator_list)
            updated = await self._update_uas(connection, uas_list)
            orphans = await connection.execute(
                sa.text(
                    "UPDATE known_drones SET registration_status = :unregistered, "
                    "uas_operator_id = NULL WHERE NOT (drone_id = ANY(:ids)) "
                    "AND registration_status IS DISTINCT FROM :unregistered"
                ),
                {"ids": [u.drone_id for u in uas_list], "unregistered": UNREGISTERED},
            )
            updated += int(orphans.rowcount)
        return len(operator_list), updated

    @staticmethod
    async def _upsert_operators(
        connection: AsyncConnection,
        operators: list[ProjectedOperator],
    ) -> None:
        for operator in operators:
            # A registration number is the operator's identity and never
            # changes (U-01); a row with this number under another id is a
            # registry that was re-created, and the new id wins.
            await connection.execute(
                sa.text(
                    "DELETE FROM known_uas_operators WHERE "
                    "upper(registration_number) = upper(:number) AND operator_id <> :id"
                ),
                {"number": operator.registration_number, "id": operator.operator_id},
            )
            await connection.execute(
                sa.text(
                    "INSERT INTO known_uas_operators "
                    "(operator_id, registration_number, status, projected_at) "
                    "VALUES (:id, :number, :status, now()) "
                    "ON CONFLICT (operator_id) DO UPDATE SET "
                    "registration_number = EXCLUDED.registration_number, "
                    "status = EXCLUDED.status, projected_at = now()"
                ),
                {
                    "id": operator.operator_id,
                    "number": operator.registration_number,
                    "status": RegistrationStatus(operator.status).value,
                },
            )

    @staticmethod
    async def _update_uas(connection: AsyncConnection, uas: list[ProjectedUas]) -> int:
        updated = 0
        for row in uas:
            result = await connection.execute(
                sa.text(
                    "UPDATE known_drones SET registration_status = :status, "
                    "uas_operator_id = :operator WHERE drone_id = :id AND ("
                    "registration_status IS DISTINCT FROM :status OR "
                    "uas_operator_id IS DISTINCT FROM :operator)"
                ),
                {
                    "id": row.drone_id,
                    "status": RegistrationStatus(row.registration_status).value,
                    "operator": row.uas_operator_id,
                },
            )
            updated += int(result.rowcount)
        return updated

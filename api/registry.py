"""The fleet registry: bases, pilots, drones, and their audit trail. P2-05, P2-06.

## Every change is two writes, or none

Each mutation writes its row and its `events` row in one transaction, so the
audit log cannot miss a change and cannot record one that did not happen.
`events` itself refuses UPDATE and DELETE in the database (migration
0001_fleet).

## Registering a drone reaches the telemetry database too

The Gateway never connects to this database, so it cannot see `drones`. It
attributes telemetry through `known_drones`, a projection in the telemetry
database, and `source_bindings` refuses a drone that projection has not heard
of. So registering or retiring a drone here writes the projection as well
(TASKS.md P2-05). Without it, a drone looks registered here and its binding
is refused there, and neither side shows why.

The projection is written *inside* the relational transaction, before it
commits. If the projection fails, the registration rolls back and says so. If
the relational commit fails after the projection succeeded, the telemetry
database knows a drone this one does not: harmless, because the projection is
never an authority, and registering again rewrites it.

Retiring is the other way round, because closing a binding is not harmless
to leave behind: the projection follows the relational commit, and a failure
there is repaired by retiring again (`FleetRegistry.retire_drone`).

## Status is derived, never stored

P2-05 requires drone status to come from telemetry freshness, not from a
person setting it. What a person does decide is stored: maintenance, and
retirement. See `derive_status`.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from api.actors import SYSTEM, Actor
from common import get_logger

_log = get_logger(__name__)

# A change with no signed-in operator behind it is attributed to the API
# itself (`api.actors.SYSTEM`). Through the HTTP API, since P6-08, every
# change carries the operator who made it: each mutation takes an `actor`.


class DroneStatus(StrEnum):
    """P2-05's statuses. Two of them are not produced yet; see `derive_status`."""

    IDLE = "IDLE"
    ASSIGNED = "ASSIGNED"
    IN_FLIGHT = "IN_FLIGHT"
    CHARGING = "CHARGING"
    MAINTENANCE = "MAINTENANCE"
    OFFLINE = "OFFLINE"


class PilotStatus(StrEnum):
    AVAILABLE = "AVAILABLE"
    ON_DUTY = "ON_DUTY"
    OFF_DUTY = "OFF_DUTY"


class RegistryError(RuntimeError):
    """A change the registry refused. `kind` picks the HTTP status; `code`
    is a stable, more specific reason a client may branch on."""

    def __init__(self, kind: str, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.code = code or kind


class NotFoundError(RegistryError):
    def __init__(self, message: str) -> None:
        super().__init__("not_found", message)


class ConflictError(RegistryError):
    def __init__(self, message: str, *, code: str = "conflict") -> None:
        super().__init__("conflict", message, code=code)


class ProjectionIncompleteError(RegistryError):
    """The relational change committed; the telemetry projection did not
    follow. Repeating the request finishes it."""

    def __init__(self, message: str) -> None:
        super().__init__("projection_incomplete", message)


# What a refusal by a database constraint is called, by SQLSTATE. The
# database's own message names tables, columns and constraints and can quote
# other rows' values, so it is logged and never returned.
_CONSTRAINT_REFUSALS = {
    "23505": ("duplicate", "it duplicates an existing record"),
    "23503": ("unknown_reference", "it refers to a record that does not exist"),
    "23514": ("invalid_value", "a value is outside what the registry accepts"),
    "23502": ("invalid_value", "a required value is missing"),
}


def _refused(entity: str, name: str, error: IntegrityError) -> ConflictError:
    """A stable refusal for the client, and the database's detail in the log."""
    sqlstate = getattr(error.orig, "sqlstate", None)
    code, reason = _CONSTRAINT_REFUSALS.get(
        str(sqlstate), ("integrity", "it conflicts with the registry")
    )
    _log.warning(
        "registry change refused by a database constraint",
        extra={
            "entity_type": entity,
            "code": code,
            "sqlstate": sqlstate,
            "error": str(error.orig),
            # asyncpg's DETAIL line: which key, with its value.
            "detail": getattr(error.orig, "detail", None),
        },
    )
    return ConflictError(f"{entity} {name!r} refused: {reason}", code=code)


class TelemetryProjection(Protocol):
    """What the registry needs from the telemetry database.

    `gateway.binding.BindingResolver` provides it; tests may provide less.
    """

    async def register_drone(
        self,
        drone_id: UUID,
        label: str,
        *,
        retired_at: datetime | None = None,
        serial: str | None = None,
    ) -> None: ...

    async def close_bindings_for_drone(
        self, drone_id: UUID, *, at: datetime
    ) -> int: ...


class LiveStateReader(Protocol):
    """Reads a drone's live state: None when its link is lost (P1-05)."""

    async def get(self, drone_id: UUID) -> dict[str, Any] | None: ...

    async def get_many(
        self, drone_ids: Sequence[UUID]
    ) -> dict[UUID, dict[str, Any] | None]:
        """Several at once, in one round trip (`api.live.RedisLiveState`)."""
        ...


def derive_status(
    *, in_maintenance: bool, retired: bool, live: Mapping[str, Any] | None
) -> DroneStatus:
    """A drone's status, from what a person decided and what telemetry says.

    - MAINTENANCE: set by a person, and it wins. An aircraft on the bench may
      well be powered and transmitting.
    - OFFLINE: retired, or no live state - no telemetry within the link
      timeout (P1-05).
    - IN_FLIGHT: live and armed. Armed is the nearest thing telemetry has to
      "flying": an armed aircraft on the ground is treated as in flight,
      which is the safe side to err on.
    - IDLE: live and disarmed.

    ASSIGNED needs missions and CHARGING needs a charging signal; neither
    exists yet, so neither is ever returned rather than guessed.
    """
    if in_maintenance:
        return DroneStatus.MAINTENANCE
    if retired or live is None:
        return DroneStatus.OFFLINE
    if live.get("armed") is True:
        return DroneStatus.IN_FLIGHT
    return DroneStatus.IDLE


@dataclass(frozen=True, slots=True)
class AirframeParams:
    max_payload_g: int | None = None
    max_range_m: float | None = None
    battery_capacity_wh: float | None = None
    cruise_speed_ms: float | None = None
    avg_power_w: float | None = None


_DRONE_COLUMNS = (
    "id, serial, label, model, max_payload_g, max_range_m, battery_capacity_wh, "
    "cruise_speed_ms, avg_power_w, home_base_id, current_pilot_id, "
    "in_maintenance, retired_at, created_at"
)
_BASE_COLUMNS = (
    "id, name, ST_Y(geom) AS lat_deg, ST_X(geom) AS lon_deg, elevation_amsl_m, "
    "capacity, charging_slots, created_at"
)
_PILOT_COLUMNS = "id, name, license_ref, status, max_concurrent_drones, created_at"


def _row(row: sa.Row[Any]) -> dict[str, Any]:
    return dict(row._mapping)


@dataclass
class FleetRegistry:
    engine: AsyncEngine
    projection: TelemetryProjection
    live: LiveStateReader
    clock: Any = None

    def _now(self) -> datetime:
        if self.clock is not None:
            now: datetime = self.clock()
            return now
        return datetime.now(tz=UTC)

    # --- audit --------------------------------------------------------------

    async def _audit(
        self,
        connection: AsyncConnection,
        entity_type: str,
        entity_id: UUID,
        event_type: str,
        payload: Mapping[str, Any],
        *,
        actor: Actor,
    ) -> None:
        await connection.execute(
            sa.text(
                "INSERT INTO events "
                "(actor_type, actor_id, entity_type, entity_id, event_type, payload) "
                "VALUES (:actor_type, :actor_id, :entity_type, :entity_id, "
                "        :event_type, CAST(:payload AS jsonb))"
            ),
            {
                "actor_type": actor.actor_type,
                "actor_id": actor.actor_id,
                "entity_type": entity_type,
                "entity_id": str(entity_id),
                "event_type": event_type,
                "payload": json.dumps(payload, default=str),
            },
        )

    async def events(
        self,
        *,
        entity_type: str | None = None,
        entity_id: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        after_id: int | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        """The audit trail in the order it was written, filtered. P2-06.

        Ordered by `id`, not `ts`: `ts` is the transaction's start, so two
        changes can share one, while `id` is the order of insertion. A page
        continues from the last `id` it returned (`after_id`), so a long
        history is read completely rather than cut at `limit`.
        """
        clauses: list[str] = []
        params: dict[str, Any] = {"limit": limit}
        if entity_type is not None:
            clauses.append("entity_type = :entity_type")
            params["entity_type"] = entity_type
        if entity_id is not None:
            clauses.append("entity_id = :entity_id")
            params["entity_id"] = entity_id
        if since is not None:
            clauses.append("ts >= :since")
            params["since"] = since
        if until is not None:
            clauses.append("ts < :until")
            params["until"] = until
        if after_id is not None:
            clauses.append("id > :after_id")
            params["after_id"] = after_id
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        async with self.engine.connect() as connection:
            rows = await connection.execute(
                sa.text(
                    "SELECT id, ts, actor_type, actor_id, entity_type, entity_id, "
                    f"event_type, payload FROM events {where} "
                    "ORDER BY id LIMIT :limit"
                ),
                params,
            )
            return [_row(row) for row in rows]

    # --- bases ----------------------------------------------------------------

    async def create_base(
        self,
        *,
        name: str,
        lat_deg: float,
        lon_deg: float,
        elevation_amsl_m: float | None,
        capacity: int,
        charging_slots: int,
        actor: Actor = SYSTEM,
    ) -> dict[str, Any]:
        try:
            async with self.engine.begin() as connection:
                created = (
                    await connection.execute(
                        sa.text(
                            "INSERT INTO bases "
                            "(name, geom, elevation_amsl_m, capacity, charging_slots) "
                            "VALUES (:name, "
                            "        ST_SetSRID(ST_MakePoint(:lon, :lat), 4326), "
                            "        :elevation, :capacity, :charging) "
                            f"RETURNING {_BASE_COLUMNS}"
                        ),
                        {
                            "name": name,
                            "lat": lat_deg,
                            "lon": lon_deg,
                            "elevation": elevation_amsl_m,
                            "capacity": capacity,
                            "charging": charging_slots,
                        },
                    )
                ).one()
                base = _row(created)
                await self._audit(
                    connection, "base", base["id"], "created", base, actor=actor
                )
                return base
        except IntegrityError as error:
            raise _refused("base", name, error) from error

    async def list_bases(self) -> list[dict[str, Any]]:
        async with self.engine.connect() as connection:
            rows = await connection.execute(
                sa.text(f"SELECT {_BASE_COLUMNS} FROM bases ORDER BY name")
            )
            return [_row(row) for row in rows]

    # --- pilots ---------------------------------------------------------------

    async def create_pilot(
        self,
        *,
        name: str,
        license_ref: str | None,
        max_concurrent_drones: int,
        actor: Actor = SYSTEM,
    ) -> dict[str, Any]:
        try:
            async with self.engine.begin() as connection:
                created = (
                    await connection.execute(
                        sa.text(
                            "INSERT INTO pilots "
                            "(name, license_ref, max_concurrent_drones) "
                            "VALUES (:name, :license_ref, :max_concurrent) "
                            f"RETURNING {_PILOT_COLUMNS}"
                        ),
                        {
                            "name": name,
                            "license_ref": license_ref,
                            "max_concurrent": max_concurrent_drones,
                        },
                    )
                ).one()
                pilot = _row(created)
                await self._audit(
                    connection, "pilot", pilot["id"], "created", pilot, actor=actor
                )
                return pilot
        except IntegrityError as error:
            raise _refused("pilot", name, error) from error

    async def list_pilots(self) -> list[dict[str, Any]]:
        async with self.engine.connect() as connection:
            rows = await connection.execute(
                sa.text(f"SELECT {_PILOT_COLUMNS} FROM pilots ORDER BY name")
            )
            return [_row(row) for row in rows]

    async def set_pilot_status(
        self, pilot_id: UUID, status: PilotStatus, *, actor: Actor = SYSTEM
    ) -> dict[str, Any]:
        async with self.engine.begin() as connection:
            updated = (
                await connection.execute(
                    sa.text(
                        "UPDATE pilots SET status = :status WHERE id = :id "
                        f"RETURNING {_PILOT_COLUMNS}"
                    ),
                    {"id": str(pilot_id), "status": status.value},
                )
            ).one_or_none()
            if updated is None:
                raise NotFoundError(f"no pilot {pilot_id}")
            pilot = _row(updated)
            await self._audit(
                connection,
                "pilot",
                pilot_id,
                "status_changed",
                {"status": status},
                actor=actor,
            )
            return pilot

    # --- drones ---------------------------------------------------------------

    async def register_drone(
        self,
        *,
        serial: str,
        label: str,
        model: str | None,
        params: AirframeParams,
        home_base_id: UUID | None,
        current_pilot_id: UUID | None,
        actor: Actor = SYSTEM,
    ) -> dict[str, Any]:
        """Register a drone here and make it bindable in the telemetry database."""
        try:
            async with self.engine.begin() as connection:
                created = (
                    await connection.execute(
                        sa.text(
                            "INSERT INTO drones "
                            "(serial, label, model, max_payload_g, max_range_m, "
                            " battery_capacity_wh, cruise_speed_ms, avg_power_w, "
                            " home_base_id, current_pilot_id) "
                            "VALUES (:serial, :label, :model, :max_payload_g, "
                            " :max_range_m, :battery_capacity_wh, :cruise_speed_ms, "
                            " :avg_power_w, :home_base_id, :current_pilot_id) "
                            f"RETURNING {_DRONE_COLUMNS}"
                        ),
                        {
                            "serial": serial,
                            "label": label,
                            "model": model,
                            "max_payload_g": params.max_payload_g,
                            "max_range_m": params.max_range_m,
                            "battery_capacity_wh": params.battery_capacity_wh,
                            "cruise_speed_ms": params.cruise_speed_ms,
                            "avg_power_w": params.avg_power_w,
                            "home_base_id": _opt(home_base_id),
                            "current_pilot_id": _opt(current_pilot_id),
                        },
                    )
                ).one()
                drone = _row(created)
                await self._audit(
                    connection, "drone", drone["id"], "registered", drone, actor=actor
                )
                # Last, and inside the transaction: see the module docstring.
                await self.projection.register_drone(
                    drone["id"], label, serial=drone["serial"]
                )
        except IntegrityError as error:
            raise _refused("drone", label, error) from error
        _log.info(
            "drone registered", extra={"drone_id": str(drone["id"]), "label": label}
        )
        return await self._with_status(drone)

    async def get_drone(self, drone_id: UUID) -> dict[str, Any]:
        async with self.engine.connect() as connection:
            found = (
                await connection.execute(
                    sa.text(f"SELECT {_DRONE_COLUMNS} FROM drones WHERE id = :id"),
                    {"id": str(drone_id)},
                )
            ).one_or_none()
        if found is None:
            raise NotFoundError(f"no drone {drone_id}")
        return await self._with_status(_row(found))

    async def list_drones(
        self, *, include_retired: bool = False
    ) -> list[dict[str, Any]]:
        where = "" if include_retired else "WHERE retired_at IS NULL"
        async with self.engine.connect() as connection:
            rows = await connection.execute(
                sa.text(f"SELECT {_DRONE_COLUMNS} FROM drones {where} ORDER BY label")
            )
            drones = [_row(row) for row in rows]
        try:
            live = await self.live.get_many([drone["id"] for drone in drones])
        except Exception as error:
            # As in `_with_status`: unknown is reported as OFFLINE.
            _log.warning(
                "could not read live state",
                extra={"drone_count": len(drones), "error": repr(error)},
            )
            live = {}
        for drone in drones:
            _set_status(drone, live.get(drone["id"]))
        return drones

    async def set_maintenance(
        self, drone_id: UUID, in_maintenance: bool, *, actor: Actor = SYSTEM
    ) -> dict[str, Any]:
        async with self.engine.begin() as connection:
            updated = (
                await connection.execute(
                    sa.text(
                        "UPDATE drones SET in_maintenance = :flag WHERE id = :id "
                        f"RETURNING {_DRONE_COLUMNS}"
                    ),
                    {"id": str(drone_id), "flag": in_maintenance},
                )
            ).one_or_none()
            if updated is None:
                raise NotFoundError(f"no drone {drone_id}")
            await self._audit(
                connection,
                "drone",
                drone_id,
                "maintenance_started" if in_maintenance else "maintenance_ended",
                {},
                actor=actor,
            )
        return await self._with_status(_row(updated))

    async def retire_drone(
        self, drone_id: UUID, *, actor: Actor = SYSTEM
    ) -> dict[str, Any]:
        """Retire, never delete: its past telemetry must stay attributable.

        The projection is marked retired and every open binding is closed, so
        its SYSID stops being attributed to it from now on.

        Unlike registering, the telemetry writes come *after* the relational
        commit. Closing a binding cannot be undone by a rollback here, so
        doing it inside the transaction meant a failed audit insert or commit
        left the drone active in this database and unattributable in that
        one, with nothing to repair it. In this order the only partial state
        is the recoverable one: retired here, bindings still open there. The
        caller is told (`ProjectionIncompleteError`), and retiring the drone
        again repairs it: an already-retired drone has its projection
        re-applied, idempotently and at its recorded `retired_at`, before
        the request is refused as a conflict.
        """
        at = self._now()
        async with self.engine.begin() as connection:
            updated = (
                await connection.execute(
                    sa.text(
                        "UPDATE drones SET retired_at = :at "
                        "WHERE id = :id AND retired_at IS NULL "
                        f"RETURNING {_DRONE_COLUMNS}"
                    ),
                    {"id": str(drone_id), "at": at},
                )
            ).one_or_none()
            existing = None
            if updated is None:
                existing = (
                    await connection.execute(
                        sa.text("SELECT label, retired_at FROM drones WHERE id = :id"),
                        {"id": str(drone_id)},
                    )
                ).one_or_none()
                if existing is None:
                    raise NotFoundError(f"no drone {drone_id}")
            else:
                await self._audit(
                    connection, "drone", drone_id, "retired", {}, actor=actor
                )

        if existing is not None:
            # Retired before. Finish a projection an earlier attempt may have
            # left incomplete, then refuse as before.
            await self._project_retirement(
                drone_id, existing.label, existing.retired_at, actor=actor
            )
            raise ConflictError(
                f"drone {drone_id} is already retired", code="already_retired"
            )
        assert updated is not None
        drone = _row(updated)
        await self._project_retirement(drone_id, drone["label"], at, actor=actor)
        return await self._with_status(drone)

    async def _project_retirement(
        self, drone_id: UUID, label: str, at: datetime, *, actor: Actor
    ) -> int:
        """Close the drone's bindings and mark its projection retired, after
        the relational retirement committed. Idempotent: a binding already
        closed is not closed again. Audited as `bindings_closed` when it
        closed any."""
        try:
            closed = await self.projection.close_bindings_for_drone(drone_id, at=at)
            await self.projection.register_drone(drone_id, label, retired_at=at)
        except Exception as error:
            _log.error(
                "drone retired but its telemetry projection was not updated",
                extra={"drone_id": str(drone_id), "error": repr(error)},
            )
            raise ProjectionIncompleteError(
                f"drone {drone_id} is retired, but its telemetry bindings could "
                "not be closed; retire it again to finish"
            ) from error
        if closed:
            async with self.engine.begin() as connection:
                await self._audit(
                    connection,
                    "drone",
                    drone_id,
                    "bindings_closed",
                    {"bindings_closed": closed, "at": at},
                    actor=actor,
                )
        return closed

    async def _with_status(self, drone: dict[str, Any]) -> dict[str, Any]:
        try:
            live = await self.live.get(drone["id"])
        except Exception as error:
            # Unknown is not the same as offline, but the API has only these
            # statuses; OFFLINE is the one that sends nobody an aircraft.
            _log.warning(
                "could not read live state",
                extra={"drone_id": str(drone["id"]), "error": repr(error)},
            )
            live = None
        return _set_status(drone, live)


def _set_status(
    drone: dict[str, Any], live: Mapping[str, Any] | None
) -> dict[str, Any]:
    drone["status"] = derive_status(
        in_maintenance=bool(drone["in_maintenance"]),
        retired=drone["retired_at"] is not None,
        live=live,
    )
    return drone


def _opt(value: UUID | None) -> str | None:
    return None if value is None else str(value)

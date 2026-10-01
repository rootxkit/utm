"""Network identification: who a track is, as the registry sees it. U-02.

Every track on `telemetry.*`, whatever its source, carries

    "identification": {"status": ..., "serial": ..., "operator_reg": ...,
                       "mismatch": ..., "reason": ...,
                       "registered_operator_reg": ...}

`status` is one of four:

| status | when |
|---|---|
| `registered` | the serial is a registered, active UAS owned by an active operator, and the operator ID broadcast with it is that operator's (case-insensitively, U-01); or the aircraft is ours, on a relay binding or by a fleet serial |
| `suspended` | the serial is registered, and the UAS or its owner is suspended or revoked |
| `unknown_operator` | the serial is not registered; or it is, and the operator ID is absent, not in the registry, or not the owner's |
| `unidentified` | no serial at all (S-32's unidentified Remote ID tracks) |

`mismatch` is true when a registered serial is broadcast with an operator
ID that is not its owner's. It is never `registered`, and the airspace
monitor raises it as an `identification_mismatch` alert. `reason` is a
stable code saying which row of the table applied.

## Decisions the table leaves open

- **Suspension outranks the operator ID.** A suspended aircraft is
  `suspended` whatever operator it claims; the mismatch is still flagged.
- **An unknown serial with a known operator is `unknown_operator`,** as the
  task states it, even when that operator is suspended: the airframe is not
  in the registry, so nothing registered is flying.
- **Our own fleet** (a registered aircraft with no UAS operator, from the
  fleet registry) is `registered` on its serial alone. The operator ID is
  not compared: the fleet has no UAS operator to compare it with.
- **A relay track** is resolved by its `drone_id`, which a station binding
  attributed (`source_bindings`): `registered`, or `suspended` when the
  registry says so. The relay authenticates the station, so its aircraft is
  identified by the binding, not by a claim it broadcasts.
- **An owner the projection does not hold** (written in another order, or
  lost) is `unknown_operator` with reason `owner_unknown`: nothing says the
  operator is active.

## The spoofing guard (absorbs S-10)

A broadcast claiming one of our aircraft's serials while that aircraft's
own, authenticated telemetry places it elsewhere is not that aircraft: it
is published as a separate, unverified track under the broadcast's own id,
`unknown_operator` with `mismatch` and reason `serial_conflict`
(`gateway/remote_id_ingest.py`).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from uuid import UUID

from common.uas_identity import RegistrationStatus
from gateway.registry_projection import (
    OperatorFacts,
    RegistrySnapshot,
    UasFacts,
    operator_key,
)


class IdentificationStatus(StrEnum):
    REGISTERED = "registered"
    SUSPENDED = "suspended"
    UNKNOWN_OPERATOR = "unknown_operator"
    UNIDENTIFIED = "unidentified"


# Statuses that, inside a PROHIBITED or REQ_AUTHORISATION zone, raise an
# `identification` alert: the seam U-12 turns into an incident.
INCIDENT_STATUSES = frozenset(
    {IdentificationStatus.UNIDENTIFIED, IdentificationStatus.UNKNOWN_OPERATOR}
)

_INACTIVE = frozenset({RegistrationStatus.SUSPENDED, RegistrationStatus.REVOKED})


class Reason(StrEnum):
    NO_SERIAL = "no_serial"
    SERIAL_UNKNOWN = "serial_unknown"
    # A Basic ID of another type (a CAA registration, a session id): an
    # identity, but no serial to look up.
    NOT_A_SERIAL = "not_a_serial"
    UAS_SUSPENDED = "uas_suspended"
    UAS_REVOKED = "uas_revoked"
    OPERATOR_SUSPENDED = "operator_suspended"
    OPERATOR_REVOKED = "operator_revoked"
    OPERATOR_ABSENT = "operator_absent"
    OPERATOR_MISMATCH = "operator_mismatch"
    OWNER_UNKNOWN = "owner_unknown"
    MATCHED = "matched"
    # In `known_drones`, but the relational registry has no such aircraft.
    NOT_IN_REGISTRY = "not_in_registry"
    FLEET = "fleet"
    RELAY_BINDING = "relay_binding"
    # S-10: a registered serial heard away from where its aircraft is.
    SERIAL_CONFLICT = "serial_conflict"


@dataclass(frozen=True, slots=True)
class Identification:
    status: IdentificationStatus
    reason: Reason
    # As broadcast (or as the registry holds it, for a relay track).
    serial: str | None = None
    operator_reg: str | None = None
    mismatch: bool = False
    # The owner's registration number, only on a mismatch: what the
    # broadcast should have said.
    registered_operator_reg: str | None = None
    # The registry's id for the aircraft, when its serial is registered.
    drone_id: UUID | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "reason": self.reason.value,
            "serial": self.serial,
            "operator_reg": self.operator_reg,
            "mismatch": self.mismatch,
            "registered_operator_reg": self.registered_operator_reg,
        }


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _inactive_reason(uas: UasFacts, owner: OperatorFacts | None) -> Reason | None:
    if uas.registration_status is RegistrationStatus.REVOKED:
        return Reason.UAS_REVOKED
    if uas.registration_status is RegistrationStatus.SUSPENDED:
        return Reason.UAS_SUSPENDED
    if owner is not None and owner.status in _INACTIVE:
        return (
            Reason.OPERATOR_REVOKED
            if owner.status is RegistrationStatus.REVOKED
            else Reason.OPERATOR_SUSPENDED
        )
    return None


def resolve(
    snapshot: RegistrySnapshot, *, serial: str | None, operator_reg: str | None
) -> Identification:
    """A broadcast identity (Basic ID serial and Operator ID) against the
    registry. The module docstring's table, row by row."""
    serial = _clean(serial)
    operator_reg = _clean(operator_reg)
    if serial is None:
        return Identification(
            IdentificationStatus.UNIDENTIFIED,
            Reason.NO_SERIAL,
            operator_reg=operator_reg,
        )
    uas = snapshot.find_uas(serial)
    if uas is None:
        return Identification(
            IdentificationStatus.UNKNOWN_OPERATOR,
            Reason.SERIAL_UNKNOWN,
            serial=serial,
            operator_reg=operator_reg,
        )
    if not uas.in_registry:
        return Identification(
            IdentificationStatus.UNKNOWN_OPERATOR,
            Reason.NOT_IN_REGISTRY,
            serial=serial,
            operator_reg=operator_reg,
            drone_id=uas.drone_id,
        )
    owner = (
        None
        if uas.uas_operator_id is None
        else snapshot.operators_by_id.get(uas.uas_operator_id)
    )
    mismatch = (
        operator_reg is not None
        and owner is not None
        and operator_key(owner.registration_number) != operator_key(operator_reg)
    )

    def identified(status: IdentificationStatus, reason: Reason) -> Identification:
        return Identification(
            status,
            reason,
            serial=serial,
            operator_reg=operator_reg,
            mismatch=mismatch,
            registered_operator_reg=(
                owner.registration_number if mismatch and owner else None
            ),
            drone_id=uas.drone_id,
        )

    inactive = _inactive_reason(uas, owner)
    if inactive is not None:
        return identified(IdentificationStatus.SUSPENDED, inactive)
    if uas.uas_operator_id is None:
        return identified(IdentificationStatus.REGISTERED, Reason.FLEET)
    if owner is None:
        return identified(IdentificationStatus.UNKNOWN_OPERATOR, Reason.OWNER_UNKNOWN)
    if operator_reg is None:
        return identified(IdentificationStatus.UNKNOWN_OPERATOR, Reason.OPERATOR_ABSENT)
    if mismatch:
        return identified(
            IdentificationStatus.UNKNOWN_OPERATOR, Reason.OPERATOR_MISMATCH
        )
    return identified(IdentificationStatus.REGISTERED, Reason.MATCHED)


def resolve_bound(snapshot: RegistrySnapshot, drone_id: UUID) -> Identification:
    """A relay track: its aircraft is the one a station binding names."""
    uas = snapshot.by_drone_id.get(drone_id)
    if uas is None:
        # Bound, so registered in the fleet; the projection read may simply
        # predate the registration.
        return Identification(
            IdentificationStatus.REGISTERED, Reason.RELAY_BINDING, drone_id=drone_id
        )
    if not uas.in_registry:
        # Bound by a station, but the registry has no such aircraft (a row
        # from before the API, or a registry restored without it).
        return Identification(
            IdentificationStatus.UNKNOWN_OPERATOR,
            Reason.NOT_IN_REGISTRY,
            serial=uas.serial,
            drone_id=drone_id,
        )
    owner = (
        None
        if uas.uas_operator_id is None
        else snapshot.operators_by_id.get(uas.uas_operator_id)
    )
    inactive = _inactive_reason(uas, owner)
    return Identification(
        IdentificationStatus.SUSPENDED
        if inactive is not None
        else IdentificationStatus.REGISTERED,
        inactive or Reason.RELAY_BINDING,
        serial=uas.serial,
        operator_reg=None if owner is None else owner.registration_number,
        drone_id=drone_id,
    )


# ODID Basic ID type of a serial number (ANSI/CTA-2063-A); `gateway.odid`.
_ID_TYPE_SERIAL = 1


def resolve_remote_id(
    snapshot: RegistrySnapshot, remote_id: dict[str, Any]
) -> Identification:
    """A direct Remote ID observation's `remote_id` block (gateway/remote_id.py)."""
    operator_reg = remote_id.get("operator_id")
    operator = operator_reg if isinstance(operator_reg, str) else None
    ua_id = remote_id.get("ua_id")
    if not remote_id.get("identified", True) or not isinstance(ua_id, str):
        return resolve(snapshot, serial=None, operator_reg=operator)
    if remote_id.get("id_type") != _ID_TYPE_SERIAL:
        if not ua_id.strip():
            return resolve(snapshot, serial=None, operator_reg=operator)
        return Identification(
            IdentificationStatus.UNKNOWN_OPERATOR,
            Reason.NOT_A_SERIAL,
            operator_reg=_clean(operator),
        )
    return resolve(snapshot, serial=ua_id, operator_reg=operator)


def serial_conflict(serial: str, operator_reg: str | None) -> Identification:
    """S-10: a registered serial heard where its aircraft verifiably is not."""
    return Identification(
        IdentificationStatus.UNKNOWN_OPERATOR,
        Reason.SERIAL_CONFLICT,
        serial=serial,
        operator_reg=_clean(operator_reg),
        mismatch=True,
    )

"""Routes of the UAS operator registry (`api.uas_registry`). U-01.

Reading needs `viewer`, every change needs `admin`, as for the fleet
registry. Refusals are `detail: {code, message}` (`api.http_errors`).

Lookups take the registration number or serial as a query parameter, not a
path segment: a legacy serial is whatever its maker printed, slashes
included.
"""

# No `from __future__ import annotations`: see the note at the top of api/app.py.

from collections.abc import Callable, Coroutine
from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from api.auth import Operator, Role
from api.auth_http import Authenticator, require
from api.http_errors import registry_http
from api.registry import RegistryError
from api.uas_registry import (
    CONTACT_FIELDS,
    MAX_PAGE,
    Competency,
    CompetencyRecord,
    OperatorType,
    RecordSource,
    RegistrationStatus,
    UasRegistry,
)
from common.uas_identity import ClassLabel

_TEXT = 500


class UasOperatorIn(BaseModel):
    # Checked against UAS_OPERATOR_REGISTRATION_PATTERN; a mismatch is 422
    # with code `invalid_registration_number`.
    registration_number: str = Field(min_length=1, max_length=64)
    legal_name: str = Field(min_length=1, max_length=_TEXT)
    operator_type: OperatorType
    contact_email: str | None = Field(default=None, max_length=_TEXT)
    contact_phone: str | None = Field(default=None, max_length=64)
    postal_address: str | None = Field(default=None, max_length=_TEXT)
    valid_until: datetime | None = None


class UasOperatorPatch(BaseModel):
    """Only the fields sent are changed; a field sent as null is cleared."""

    legal_name: str | None = Field(default=None, min_length=1, max_length=_TEXT)
    operator_type: OperatorType | None = None
    contact_email: str | None = Field(default=None, max_length=_TEXT)
    contact_phone: str | None = Field(default=None, max_length=64)
    postal_address: str | None = Field(default=None, max_length=_TEXT)
    valid_until: datetime | None = None


class UasOperatorContact(BaseModel):
    contact_email: str | None
    contact_phone: str | None
    postal_address: str | None


class UasOperatorOut(BaseModel):
    id: UUID
    registration_number: str
    legal_name: str
    operator_type: OperatorType
    # A person's contact details: for operators and admins, null for viewers.
    contact: UasOperatorContact | None
    status: RegistrationStatus
    # The instant the registration stops being valid; null when not recorded.
    valid_until: datetime | None
    source: RecordSource
    created_at: datetime
    updated_at: datetime


class StatusChangeIn(BaseModel):
    """Why, for the audit log."""

    reason: str | None = Field(default=None, max_length=_TEXT)


class CompetencyIn(BaseModel):
    competency: Competency
    certificate_ref: str | None = Field(default=None, max_length=_TEXT)
    valid_until: datetime | None = None


class CompetencyOut(BaseModel):
    competency: Competency
    certificate_ref: str | None
    valid_until: datetime | None
    recorded_at: datetime


class RemotePilotIn(BaseModel):
    name: str = Field(min_length=1, max_length=_TEXT)
    # The pilot's certificate or registration reference; unique.
    license_ref: str | None = Field(default=None, max_length=_TEXT)
    uas_operator_id: UUID
    competencies: list[CompetencyIn] = Field(default_factory=list)


class RemotePilotOut(BaseModel):
    id: UUID
    name: str
    license_ref: str | None
    # Null for a pilot of our own fleet recorded before U-01.
    uas_operator_id: UUID | None
    operator_registration_number: str | None
    operator_status: RegistrationStatus | None
    registration_status: RegistrationStatus
    competencies: list[CompetencyOut]
    created_at: datetime


class UasIn(BaseModel):
    # CTA-2063-A for classes C1, C2, C3, C5 and C6; free text otherwise.
    serial: str = Field(min_length=1, max_length=64)
    # Null: no class label (legacy or privately built).
    class_label: ClassLabel | None
    mtom_g: int = Field(gt=0)
    uas_operator_id: UUID
    model: str | None = Field(default=None, max_length=_TEXT)
    # What consoles show; the serial when not given.
    label: str | None = Field(default=None, max_length=_TEXT)


class UasPatch(BaseModel):
    """Only the fields sent are changed. `class_label: null` means none."""

    class_label: ClassLabel | None = None
    mtom_g: int | None = Field(default=None, gt=0)
    model: str | None = Field(default=None, max_length=_TEXT)
    uas_operator_id: UUID | None = None


class UasOut(BaseModel):
    id: UUID
    serial: str
    # Whether the serial is a well-formed ANSI/CTA-2063-A serial.
    serial_cta2063: bool
    label: str
    model: str | None
    class_label: ClassLabel | None
    mtom_g: int | None
    # Null for a fleet aircraft registered before U-01.
    uas_operator_id: UUID | None
    operator_registration_number: str | None
    operator_status: RegistrationStatus | None
    registration_status: RegistrationStatus
    retired_at: datetime | None
    created_at: datetime


Handler = Callable[[], Coroutine[Any, Any, dict[str, Any]]]

# Who may see an operator's contact details.
_SEES_CONTACT = frozenset({Role.OPERATOR, Role.ADMIN})


def _shown(row: dict[str, Any], who: Operator) -> dict[str, Any]:
    """An operator as `who` may see it: contact details only for operators
    and admins, never for viewers."""
    shown = {name: value for name, value in row.items() if name not in CONTACT_FIELDS}
    shown["contact"] = (
        {name: row.get(name) for name in CONTACT_FIELDS}
        if who.role in _SEES_CONTACT
        else None
    )
    return shown


async def _call(action: Handler) -> dict[str, Any]:
    try:
        return await action()
    except RegistryError as error:
        raise registry_http(error) from error


def uas_router(uas: UasRegistry | None, auth: Authenticator) -> APIRouter:
    """The routes. With no registry (a schema export) every route answers 503."""
    viewer = require(auth, Role.VIEWER)
    admin = require(auth, Role.ADMIN)
    router = APIRouter(prefix="/uas", tags=["uas registry"])

    def registry() -> UasRegistry:
        if uas is None:
            raise HTTPException(status_code=503, detail="no relational database")
        return uas

    # --- operators ----------------------------------------------------------------

    @router.post("/operators", response_model=UasOperatorOut, status_code=201)
    async def create_operator(
        body: UasOperatorIn, operator: Annotated[Operator, Depends(admin)]
    ) -> dict[str, Any]:
        created = await _call(
            lambda: registry().create_operator(
                **body.model_dump(), actor=operator.actor
            )
        )
        return _shown(created, operator)

    @router.get("/operators", response_model=list[UasOperatorOut])
    async def list_operators(
        who: Annotated[Operator, Depends(viewer)],
        status: RegistrationStatus | None = None,
        source: RecordSource | None = None,
        q: str | None = Query(default=None, max_length=_TEXT),
        limit: int = Query(default=100, ge=1, le=MAX_PAGE),
        offset: int = Query(default=0, ge=0),
    ) -> list[dict[str, Any]]:
        """`q` matches the registration number or the legal name."""
        rows = await registry().list_operators(
            status=status, source=source, query=q, limit=limit, offset=offset
        )
        return [_shown(row, who) for row in rows]

    @router.get("/operators/lookup", response_model=UasOperatorOut)
    async def operator_by_registration(
        who: Annotated[Operator, Depends(viewer)],
        registration_number: str = Query(min_length=1, max_length=64),
    ) -> dict[str, Any]:
        """Exactly one operator, by registration number, ignoring case."""
        found = await _call(
            lambda: registry().operator_by_registration(registration_number)
        )
        return _shown(found, who)

    @router.get("/operators/{operator_id}", response_model=UasOperatorOut)
    async def get_operator(
        operator_id: UUID, who: Annotated[Operator, Depends(viewer)]
    ) -> dict[str, Any]:
        return _shown(await _call(lambda: registry().get_operator(operator_id)), who)

    @router.patch("/operators/{operator_id}", response_model=UasOperatorOut)
    async def update_operator(
        operator_id: UUID,
        body: UasOperatorPatch,
        operator: Annotated[Operator, Depends(admin)],
    ) -> dict[str, Any]:
        updated = await _call(
            lambda: registry().update_operator(
                operator_id, body.model_dump(exclude_unset=True), actor=operator.actor
            )
        )
        return _shown(updated, operator)

    def operator_status_route(path: str, status: RegistrationStatus) -> None:
        @router.post(
            f"/operators/{{operator_id}}/{path}",
            response_model=UasOperatorOut,
            name=f"{path}_uas_operator",
        )
        async def change(
            operator_id: UUID,
            operator: Annotated[Operator, Depends(admin)],
            body: StatusChangeIn | None = None,
        ) -> dict[str, Any]:
            changed = await _call(
                lambda: registry().set_operator_status(
                    operator_id,
                    status,
                    reason=body.reason if body else None,
                    actor=operator.actor,
                )
            )
            return _shown(changed, operator)

    # --- remote pilots --------------------------------------------------------------

    @router.post("/pilots", response_model=RemotePilotOut, status_code=201)
    async def create_pilot(
        body: RemotePilotIn, operator: Annotated[Operator, Depends(admin)]
    ) -> dict[str, Any]:
        return await _call(
            lambda: registry().create_remote_pilot(
                name=body.name,
                license_ref=body.license_ref,
                uas_operator_id=body.uas_operator_id,
                competencies=[
                    CompetencyRecord(**record.model_dump())
                    for record in body.competencies
                ],
                actor=operator.actor,
            )
        )

    @router.get("/pilots", response_model=list[RemotePilotOut])
    async def list_pilots(
        _: Annotated[Operator, Depends(viewer)],
        operator_id: UUID | None = None,
        status: RegistrationStatus | None = None,
        q: str | None = Query(default=None, max_length=_TEXT),
        limit: int = Query(default=100, ge=1, le=MAX_PAGE),
        offset: int = Query(default=0, ge=0),
    ) -> list[dict[str, Any]]:
        """`q` matches the name or the certificate reference."""
        return await registry().list_remote_pilots(
            uas_operator_id=operator_id,
            status=status,
            query=q,
            limit=limit,
            offset=offset,
        )

    @router.get("/pilots/{pilot_id}", response_model=RemotePilotOut)
    async def get_pilot(
        pilot_id: UUID, _: Annotated[Operator, Depends(viewer)]
    ) -> dict[str, Any]:
        return await _call(lambda: registry().get_remote_pilot(pilot_id))

    @router.put("/pilots/{pilot_id}/competencies", response_model=RemotePilotOut)
    async def record_competency(
        pilot_id: UUID,
        body: CompetencyIn,
        operator: Annotated[Operator, Depends(admin)],
    ) -> dict[str, Any]:
        """Record one competency, replacing the pilot's previous record of it."""
        return await _call(
            lambda: registry().record_competency(
                pilot_id, CompetencyRecord(**body.model_dump()), actor=operator.actor
            )
        )

    def pilot_status_route(path: str, status: RegistrationStatus) -> None:
        @router.post(
            f"/pilots/{{pilot_id}}/{path}",
            response_model=RemotePilotOut,
            name=f"{path}_remote_pilot",
        )
        async def change(
            pilot_id: UUID,
            operator: Annotated[Operator, Depends(admin)],
            body: StatusChangeIn | None = None,
        ) -> dict[str, Any]:
            return await _call(
                lambda: registry().set_pilot_status(
                    pilot_id,
                    status,
                    reason=body.reason if body else None,
                    actor=operator.actor,
                )
            )

    # --- UAS ----------------------------------------------------------------------

    @router.post("/aircraft", response_model=UasOut, status_code=201)
    async def register_uas(
        body: UasIn, operator: Annotated[Operator, Depends(admin)]
    ) -> dict[str, Any]:
        return await _call(
            lambda: registry().register_uas(**body.model_dump(), actor=operator.actor)
        )

    @router.get("/aircraft", response_model=list[UasOut])
    async def list_uas(
        _: Annotated[Operator, Depends(viewer)],
        operator_id: UUID | None = None,
        status: RegistrationStatus | None = None,
        class_label: ClassLabel | None = None,
        q: str | None = Query(default=None, max_length=_TEXT),
        include_fleet: bool = False,
        limit: int = Query(default=100, ge=1, le=MAX_PAGE),
        offset: int = Query(default=0, ge=0),
    ) -> list[dict[str, Any]]:
        """`q` matches serial, label or model. Fleet aircraft with no
        operator are included only with `include_fleet`."""
        return await registry().list_uas(
            uas_operator_id=operator_id,
            status=status,
            class_label=class_label,
            query=q,
            include_fleet=include_fleet,
            limit=limit,
            offset=offset,
        )

    @router.get("/aircraft/lookup", response_model=UasOut)
    async def uas_by_serial(
        _: Annotated[Operator, Depends(viewer)],
        serial: str = Query(min_length=1, max_length=64),
    ) -> dict[str, Any]:
        """Exactly one aircraft, by serial: an exact match, else the only one
        that matches ignoring case."""
        return await _call(lambda: registry().uas_by_serial(serial))

    @router.get("/aircraft/{drone_id}", response_model=UasOut)
    async def get_uas(
        drone_id: UUID, _: Annotated[Operator, Depends(viewer)]
    ) -> dict[str, Any]:
        return await _call(lambda: registry().get_uas(drone_id))

    @router.patch("/aircraft/{drone_id}", response_model=UasOut)
    async def update_uas(
        drone_id: UUID,
        body: UasPatch,
        operator: Annotated[Operator, Depends(admin)],
    ) -> dict[str, Any]:
        return await _call(
            lambda: registry().update_uas(
                drone_id, body.model_dump(exclude_unset=True), actor=operator.actor
            )
        )

    def uas_status_route(path: str, status: RegistrationStatus) -> None:
        @router.post(
            f"/aircraft/{{drone_id}}/{path}",
            response_model=UasOut,
            name=f"{path}_uas",
        )
        async def change(
            drone_id: UUID,
            operator: Annotated[Operator, Depends(admin)],
            body: StatusChangeIn | None = None,
        ) -> dict[str, Any]:
            return await _call(
                lambda: registry().set_uas_status(
                    drone_id,
                    status,
                    reason=body.reason if body else None,
                    actor=operator.actor,
                )
            )

    for path, status in (
        ("suspend", RegistrationStatus.SUSPENDED),
        ("reactivate", RegistrationStatus.ACTIVE),
        ("revoke", RegistrationStatus.REVOKED),
    ):
        operator_status_route(path, status)
        pilot_status_route(path, status)
        uas_status_route(path, status)

    return router

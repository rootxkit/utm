"""Routes of the geographical zones (`api.zones`). P6-01, U-03.

Reading needs `viewer`. Changing a zone needs one of `ZONE_WRITERS`, today
`admin`: U-13 adds the `regulator` role there, the authority's own, without
touching the routes. Every change is in `events` (`api.zones`).

A zone is sent and returned as an ED-269 `UASZoneVersion`, with ED-269's own
field names, so the console's editor writes exactly what an export contains.
The models below describe that shape for the generated client types; the
authoritative check is `airspace.ed269.parse_zone`, the same strict reader an
import uses. A refusal is 422 with `detail: {code, message, problems}`, each
problem naming the field (`zone.geometry[0].upperLimit`) and why.

An import posts an ED-269 document as the request body
(`POST /airspace/zones/import`), with `dry_run=true` to validate it and see
what it would create and replace, writing nothing.
"""

# No `from __future__ import annotations`: see the note at the top of api/app.py.

from collections.abc import Callable, Coroutine
from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from airspace.ed269 import (
    Ed269Error,
    Problem,
    Purpose,
    Reason,
    Restriction,
    Uom,
    VerticalReference,
    YesNo,
    parse,
    parse_zone,
)
from api.auth import Operator, Role
from api.auth_http import Authenticator, authenticated, require
from api.http_errors import registry_http
from api.registry import RegistryError
from api.zones import ZoneImportRefusedError, ZoneStore

# Who may change zones. U-13 adds Role.REGULATOR here.
ZONE_WRITERS = frozenset({Role.ADMIN})
# An ED-269 file is a few hundred kilobytes for a whole state (Luxembourg's
# 46 zones are 120 kB); this is far above any real one and bounds memory.
MAX_IMPORT_BYTES = 10 * 1024 * 1024


class Ed269Model(BaseModel):
    """ED-269's camelCase names on the wire, snake_case here."""

    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, extra="forbid"
    )


class ZoneAuthority(Ed269Model):
    name: str | None = None
    service: str | None = None
    contact_name: str | None = None
    site_url: str | None = Field(default=None, alias="siteURL")
    email: str | None = None
    phone: str | None = None
    purpose: Purpose | None = None
    # An ISO 8601 duration, e.g. P2D.
    interval_before: str | None = None


class DailyPeriod(Ed269Model):
    day: list[Literal["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN", "ANY"]]
    # A time of day with an offset, e.g. "17:00:00.00Z" or "08:30+04:00".
    start_time: str
    end_time: str


class Applicability(Ed269Model):
    permanent: YesNo
    # ISO 8601 with an offset.
    start_date_time: str | None = None
    end_date_time: str | None = None
    schedule: list[DailyPeriod] | None = None


class PolygonProjection(Ed269Model):
    type: Literal["Polygon"]
    # Rings of [longitude, latitude], each closed; the first is the outside.
    coordinates: list[list[list[float]]]


class CircleProjection(Ed269Model):
    type: Literal["Circle"]
    # [longitude, latitude].
    center: list[float]
    # In the volume's uomDimensions.
    radius: float


class AirspaceVolume(Ed269Model):
    uom_dimensions: Uom
    # Absent: from the surface (lower) or unlimited (upper).
    lower_limit: float | None = None
    lower_vertical_reference: VerticalReference
    upper_limit: float | None = None
    upper_vertical_reference: VerticalReference
    horizontal_projection: PolygonProjection | CircleProjection = Field(
        discriminator="type"
    )


class Ed269Zone(Ed269Model):
    """An ED-269 UASZoneVersion with one volume."""

    identifier: str
    # ISO 3166-1 alpha-3.
    country: str
    name: str | None = None
    # ED-269's zone type; "COMMON" in every published file seen.
    type: str
    restriction: Restriction
    reason: list[Reason] | None = None
    message: str | None = None
    applicability: list[Applicability]
    zone_authority: list[ZoneAuthority]
    geometry: list[AirspaceVolume]
    # Published fields carried as they are, not interpreted here.
    restriction_conditions: str | list[str] | None = None
    region: int | None = None
    other_reason_info: str | None = None
    regulation_exemption: YesNo | None = None
    u_space_class: str | None = None
    extended_properties: Any | None = None
    title: str | None = None


class ZoneOut(BaseModel):
    id: UUID
    # geozone, corridor or base. Only a geozone restricts, is edited here
    # and is exported.
    type: str
    # Whether the zone's applicability includes the moment of the request.
    active_now: bool
    feature: Ed269Zone
    # A GeoJSON Polygon in WGS84 to draw: the zone's own, or the one
    # inscribed in its circle.
    geometry: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class ProblemOut(BaseModel):
    field: str
    reason: str


class ImportReportOut(BaseModel):
    sha256: str
    dry_run: bool
    zones: int
    # Identifiers.
    created: list[str]
    updated: list[str]
    unchanged: list[str]


class Ed269Document(BaseModel):
    """An ED-269 document: `features` (or `UASZoneList`), checked strictly."""

    model_config = ConfigDict(extra="allow")

    title: str | None = None
    description: str | None = None
    features: list[Ed269Zone] = Field(default_factory=list)


def _refusal(problems: list[Problem], *, more: int = 0, code: str) -> HTTPException:
    return HTTPException(
        status_code=422 if code == "invalid_ed269" else 409,
        detail={
            "code": code,
            "message": "; ".join(f"{p.field}: {p.reason}" for p in problems[:5])
            + (f"; and {len(problems) - 5 + more} more" if len(problems) > 5 else ""),
            "problems": [p.as_dict() for p in problems],
            "more": more,
        },
    )


Handler = Callable[[], Coroutine[Any, Any, Any]]


async def _call(action: Handler) -> Any:
    try:
        return await action()
    except ZoneImportRefusedError as error:
        raise _refusal(error.problems, code=error.code) from error
    except RegistryError as error:
        raise registry_http(error) from error


def _checked(body: Ed269Zone) -> Any:
    """The body through the strict ED-269 reader, or 422 naming the fields."""
    try:
        return parse_zone(
            body.model_dump(by_alias=True, exclude_none=True, mode="json"), "zone"
        )
    except Ed269Error as error:
        raise _refusal(
            list(error.problems), more=error.more, code="invalid_ed269"
        ) from error


def zone_writer(
    auth: Authenticator,
) -> Callable[[Request], Coroutine[Any, Any, Operator]]:
    check = authenticated(auth)

    async def dependency(request: Request) -> Operator:
        operator = await check(request)
        if operator.role not in ZONE_WRITERS:
            raise HTTPException(
                status_code=403,
                detail="changing zones needs the "
                + " or ".join(sorted(role.value for role in ZONE_WRITERS))
                + " role",
            )
        return operator

    return dependency


def zone_router(zones: ZoneStore | None, auth: Authenticator) -> APIRouter:
    """The routes. With no store (a schema export) every route answers 503."""
    viewer = require(auth, Role.VIEWER)
    writer = zone_writer(auth)
    router = APIRouter(prefix="/airspace/zones", tags=["airspace zones"])

    def store() -> ZoneStore:
        if zones is None:
            raise HTTPException(status_code=503, detail="no relational database")
        return zones

    @router.get("", response_model=list[ZoneOut], response_model_exclude_none=True)
    async def list_zones(
        _: Annotated[Operator, Depends(viewer)],
    ) -> list[dict[str, Any]]:
        """Every zone, for drawing: geozones, corridors and bases. The
        monitor alerts on geozones that restrict and apply."""
        return await store().list_zones()

    @router.get(
        "/export",
        responses={200: {"content": {"application/json": {}}}},
        response_model=Ed269Document,
    )
    async def export_zones(
        _: Annotated[Operator, Depends(viewer)],
    ) -> JSONResponse:
        """Every geozone as an ED-269 document, to save as a file."""
        exported = await store().export()
        return JSONResponse(
            exported,
            headers={"Content-Disposition": 'attachment; filename="zones-ed269.json"'},
        )

    @router.post(
        "/import",
        response_model=ImportReportOut,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": {"$ref": "#/components/schemas/Ed269Document"}
                    }
                },
            }
        },
    )
    async def import_zones(
        request: Request,
        operator: Annotated[Operator, Depends(writer)],
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Add and replace zones from an ED-269 document, by identifier, all
        or nothing. `dry_run` validates and reports, writing nothing."""
        data = await request.body()
        if len(data) > MAX_IMPORT_BYTES:
            raise HTTPException(
                status_code=413, detail=f"at most {MAX_IMPORT_BYTES} bytes"
            )
        try:
            parsed = parse(data)
        except Ed269Error as error:
            raise _refusal(
                list(error.problems), more=error.more, code="invalid_ed269"
            ) from error
        report = await _call(
            lambda: store().import_document(
                parsed, data, actor=operator.actor, dry_run=dry_run
            )
        )
        result: dict[str, Any] = report.as_dict()
        return result

    @router.get("/{zone_id}", response_model=ZoneOut, response_model_exclude_none=True)
    async def get_zone(
        zone_id: UUID, _: Annotated[Operator, Depends(viewer)]
    ) -> dict[str, Any]:
        result: dict[str, Any] = await _call(lambda: store().get(zone_id))
        return result

    @router.post(
        "", response_model=ZoneOut, status_code=201, response_model_exclude_none=True
    )
    async def create_zone(
        body: Ed269Zone, operator: Annotated[Operator, Depends(writer)]
    ) -> dict[str, Any]:
        """Create a zone, as the editor draws it."""
        geozone = _checked(body)
        result: dict[str, Any] = await _call(
            lambda: store().create(geozone, actor=operator.actor)
        )
        return result

    @router.put("/{zone_id}", response_model=ZoneOut, response_model_exclude_none=True)
    async def replace_zone(
        zone_id: UUID, body: Ed269Zone, operator: Annotated[Operator, Depends(writer)]
    ) -> dict[str, Any]:
        """Replace a zone with the one sent; the identifier may change."""
        geozone = _checked(body)
        result: dict[str, Any] = await _call(
            lambda: store().update(zone_id, geozone, actor=operator.actor)
        )
        return result

    @router.delete("/{zone_id}", status_code=204)
    async def delete_zone(
        zone_id: UUID, operator: Annotated[Operator, Depends(writer)]
    ) -> Response:
        await _call(lambda: store().delete(zone_id, actor=operator.actor))
        return Response(status_code=204)

    return router

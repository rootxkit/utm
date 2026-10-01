"""Routes of the source switches (`api.sources`). U-15.

Reading needs `viewer`; switching needs `admin`, with a reason. Refusals
are `detail: {code, message}`, as the registry's are (`api.http_errors`):

- 422 `unknown_source_type`, `invalid_instance_id`, `reason_required`,
  `reason_too_long`;
- 503 `control_channel_unavailable`: NATS is not connected, or JetStream
  cannot take the new state (not enabled, store full). Nothing was changed:
  the database and what the adapters follow still agree;
- 503 `sources_unavailable`: this API was built without the store.

A switch to the state a source is already in answers 200 with the row and
writes no event.
"""

# No `from __future__ import annotations`: see the note at the top of api/app.py.

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path
from pydantic import BaseModel, Field

from api.auth import Operator, Role
from api.auth_http import Authenticator, require
from api.sources import (
    MAX_REASON,
    ChannelUnavailableError,
    InvalidSourceError,
    SourceControlService,
    SourceError,
)


class SourceControlOut(BaseModel):
    source_type: str
    # Null for a switch on the whole type.
    instance_id: str | None
    enabled: bool
    reason: str
    # The username of the admin who made the switch.
    changed_by: str
    changed_at: datetime


class SourcesOut(BaseModel):
    # Whether an instance with no switch of its own is disabled.
    default_deny: bool
    # Every type a switch can name, whether or not an adapter runs for it.
    source_types: list[str]
    controls: list[SourceControlOut]


class SourceSwitchIn(BaseModel):
    enabled: bool
    reason: str = Field(min_length=1, max_length=MAX_REASON)


def _http(error: SourceError) -> HTTPException:
    detail = {"code": error.code, "message": str(error)}
    if isinstance(error, InvalidSourceError):
        return HTTPException(status_code=422, detail=detail)
    if isinstance(error, ChannelUnavailableError):
        return HTTPException(status_code=503, detail=detail)
    return HTTPException(status_code=400, detail=detail)


def sources_router(
    service: SourceControlService | None, auth: Authenticator
) -> APIRouter:
    router = APIRouter(prefix="/sources", tags=["sources"])
    viewer = require(auth, Role.VIEWER)
    admin = require(auth, Role.ADMIN)

    def available() -> SourceControlService:
        if service is None:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "sources_unavailable",
                    "message": "source control is not configured on this API",
                },
            )
        return service

    async def switch(
        source_type: str,
        instance_id: str | None,
        body: SourceSwitchIn,
        operator: Operator,
    ) -> dict[str, Any]:
        try:
            return await available().switch(
                source_type,
                instance_id,
                enabled=body.enabled,
                reason=body.reason,
                actor=operator.actor,
                actor_name=operator.username,
            )
        except SourceError as error:
            raise _http(error) from error

    @router.get("", response_model=SourcesOut)
    async def list_sources(
        _: Annotated[Operator, Depends(viewer)],
    ) -> dict[str, Any]:
        """Every switch that has been made. Live state (last seen, refused
        counts) is on the console feed, from the adapters."""
        return await available().listing()

    @router.put("/{source_type}", response_model=SourceControlOut)
    async def switch_type(
        source_type: Annotated[str, Path(max_length=32)],
        body: SourceSwitchIn,
        operator: Annotated[Operator, Depends(admin)],
    ) -> dict[str, Any]:
        """Switch a whole type: every station, receiver, provider or feed."""
        return await switch(source_type, None, body, operator)

    @router.put(
        "/{source_type}/instances/{instance_id}", response_model=SourceControlOut
    )
    async def switch_instance(
        source_type: Annotated[str, Path(max_length=32)],
        instance_id: Annotated[str, Path(max_length=128)],
        body: SourceSwitchIn,
        operator: Annotated[Operator, Depends(admin)],
    ) -> dict[str, Any]:
        """Switch one station, receiver, provider or feed."""
        return await switch(source_type, instance_id, body, operator)

    return router

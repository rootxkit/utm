"""How a registry refusal reaches an HTTP client. P2-05, S-16, U-01.

Shared by the fleet routes (`api.app`) and the UAS registry routes
(`api.uas_routes`), so both refuse in the same shape: `detail` is
`{code, message}`, with a stable `code` to branch on.
"""

from __future__ import annotations

from fastapi import HTTPException

from api.registry import (
    ConflictError,
    InvalidError,
    NotFoundError,
    ProjectionIncompleteError,
    RegistryError,
)


def registry_http(error: RegistryError) -> HTTPException:
    """A stable `code` to branch on and a message for people. Neither ever
    carries the database's own error text (`api.registry.refused`)."""
    detail = {"code": error.code, "message": str(error)}
    if isinstance(error, NotFoundError):
        return HTTPException(status_code=404, detail=detail)
    if isinstance(error, ConflictError):
        return HTTPException(status_code=409, detail=detail)
    if isinstance(error, InvalidError):
        return HTTPException(status_code=422, detail=detail)
    if isinstance(error, ProjectionIncompleteError):
        # The change was made; its effect on telemetry was not. A retry
        # completes it, which is what 503 tells a client.
        return HTTPException(status_code=503, detail=detail)
    return HTTPException(status_code=400, detail=detail)

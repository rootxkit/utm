"""P6-08 at the HTTP boundary, without a database.

The account store is a fake that knows two bearer tokens (`auth_fakes`), but
the checks are the real ones. The first test walks every route the API has,
so a route added later without a role check fails here, not in production.
"""

from __future__ import annotations

import re
from typing import Any, cast
from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from api.app import create_api_app
from api.auth import CSRF_HEADER, FEED_COOKIE, SESSION_COOKIE, verify_feed_ticket
from api.registry import FleetRegistry
from api.replay import ReplayStore
from api.tests.auth_fakes import (
    ADMIN,
    ADMIN_HEADERS,
    FEED_SECRET,
    VIEWER,
    VIEWER_HEADERS,
    api_kwargs,
)

# Pages and files that carry no data. Everything else needs an operator.
PUBLIC = {
    "/",
    "/login",
    "/auth/login",
    "/auth/logout",
    "/replay",
    "/map",
    "/config.json",
    "/openapi.json",
    "/docs",
    "/docs/oauth2-redirect",
    "/redoc",
    "/basemap/{path:path}",
}
# Routes a viewer may call. Every other protected route needs more.
VIEWER_ROUTES = {
    ("GET", "/auth/me"),
    ("POST", "/auth/feed-ticket"),
    ("GET", "/bases"),
    ("GET", "/pilots"),
    ("GET", "/drones"),
    ("GET", "/drones/{drone_id}"),
    ("GET", "/events"),
    ("GET", "/replay/drones"),
    ("GET", "/airspace/zones"),
    ("GET", "/terrain"),
    ("GET", "/replay/drones/{drone_id}/flights"),
    ("GET", "/replay/drones/{drone_id}"),
    # U-01: the UAS operator registry is read by viewers, changed by admins.
    ("GET", "/uas/operators"),
    ("GET", "/uas/operators/lookup"),
    ("GET", "/uas/operators/{operator_id}"),
    ("GET", "/uas/pilots"),
    ("GET", "/uas/pilots/{pilot_id}"),
    ("GET", "/uas/aircraft"),
    ("GET", "/uas/aircraft/lookup"),
    ("GET", "/uas/aircraft/{drone_id}"),
}


def build() -> FastAPI:
    unused = cast(Any, None)
    replay = ReplayStore(
        telemetry=unused,
        relational=None,
        gap_threshold_s=3.0,
        evidence_slack_s=5.0,
        flight_split_s=120.0,
        max_samples=10,
    )
    return create_api_app(cast(FleetRegistry, unused), replay=replay, **api_kwargs())


def protected_routes(app: FastAPI) -> list[tuple[str, str]]:
    """Every documented route but the public ones.

    Read from the OpenAPI schema rather than `app.routes`: routes added with
    `include_router` do not appear there as `APIRoute` in every FastAPI
    version, and a walk that silently misses them passes for the wrong
    reason. Undocumented routes are pages and files, all in PUBLIC.
    """
    found = []
    for path, operations in app.openapi()["paths"].items():
        if path in PUBLIC:
            continue
        for method in operations:
            found.append((method.upper(), path))
    return sorted(found)


def concrete(path: str) -> str:
    return re.sub(r"\{[^}]+\}", str(uuid4()), path)


async def call(
    app: FastAPI,
    method: str,
    path: str,
    headers: dict[str, str] | None = None,
    cookies: dict[str, str] | None = None,
) -> Any:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers=headers or {},
        cookies=cookies or {},
    ) as client:
        return await client.request(method, concrete(path), json={})


def test_the_route_list_is_what_this_test_thinks_it_is() -> None:
    """If a route is added, it must be classified here on purpose."""
    routes = protected_routes(build())

    assert set(VIEWER_ROUTES) <= set(routes)
    assert len(routes) >= 20


async def test_every_protected_route_refuses_an_anonymous_caller() -> None:
    app = build()

    for method, path in protected_routes(app):
        response = await call(app, method, path)
        assert response.status_code == 401, (method, path, response.text)


async def test_a_viewer_is_refused_every_route_above_viewer() -> None:
    app = build()

    for method, path in protected_routes(app):
        if (method, path) in VIEWER_ROUTES:
            continue
        response = await call(app, method, path, headers=VIEWER_HEADERS)
        assert response.status_code == 403, (method, path, response.text)


async def test_a_viewer_gets_past_the_check_on_viewer_routes() -> None:
    """The paired presence: not refused by auth. What the route does next is
    not this test's business, so anything but 401 and 403 is a pass."""
    app = build()

    for method, path in VIEWER_ROUTES:
        try:
            response = await call(app, method, path, headers=VIEWER_HEADERS)
        except (AttributeError, TypeError):
            # The fake registry is None: reaching it means auth let us in.
            continue
        assert response.status_code not in (401, 403), (method, path)


async def test_an_admin_gets_past_the_check_on_admin_routes() -> None:
    app = build()

    for method, path in protected_routes(app):
        try:
            response = await call(app, method, path, headers=ADMIN_HEADERS)
        except (AttributeError, TypeError, NotImplementedError):
            continue
        assert response.status_code not in (401, 403), (method, path)


async def test_me_says_who_is_signed_in() -> None:
    response = await call(build(), "GET", "/auth/me", headers=VIEWER_HEADERS)

    assert response.json() == VIEWER.as_dict()


# --- cookies and cross-site requests ----------------------------------------------


async def test_a_cookie_session_without_the_request_header_cannot_change_state() -> (
    None
):
    """What a forged form on another site would send."""
    response = await call(
        build(),
        "POST",
        "/auth/feed-ticket",
        cookies={SESSION_COOKIE: "viewer-token"},
    )

    assert response.status_code == 403
    assert CSRF_HEADER in response.text


async def test_a_cookie_session_with_the_header_can() -> None:
    response = await call(
        build(),
        "POST",
        "/auth/feed-ticket",
        headers={CSRF_HEADER: "1"},
        cookies={SESSION_COOKIE: "viewer-token"},
    )

    assert response.status_code == 200
    ticket = response.cookies.get(FEED_COOKIE)
    assert ticket is not None
    claims = verify_feed_ticket(FEED_SECRET, ticket, now_s=0.0)
    assert claims is not None and claims.operator_id == VIEWER.id


async def test_a_cookie_session_may_read_without_the_header() -> None:
    response = await call(
        build(), "GET", "/auth/me", cookies={SESSION_COOKIE: "viewer-token"}
    )

    assert response.status_code == 200


async def test_a_bearer_token_needs_no_request_header() -> None:
    """A browser never attaches one on its own, so it cannot be forged."""
    response = await call(build(), "POST", "/auth/feed-ticket", headers=VIEWER_HEADERS)

    assert response.status_code == 200


async def test_an_unknown_token_is_anonymous() -> None:
    response = await call(
        build(), "GET", "/auth/me", headers={"Authorization": "Bearer nope"}
    )

    assert response.status_code == 401


# --- admins guarding themselves ------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("PUT", f"/operators/{ADMIN.id}/role", {"role": "viewer"}),
        ("POST", f"/operators/{ADMIN.id}/disable", {}),
    ],
)
async def test_an_admin_cannot_lock_themselves_out(
    method: str, path: str, body: dict[str, Any]
) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=build()),
        base_url="http://test",
        headers=ADMIN_HEADERS,
    ) as client:
        response = await client.request(method, path, json=body)

    assert response.status_code == 409

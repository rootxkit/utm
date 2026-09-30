"""P6-08 over HTTP: who is asking, and may they. Dependencies and routes.

`authenticated()` resolves the operator from the session cookie or a bearer
token and refuses anyone else with 401; `require(role)` refuses a signed-in
operator whose role is too low with 403. Every route of the API depends on
one of them; `create_api_app` has no way to add a route without.

A state-changing request authenticated by cookie must also carry
`X-Courier-Request` (see `api/auth.py`), or it is refused as a possible
cross-site request. A bearer token is not sent by a browser on its own, so
it needs no such proof.
"""

# No `from __future__ import annotations` here, deliberately: route
# signatures use `Annotated[Operator, Depends(viewer)]` where `viewer` is a
# local of the factory, and a postponed annotation is evaluated against
# module globals, where it does not exist. FastAPI then silently treats the
# parameter as a required query string called `_`.

from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Annotated, Any, Protocol
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from api.assets import STATIC
from api.auth import (
    CSRF_HEADER,
    FEED_COOKIE,
    SESSION_COOKIE,
    AuthError,
    Login,
    Operator,
    Role,
    issue_feed_ticket,
    wall_clock_s,
)
from api.ratelimit import LoginRateLimiter, retry_after_header

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


class Authenticator(Protocol):
    """What the API needs from the account store. `OperatorStore` is one."""

    async def session(self, token: str) -> Operator | None: ...


class AccountStore(Authenticator, Protocol):
    async def login(
        self,
        username: str,
        password: str,
        *,
        remote_addr: str | None = None,
        user_agent: str | None = None,
    ) -> Login: ...

    async def logout(self, token: str) -> bool: ...

    async def create_operator(
        self, *, username: str, display_name: str, role: Role, password: str, actor: Any
    ) -> dict[str, Any]: ...

    async def list_operators(self) -> list[dict[str, Any]]: ...

    async def set_role(
        self, operator_id: UUID, role: Role, *, actor: Any
    ) -> dict[str, Any]: ...

    async def disable(self, operator_id: UUID, *, actor: Any) -> dict[str, Any]: ...

    async def enable(self, operator_id: UUID, *, actor: Any) -> dict[str, Any]: ...

    async def set_password(
        self, operator_id: UUID, password: str, *, actor: Any
    ) -> dict[str, Any]: ...


def _token_from(request: Request) -> tuple[str | None, bool]:
    """The session token, and whether it came from a cookie."""
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip() or None, False
    return request.cookies.get(SESSION_COOKIE), True


def authenticated(
    auth: Authenticator,
) -> Callable[[Request], Awaitable[Operator]]:
    async def dependency(request: Request) -> Operator:
        token, from_cookie = _token_from(request)
        operator = await auth.session(token) if token else None
        if operator is None:
            raise HTTPException(status_code=401, detail="sign in required")
        if (
            from_cookie
            and request.method not in _SAFE_METHODS
            and request.headers.get(CSRF_HEADER) != "1"
        ):
            raise HTTPException(
                status_code=403, detail=f"{CSRF_HEADER} header required"
            )
        request.state.operator = operator
        return operator

    return dependency


def require(
    auth: Authenticator, role: Role
) -> Callable[[Request], Awaitable[Operator]]:
    check = authenticated(auth)

    async def dependency(request: Request) -> Operator:
        operator = await check(request)
        if not operator.role.allows(role):
            raise HTTPException(
                status_code=403, detail=f"this needs the {role.value} role"
            )
        return operator

    return dependency


# --- routes -----------------------------------------------------------------


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=512)
    # A script asks for the token to send as a bearer header. A browser does
    # not: its session lives in an HttpOnly cookie that page scripts cannot
    # read, and handing the token to the page would undo that.
    want_token: bool = False


class OperatorIn(BaseModel):
    username: str
    display_name: str = ""
    role: Role
    password: str


class RoleIn(BaseModel):
    role: Role


class PasswordIn(BaseModel):
    password: str


class OperatorOut(BaseModel):
    id: UUID
    username: str
    display_name: str
    role: Role
    created_at: datetime
    disabled_at: datetime | None
    password_changed_at: datetime
    locked_until: datetime | None


class MeOut(BaseModel):
    """Who is signed in, for the console header and its role checks."""

    id: UUID
    username: str
    display_name: str
    role: Role


def _http(error: AuthError) -> HTTPException:
    status = {
        "invalid_credentials": 401,
        "not_found": 404,
        "conflict": 409,
    }.get(error.kind, 422)
    return HTTPException(status_code=status, detail=str(error))


def auth_router(
    store: AccountStore,
    *,
    feed_secret: bytes,
    feed_ticket_ttl_s: float,
    cookie_secure: bool,
    login_limiter: LoginRateLimiter | None = None,
) -> APIRouter:
    router = APIRouter()
    limiter = login_limiter if login_limiter is not None else LoginRateLimiter()
    viewer = require(store, Role.VIEWER)
    admin = require(store, Role.ADMIN)

    def set_feed_cookie(response: Response, operator: Operator) -> None:
        ticket = issue_feed_ticket(
            feed_secret, operator, now_s=wall_clock_s(), ttl_s=feed_ticket_ttl_s
        )
        response.set_cookie(
            FEED_COOKIE,
            ticket,
            max_age=int(feed_ticket_ttl_s),
            httponly=True,
            secure=cookie_secure,
            samesite="strict",
        )

    @router.get("/login", response_class=HTMLResponse, include_in_schema=False)
    async def login_page() -> str:
        return (STATIC / "login.html").read_text(encoding="utf-8")

    @router.post("/auth/login")
    async def login(
        body: LoginIn, request: Request, response: Response
    ) -> dict[str, Any]:
        remote_addr = request.client.host if request.client else None
        # Before the store, so a refused attempt costs no hash and no row.
        wait_s = limiter.attempt(address=remote_addr, username=body.username)
        if wait_s is not None:
            raise HTTPException(
                status_code=429,
                detail="too many sign-in attempts; try again later",
                headers={"Retry-After": retry_after_header(wait_s)},
            )
        try:
            result = await store.login(
                body.username,
                body.password,
                remote_addr=remote_addr,
                user_agent=request.headers.get("user-agent"),
            )
        except AuthError as error:
            raise _http(error) from error
        max_age = max(0, int(result.expires_at.timestamp() - wall_clock_s()))
        response.set_cookie(
            SESSION_COOKIE,
            result.token,
            max_age=max_age,
            httponly=True,
            secure=cookie_secure,
            samesite="strict",
        )
        set_feed_cookie(response, result.operator)
        answer: dict[str, Any] = {"operator": result.operator.as_dict()}
        if body.want_token:
            answer["token"] = result.token
        return answer

    @router.post("/auth/logout")
    async def logout(request: Request, response: Response) -> dict[str, Any]:
        token, _ = _token_from(request)
        ended = await store.logout(token) if token else False
        response.delete_cookie(SESSION_COOKIE)
        response.delete_cookie(FEED_COOKIE)
        return {"ended": ended}

    @router.get("/auth/me", response_model=MeOut)
    async def me(operator: Annotated[Operator, Depends(viewer)]) -> dict[str, Any]:
        return operator.as_dict()

    @router.post("/auth/feed-ticket")
    async def feed_ticket(
        response: Response, operator: Annotated[Operator, Depends(viewer)]
    ) -> dict[str, Any]:
        """A fresh console-feed ticket, as a cookie. The page calls this
        before the previous one runs out and before every reconnect."""
        set_feed_cookie(response, operator)
        return {"ttl_s": feed_ticket_ttl_s}

    @router.get("/operators", response_model=list[OperatorOut])
    async def list_operators(
        _: Annotated[Operator, Depends(admin)],
    ) -> list[dict[str, Any]]:
        return await store.list_operators()

    @router.post("/operators", response_model=OperatorOut, status_code=201)
    async def create_operator(
        body: OperatorIn, operator: Annotated[Operator, Depends(admin)]
    ) -> dict[str, Any]:
        try:
            return await store.create_operator(
                username=body.username,
                display_name=body.display_name,
                role=body.role,
                password=body.password,
                actor=operator.actor,
            )
        except AuthError as error:
            raise _http(error) from error

    @router.put("/operators/{operator_id}/role", response_model=OperatorOut)
    async def set_role(
        operator_id: UUID, body: RoleIn, operator: Annotated[Operator, Depends(admin)]
    ) -> dict[str, Any]:
        if operator_id == operator.id and body.role is not Role.ADMIN:
            # The last admin demoting themselves is how a system ends up
            # with no one able to manage it.
            raise HTTPException(
                status_code=409, detail="an admin cannot lower their own role"
            )
        try:
            return await store.set_role(operator_id, body.role, actor=operator.actor)
        except AuthError as error:
            raise _http(error) from error

    @router.post("/operators/{operator_id}/disable", response_model=OperatorOut)
    async def disable(
        operator_id: UUID, operator: Annotated[Operator, Depends(admin)]
    ) -> dict[str, Any]:
        if operator_id == operator.id:
            raise HTTPException(
                status_code=409, detail="an admin cannot disable themselves"
            )
        try:
            return await store.disable(operator_id, actor=operator.actor)
        except AuthError as error:
            raise _http(error) from error

    @router.post("/operators/{operator_id}/enable", response_model=OperatorOut)
    async def enable(
        operator_id: UUID, operator: Annotated[Operator, Depends(admin)]
    ) -> dict[str, Any]:
        try:
            return await store.enable(operator_id, actor=operator.actor)
        except AuthError as error:
            raise _http(error) from error

    @router.put("/operators/{operator_id}/password", response_model=OperatorOut)
    async def set_password(
        operator_id: UUID,
        body: PasswordIn,
        operator: Annotated[Operator, Depends(admin)],
    ) -> dict[str, Any]:
        try:
            return await store.set_password(
                operator_id, body.password, actor=operator.actor
            )
        except AuthError as error:
            raise _http(error) from error

    return router

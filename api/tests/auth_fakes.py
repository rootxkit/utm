"""An account store for tests that are about something other than sign-in.

It knows a fixed set of bearer tokens and nothing else, so a test that uses
it still goes through the real `require()` dependencies: a route that forgot
its role check is refused here exactly as in production.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from api.auth import Login, Operator, Role

ADMIN = Operator(
    id=UUID("00000000-0000-4000-8000-00000000a0a0"),
    username="test-admin",
    display_name="Test Admin",
    role=Role.ADMIN,
)
VIEWER = Operator(
    id=UUID("00000000-0000-4000-8000-00000000b0b0"),
    username="test-viewer",
    display_name="Test Viewer",
    role=Role.VIEWER,
)
OPERATOR = Operator(
    id=UUID("00000000-0000-4000-8000-00000000c0c0"),
    username="test-operator",
    display_name="Test Operator",
    role=Role.OPERATOR,
)
TOKENS = {"admin-token": ADMIN, "viewer-token": VIEWER, "operator-token": OPERATOR}
ADMIN_HEADERS = {"Authorization": "Bearer admin-token"}
VIEWER_HEADERS = {"Authorization": "Bearer viewer-token"}
OPERATOR_HEADERS = {"Authorization": "Bearer operator-token"}
FEED_SECRET = b"test-feed-secret-0123456789abcdef0123"


class FakeAccounts:
    async def session(self, token: str) -> Operator | None:
        return TOKENS.get(token)

    async def login(self, *args: Any, **kwargs: Any) -> Login:
        raise NotImplementedError

    async def logout(self, token: str) -> bool:
        return token in TOKENS

    async def create_operator(self, **kwargs: Any) -> dict[str, Any]:
        raise NotImplementedError

    async def list_operators(self) -> list[dict[str, Any]]:
        return []

    async def set_role(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        raise NotImplementedError

    async def disable(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        raise NotImplementedError

    async def enable(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        raise NotImplementedError

    async def set_password(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        raise NotImplementedError


def api_kwargs() -> dict[str, Any]:
    """What `create_api_app` needs besides the registry, for such tests."""
    return {
        "auth": FakeAccounts(),
        "feed_secret": FEED_SECRET,
        "feed_ticket_ttl_s": 60.0,
        "cookie_secure": False,
    }

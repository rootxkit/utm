"""`/events` times are refused without a zone and passed on in UTC. S-16.

The registry is a fake that records what the route asked it for: what is
under test is the route's handling of the query string, not the SQL.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from httpx import ASGITransport, AsyncClient

from api.app import create_api_app
from api.registry import FleetRegistry
from api.tests.auth_fakes import VIEWER_HEADERS, api_kwargs


class RecordingRegistry:
    engine = None

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def events(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append(kwargs)
        return []


@pytest.fixture
def registry() -> RecordingRegistry:
    return RecordingRegistry()


@pytest.fixture
async def http(registry: RecordingRegistry) -> AsyncIterator[AsyncClient]:
    app = create_api_app(cast(FleetRegistry, registry), **api_kwargs())
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers=VIEWER_HEADERS,
    ) as client:
        yield client


@pytest.mark.parametrize("field", ["since", "until"])
async def test_a_time_without_a_zone_is_refused(
    http: AsyncClient, registry: RecordingRegistry, field: str
) -> None:
    response = await http.get("/events", params={field: "2026-09-01T12:00:00"})

    assert response.status_code == 422
    assert "zone" in response.text
    assert registry.calls == []


async def test_a_zoned_time_reaches_the_registry_in_utc(
    http: AsyncClient, registry: RecordingRegistry
) -> None:
    """The paired presence: the same instant with a zone is accepted."""
    response = await http.get(
        "/events",
        params={"since": "2026-09-01T16:00:00+04:00", "until": "2026-09-01T13:00:00Z"},
    )

    assert response.status_code == 200, response.text
    [call] = registry.calls
    assert call["since"] == datetime(2026, 9, 1, 12, tzinfo=UTC)
    # Equal instants compare equal across zones; the zone itself must be UTC.
    assert call["since"].tzinfo is UTC
    assert call["until"] == datetime(2026, 9, 1, 13, tzinfo=UTC)


async def test_no_times_pass_as_none(
    http: AsyncClient, registry: RecordingRegistry
) -> None:
    assert (await http.get("/events")).status_code == 200
    assert registry.calls[0]["since"] is None
    assert registry.calls[0]["until"] is None

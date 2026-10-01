"""Source switches against the real relational database. U-15.

Every switch must leave a `source_controls` row and an `events` row with the
admin and the reason, in one transaction, and only then be published. The
bus is a recording fake here; `test_sources_nats.py` drives the real one.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

import pytest
import sqlalchemy as sa
from httpx import ASGITransport, AsyncClient
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from api.app import create_api_app
from api.sources import SourceControlService, SourceControlStore
from api.tests.auth_fakes import ADMIN, ADMIN_HEADERS, VIEWER_HEADERS, api_kwargs
from api.tests.conftest import migrate_relational
from common.sources import RELAY, REMOTE_ID, SourceControlState

pytestmark = pytest.mark.postgres


class RecordingChannel:
    def __init__(self) -> None:
        self.published: list[SourceControlState] = []
        self.fail = False

    async def publish(self, state: SourceControlState) -> None:
        if self.fail:
            raise ConnectionError("no bus")
        self.published.append(state)


def build(service: SourceControlService | None) -> Any:
    return create_api_app(None, sources=service, **api_kwargs())  # type: ignore[arg-type]


@pytest.fixture
def channel() -> RecordingChannel:
    return RecordingChannel()


@pytest.fixture
def service(
    relational_engine: AsyncEngine, channel: RecordingChannel
) -> SourceControlService:
    return SourceControlService(
        store=SourceControlStore(engine=relational_engine), channel=channel
    )


@pytest.fixture
async def client(service: SourceControlService) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(
        transport=ASGITransport(app=build(service)),
        base_url="http://test",
        headers=ADMIN_HEADERS,
    ) as http:
        yield http


def station() -> str:
    return f"station-{uuid4().hex[:8]}"


async def events_for(engine: AsyncEngine, entity_id: str) -> list[Any]:
    async with engine.connect() as connection:
        return list(
            await connection.execute(
                sa.text(
                    "SELECT actor_type, actor_id, event_type, payload FROM events "
                    "WHERE entity_type = 'source' AND entity_id = :id ORDER BY id"
                ),
                {"id": entity_id},
            )
        )


async def test_switching_a_station_off_is_recorded_audited_and_published(
    client: AsyncClient, relational_engine: AsyncEngine, channel: RecordingChannel
) -> None:
    name = station()
    response = await client.put(
        f"/sources/relay/instances/{name}",
        json={"enabled": False, "reason": "operator licence suspended"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["instance_id"] == name
    assert body["enabled"] is False
    assert body["changed_by"] == ADMIN.username

    [event] = await events_for(relational_engine, f"relay/{name}")
    assert (event.actor_type, event.actor_id) == ("operator", str(ADMIN.id))
    assert event.event_type == "source_disabled"
    assert event.payload["reason"] == "operator licence suspended"
    assert event.payload["previous_enabled"] is None

    published = channel.published[-1]
    assert not published.enabled(RELAY, name)
    assert published.enabled(RELAY, station())


async def test_switching_it_back_on_is_a_second_event_and_a_newer_state(
    client: AsyncClient, relational_engine: AsyncEngine, channel: RecordingChannel
) -> None:
    name = station()
    path = f"/sources/relay/instances/{name}"
    await client.put(path, json={"enabled": False, "reason": "test off"})
    off = channel.published[-1]
    response = await client.put(path, json={"enabled": True, "reason": "test on"})

    assert response.status_code == 200
    events = await events_for(relational_engine, f"relay/{name}")
    assert [e.event_type for e in events] == ["source_disabled", "source_enabled"]
    assert events[1].payload["previous_enabled"] is False
    on = channel.published[-1]
    assert on.version > off.version
    assert on.enabled(RELAY, name)


async def test_a_switch_to_the_state_already_held_writes_no_event(
    client: AsyncClient, relational_engine: AsyncEngine
) -> None:
    name = station()
    path = f"/sources/relay/instances/{name}"
    await client.put(path, json={"enabled": False, "reason": "first"})
    again = await client.put(path, json={"enabled": False, "reason": "second"})

    assert again.status_code == 200
    assert again.json()["reason"] == "first"
    assert len(await events_for(relational_engine, f"relay/{name}")) == 1


async def test_a_whole_type_is_switched_and_listed(
    client: AsyncClient, channel: RecordingChannel
) -> None:
    response = await client.put(
        "/sources/remote_id", json={"enabled": False, "reason": "receiver spoofing"}
    )
    assert response.status_code == 200, response.text
    assert response.json()["instance_id"] is None
    assert not channel.published[-1].enabled(REMOTE_ID, "any-receiver")

    listing = (await client.get("/sources")).json()
    assert REMOTE_ID in listing["source_types"]
    assert {
        (c["source_type"], c["instance_id"], c["enabled"]) for c in listing["controls"]
    } >= {(REMOTE_ID, None, False)}

    # Restored, so the other tests in this database see Remote ID on.
    await client.put("/sources/remote_id", json={"enabled": True, "reason": "cleared"})
    assert channel.published[-1].enabled(REMOTE_ID, "any-receiver")


async def test_a_viewer_lists_and_cannot_switch(service: SourceControlService) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=build(service)),
        base_url="http://test",
        headers=VIEWER_HEADERS,
    ) as viewer:
        assert (await viewer.get("/sources")).status_code == 200
        refused = await viewer.put(
            f"/sources/relay/instances/{station()}",
            json={"enabled": False, "reason": "not mine to switch"},
        )
    assert refused.status_code == 403


@pytest.mark.parametrize(
    ("path", "body", "code"),
    [
        (
            "/sources/radar/instances/x",
            {"enabled": False, "reason": "r"},
            "unknown_source_type",
        ),
        (
            "/sources/relay/instances/-bad",
            {"enabled": False, "reason": "r"},
            "invalid_instance_id",
        ),
        (
            "/sources/relay/instances/ok-1",
            {"enabled": False, "reason": "   "},
            "reason_required",
        ),
    ],
)
async def test_a_bad_switch_is_refused_with_a_stable_code(
    client: AsyncClient, path: str, body: dict[str, Any], code: str
) -> None:
    response = await client.put(path, json=body)
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == code


async def test_a_switch_without_a_reason_is_refused(client: AsyncClient) -> None:
    response = await client.put(
        f"/sources/relay/instances/{station()}", json={"enabled": False}
    )
    assert response.status_code == 422


async def test_without_the_bus_nothing_is_changed(
    relational_engine: AsyncEngine,
) -> None:
    service = SourceControlService(
        store=SourceControlStore(engine=relational_engine), channel=None
    )
    name = station()
    async with AsyncClient(
        transport=ASGITransport(app=build(service)),
        base_url="http://test",
        headers=ADMIN_HEADERS,
    ) as http:
        response = await http.put(
            f"/sources/relay/instances/{name}",
            json={"enabled": False, "reason": "no bus"},
        )
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "control_channel_unavailable"
    assert await events_for(relational_engine, f"relay/{name}") == []


async def test_a_failed_publish_is_recorded_reported_and_repaired(
    client: AsyncClient,
    relational_engine: AsyncEngine,
    channel: RecordingChannel,
    service: SourceControlService,
) -> None:
    name = station()
    channel.fail = True
    response = await client.put(
        f"/sources/relay/instances/{name}",
        json={"enabled": False, "reason": "bus down"},
    )

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "not_propagated"
    assert len(await events_for(relational_engine, f"relay/{name}")) == 1
    assert service.publish_failures == 1

    channel.fail = False
    assert await service.publish_current()
    assert not channel.published[-1].enabled(RELAY, name)


async def test_the_published_state_carries_default_deny(
    relational_engine: AsyncEngine, channel: RecordingChannel
) -> None:
    service = SourceControlService(
        store=SourceControlStore(engine=relational_engine),
        channel=channel,
        default_deny=True,
    )
    assert await service.publish_current()
    assert channel.published[-1].default_deny
    assert not channel.published[-1].enabled(RELAY, station())


async def test_two_switches_at_once_are_both_recorded_and_published_in_order(
    service: SourceControlService, channel: RecordingChannel
) -> None:
    first, second = station(), station()
    await asyncio.gather(
        service.switch(
            RELAY, first, enabled=False, reason="a", actor=ADMIN.actor, actor_name="a"
        ),
        service.switch(
            RELAY, second, enabled=False, reason="b", actor=ADMIN.actor, actor_name="b"
        ),
    )
    last = channel.published[-1]
    assert not last.enabled(RELAY, first)
    assert not last.enabled(RELAY, second)
    versions = [state.version for state in channel.published]
    assert versions == sorted(versions)


async def test_the_database_refuses_a_switch_without_a_reason(
    relational_engine: AsyncEngine,
) -> None:
    with pytest.raises(IntegrityError):
        async with relational_engine.begin() as connection:
            await connection.execute(
                sa.text(
                    "INSERT INTO source_controls "
                    "(source_type, instance_id, enabled, reason, changed_by) "
                    "VALUES ('relay', 'x-1', false, '  ', 'someone')"
                )
            )


async def test_the_migration_goes_down_and_up(
    prepared_relational_database: str, relational_engine: AsyncEngine
) -> None:
    await asyncio.to_thread(
        migrate_relational, prepared_relational_database, "0005_uas_registry", down=True
    )
    async with relational_engine.connect() as connection:
        tables: set[str] = set(
            (
                await connection.execute(
                    sa.text(
                        "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
                    )
                )
            ).scalars()
        )
    assert "source_controls" not in tables
    await asyncio.to_thread(migrate_relational, prepared_relational_database, "head")

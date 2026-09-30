"""The API's batched live-state read, against real Redis. S-16.

State is written by the Gateway's own writer, so the batch is checked
against what production writes, and against the one-drone read it
replaces for the drone list.
"""

from __future__ import annotations

import os
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
import redis.asyncio

from api.live import RedisLiveState
from gateway.drone_state import DroneStateRow
from gateway.live_state import LiveState, state_key

pytestmark = pytest.mark.redis


@pytest.fixture
async def client() -> AsyncIterator[Any]:
    url = os.environ.get("REDIS_URL")
    if not url:
        pytest.skip("REDIS_URL is not set; `make up` starts Redis")
    connection = redis.asyncio.from_url(url)
    try:
        yield connection
    finally:
        await connection.aclose()


@pytest.fixture
async def drones(client: Any) -> AsyncIterator[list[UUID]]:
    created = [uuid4() for _ in range(3)]
    try:
        yield created
    finally:
        for drone_id in created:
            await client.delete(state_key(drone_id))


def row(drone_id: UUID, *, armed: bool) -> DroneStateRow:
    return DroneStateRow(
        drone_id=drone_id,
        ts=datetime.now(tz=UTC),
        station_id="live-test",
        armed=armed,
    )


async def test_a_batch_reads_what_one_at_a_time_reads(
    client: Any, drones: list[UUID]
) -> None:
    flying, idle, silent = drones
    writer = LiveState(redis=client, link_timeout_s=30.0, clock=time.time)
    written = await writer.update([row(flying, armed=True), row(idle, armed=False)])
    assert written == {flying: 1, idle: 1}
    reader = RedisLiveState(client)

    batch = await reader.get_many(drones)

    assert batch == {d: await reader.get(d) for d in drones}
    flying_state, idle_state = batch[flying], batch[idle]
    assert flying_state is not None and flying_state["armed"] is True
    assert idle_state is not None and idle_state["armed"] is False
    assert batch[silent] is None


@pytest.mark.parametrize("garbage", [b"{not json", b"[1, 2]", b"\xff\xfe"])
async def test_one_unreadable_state_makes_only_that_drone_unknown(
    client: Any, drones: list[UUID], garbage: bytes
) -> None:
    good, bad, _ = drones
    writer = LiveState(redis=client, link_timeout_s=30.0, clock=time.time)
    assert await writer.update([row(good, armed=True)]) == {good: 1}
    await client.hset(state_key(bad), "state", garbage)

    batch = await RedisLiveState(client).get_many(drones)

    good_state = batch[good]
    assert good_state is not None and good_state["armed"] is True
    assert batch[bad] is None


async def test_an_empty_fleet_asks_nothing(client: Any) -> None:
    assert await RedisLiveState(client).get_many([]) == {}

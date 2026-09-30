"""P1-06 against a real broker: a subscriber receives every position update.

The unit tests in `test_publisher.py` use a recording bus, which proves what
is *sent*. The criterion is about what is *received*, and a real NATS server
is the only thing that can drop, reorder or coalesce. So this drives records
through the ingest pipeline into a real `TelemetryPublisher` on a real
connection, and counts what a separate subscriber gets.

It counts exactly, both ways. Fewer means updates are lost. More would mean a
row was published twice, which would double every position on a map.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator
from typing import Any, cast

import nats
import pytest

from common.bus import round_trip
from gateway.pipeline import IngestPipeline
from gateway.publisher import TelemetryPublisher
from gateway.tests.test_pipeline import (
    DRONE,
    EPOCH,
    FakeResolver,
    FakeWriter,
    heartbeat,
    position,
    record,
)

pytestmark = pytest.mark.nats

BATCHES = 20
POSITIONS_PER_BATCH = 50


def nats_url() -> str:
    url = os.environ.get("NATS_URL")
    if not url:
        pytest.skip("NATS_URL is not set; `make up` starts the broker")
    return url


@pytest.fixture
async def publisher_client() -> AsyncIterator[Any]:
    client = await nats.connect(nats_url())
    try:
        yield client
    finally:
        await client.drain()


@pytest.fixture
async def subscriber_client() -> AsyncIterator[Any]:
    client = await nats.connect(nats_url())
    try:
        yield client
    finally:
        await client.drain()


async def test_a_subscriber_receives_every_position_update(
    publisher_client: Any, subscriber_client: Any
) -> None:
    received: list[dict[str, Any]] = []

    async def collect(message: Any) -> None:
        received.append(json.loads(message.data))

    await subscriber_client.subscribe(f"telemetry.{DRONE}", cb=collect)
    await round_trip(subscriber_client)

    pipeline = IngestPipeline(
        station_id="nats-p1-06",
        resolver=cast(Any, FakeResolver()),
        writer=cast(Any, FakeWriter()),
        publisher=TelemetryPublisher(bus=publisher_client),
    )

    sent = 0
    seq = 0
    for _ in range(BATCHES):
        batch = [record(seq, heartbeat())]
        seq += 1
        for _ in range(POSITIONS_PER_BATCH):
            batch.append(record(seq, position(), offset_ns=seq * 1_000_000))
            seq += 1
        rows = await pipeline.process(EPOCH, batch)
        sent += len(rows)
    await round_trip(publisher_client)

    expected = BATCHES * POSITIONS_PER_BATCH
    assert sent == expected, "the pipeline did not produce one row per position"

    deadline = asyncio.get_running_loop().time() + 5.0
    while len(received) < expected and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.05)
    # A short settle, so a duplicate arriving after the count is reached is
    # still seen.
    await asyncio.sleep(0.2)

    assert len(received) == expected
    timestamps = [message["ts"] for message in received]
    assert timestamps == sorted(timestamps), "updates arrived out of order"
    assert len(set(timestamps)) == expected, "an update was delivered twice"


async def test_a_message_that_is_not_a_position_publishes_nothing(
    publisher_client: Any, subscriber_client: Any
) -> None:
    """The absence half: a HEARTBEAT alone updates no position, and a
    subscriber counting positions must not be sent one."""
    received: list[bytes] = []

    async def collect(message: Any) -> None:
        received.append(message.data)

    await subscriber_client.subscribe(f"telemetry.{DRONE}", cb=collect)
    await round_trip(subscriber_client)
    pipeline = IngestPipeline(
        station_id="nats-p1-06-absent",
        resolver=cast(Any, FakeResolver()),
        writer=cast(Any, FakeWriter()),
        publisher=TelemetryPublisher(bus=publisher_client),
    )

    await pipeline.process(EPOCH, [record(0, heartbeat()), record(1, heartbeat())])
    await round_trip(publisher_client)
    await asyncio.sleep(0.3)

    assert received == []

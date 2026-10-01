"""The source switches over a real broker. U-15.

The writer (`api.sources.NatsControlChannel`) and the followers
(`common.sources`) only meet on NATS, so only a real broker shows that a
change pushed on the subject arrives, that one published before a follower
started is read from the bucket, and that a bucket nobody created yet reads
as "nothing switched off" rather than as an error. Each test has a bucket
and a subject of its own and deletes the bucket after.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

import nats
import pytest

from api.sources import NatsControlChannel
from common.sources import (
    RELAY,
    REMOTE_ID,
    Control,
    SourceControlFollower,
    SourceControlState,
    bucket_reader,
    follow,
)

pytestmark = pytest.mark.nats


def nats_url() -> str:
    url = os.environ.get("NATS_URL")
    if not url:
        pytest.skip("NATS_URL is not set; `make up` starts the broker")
    return url


@pytest.fixture
async def client() -> AsyncIterator[Any]:
    connection = await nats.connect(nats_url())
    try:
        yield connection
    finally:
        await connection.drain()


@pytest.fixture
async def names(client: Any) -> AsyncIterator[tuple[str, str]]:
    suffix = uuid4().hex[:12]
    bucket, subject = (
        f"source_control_u15_test_{suffix}",
        f"control.sources.test.{suffix}",
    )
    try:
        yield bucket, subject
    finally:
        # A test that never created it has nothing to delete.
        with contextlib.suppress(Exception):
            await client.jetstream().delete_key_value(bucket)


def switched_off(
    source_type: str, instance_id: str | None, version: int
) -> SourceControlState:
    return SourceControlState(
        version=version,
        controls=(
            Control(
                source_type=source_type,
                instance_id=instance_id,
                enabled=False,
                reason="test",
                changed_by="test-admin",
                changed_at="2026-10-01T12:00:00+00:00",
            ),
        ),
    )


async def eventually(condition: Any, *, timeout_s: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_s
    while not condition():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.02)


async def test_a_bucket_nobody_created_reads_as_nothing_published(
    client: Any, names: tuple[str, str]
) -> None:
    bucket, _ = names
    follower = SourceControlFollower(read=bucket_reader(client, bucket))

    assert await follower.refresh()
    assert follower.enabled(RELAY, "station-1")
    assert follower.read_failures == 0


async def test_a_pushed_change_reaches_a_follower_without_a_poll(
    client: Any, names: tuple[str, str]
) -> None:
    bucket, subject = names
    writer = NatsControlChannel(client=client, bucket=bucket, subject=subject)
    await writer.ensure_bucket()
    # A poll interval far beyond the test: only the push can deliver it.
    follower = SourceControlFollower(read=bucket_reader(client, bucket), poll_s=3600)
    subscription = await follow(client, follower, subject=subject)
    try:
        await writer.publish(switched_off(RELAY, "station-1", version=1))
        await eventually(lambda: not follower.enabled(RELAY, "station-1"))
    finally:
        await follower.stop()
        await subscription.unsubscribe()


async def test_a_follower_started_later_reads_the_bucket(
    client: Any, names: tuple[str, str]
) -> None:
    bucket, subject = names
    writer = NatsControlChannel(client=client, bucket=bucket, subject=subject)
    await writer.ensure_bucket()
    await writer.publish(switched_off(REMOTE_ID, None, version=7))

    follower = SourceControlFollower(read=bucket_reader(client, bucket), poll_s=3600)
    subscription = await follow(client, follower, subject=subject)
    try:
        assert follower.state.version == 7
        assert not follower.enabled(REMOTE_ID, "rx-1")
    finally:
        await follower.stop()
        await subscription.unsubscribe()


async def test_ensuring_the_bucket_twice_keeps_what_it_holds(
    client: Any, names: tuple[str, str]
) -> None:
    bucket, subject = names
    writer = NatsControlChannel(client=client, bucket=bucket, subject=subject)
    await writer.ensure_bucket()
    await writer.publish(switched_off(RELAY, "station-2", version=3))
    await writer.ensure_bucket()

    payload = await bucket_reader(client, bucket)()
    assert payload is not None
    assert SourceControlState.from_json(payload).version == 3

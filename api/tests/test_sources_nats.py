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
import logging
import os
from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

import nats
import pytest
import sqlalchemy as sa
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine

from api.app import create_api_app
from api.sources import NatsControlChannel, SourceControlService, SourceControlStore
from api.tests.auth_fakes import ADMIN_HEADERS, api_kwargs
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
        await writer.announce(switched_off(RELAY, "station-1", version=1))
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
    await writer.store(switched_off(REMOTE_ID, None, version=7))

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
    await writer.store(switched_off(RELAY, "station-2", version=3))
    await writer.ensure_bucket()

    payload = await bucket_reader(client, bucket)()
    assert payload is not None
    assert SourceControlState.from_json(payload).version == 3


# --- a broker without JetStream ----------------------------------------------


class NoJetStream:
    """A connection to the real broker whose JetStream API nobody serves.

    That is what a NATS started without `-js` looks like to a client (CI's
    service container did exactly this): every `$JS.API` request fails,
    with no responders or 503. Core publish and subscribe still work.
    """

    def __init__(self, client: Any) -> None:
        self._client = client

    def jetstream(self, **_: Any) -> Any:
        return self._client.jetstream(prefix="$JS.U15NOJETSTREAM.API")

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)


async def test_without_jetstream_the_bucket_cannot_be_written(
    client: Any, names: tuple[str, str]
) -> None:
    """The failure is raised, never swallowed: the API refuses on it."""
    bucket, subject = names
    writer = NatsControlChannel(
        client=NoJetStream(client), bucket=bucket, subject=subject
    )
    with pytest.raises(Exception):  # noqa: B017 - whichever nats-py raises
        await writer.store(switched_off(RELAY, "station-1", version=1))


async def test_without_jetstream_a_new_follower_enables_everything_and_says_so(
    client: Any, names: tuple[str, str], caplog: pytest.LogCaptureFixture
) -> None:
    bucket, subject = names
    follower = SourceControlFollower(
        read=bucket_reader(NoJetStream(client), bucket),
        poll_s=3600,
        start_attempts=2,
        start_backoff_s=0.01,
    )
    with caplog.at_level(logging.WARNING, logger="common.sources"):
        subscription = await follow(client, follower, subject=subject)
    try:
        # Never fails closed: nothing known, so nothing is switched off.
        assert follower.enabled(RELAY, "station-1")
        assert follower.enabled(REMOTE_ID, "rx-1")
        assert follower.state_unknown
        assert follower.status()["source_control_state_unknown"] == 1
        assert follower.status()["source_control_read_ok"] == 0
        assert follower.read_failures == 2
        levels = {r.getMessage(): r.levelno for r in caplog.records}
        assert (
            levels["could not read the source control state; keeping what is held"]
            == logging.ERROR
        )
        assert (
            levels[
                "source control state unknown at start; every source is enabled "
                "until it can be read"
            ]
            == logging.WARNING
        )
    finally:
        await follower.stop()
        await subscription.unsubscribe()


async def test_when_jetstream_goes_away_a_follower_keeps_what_it_holds(
    client: Any, names: tuple[str, str]
) -> None:
    bucket, subject = names
    writer = NatsControlChannel(client=client, bucket=bucket, subject=subject)
    await writer.ensure_bucket()
    await writer.store(switched_off(RELAY, "station-1", version=4))
    follower = SourceControlFollower(read=bucket_reader(client, bucket), poll_s=3600)
    await follower.start()
    try:
        assert not follower.enabled(RELAY, "station-1")

        follower.read = bucket_reader(NoJetStream(client), bucket)
        assert not await follower.refresh()

        assert not follower.enabled(RELAY, "station-1")
        assert follower.state.version == 4
        assert follower.status()["source_control_read_ok"] == 0
        assert follower.status()["source_control_state_unknown"] == 0
    finally:
        await follower.stop()


@pytest.mark.postgres
async def test_without_jetstream_a_switch_is_refused_and_nothing_recorded(
    client: Any, names: tuple[str, str], relational_engine: AsyncEngine
) -> None:
    bucket, subject = names
    service = SourceControlService(
        store=SourceControlStore(engine=relational_engine),
        channel=NatsControlChannel(
            client=NoJetStream(client), bucket=bucket, subject=subject
        ),
    )
    instance = f"nojs-{uuid4().hex[:8]}"
    app = create_api_app(None, sources=service, **api_kwargs())  # type: ignore[arg-type]
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", headers=ADMIN_HEADERS
    ) as http:
        response = await http.put(
            f"/sources/relay/instances/{instance}",
            json={"enabled": False, "reason": "no JetStream"},
        )
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "control_channel_unavailable"
    async with relational_engine.connect() as connection:
        written: int = (
            await connection.execute(
                sa.text(
                    "SELECT (SELECT count(*) FROM source_controls WHERE instance_id = :i)"
                    " + (SELECT count(*) FROM events WHERE entity_id = :e)"
                ),
                {"i": instance, "e": f"relay/{instance}"},
            )
        ).scalar_one()
    assert written == 0


@pytest.mark.postgres
async def test_with_jetstream_a_switch_is_recorded_and_reaches_a_follower(
    client: Any, names: tuple[str, str], relational_engine: AsyncEngine
) -> None:
    """The paired presence for the refusal above: the same switch, through
    the real bucket and subject, recorded and followed."""
    bucket, subject = names
    channel = NatsControlChannel(client=client, bucket=bucket, subject=subject)
    await channel.ensure_bucket()
    service = SourceControlService(
        store=SourceControlStore(engine=relational_engine), channel=channel
    )
    follower = SourceControlFollower(read=bucket_reader(client, bucket), poll_s=3600)
    subscription = await follow(client, follower, subject=subject)
    instance = f"js-{uuid4().hex[:8]}"
    try:
        app = create_api_app(None, sources=service, **api_kwargs())  # type: ignore[arg-type]
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            headers=ADMIN_HEADERS,
        ) as http:
            response = await http.put(
                f"/sources/relay/instances/{instance}",
                json={"enabled": False, "reason": "JetStream on"},
            )
        assert response.status_code == 200, response.text
        await eventually(lambda: not follower.enabled(RELAY, instance))
    finally:
        await follower.stop()
        await subscription.unsubscribe()


@pytest.mark.postgres
async def test_a_corrupt_bucket_is_overwritten_and_followers_survive_it(
    client: Any,
    names: tuple[str, str],
    relational_engine: AsyncEngine,
    caplog: pytest.LogCaptureFixture,
) -> None:
    bucket, subject = names
    channel = NatsControlChannel(client=client, bucket=bucket, subject=subject)
    await channel.ensure_bucket()
    await channel.store(switched_off(RELAY, "station-1", version=1))
    follower = SourceControlFollower(read=bucket_reader(client, bucket), poll_s=3600)
    await follower.start()
    try:
        assert not follower.enabled(RELAY, "station-1")
        kv = await client.jetstream().key_value(bucket)
        await kv.put("state", b"{not json")

        # The follower keeps what it held and counts the corrupt value.
        assert not await follower.refresh()
        assert not follower.enabled(RELAY, "station-1")
        assert follower.ignored_malformed == 1

        # The API reads it as nothing, says so, and overwrites it.
        with caplog.at_level(logging.ERROR, logger="api.sources"):
            assert await channel.load() is None
        assert any("does not parse" in r.getMessage() for r in caplog.records)
        service = SourceControlService(
            store=SourceControlStore(engine=relational_engine), channel=channel
        )
        assert await service.publish_current()
        payload = await bucket_reader(client, bucket)()
        assert payload is not None
        SourceControlState.from_json(payload)
        assert await follower.refresh()
        assert follower.status()["source_control_read_ok"] == 1
    finally:
        await follower.stop()

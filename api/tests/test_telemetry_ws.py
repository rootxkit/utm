"""The console feed, through a real NATS and a real WebSocket.

Marked `nats` because it needs the broker from `make up`. A fake bus would
prove the fan-out logic and nothing about whether the subject wildcards match
what the Gateway actually publishes - which is precisely the seam where a
console shows an empty map against a working pipeline.

The test that matters is `test_a_published_row_reaches_a_browser`: it publishes
exactly what `gateway/publisher.py` publishes and asserts a browser receives
it, so the two halves cannot drift apart silently.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import nats
import pytest
from httpx import ASGITransport, AsyncClient

from api.telemetry_ws import ConsoleHub, create_app
from common.bus import round_trip
from gateway.drone_state import DroneStateRow
from gateway.publisher import TelemetryPublisher
from gateway.station_state import LinkState

FEED_SECRET = b"test-feed-secret-0123456789abcdef0123"

pytestmark = pytest.mark.nats

NOON = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)


def nats_url() -> str:
    url = os.environ.get("NATS_URL")
    if not url:
        pytest.skip("NATS_URL is not set; `make up` starts the broker")
    return url


@pytest.fixture
async def bus() -> AsyncIterator[Any]:
    client = await nats.connect(nats_url())
    try:
        yield client
    finally:
        await client.drain()


def a_row() -> DroneStateRow:
    return DroneStateRow(
        drone_id=uuid4(),
        ts=NOON,
        station_id="tbilisi-base-1",
        lat_deg=41.7151,
        lon_deg=44.8271,
        alt_amsl_m=450.0,
        alt_above_home_m=60.0,
        heading_deg=90.0,
        batt_pct=87.0,
    )


def assert_subscribed(app: Any) -> None:
    """Fail on a bus the app never reached, rather than on the wait that follows.

    `create_app` logs and carries on when NATS is unreachable, because a
    console that loads and shows nothing arriving is more useful to an operator
    than one that will not load. In a test that same tolerance is a trap: a
    failed connection produces no subscription, no messages, and a five second
    timeout reporting "no station message" - which points at delivery when the
    fault was the connection.

    Checked once, before anything is awaited, so the two cannot be confused.
    """
    client = app.state.nats
    assert client is not None, (
        "the app did not connect to NATS, so nothing was ever subscribed; "
        "check NATS_URL and that the broker is running"
    )
    assert client.is_connected, "the app's NATS connection dropped before the publish"


async def drain_until(
    received: list[dict[str, Any]], kind: str, limit_s: float = 5.0
) -> dict[str, Any]:
    started = asyncio.get_running_loop().time()
    deadline = started + limit_s
    while asyncio.get_running_loop().time() < deadline:
        for message in received:
            if message["kind"] == kind:
                return message
        await asyncio.sleep(0.05)
    waited = asyncio.get_running_loop().time() - started
    raise AssertionError(
        f"no {kind!r} message within {waited:.2f}s; saw {[m['kind'] for m in received]}"
    )


# --- the hub, without a broker ---------------------------------------------


def test_the_hub_labels_a_message_by_its_subject() -> None:
    hub = ConsoleHub()
    queue = hub.attach()

    hub.broadcast("telemetry.abc", json.dumps({"lat_deg": 41.0}).encode())

    message = json.loads(queue.get_nowait())
    assert message["kind"] == "telemetry"
    assert message["name"] == "abc"
    assert message["data"]["lat_deg"] == 41.0


def test_a_slow_console_drops_the_oldest_not_the_newest() -> None:
    """A map wants the current position, not a backlog of old ones.

    An unbounded queue would grow in the server's memory for as long as one
    browser stays slow, which is a fleet-wide problem caused by one laptop.
    """
    hub = ConsoleHub()
    queue = hub.attach()

    for index in range(300):
        hub.broadcast("telemetry.abc", json.dumps({"n": index}).encode())

    assert queue.qsize() == 256
    newest = json.loads(queue.get_nowait())
    # The oldest were evicted, so what remains ends at the newest.
    assert newest["data"]["n"] > 0


def test_an_undecodable_payload_is_dropped_not_forwarded() -> None:
    """A browser must not have to defend against malformed JSON."""
    hub = ConsoleHub()
    queue = hub.attach()

    hub.broadcast("telemetry.abc", b"{not json")

    assert queue.empty()


def test_a_detached_console_stops_receiving() -> None:
    hub = ConsoleHub()
    queue = hub.attach()
    hub.detach(queue)

    hub.broadcast("telemetry.abc", json.dumps({"n": 1}).encode())

    assert queue.empty()


# --- through the broker ----------------------------------------------------


async def test_a_published_row_reaches_a_browser(bus: Any) -> None:
    """The seam test: Gateway publishes, browser receives.

    Published through `TelemetryPublisher`, the same code the ingest path
    uses, so a change to the subject scheme on either side fails here rather
    than showing an empty map.
    """
    app = create_app(nats_url(), feed_secret=FEED_SECRET)
    received: list[dict[str, Any]] = []

    async with (
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test"),
        app.router.lifespan_context(app),
    ):
        assert_subscribed(app)
        queue = app.state.hub.attach()

        async def collect() -> None:
            while True:
                received.append(json.loads(await queue.get()))

        collector = asyncio.create_task(collect())
        try:
            row = a_row()
            await TelemetryPublisher(bus=bus).publish_row(row)
            # `publish` buffers. A round trip, not `flush()`, is what confirms
            # the server has it (common/bus.py).
            await round_trip(bus)
            message = await drain_until(received, "telemetry")
        finally:
            collector.cancel()

    assert message["name"] == str(row.drone_id)
    assert message["data"]["lat_deg"] == pytest.approx(41.7151)
    assert message["data"]["alt_above_home_m"] == pytest.approx(60.0)
    assert "alt_agl_m" not in message["data"]


async def test_station_state_reaches_a_browser_with_the_distinction(
    bus: Any,
) -> None:
    """An unreachable station must not arrive looking like data loss."""
    app = create_app(nats_url(), feed_secret=FEED_SECRET)
    received: list[dict[str, Any]] = []

    async with app.router.lifespan_context(app):
        assert_subscribed(app)
        queue = app.state.hub.attach()

        async def collect() -> None:
            while True:
                received.append(json.loads(await queue.get()))

        collector = asyncio.create_task(collect())
        try:
            await TelemetryPublisher(bus=bus).publish_station(
                "tbilisi-base-1", LinkState.UNREACHABLE, last_datagram_age_ms=40
            )
            await round_trip(bus)
            message = await drain_until(received, "station")
        finally:
            collector.cancel()

    assert message["data"]["state"] == "unreachable"
    assert message["data"]["data_is_lost"] is False
    assert message["data"]["buffering"] is True


# --- the page --------------------------------------------------------------


async def test_the_map_page_is_served() -> None:
    app = create_app(nats_url(), feed_secret=FEED_SECRET)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        response = await client.get("/")

    assert response.status_code == 200
    assert "maplibre" in response.text.lower()


async def test_the_page_carries_both_languages() -> None:
    """CLAUDE.md: user-facing strings go through i18n from day one.

    Checked rather than assumed, because "we will add i18n later" is how a
    page ends up with a hundred hardcoded strings.
    """
    app = create_app(nats_url(), feed_secret=FEED_SECRET)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        page = (await client.get("/")).text

    assert "ka:" in page
    assert "en:" in page
    # The station states the operator must be able to tell apart.
    for state in ("healthy", "radio_silent", "unreachable", "data_lost"):
        assert state in page


async def test_the_page_handles_only_the_console_message_kinds() -> None:
    """P1-08 is minimal on purpose; corridors are P5 and the console is P6.

    Asserted on the message kinds the page branches on rather than by
    searching for the word "corridor" - the first version of this test failed
    against the page's own comment explaining that it does not render them,
    which measured the prose and not the behaviour.
    """
    app = create_app(nats_url(), feed_secret=FEED_SECRET)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        page = (await client.get("/")).text

    handled = set(re.findall(r'message\.kind === "(\w+)"', page))
    # `alert` since P6-03: the airspace monitor's alerts panel.
    assert handled == {"telemetry", "station", "events", "alert"}

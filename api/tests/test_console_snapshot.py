"""What a console sees when it attaches after everything already happened.

No broker and no socket: `ConsoleHub` is a plain object, and the question here
is purely about what it remembers. `test_telemetry_ws.py` covers the wiring
through a real NATS; this covers the half that was missing, and it is cheap
enough to run everywhere.

The bug these were written for: the console showed "Stations: none" beside a
station that was connected and streaming. Two separate causes, and this file
owns the second one - the Gateway now publishes station state on a timer, but a
browser opened at 14:03 still could not be served by anything published at
14:02. The bus carries the present and does not remember it, so the hub does.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from httpx import ASGITransport, AsyncClient

from api.telemetry_ws import (
    CONNECT_TIMEOUT_S,
    SNAPSHOT_MAX_ENTRIES,
    ConsoleHub,
    create_app,
)
from tests.ports import free_tcp_port

FEED_SECRET = b"test-feed-secret-0123456789abcdef0123"


def publish(hub: ConsoleHub, subject: str, **body: Any) -> None:
    hub.broadcast(subject, json.dumps(body).encode("utf-8"))


def drain(queue: asyncio.Queue[str]) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    while not queue.empty():
        messages.append(json.loads(queue.get_nowait()))
    return messages


# --- a late console is served ----------------------------------------------


def test_a_station_published_before_the_console_attached_is_replayed() -> None:
    """The failing case from the SITL run, reduced to its smallest form."""
    hub = ConsoleHub()
    publish(hub, "station.tbilisi-base-1", station_id="tbilisi-base-1", state="healthy")

    queue = hub.attach()

    assert [(m["kind"], m["name"]) for m in drain(queue)] == [
        ("station", "tbilisi-base-1")
    ]


def test_a_drone_published_before_the_console_attached_is_replayed() -> None:
    """A drone holding station stops emitting changes, not messages.

    Without the snapshot, reopening the browser over a stationary fleet draws
    an empty map until something moves.
    """
    hub = ConsoleHub()
    publish(hub, "telemetry.0b63df96", drone_id="0b63df96", lat_deg=41.7)

    assert [m["name"] for m in drain(hub.attach())] == ["0b63df96"]


def test_the_replay_is_the_latest_state_and_not_a_history() -> None:
    """One message per source. A console wants the present, not a backlog."""
    hub = ConsoleHub()
    publish(hub, "telemetry.d1", drone_id="d1", alt_amsl_m=100.0)
    publish(hub, "telemetry.d1", drone_id="d1", alt_amsl_m=140.0)

    replayed = drain(hub.attach())

    assert len(replayed) == 1
    assert replayed[0]["data"]["alt_amsl_m"] == 140.0


def test_every_station_and_drone_seen_is_replayed() -> None:
    hub = ConsoleHub()
    publish(hub, "station.tbilisi-base-1", station_id="tbilisi-base-1")
    publish(hub, "telemetry.d1", drone_id="d1")
    publish(hub, "telemetry.d2", drone_id="d2")

    assert {(m["kind"], m["name"]) for m in drain(hub.attach())} == {
        ("station", "tbilisi-base-1"),
        ("telemetry", "d1"),
        ("telemetry", "d2"),
    }


def test_each_console_gets_its_own_copy_of_the_snapshot() -> None:
    """The second browser must not be served an already-drained queue."""
    hub = ConsoleHub()
    publish(hub, "station.tbilisi-base-1", station_id="tbilisi-base-1")

    first = hub.attach()
    second = hub.attach()
    drain(first)

    assert len(drain(second)) == 1


# --- and what is deliberately not replayed ---------------------------------


def test_an_event_is_not_replayed() -> None:
    """The paired absence test, and the reason `events` is excluded.

    Events are not superseded by the next one: two unclaimed sources are two
    facts, and keeping the newest per subject would silently turn them into
    one. Showing a console one of them would be worse than showing it none,
    because it would look complete. P6-01 gives events a queried history.
    """
    hub = ConsoleHub()
    publish(hub, "events.unclaimed_source", sysid=201, compid=1)
    publish(hub, "events.unclaimed_source", sysid=202, compid=1)

    assert drain(hub.attach()) == []


def test_an_event_still_reaches_a_console_that_is_already_attached() -> None:
    """Excluded from the snapshot is not excluded from the feed.

    The pair to the test above: without this, dropping events from the
    snapshot could have quietly dropped them altogether and nothing would
    have said so.
    """
    hub = ConsoleHub()
    queue = hub.attach()

    publish(hub, "events.unclaimed_source", sysid=201, compid=1)

    assert [m["kind"] for m in drain(queue)] == ["events"]


# --- the snapshot stays bounded --------------------------------------------


def test_an_active_alert_is_replayed_to_a_console_that_attaches_later() -> None:
    """P6-03: an alert raised before the console opened is still showing."""
    hub = ConsoleHub()
    publish(hub, "alert.conflict:a:b", state="raised", severity="critical")

    replayed = drain(hub.attach())

    assert [m["kind"] for m in replayed] == ["alert"]
    assert replayed[0]["data"]["state"] == "raised"


def test_a_cleared_alert_is_not_replayed() -> None:
    """The paired case: clearing removes it, rather than replaying 'cleared'
    forever or, worse, leaving 'raised' as the last word."""
    hub = ConsoleHub()
    publish(hub, "alert.conflict:a:b", state="raised", severity="critical")
    publish(hub, "alert.conflict:a:b", state="cleared", severity="critical")

    assert drain(hub.attach()) == []


def test_source_activity_is_replayed_latest_first_per_type() -> None:
    """U-15: a console opened after a receiver was switched off still sees
    it as disabled, from the adapter's latest word on its type."""
    hub = ConsoleHub()
    publish(hub, "source.remote_id", source_type="remote_id", enabled=True)
    publish(hub, "source.remote_id", source_type="remote_id", enabled=False)
    publish(hub, "source.relay", source_type="relay", enabled=True)

    replayed = drain(hub.attach())

    assert [(m["kind"], m["name"], m["data"]["enabled"]) for m in replayed] == [
        ("source", "remote_id", False),
        ("source", "relay", True),
    ]


def test_a_cleared_alert_still_reaches_a_console_already_attached() -> None:
    hub = ConsoleHub()
    queue = hub.attach()
    publish(hub, "alert.conflict:a:b", state="raised")
    publish(hub, "alert.conflict:a:b", state="cleared")

    assert [m["data"]["state"] for m in drain(queue)] == ["raised", "cleared"]


def test_the_snapshot_evicts_the_least_recently_seen_source() -> None:
    """One entry per source seen, for the life of the process, is a leak.

    Small over a fleet; not small over months of retired airframes, and an
    unbounded cache in the console feed is the kind of slow growth nobody
    attributes to the right component.
    """
    hub = ConsoleHub()
    for n in range(SNAPSHOT_MAX_ENTRIES + 10):
        publish(hub, f"telemetry.d{n}", drone_id=f"d{n}")

    assert len(hub.snapshot) == SNAPSHOT_MAX_ENTRIES
    # The oldest went; the newest stayed.
    assert ("telemetry", "d0") not in hub.snapshot
    assert ("telemetry", f"d{SNAPSHOT_MAX_ENTRIES + 9}") in hub.snapshot


def test_a_source_that_keeps_reporting_is_not_evicted() -> None:
    """Eviction is by last sighting, not by first.

    A drone that has been airborne all day must not be the one dropped
    because it connected first.
    """
    hub = ConsoleHub()
    publish(hub, "telemetry.oldest", drone_id="oldest")
    for n in range(SNAPSHOT_MAX_ENTRIES + 10):
        publish(hub, f"telemetry.d{n}", drone_id=f"d{n}")
        publish(hub, "telemetry.oldest", drone_id="oldest")

    assert ("telemetry", "oldest") in hub.snapshot


# --- the live path is unchanged --------------------------------------------


def test_a_live_message_still_reaches_an_attached_console() -> None:
    hub = ConsoleHub()
    queue = hub.attach()

    publish(hub, "telemetry.d1", drone_id="d1")

    assert [m["name"] for m in drain(queue)] == ["d1"]


def test_an_undecodable_payload_is_dropped_rather_than_remembered() -> None:
    """A malformed message must not become a permanent part of the snapshot."""
    hub = ConsoleHub()
    hub.broadcast("station.tbilisi-base-1", b"{not json")

    assert hub.snapshot == {}
    assert drain(hub.attach()) == []


# --- starting up without a broker ------------------------------------------
#
# `create_app` promises that a console with an unreachable bus "will serve but
# stay empty", on the grounds that an operator learns more from a page saying
# nothing is arriving than from a page that will not load.
#
# That path had never been executed. `nats.connect` does not fail fast: with
# library defaults it retries the initial connection roughly sixty times, a few
# seconds apart, so the promise was false by minutes. It was found by trying to
# point a test at a dead port and watching the test hang.


async def test_the_console_starts_when_the_bus_is_unreachable() -> None:
    """The presence test for the degradation, and it must be quick.

    The bound is generous against a slow CI runner but far below the library's
    own retry budget, which is the thing being ruled out.
    """
    # The production default deliberately, because the bound is the claim.
    app = create_app(f"nats://127.0.0.1:{free_tcp_port()}", feed_secret=FEED_SECRET)

    started = time.monotonic()
    async with app.router.lifespan_context(app):
        elapsed = time.monotonic() - started
        assert app.state.nats is None
        assert app.state.hub is not None

    assert elapsed < CONNECT_TIMEOUT_S + 10.0, (
        f"startup took {elapsed:.1f}s against a dead broker; the initial "
        "connect is not bounded"
    )


async def test_a_console_can_attach_with_no_bus_and_is_simply_empty() -> None:
    """The point of degrading rather than refusing to start.

    A browser must still get a WebSocket, so the page loads and shows an empty
    fleet, instead of failing to connect and showing nothing at all.
    """
    app = create_app(f"nats://127.0.0.1:{free_tcp_port()}", feed_secret=FEED_SECRET)

    async with app.router.lifespan_context(app):
        queue = app.state.hub.attach()

        assert queue.empty()
        assert len(app.state.hub.clients) == 1


async def test_health_reports_the_bus_as_disconnected_rather_than_lying() -> None:
    """An operator has to be able to see that the bus is the problem."""
    app = create_app(
        f"nats://127.0.0.1:{free_tcp_port()}",
        feed_secret=FEED_SECRET,
        connect_timeout_s=0.5,
    )

    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        response = await client.get("/healthz")

    assert response.status_code == 200
    assert response.json()["bus_connected"] is False

"""The console's live feed: one WebSocket, fed by NATS.

P1-08. The browser opens one connection and receives everything it needs to
draw a map: drone positions, station link state, and unclaimed sources.

**This never reads the database.** It subscribes to `telemetry.*`, `station.*`
and `events.*` and forwards what arrives. A map open on ten drones at 4 Hz is
forty messages a second; served from a hypertable that would be forty queries a
second, and a browser refresh would become a query storm. The database holds
the flight record. The bus carries the present.

## Who may watch (P6-08)

Only a browser holding a feed ticket: a short-lived statement signed by the
API when an operator signed in, carried in the `courier_feed` cookie and
checked here with a shared secret, without a database. Without one, or when
it runs out, the socket closes with 4401 and the page fetches a new ticket
from the API, which checks the session. A revoked session therefore keeps
the feed for at most one ticket's lifetime.

## What the console is told, and what it is not left to infer

Every station message carries `data_is_lost` and `buffering` as explicit
fields. The console does not decide what an `unreachable` station means, and it
certainly does not infer link health from telemetry having stopped - which is
what would render the 3-to-25 second window where the Gateway and the relay
disagree as data loss. Spec §9, and P6-03's warning that a pilot who learns the
alerts overstate things will discount the one that does not.

## Scope

`web-pilot/` is the real console and is scaffolded in P6-01. The page served
here is the minimal one P1-08 asks for - one marker per drone, heading,
battery, link age - and exists to prove the chain end to end. It is not the
console and should not grow into one.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import nats
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from nats.aio.client import Client as NatsClient
from nats.aio.msg import Msg

from api.assets import STATIC, mount_map_assets
from api.auth import FEED_COOKIE, verify_feed_ticket, wall_clock_s
from common import get_logger
from common.bus import round_trip

_log = get_logger(__name__)

# Everything the console needs, and nothing it does not. `>` would also carry
# subjects added later for services that are not a browser.
SUBSCRIBED_SUBJECTS = ("telemetry.*", "station.*", "events.*", "alert.*")

# Subjects that carry *state* - the latest message on one of these replaces the
# previous one, so the latest is a complete picture and is worth replaying to a
# console that attaches later.
#
# `events` is deliberately not in here. An event is not superseded by the next
# one: two unclaimed sources are two facts, and keeping only the newest per
# subject would quietly turn them into one. A console that attaches after an
# event was published does not see it, and that is a real gap - P6-01 fixes it
# with a queried event history, not by pretending the bus remembers.
#
# `alert` is state too (P6-03): an alert is active until the airspace monitor
# publishes it cleared, and a console opened after it was raised must still
# see it. A cleared alert is removed from the snapshot rather than replayed.
SNAPSHOT_KINDS = frozenset({"telemetry", "station", "alert"})

# A bound on the snapshot, which is otherwise one entry per distinct drone and
# station the process has ever seen. In a fleet that is small; over a long
# uptime with retired airframes it is not, and an unbounded cache in the
# console feed is exactly the kind of slow leak nobody attributes correctly.
SNAPSHOT_MAX_ENTRIES = 512

# How long startup may spend trying to reach the bus before giving up and
# serving anyway.
#
# This exists because `nats.connect` does not fail fast: with library defaults
# it retries the initial connection about sixty times, several seconds apart,
# so a console started while the broker is down hangs for minutes instead of
# loading. The `except` below, and the promise in its comment, had never run.
# Bounding startup here leaves the client's own reconnect policy untouched for
# the case that matters - a broker that goes away *after* a good connection.
CONNECT_TIMEOUT_S = 5.0

# P6-08. The feed closes with this when the browser has no valid ticket, or
# its ticket has run out. In the 4000-4999 range reserved for applications.
CLOSE_SIGN_IN_REQUIRED = 4401
# S-16. Sent before the handshake completes, which the server turns into an
# HTTP 403: a page on a host that may not open the feed.
CLOSE_POLICY_VIOLATION = 1008


def normalise_origin(origin: str) -> str:
    return origin.strip().rstrip("/").lower()


def origin_allowed(
    origin: str | None,
    host: str | None,
    allowed: frozenset[str],
    *,
    feed_secure: bool = False,
) -> bool:
    """May a page from `origin` open the feed served as `host`? S-16.

    `feed_secure` is whether the feed was reached over TLS (`wss`), as
    uvicorn reports it: behind the TLS front that is the front's
    `X-Forwarded-Proto`, honoured for a trusted proxy (`FORWARDED_ALLOW_IPS`).

    A browser always sends `Origin` on a WebSocket handshake, and a page on
    another site can open one to any address, carrying this site's cookies
    unless SameSite stops them. So the origin is checked:

    - no `Origin` at all: not a browser, and so not a page acting on a
      signed-in operator's cookies. Allowed; the feed ticket still decides.
    - an origin in `allowed` (`CONSOLE_ALLOWED_ORIGINS`): allowed.
    - otherwise the page's host name must be the feed's own. The port is not
      compared: behind the TLS front the page and the feed share one origin,
      but in development the API serves its pages on one port and the feed
      listens on another, and cookies - the ticket included - are shared
      across ports on a host anyway. The scheme is compared: a feed served
      over TLS refuses a page served without it (`http` on the same host
      is anyone who can sit on the path), and a plain `ws` feed, as in
      development, takes `http` pages. Any other scheme is refused.
    - an origin that does not parse is refused, never an error.
    """
    if origin is None:
        return True
    normalised = normalise_origin(origin)
    if normalised in allowed:
        return True
    try:
        page = urlsplit(normalised)
        page_host = page.hostname
        feed_host = urlsplit(f"//{host}").hostname if host else None
    except ValueError:
        # e.g. `https://[evil`: an unclosed IPv6 bracket.
        return False
    # A plain feed also takes an `https` page: that is a TLS front whose
    # `X-Forwarded-Proto` is not trusted, and refusing it would take the
    # console down over a setting rather than protect anything.
    schemes = {"https"} if feed_secure else {"http", "https"}
    return page.scheme in schemes and page_host is not None and page_host == feed_host


@dataclass
class ConsoleHub:
    """Fans NATS messages out to every connected browser.

    One NATS subscription set for the whole process, not one per browser: ten
    consoles open on the same fleet must not multiply the load on the bus.
    """

    clients: set[asyncio.Queue[str]] = field(default_factory=set)
    # The latest state message per `(kind, name)`, replayed to each new
    # console. Insertion-ordered, so the oldest entry is the one evicted.
    snapshot: dict[tuple[str, str], str] = field(default_factory=dict)

    def attach(self) -> asyncio.Queue[str]:
        # Bounded. A browser on a slow link must not grow an unbounded backlog
        # in the server's memory; it is dropped from instead, because stale
        # positions have no value once newer ones exist.
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=256)
        # Current state first, before any live message. Without this a console
        # sees only what changes after it connects: a station that went healthy
        # a minute ago is invisible, and so is a drone that is holding station
        # and not moving. The bus carries the present, but it does not
        # remember it, so the hub does.
        for message in list(self.snapshot.values()):
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(message)
        self.clients.add(queue)
        return queue

    def detach(self, queue: asyncio.Queue[str]) -> None:
        self.clients.discard(queue)

    def broadcast(self, subject: str, payload: bytes) -> None:
        kind, _, name = subject.partition(".")
        try:
            body = json.loads(payload)
        except json.JSONDecodeError:
            _log.warning("undecodable bus payload", extra={"subject": subject})
            return

        message = json.dumps({"kind": kind, "name": name, "data": body})
        if (
            kind == "alert"
            and isinstance(body, dict)
            and body.get("state") == "cleared"
        ):
            self.snapshot.pop((kind, name), None)
        elif kind in SNAPSHOT_KINDS:
            self._remember(kind, name, message)
        for queue in list(self.clients):
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                # Drop the oldest and keep the newest: on a map, the current
                # position matters and the one from two seconds ago does not.
                with contextlib.suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
                with contextlib.suppress(asyncio.QueueFull):
                    queue.put_nowait(message)

    def _remember(self, kind: str, name: str, message: str) -> None:
        key = (kind, name)
        # Re-inserting moves the key to the end, so a drone still reporting is
        # never the one evicted; the entry that goes is the one that has been
        # silent longest.
        self.snapshot.pop(key, None)
        self.snapshot[key] = message
        while len(self.snapshot) > SNAPSHOT_MAX_ENTRIES:
            evicted = next(iter(self.snapshot))
            del self.snapshot[evicted]
            _log.warning(
                "console snapshot full; dropped the least recently seen source",
                extra={"dropped": evicted, "limit": SNAPSHOT_MAX_ENTRIES},
            )


def create_app(
    nats_url: str,
    *,
    feed_secret: bytes,
    connect_timeout_s: float = CONNECT_TIMEOUT_S,
    basemap_dir: Path | None = None,
    clock_s: Callable[[], float] = wall_clock_s,
    allowed_origins: Iterable[str] = (),
) -> FastAPI:
    """Build the app. The NATS URL is injected so tests can point elsewhere.

    `feed_secret` is required: the feed is served only to a browser holding
    a ticket the API signed with it (P6-08). There is no way to build an
    open one.

    `connect_timeout_s` is injected for the same reason: a test that only needs
    to prove the app serves without a bus should not pay the production
    timeout to do it.

    `allowed_origins` are pages on other hosts that may open the feed, in
    addition to pages on the feed's own host (`origin_allowed`).
    """
    hub = ConsoleHub()
    allowed = frozenset(normalise_origin(origin) for origin in allowed_origins)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        client: NatsClient | None = None
        try:
            client = await asyncio.wait_for(
                nats.connect(nats_url), timeout=connect_timeout_s
            )
        except Exception as error:
            # A console that will not load because the bus is down is worse
            # than one that loads and says nothing is arriving: the second at
            # least shows the operator that the link is the problem.
            _log.error(
                "could not reach NATS; the console will serve but stay empty",
                extra={"nats_url": nats_url, "error": str(error)},
            )

        if client is not None:

            async def on_message(message: Msg) -> None:
                hub.broadcast(message.subject, message.data)

            for subject in SUBSCRIBED_SUBJECTS:
                await client.subscribe(subject, cb=on_message)
            # `subscribe` returns before the server has registered the
            # interest, so without this a console attaching just before a
            # publish silently misses it. Not `flush()`: its PONG can come
            # back before the SUBs have been sent (common/bus.py).
            await round_trip(client)

        app.state.hub = hub
        app.state.nats = client
        try:
            yield
        finally:
            if client is not None:
                await client.drain()

    app = FastAPI(title="courier console feed", lifespan=lifespan)

    @app.get("/", response_class=HTMLResponse)
    async def map_page() -> str:
        """The minimal P1-08 map. `web-pilot/` replaces this in P6-01."""
        return (STATIC / "map.html").read_text(encoding="utf-8")

    # P1-12. The vendored map libraries, and the base map, served by the
    # console itself so the page works with no internet.
    mount_map_assets(app, basemap_dir)

    @app.get("/healthz")
    async def health() -> dict[str, Any]:
        return {
            "bus_connected": app.state.nats is not None and app.state.nats.is_connected,
            "consoles": len(hub.clients),
        }

    @app.websocket("/ws/telemetry")
    async def telemetry(websocket: WebSocket) -> None:
        origin = websocket.headers.get("origin")
        if not origin_allowed(
            origin,
            websocket.headers.get("host"),
            allowed,
            feed_secure=websocket.url.scheme == "wss",
        ):
            # Refused at the handshake (HTTP 403): a page from another site
            # has no business reading a close code from this feed.
            _log.warning("console feed refused an origin", extra={"origin": origin})
            await websocket.close(code=CLOSE_POLICY_VIOLATION)
            return
        # Accepted before the ticket is checked, so a refusal is a close
        # code the page can read (4401) rather than a failed handshake.
        await websocket.accept()
        ticket = verify_feed_ticket(
            feed_secret, websocket.cookies.get(FEED_COOKIE, ""), now_s=clock_s()
        )
        if ticket is None:
            await websocket.close(code=CLOSE_SIGN_IN_REQUIRED, reason="sign in")
            return
        queue = hub.attach()
        # Watching for the browser leaving, alongside waiting for messages. A
        # handler that only waits on the queue learns of a closed socket at
        # its next send, which on an idle feed may never come: the
        # connection, and whatever holds it open, would stay forever.
        listener = asyncio.create_task(websocket.receive())
        try:
            while True:
                remaining_s = ticket.expires_at_s - clock_s()
                if remaining_s <= 0:
                    # The page fetches a new ticket from the API, which checks
                    # the session in the database, and reconnects. This is
                    # what makes a revoked session lose the feed.
                    await websocket.close(
                        code=CLOSE_SIGN_IN_REQUIRED, reason="ticket expired"
                    )
                    return
                getter = asyncio.create_task(queue.get())
                done, _ = await asyncio.wait(
                    {getter, listener},
                    timeout=remaining_s,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if listener in done:
                    getter.cancel()
                    if (
                        listener.exception() is not None
                        or listener.result().get("type") == "websocket.disconnect"
                    ):
                        return
                    # Anything a browser sends is ignored; keep listening.
                    listener = asyncio.create_task(websocket.receive())
                    continue
                if getter in done:
                    await websocket.send_text(getter.result())
                else:
                    getter.cancel()
        except WebSocketDisconnect:
            pass
        finally:
            listener.cancel()
            hub.detach(queue)

    return app

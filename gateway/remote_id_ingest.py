"""Remote ID ingest: receiver datagrams in, aircraft on the bus out. P1-15.

    python -m gateway.remote_id_ingest

Receivers send one UDP datagram per Open Drone ID message or message pack
they hear, as JSON:

    {"receiver_id": "rx-tbilisi-1", "transmitter": "AA:BB:CC:DD:EE:FF",
     "payload_hex": "12...", "rssi_dbm": -71}

`payload_hex` is the message exactly as broadcast, so this one decoder
serves every kind of receiver; an adapter for a receiver that decodes on its
own (a commercial unit's MQTT, say) re-encodes to this rather than adding a
second decoder. Each completed observation is published as
`telemetry.<aircraft id>`, beside the Gateway's MAVLink telemetry, and
stored in `remote_id_observations` in the telemetry database, so replay has
it (`gateway/remote_id_store.py`). A broadcast whose serial is one of our
registered aircraft is not a second aircraft (`gateway/remote_id_match.py`).

## Receivers

With `REMOTE_ID_RECEIVER_KEYS` set, every datagram must be signed by a known
receiver, recently, and only once (`gateway/remote_id_auth.py`). Without it
the ingest accepts unsigned datagrams, and `gateway/config.py` then refuses to
bind anywhere but loopback.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import signal
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import nats
from nats.aio.msg import Msg
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from common import configure_logging, get_logger, load_settings
from common.geoid import GeoidGrid
from gateway import odid
from gateway.config import RemoteIdSettings
from gateway.publisher import Bus
from gateway.rate_limit import RateLimiter
from gateway.remote_id import Frame, RemoteIdTracker
from gateway.remote_id_auth import (
    AuthenticationError,
    ReceiverAuthenticator,
    load_keys,
    split,
)
from gateway.remote_id_match import FleetSerials, LinkFreshness, as_registered
from gateway.remote_id_store import PendingRows, RemoteIdWriter, row_from_observation

_log = get_logger(__name__)

MAX_DATAGRAM_BYTES = 4096
FLUSH_INTERVAL_S = 0.5
# How often the registered serials are re-read, so an aircraft registered
# while the ingest runs is matched without a restart.
SERIALS_REFRESH_S = 60.0
REQUIRED_FIELDS = ("receiver_id", "transmitter", "payload_hex")


class DatagramError(ValueError):
    """A datagram that is not a receiver report."""


def parse_datagram(data: bytes, *, received_at: datetime) -> Frame:
    if len(data) > MAX_DATAGRAM_BYTES:
        raise DatagramError(f"{len(data)} bytes, more than {MAX_DATAGRAM_BYTES}")
    try:
        report = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DatagramError(f"not JSON: {error}") from error
    if not isinstance(report, dict):
        raise DatagramError("not a JSON object")
    for name in REQUIRED_FIELDS:
        if not isinstance(report.get(name), str) or not report[name]:
            raise DatagramError(f"missing {name}")
    try:
        payload = bytes.fromhex(report["payload_hex"])
    except ValueError as error:
        raise DatagramError("payload_hex is not hex") from error
    rssi = report.get("rssi_dbm")
    if rssi is not None and (
        isinstance(rssi, bool) or not isinstance(rssi, int | float)
    ):
        raise DatagramError("rssi_dbm is not a number")
    return Frame(
        receiver_id=report["receiver_id"],
        transmitter=report["transmitter"],
        # The ingest's clock, not the receiver's: receivers are not trusted
        # to keep time, and the monitor compares aircraft by arrival.
        received_at=received_at,
        payload=payload,
        rssi_dbm=None if rssi is None else float(rssi),
    )


def wall_clock() -> datetime:
    return datetime.now(tz=UTC)


@dataclass
class RemoteIdIngest:
    tracker: RemoteIdTracker
    bus: Bus
    # Where observations are kept; None only in tests that do not look.
    store: PendingRows | None = None
    # Which geoid turned HAE into AMSL, recorded with every stored height.
    geoid_model: str | None = None
    # None: unsigned datagrams are accepted (loopback only, gateway/config.py).
    authenticator: ReceiverAuthenticator | None = None
    # Our aircraft's serials, and whether their own telemetry is live.
    fleet: FleetSerials = field(default_factory=FleetSerials)
    links: LinkFreshness = field(default_factory=LinkFreshness)
    clock_s: Callable[[], float] = time.monotonic
    wall: Callable[[], datetime] = wall_clock
    # S-07. A refused datagram is logged at most once per source per
    # interval, with a count of those suppressed in between. The port is
    # reachable by whatever can route to it, and a line per datagram is a
    # way for a stranger to fill the disk.
    refusals: RateLimiter = field(default_factory=RateLimiter)
    refused: int = field(default=0, init=False)
    published: int = field(default=0, init=False)
    # Broadcasts by our own aircraft while their telemetry was live: stored,
    # not published.
    withheld: int = field(default=0, init=False)

    async def on_datagram(self, data: bytes, source: str) -> None:
        received_at = self.wall()
        try:
            if self.authenticator is not None:
                report = self.authenticator.check(data, now_s=received_at.timestamp())
            else:
                report, _ = split(data)
            frame = parse_datagram(report, received_at=received_at)
            observation = self.tracker.take(frame, now_s=self.clock_s())
        except (AuthenticationError, DatagramError, odid.DecodeError) as error:
            self.refused += 1
            suppressed = self.refusals.admit(source)
            if suppressed is not None:
                _log.warning(
                    "remote id datagram refused",
                    extra={
                        "source": source,
                        "error": str(error),
                        "suppressed": suppressed,
                    },
                )
            return
        if observation is None:
            return
        ours = self.fleet.match(observation)
        if self.store is not None:
            # Before publishing: a bus failure must not lose the record too.
            self.store.add(
                row_from_observation(
                    observation,
                    # Where the aircraft was placed: the broadcast's own time
                    # when plausible, else the receive time (S-27).
                    ts=datetime.fromisoformat(observation["captured_at"]),
                    payload=frame.payload,
                    geoid_model=self.geoid_model,
                    matched_drone_id=None if ours is None else ours.drone_id,
                )
            )
        if ours is not None:
            if self.links.live(ours.drone_id, now_s=self.clock_s()):
                self.withheld += 1
                return
            observation = as_registered(observation, ours)
        try:
            await self.bus.publish(
                f"telemetry.{observation['drone_id']}",
                json.dumps(observation).encode("utf-8"),
            )
        except Exception as error:
            _log.error(
                "could not publish a remote id observation",
                extra={"drone_id": observation["drone_id"], "error": repr(error)},
            )
            return
        self.published += 1


class _Protocol(asyncio.DatagramProtocol):
    def __init__(self, ingest: RemoteIdIngest) -> None:
        self.ingest = ingest
        self.tasks: set[asyncio.Task[None]] = set()

    def datagram_received(self, data: bytes, addr: tuple[str | object, ...]) -> None:
        task = asyncio.create_task(self.ingest.on_datagram(data, str(addr[0])))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)


async def listen(
    ingest: RemoteIdIngest, host: str, port: int
) -> asyncio.DatagramTransport:
    """Bind the receiver socket; every datagram goes to `ingest`."""
    transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(
        lambda: _Protocol(ingest), local_addr=(host, port)
    )
    return transport


def load_geoid(path: Path | None) -> GeoidGrid | None:
    if path is None:
        _log.warning(
            "no geoid model configured; Remote ID aircraft will have no AMSL "
            "altitude and the airspace monitor will not evaluate them"
        )
        return None
    return GeoidGrid.load(path)


def geoid_model(geoid: GeoidGrid | None, path: Path | None) -> str | None:
    if geoid is None or path is None:
        return None
    return geoid.description or path.name


def tracker_from_settings(
    settings: RemoteIdSettings, geoid: GeoidGrid | None
) -> RemoteIdTracker:
    return RemoteIdTracker(
        geoid=geoid,
        time_tolerance_s=settings.remote_id_time_tolerance_s,
        max_latency_s=settings.remote_id_max_latency_s,
    )


async def flush_periodically(store: PendingRows, stop: asyncio.Event) -> None:
    while not stop.is_set():
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), FLUSH_INTERVAL_S)
        await store.flush()


async def refresh_serials_periodically(
    fleet: FleetSerials, engine: AsyncEngine, stop: asyncio.Event
) -> None:
    while not stop.is_set():
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), SERIALS_REFRESH_S)
        if stop.is_set():
            return
        try:
            await fleet.refresh(engine)
        except (SQLAlchemyError, OSError) as error:
            # Keep the serials we have: a database hiccup must not make our
            # aircraft appear twice.
            _log.error("could not re-read serials", extra={"error": repr(error)})


async def run(settings: RemoteIdSettings) -> None:
    bus = await nats.connect(str(settings.nats_url))
    engine = create_async_engine(str(settings.telemetry_database_url))
    store = PendingRows(writer=RemoteIdWriter(engine))
    geoid = load_geoid(settings.geoid_path)
    authenticator = None
    if settings.remote_id_receiver_keys is not None:
        authenticator = ReceiverAuthenticator(
            keys=load_keys(settings.remote_id_receiver_keys),
            max_skew_s=settings.remote_id_max_skew_s,
        )
    fleet = FleetSerials()
    await fleet.refresh(engine)
    ingest = RemoteIdIngest(
        tracker=tracker_from_settings(settings, geoid),
        bus=bus,
        store=store,
        geoid_model=geoid_model(geoid, settings.geoid_path),
        authenticator=authenticator,
        fleet=fleet,
    )

    async def on_telemetry(message: Msg) -> None:
        ingest.links.on_telemetry(message.data, now_s=ingest.clock_s())

    await bus.subscribe("telemetry.*", cb=on_telemetry)
    transport = await listen(
        ingest, settings.remote_id_bind_host, settings.remote_id_bind_port
    )
    _log.info(
        "remote id ingest running",
        extra={
            "host": settings.remote_id_bind_host,
            "port": settings.remote_id_bind_port,
            "geoid": str(settings.geoid_path) if settings.geoid_path else None,
            "serials_matched": len(fleet.by_serial),
            "receivers": (
                sorted(authenticator.keys) if authenticator else "unauthenticated"
            ),
        },
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
    flusher = asyncio.create_task(flush_periodically(store, stop))
    refresher = asyncio.create_task(refresh_serials_periodically(fleet, engine, stop))
    try:
        await stop.wait()
    finally:
        transport.close()
        stop.set()
        # The flusher's last pass writes what was still pending.
        await flusher
        await refresher
        if store.pending:
            _log.error(
                "remote id observations not stored at shutdown",
                extra={"pending": store.pending},
            )
        await bus.drain()
        await engine.dispose()


def main() -> None:
    settings = load_settings(RemoteIdSettings)
    configure_logging(service=settings.service_name, level=settings.log_level.value)
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run(settings))


if __name__ == "__main__":
    main()

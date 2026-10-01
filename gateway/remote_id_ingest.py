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

## Switched off (U-15)

Remote ID as a whole, or one receiver, can be switched off without a
restart (`common/sources.py`). A datagram from a disabled receiver is
dropped once its receiver is established, before the tracker, the store or
the bus sees it, and counted (`dropped_source_disabled` in the status line,
and per receiver on `source.remote_id`). Switching it on again needs
nothing at the receiver: the next datagram is taken.

## Identification (U-02)

Every observation published carries `identification`, the registry's
verdict on its serial and operator ID (`gateway/identification.py`), from
the registry projection the ingest re-reads every `REGISTRY_REFRESH_S`
(`gateway/registry_projection.py`). A broadcast of one of our aircraft's
serials far from where that aircraft's live relay telemetry places it is
published as a separate track, `unknown_operator` with a mismatch (S-10),
and counted (`serial_conflicts`).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import signal
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import nats
from nats.aio.msg import Msg
from sqlalchemy.ext.asyncio import create_async_engine

from common import configure_logging, get_logger, load_settings
from common.bus import RECONNECT_FOREVER
from common.geoid import GeoidGrid
from common.sources import REMOTE_ID, follow, follower_from_settings
from gateway import odid
from gateway.config import RemoteIdSettings
from gateway.identification import resolve_remote_id, serial_conflict
from gateway.publisher import Bus
from gateway.rate_limit import RateLimiter
from gateway.registry_projection import RegistryFollower, RegistrySnapshot
from gateway.remote_id import SOURCE, Frame, RemoteIdTracker
from gateway.remote_id_auth import (
    AuthenticationError,
    ReceiverAuthenticator,
    load_keys,
    split,
)
from gateway.remote_id_match import (
    DEFAULT_SPOOF_DISTANCE_M,
    FleetSerials,
    LinkFreshness,
    Verdict,
    as_registered,
    judge,
    report_conflict,
)
from gateway.remote_id_store import PendingRows, RemoteIdWriter, row_from_observation
from gateway.source_activity import SourceActivity, publish_periodically

_log = get_logger(__name__)

MAX_DATAGRAM_BYTES = 4096
FLUSH_INTERVAL_S = 0.5
# How often the running totals are logged, as the airspace monitor's are.
STATUS_INTERVAL_S = 60.0
# Why `captured_at` fell back to the receive time (gateway/remote_id.py).
TIME_FALLBACK_REASONS = ("unknown", "invalid", "too_old", "clock_ahead")
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
    # U-15. Which receivers are switched on, and what each is doing. None:
    # every receiver is (a test, or an ingest with no control channel).
    sources: SourceActivity | None = None
    # Datagrams from a receiver, or a type, that is switched off.
    dropped_source_disabled: int = field(default=0, init=False)
    # U-02. The registry as last read; `RegistryFollower` replaces it.
    registry: RegistrySnapshot = field(default_factory=RegistrySnapshot)
    # S-10. See `gateway/remote_id_match.py`.
    spoof_distance_m: float = DEFAULT_SPOOF_DISTANCE_M
    # Broadcasts of our serials away from where our aircraft is (S-10).
    serial_conflicts: int = field(default=0, init=False)
    # Observations published by identification status.
    identified_as: Counter[str] = field(default_factory=Counter, init=False)

    async def on_datagram(self, data: bytes, source: str) -> None:
        received_at = self.wall()
        try:
            if self.authenticator is not None:
                report = self.authenticator.check(data, now_s=received_at.timestamp())
            else:
                report, _ = split(data)
            frame = parse_datagram(report, received_at=received_at)
            if self.sources is not None and not self.sources.admit(frame.receiver_id):
                # Before the tracker: a disabled receiver's broadcasts are
                # not half taken, and nothing of them is stored or published.
                self.dropped_source_disabled += 1
                return
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
        lat, lon = observation.get("lat_deg"), observation.get("lon_deg")
        judgement = judge(
            self.fleet.match(observation),
            None if lat is None or lon is None else (float(lat), float(lon)),
            self.links,
            now_s=self.clock_s(),
            spoof_distance_m=self.spoof_distance_m,
        )
        conflict = judgement.verdict is Verdict.CONFLICT
        if conflict:
            # S-10: not our aircraft, whatever serial it claims.
            self.serial_conflicts += 1
            report_conflict(
                self.refusals,
                judgement,
                broadcast_drone_id=observation["drone_id"],
                station_id=observation.get("station_id"),
                source=SOURCE,
                spoof_distance_m=self.spoof_distance_m,
                serial_conflicts=self.serial_conflicts,
            )
        ours = None if conflict else judgement.aircraft
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
        if judgement.verdict is Verdict.WITHHOLD:
            self.withheld += 1
            return
        if judgement.verdict is Verdict.AS_OURS and ours is not None:
            observation = as_registered(observation, ours)
        rid = observation["remote_id"]
        identification = (
            serial_conflict(rid["ua_id"], rid.get("operator_id"))
            if conflict
            else resolve_remote_id(self.registry, rid)
        )
        observation["identification"] = identification.as_dict()
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
        self.identified_as[identification.status.value] += 1

    def status(self) -> dict[str, int]:
        """The running totals, as the status line logs them."""
        tracker = self.tracker
        totals = {
            "published": self.published,
            "refused": self.refused,
            "dropped_source_disabled": self.dropped_source_disabled,
            "withheld": self.withheld,
            "transmitters": tracker.transmitters,
            "unidentified": tracker.unidentified,
            "identity_changes": tracker.identity_changes,
            "address_conflicts": tracker.address_conflicts,
            "silences": tracker.silences,
            "serial_conflicts": self.serial_conflicts,
        }
        for status in ("registered", "suspended", "unknown_operator", "unidentified"):
            totals[f"identified_{status}"] = self.identified_as[status]
        for reason in TIME_FALLBACK_REASONS:
            totals[f"time_fallback_{reason}"] = tracker.time_fallbacks[reason]
        if self.sources is not None:
            # U-15: refusals, and whether the switches could be read.
            totals.update(self.sources.status())
        if self.store is not None:
            totals["store_pending"] = self.store.pending
            totals["store_written"] = self.store.written
            totals["store_dropped"] = self.store.dropped
        return totals


async def log_status_periodically(
    ingest: RemoteIdIngest,
    stop: asyncio.Event,
    *,
    every_s: float = STATUS_INTERVAL_S,
) -> None:
    """Log `ingest.status()` every `every_s`, and once more on stopping."""
    while not stop.is_set():
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), every_s)
        _log.info("remote id ingest status", extra=ingest.status())


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
        min_vertical_accuracy=settings.remote_id_min_vertical_accuracy,
        pressure_hold_s=settings.remote_id_pressure_hold_s,
        identity_ttl_s=settings.remote_id_identity_ttl_s,
        max_gap_s=settings.remote_id_max_gap_s,
        identify_within_s=settings.remote_id_identify_within_s,
    )


async def flush_periodically(store: PendingRows, stop: asyncio.Event) -> None:
    while not stop.is_set():
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), FLUSH_INTERVAL_S)
        await store.flush()


async def run(settings: RemoteIdSettings) -> None:
    bus = await nats.connect(
        str(settings.nats_url), max_reconnect_attempts=RECONNECT_FOREVER
    )
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
    # U-02. The registry projection: serials for matching, and the facts
    # every observation is identified by. Re-read every REGISTRY_REFRESH_S,
    # so an aircraft registered or suspended while the ingest runs is seen
    # without a restart.
    registry = RegistryFollower(engine=engine, refresh_s=settings.registry_refresh_s)
    await registry.refresh()
    # U-15. The switches, from the bucket the API writes; never from the
    # relational database.
    follower = follower_from_settings(bus, settings)
    sources = SourceActivity(
        source_type=REMOTE_ID,
        switch=follower,
        known=authenticator.keys if authenticator is not None else (),
    )
    ingest = RemoteIdIngest(
        tracker=tracker_from_settings(settings, geoid),
        bus=bus,
        store=store,
        geoid_model=geoid_model(geoid, settings.geoid_path),
        authenticator=authenticator,
        fleet=fleet,
        sources=sources,
        spoof_distance_m=settings.remote_id_spoof_distance_m,
    )

    def take_registry(snapshot: RegistrySnapshot) -> None:
        fleet.take(snapshot)
        ingest.registry = snapshot

    registry.on_change = take_registry
    take_registry(registry.snapshot)
    control = await follow(bus, follower, subject=settings.source_control_subject)

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
            "registry_loaded": registry.loaded,
            "receivers": (
                sorted(authenticator.keys) if authenticator else "unauthenticated"
            ),
            "source_control_version": follower.state.version,
            "disabled": [f"{t}/{i or '*'}" for t, i in follower.state.disabled()],
        },
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
    flusher = asyncio.create_task(flush_periodically(store, stop))
    refresher = asyncio.create_task(registry.run(stop))
    reporter = asyncio.create_task(log_status_periodically(ingest, stop))
    announcer = asyncio.create_task(publish_periodically(sources, bus, stop))
    try:
        await stop.wait()
    finally:
        transport.close()
        stop.set()
        # The flusher's last pass writes what was still pending.
        await flusher
        await refresher
        await reporter
        await announcer
        await follower.stop()
        await control.unsubscribe()
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

"""Network Remote ID ingest: USSP flights in, aircraft on the bus out. U-02.

    python -m gateway.network_rid_ingest

Its own adapter process, as every source is (`ARCHITECTURE.md` §2.1): it
polls each configured ASTM F3411 Service Provider as a Display Provider
(`gateway/network_rid.py`), and publishes every flight it hears as
`telemetry.<aircraft id>`, beside the relay and direct Remote ID tracks,
with `source: "network_remote_id"` and the provider as `station_id`.

## Providers

`NETWORK_RID_PROVIDERS` is a JSON list, one object per provider:

    [{"id": "ussp-a", "base_url": "https://ussp-a.example/rid/v2",
      "token_url": "https://auth.ussp-a.example/oauth/token",
      "client_id": "utm-ge", "client_secret": "...",
      "scope": "rid.display_provider", "audience": "ussp-a.example",
      "areas": [[41.60, 44.70, 41.80, 44.95]]}]

Areas are `[lat_min, lon_min, lat_max, lon_max]`. The secret lives in the
environment (an `.env` that is never committed, or the deployment's secret
store), never in a file in the repository; it is never logged. A provider
reached over plain HTTP is refused unless it is on this host (the fake SP
for SITL, `tools/fake_rid_sp.py`).

## Switched off (U-15)

Network Remote ID as a whole (`network_remote_id`), or one provider, can be
switched off without a restart. A disabled provider is not polled at all;
each poll skipped is counted against it (`refused_source_disabled`, and per
provider on `source.network_remote_id`). Its tracks stop, and the airspace
monitor and the console put them out of the picture as *source disabled*.
Switching it on again resumes polling at the next interval.

## Identification

Each flight's details give its serial (`uas_id.serial_number`) and
operator (`operator_id`), resolved against the registry projection like a
broadcast (`gateway/identification.py`). A flight whose details could not
be read yet has no serial: it is `unidentified` until they can be.

## Failures

A provider that is down, slow, answers with an error, or refuses the
client's credentials costs that poll for that provider, counted
(`provider_errors`, `auth_failures`, `format_errors`) and logged at most
once a minute per provider and kind. The next poll tries again; nothing
crashes and the other providers are not affected.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import signal
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx
import nats
from nats.aio.msg import Msg
from sqlalchemy.ext.asyncio import create_async_engine

from common import configure_logging, get_logger, load_settings
from common.bus import RECONNECT_FOREVER
from common.geoid import GeoidGrid
from common.sources import NETWORK_REMOTE_ID, follow, follower_from_settings
from gateway.config import NetworkRidProvider, NetworkRidSettings
from gateway.identification import resolve, serial_conflict
from gateway.network_rid import (
    DEFAULT_DETAILS_CONCURRENCY,
    DEFAULT_DETAILS_TTL_S,
    DEFAULT_MAX_AGE_S,
    DEFAULT_MAX_DETAILS_PER_POLL,
    DEFAULT_MAX_DIAGONAL_KM,
    DEFAULT_MAX_FLIGHTS_PER_RESPONSE,
    DEFAULT_MAX_TILES_PER_POLL,
    DEFAULT_POLL_DEADLINE_S,
    SOURCE,
    Area,
    AuthError,
    Budget,
    Details,
    FlightState,
    FormatError,
    OversizeError,
    Page,
    ProviderError,
    ServiceProviderClient,
    TokenSource,
    flights_in,
    observation,
    place,
    unique,
)
from gateway.publisher import Bus
from gateway.rate_limit import RateLimiter
from gateway.registry_projection import RegistryFollower, RegistrySnapshot
from gateway.remote_id import (
    DEFAULT_MAX_LATENCY_S,
    DEFAULT_MIN_VERTICAL_ACCURACY,
    DEFAULT_TIME_TOLERANCE_S,
    Geoid,
)
from gateway.remote_id_match import (
    DEFAULT_SPOOF_DISTANCE_M,
    LinkFreshness,
    Registered,
    Verdict,
    judge,
    report_conflict,
)
from gateway.source_activity import SourceActivity, publish_periodically

_log = get_logger(__name__)

STATUS_INTERVAL_S = 60.0


def wall_clock() -> datetime:
    return datetime.now(tz=UTC)


@dataclass
class ProviderPoller:
    """Polls one Service Provider and publishes what it says."""

    provider: str
    client: ServiceProviderClient
    areas: list[Area]
    bus: Bus
    # The registry as last read (`RegistryFollower`).
    registry: Callable[[], RegistrySnapshot] = RegistrySnapshot
    # U-15. None: never switched off (a test, or no control channel).
    sources: SourceActivity | None = None
    # When each of our aircraft last sent relay telemetry, and where: the
    # same rule as direct Remote ID decides whether a flight speaks for it
    # (`gateway/remote_id_match.py`, `judge`).
    links: LinkFreshness = field(default_factory=LinkFreshness)
    spoof_distance_m: float = DEFAULT_SPOOF_DISTANCE_M
    geoid: Geoid | None = None
    max_diagonal_km: float = DEFAULT_MAX_DIAGONAL_KM
    max_age_s: float = DEFAULT_MAX_AGE_S
    details_ttl_s: float = DEFAULT_DETAILS_TTL_S
    time_tolerance_s: float = DEFAULT_TIME_TOLERANCE_S
    max_latency_s: float = DEFAULT_MAX_LATENCY_S
    min_vertical_accuracy: int = DEFAULT_MIN_VERTICAL_ACCURACY
    # What one poll may cost, whatever the provider sends.
    max_flights_per_response: int = DEFAULT_MAX_FLIGHTS_PER_RESPONSE
    max_tiles_per_poll: int = DEFAULT_MAX_TILES_PER_POLL
    max_details_per_poll: int = DEFAULT_MAX_DETAILS_PER_POLL
    details_concurrency: int = DEFAULT_DETAILS_CONCURRENCY
    poll_deadline_s: float = DEFAULT_POLL_DEADLINE_S
    wall: Callable[[], datetime] = wall_clock
    clock_s: Callable[[], float] = time.monotonic
    failures: RateLimiter = field(default_factory=RateLimiter)
    # Totals, for the status line.
    polls: int = field(default=0, init=False)
    skipped_disabled: int = field(default=0, init=False)
    provider_errors: int = field(default=0, init=False)
    auth_failures: int = field(default=0, init=False)
    format_errors: int = field(default=0, init=False)
    oversize: int = field(default=0, init=False)
    deadline_exceeded: int = field(default=0, init=False)
    tiles_skipped: int = field(default=0, init=False)
    flights_dropped: int = field(default=0, init=False)
    details_failures: int = field(default=0, init=False)
    details_deferred: int = field(default=0, init=False)
    published: int = field(default=0, init=False)
    withheld: int = field(default=0, init=False)
    serial_conflicts: int = field(default=0, init=False)
    unchanged: int = field(default=0, init=False)
    too_old: int = field(default=0, init=False)
    time_notes: Counter[str] = field(default_factory=Counter, init=False)
    identified_as: Counter[str] = field(default_factory=Counter, init=False)
    _last_ts: dict[str, datetime] = field(default_factory=dict, init=False)
    _last_seen_s: dict[str, float] = field(default_factory=dict, init=False)
    _details: dict[str, tuple[Details, float]] = field(default_factory=dict, init=False)

    async def poll(self) -> None:
        """One pass over every area. Never raises for the provider's sake,
        and never takes longer than `poll_deadline_s`."""
        if self.sources is not None and not self.sources.enabled(self.provider):
            # U-15: a disabled provider is not asked anything.
            self.skipped_disabled += 1
            self.sources.refuse(self.provider)
            return
        self.polls += 1
        deadline_s = self.clock_s() + self.poll_deadline_s
        pages: list[Page] = []
        if not await self._fetch(pages, deadline_s):
            return
        flights = unique(pages)
        if self.sources is not None and not self.sources.admit(
            self.provider, len(flights)
        ):
            # Switched off while the poll was in flight.
            return
        fresh = [(f, p) for f, p in flights if self._moved(f)]
        await self._fetch_details([f.flight_id for f, _ in fresh], deadline_s)
        for flight, page in fresh:
            await self._take(flight, page)
        self._forget()

    async def _fetch(self, pages: list[Page], deadline_s: float) -> bool:
        """Every tile, into `pages` as each answers. False when nothing is
        to be taken from this poll; a deadline keeps what already arrived."""
        budget = Budget(self.max_tiles_per_poll)
        try:
            async with asyncio.timeout(max(0.0, deadline_s - self.clock_s())):
                for area in self.areas:
                    for tile in area.tiles(self.max_diagonal_km):
                        await flights_in(
                            self.client,
                            tile,
                            wall=self.wall,
                            budget=budget,
                            pages=pages,
                            max_flights=self.max_flights_per_response,
                        )
        except TimeoutError:
            self.deadline_exceeded += 1
            self._failed("deadline", TimeoutError(f"{self.poll_deadline_s} s"))
        except AuthError as error:
            self.auth_failures += 1
            self._failed("auth", error)
            return False
        except OversizeError as error:
            self.oversize += 1
            self._failed("oversize", error)
            return False
        except FormatError as error:
            self.format_errors += 1
            self._failed("format", error)
            return False
        except ProviderError as error:
            self.provider_errors += 1
            self._failed("provider", error)
            return False
        dropped = sum(page.dropped for page in pages)
        if budget.skipped:
            self.tiles_skipped += budget.skipped
            self._failed(
                "tiles", ProviderError(f"{budget.skipped} tiles over the per-poll cap")
            )
        if dropped:
            self.flights_dropped += dropped
            self._failed(
                "flights", ProviderError(f"{dropped} flights over the per-response cap")
            )
        return True

    def _moved(self, flight: FlightState) -> bool:
        """Whether the SP's state is newer than the one last taken."""
        self._last_seen_s[flight.flight_id] = self.clock_s()
        held = self._last_ts.get(flight.flight_id)
        if held is not None and flight.timestamp <= held:
            self.unchanged += 1
            return False
        return True

    async def _fetch_details(self, flight_ids: list[str], deadline_s: float) -> None:
        """Details for flights without fresh ones: at most
        `max_details_per_poll`, `details_concurrency` at a time, within the
        poll's deadline. The rest use what is held, and wait for a later poll."""
        now_s = self.clock_s()
        due = [
            flight_id
            for flight_id in flight_ids
            if (held := self._details.get(flight_id)) is None
            or now_s - held[1] >= self.details_ttl_s
        ]
        if len(due) > self.max_details_per_poll:
            self.details_deferred += len(due) - self.max_details_per_poll
            due = due[: self.max_details_per_poll]
        if not due:
            return
        gate = asyncio.Semaphore(self.details_concurrency)

        async def one(flight_id: str) -> None:
            async with gate:
                try:
                    details = await self.client.details(flight_id)
                except ProviderError as error:
                    self.details_failures += 1
                    self._failed("details", error)
                    return
                self._details[flight_id] = (details, self.clock_s())

        try:
            async with asyncio.timeout(max(0.0, deadline_s - self.clock_s())):
                await asyncio.gather(*(one(flight_id) for flight_id in due))
        except TimeoutError:
            self.deadline_exceeded += 1
            self._failed("deadline", TimeoutError("details"))

    async def _take(self, flight: FlightState, page: Page) -> None:
        placement = place(
            flight,
            response_at=page.response_at,
            received_at=page.received_at,
            max_age_s=self.max_age_s,
            time_tolerance_s=self.time_tolerance_s,
            max_latency_s=self.max_latency_s,
        )
        if placement is None:
            self.too_old += 1
            return
        if placement.note is not None:
            self.time_notes[placement.note] += 1
        self._last_ts[flight.flight_id] = flight.timestamp
        held = self._details.get(flight.flight_id)
        details = None if held is None else held[0]
        serial = None if details is None else details.serial
        operator = None if details is None else details.operator_id
        snapshot = self.registry()
        facts = None if serial is None else snapshot.by_serial.get(serial)
        judgement = judge(
            None if facts is None else Registered(facts.drone_id, facts.label),
            (flight.lat_deg, flight.lon_deg),
            self.links,
            now_s=self.clock_s(),
            spoof_distance_m=self.spoof_distance_m,
        )
        if judgement.verdict is Verdict.WITHHOLD:
            # Our aircraft, its relay live and in agreement: the relay track
            # is the better one (P1-15), whichever broadcast source this is.
            self.withheld += 1
            return
        if judgement.verdict is Verdict.CONFLICT and serial is not None:
            self.serial_conflicts += 1
            identification = serial_conflict(serial, operator)
            registered = None
        else:
            identification = resolve(snapshot, serial=serial, operator_reg=operator)
            ours = judgement.aircraft
            registered = None if ours is None else (ours.drone_id, ours.label)
        message = observation(
            flight,
            details,
            placement,
            provider=self.provider,
            received_at=page.received_at,
            geoid=self.geoid,
            min_vertical_accuracy=self.min_vertical_accuracy,
            registered=registered,
        )
        if judgement.verdict is Verdict.CONFLICT:
            report_conflict(
                self.failures,
                judgement,
                broadcast_drone_id=message["drone_id"],
                station_id=self.provider,
                source=SOURCE,
                spoof_distance_m=self.spoof_distance_m,
                serial_conflicts=self.serial_conflicts,
            )
        message["identification"] = identification.as_dict()
        try:
            await self.bus.publish(
                f"telemetry.{message['drone_id']}", json.dumps(message).encode("utf-8")
            )
        except Exception as error:
            _log.error(
                "could not publish a network remote id observation",
                extra={"drone_id": message["drone_id"], "error": repr(error)},
            )
            return
        self.published += 1
        self.identified_as[identification.status.value] += 1

    def _forget(self) -> None:
        """Flights the SP has not mentioned for `max_age_s` are dropped."""
        now_s = self.clock_s()
        for flight_id, seen_s in list(self._last_seen_s.items()):
            if now_s - seen_s > self.max_age_s:
                del self._last_seen_s[flight_id]
                self._last_ts.pop(flight_id, None)
                self._details.pop(flight_id, None)

    def _failed(self, kind: str, error: Exception) -> None:
        suppressed = self.failures.admit(kind)
        if suppressed is not None:
            _log.warning(
                "network remote id poll failed",
                extra={
                    "station_id": self.provider,
                    "kind": kind,
                    "error": str(error),
                    "suppressed": suppressed,
                },
            )

    def status(self) -> dict[str, int]:
        totals = {
            "polls": self.polls,
            "skipped_disabled": self.skipped_disabled,
            "published": self.published,
            "withheld": self.withheld,
            "serial_conflicts": self.serial_conflicts,
            "unchanged": self.unchanged,
            "too_old": self.too_old,
            "provider_errors": self.provider_errors,
            "auth_failures": self.auth_failures,
            "format_errors": self.format_errors,
            "oversize": self.oversize,
            "deadline_exceeded": self.deadline_exceeded,
            "tiles_skipped": self.tiles_skipped,
            "flights_dropped": self.flights_dropped,
            "details_failures": self.details_failures,
            "details_deferred": self.details_deferred,
            "flights_held": len(self._last_seen_s),
        }
        for note in ("ahead_of_response", "clock_ahead", "too_old"):
            totals[f"time_{note}"] = self.time_notes[note]
        for status in ("registered", "suspended", "unknown_operator", "unidentified"):
            totals[f"identified_{status}"] = self.identified_as[status]
        return totals


async def poll_periodically(
    poller: ProviderPoller, stop: asyncio.Event, *, every_s: float
) -> None:
    while not stop.is_set():
        await poller.poll()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), every_s)


def build_poller(
    provider: NetworkRidProvider,
    http: httpx.AsyncClient,
    bus: Bus,
    settings: NetworkRidSettings,
    **kwargs: Any,
) -> ProviderPoller:
    tokens = TokenSource(
        http=http,
        token_url=str(provider.token_url),
        client_id=provider.client_id,
        client_secret=provider.client_secret.get_secret_value(),
        scope=provider.scope,
        audience=provider.audience,
    )
    return ProviderPoller(
        provider=provider.id,
        client=ServiceProviderClient(
            http=http,
            base_url=str(provider.base_url),
            tokens=tokens,
            max_body_bytes=settings.network_rid_max_body_bytes,
        ),
        areas=[Area(*box) for box in provider.areas],
        bus=bus,
        max_diagonal_km=settings.network_rid_max_diagonal_km,
        max_age_s=settings.network_rid_max_age_s,
        details_ttl_s=settings.network_rid_details_ttl_s,
        time_tolerance_s=settings.network_rid_time_tolerance_s,
        max_latency_s=settings.network_rid_max_latency_s,
        max_flights_per_response=settings.network_rid_max_flights_per_response,
        max_tiles_per_poll=settings.network_rid_max_tiles_per_poll,
        max_details_per_poll=settings.network_rid_max_details_per_poll,
        details_concurrency=settings.network_rid_details_concurrency,
        poll_deadline_s=settings.network_rid_poll_deadline_s,
        spoof_distance_m=settings.remote_id_spoof_distance_m,
        **kwargs,
    )


async def log_status_periodically(
    pollers: list[ProviderPoller],
    registry: RegistryFollower,
    stop: asyncio.Event,
    *,
    every_s: float = STATUS_INTERVAL_S,
) -> None:
    while not stop.is_set():
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), every_s)
        for poller in pollers:
            _log.info(
                "network remote id status",
                extra={
                    "station_id": poller.provider,
                    **poller.status(),
                    **registry.status(),
                },
            )


async def run(settings: NetworkRidSettings) -> None:
    bus = await nats.connect(
        str(settings.nats_url), max_reconnect_attempts=RECONNECT_FOREVER
    )
    engine = create_async_engine(str(settings.telemetry_database_url))
    registry = RegistryFollower(engine=engine, refresh_s=settings.registry_refresh_s)
    await registry.refresh()
    geoid = None if settings.geoid_path is None else GeoidGrid.load(settings.geoid_path)
    if geoid is None:
        _log.warning(
            "no geoid model configured; network Remote ID aircraft will have no "
            "AMSL altitude and the airspace monitor will not evaluate them"
        )
    follower = follower_from_settings(bus, settings)
    providers = settings.network_rid_providers
    sources = SourceActivity(
        source_type=NETWORK_REMOTE_ID,
        switch=follower,
        known=[provider.id for provider in providers],
    )
    control = await follow(bus, follower, subject=settings.source_control_subject)
    http = httpx.AsyncClient(timeout=settings.network_rid_http_timeout_s)
    # Our aircraft's relay telemetry, for the rule that decides whether a
    # flight speaks for one of them (`gateway/remote_id_match.py`).
    links = LinkFreshness()
    pollers = [
        build_poller(
            provider,
            http,
            bus,
            settings,
            registry=lambda: registry.snapshot,
            sources=sources,
            geoid=geoid,
            links=links,
        )
        for provider in providers
    ]

    async def on_telemetry(message: Msg) -> None:
        links.on_telemetry(message.data, now_s=time.monotonic())

    await bus.subscribe("telemetry.*", cb=on_telemetry)
    if not pollers:
        _log.warning("no network remote id providers configured; nothing to poll")
    _log.info(
        "network remote id ingest running",
        extra={
            "providers": [p.provider for p in pollers],
            "poll_s": settings.network_rid_poll_s,
            "registry_loaded": registry.loaded,
            "source_control_version": follower.state.version,
        },
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
    tasks = [
        asyncio.create_task(
            poll_periodically(poller, stop, every_s=settings.network_rid_poll_s)
        )
        for poller in pollers
    ]
    tasks.append(asyncio.create_task(registry.run(stop)))
    tasks.append(asyncio.create_task(publish_periodically(sources, bus, stop)))
    tasks.append(asyncio.create_task(log_status_periodically(pollers, registry, stop)))
    try:
        await stop.wait()
    finally:
        stop.set()
        for task in tasks:
            await task
        await follower.stop()
        await control.unsubscribe()
        await http.aclose()
        await bus.drain()
        await engine.dispose()


def main() -> None:
    settings = load_settings(NetworkRidSettings)
    configure_logging(service=settings.service_name, level=settings.log_level.value)
    # httpx logs every request at INFO: a line a second per provider. The
    # poller logs what matters, failures rate-limited, totals every minute.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run(settings))


if __name__ == "__main__":
    main()

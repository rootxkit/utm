"""Run the Gateway: terminate relay-v1, store, convert, publish.

    python -m gateway

Composition only. Every decision this process makes lives in a module with its
own tests; this file exists to wire them together in the one order that is
correct, and to fail at startup rather than in flight when something is
missing.

The order matters and is the same order the data takes:

    RelayServer          terminates relay-v1, stores and acknowledges
      -> TimescaleIngestStore   archive + index, durable before the ack
      -> StationPipelines       parse, classify, resolve, assemble
           -> DroneStateWriter  the hypertable
           -> TelemetryPublisher  the bus, for the console

U-15: each station, and relays as a whole, can be switched off without a
restart. The switches come from the bucket the API writes (`common/sources.py`),
never from the relational database; a change closes the sessions it disables.

Tokens are read from a file, one `station_id: token` per line, because spec
§12 question 1 - where station tokens live, how they are issued and revoked -
is still open. This is deliberately the simplest thing that is not a hardcoded
secret, and it is not the answer.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import signal
import sys
from pathlib import Path

import nats
import redis.asyncio
from sqlalchemy.ext.asyncio import create_async_engine

from common import configure_logging, get_logger, load_settings
from common.sources import (
    RELAY,
    SourceControlFollower,
    SourceControlState,
    bucket_reader,
    follow,
)
from gateway.archive import RawArchive
from gateway.binding import BindingResolver
from gateway.config import GatewaySettings
from gateway.firmware_store import FirmwareRegistry
from gateway.ingest_store_pg import TimescaleIngestStore
from gateway.live_state import LiveState
from gateway.pipeline import StationPipelines
from gateway.publisher import TelemetryPublisher
from gateway.relay_server import RelayServer
from gateway.retention import BYTES_PER_GIB, ArchiveRetention, RetentionSchedule
from gateway.source_activity import SourceActivity, publish_periodically
from gateway.state_buffer import BufferedStateWriter
from gateway.state_writer import DroneStateWriter

_log = get_logger(__name__)

DEFAULT_RELAY_PORT = 8081


class FileAuthenticator:
    """Resolves bearer tokens from a file of `station_id: token` lines.

    A placeholder with a known expiry: spec §12 question 1 owns token storage,
    rotation and revocation. It is a file rather than a constant so that a
    token is never committed, and it is read once at startup so a rotation
    means a restart - which is honest about what this is.
    """

    def __init__(self, path: Path) -> None:
        self._by_token: dict[str, str] = {}
        for number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            station_id, separator, token = stripped.partition(":")
            if not separator:
                raise ValueError(
                    f"{path}:{number}: expected 'station_id: token', got {line!r}"
                )
            station_id = station_id.strip()
            token = token.strip()
            # S-08. An empty token would be a station anyone can claim by
            # sending `Authorization: Bearer ` with nothing after it, and a
            # token shared by two stations makes the second silently steal
            # the first's identity: the last line wins in a dict and every
            # record lands under the wrong station. Both are refused at
            # startup, where the operator is looking, not at the upgrade.
            if not station_id:
                raise ValueError(f"{path}:{number}: the station_id is empty")
            if not token:
                raise ValueError(
                    f"{path}:{number}: the token for {station_id!r} is empty"
                )
            if token in self._by_token:
                raise ValueError(
                    f"{path}:{number}: the token for {station_id!r} is already "
                    f"assigned to {self._by_token[token]!r}; a token identifies "
                    f"exactly one station (relay-v1 §3)"
                )
            self._by_token[token] = station_id
        if not self._by_token:
            raise ValueError(f"{path} defines no tokens")

    async def station_for_token(self, token: str) -> str | None:
        return self._by_token.get(token)

    @property
    def stations(self) -> set[str]:
        """Every station with a token: the relays this Gateway can serve."""
        return set(self._by_token.values())


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="gateway", description=__doc__)
    parser.add_argument(
        "--tokens",
        type=Path,
        default=Path("gateway.tokens"),
        help="file of 'station_id: token' lines (default: gateway.tokens)",
    )
    parser.add_argument("--host", default="0.0.0.0", help="relay-v1 bind host")
    parser.add_argument(
        "--port", type=int, default=DEFAULT_RELAY_PORT, help="relay-v1 bind port"
    )
    return parser.parse_args(argv)


async def run(args: argparse.Namespace) -> int:
    settings = load_settings(GatewaySettings)
    configure_logging(service=settings.service_name, level=settings.log_level.value)

    try:
        authenticator = FileAuthenticator(args.tokens)
    except (OSError, ValueError) as error:
        _log.error("cannot read station tokens", extra={"error": str(error)})
        return 2

    engine = create_async_engine(str(settings.telemetry_database_url))
    archive = RawArchive(root=settings.archive_root)
    store = TimescaleIngestStore(engine=engine, archive=archive)

    bus = await nats.connect(str(settings.nats_url))
    # One publisher, two producers. The pipeline publishes what it parsed out
    # of the datagrams; the relay server publishes the health of the link that
    # carried them. The console needs both, and a station with no aircraft on
    # it produces only the second.
    publisher = TelemetryPublisher(bus=bus)
    # P1-05. Expiry is the definition of link lost, so the TTL is the same
    # setting that defines link loss everywhere else.
    redis_client = redis.asyncio.from_url(str(settings.redis_url))
    state_writer = BufferedStateWriter(
        inner=DroneStateWriter(engine=engine),
        flush_rows=settings.state_flush_rows,
        flush_interval_s=settings.state_flush_interval_s,
    )
    pipelines = StationPipelines(
        resolver=BindingResolver(engine=engine),
        writer=state_writer,
        publisher=publisher,
        firmware=FirmwareRegistry(engine=engine),
        live_state=LiveState(
            redis=redis_client, link_timeout_s=settings.link_timeout_s
        ),
    )

    server = RelayServer(
        store=store,
        authenticator=authenticator,
        processor=pipelines,
        station_reporter=publisher,
        lagging_after_s=settings.link_timeout_s,
        host=args.host,
        port=args.port,
    )

    async def on_switch(_: SourceControlState, __: SourceControlState) -> None:
        closed = await server.apply_source_control()
        if closed:
            _log.info("relay sessions closed by a switch", extra={"closed": closed})

    follower = SourceControlFollower(
        read=bucket_reader(bus, settings.source_control_bucket),
        on_change=on_switch,
        poll_s=settings.source_control_poll_s,
    )
    server.sources = SourceActivity(
        source_type=RELAY,
        switch=follower,
        known=authenticator.stations,
        connected=server.connected_stations,
    )
    # Before the server listens, so a station disabled before this start
    # is refused from its first attempt.
    control = await follow(bus, follower, subject=settings.source_control_subject)
    await server.start()
    _log.info(
        "gateway listening",
        extra={
            "relay_url": f"ws://{args.host}:{server.port_in_use}/relay/v1",
            "archive_root": str(settings.archive_root),
            "source_control_version": follower.state.version,
            "disabled": [f"{t}/{i or '*'}" for t, i in follower.state.disabled()],
        },
    )

    stopping = asyncio.Event()
    announcer = asyncio.create_task(
        publish_periodically(server.sources, publisher.bus, stopping)
    )

    # S-07. Retention had no caller: the archive was bounded by policy on
    # paper and by the disk in practice.
    sweeper: asyncio.Task[None] | None = None
    if settings.retention_sweep_enabled:
        schedule = RetentionSchedule(
            retention=ArchiveRetention(
                engine=engine,
                archive=archive,
                retention_days=settings.telemetry_retention_days,
                max_bytes_per_station=settings.archive_max_gib_per_station
                * BYTES_PER_GIB,
            ),
            store=store,
            interval_s=settings.retention_sweep_interval_s,
        )
        sweeper = asyncio.create_task(schedule.run_until(stopping))
        _log.info(
            "retention sweep scheduled",
            extra={
                "interval_s": settings.retention_sweep_interval_s,
                "retention_days": settings.telemetry_retention_days,
                "max_gib_per_station": settings.archive_max_gib_per_station,
            },
        )
    else:
        _log.warning(
            "retention sweep disabled; the archive is bounded only by the disk"
        )

    def stop() -> None:
        stopping.set()

    loop = asyncio.get_running_loop()
    for name in ("SIGINT", "SIGTERM"):
        with contextlib.suppress(NotImplementedError, AttributeError):
            # Windows has no add_signal_handler for SIGTERM; KeyboardInterrupt
            # covers the interactive case there, which is how it is run
            # locally until P0-08 moves development to WSL2.
            loop.add_signal_handler(getattr(signal, name), stop)

    try:
        await stopping.wait()
    finally:
        _log.info("gateway stopping")
        stopping.set()
        if sweeper is not None:
            try:
                await sweeper
            except Exception as error:
                # Shutdown goes on regardless: the server must stop and the
                # state writer must flush whatever the sweeper did.
                _log.error(
                    "retention task ended with an error", extra={"error": repr(error)}
                )
        await announcer
        await follower.stop()
        await control.unsubscribe()
        await server.stop()
        # After the server, so no batch can arrive once the last flush ran.
        await state_writer.close()
        await bus.drain()
        await redis_client.aclose()
        await engine.dispose()
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        return asyncio.run(run(parse_args(argv)))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())

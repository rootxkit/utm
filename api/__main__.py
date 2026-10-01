"""Run the core API: `python -m api`. P2-05, P2-06.

Loopback by default (`API_HOST`). Every route needs a signed-in operator
(P6-08); create the first admin with `python tools/operators.py create-admin`.

U-15: the API is the writer of the source switches, so it holds a NATS
connection for publishing them (`api/sources.py`). Without the bus it still
serves, and a switch answers 503 without changing anything.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import redis.asyncio
import uvicorn
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import create_async_engine

from api.app import create_api_app
from api.auth import OperatorStore
from api.config import ApiSettings
from api.live import RedisLiveState
from api.ratelimit import LoginRateLimiter
from api.registry import FleetRegistry
from api.replay import ReplayStore
from api.sources import (
    NatsControlChannel,
    SourceControlService,
    SourceControlStore,
    nats_channel_factory,
    republish_periodically,
)
from api.uas_registry import UasRegistry
from common import configure_logging, get_logger, load_settings
from common.terrain import Terrain
from gateway.binding import BindingResolver
from gateway.registry_projection import IdentityProjection

_log = get_logger(__name__)

# How long startup may wait for the bus before serving without it; see
# api/telemetry_ws.py CONNECT_TIMEOUT_S for why this is bounded.
NATS_CONNECT_TIMEOUT_S = 5.0


def build_app(settings: ApiSettings) -> FastAPI:
    engine = create_async_engine(str(settings.database_url))
    telemetry_engine = create_async_engine(str(settings.telemetry_database_url))
    redis_client = redis.asyncio.from_url(str(settings.redis_url))
    registry = FleetRegistry(
        engine=engine,
        projection=BindingResolver(engine=telemetry_engine),
        live=RedisLiveState(redis_client),
    )
    replay = ReplayStore(
        telemetry=telemetry_engine,
        relational=engine,
        gap_threshold_s=settings.replay_gap_threshold_s,
        evidence_slack_s=settings.replay_evidence_slack_s,
        flight_split_s=settings.replay_flight_split_s,
        max_samples=settings.replay_max_samples,
        max_flight_window_s=settings.replay_max_flight_window_s,
    )
    operators = OperatorStore(
        engine=engine,
        session_ttl_s=settings.session_ttl_s,
        idle_timeout_s=settings.session_idle_timeout_s,
        max_failed_logins=settings.login_max_failures,
        lockout_s=settings.login_lockout_s,
        max_concurrent_hashes=settings.password_hash_concurrency,
    )
    uas = UasRegistry(
        engine=engine,
        projection=registry.projection,
        registration_pattern=settings.registration_pattern,
        identity=IdentityProjection(engine=telemetry_engine),
    )
    sources = SourceControlService(
        store=SourceControlStore(engine=engine),
        channel=None,
        default_deny=settings.sources_default_deny,
        connect=nats_channel_factory(
            str(settings.nats_url),
            bucket=settings.source_control_bucket,
            subject=settings.source_control_subject,
            connect_timeout_s=NATS_CONNECT_TIMEOUT_S,
        ),
    )
    app = create_api_app(
        registry,
        sources=sources,
        uas=uas,
        auth=operators,
        feed_secret=settings.feed_ticket_secret.get_secret_value().encode("utf-8"),
        feed_ticket_ttl_s=settings.feed_ticket_ttl_s,
        cookie_secure=settings.cookie_secure,
        replay=replay,
        basemap_dir=settings.basemap_dir,
        console_feed_url=settings.console_feed_url,
        console_app_dir=settings.console_app_dir,
        terrain=Terrain(settings.terrain_dir) if settings.terrain_dir else None,
        zone_max_ring_vertices=settings.zone_max_ring_vertices,
        login_limiter=LoginRateLimiter(
            max_per_address=settings.login_rate_max_per_address,
            max_per_username=settings.login_rate_max_per_username,
            window_s=settings.login_rate_window_s,
        ),
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        # The first connection is made by the republish loop's first pass,
        # and remade by any later pass that finds it missing or closed.
        stop = asyncio.Event()
        republisher = asyncio.create_task(
            republish_periodically(
                sources, stop, every_s=settings.source_control_republish_s
            )
        )
        # U-02: the identity projection, re-made at start and periodically.
        projector = asyncio.create_task(
            uas.sync_projection_periodically(
                stop, every_s=settings.registry_projection_sync_s
            )
        )
        try:
            yield
        finally:
            stop.set()
            await republisher
            await projector
            channel = sources.channel
            if isinstance(channel, NatsControlChannel) and not channel.closed:
                await channel.client.drain()
            await redis_client.aclose()
            await asyncio.gather(engine.dispose(), telemetry_engine.dispose())

    app.router.lifespan_context = lifespan
    return app


def main() -> None:
    settings = load_settings(ApiSettings)
    configure_logging(service=settings.service_name, level=settings.log_level.value)
    uvicorn.run(
        build_app(settings),
        host=settings.api_host,
        port=settings.api_port,
        **settings.uvicorn_proxy_options(),
    )


if __name__ == "__main__":
    main()

"""Run the core API: `python -m api`. P2-05, P2-06.

Loopback by default (`API_HOST`). Every route needs a signed-in operator
(P6-08); create the first admin with `python tools/operators.py create-admin`.
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
from common import configure_logging, load_settings
from common.terrain import Terrain
from gateway.binding import BindingResolver


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
    )
    app = create_api_app(
        registry,
        auth=operators,
        feed_secret=settings.feed_ticket_secret.get_secret_value().encode("utf-8"),
        feed_ticket_ttl_s=settings.feed_ticket_ttl_s,
        cookie_secure=settings.cookie_secure,
        replay=replay,
        basemap_dir=settings.basemap_dir,
        console_feed_url=settings.console_feed_url,
        console_app_dir=settings.console_app_dir,
        terrain=Terrain(settings.terrain_dir) if settings.terrain_dir else None,
        login_limiter=LoginRateLimiter(
            max_per_address=settings.login_rate_max_per_address,
            max_per_username=settings.login_rate_max_per_username,
            window_s=settings.login_rate_window_s,
        ),
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
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

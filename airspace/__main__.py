"""Run the airspace monitor: `python -m airspace`. P5-06, P5-07, P5-19, U-03.

Reads the separation policy, the height limit and the zones from the
relational database, the terrain tiles from `TERRAIN_DIR` and the geoid
from `GEOID_PATH`, then follows the Gateway's telemetry on the bus. Zones
are re-read every `ZONE_REFRESH_S` (60 s), and so are the separation policy,
the height limit and the CONDITIONAL zone severity, so a zone drawn in the
console, imported, edited or deleted takes effect within a minute without a
restart; a change is logged.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal

import nats
from nats.aio.msg import Msg
from sqlalchemy.ext.asyncio import create_async_engine

from airspace.config import AirspaceSettings
from airspace.ed269 import Restriction
from airspace.monitor import AirspaceMonitor, Severity
from airspace.policy import (
    load_conditional_zone_severity,
    load_height_limit,
    load_policy,
)
from airspace.service import AirspaceService, EventsAuditLog, run_ticker
from airspace.zones import Zone, load_zones, unjudgeable
from common import configure_logging, get_logger, load_settings
from common.geoid import GeoidGrid
from common.terrain import Terrain

_log = get_logger(__name__)

TICK_S = 1.0
# How long an edited zone can take to reach the monitor. A cheap query; a
# push on change is U-04's (dynamic restrictions within one tick).
ZONE_REFRESH_S = 60.0


def warn_unjudgeable_zones(
    zones: list[Zone],
    *,
    terrain: Terrain | None,
    geoid: GeoidGrid | None,
    before: dict[str, list[str]] | None,
) -> dict[str, list[str]]:
    """Log, when it changes, which zones have limits nothing configured can
    judge; such a zone is never evaluated vertically (U-03)."""
    missing = unjudgeable(zones, terrain=terrain is not None, geoid=geoid is not None)
    if missing != before and missing:
        prohibited = {
            zone.identifier
            for zone in zones
            if zone.restriction is Restriction.PROHIBITED
        }
        unjudged_prohibited = sorted(set(missing.get("TERRAIN_DIR", [])) & prohibited)
        # A PROHIBITED zone whose AGL limit cannot be judged raises a warning
        # where it should raise critical: an error, not a warning.
        (_log.error if unjudged_prohibited else _log.warning)(
            "zones with limits that cannot be evaluated: configure what they need",
            extra={"missing": missing, "prohibited": unjudged_prohibited},
        )
    return missing


async def run(settings: AirspaceSettings) -> None:
    engine = create_async_engine(str(settings.database_url))
    policy = await load_policy(engine)
    max_height_agl_m = await load_height_limit(engine)
    terrain = (
        None
        if settings.terrain_dir is None
        else Terrain(
            settings.terrain_dir,
            max_tiles=settings.terrain_cache_tiles,
            retry_missing_s=settings.terrain_retry_missing_s,
        )
    )
    if max_height_agl_m is not None and terrain is None:
        _log.warning(
            "a height limit is set but TERRAIN_DIR is not; the limit will not "
            "be evaluated",
            extra={"max_height_agl_m": max_height_agl_m},
        )
    geoid = None if settings.geoid_path is None else GeoidGrid.load(settings.geoid_path)
    zones = await load_zones(engine)
    monitor = AirspaceMonitor(
        policy=policy,
        zones=zones,
        terrain=terrain,
        geoid=geoid,
        conditional_severity=Severity(await load_conditional_zone_severity(engine)),
        max_height_agl_m=max_height_agl_m,
        live_max_age_s=settings.live_max_age_s,
        neighbour_max_age_s=settings.neighbour_max_age_s,
        source_state_max=settings.source_state_max,
        pressure_uncertainty_m=settings.pressure_uncertainty_m,
    )
    bus = await nats.connect(str(settings.nats_url))
    service = AirspaceService(
        monitor=monitor,
        bus=bus,
        audit=EventsAuditLog(engine),
        audit_queue_size=settings.audit_queue_size,
        close_timeout_s=settings.audit_close_timeout_s,
        tiles=terrain,
        tile_log_every_s=settings.terrain_retry_missing_s,
    )
    _log.info(
        "airspace monitor running",
        extra={
            "t_cpa_max_s": policy.t_cpa_max_s,
            "d_horizontal_min_m": policy.d_horizontal_min_m,
            "d_vertical_min_m": policy.d_vertical_min_m,
            "neighbour_radius_m": policy.neighbour_radius_m,
            "zones": len(monitor.zones),
            "max_height_agl_m": max_height_agl_m,
            "terrain_dir": str(settings.terrain_dir) if settings.terrain_dir else None,
            "geoid_path": str(settings.geoid_path) if settings.geoid_path else None,
            "conditional_zone_severity": monitor.conditional_severity.value,
            "zone_refresh_s": ZONE_REFRESH_S,
        },
    )
    unjudged = warn_unjudgeable_zones(zones, terrain=terrain, geoid=geoid, before=None)

    async def on_message(message: Msg) -> None:
        await service.on_telemetry(message.data)

    await bus.subscribe("telemetry.*", cb=on_message)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        # Windows has no add_signal_handler; Ctrl+C still raises there.
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    async def refresh() -> None:
        nonlocal unjudged
        # Read everything before changing anything, so a failure part-way
        # leaves the monitor consistent with one database state.
        zones = await load_zones(engine)
        new_policy = await load_policy(engine)
        new_limit_m = await load_height_limit(engine)
        conditional = Severity(await load_conditional_zone_severity(engine))
        if [z.zone_id for z in zones] != [z.zone_id for z in monitor.zones] or any(
            new != old for new, old in zip(zones, monitor.zones, strict=True)
        ):
            _log.info("zones changed", extra={"zones": len(zones)})
        monitor.zones = zones
        unjudged = warn_unjudgeable_zones(
            zones, terrain=terrain, geoid=geoid, before=unjudged
        )
        if conditional is not monitor.conditional_severity:
            _log.info(
                "conditional zone severity changed",
                extra={
                    "before": monitor.conditional_severity.value,
                    "after": conditional.value,
                },
            )
            monitor.conditional_severity = conditional
        if new_limit_m != monitor.max_height_agl_m:
            _log.info(
                "height limit changed",
                extra={"before": monitor.max_height_agl_m, "after": new_limit_m},
            )
            monitor.max_height_agl_m = new_limit_m
        monitor.update_policy(new_policy)

    task = asyncio.create_task(
        run_ticker(
            service,
            stop=stop,
            tick_s=TICK_S,
            refresh_every_s=ZONE_REFRESH_S,
            refresh=refresh,
        )
    )
    try:
        await stop.wait()
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await bus.drain()
        # Queued audit rows are written before the engine goes.
        await service.close()
        await engine.dispose()


def main() -> None:
    settings = load_settings(AirspaceSettings)
    configure_logging(service=settings.service_name, level=settings.log_level.value)
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run(settings))


if __name__ == "__main__":
    main()

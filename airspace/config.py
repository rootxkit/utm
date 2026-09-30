"""Airspace configuration."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field

from common import NatsSettings, PostgresSettings, RedisSettings, ServiceSettings


class AirspaceSettings(ServiceSettings, PostgresSettings, RedisSettings, NatsSettings):
    """Everything the airspace monitor needs to start.

    Separation minima, height limits and alert thresholds are not here. They
    are airspace policy, they are edited by operators, and they belong in the
    database where a change is audited (P5-07, P5-19).
    """

    service_name: str = "airspace"
    # P5-19. The same tiles the API serves (P5-00). Unset: the height limit
    # in airspace_policy is not evaluated, and the service says so.
    terrain_dir: Path | None = Field(default=None, validation_alias="TERRAIN_DIR")
    # S-13. Terrain tiles held in memory, least recently used out. A tile is
    # a 1 x 1 degree cell of about 26 MB, so 8 is about 210 MB: an operating
    # area and every cell around it, bounded below what a small container
    # allows, where the old unbounded cache grew with every cell ever flown.
    terrain_cache_tiles: int = Field(
        default=8, ge=1, validation_alias="TERRAIN_CACHE_TILES"
    )
    # S-11. A telemetry message delivered more than this later than its
    # source's usual delay is not live: a backlog replayed after an outage.
    # It is counted and logged, never evaluated as "now". Delay is measured
    # against the source's own clock offset (`airspace/clock.py`), so a
    # ground-station clock that is wrong by any constant amount costs no
    # alerts. Live delivery is under 2 s end to end, and this is kept below
    # the 15 s after which an aircraft is dropped as stale, so an accepted
    # message is evaluated before it is already stale.
    live_max_age_s: float = Field(default=10.0, gt=0, validation_alias="LIVE_MAX_AGE_S")
    # S-11. How fast a source's clock offset estimate (a running minimum of
    # `wall - ts`) relaxes upwards, in seconds per second. It has to move at
    # all, or a clock stepped backwards would look like a backlog for ever;
    # at 0.1 a 60 s step is absorbed in 10 minutes, while a backlog replay,
    # which drains many times faster than real time, cannot hide behind it.
    clock_relax_s_per_s: float = Field(
        default=0.1, ge=0, validation_alias="CLOCK_RELAX_S_PER_S"
    )
    # S-11. A neighbour's latest sample older than this, relative to the
    # subject's capture time, is left out of the CPA check. Younger ones are
    # advanced along their velocity to the subject's capture time. The
    # advance is a straight line; over 10 s a multirotor can have turned
    # through any angle, so beyond that the line says nothing.
    neighbour_max_age_s: float = Field(
        default=10.0, gt=0, validation_alias="NEIGHBOUR_MAX_AGE_S"
    )
    # S-13. Audit rows waiting for the background writer. Transitions are
    # rare (the 2026-09-29 SITL run wrote 16 rows in four minutes), so 1000
    # entries, each a small tuple, cover hours of a slow or absent database
    # before a row is dropped, while a flapping fleet stays bounded.
    audit_queue_size: int = Field(
        default=1000, ge=1, validation_alias="AUDIT_QUEUE_SIZE"
    )

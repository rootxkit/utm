"""Airspace configuration."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field

from common import (
    NatsSettings,
    PostgresSettings,
    RedisSettings,
    ServiceSettings,
    SourceControlSettings,
)


class AirspaceSettings(
    ServiceSettings,
    PostgresSettings,
    RedisSettings,
    NatsSettings,
    SourceControlSettings,
):
    """Everything the airspace monitor needs to start.

    Separation minima, height limits and alert thresholds are not here. They
    are airspace policy, they are edited by operators, and they belong in the
    database where a change is audited (P5-07, P5-19).
    """

    service_name: str = "airspace"
    # P5-19. The same tiles the API serves (P5-00). Unset: the height limit
    # in airspace_policy is not evaluated, and the service says so.
    terrain_dir: Path | None = Field(default=None, validation_alias="TERRAIN_DIR")
    # U-03. The geoid grid the Remote ID ingest uses (P1-15), for zone limits
    # given above the WGS-84 ellipsoid. Unset: those limits are not
    # evaluated, and the service says so.
    geoid_path: Path | None = Field(default=None, validation_alias="GEOID_PATH")
    # S-13. Terrain tiles held in memory, least recently used out. A tile is
    # a 1 x 1 degree cell of about 26 MB, so 8 is about 210 MB: an operating
    # area and every cell around it, bounded below what a small container
    # allows, where the old unbounded cache grew with every cell ever flown.
    terrain_cache_tiles: int = Field(
        default=8, ge=1, validation_alias="TERRAIN_CACHE_TILES"
    )
    # A listed tile that cannot be read is remembered as missing for this
    # long before the disk is tried again, and its failure is logged once
    # per this interval per cell with a count. A minute notices an operator
    # copying the file in soon enough, and costs one failed open a minute
    # instead of one, with a traceback, per telemetry message.
    terrain_retry_missing_s: float = Field(
        default=60.0, gt=0, validation_alias="TERRAIN_RETRY_MISSING_S"
    )
    # S-11. A telemetry message that reaches the monitor more than this
    # after the Gateway received it (`rx_ts`) is late: the bus or this
    # service is behind. It is counted and logged, never evaluated as "now".
    # Only the Gateway-to-monitor leg is judged; whether a record is a
    # replayed backlog is the Gateway's own flag, and a wrong station clock
    # or a slow Gateway costs no alerts. Live delivery on this leg is well
    # under a second, and this is kept below the 15 s after which an
    # aircraft is dropped as stale, so an accepted message is evaluated
    # before it is already stale.
    live_max_age_s: float = Field(default=10.0, gt=0, validation_alias="LIVE_MAX_AGE_S")
    # S-11. Per (aircraft, station) ordering state kept, least recently seen
    # out. One entry per pair actually reporting; 4096 is far beyond any
    # fleet and station count here, and a stream of new station ids (a
    # misconfigured relay) is bounded instead of growing memory.
    source_state_max: int = Field(
        default=4096, ge=1, validation_alias="SOURCE_STATE_MAX"
    )
    # S-11. A neighbour's latest sample older than this, relative to the
    # subject's capture time, is left out of the CPA check. Younger ones are
    # advanced along their velocity to the subject's capture time. The
    # advance is a straight line; over 10 s a multirotor can have turned
    # through any angle, so beyond that the line says nothing.
    neighbour_max_age_s: float = Field(
        default=10.0, gt=0, validation_alias="NEIGHBOUR_MAX_AGE_S"
    )
    # S-33. How far a Remote ID pressure altitude may be from AMSL: about
    # 8 m per hPa between the local QNH and 1013.25 hPa, so 250 m covers a
    # 30 hPa day. For an aircraft on pressure altitude, a zone's band is
    # widened by it (inside the widened band only: a warning), and alerts
    # say the altitude is approximate to within it.
    pressure_uncertainty_m: float = Field(
        default=250.0, ge=0, validation_alias="PRESSURE_UNCERTAINTY_M"
    )
    # U-02. What an `identification` alert raises (an unidentified or
    # unknown-operator aircraft in a PROHIBITED or REQ_AUTHORISATION zone,
    # the incident seam), and an `identification_mismatch` alert.
    identification_alert_severity: Literal["critical", "warning", "info"] = Field(
        default="critical", validation_alias="IDENTIFICATION_ALERT_SEVERITY"
    )
    identification_mismatch_severity: Literal["critical", "warning", "info"] = Field(
        default="warning", validation_alias="IDENTIFICATION_MISMATCH_SEVERITY"
    )
    # S-13. Audit rows waiting for the background writer. Transitions are
    # rare (the 2026-09-29 SITL run wrote 16 rows in four minutes), so 1000
    # entries, each a small tuple, cover hours of a slow or absent database
    # before a row is dropped, while a flapping fleet stays bounded.
    audit_queue_size: int = Field(
        default=1000, ge=1, validation_alias="AUDIT_QUEUE_SIZE"
    )
    # S-13. How long shutdown waits for queued audit rows to be written.
    # Rows still queued after it are abandoned, counted and logged. Five
    # seconds is a few database round trips, and well inside the grace a
    # container gets before SIGKILL.
    audit_close_timeout_s: float = Field(
        default=5.0, gt=0, validation_alias="AUDIT_CLOSE_TIMEOUT_S"
    )

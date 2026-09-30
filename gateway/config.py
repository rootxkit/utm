"""Gateway configuration."""

from __future__ import annotations

import ipaddress
from pathlib import Path
from typing import Self

from pydantic import Field, model_validator

from common import (
    NatsSettings,
    RedisSettings,
    ServiceSettings,
    TelemetryDatabaseSettings,
)


class GatewaySettings(
    ServiceSettings, TelemetryDatabaseSettings, RedisSettings, NatsSettings
):
    """Everything the Gateway needs to start.

    The Gateway writes telemetry to TimescaleDB, live state to Redis, and
    publishes to NATS. It does not touch the relational database.
    """

    service_name: str = "gateway"

    # UDP socket the MAVLink stream arrives on. Whatever is on the other end —
    # QGC forwarding at Stage 0, mavlink-router at Stage 1, an onboard agent at
    # Stage 2 — is deliberately not this service's concern.
    mavlink_bind_host: str = Field(
        default="0.0.0.0", validation_alias="MAVLINK_BIND_HOST"
    )
    mavlink_bind_port: int = Field(
        default=14445, ge=1, le=65535, validation_alias="MAVLINK_BIND_PORT"
    )

    # A vehicle whose state has not been refreshed within this window is
    # treated as link-lost; it matches the Redis TTL in ARCHITECTURE.md §1.
    link_timeout_s: float = Field(
        default=15.0, gt=0.0, validation_alias="LINK_TIMEOUT_S"
    )

    # P1-04: drone_state is inserted when this many rows are buffered or this
    # long after the first one, whichever comes first.
    state_flush_rows: int = Field(
        default=100, ge=1, validation_alias="STATE_FLUSH_ROWS"
    )
    state_flush_interval_s: float = Field(
        default=0.5, gt=0.0, validation_alias="STATE_FLUSH_INTERVAL_S"
    )

    # Where the raw archive's hourly segments live. Server-side, not on a
    # pilot's laptop: the laptop's disk is protected by the relay's queue cap
    # (P7-11), which is a different mechanism for the same principle.
    archive_root: Path = Field(
        default=Path("/var/lib/courier/archive"), validation_alias="ARCHIVE_ROOT"
    )

    # Ceiling on the archive per station. Retention is normally by age - see
    # `telemetry_retention_days`, which is shared with P1-04 - and this is the
    # bound that applies when a station produces more than expected before the
    # period expires.
    #
    # Bounded by policy, never by disk exhaustion. The relay reports a cap drop
    # to the Gateway as a `gap`; the Gateway has nobody downstream to report to,
    # so the ingest_events row written on deletion is the entire audit trail.
    archive_max_gib_per_station: int = Field(
        default=250, ge=1, validation_alias="ARCHIVE_MAX_GIB_PER_STATION"
    )

    # S-07: the retention pass (`gateway/retention.py`) runs inside the
    # Gateway on this interval. Hourly is plenty: the policy is in days.
    # The switch exists for an operator who wants sweeps run from elsewhere,
    # or paused during an investigation; off, nothing is ever deleted, and
    # the archive is bounded by the disk, which the log says at startup.
    retention_sweep_enabled: bool = Field(
        default=True, validation_alias="RETENTION_SWEEP_ENABLED"
    )
    retention_sweep_interval_s: float = Field(
        default=3600.0, gt=0.0, validation_alias="RETENTION_SWEEP_INTERVAL_S"
    )


class RemoteIdSettings(ServiceSettings, NatsSettings, TelemetryDatabaseSettings):
    """Remote ID ingest (P1-15): receiver datagrams in, telemetry out.

    Needs the bus, and the telemetry database, where it keeps what it heard
    (`remote_id_observations`). Never the relational one.
    """

    service_name: str = "remote-id-ingest"
    remote_id_bind_host: str = Field(
        default="127.0.0.1", validation_alias="REMOTE_ID_BIND_HOST"
    )
    # A file of `receiver_id: base64 key` lines (tools/remote_id_keys.py).
    # Unset: unsigned datagrams are accepted, so the bind must be loopback.
    remote_id_receiver_keys: Path | None = Field(
        default=None, validation_alias="REMOTE_ID_RECEIVER_KEYS"
    )
    # How far a signed report's time may be from the ingest's before it is
    # refused as a replay. Receivers need a clock set to within this (NTP).
    remote_id_max_skew_s: float = Field(
        default=30.0, gt=0, validation_alias="REMOTE_ID_MAX_SKEW_S"
    )
    remote_id_bind_port: int = Field(
        default=14600, ge=1, le=65535, validation_alias="REMOTE_ID_BIND_PORT"
    )
    # A geoid model file (EGM2008 by default, infra/geoid/fetch_geoid.sh).
    # Without one, Remote ID aircraft have no AMSL altitude and the airspace
    # monitor does not evaluate them.
    geoid_path: Path | None = Field(default=None, validation_alias="GEOID_PATH")

    @model_validator(mode="after")
    def unsigned_only_on_loopback(self) -> Self:
        """Without receiver keys, anything that reaches the port can put an
        aircraft on the map and in the airspace monitor. That is acceptable
        only when nothing but this host can reach it."""
        if self.remote_id_receiver_keys is None and not _is_loopback(
            self.remote_id_bind_host
        ):
            raise ValueError(
                f"REMOTE_ID_BIND_HOST={self.remote_id_bind_host} accepts "
                "datagrams from other hosts; set REMOTE_ID_RECEIVER_KEYS so "
                "they must be signed, or bind to 127.0.0.1"
            )
        return self


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        # A host name other than localhost may resolve anywhere.
        return False

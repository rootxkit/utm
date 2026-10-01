"""Gateway configuration."""

from __future__ import annotations

import ipaddress
from pathlib import Path
from typing import Self

from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)

from common import (
    NatsSettings,
    RedisSettings,
    ServiceSettings,
    Settings,
    SourceControlSettings,
    TelemetryDatabaseSettings,
)
from common.sources import INSTANCE_ID_PATTERN
from gateway.network_rid import (
    DEFAULT_DETAILS_CONCURRENCY,
    DEFAULT_DETAILS_TTL_S,
    DEFAULT_MAX_AGE_S,
    DEFAULT_MAX_BODY_BYTES,
    DEFAULT_MAX_DETAILS_PER_POLL,
    DEFAULT_MAX_DIAGONAL_KM,
    DEFAULT_MAX_FLIGHTS_PER_RESPONSE,
    DEFAULT_MAX_TILES_PER_POLL,
    DEFAULT_POLL_DEADLINE_S,
    Area,
)
from gateway.network_rid import DEFAULT_SCOPE as DEFAULT_NETWORK_RID_SCOPE
from gateway.registry_projection import DEFAULT_REFRESH_S
from gateway.remote_id import (
    DEFAULT_IDENTIFY_WITHIN_S,
    DEFAULT_IDENTITY_TTL_S,
    DEFAULT_MAX_GAP_S,
    DEFAULT_MAX_LATENCY_S,
    DEFAULT_MIN_VERTICAL_ACCURACY,
    DEFAULT_PRESSURE_HOLD_S,
    DEFAULT_TIME_TOLERANCE_S,
)
from gateway.remote_id_match import DEFAULT_SPOOF_DISTANCE_M


class RegistryProjectionSettings(Settings):
    """How often an adapter re-reads the registry projection (U-02,
    `gateway/registry_projection.py`): a registry change reaches its
    resolver within this, plus the API's transaction."""

    registry_refresh_s: float = Field(
        default=DEFAULT_REFRESH_S, gt=0, validation_alias="REGISTRY_REFRESH_S"
    )


class GatewaySettings(
    ServiceSettings,
    TelemetryDatabaseSettings,
    RedisSettings,
    NatsSettings,
    SourceControlSettings,
    RegistryProjectionSettings,
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


class RemoteIdSettings(
    ServiceSettings,
    NatsSettings,
    TelemetryDatabaseSettings,
    SourceControlSettings,
    RegistryProjectionSettings,
):
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
    # S-27 (gateway/remote_id.py, "Time"). How far ahead of the ingest's
    # clock a broadcast's own time may be, and how old it may be on arrival,
    # for the aircraft to be placed at it rather than at its receive time.
    remote_id_time_tolerance_s: float = Field(
        default=DEFAULT_TIME_TOLERANCE_S,
        ge=0,
        validation_alias="REMOTE_ID_TIME_TOLERANCE_S",
    )
    remote_id_max_latency_s: float = Field(
        default=DEFAULT_MAX_LATENCY_S,
        gt=0,
        validation_alias="REMOTE_ID_MAX_LATENCY_S",
    )
    # S-33. The poorest declared vertical accuracy (MAV_ODID_VER_ACC code,
    # 1 to 6 for under 150, 45, 25, 10, 3 and 1 m) at which a broadcast's
    # geodetic altitude is used; below it, its pressure altitude is.
    remote_id_min_vertical_accuracy: int = Field(
        default=DEFAULT_MIN_VERTICAL_ACCURACY,
        ge=1,
        le=6,
        validation_alias="REMOTE_ID_MIN_VERTICAL_ACCURACY",
    )
    # S-33. Once on pressure altitude, how long a transmitter stays on it
    # after its last poor geodetic altitude, so the source does not flip.
    remote_id_pressure_hold_s: float = Field(
        default=DEFAULT_PRESSURE_HOLD_S,
        ge=0,
        validation_alias="REMOTE_ID_PRESSURE_HOLD_S",
    )
    # S-32 (gateway/remote_id.py, "An identity is used only while it is
    # fresh"). How long a Basic ID names its transmitter's Locations; how
    # long a silence ends everything known about a transmitter address; how
    # long a Location waits for a Basic ID before it is published as an
    # unidentified track.
    remote_id_identity_ttl_s: float = Field(
        default=DEFAULT_IDENTITY_TTL_S,
        gt=0,
        validation_alias="REMOTE_ID_IDENTITY_TTL_S",
    )
    remote_id_max_gap_s: float = Field(
        default=DEFAULT_MAX_GAP_S, gt=0, validation_alias="REMOTE_ID_MAX_GAP_S"
    )
    remote_id_identify_within_s: float = Field(
        default=DEFAULT_IDENTIFY_WITHIN_S,
        ge=0,
        validation_alias="REMOTE_ID_IDENTIFY_WITHIN_S",
    )
    # S-10 (U-02). How far a broadcast of one of our serials may be from
    # where that aircraft's live relay telemetry places it and still be it;
    # beyond, it is a separate unverified track (`gateway/remote_id_match.py`).
    remote_id_spoof_distance_m: float = Field(
        default=DEFAULT_SPOOF_DISTANCE_M,
        gt=0,
        validation_alias="REMOTE_ID_SPOOF_DISTANCE_M",
    )

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


class NetworkRidProvider(BaseModel):
    """One ASTM F3411 Service Provider polled as a Display Provider (U-02).

    `id` is the instance U-15 switches and the `station_id` its tracks
    carry. `areas` are `[lat_min, lon_min, lat_max, lon_max]` boxes.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(pattern=INSTANCE_ID_PATTERN.pattern)
    base_url: AnyHttpUrl
    token_url: AnyHttpUrl
    client_id: str = Field(min_length=1)
    client_secret: SecretStr
    scope: str = DEFAULT_NETWORK_RID_SCOPE
    audience: str | None = None
    areas: list[tuple[float, float, float, float]] = Field(min_length=1)

    @model_validator(mode="after")
    def _https_off_this_host(self) -> Self:
        """A client secret and a bearer token are sent to these URLs: in
        clear only to this host (the fake SP for SITL)."""
        for name, url in (("base_url", self.base_url), ("token_url", self.token_url)):
            if url.scheme != "https" and not _is_loopback(url.host or ""):
                raise ValueError(
                    f"provider {self.id}: {name} {url} is plain HTTP off this host; "
                    "use https"
                )
        return self

    @field_validator("areas")
    @classmethod
    def _areas_are_boxes(
        cls, value: list[tuple[float, float, float, float]]
    ) -> list[tuple[float, float, float, float]]:
        for box in value:
            Area(*box)  # raises ValueError for a box out of order or range
        return value


class NetworkRidSettings(
    ServiceSettings,
    NatsSettings,
    TelemetryDatabaseSettings,
    SourceControlSettings,
    RegistryProjectionSettings,
):
    """Network Remote ID ingest (U-02): USSP flights in, telemetry out.

    Needs the bus and the telemetry database (the registry projection).
    Never the relational one.
    """

    service_name: str = "network-rid-ingest"
    # JSON, one object per provider (gateway/network_rid_ingest.py). Empty:
    # nothing is polled, and the service says so.
    network_rid_providers: list[NetworkRidProvider] = Field(
        default_factory=list, validation_alias="NETWORK_RID_PROVIDERS"
    )
    # How often each provider is polled. F3411 asks a Display Provider to
    # refresh at least once a second for a display that is current.
    network_rid_poll_s: float = Field(
        default=1.0, gt=0, validation_alias="NETWORK_RID_POLL_S"
    )
    # F3411-22a NetMaxDisplayAreaDiagonalKm (7 km; v19's was 3.6 km): the
    # largest view an SP answers, so an area is polled in tiles no larger.
    network_rid_max_diagonal_km: float = Field(
        default=DEFAULT_MAX_DIAGONAL_KM,
        gt=0,
        validation_alias="NETWORK_RID_MAX_DIAGONAL_KM",
    )
    # F3411 NetMaxNearRealTimeDataPeriod: a state older than this is not
    # shown as current.
    network_rid_max_age_s: float = Field(
        default=DEFAULT_MAX_AGE_S, gt=0, validation_alias="NETWORK_RID_MAX_AGE_S"
    )
    # How long a flight's details (serial, operator) are reused.
    network_rid_details_ttl_s: float = Field(
        default=DEFAULT_DETAILS_TTL_S,
        gt=0,
        validation_alias="NETWORK_RID_DETAILS_TTL_S",
    )
    network_rid_http_timeout_s: float = Field(
        default=5.0, gt=0, validation_alias="NETWORK_RID_HTTP_TIMEOUT_S"
    )
    # What one poll of one provider may cost us, whatever it sends: a body
    # over this is refused unread; flights past the cap in one response,
    # tiles past the cap in one poll (413 splits included) and details past
    # the cap are counted and left; details are fetched this many at a time;
    # a poll stops at its deadline and keeps what had arrived.
    network_rid_max_body_bytes: int = Field(
        default=DEFAULT_MAX_BODY_BYTES,
        ge=1024,
        validation_alias="NETWORK_RID_MAX_BODY_BYTES",
    )
    network_rid_max_flights_per_response: int = Field(
        default=DEFAULT_MAX_FLIGHTS_PER_RESPONSE,
        ge=1,
        validation_alias="NETWORK_RID_MAX_FLIGHTS_PER_RESPONSE",
    )
    network_rid_max_tiles_per_poll: int = Field(
        default=DEFAULT_MAX_TILES_PER_POLL,
        ge=1,
        validation_alias="NETWORK_RID_MAX_TILES_PER_POLL",
    )
    network_rid_max_details_per_poll: int = Field(
        default=DEFAULT_MAX_DETAILS_PER_POLL,
        ge=1,
        validation_alias="NETWORK_RID_MAX_DETAILS_PER_POLL",
    )
    network_rid_details_concurrency: int = Field(
        default=DEFAULT_DETAILS_CONCURRENCY,
        ge=1,
        validation_alias="NETWORK_RID_DETAILS_CONCURRENCY",
    )
    network_rid_poll_deadline_s: float = Field(
        default=DEFAULT_POLL_DEADLINE_S,
        gt=0,
        validation_alias="NETWORK_RID_POLL_DEADLINE_S",
    )
    # S-10: the same guard as the Remote ID ingest's, for flights of ours.
    remote_id_spoof_distance_m: float = Field(
        default=DEFAULT_SPOOF_DISTANCE_M,
        gt=0,
        validation_alias="REMOTE_ID_SPOOF_DISTANCE_M",
    )
    # Only when a response carries no timestamp of its own: how far the
    # state's time may be ahead of ours, or behind, and still place it.
    network_rid_time_tolerance_s: float = Field(
        default=DEFAULT_TIME_TOLERANCE_S,
        ge=0,
        validation_alias="NETWORK_RID_TIME_TOLERANCE_S",
    )
    network_rid_max_latency_s: float = Field(
        default=DEFAULT_MAX_LATENCY_S,
        gt=0,
        validation_alias="NETWORK_RID_MAX_LATENCY_S",
    )
    # The same grid as the Remote ID ingest: F3411 altitudes are above the
    # WGS-84 ellipsoid.
    geoid_path: Path | None = Field(default=None, validation_alias="GEOID_PATH")

    @field_validator("network_rid_providers")
    @classmethod
    def _unique_ids(cls, value: list[NetworkRidProvider]) -> list[NetworkRidProvider]:
        ids = [provider.id for provider in value]
        if len(ids) != len(set(ids)):
            raise ValueError(f"provider ids must be unique: {ids}")
        return value


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        # A host name other than localhost may resolve anywhere.
        return False

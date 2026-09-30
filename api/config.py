"""Core API configuration."""

from __future__ import annotations

from pathlib import Path
from typing import Self

from pydantic import Field, SecretStr, model_validator

from common import NatsSettings, PostgresSettings, RedisSettings, ServiceSettings
from common.config import Environment, TelemetryDatabaseSettings

# The feed secret that ships in infra/.env.example. Refused outside dev.
_EXAMPLE_FEED_SECRET_PREFIX = "dev-only-"


class FeedTicketSettings(ServiceSettings):
    """P6-08. Shared by the API, which signs console-feed tickets, and the
    console, which checks them without a database."""

    feed_ticket_secret: SecretStr = Field(
        validation_alias="FEED_TICKET_SECRET", min_length=32
    )
    # How long a ticket lets a browser hold the console feed. Also the
    # longest a revoked session keeps receiving it.
    feed_ticket_ttl_s: float = Field(
        default=600.0, gt=0, validation_alias="FEED_TICKET_TTL_S"
    )

    @model_validator(mode="after")
    def _reject_example_secret_outside_dev(self) -> Self:
        if (
            self.env is not Environment.DEV
            and self.feed_ticket_secret.get_secret_value().startswith(
                _EXAMPLE_FEED_SECRET_PREFIX
            )
        ):
            raise ValueError(
                "FEED_TICKET_SECRET is the example value while "
                f"COURIER_ENV={self.env.value}"
            )
        return self


class ApiSettings(
    FeedTicketSettings,
    PostgresSettings,
    TelemetryDatabaseSettings,
    RedisSettings,
    NatsSettings,
):
    """Everything the core API needs to start.

    The telemetry database too: registering a drone writes its projection
    there (P2-05), because the Gateway cannot read this service's database.
    """

    service_name: str = "api"

    # Loopback by default. Operators sign in (P6-08), but the API is exposed
    # to other machines only through the TLS front of P0-09.
    api_host: str = Field(default="127.0.0.1", validation_alias="API_HOST")
    api_port: int = Field(default=8010, ge=1, le=65535, validation_alias="API_PORT")

    # P6-08. A session ends at the first of: this long after sign-in, this
    # long unused, or being revoked.
    session_ttl_s: float = Field(
        default=12 * 3600.0, gt=0, validation_alias="SESSION_TTL_S"
    )
    session_idle_timeout_s: float = Field(
        default=3600.0, gt=0, validation_alias="SESSION_IDLE_TIMEOUT_S"
    )
    # Failed sign-ins in a row before an account is locked, and for how long.
    login_max_failures: int = Field(
        default=5, ge=1, validation_alias="LOGIN_MAX_FAILURES"
    )
    login_lockout_s: float = Field(
        default=900.0, gt=0, validation_alias="LOGIN_LOCKOUT_S"
    )
    # S-15. Sign-in attempts allowed per client address and per username
    # within the window, before any password is hashed. Beyond them the
    # API answers 429. Unknown usernames are counted like known ones.
    login_rate_window_s: float = Field(
        default=300.0, gt=0, validation_alias="LOGIN_RATE_WINDOW_S"
    )
    login_rate_max_per_address: int = Field(
        default=20, ge=1, validation_alias="LOGIN_RATE_MAX_PER_ADDRESS"
    )
    login_rate_max_per_username: int = Field(
        default=10, ge=1, validation_alias="LOGIN_RATE_MAX_PER_USERNAME"
    )
    # Secure cookies are sent only over HTTPS. Off only for plain-HTTP
    # development on this machine.
    cookie_secure: bool = Field(default=True, validation_alias="COOKIE_SECURE")
    # P6-01. The built operator console (`npm run build` in web-pilot/).
    # Served at /app when present.
    console_app_dir: Path = Field(
        default=Path("web-pilot/dist"), validation_alias="CONSOLE_APP_DIR"
    )
    # P5-00. Terrain tiles from tools/terrain_fetch.py. Unset: /terrain
    # answers 503 and no height above ground is shown.
    terrain_dir: Path | None = Field(default=None, validation_alias="TERRAIN_DIR")
    # Where a browser reaches the console feed. Behind the TLS front of
    # P0-09 this is wss://<domain>/ws/telemetry.
    console_feed_url: str = Field(
        default="ws://127.0.0.1:8000/ws/telemetry",
        validation_alias="CONSOLE_FEED_URL",
    )

    # P10-03. The replay page draws the same base map as the console.
    basemap_dir: Path = Field(
        default=Path("local/basemap"), validation_alias="BASEMAP_DIR"
    )
    # P10-03. Two consecutive samples further apart than this are a hole in
    # the track, drawn as one and never joined by a line. Not a statement
    # about any stream rate (spec §6.4): only the point past which a straight
    # segment would be inventing a path.
    replay_gap_threshold_s: float = Field(
        default=3.0, gt=0, validation_alias="REPLAY_GAP_THRESHOLD_S"
    )
    # How far apart in time a hole and a logged cause may be and still be
    # matched. Covers a loss detected at the next `status` and clocks that
    # differ between a station and the Gateway.
    replay_evidence_slack_s: float = Field(
        default=5.0, ge=0, validation_alias="REPLAY_EVIDENCE_SLACK_S"
    )
    # Armed telemetry silent for longer than this is two flights, not one.
    replay_flight_split_s: float = Field(
        default=120.0, gt=0, validation_alias="REPLAY_FLIGHT_SPLIT_S"
    )
    # A window with more rows than this is refused, not thinned.
    replay_max_samples: int = Field(
        default=100_000, gt=0, validation_alias="REPLAY_MAX_SAMPLES"
    )
    # S-16. The longest window the flight list scans; a longer one is
    # refused, as a replay over `replay_max_samples` is. 90 days.
    replay_max_flight_window_s: float = Field(
        default=90 * 86400.0, gt=0, validation_alias="REPLAY_MAX_FLIGHT_WINDOW_S"
    )


class ConsoleSettings(FeedTicketSettings, NatsSettings):
    """The P1-08 console feed.

    Only the bus, deliberately. The console is a NATS subscriber and must not
    reach the database: a browser refresh becoming a hypertable query is the
    thing this design exists to prevent, and a service that cannot connect to
    the database cannot accidentally start doing so.
    """

    service_name: str = "console"

    console_host: str = Field(default="127.0.0.1", validation_alias="CONSOLE_HOST")
    console_port: int = Field(
        default=8000, ge=1, le=65535, validation_alias="CONSOLE_PORT"
    )
    # P1-12. Where `infra/basemap/fetch_basemap.sh` put the base map. Per
    # machine and never committed; relative paths are from the working
    # directory the console is started in.
    basemap_dir: Path = Field(
        default=Path("local/basemap"), validation_alias="BASEMAP_DIR"
    )
    # S-16. Comma-separated page origins on other hosts that may open the
    # feed, e.g. `https://ops.example.ge`. Pages on the feed's own host are
    # always allowed (`api.telemetry_ws.origin_allowed`); empty adds none.
    console_allowed_origins: str = Field(
        default="", validation_alias="CONSOLE_ALLOWED_ORIGINS"
    )

    @property
    def allowed_origins(self) -> tuple[str, ...]:
        return tuple(
            origin.strip()
            for origin in self.console_allowed_origins.split(",")
            if origin.strip()
        )

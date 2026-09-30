"""Environment-based configuration, validated once at startup.

No service reads `os.environ`. A service declares what it needs as a settings
class, calls `load_settings`, and either gets a fully validated object or fails
to start with a message naming the variable that is wrong. A missing NATS URL
should stop a process on the ground, not surface as an AttributeError while a
vehicle is airborne.

Settings are composed from mixins so that each service declares exactly the
infrastructure it touches::

    class GatewaySettings(
        ServiceSettings, TelemetryDatabaseSettings, RedisSettings, NatsSettings
    ):
        service_name: str = "gateway"

Everything is frozen. Configuration that changes under a running process is a
source of irreproducible behaviour, and reproducibility is the whole argument
for deterministic deconfliction.
"""

from __future__ import annotations

import enum
from typing import Annotated, Any, Self

from pydantic import (
    AnyUrl,
    Field,
    PostgresDsn,
    RedisDsn,
    UrlConstraints,
    ValidationError,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = [
    "ConfigurationError",
    "Environment",
    "LogLevel",
    "NatsSettings",
    "PostgresSettings",
    "RedisSettings",
    "ServiceSettings",
    "Settings",
    "TelemetryDatabaseSettings",
    "load_settings",
]


class Environment(enum.StrEnum):
    DEV = "dev"
    STAGING = "staging"
    PROD = "prod"


class LogLevel(enum.StrEnum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


# NATS is not one of pydantic's known schemes.
NatsDsn = Annotated[AnyUrl, UrlConstraints(allowed_schemes=["nats", "tls"])]

# Credential fragments that ship in infra/.env.example. Harmless in
# development, and a deployment mistake anywhere else.
_DEVELOPMENT_CREDENTIALS = ("courier_dev", "changeme", "password", "secret")


class ConfigurationError(RuntimeError):
    """Configuration is missing or invalid. Raised instead of starting."""


class Settings(BaseSettings):
    """Base for every settings class in the system."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        # A service ignores variables meant for its neighbours rather than
        # refusing to start because of them.
        extra="ignore",
        frozen=True,
    )


class ServiceSettings(Settings):
    """Settings every service has."""

    service_name: str = Field(default="courier")
    env: Environment = Field(default=Environment.DEV, validation_alias="COURIER_ENV")
    log_level: LogLevel = Field(default=LogLevel.INFO, validation_alias="LOG_LEVEL")

    @model_validator(mode="after")
    def _reject_development_credentials_outside_dev(self) -> Self:
        """Refuse to start staging or production on the example credentials.

        This catches the specific accident of a real deployment inheriting
        infra/.env.example, which is the most likely way a fleet ends up
        reachable with a published password.
        """
        if self.env is Environment.DEV:
            return self

        offenders = [
            name
            for name, value in self
            if name.endswith("_url")
            and any(credential in str(value) for credential in _DEVELOPMENT_CREDENTIALS)
        ]
        if offenders:
            raise ValueError(
                f"{', '.join(sorted(offenders))} still carries development "
                f"credentials while COURIER_ENV={self.env.value}"
            )
        return self


class PostgresSettings(Settings):
    """Relational state: registry, airspace zones, audit log."""

    database_url: PostgresDsn = Field(validation_alias="DATABASE_URL")


class TelemetryDatabaseSettings(Settings):
    """The TimescaleDB hypertable behind drone_state."""

    telemetry_database_url: PostgresDsn = Field(
        validation_alias="TELEMETRY_DATABASE_URL"
    )

    # ONE setting, TWO consumers: P1-04's drone_state retention policy and the
    # Gateway's raw archive. They must not be set independently.
    #
    # The archive must not outlive the telemetry it explains, and the telemetry
    # must not outlive the archive either. A drone_state row whose datagrams
    # have been deleted cannot be checked against what the aircraft actually
    # sent; an archive whose drone_state is gone is bytes nobody can locate by
    # drone or by flight. Either way half a record is worse than none, because
    # it reads as complete.
    #
    # 90 days is what an occurrence investigation needs: a report is filed in
    # days but worked for weeks, and the questions are "what did this aircraft
    # do on this flight" and "had this been happening before".
    telemetry_retention_days: int = Field(
        default=90, ge=1, validation_alias="TELEMETRY_RETENTION_DAYS"
    )


class RedisSettings(Settings):
    """Live drone state. Key expiry is the definition of a lost link."""

    redis_url: RedisDsn = Field(validation_alias="REDIS_URL")


class NatsSettings(Settings):
    """Internal pub/sub."""

    nats_url: NatsDsn = Field(validation_alias="NATS_URL")


def _describe(error: ValidationError) -> str:
    """Turn a pydantic error into something readable at 3am."""
    lines: list[str] = []
    for detail in error.errors():
        location = ".".join(str(part) for part in detail["loc"]) or "(model)"
        lines.append(f"  {location}: {detail['msg']}")
    return "\n".join(lines)


def load_settings[SettingsT: Settings](
    settings_class: type[SettingsT], **overrides: Any
) -> SettingsT:
    """Build and validate a settings object, or raise ConfigurationError.

    Overrides are for tests. Production passes none and reads the environment.
    """
    try:
        return settings_class(**overrides)
    except ValidationError as error:
        raise ConfigurationError(
            f"invalid configuration for {settings_class.__name__}:\n{_describe(error)}"
        ) from error

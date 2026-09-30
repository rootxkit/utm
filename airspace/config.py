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

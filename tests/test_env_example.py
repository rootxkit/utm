"""infra/.env.example must actually start every service.

The example environment is what a new developer copies and what a deployment
is templated from. If a settings class grows a required variable and the
example does not, the failure surfaces as a service that will not boot on a
machine nobody has debugged yet. Cheaper to catch here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from airspace.config import AirspaceSettings
from api.config import ApiSettings, ConsoleSettings
from common.config import Settings, load_settings
from gateway.config import GatewaySettings, RemoteIdSettings

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_EXAMPLE = REPO_ROOT / "infra" / ".env.example"

# The agent is deliberately absent. It is the ground relay, configured from
# TOML rather than the environment, because it is edited by a pilot on a laptop
# and not by an operator with a deployment pipeline. Its example configuration
# is agent/relay.example.toml and its own tests cover it.
SERVICE_SETTINGS: tuple[type[Settings], ...] = (
    GatewaySettings,
    ApiSettings,
    ConsoleSettings,
    AirspaceSettings,
    RemoteIdSettings,
)

# Variables the example sets that are consumed by docker-compose rather than by
# a settings class. They are not expected to appear anywhere in Python.
COMPOSE_ONLY = frozenset(
    {
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
        "POSTGRES_DB",
        "POSTGRES_PORT",
        "TIMESCALE_USER",
        "TIMESCALE_PASSWORD",
        "TIMESCALE_DB",
        "TIMESCALE_PORT",
        "REDIS_PORT",
        "REDIS_MAXMEMORY",
        "NATS_PORT",
        "NATS_MONITOR_PORT",
    }
)


@pytest.fixture(autouse=True)
def _isolate_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """The example file must stand on its own, not lean on the developer's shell."""
    for name in _example_variables():
        monkeypatch.delenv(name, raising=False)


def _example_variables() -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        values[name.strip()] = value.strip()
    return values


def test_example_file_exists() -> None:
    assert ENV_EXAMPLE.is_file()


@pytest.mark.parametrize(
    "settings_class", SERVICE_SETTINGS, ids=lambda cls: cls.__name__
)
def test_service_starts_from_the_example_environment(
    settings_class: type[Settings],
) -> None:
    settings = load_settings(settings_class, _env_file=ENV_EXAMPLE)

    assert settings.service_name  # type: ignore[attr-defined]


def test_every_example_variable_is_consumed_somewhere() -> None:
    """An orphaned variable in the example is either dead or a missing field."""
    declared: set[str] = set()
    for settings_class in SERVICE_SETTINGS:
        for name, field in settings_class.model_fields.items():
            alias = field.validation_alias
            declared.add(str(alias) if alias is not None else name.upper())

    orphans = sorted(set(_example_variables()) - declared - COMPOSE_ONLY)
    assert not orphans, (
        f"infra/.env.example sets {orphans}, which no settings class reads "
        f"and docker-compose does not use either"
    )

"""Network Remote ID providers come from the environment, checked. U-02."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from gateway.config import NetworkRidSettings
from gateway.network_rid_ingest import build_poller

PROVIDER: dict[str, Any] = {
    "id": "ussp-a",
    "base_url": "https://ussp-a.example/rid/v2",
    "token_url": "https://auth.ussp-a.example/oauth/token",
    "client_id": "utm-ge",
    "client_secret": "never-logged",
    "areas": [[41.6, 44.7, 41.8, 44.95]],
}


def settings(monkeypatch: pytest.MonkeyPatch, providers: Any) -> NetworkRidSettings:
    monkeypatch.setenv("NETWORK_RID_PROVIDERS", json.dumps(providers))
    monkeypatch.setenv("NATS_URL", "nats://127.0.0.1:4222")
    monkeypatch.setenv(
        "TELEMETRY_DATABASE_URL", "postgresql+asyncpg://u:p@127.0.0.1:5433/t"
    )
    return NetworkRidSettings()  # type: ignore[call-arg]  # from the environment


def test_a_provider_is_read_from_json(monkeypatch: pytest.MonkeyPatch) -> None:
    [provider] = settings(monkeypatch, [PROVIDER]).network_rid_providers
    assert provider.id == "ussp-a"
    assert provider.scope == "rid.display_provider"
    assert provider.client_secret.get_secret_value() == "never-logged"
    assert "never-logged" not in repr(provider)


def test_no_providers_is_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    assert settings(monkeypatch, []).network_rid_providers == []


def test_plain_http_is_refused_off_this_host(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValidationError, match="plain HTTP"):
        settings(monkeypatch, [{**PROVIDER, "base_url": "http://ussp-a.example/rid"}])


def test_plain_http_to_this_host_is_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    local = {
        **PROVIDER,
        "base_url": "http://127.0.0.1:8090",
        "token_url": "http://localhost:8090/token",
    }
    assert settings(monkeypatch, [local]).network_rid_providers[0].id == "ussp-a"


@pytest.mark.parametrize(
    "change",
    [
        {"areas": []},
        {"areas": [[41.8, 44.7, 41.6, 44.95]]},
        {"id": "has a space"},
        {"client_id": ""},
        {"unexpected": 1},
    ],
)
def test_a_bad_provider_is_refused(
    monkeypatch: pytest.MonkeyPatch, change: dict[str, Any]
) -> None:
    with pytest.raises(ValidationError):
        settings(monkeypatch, [{**PROVIDER, **change}])


def test_provider_ids_are_unique(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValidationError, match="unique"):
        settings(monkeypatch, [PROVIDER, PROVIDER])


async def test_a_poller_is_built_from_the_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured = settings(monkeypatch, [PROVIDER])

    class Bus:
        async def publish(self, subject: str, payload: bytes) -> None:
            pass

    async with httpx.AsyncClient() as http:
        poller = build_poller(
            configured.network_rid_providers[0], http, Bus(), configured
        )
    assert poller.provider == "ussp-a"
    assert poller.client.base_url == "https://ussp-a.example/rid/v2"
    assert poller.client.tokens.client_secret == "never-logged"
    assert [area.view() for area in poller.areas] == [
        "41.6000000,44.7000000,41.8000000,44.9500000"
    ]
    assert poller.max_age_s == configured.network_rid_max_age_s

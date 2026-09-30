"""The sign-in limit per address behind a proxy, through real uvicorn. S-15.

uvicorn decides who the client is: it takes `X-Forwarded-For` only from a
peer in `forwarded_allow_ips`. So the test runs the API under uvicorn,
configured exactly as `python -m api` configures it, and connects from
127.0.0.1 - trusted in one run, not in the other.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from typing import Any, cast

import httpx
import pytest
import uvicorn
from pydantic import SecretStr

import api.__main__ as api_main
import api.console as console_main
from api.app import create_api_app
from api.config import ApiSettings, ConsoleSettings, ProxySettings
from api.ratelimit import LoginRateLimiter
from api.registry import FleetRegistry
from api.tests.auth_fakes import api_kwargs
from api.tests.test_login_rate_limit import CountingAccounts
from common import load_settings
from tests.ports import free_tcp_port

PER_ADDRESS = 2


def proxy_settings(monkeypatch: pytest.MonkeyPatch, allow: str) -> ProxySettings:
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", allow)
    return load_settings(ProxySettings)


@contextlib.asynccontextmanager
async def serving(settings: ProxySettings) -> AsyncIterator[str]:
    # Per address only: a per-username budget would also refuse, and hide
    # which of the two did.
    limiter = LoginRateLimiter(max_per_address=PER_ADDRESS, max_per_username=10_000)
    app = create_api_app(
        cast(FleetRegistry, cast(Any, None)),
        login_limiter=limiter,
        **{**api_kwargs(), "auth": CountingAccounts()},
    )
    port = free_tcp_port()
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            log_level="warning",
            **settings.uvicorn_proxy_options(),
        )
    )
    task = asyncio.create_task(server.serve())
    try:
        for _ in range(500):
            if server.started:
                break
            await asyncio.sleep(0.01)
        assert server.started, "the API never started"
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await task


async def attempts(base_url: str, forwarded_for: list[str]) -> list[int]:
    codes: list[int] = []
    async with httpx.AsyncClient(base_url=base_url) as http:
        for address in forwarded_for:
            response = await http.post(
                "/auth/login",
                json={"username": f"user-{len(codes)}", "password": "wrong"},
                headers={"X-Forwarded-For": address},
            )
            codes.append(response.status_code)
    return codes


async def test_behind_a_trusted_proxy_each_client_has_its_own_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with serving(proxy_settings(monkeypatch, "127.0.0.1")) as url:
        codes = await attempts(
            url, ["198.51.100.1"] * (PER_ADDRESS + 1) + ["198.51.100.2"]
        )

    # The first client is refused at its own limit; the second is not
    # charged for the first one's attempts.
    assert codes == [401] * PER_ADDRESS + [429, 401]


async def test_from_an_untrusted_peer_a_forged_address_buys_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The paired direction: the same requests, from a peer not trusted to
    forward, all count against the peer's own address."""
    async with serving(proxy_settings(monkeypatch, "192.0.2.10")) as url:
        codes = await attempts(
            url, [f"198.51.100.{n}" for n in range(1, PER_ADDRESS + 3)]
        )

    assert codes == [401] * PER_ADDRESS + [429, 429]


# --- the entry points pass the setting to uvicorn -------------------------------


def test_the_default_trusts_only_this_machine(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FORWARDED_ALLOW_IPS", raising=False)
    assert load_settings(ProxySettings).uvicorn_proxy_options() == {
        "proxy_headers": True,
        "forwarded_allow_ips": "127.0.0.1",
    }


def test_python_m_api_passes_the_proxy_setting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = ApiSettings.model_construct(forwarded_allow_ips="172.18.0.0/16")
    ran: dict[str, Any] = {}
    monkeypatch.setattr(api_main, "load_settings", lambda _: settings)
    monkeypatch.setattr(api_main, "configure_logging", lambda **_: None)
    monkeypatch.setattr(api_main, "build_app", lambda _: "app")
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: ran.update(kw))

    api_main.main()

    assert ran["proxy_headers"] is True
    assert ran["forwarded_allow_ips"] == "172.18.0.0/16"


def test_the_console_passes_the_proxy_setting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = ConsoleSettings.model_construct(
        forwarded_allow_ips="*",
        nats_url="nats://127.0.0.1:1",
        feed_ticket_secret=SecretStr("s" * 32),
    )
    ran: dict[str, Any] = {}
    monkeypatch.setattr(console_main, "load_settings", lambda _: settings)
    monkeypatch.setattr(console_main, "configure_logging", lambda **_: None)
    monkeypatch.setattr(console_main, "create_app", lambda *a, **k: "app")
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: ran.update(kw))

    console_main.main()

    assert ran["proxy_headers"] is True
    assert ran["forwarded_allow_ips"] == "*"

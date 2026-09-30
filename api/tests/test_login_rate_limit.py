"""Sign-in attempt limits. S-15.

The limiter on its own with an injected clock, then through the login route
with a store that counts how often it is asked: a refused attempt must not
reach the store, because reaching it is what costs a scrypt.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from httpx import ASGITransport, AsyncClient

from api.app import create_api_app
from api.auth import AuthError, Login
from api.ratelimit import LoginRateLimiter, address_key, retry_after_header
from api.registry import FleetRegistry
from api.tests.auth_fakes import ADMIN, FakeAccounts, api_kwargs

GOOD_PASSWORD = "the right password"


class Clock:
    def __init__(self) -> None:
        self.now_s = 1000.0

    def __call__(self) -> float:
        return self.now_s


def limiter(clock: Clock, **overrides: Any) -> LoginRateLimiter:
    settings: dict[str, Any] = {
        "max_per_address": 5,
        "max_per_username": 3,
        "window_s": 60.0,
        "clock_s": clock,
        **overrides,
    }
    return LoginRateLimiter(**settings)


# --- the limiter ------------------------------------------------------------


def test_attempts_up_to_the_username_limit_pass_and_the_next_is_refused() -> None:
    clock = Clock()
    limit = limiter(clock)

    passed = [limit.attempt(address=f"10.0.0.{i}", username="ann") for i in range(3)]
    refused = limit.attempt(address="10.0.0.9", username="ann")

    assert passed == [None, None, None]
    assert refused == pytest.approx(60.0)


def test_the_username_is_counted_as_the_store_normalises_it() -> None:
    limit = limiter(Clock())
    for name in ("ann", " ANN", "Ann "):
        assert limit.attempt(address=None, username=name) is None

    assert limit.attempt(address=None, username="aNn") is not None


def test_one_address_trying_many_names_is_refused() -> None:
    limit = limiter(Clock())
    for i in range(5):
        assert limit.attempt(address="10.0.0.1", username=f"name{i}") is None

    assert limit.attempt(address="10.0.0.1", username="another") is not None
    # The paired absence: another address may still try.
    assert limit.attempt(address="10.0.0.2", username="another") is None


def test_attempts_are_allowed_again_once_the_window_has_passed() -> None:
    clock = Clock()
    limit = limiter(clock)
    for _ in range(3):
        limit.attempt(address=None, username="ann")
    clock.now_s += 30.0
    assert limit.attempt(address=None, username="ann") == pytest.approx(30.0)

    clock.now_s += 30.0

    assert limit.attempt(address=None, username="ann") is None


def test_a_refused_attempt_does_not_extend_the_wait() -> None:
    clock = Clock()
    limit = limiter(clock)
    for _ in range(3):
        limit.attempt(address=None, username="ann")
    for _ in range(10):
        clock.now_s += 5.0
        limit.attempt(address=None, username="ann")

    clock.now_s = 1000.0 + 60.0

    assert limit.attempt(address=None, username="ann") is None


def test_memory_is_bounded_under_a_spray_of_names() -> None:
    limit = limiter(Clock(), max_keys=50, max_per_address=10_000)
    for i in range(500):
        limit.attempt(address="10.0.0.1", username=f"spray{i}")

    assert len(limit._windows) <= 50
    # The address that sprayed is the most recently used key and is kept.
    assert ("address", "10.0.0.1") in limit._windows


def test_two_ipv6_addresses_in_one_64_share_one_budget() -> None:
    limit = limiter(Clock(), max_per_address=2, max_per_username=100)
    assert limit.attempt(address="2001:db8:1:2::1", username="a") is None
    assert limit.attempt(address="2001:db8:1:2:ffff::9", username="b") is None

    assert limit.attempt(address="2001:db8:1:2:abcd::5", username="c") is not None
    # The paired absence: the next /64 has its own budget.
    assert limit.attempt(address="2001:db8:1:3::1", username="d") is None


def test_two_ipv4_addresses_do_not_share_a_budget() -> None:
    limit = limiter(Clock(), max_per_address=2, max_per_username=100)
    for name in ("a", "b"):
        assert limit.attempt(address="192.0.2.1", username=name) is None
    assert limit.attempt(address="192.0.2.1", username="c") is not None

    assert limit.attempt(address="192.0.2.2", username="d") is None


@pytest.mark.parametrize(
    ("address", "key"),
    [
        ("192.0.2.7", "192.0.2.7"),
        ("2001:db8:1:2:3:4:5:6", "2001:db8:1:2::/64"),
        ("2001:DB8:1:2::1", "2001:db8:1:2::/64"),
        ("::ffff:192.0.2.7", "192.0.2.7"),
        ("testclient", "testclient"),
    ],
)
def test_address_keys(address: str, key: str) -> None:
    assert address_key(address) == key


@pytest.mark.parametrize(("wait_s", "header"), [(0.0, "1"), (0.2, "1"), (59.1, "60")])
def test_retry_after_is_whole_seconds_and_never_zero(
    wait_s: float, header: str
) -> None:
    assert retry_after_header(wait_s) == header


# --- through the route ------------------------------------------------------


class CountingAccounts(FakeAccounts):
    """Knows one account, `ann`. Counts every sign-in it is asked for."""

    def __init__(self) -> None:
        self.logins = 0

    async def login(self, username: str, password: str, **_: Any) -> Login:
        self.logins += 1
        if username == "ann" and password == GOOD_PASSWORD:
            return Login(
                token="t",
                operator=ADMIN,
                expires_at=datetime.now(tz=UTC) + timedelta(hours=1),
            )
        raise AuthError("invalid_credentials", "wrong username or password")


def client(accounts: CountingAccounts, limit: LoginRateLimiter) -> AsyncClient:
    kwargs = {**api_kwargs(), "auth": accounts}
    app = create_api_app(
        cast(FleetRegistry, cast(Any, None)), login_limiter=limit, **kwargs
    )
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.parametrize("username", ["ann", "nobody-by-this-name"])
async def test_the_route_answers_429_without_asking_the_store(username: str) -> None:
    """Known and unknown names alike: the answer must not tell them apart."""
    accounts = CountingAccounts()
    async with client(accounts, limiter(Clock())) as http:
        answers = [
            (
                await http.post(
                    "/auth/login", json={"username": username, "password": "wrong"}
                )
            ).status_code
            for _ in range(4)
        ]
        refused = await http.post(
            "/auth/login", json={"username": username, "password": "wrong"}
        )

    assert answers == [401, 401, 401, 429]
    assert accounts.logins == 3
    assert refused.status_code == 429
    assert refused.headers["Retry-After"] == "60"


async def test_under_the_limit_the_right_password_signs_in() -> None:
    """The paired presence: the limiter lets a normal sign-in through."""
    accounts = CountingAccounts()
    async with client(accounts, limiter(Clock())) as http:
        await http.post("/auth/login", json={"username": "ann", "password": "wrong"})
        response = await http.post(
            "/auth/login", json={"username": "ann", "password": GOOD_PASSWORD}
        )

    assert response.status_code == 200
    assert accounts.logins == 2


async def test_the_route_limits_by_default() -> None:
    """Built without a limiter, the API still has one."""
    accounts = CountingAccounts()
    kwargs = {**api_kwargs(), "auth": accounts}
    app = create_api_app(cast(FleetRegistry, cast(Any, None)), **kwargs)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as http:
        codes = [
            (
                await http.post(
                    "/auth/login", json={"username": "ann", "password": "wrong"}
                )
            ).status_code
            for _ in range(LoginRateLimiter().max_per_username + 1)
        ]

    assert codes[-1] == 429
    assert set(codes[:-1]) == {401}

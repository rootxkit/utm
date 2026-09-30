"""Passwords, feed tickets and roles: the parts of P6-08 with no database.

Every refusal here is paired with the same input accepted, so a check that
refuses everything cannot pass (CLAUDE.md, "test presence, not only absence").
"""

from __future__ import annotations

import asyncio
import base64
import json
import threading
import time
from typing import Any, cast
from uuid import uuid4

import pytest

from api import auth as auth_module
from api.auth import (
    AuthError,
    Operator,
    OperatorStore,
    Role,
    ScryptCost,
    check_password_policy,
    hash_password,
    issue_feed_ticket,
    normalise_username,
    verify_feed_ticket,
    verify_password,
)

FAST = ScryptCost(n_log2=10)
SECRET = b"a-test-secret-that-is-long-enough-000"
OPERATOR = Operator(
    id=uuid4(), username="nino", display_name="Nino", role=Role.OPERATOR
)


# --- passwords --------------------------------------------------------------


def test_the_right_password_verifies() -> None:
    stored = hash_password("correct horse battery", FAST)

    assert verify_password("correct horse battery", stored) is True


def test_a_wrong_password_does_not() -> None:
    stored = hash_password("correct horse battery", FAST)

    assert verify_password("correct horse batterY", stored) is False


def test_two_hashes_of_one_password_differ_by_salt() -> None:
    assert hash_password("same password here", FAST) != hash_password(
        "same password here", FAST
    )


def test_a_hash_records_its_own_cost_so_it_verifies_after_the_default_changes() -> None:
    stored = hash_password("correct horse battery", ScryptCost(n_log2=11, r=4))

    assert stored.startswith("scrypt$11$4$1$")
    assert verify_password("correct horse battery", stored) is True


@pytest.mark.parametrize(
    ("cost", "prefix"),
    [(FAST, "scrypt$10$8$1$"), (ScryptCost(n_log2=11, r=4), "scrypt$11$4$1$")],
)
def test_the_unknown_user_hash_is_at_the_stores_cost(
    cost: ScryptCost, prefix: str
) -> None:
    """An unknown name must cost what a known one does: same scrypt cost.
    No database is reached by constructing a store."""
    store = OperatorStore(
        engine=cast(Any, None),
        session_ttl_s=60,
        idle_timeout_s=60,
        max_failed_logins=3,
        lockout_s=60,
        cost=cost,
    )

    assert store._unknown_user_hash.startswith(prefix)


class Overlap:
    """Stands in for a hash: sleeps, and records how many ran at once."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.running = 0
        self.most = 0

    def __call__(self, *_: Any) -> Any:
        with self.lock:
            self.running += 1
            self.most = max(self.most, self.running)
        time.sleep(0.05)
        with self.lock:
            self.running -= 1
        return "scrypt$stub"


def a_store(**overrides: Any) -> OperatorStore:
    settings: dict[str, Any] = {
        "engine": cast(Any, None),
        "session_ttl_s": 60,
        "idle_timeout_s": 60,
        "max_failed_logins": 3,
        "lockout_s": 60,
        "cost": FAST,
        **overrides,
    }
    return OperatorStore(**settings)


@pytest.mark.parametrize("limit", [1, 2, 3])
async def test_no_more_hashes_run_at_once_than_the_limit(
    monkeypatch: pytest.MonkeyPatch, limit: int
) -> None:
    """Hashing and verifying share the limit, and reach it: with eight
    waiting, exactly `limit` run together."""
    store = a_store(max_concurrent_hashes=limit)
    overlap = Overlap()
    monkeypatch.setattr(auth_module, "hash_password", overlap)
    monkeypatch.setattr(auth_module, "verify_password", overlap)

    await asyncio.gather(
        *(store._hash("p") for _ in range(4)),
        *(store._verify("p", "stored") for _ in range(4)),
    )

    assert overlap.most == limit


def test_a_limit_below_one_is_refused() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        a_store(max_concurrent_hashes=0)


@pytest.mark.parametrize(
    "stored", ["", "bcrypt$x", "scrypt$15$8$1$only-five-parts", "scrypt$a$b$c$d$e"]
)
def test_a_malformed_hash_verifies_nothing(stored: str) -> None:
    assert verify_password("anything at all", stored) is False


def test_a_long_enough_password_passes_the_policy() -> None:
    check_password_policy("twelve chars", username="nino")


@pytest.mark.parametrize(
    ("password", "why"),
    [("eleven char", "12"), ("xx-NINO-xx-xx", "username"), ("a" * 300, "long")],
)
def test_the_policy_refuses(password: str, why: str) -> None:
    with pytest.raises(AuthError) as refused:
        check_password_policy(password, username="nino")

    assert refused.value.kind == "weak_password"
    assert why in str(refused.value)


def test_usernames_are_lower_case_and_plain() -> None:
    assert normalise_username("  Nino.K ") == "nino.k"
    for bad in ("ab", "has space", "semi;colon", "x" * 65):
        with pytest.raises(AuthError):
            normalise_username(bad)


# --- feed tickets ---------------------------------------------------------------


def test_a_fresh_ticket_carries_who_and_what_role() -> None:
    ticket = issue_feed_ticket(SECRET, OPERATOR, now_s=1000.0, ttl_s=60.0)

    claims = verify_feed_ticket(SECRET, ticket, now_s=1030.0)

    assert claims is not None
    assert claims.operator_id == OPERATOR.id
    assert claims.role is Role.OPERATOR


def test_an_expired_ticket_is_refused() -> None:
    ticket = issue_feed_ticket(SECRET, OPERATOR, now_s=1000.0, ttl_s=60.0)

    assert verify_feed_ticket(SECRET, ticket, now_s=1060.0) is None


def test_a_ticket_signed_with_another_secret_is_refused() -> None:
    ticket = issue_feed_ticket(b"x" * 40, OPERATOR, now_s=1000.0, ttl_s=60.0)

    assert verify_feed_ticket(SECRET, ticket, now_s=1001.0) is None


def test_a_ticket_whose_claims_were_edited_is_refused() -> None:
    """Promoting yourself to admin, or extending your own expiry."""
    ticket = issue_feed_ticket(SECRET, OPERATOR, now_s=1000.0, ttl_s=60.0)
    payload, signature = ticket.split(".")
    claims = json.loads(base64.urlsafe_b64decode(payload + "=="))
    claims["role"] = "admin"
    claims["exp"] = 10**12
    forged = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=")

    assert (
        verify_feed_ticket(SECRET, f"{forged.decode()}.{signature}", now_s=1001.0)
        is None
    )


@pytest.mark.parametrize("ticket", ["", "no-dot", "a.b.c", "%%%.%%%"])
def test_garbage_is_not_a_ticket(ticket: str) -> None:
    assert verify_feed_ticket(SECRET, ticket, now_s=0.0) is None


# --- roles ------------------------------------------------------------------------


def test_roles_are_ordered() -> None:
    assert Role.ADMIN.allows(Role.OPERATOR)
    assert Role.OPERATOR.allows(Role.VIEWER)
    assert Role.VIEWER.allows(Role.VIEWER)
    assert not Role.VIEWER.allows(Role.OPERATOR)
    assert not Role.OPERATOR.allows(Role.ADMIN)

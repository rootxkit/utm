"""Operator accounts, passwords, sessions and feed tickets. P6-08.

## What is protected, and how

- **The API** accepts a request only from a signed-in operator: a session
  token in the `courier_session` cookie (the browser) or an
  `Authorization: Bearer` header (a script). Sessions live in the relational
  database (migration 0003_operators), so one can be revoked and stops
  working at once.
- **The console feed** (`api/telemetry_ws.py`) must never read a database,
  so it cannot look a session up. The API gives a signed-in browser a
  short-lived **feed ticket** instead: an HMAC-signed statement "this
  operator, this role, until this time", checked by the console with a
  secret the two services share. A revoked session's feed ends when its
  ticket does, `feed_ticket_ttl_s` at most.

## Roles

Ordered: `viewer` < `operator` < `admin`. A role may do everything the ones
below it may.

- `viewer`: see everything - map, alerts, replay, registry, audit log.
- `operator`: also act on alerts (acknowledge).
- `admin`: also change the registry and manage accounts.

## Passwords

scrypt from the standard library, so no dependency and no C extension on a
Windows laptop. Each hash records its own cost, so the cost can be raised
later and old hashes still verify. A login for an unknown username still
computes a hash, so the response time does not reveal which usernames exist.

Repeated failures lock the account for `lockout_s`; every attempt, failed or
not, is an `events` row.

scrypt at the default cost takes 32 MiB and tens of milliseconds, so it never
runs on the event loop (`asyncio.to_thread`), at most
`max_concurrent_hashes` at a time, and never while a row lock is
held; `OperatorStore.login` explains why the lockout stays race-free anyway.
The HTTP layer also limits sign-in attempts per client address and per
username (`api/ratelimit.py`), which bounds how much hashing anyone can ask
for.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from api.actors import SYSTEM, Actor

SESSION_COOKIE = "courier_session"
FEED_COOKIE = "courier_feed"
# Required on every state-changing request authenticated by cookie. A form
# on another site cannot set a custom header without a CORS preflight this
# API never grants, so the header proves the request came from our own page.
CSRF_HEADER = "X-Courier-Request"

MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 256
# Attempted usernames are logged for failed logins; a password typed into
# the username field would otherwise be kept in full.
_LOGGED_USERNAME_CHARS = 64


class Role(StrEnum):
    VIEWER = "viewer"
    OPERATOR = "operator"
    ADMIN = "admin"

    @property
    def rank(self) -> int:
        return _RANKS[self]

    def allows(self, needed: Role) -> bool:
        return self.rank >= needed.rank


_RANKS = {Role.VIEWER: 0, Role.OPERATOR: 1, Role.ADMIN: 2}


@dataclass(frozen=True, slots=True)
class Operator:
    id: UUID
    username: str
    display_name: str
    role: Role

    @property
    def actor(self) -> Actor:
        return Actor("operator", str(self.id))

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "username": self.username,
            "display_name": self.display_name,
            "role": self.role.value,
        }


class AuthError(Exception):
    """A refused authentication or account change. `kind` is for HTTP."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


# --- passwords --------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ScryptCost:
    n_log2: int = 15
    r: int = 8
    p: int = 1

    @property
    def maxmem(self) -> int:
        # scrypt needs 128 * r * n bytes; twice that leaves room.
        return 256 * self.r * (1 << self.n_log2)


DEFAULT_COST = ScryptCost()
_DIGEST_BYTES = 32


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def hash_password(password: str, cost: ScryptCost = DEFAULT_COST) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=1 << cost.n_log2,
        r=cost.r,
        p=cost.p,
        maxmem=cost.maxmem,
        dklen=_DIGEST_BYTES,
    )
    return f"scrypt${cost.n_log2}${cost.r}${cost.p}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algorithm, n_log2, r, p, salt, digest = stored.split("$")
        if algorithm != "scrypt":
            return False
        cost = ScryptCost(int(n_log2), int(r), int(p))
        computed = hashlib.scrypt(
            password.encode("utf-8"),
            salt=_unb64(salt),
            n=1 << cost.n_log2,
            r=cost.r,
            p=cost.p,
            maxmem=cost.maxmem,
            dklen=_DIGEST_BYTES,
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(computed, _unb64(digest))


def check_password_policy(password: str, *, username: str) -> None:
    """Length, and not the username. Length is what resists guessing;
    composition rules mostly produce predictable passwords."""
    if len(password) < MIN_PASSWORD_LENGTH:
        raise AuthError(
            "weak_password",
            f"a password needs at least {MIN_PASSWORD_LENGTH} characters",
        )
    if len(password) > MAX_PASSWORD_LENGTH:
        raise AuthError("weak_password", "that password is too long")
    if username.lower() in password.lower():
        raise AuthError("weak_password", "a password must not contain the username")


def normalise_username(username: str) -> str:
    name = username.strip().lower()
    if not 3 <= len(name) <= 64 or not all(c.isalnum() or c in "._-" for c in name):
        raise AuthError(
            "invalid_username",
            "a username is 3-64 letters, digits, dots, dashes or underscores",
        )
    return name


# --- feed tickets -----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FeedTicket:
    operator_id: UUID
    role: Role
    expires_at_s: float


def issue_feed_ticket(
    secret: bytes, operator: Operator, *, now_s: float, ttl_s: float
) -> str:
    payload = json.dumps(
        {"sub": str(operator.id), "role": operator.role.value, "exp": now_s + ttl_s},
        separators=(",", ":"),
    ).encode("utf-8")
    signature = hmac.new(secret, payload, hashlib.sha256).digest()
    return f"{_b64(payload)}.{_b64(signature)}"


def verify_feed_ticket(
    secret: bytes, ticket: str, *, now_s: float
) -> FeedTicket | None:
    """The ticket's claims if its signature holds and it has not expired."""
    try:
        encoded_payload, encoded_signature = ticket.split(".")
        payload = _unb64(encoded_payload)
        signature = _unb64(encoded_signature)
    except (ValueError, TypeError):
        return None
    expected = hmac.new(secret, payload, hashlib.sha256).digest()
    if not hmac.compare_digest(signature, expected):
        return None
    try:
        claims = json.loads(payload)
        result = FeedTicket(
            operator_id=UUID(claims["sub"]),
            role=Role(claims["role"]),
            expires_at_s=float(claims["exp"]),
        )
    except (ValueError, KeyError, TypeError):
        return None
    if result.expires_at_s <= now_s:
        return None
    return result


# --- accounts and sessions --------------------------------------------------


def _token_digest(token: str) -> bytes:
    return hashlib.sha256(token.encode("utf-8")).digest()


_OPERATOR_COLUMNS = (
    "id, username, display_name, role, created_at, disabled_at, "
    "password_changed_at, locked_until"
)


@dataclass(frozen=True, slots=True)
class Login:
    token: str
    operator: Operator
    expires_at: datetime


@dataclass
class OperatorStore:
    engine: AsyncEngine
    session_ttl_s: float
    idle_timeout_s: float
    max_failed_logins: int
    lockout_s: float
    cost: ScryptCost = DEFAULT_COST
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(tz=UTC))
    # How often last_seen_at is written. Every request would be a write.
    touch_interval_s: float = 60.0
    # How many scrypt computations may run at once. Each takes `cost.maxmem`
    # / 2 of memory (32 MiB by default) and a core; unbounded, a burst of
    # sign-ins would use as many as the thread pool has workers. Beyond this
    # they queue, which makes a burst slower rather than the process larger.
    max_concurrent_hashes: int = 2
    # A dummy hash to verify against when the username is unknown, so an
    # unknown name takes as long to refuse as a wrong password. At this
    # store's cost: a cheaper dummy would make unknown names answer faster.
    _unknown_user_hash: str = field(init=False, repr=False)
    _hash_slots: asyncio.Semaphore = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if self.max_concurrent_hashes < 1:
            raise ValueError("max_concurrent_hashes must be at least 1")
        self._hash_slots = asyncio.Semaphore(self.max_concurrent_hashes)
        # Once, at construction, so no request pays for it.
        self._unknown_user_hash = hash_password(secrets.token_urlsafe(24), self.cost)

    async def _hash(self, password: str) -> str:
        async with self._hash_slots:
            return await asyncio.to_thread(hash_password, password, self.cost)

    async def _verify(self, password: str, stored: str) -> bool:
        async with self._hash_slots:
            return await asyncio.to_thread(verify_password, password, stored)

    # --- accounts ---------------------------------------------------------

    async def create_operator(
        self,
        *,
        username: str,
        display_name: str,
        role: Role,
        password: str,
        actor: Actor = SYSTEM,
    ) -> dict[str, Any]:
        name = normalise_username(username)
        check_password_policy(password, username=name)
        password_hash = await self._hash(password)
        try:
            async with self.engine.begin() as connection:
                row = (
                    await connection.execute(
                        sa.text(
                            "INSERT INTO operators "
                            "(username, display_name, role, password_hash) "
                            "VALUES (:username, :display_name, :role, :hash) "
                            f"RETURNING {_OPERATOR_COLUMNS}"
                        ),
                        {
                            "username": name,
                            "display_name": display_name.strip() or name,
                            "role": role.value,
                            "hash": password_hash,
                        },
                    )
                ).one()
                created = _operator_row(row)
                await _audit(
                    connection,
                    actor,
                    created["id"],
                    "operator_created",
                    {"username": name, "role": role.value},
                )
        except IntegrityError as error:
            raise AuthError("conflict", f"username {name!r} is taken") from error
        return created

    async def list_operators(self) -> list[dict[str, Any]]:
        async with self.engine.connect() as connection:
            rows = await connection.execute(
                sa.text(f"SELECT {_OPERATOR_COLUMNS} FROM operators ORDER BY username")
            )
            return [_operator_row(row) for row in rows]

    async def set_role(
        self, operator_id: UUID, role: Role, *, actor: Actor
    ) -> dict[str, Any]:
        return await self._update(
            operator_id,
            "role = :role",
            {"role": role.value},
            "operator_role_changed",
            {"role": role.value},
            actor=actor,
            revoke_sessions=True,
        )

    async def disable(self, operator_id: UUID, *, actor: Actor) -> dict[str, Any]:
        return await self._update(
            operator_id,
            "disabled_at = COALESCE(disabled_at, :now)",
            {"now": self.clock()},
            "operator_disabled",
            {},
            actor=actor,
            revoke_sessions=True,
        )

    async def enable(self, operator_id: UUID, *, actor: Actor) -> dict[str, Any]:
        return await self._update(
            operator_id,
            "disabled_at = NULL, failed_logins = 0, locked_until = NULL",
            {},
            "operator_enabled",
            {},
            actor=actor,
            revoke_sessions=False,
        )

    async def set_password(
        self, operator_id: UUID, password: str, *, actor: Actor
    ) -> dict[str, Any]:
        async with self.engine.connect() as connection:
            username = (
                await connection.execute(
                    sa.text("SELECT username FROM operators WHERE id = :id"),
                    {"id": operator_id},
                )
            ).scalar_one_or_none()
        if username is None:
            raise AuthError("not_found", f"no operator {operator_id}")
        check_password_policy(password, username=str(username))
        password_hash = await self._hash(password)
        return await self._update(
            operator_id,
            "password_hash = :hash, password_changed_at = :now, "
            "failed_logins = 0, locked_until = NULL",
            {"hash": password_hash, "now": self.clock()},
            "operator_password_set",
            {},
            actor=actor,
            revoke_sessions=True,
        )

    async def _update(
        self,
        operator_id: UUID,
        assignments: str,
        params: dict[str, Any],
        event_type: str,
        payload: dict[str, Any],
        *,
        actor: Actor,
        revoke_sessions: bool,
    ) -> dict[str, Any]:
        async with self.engine.begin() as connection:
            row = (
                await connection.execute(
                    sa.text(
                        f"UPDATE operators SET {assignments} WHERE id = :id "
                        f"RETURNING {_OPERATOR_COLUMNS}"
                    ),
                    {"id": operator_id, **params},
                )
            ).one_or_none()
            if row is None:
                raise AuthError("not_found", f"no operator {operator_id}")
            revoked = 0
            if revoke_sessions:
                revoked = await self._revoke_all(connection, operator_id)
            await _audit(
                connection,
                actor,
                operator_id,
                event_type,
                {**payload, "sessions_revoked": revoked},
            )
            return _operator_row(row)

    async def _revoke_all(self, connection: AsyncConnection, operator_id: UUID) -> int:
        result = await connection.execute(
            sa.text(
                "UPDATE operator_sessions SET revoked_at = :now "
                "WHERE operator_id = :id AND revoked_at IS NULL"
            ),
            {"id": operator_id, "now": self.clock()},
        )
        return int(result.rowcount or 0)

    # --- sessions -----------------------------------------------------------

    async def login(
        self,
        username: str,
        password: str,
        *,
        remote_addr: str | None = None,
        user_agent: str | None = None,
    ) -> Login:
        """A new session, or `AuthError("invalid_credentials")`.

        The error does not say whether the name exists, the password was
        wrong or the account is locked or disabled. The audit log does.
        """
        now = self.clock()
        name = username.strip().lower()
        refused = AuthError("invalid_credentials", "wrong username or password")
        # The hash is checked first: off the event loop, with no transaction
        # open, against the stored hash as read here. Holding `FOR UPDATE`
        # across a 32 MiB scrypt would queue every attempt on the account
        # behind it and pin a pooled connection for the whole computation.
        #
        # The lockout stays race-free because every decision that counts is
        # taken again under the row lock, from the locked row:
        # - disabled and locked are read from the locked row, so an attempt
        #   hashed while another attempt was locking the account is refused,
        #   right password or not;
        # - the failure count is incremented from the locked row's value, so
        #   concurrent failures serialise on the lock and none is lost;
        # - the hash that was verified must still be the stored one. If the
        #   password changed in between, the verdict is about a password that
        #   no longer exists: the attempt is refused and not counted.
        # Concurrent attempts may all be hashed at once, but the lock decides
        # the order they are judged in, and once `max_failed_logins` of them
        # have failed every later one meets the lock.
        async with self.engine.connect() as connection:
            stored_hash = (
                await connection.execute(
                    sa.text("SELECT password_hash FROM operators WHERE username = :n"),
                    {"n": name},
                )
            ).scalar_one_or_none()
        password_ok = await self._verify(
            password,
            self._unknown_user_hash if stored_hash is None else str(stored_hash),
        )

        login: Login | None = None
        # One transaction either way. A refusal is not an exception inside it:
        # the failed attempt and its count must be committed, or the lockout
        # never triggers.
        async with self.engine.begin() as connection:
            row = (
                await connection.execute(
                    sa.text(
                        "SELECT id, username, display_name, role, password_hash, "
                        "failed_logins, locked_until, disabled_at "
                        "FROM operators WHERE username = :name FOR UPDATE"
                    ),
                    {"name": name},
                )
            ).one_or_none()
            if row is None:
                await _audit(
                    connection,
                    SYSTEM,
                    None,
                    "login_failed",
                    {
                        "reason": "unknown_username",
                        "username": name[:_LOGGED_USERNAME_CHARS],
                        "remote_addr": remote_addr,
                    },
                )
            else:
                login = await self._check_and_open(
                    connection,
                    row,
                    password_ok=password_ok,
                    hash_changed=row.password_hash != stored_hash,
                    now=now,
                    remote_addr=remote_addr,
                    user_agent=user_agent,
                )
        if login is None:
            raise refused
        return login

    async def _check_and_open(
        self,
        connection: AsyncConnection,
        row: Any,
        *,
        password_ok: bool,
        hash_changed: bool,
        now: datetime,
        remote_addr: str | None,
        user_agent: str | None,
    ) -> Login | None:
        """Judge an attempt from the locked row. The password was checked
        before the lock was taken; `login` explains why that is safe."""
        operator_id = row.id
        reason: str | None = None
        if row.disabled_at is not None:
            reason = "disabled"
        elif row.locked_until is not None and row.locked_until > now:
            reason = "locked"
        elif hash_changed:
            # Created or given a new password while this attempt was being
            # hashed. Not the caller's failure, so not counted.
            reason = "password_changed_during_login"
        elif not password_ok:
            reason = "wrong_password"

        if reason is not None:
            if reason == "wrong_password":
                failures = row.failed_logins + 1
                locked_until = None
                if failures >= self.max_failed_logins:
                    locked_until = now + timedelta(seconds=self.lockout_s)
                    failures = 0
                    reason = "wrong_password_now_locked"
                await connection.execute(
                    sa.text(
                        "UPDATE operators SET failed_logins = :failures, "
                        "locked_until = :locked_until WHERE id = :id"
                    ),
                    {
                        "id": operator_id,
                        "failures": failures,
                        "locked_until": locked_until,
                    },
                )
            await _audit(
                connection,
                SYSTEM,
                operator_id,
                "login_failed",
                {"reason": reason, "remote_addr": remote_addr},
            )
            return None

        token = secrets.token_urlsafe(32)
        expires_at = now + timedelta(seconds=self.session_ttl_s)
        await connection.execute(
            sa.text(
                "UPDATE operators SET failed_logins = 0, locked_until = NULL "
                "WHERE id = :id"
            ),
            {"id": operator_id},
        )
        await connection.execute(
            sa.text(
                "INSERT INTO operator_sessions (operator_id, token_sha256, "
                " created_at, expires_at, last_seen_at, remote_addr, user_agent) "
                "VALUES (:id, :digest, :now, :expires, :now, :addr, :agent)"
            ),
            {
                "id": operator_id,
                "digest": _token_digest(token),
                "now": now,
                "expires": expires_at,
                "addr": remote_addr,
                "agent": (user_agent or "")[:256] or None,
            },
        )
        operator = Operator(
            id=operator_id,
            username=row.username,
            display_name=row.display_name,
            role=Role(row.role),
        )
        await _audit(
            connection,
            operator.actor,
            operator_id,
            "login",
            {"remote_addr": remote_addr},
        )
        return Login(token=token, operator=operator, expires_at=expires_at)

    async def session(self, token: str) -> Operator | None:
        """The operator behind a live session token, or None."""
        now = self.clock()
        async with self.engine.begin() as connection:
            row = (
                await connection.execute(
                    sa.text(
                        "SELECT s.id AS session_id, s.last_seen_at, o.id, o.username, "
                        "o.display_name, o.role "
                        "FROM operator_sessions s JOIN operators o "
                        "  ON o.id = s.operator_id "
                        "WHERE s.token_sha256 = :digest AND s.revoked_at IS NULL "
                        "  AND s.expires_at > :now AND s.last_seen_at > :idle_after "
                        "  AND o.disabled_at IS NULL"
                    ),
                    {
                        "digest": _token_digest(token),
                        "now": now,
                        "idle_after": now - timedelta(seconds=self.idle_timeout_s),
                    },
                )
            ).one_or_none()
            if row is None:
                return None
            if (now - row.last_seen_at).total_seconds() >= self.touch_interval_s:
                await connection.execute(
                    sa.text(
                        "UPDATE operator_sessions SET last_seen_at = :now "
                        "WHERE id = :id"
                    ),
                    {"id": row.session_id, "now": now},
                )
        return Operator(
            id=row.id,
            username=row.username,
            display_name=row.display_name,
            role=Role(row.role),
        )

    async def logout(self, token: str) -> bool:
        async with self.engine.begin() as connection:
            row = (
                await connection.execute(
                    sa.text(
                        "UPDATE operator_sessions SET revoked_at = :now "
                        "WHERE token_sha256 = :digest AND revoked_at IS NULL "
                        "RETURNING operator_id"
                    ),
                    {"digest": _token_digest(token), "now": self.clock()},
                )
            ).one_or_none()
            if row is None:
                return False
            await _audit(
                connection,
                Actor("operator", str(row.operator_id)),
                row.operator_id,
                "logout",
                {},
            )
            return True


async def _audit(
    connection: AsyncConnection,
    actor: Actor,
    operator_id: UUID | None,
    event_type: str,
    payload: dict[str, Any],
) -> None:
    await connection.execute(
        sa.text(
            "INSERT INTO events "
            "(actor_type, actor_id, entity_type, entity_id, event_type, payload) "
            "VALUES (:actor_type, :actor_id, 'operator', :entity_id, :event_type, "
            "        CAST(:payload AS jsonb))"
        ),
        {
            "actor_type": actor.actor_type,
            "actor_id": actor.actor_id,
            "entity_id": str(operator_id) if operator_id is not None else "-",
            "event_type": event_type,
            "payload": json.dumps(payload, default=str),
        },
    )


def _operator_row(row: Any) -> dict[str, Any]:
    return {
        "id": row.id,
        "username": row.username,
        "display_name": row.display_name,
        "role": row.role,
        "created_at": row.created_at,
        "disabled_at": row.disabled_at,
        "password_changed_at": row.password_changed_at,
        "locked_until": row.locked_until,
    }


def wall_clock_s() -> float:
    """Wall-clock seconds, for ticket expiry: both services must agree on it,
    which a monotonic clock does not promise across processes."""
    return time.time()

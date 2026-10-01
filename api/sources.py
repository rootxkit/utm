"""Source switches: the writer side of U-15 (`common/sources.py`).

The API is the only writer. A switch is one transaction in the relational
database: the `source_controls` row and an `events` row (entity_type
`source`, entity_id `<type>/<instance>` or `<type>/*`) with the operator and
the reason. The new state is written to the NATS bucket (the followers'
read path) inside that transaction, before the commit:

- if the bucket cannot be written - NATS not connected, started without
  JetStream, or its store full - the transaction is rolled back and the
  caller is told `control_channel_unavailable` (503). The database and the
  adapters still agree, on the state before: a switch that cannot take
  effect is not recorded as made.
- if the commit fails after the bucket was written, the bucket is put back
  from the database at once, and again by the periodic republish.

After the commit the state is announced on the subject, so followers apply
it within a round trip; an announcement that fails is logged, and the
followers find the state in the bucket at their next read.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from api.actors import Actor
from common import get_logger
from common.bus import RECONNECT_FOREVER
from common.sources import (
    DEFAULT_BUCKET,
    DEFAULT_SUBJECT,
    INSTANCE_ID_PATTERN,
    SOURCE_TYPES,
    STATE_KEY,
    WHOLE_TYPE,
    Control,
    SourceControlState,
    bucket_reader,
)

_log = get_logger(__name__)

ENTITY_TYPE = "source"
MAX_REASON = 500


class SourceError(Exception):
    """A refused switch. `code` is stable for clients to branch on."""

    code = "source_refused"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code


class InvalidSourceError(SourceError):
    code = "invalid_source"


class ChannelUnavailableError(SourceError):
    """No connection to the bus: nothing is written, so nothing is half done."""

    code = "control_channel_unavailable"


def entity_id(source_type: str, instance_id: str | None) -> str:
    return f"{source_type}/{WHOLE_TYPE if instance_id is None else instance_id}"


def check_source(source_type: str, instance_id: str | None) -> None:
    if source_type not in SOURCE_TYPES:
        raise InvalidSourceError(
            f"unknown source type {source_type!r}; known: {', '.join(SOURCE_TYPES)}",
            code="unknown_source_type",
        )
    if instance_id is not None and not INSTANCE_ID_PATTERN.match(instance_id):
        raise InvalidSourceError(
            f"{instance_id!r} is not an instance id: letters, digits and . _ : -, "
            "starting with a letter or digit, at most 128",
            code="invalid_instance_id",
        )


def check_reason(reason: str) -> str:
    stripped = reason.strip()
    if not stripped:
        raise InvalidSourceError("a reason is required", code="reason_required")
    if len(stripped) > MAX_REASON:
        raise InvalidSourceError(
            f"the reason is longer than {MAX_REASON} characters", code="reason_too_long"
        )
    return stripped


def _row(row: Any) -> dict[str, Any]:
    return {
        "source_type": row.source_type,
        "instance_id": None if row.instance_id == WHOLE_TYPE else row.instance_id,
        "enabled": row.enabled,
        "reason": row.reason,
        "changed_by": row.changed_by,
        "changed_at": row.changed_at,
    }


def state_of(
    rows: list[dict[str, Any]], *, version: int, default_deny: bool
) -> SourceControlState:
    return SourceControlState(
        version=version,
        default_deny=default_deny,
        controls=tuple(
            Control(
                source_type=row["source_type"],
                instance_id=row["instance_id"],
                enabled=row["enabled"],
                reason=row["reason"],
                changed_by=row["changed_by"],
                changed_at=row["changed_at"].astimezone(UTC).isoformat(),
            )
            for row in rows
        ),
    )


_SELECT = (
    "SELECT source_type, instance_id, enabled, reason, changed_by, changed_at "
    "FROM source_controls"
)
# The version of a published state: a database sequence, so it only ever
# goes up, whatever any clock does (migration 0006_source_controls).
_NEXT_VERSION = sa.text("SELECT nextval('source_control_version_seq')")


async def _list(connection: AsyncConnection) -> list[dict[str, Any]]:
    rows = await connection.execute(
        sa.text(f"{_SELECT} ORDER BY source_type, instance_id")
    )
    return [_row(row) for row in rows]


@dataclass
class SourceControlStore:
    """The `source_controls` table and its audit rows."""

    engine: AsyncEngine
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(tz=UTC))

    async def list(
        self, connection: AsyncConnection | None = None
    ) -> list[dict[str, Any]]:
        """Every switch; on `connection` when given, so a transaction sees
        its own change."""
        if connection is not None:
            return await _list(connection)
        async with self.engine.connect() as fresh:
            return await _list(fresh)

    async def next_version(self, connection: AsyncConnection | None = None) -> int:
        if connection is not None:
            return int((await connection.execute(_NEXT_VERSION)).scalar_one())
        async with self.engine.begin() as fresh:
            return int((await fresh.execute(_NEXT_VERSION)).scalar_one())

    async def set(
        self,
        source_type: str,
        instance_id: str | None,
        *,
        enabled: bool,
        reason: str,
        actor: Actor,
        actor_name: str,
        before_commit: Callable[[AsyncConnection], Awaitable[None]] | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Switch one source. Returns the row and whether anything changed.

        Setting a source to the state it is already in changes nothing and
        writes no event: an audit log of no-ops buries the switches.
        `before_commit` runs on the transaction once the row and the event
        are written; if it raises, nothing is committed.
        """
        check_source(source_type, instance_id)
        reason = check_reason(reason)
        key = WHOLE_TYPE if instance_id is None else instance_id
        async with self.engine.begin() as connection:
            current = (
                await connection.execute(
                    sa.text(
                        f"{_SELECT} WHERE source_type = :t AND instance_id = :i "
                        "FOR UPDATE"
                    ),
                    {"t": source_type, "i": key},
                )
            ).one_or_none()
            if current is not None and current.enabled == enabled:
                return _row(current), False
            now = self.clock()
            row = (
                await connection.execute(
                    sa.text(
                        "INSERT INTO source_controls "
                        "(source_type, instance_id, enabled, reason, changed_by, "
                        " changed_at) "
                        "VALUES (:t, :i, :enabled, :reason, :by, :at) "
                        "ON CONFLICT (source_type, instance_id) DO UPDATE SET "
                        " enabled = EXCLUDED.enabled, reason = EXCLUDED.reason, "
                        " changed_by = EXCLUDED.changed_by, "
                        " changed_at = EXCLUDED.changed_at "
                        "RETURNING source_type, instance_id, enabled, reason, "
                        " changed_by, changed_at"
                    ),
                    {
                        "t": source_type,
                        "i": key,
                        "enabled": enabled,
                        "reason": reason,
                        "by": actor_name,
                        "at": now,
                    },
                )
            ).one()
            await connection.execute(
                sa.text(
                    "INSERT INTO events (ts, actor_type, actor_id, entity_type, "
                    " entity_id, event_type, payload) "
                    "VALUES (:ts, :actor_type, :actor_id, :entity_type, "
                    " :entity_id, :event_type, CAST(:payload AS jsonb))"
                ),
                {
                    "ts": now,
                    "actor_type": actor.actor_type,
                    "actor_id": actor.actor_id,
                    "entity_type": ENTITY_TYPE,
                    "entity_id": entity_id(source_type, instance_id),
                    "event_type": "source_enabled" if enabled else "source_disabled",
                    "payload": json.dumps(
                        {
                            "source_type": source_type,
                            "instance_id": instance_id,
                            "enabled": enabled,
                            "previous_enabled": (
                                None if current is None else current.enabled
                            ),
                            "reason": reason,
                            "changed_by": actor_name,
                        }
                    ),
                },
            )
            if before_commit is not None:
                await before_commit(connection)
        return _row(row), True


class ControlChannel(Protocol):
    @property
    def closed(self) -> bool:
        """The connection under it is gone for good (the client gave up)."""
        ...

    async def load(self) -> SourceControlState | None:
        """What the bucket holds; None when nothing. Raises if unreadable."""
        ...

    async def store(self, state: SourceControlState) -> None:
        """Write the bucket: the followers' read path. Raises if it cannot."""
        ...

    async def announce(self, state: SourceControlState) -> None:
        """Push on the subject, so followers apply it without waiting."""
        ...


ChannelFactory = Callable[[], Awaitable[ControlChannel]]


@dataclass
class NatsControlChannel:
    """The bucket and the subject (`common/sources.py`)."""

    client: Any
    bucket: str = DEFAULT_BUCKET
    subject: str = DEFAULT_SUBJECT

    @property
    def closed(self) -> bool:
        return bool(self.client.is_closed)

    async def ensure_bucket(self) -> None:
        """Create the bucket if it is not there. One key, a short history,
        file storage: it outlives a broker restart."""
        from nats.js.api import KeyValueConfig, StorageType
        from nats.js.errors import BucketNotFoundError, NotFoundError

        js = self.client.jetstream()
        try:
            await js.key_value(self.bucket)
        except (BucketNotFoundError, NotFoundError):
            await js.create_key_value(
                config=KeyValueConfig(
                    bucket=self.bucket,
                    description="U-15 source switches; written by the API only",
                    history=5,
                    storage=StorageType.FILE,
                )
            )

    async def load(self) -> SourceControlState | None:
        payload = await bucket_reader(self.client, self.bucket)()
        return None if payload is None else SourceControlState.from_json(payload)

    async def store(self, state: SourceControlState) -> None:
        """Raises when JetStream cannot take it (not enabled, store full,
        broker away). A missing bucket is created first: one lost with the
        broker's store, or never made because JetStream was down when the
        API started."""
        from nats.js.errors import BucketNotFoundError, NotFoundError

        try:
            kv = await self.client.jetstream().key_value(self.bucket)
        except (BucketNotFoundError, NotFoundError):
            await self.ensure_bucket()
            kv = await self.client.jetstream().key_value(self.bucket)
        await kv.put(STATE_KEY, state.to_json())

    async def announce(self, state: SourceControlState) -> None:
        await self.client.publish(self.subject, state.to_json())
        await self.client.flush()


def same_content(a: SourceControlState | None, b: SourceControlState) -> bool:
    """Whether two states switch the same things, whatever their versions."""
    return (
        a is not None
        and a.default_deny == b.default_deny
        and sorted(json.dumps(c.as_dict(), sort_keys=True) for c in a.controls)
        == sorted(json.dumps(c.as_dict(), sort_keys=True) for c in b.controls)
    )


@dataclass
class SourceControlService:
    store: SourceControlStore
    channel: ControlChannel | None
    default_deny: bool = False
    # Makes a new channel when there is none, or the old one's connection
    # has given up (`api/__main__.py`). None: the channel given is all.
    connect: ChannelFactory | None = None
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)
    published_version: int | None = field(default=None, init=False)
    publish_failures: int = field(default=0, init=False)
    connect_failures: int = field(default=0, init=False)

    async def listing(self) -> dict[str, Any]:
        return {
            "default_deny": self.default_deny,
            "source_types": list(SOURCE_TYPES),
            "controls": await self.store.list(),
        }

    def _usable(self) -> ControlChannel | None:
        channel = self.channel
        return None if channel is None or channel.closed else channel

    async def ensure_channel(self) -> ControlChannel | None:
        """The channel, reconnecting first if it is missing or closed. A
        failed attempt is logged at error level and counted."""
        channel = self._usable()
        if channel is not None or self.connect is None:
            return channel
        try:
            self.channel = await self.connect()
        except Exception as error:
            self.connect_failures += 1
            _log.error(
                "could not reach NATS for the source switches; retrying",
                extra={"error": repr(error), "connect_failures": self.connect_failures},
            )
            return None
        _log.info("control channel (NATS) connected")
        return self._usable()

    async def switch(
        self,
        source_type: str,
        instance_id: str | None,
        *,
        enabled: bool,
        reason: str,
        actor: Actor,
        actor_name: str,
    ) -> dict[str, Any]:
        check_source(source_type, instance_id)
        check_reason(reason)
        written: list[SourceControlState] = []
        # Serialised with the republish, so the bucket never goes back to a
        # state older than one a switch has just written.
        async with self._lock:
            maybe = await self.ensure_channel()
            if maybe is None:
                raise ChannelUnavailableError(
                    "the control channel (NATS) is not connected; nothing was changed"
                )
            channel: ControlChannel = maybe

            async def write_bucket(connection: AsyncConnection) -> None:
                state = state_of(
                    await self.store.list(connection),
                    version=await self.store.next_version(connection),
                    default_deny=self.default_deny,
                )
                try:
                    await channel.store(state)
                except Exception as error:
                    self.publish_failures += 1
                    _log.error(
                        "source switch refused: the control channel cannot take "
                        "it; nothing was changed",
                        extra={
                            "source_type": source_type,
                            "instance_id": instance_id,
                            "enabled": enabled,
                            "error": repr(error),
                            "publish_failures": self.publish_failures,
                        },
                    )
                    raise ChannelUnavailableError(
                        "the control channel (NATS JetStream) cannot take the "
                        "switch; nothing was changed"
                    ) from error
                written.append(state)

            try:
                row, changed = await self.store.set(
                    source_type,
                    instance_id,
                    enabled=enabled,
                    reason=reason,
                    actor=actor,
                    actor_name=actor_name,
                    before_commit=write_bucket,
                )
            except ChannelUnavailableError:
                raise
            except Exception:
                if written:
                    # The bucket says what the database never committed.
                    _log.error("switch not committed; restoring the bucket")
                    await self._publish_locked()
                raise
            if written:
                self.published_version = written[-1].version
        _log.info(
            "source switched" if changed else "source already in that state",
            extra={
                "source_type": source_type,
                "instance_id": instance_id,
                "enabled": enabled,
                "actor_id": actor.actor_id,
                "version": written[-1].version if written else None,
            },
        )
        if written:
            try:
                await channel.announce(written[-1])
            except Exception as error:
                # The bucket has it; followers read it within a poll.
                _log.warning(
                    "could not announce the switch; followers read it at their "
                    "next poll",
                    extra={"error": repr(error)},
                )
        return row

    async def publish_current(self) -> bool:
        """Make the bucket say what the database says, and announce it.
        False on failure, which is logged at error level and counted."""
        async with self._lock:
            return await self._publish_locked()

    async def _publish_locked(self) -> bool:
        channel = await self.ensure_channel()
        if channel is None:
            self.publish_failures += 1
            _log.error(
                "source switches not published: no control channel (NATS)",
                extra={"publish_failures": self.publish_failures},
            )
            return False
        try:
            rows = await self.store.list()
            held = await channel.load()
            probe = state_of(rows, version=0, default_deny=self.default_deny)
            if held is not None and same_content(held, probe):
                # Already there: announce the same version, which followers
                # that have it ignore.
                state = held
            else:
                state = state_of(
                    rows,
                    version=await self.store.next_version(),
                    default_deny=self.default_deny,
                )
                await channel.store(state)
            await channel.announce(state)
        except Exception as error:
            self.publish_failures += 1
            _log.error(
                "could not publish the source control state",
                extra={"error": repr(error), "publish_failures": self.publish_failures},
            )
            return False
        if state.version != self.published_version:
            _log.info(
                "source control state published",
                extra={
                    "version": state.version,
                    "default_deny": state.default_deny,
                    "disabled": [entity_id(t, i) for t, i in state.disabled()],
                },
            )
        self.published_version = state.version
        return True


def nats_channel_factory(
    nats_url: str,
    *,
    bucket: str = DEFAULT_BUCKET,
    subject: str = DEFAULT_SUBJECT,
    connect_timeout_s: float = 5.0,
    connect: Callable[..., Awaitable[Any]] | None = None,
) -> ChannelFactory:
    """Makes a NATS control channel. The client reconnects for ever
    (`common.bus.RECONNECT_FOREVER`), and the first connection is bounded
    by `connect_timeout_s` so the API serves without the bus. A bucket that
    cannot be made now (JetStream down) is logged and made at the first
    write instead."""
    import nats

    connector = nats.connect if connect is None else connect

    async def factory() -> ControlChannel:
        client = await asyncio.wait_for(
            connector(nats_url, max_reconnect_attempts=RECONNECT_FOREVER),
            timeout=connect_timeout_s,
        )
        channel = NatsControlChannel(client=client, bucket=bucket, subject=subject)
        try:
            await channel.ensure_bucket()
        except Exception as error:
            _log.error(
                "could not make the source control bucket; JetStream may be "
                "off or full. Switches are refused until it can be written",
                extra={"bucket": bucket, "error": repr(error)},
            )
        return channel

    return factory


async def republish_periodically(
    service: SourceControlService, stop: asyncio.Event, *, every_s: float
) -> None:
    """Publish at once, then every `every_s`, until `stop`. Each pass
    reconnects first if the channel is missing or its connection gave up."""
    while not stop.is_set():
        await service.publish_current()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), every_s)

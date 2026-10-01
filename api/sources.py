"""Source switches: the writer side of U-15 (`common/sources.py`).

The API is the only writer. A switch is one transaction in the relational
database: the `source_controls` row and an `events` row (entity_type
`source`, entity_id `<type>/<instance>` or `<type>/*`) with the operator and
the reason. Only after it commits is the whole state published to NATS, the
bucket first and the subject second, so a follower that is told of a change
and reads the bucket finds it there.

A publish that fails after the commit leaves the change made and not yet
in effect. The caller is told so (`NotPropagatedError`, 503), and the
periodic republish (`republish_periodically`) carries it out once the bus
answers; nothing is rolled back, because the database is the record.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine

from api.actors import Actor
from common import get_logger
from common.sources import (
    DEFAULT_BUCKET,
    DEFAULT_SUBJECT,
    INSTANCE_ID_PATTERN,
    SOURCE_TYPES,
    STATE_KEY,
    WHOLE_TYPE,
    Control,
    SourceControlState,
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


class NotPropagatedError(SourceError):
    """Recorded, and not yet in effect: the republish will carry it."""

    code = "not_propagated"


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


def version_of(rows: list[dict[str, Any]]) -> int:
    """The state's version: the newest change, in microseconds since the
    epoch. Every switch moves `changed_at` forward, so a later state never
    has a smaller version, and a republish of the same rows the same one."""
    if not rows:
        return 0
    newest: datetime = max(row["changed_at"] for row in rows)
    return int(newest.timestamp() * 1_000_000)


def state_of(rows: list[dict[str, Any]], *, default_deny: bool) -> SourceControlState:
    return SourceControlState(
        version=version_of(rows),
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


# One microsecond: how far a change is moved past the row it replaces.
_ONE_MICROSECOND = datetime.resolution

_SELECT = (
    "SELECT source_type, instance_id, enabled, reason, changed_by, changed_at "
    "FROM source_controls"
)


@dataclass
class SourceControlStore:
    """The `source_controls` table and its audit rows."""

    engine: AsyncEngine
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(tz=UTC))

    async def list(self) -> list[dict[str, Any]]:
        async with self.engine.connect() as connection:
            rows = await connection.execute(
                sa.text(f"{_SELECT} ORDER BY source_type, instance_id")
            )
            return [_row(row) for row in rows]

    async def set(
        self,
        source_type: str,
        instance_id: str | None,
        *,
        enabled: bool,
        reason: str,
        actor: Actor,
        actor_name: str,
    ) -> tuple[dict[str, Any], bool]:
        """Switch one source. Returns the row and whether anything changed.

        Setting a source to the state it is already in changes nothing and
        writes no event: an audit log of no-ops buries the switches.
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
            # Never earlier than the row it replaces, so the published
            # version only moves forward even if this clock steps back.
            now = self.clock()
            if current is not None and current.changed_at >= now:
                now = current.changed_at + _ONE_MICROSECOND
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
        return _row(row), True


class ControlChannel(Protocol):
    async def publish(self, state: SourceControlState) -> None: ...


@dataclass
class NatsControlChannel:
    """The bucket and the subject (`common/sources.py`)."""

    client: Any
    bucket: str = DEFAULT_BUCKET
    subject: str = DEFAULT_SUBJECT

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

    async def publish(self, state: SourceControlState) -> None:
        payload = state.to_json()
        kv = await self.client.jetstream().key_value(self.bucket)
        await kv.put(STATE_KEY, payload)
        await self.client.publish(self.subject, payload)
        await self.client.flush()


@dataclass
class SourceControlService:
    store: SourceControlStore
    channel: ControlChannel | None
    default_deny: bool = False
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)
    published_version: int | None = field(default=None, init=False)
    publish_failures: int = field(default=0, init=False)

    async def listing(self) -> dict[str, Any]:
        return {
            "default_deny": self.default_deny,
            "source_types": list(SOURCE_TYPES),
            "controls": await self.store.list(),
        }

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
        if self.channel is None:
            raise ChannelUnavailableError(
                "the control channel (NATS) is not connected; nothing was changed"
            )
        row, changed = await self.store.set(
            source_type,
            instance_id,
            enabled=enabled,
            reason=reason,
            actor=actor,
            actor_name=actor_name,
        )
        _log.info(
            "source switched" if changed else "source already in that state",
            extra={
                "source_type": source_type,
                "instance_id": instance_id,
                "enabled": enabled,
                "actor_id": actor.actor_id,
            },
        )
        if not await self.publish_current():
            raise NotPropagatedError(
                "recorded, but not yet sent to the adapters; it is retried "
                "automatically"
            )
        return row

    async def publish_current(self) -> bool:
        """Publish the state as the database holds it. False on failure,
        which is logged and counted. Serialised, so two switches in quick
        succession reach the bus in the order they were read."""
        if self.channel is None:
            return False
        async with self._lock:
            try:
                state = state_of(
                    await self.store.list(), default_deny=self.default_deny
                )
                await self.channel.publish(state)
            except Exception as error:
                self.publish_failures += 1
                _log.error(
                    "could not publish the source control state",
                    extra={
                        "error": repr(error),
                        "publish_failures": self.publish_failures,
                    },
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


async def republish_periodically(
    service: SourceControlService, stop: asyncio.Event, *, every_s: float
) -> None:
    """Publish at once, then every `every_s`, until `stop`."""
    while not stop.is_set():
        await service.publish_current()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), every_s)

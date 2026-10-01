"""Which position sources are switched on (U-15, `ARCHITECTURE.md` §2.1).

Every source can be switched off without a deploy, by type (all operator
relays, all Remote ID receivers, ...) or by instance (one station, one
receiver, one provider, one feed). This module is the shared reading of that
switch: the state, the rule that decides whether a source is enabled, and the
follower each adapter and the airspace monitor run to keep the state current.

## Where the state lives and how it travels

The API is the only writer. A switch is a row in `source_controls` in the
relational database and an `events` row with the actor and the reason, in
one transaction. The API then publishes the whole state:

- into a JetStream key-value bucket (`SOURCE_CONTROL_BUCKET`, one key,
  `state`), which is the read path: durable on the broker's disk, readable
  by any process that can reach NATS, and so by the Gateway, which must
  never connect to the relational database (CLAUDE.md);
- on the core subject `SOURCE_CONTROL_SUBJECT`, so a follower applies a
  change within a round trip instead of at its next read.

A follower reads the bucket at start and every `SOURCE_CONTROL_POLL_S`, and
applies whatever arrives on the subject in between. A message lost on the
subject, or a broker that restarted, costs at most one poll interval. The
API republishes from the database periodically, so a write to the bucket
that failed after the database committed is repaired without anyone acting.

Why not the telemetry database as the read path: the API would have to
write a second database on every switch, and the Gateway would have to poll
it, with no way to be told of a change. Every follower already holds a NATS
connection, and the bucket keeps the last state across a broker restart.

## The rule

A type switched off disables every instance of it. Otherwise an instance's
own row decides. A source the table does not know is enabled, unless the
API runs with `SOURCES_DEFAULT_DENY`, which travels with the state so every
follower applies the same default. Before any state has ever been published,
every source is enabled: nothing has been switched off.

The types are open-ended on purpose. Network Remote ID providers (U-02,
U-17), ADS-B feeds (U-07) and sensors (U-14) are further values of
`source_type`, not a schema change.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from common.logging import get_logger

_log = get_logger(__name__)

# The source types this system has adapters for, or will (TASKS.md U-02,
# U-07, U-14). `relay` and `remote_id` are the two that exist today.
RELAY = "relay"
REMOTE_ID = "remote_id"
NETWORK_REMOTE_ID = "network_remote_id"
ADSB = "adsb"
SENSOR = "sensor"
SOURCE_TYPES: tuple[str, ...] = (RELAY, REMOTE_ID, NETWORK_REMOTE_ID, ADSB, SENSOR)

# What `instance_id` holds in the database for a switch on a whole type.
WHOLE_TYPE = "*"

SOURCE_TYPE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
# Station and receiver ids as the token and key files write them. No slash
# and no space, so an id is one path segment and one log token.
INSTANCE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")

DEFAULT_BUCKET = "source_control"
DEFAULT_SUBJECT = "control.sources"
STATE_KEY = "state"
DEFAULT_POLL_S = 5.0

# Why a source is disabled, as `SourceControlState.why_disabled` says it.
BY_TYPE = "type"
BY_INSTANCE = "instance"
BY_DEFAULT = "default_deny"


@dataclass(frozen=True, slots=True)
class Control:
    """One switch: a whole type (`instance_id` None) or one instance."""

    source_type: str
    instance_id: str | None
    enabled: bool
    reason: str
    changed_by: str
    # ISO 8601, UTC; carried as text, since followers only show it.
    changed_at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_type": self.source_type,
            "instance_id": self.instance_id,
            "enabled": self.enabled,
            "reason": self.reason,
            "changed_by": self.changed_by,
            "changed_at": self.changed_at,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> Control:
        source_type = raw["source_type"]
        instance_id = raw.get("instance_id")
        enabled = raw["enabled"]
        if not isinstance(source_type, str) or not SOURCE_TYPE_PATTERN.match(
            source_type
        ):
            raise ValueError(f"not a source type: {source_type!r}")
        if instance_id is not None and (
            not isinstance(instance_id, str) or not instance_id
        ):
            raise ValueError(f"not an instance id: {instance_id!r}")
        if not isinstance(enabled, bool):
            raise ValueError(f"enabled is not a boolean: {enabled!r}")
        return cls(
            source_type=source_type,
            instance_id=instance_id,
            enabled=enabled,
            reason=str(raw.get("reason") or ""),
            changed_by=str(raw.get("changed_by") or ""),
            changed_at=str(raw.get("changed_at") or ""),
        )


@dataclass(frozen=True, slots=True)
class SourceControlState:
    """Every switch, as the API last published it.

    `version` orders publications: a follower never replaces a state with
    an older one, which a slow poll racing a fresh push could otherwise do.
    """

    version: int = 0
    default_deny: bool = False
    controls: tuple[Control, ...] = ()
    _by_key: dict[tuple[str, str | None], Control] = field(
        default_factory=dict, init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        for control in self.controls:
            self._by_key[(control.source_type, control.instance_id)] = control

    def control(self, source_type: str, instance_id: str | None) -> Control | None:
        return self._by_key.get((source_type, instance_id))

    def why_disabled(self, source_type: str, instance_id: str | None) -> str | None:
        """None when enabled; else `type`, `instance` or `default_deny`."""
        whole = self._by_key.get((source_type, None))
        if whole is not None and not whole.enabled:
            return BY_TYPE
        if instance_id is None:
            return None
        own = self._by_key.get((source_type, instance_id))
        if own is not None:
            return None if own.enabled else BY_INSTANCE
        return BY_DEFAULT if self.default_deny else None

    def enabled(self, source_type: str, instance_id: str | None) -> bool:
        return self.why_disabled(source_type, instance_id) is None

    def to_json(self) -> bytes:
        return json.dumps(
            {
                "version": self.version,
                "default_deny": self.default_deny,
                "controls": [control.as_dict() for control in self.controls],
            },
            sort_keys=True,
        ).encode("utf-8")

    @classmethod
    def from_json(cls, payload: bytes) -> SourceControlState:
        """Raises ValueError for anything that is not a published state."""
        try:
            raw = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError(f"not JSON: {error}") from error
        if not isinstance(raw, dict):
            raise ValueError("not a JSON object")
        version = raw.get("version")
        default_deny = raw.get("default_deny", False)
        controls = raw.get("controls", [])
        if isinstance(version, bool) or not isinstance(version, int):
            raise ValueError(f"version is not an integer: {version!r}")
        if not isinstance(default_deny, bool):
            raise ValueError("default_deny is not a boolean")
        if not isinstance(controls, list):
            raise ValueError("controls is not a list")
        try:
            parsed = tuple(Control.from_dict(item) for item in controls)
        except (KeyError, TypeError, AttributeError) as error:
            raise ValueError(f"a control is malformed: {error!r}") from error
        return cls(version=version, default_deny=default_deny, controls=parsed)

    def disabled(self) -> list[tuple[str, str | None]]:
        """Every switch that is off, for a log line."""
        return [
            (control.source_type, control.instance_id)
            for control in self.controls
            if not control.enabled
        ]


class SourceSwitch(Protocol):
    """What an adapter asks: may this source's data be taken?"""

    def enabled(self, source_type: str, instance_id: str | None) -> bool: ...


def source_of_telemetry(message: Mapping[str, Any]) -> tuple[str, str]:
    """The `(source_type, instance_id)` a `telemetry.*` message came from.

    Relay telemetry carries no `source` and names its station in
    `station_id`; a Remote ID observation says `source: "remote_id"` and
    names its receiver in `station_id` (gateway/remote_id.py). Further
    adapters follow the second shape.
    """
    source = message.get("source")
    source_type = source if isinstance(source, str) and source else RELAY
    station = message.get("station_id")
    return source_type, "" if station is None else str(station)


StateReader = Callable[[], Awaitable[bytes | None]]
OnChange = Callable[[SourceControlState, SourceControlState], Awaitable[None]]


@dataclass
class SourceControlFollower:
    """Keeps the published state current in one process.

    `read` returns the bucket's value, or None when nothing has been
    published yet; it may raise, and a failed read keeps the state held.
    `on_change(before, after)` runs on every applied change, after `state`
    already says the new thing, so whatever it calls sees the new state.
    """

    read: StateReader
    on_change: OnChange | None = None
    poll_s: float = DEFAULT_POLL_S
    state: SourceControlState = field(default_factory=SourceControlState)
    # Totals for the owner's status line.
    reads: int = field(default=0, init=False)
    read_failures: int = field(default=0, init=False)
    changes: int = field(default=0, init=False)
    ignored_older: int = field(default=0, init=False)
    ignored_malformed: int = field(default=0, init=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)
    _poller: asyncio.Task[None] | None = field(default=None, init=False)

    def enabled(self, source_type: str, instance_id: str | None) -> bool:
        return self.state.enabled(source_type, instance_id)

    def why_disabled(self, source_type: str, instance_id: str | None) -> str | None:
        return self.state.why_disabled(source_type, instance_id)

    async def refresh(self) -> bool:
        """Read the bucket and apply what it holds. False when the read
        failed; the state held is kept, and the failure is logged."""
        try:
            payload = await self.read()
        except Exception as error:
            self.read_failures += 1
            _log.error(
                "could not read the source control state; keeping what is held",
                extra={
                    "error": repr(error),
                    "version": self.state.version,
                    "read_failures": self.read_failures,
                },
            )
            return False
        self.reads += 1
        if payload is not None:
            await self.offer(payload, origin="read")
        return True

    async def offer(self, payload: bytes, *, origin: str) -> bool:
        """Apply a published state, unless it is older than the one held or
        not a state at all. True when it changed something."""
        try:
            state = SourceControlState.from_json(payload)
        except ValueError as error:
            self.ignored_malformed += 1
            _log.error(
                "ignored a source control state that does not parse",
                extra={"origin": origin, "error": str(error)},
            )
            return False
        return await self.apply(state, origin=origin)

    async def apply(self, state: SourceControlState, *, origin: str) -> bool:
        async with self._lock:
            before = self.state
            if state.version < before.version:
                self.ignored_older += 1
                return False
            if state == before:
                return False
            self.state = state
            self.changes += 1
            _log.info(
                "source control state applied",
                extra={
                    "origin": origin,
                    "version": state.version,
                    "default_deny": state.default_deny,
                    "disabled": [f"{t}/{i or WHOLE_TYPE}" for t, i in state.disabled()],
                },
            )
            if self.on_change is not None:
                try:
                    await self.on_change(before, state)
                except Exception:
                    # The state is applied either way: every check reads it.
                    # What failed is the owner's reaction, and the next poll
                    # does not repeat it, so it is logged loudly.
                    _log.exception(
                        "reacting to a source control change failed",
                        extra={"version": state.version},
                    )
            return True

    async def start(self) -> None:
        """Read once, then poll every `poll_s` until `stop`."""
        await self.refresh()
        self._poller = asyncio.create_task(self._poll(), name="source-control-poll")

    async def stop(self) -> None:
        if self._poller is not None:
            self._poller.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._poller
            self._poller = None

    async def _poll(self) -> None:
        while True:
            await asyncio.sleep(self.poll_s)
            await self.refresh()

    def status(self) -> dict[str, int]:
        return {
            "source_control_version": self.state.version,
            "source_control_read_failures": self.read_failures,
            "source_control_changes": self.changes,
        }


def bucket_reader(
    client: Any, bucket: str = DEFAULT_BUCKET, key: str = STATE_KEY
) -> StateReader:
    """A `read` for `SourceControlFollower` over a nats-py client.

    A bucket or key that does not exist yet is "nothing published", not a
    failure: a fresh deployment has switched nothing off.
    """
    from nats.js.errors import BucketNotFoundError, KeyNotFoundError, NotFoundError

    async def read() -> bytes | None:
        try:
            kv = await client.jetstream().key_value(bucket)
            entry = await kv.get(key)
        except (BucketNotFoundError, KeyNotFoundError, NotFoundError):
            return None
        value = entry.value
        return None if value is None else bytes(value)

    return read


async def follow(
    client: Any,
    follower: SourceControlFollower,
    *,
    subject: str = DEFAULT_SUBJECT,
) -> Any:
    """Subscribe `follower` to pushed changes, read once, start polling.

    Returns the subscription, which the caller unsubscribes (or drains with
    the connection) on stopping.
    """

    async def on_message(message: Any) -> None:
        await follower.offer(bytes(message.data), origin="push")

    subscription = await client.subscribe(subject, cb=on_message)
    await follower.start()
    return subscription

"""What one adapter's sources are doing, and the switch applied to them. U-15.

Every adapter asks the same question before it takes anything from a
source: is this source switched on (`common/sources.py`)? This module asks
it, and keeps the evidence an operator needs to tell a source the authority
switched off from one that is merely silent:

- per instance, when it was last heard, how much was taken from it, and
  how much was refused because it is disabled;
- a refusal is logged at most once per instance per interval, with a count
  of those suppressed in between (`gateway.rate_limit`), so a disabled
  receiver still transmitting is visible without filling the disk;
- `snapshot()` is what the adapter publishes on `source.<type>` every few
  seconds, which the console feed forwards; `status()` is what goes in the
  adapter's status line.

It knows nothing about relays or Remote ID: the relay server counts a
refused upgrade and the Remote ID ingest a dropped datagram, through the
same calls. A further adapter (U-02, U-07, U-14) does the same.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections import OrderedDict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from common import get_logger
from common.sources import WHOLE_TYPE, SourceControlFollower, SourceControlState
from gateway.publisher import Bus
from gateway.rate_limit import RateLimiter

_log = get_logger(__name__)

SOURCE_SUBJECT = "source"
# How often the adapter says what its sources are doing. Fast enough that a
# switch shows in the console within a couple of seconds of taking effect.
PUBLISH_INTERVAL_S = 2.0
# How often the totals go in the status line.
STATUS_INTERVAL_S = 60.0
# Instance ids are named by whoever connects (an unauthenticated loopback
# receiver can say anything), so the table is bounded, least recently
# active out. Far beyond any real count of stations or receivers.
MAX_INSTANCES = 1024


def source_subject(source_type: str) -> str:
    return f"{SOURCE_SUBJECT}.{source_type}"


def wall_clock() -> datetime:
    return datetime.now(tz=UTC)


@dataclass
class InstanceActivity:
    last_seen_at: datetime | None = None
    accepted: int = 0
    refused_disabled: int = 0
    last_refused_at: datetime | None = None


class StateHolder(Protocol):
    """`common.sources.SourceControlFollower`, or anything holding a state."""

    @property
    def state(self) -> SourceControlState: ...


@dataclass
class SourceActivity:
    source_type: str
    # None: nothing is ever disabled (a test, or no control channel).
    switch: StateHolder | None = None
    # Instances the adapter is configured for (a token file, a key file):
    # listed even before they are first heard, so a station that never
    # connected is visibly silent rather than absent.
    known: Iterable[str] = ()
    # Instances with an open connection, for adapters that have one.
    connected: Callable[[], set[str]] | None = None
    wall: Callable[[], datetime] = wall_clock
    refusals: RateLimiter = field(default_factory=RateLimiter)
    max_instances: int = MAX_INSTANCES
    instances: OrderedDict[str, InstanceActivity] = field(
        default_factory=OrderedDict, init=False
    )
    refused_disabled: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        self.known = tuple(sorted(set(self.known)))

    @property
    def state(self) -> SourceControlState:
        return SourceControlState() if self.switch is None else self.switch.state

    def enabled(self, instance_id: str | None) -> bool:
        return self.state.enabled(self.source_type, instance_id)

    def admit(self, instance_id: str, count: int = 1) -> bool:
        """Whether `count` items from `instance_id` may be taken. A refusal
        is counted, and logged at most once per instance per interval."""
        if not self.enabled(instance_id):
            self.refuse(instance_id, count)
            return False
        activity = self._activity(instance_id)
        activity.last_seen_at = self.wall()
        activity.accepted += count
        return True

    def refuse(self, instance_id: str, count: int = 1) -> None:
        """Count `count` items refused from a disabled `instance_id`."""
        why = self.state.why_disabled(self.source_type, instance_id)
        activity = self._activity(instance_id)
        activity.last_refused_at = self.wall()
        activity.refused_disabled += count
        self.refused_disabled += count
        suppressed = self.refusals.admit(instance_id)
        if suppressed is not None:
            _log.warning(
                "source disabled; refused",
                extra={
                    "source_type": self.source_type,
                    "station_id": instance_id,
                    "disabled_by": why,
                    "suppressed": suppressed,
                    "refused_disabled": activity.refused_disabled,
                },
            )

    def seen(self, instance_id: str) -> None:
        """Heard, without taking anything (a relay's `status` message)."""
        self._activity(instance_id).last_seen_at = self.wall()

    def _activity(self, instance_id: str) -> InstanceActivity:
        activity = self.instances.pop(instance_id, None)
        if activity is None:
            activity = InstanceActivity()
        self.instances[instance_id] = activity
        while len(self.instances) > self.max_instances:
            self.instances.popitem(last=False)
        return activity

    def snapshot(self) -> dict[str, Any]:
        """What `source.<type>` carries: every instance known or heard."""
        state = self.state
        connected = self.connected() if self.connected is not None else None
        names = sorted(set(self.known) | set(self.instances) | (connected or set()))
        instances = []
        for name in names:
            activity = self.instances.get(name, InstanceActivity())
            instances.append(
                {
                    "instance_id": name,
                    "enabled": state.enabled(self.source_type, name),
                    "disabled_by": state.why_disabled(self.source_type, name),
                    "last_seen_at": _iso(activity.last_seen_at),
                    "accepted": activity.accepted,
                    "refused_disabled": activity.refused_disabled,
                    "last_refused_at": _iso(activity.last_refused_at),
                    "connected": None if connected is None else name in connected,
                }
            )
        return {
            "source_type": self.source_type,
            "enabled": state.enabled(self.source_type, None),
            "control_version": state.version,
            "published_at": self.wall().isoformat(),
            "instances": instances,
        }

    def status(self) -> dict[str, int]:
        state = self.state
        return {
            "refused_source_disabled": self.refused_disabled,
            "sources_disabled": sum(
                1
                for name in set(self.known) | set(self.instances)
                if not state.enabled(self.source_type, name)
            ),
            "source_type_disabled": int(not state.enabled(self.source_type, None)),
            # Whether the switches themselves could be read (common/sources.py).
            **(
                self.switch.status()
                if isinstance(self.switch, SourceControlFollower)
                else {}
            ),
        }


def _iso(moment: datetime | None) -> str | None:
    return None if moment is None else moment.isoformat()


async def publish_periodically(
    activity: SourceActivity,
    bus: Bus,
    stop: asyncio.Event,
    *,
    every_s: float = PUBLISH_INTERVAL_S,
    status_every_s: float = STATUS_INTERVAL_S,
) -> None:
    """Publish `activity.snapshot()` every `every_s` and log its totals
    every `status_every_s`, until `stop`. A failed publish is logged and
    the loop goes on: the console is a view, never a condition."""
    since_status_s = 0.0
    while not stop.is_set():
        try:
            await bus.publish(
                source_subject(activity.source_type),
                json.dumps(activity.snapshot()).encode("utf-8"),
            )
        except Exception as error:
            _log.error(
                "could not publish source activity",
                extra={"source_type": activity.source_type, "error": repr(error)},
            )
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), every_s)
        since_status_s += every_s
        if since_status_s >= status_every_s or stop.is_set():
            since_status_s = 0.0
            _log.info(
                "source status",
                extra={
                    "source_type": activity.source_type,
                    "disabled": [
                        f"{t}/{i or WHOLE_TYPE}"
                        for t, i in activity.state.disabled()
                        if t == activity.source_type
                    ],
                    **activity.status(),
                },
            )

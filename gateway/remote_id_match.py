"""One of our aircraft heard broadcasting Remote ID is one track. P1-15.

Our aircraft send MAVLink telemetry, and many will also broadcast Remote ID,
as the regulation requires. Without matching they appear twice, under two
ids, and the airspace monitor raises a conflict between an aircraft and
itself. The broadcast serial is matched against `known_drones.serial`, the
projection of the fleet registry's serial numbers.

## What a matched broadcast does

- **While the aircraft's own telemetry is live**, the broadcast is stored,
  with the drone it matched, but not published. The MAVLink track is the
  better one: authenticated through the relay, and several times a second.
- **When its telemetry has gone quiet** (a radio link lost, a relay down),
  the broadcast is published *as that aircraft*: its drone_id, its label,
  still marked `source: remote_id` and unauthenticated. The track does not
  vanish when our link does, and it never becomes two.

Only a serial number is matched (ID type 1). A CAA registration or session
id can move between airframes; matching one would attach a stranger's
broadcast to our aircraft.

## A broadcast that is not where our aircraft is (S-10, U-02)

While the aircraft's telemetry is live, the ingest also knows where the
relay, which authenticates its station, last placed it. A broadcast of its
serial more than `spoof_distance_m` from there is not our aircraft: it is
published as a separate, unverified track under its own id, never withheld
and never under our aircraft's id (`gateway/identification.py`).

## Freshness

The ingest follows `telemetry.*` on the bus and notes when each drone last
sent MAVLink telemetry, ignoring Remote ID observations, its own included. A
drone is live for `live_for_s` after its last message. MAVLink arrives
several times a second, so five seconds of silence is a lost link, and the
broadcast takes over well inside the airspace monitor's 15 s staleness
horizon, before the aircraft would drop out of it.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine

from common import get_logger
from gateway import odid
from gateway.rate_limit import RateLimiter
from gateway.registry_projection import RegistrySnapshot

_log = get_logger(__name__)

DEFAULT_LIVE_FOR_S = 5.0
# S-10. How far a broadcast of one of our serials may be from where the
# relay last placed that aircraft and still be it. Remote ID positions are
# GNSS fixes within metres, broadcast within a second; 300 m covers 5 s of
# relay latency at 60 m/s. Configuration: REMOTE_ID_SPOOF_DISTANCE_M.
DEFAULT_SPOOF_DISTANCE_M = 300.0
_EARTH_RADIUS_M = 6_371_008.8

_SERIALS = sa.text(
    """
    SELECT serial, drone_id, label FROM known_drones
    WHERE serial IS NOT NULL AND retired_at IS NULL
    """
)


@dataclass(frozen=True, slots=True)
class Registered:
    drone_id: UUID
    label: str


@dataclass
class FleetSerials:
    by_serial: dict[str, Registered] = field(default_factory=dict)

    def match(self, observation: dict[str, Any]) -> Registered | None:
        rid = observation["remote_id"]
        if rid["id_type"] != odid.IdType.SERIAL_NUMBER:
            return None
        return self.by_serial.get(rid["ua_id"])

    def match_serial(self, serial: str | None) -> Registered | None:
        """A serial from any source (network Remote ID's `uas_id`)."""
        return None if not serial else self.by_serial.get(serial)

    async def refresh(self, engine: AsyncEngine) -> None:
        async with engine.connect() as connection:
            rows = (await connection.execute(_SERIALS)).all()
        self.by_serial = {
            str(row.serial): Registered(drone_id=row.drone_id, label=str(row.label))
            for row in rows
        }

    def take(self, snapshot: RegistrySnapshot) -> None:
        """The serials of a registry snapshot (U-02), which is read more
        often than `refresh` and holds the same rows."""
        self.by_serial = {
            serial: Registered(drone_id=facts.drone_id, label=facts.label)
            for serial, facts in snapshot.by_serial.items()
        }


def distance_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Great-circle distance between two (lat, lon) in degrees, metres."""
    lat1, lon1 = map(math.radians, a)
    lat2, lon2 = map(math.radians, b)
    h = (
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    )
    return 2 * _EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(h)))


@dataclass
class LinkFreshness:
    """When each drone last sent relay telemetry, and where it was."""

    live_for_s: float = DEFAULT_LIVE_FOR_S
    _heard_s: dict[UUID, float] = field(default_factory=dict, init=False)
    _position: dict[UUID, tuple[float, float]] = field(default_factory=dict, init=False)

    # Rows the link's liveness and position were not taken from: a relay
    # backlog, or a row captured too long before the Gateway received it.
    ignored_history: int = field(default=0, init=False)

    def on_telemetry(self, payload: bytes, *, now_s: float) -> None:
        """A `telemetry.*` message from the bus. Only live relay telemetry
        counts: it carries no `source`, where every broadcast source (direct
        and network Remote ID) names itself.

        A row the Gateway flagged `backlog`, or one whose `captured_at` is
        more than `live_for_s` behind its `rx_ts`, is history: a relay
        draining its queue after an outage delivers minutes-old positions,
        and taking them as where the aircraft is now would make its own
        current broadcast look like a spoof (S-10). Such rows neither make
        the link live nor move the position.
        """
        try:
            message = json.loads(payload)
            if message.get("source") not in (None, "relay"):
                return
            drone_id = UUID(str(message["drone_id"]))
            if _history(message, self.live_for_s):
                self.ignored_history += 1
                return
            self._heard_s[drone_id] = now_s
            lat, lon = message.get("lat_deg"), message.get("lon_deg")
            if isinstance(lat, int | float) and isinstance(lon, int | float):
                self._position[drone_id] = (float(lat), float(lon))
        except (ValueError, KeyError, TypeError, AttributeError):
            _log.warning("unreadable telemetry message on the bus")

    def live(self, drone_id: UUID, *, now_s: float) -> bool:
        heard_s = self._heard_s.get(drone_id)
        return heard_s is not None and now_s - heard_s <= self.live_for_s

    def position(self, drone_id: UUID) -> tuple[float, float] | None:
        """Where the relay last placed the drone; None if it never did."""
        return self._position.get(drone_id)


def _history(message: dict[str, Any], live_for_s: float) -> bool:
    """A relay row that says where the aircraft was, not where it is."""
    if message.get("backlog") is True:
        return True
    captured, received = message.get("captured_at"), message.get("rx_ts")
    if not isinstance(captured, str) or not isinstance(received, str):
        return False
    behind_s = (
        datetime.fromisoformat(received) - datetime.fromisoformat(captured)
    ).total_seconds()
    return behind_s > live_for_s


class Verdict(StrEnum):
    """What a broadcast claiming a serial is, against our fleet (U-02)."""

    # Not one of our serials.
    STRANGER = "stranger"
    # Ours, its relay telemetry live and agreeing: the relay track is better.
    WITHHOLD = "withhold"
    # Ours, its relay quiet: the broadcast speaks for it (P1-15).
    AS_OURS = "as_ours"
    # Ours by serial, but away from where its live relay telemetry places it
    # (S-10): a separate, unverified track.
    CONFLICT = "conflict"


@dataclass(frozen=True, slots=True)
class FleetJudgement:
    verdict: Verdict
    aircraft: Registered | None = None
    # How far from the relay's position, when that was judged.
    apart_m: float | None = None


def judge(
    ours: Registered | None,
    position: tuple[float, float] | None,
    links: LinkFreshness,
    *,
    now_s: float,
    spoof_distance_m: float,
) -> FleetJudgement:
    """One rule for every broadcast source, direct and network Remote ID:
    withhold, speak for our aircraft, or split off as a conflict."""
    if ours is None:
        return FleetJudgement(Verdict.STRANGER)
    if not links.live(ours.drone_id, now_s=now_s):
        return FleetJudgement(Verdict.AS_OURS, ours)
    relay = links.position(ours.drone_id)
    if relay is None or position is None:
        return FleetJudgement(Verdict.WITHHOLD, ours)
    apart_m = distance_m(relay, position)
    if apart_m > spoof_distance_m:
        return FleetJudgement(Verdict.CONFLICT, ours, apart_m)
    return FleetJudgement(Verdict.WITHHOLD, ours, apart_m)


def report_conflict(
    limiter: RateLimiter,
    judgement: FleetJudgement,
    *,
    broadcast_drone_id: str,
    station_id: object,
    source: str,
    spoof_distance_m: float,
    serial_conflicts: int,
) -> None:
    """Log an S-10 conflict, at most once per aircraft per interval."""
    if judgement.aircraft is None:
        return
    suppressed = limiter.admit(("serial_conflict", judgement.aircraft.drone_id))
    if suppressed is None:
        return
    _log.warning(
        "broadcast of our serial away from our aircraft; published as a "
        "separate unverified track",
        extra={
            "drone_id": str(judgement.aircraft.drone_id),
            "broadcast_drone_id": broadcast_drone_id,
            "source": source,
            "apart_m": None if judgement.apart_m is None else round(judgement.apart_m),
            "spoof_distance_m": spoof_distance_m,
            "station_id": station_id,
            "suppressed": suppressed,
            "serial_conflicts": serial_conflicts,
        },
    )


def as_registered(observation: dict[str, Any], aircraft: Registered) -> dict[str, Any]:
    """The broadcast, published as the aircraft it matched."""
    return {
        **observation,
        "drone_id": str(aircraft.drone_id),
        "label": aircraft.label,
        "remote_id": {**observation["remote_id"], "matched": True},
    }

"""From telemetry to alerts: conflicts between aircraft, zone incursions, and
aircraft above the height limit.

Fed one telemetry message at a time (the Gateway's `telemetry.<drone_id>`
payload). Returns what changed: alerts raised and alerts cleared. The caller
publishes them and writes them to the audit log; this module does neither, so
it can be tested without a bus or a database.

## What is considered

Only **armed** aircraft (or, for Remote ID, aircraft declared airborne) with
a position, an AMSL altitude and a velocity. An
aircraft on the ground at a base is routinely within metres of another, and
alerting on it would teach operators to ignore the alert (P6-03). Armed is the
nearest thing telemetry has to "flying"; erring towards armed-on-the-ground
counting as airborne is the safe side.

An aircraft whose telemetry has not been heard for `stale_after_s` is dropped
and its alerts cleared: it is not known to be anywhere any more, and the link
state already says why (P1-05).

## Time is the capture time, not the arrival time (S-11)

Every message carries `ts`, the relay's capture clock. A track is placed at
that instant, and a pair's CPA is computed at the later of the two capture
times with the older track advanced along its velocity (`cpa.advance`): a
neighbour's 5 s old sample, used as if current, is 75 m wrong at 15 m/s
against a 60 m threshold. A neighbour older than `neighbour_max_age_s` is
left out altogether. A message captured further than `live_max_age_s` from
the monitor's clock, either way, is not live (a replayed backlog, or a wrong
ground-station clock) and is counted and not evaluated: it must not raise an
alert about where an aircraft was minutes ago. A message older than the
sample already held for its aircraft is ignored for the same reason.

## Raise once, clear with hysteresis

An alert is raised once per condition, not once per tick, and cleared only
once telemetry has *shown* the condition false for longer than
`clear_after_s`. Without the delay an aircraft sitting on a zone boundary, or
a pair hovering near the threshold, would raise and clear every second - the
kind of alert people learn to scroll past.

Silence is not evidence. A pair whose telemetry stops is not shown to be
clear of each other, so its alert stays until the aircraft are dropped as
stale, and is cleared then with the reason `stale` ("no longer tracked"),
not `resolved`. Every clear carries its reason (`Cleared.reason`), and the
service publishes and audits it.

## Height above ground (P5-19)

An aircraft is too high when its AMSL altitude minus the ground elevation
under it (the DEM, `common/terrain.py`) exceeds `max_height_agl_m`. Telemetry
cannot say this by itself: MAVLink gives height above home, which over a
valley 150 m below home is 150 m short.

Where the ground elevation is unknown (no terrain configured, or a cell that
was never fetched) the limit is **not evaluated**. The monitor stays silent
rather than guessing. The console shows the elevation as unknown there, and
the service logs it at start-up. The DEM is a surface model accurate to a few
metres (`docs/runbooks/p5-00-terrain.md`), so an aircraft near the limit over
trees or roofs can be flagged a few metres early.

## Stage 0: an alert, not a resolution

The alert names both aircraft, the time to closest approach and the distance.
The deterministic resolution and its delivery as a pilot instruction are P5-08
and P5-09, not built here.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID

from airspace.cpa import Approach, SeparationPolicy, Track, closest_approach
from airspace.neighbours import NeighbourIndex
from airspace.zones import Zone, ZoneType
from common import get_logger
from common.terrain import Elevation

_log = get_logger(__name__)


class Severity(StrEnum):
    CRITICAL = "critical"
    WARNING = "warning"


class AlertKind(StrEnum):
    CONFLICT = "conflict"
    ZONE = "zone"
    HEIGHT = "height"


class GroundElevation(Protocol):
    """`common.terrain.Terrain`, or anything that answers like it."""

    def elevation(self, lat_deg: float, lon_deg: float) -> Elevation | None: ...


@dataclass(frozen=True, slots=True)
class Alert:
    key: str
    kind: AlertKind
    severity: Severity
    drone_ids: tuple[UUID, ...]
    labels: tuple[str | None, ...]
    detail: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "kind": self.kind.value,
            "severity": self.severity.value,
            "drone_ids": [str(drone_id) for drone_id in self.drone_ids],
            "labels": list(self.labels),
            "detail": self.detail,
        }


class ClearReason(StrEnum):
    # Telemetry showed the condition false for longer than `clear_after_s`.
    RESOLVED = "resolved"
    # An aircraft involved is no longer tracked; nothing showed it clear.
    STALE = "stale"


@dataclass(frozen=True, slots=True)
class Cleared:
    alert: Alert
    reason: ClearReason


@dataclass(frozen=True, slots=True)
class Change:
    raised: list[Alert]
    cleared: list[Cleared]


def captured_at_s(message: dict[str, Any]) -> float:
    """The message's capture time as epoch seconds, from its ISO 8601 `ts`.

    Raises ValueError when there is none: a position with no time cannot be
    compared with anything, and guessing "now" is what S-11 removed.
    """
    ts = message.get("ts")
    if not isinstance(ts, str):
        raise ValueError("telemetry has no ts")
    moment = datetime.fromisoformat(ts)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.timestamp()


def track_from_telemetry(message: dict[str, Any]) -> Track | None:
    """A Track, or None if the message cannot place the aircraft in 3-D.

    Raises ValueError for a message that has the fields but cannot be used:
    no capture time, or a number that is not finite (an `inf` latitude would
    reach `math.floor` in the neighbour grid and overflow there, S-12).
    """
    needed = ("lat_deg", "lon_deg", "alt_amsl_m", "vx_ms", "vy_ms", "vz_ms")
    if any(message.get(name) is None for name in needed):
        return None
    values = {name: float(message[name]) for name in needed}
    for name, value in values.items():
        if not math.isfinite(value):
            raise ValueError(f"{name} is not finite: {value}")
    return Track(
        drone_id=UUID(str(message["drone_id"])),
        lat_deg=values["lat_deg"],
        lon_deg=values["lon_deg"],
        alt_amsl_m=values["alt_amsl_m"],
        vn_ms=values["vx_ms"],
        ve_ms=values["vy_ms"],
        vd_ms=values["vz_ms"],
        captured_at_s=captured_at_s(message),
    )


def _flying(message: dict[str, Any]) -> bool:
    """Armed, for MAVLink telemetry; declared airborne, for Remote ID (P1-15).

    Remote ID has no arming state and says `armed: None`; its `airborne` is
    False only for a declared "ground" status.
    """
    return message.get("armed") is True or message.get("airborne") is True


def conflict_key(a: UUID, b: UUID) -> str:
    first, second = sorted((str(a), str(b)))
    return f"conflict:{first}:{second}"


def zone_key(drone_id: UUID, zone: Zone) -> str:
    return f"zone:{zone.zone_id}:{drone_id}"


def height_key(drone_id: UUID) -> str:
    return f"height:{drone_id}"


@dataclass
class AirspaceMonitor:
    policy: SeparationPolicy
    zones: list[Zone] = field(default_factory=list)
    # Both needed for the height limit; with either missing it is not checked.
    terrain: GroundElevation | None = None
    max_height_agl_m: float | None = None
    stale_after_s: float = 15.0
    clear_after_s: float = 3.0
    # S-11; the defaults match `airspace.config.AirspaceSettings`.
    live_max_age_s: float = 10.0
    neighbour_max_age_s: float = 10.0

    index: NeighbourIndex = field(init=False)
    # Messages not evaluated because they were not live, or older than the
    # sample already held. Counted so a replayed backlog, or a wrong
    # ground-station clock, shows in the log rather than in the silence.
    rejected: int = field(default=0, init=False)
    _rejected_logged: set[UUID] = field(default_factory=set, init=False)
    # Checks that raised instead of answering (S-12). Each is logged with its
    # traceback; the count is here so a test, or a health report, can see it.
    check_failures: int = field(default=0, init=False)
    _labels: dict[UUID, str | None] = field(default_factory=dict, init=False)
    _last_seen_s: dict[UUID, float] = field(default_factory=dict, init=False)
    _active: dict[str, Alert] = field(default_factory=dict, init=False)
    # When each active alert's condition was last seen true, and last seen
    # false by a message that evaluated it.
    _last_true_s: dict[str, float] = field(default_factory=dict, init=False)
    _last_false_s: dict[str, float] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        self.index = NeighbourIndex(radius_m=self.policy.neighbour_radius_m)

    @property
    def active(self) -> list[Alert]:
        return list(self._active.values())

    def observe(self, message: dict[str, Any], *, now_s: float) -> Change:
        """Take one telemetry message; return the alerts it raised or cleared.

        `now_s` is the monitor's wall clock, on the same epoch as the
        message's `ts`. The message is evaluated at its capture time; `now_s`
        only decides whether it is live at all, and what has gone stale.
        """
        drone_id = UUID(str(message["drone_id"]))
        self._labels[drone_id] = message.get("label")
        track = track_from_telemetry(message) if _flying(message) else None

        raised: list[Alert] = []
        if track is None:
            self.index.remove(drone_id)
            self._last_seen_s.pop(drone_id, None)
        elif not self._rejects(track, now_s):
            at_s = track.captured_at_s
            self.index.upsert(track)
            self._last_seen_s[drone_id] = at_s
            # Each check on its own: a missing terrain tile must not silence
            # the conflict and zone alerts already found (S-12).
            not_evaluated: set[AlertKind] = set()
            for kind, check in (
                (AlertKind.CONFLICT, self._check_conflicts),
                (AlertKind.ZONE, self._check_zones),
                (AlertKind.HEIGHT, self._check_height),
            ):
                found = self._guarded(kind, check, track, at_s)
                if found is None:
                    not_evaluated.add(kind)
                else:
                    raised.extend(found)
            # Every active alert this aircraft is part of was just evaluated.
            # The ones not refreshed are false as of this message. A check
            # that failed evaluated nothing: its alerts are neither refreshed
            # nor shown false, so an error cannot clear one.
            for key, alert in self._active.items():
                if (
                    drone_id in alert.drone_ids
                    and alert.kind not in not_evaluated
                    and self._last_true_s[key] != at_s
                ):
                    self._last_false_s[key] = at_s

        cleared = self._expire(now_s)
        return Change(raised=raised, cleared=cleared)

    def _guarded(
        self,
        kind: AlertKind,
        check: Callable[[Track, float], list[Alert]],
        track: Track,
        at_s: float,
    ) -> list[Alert] | None:
        """The check's alerts, or None when it raised instead of answering."""
        try:
            return check(track, at_s)
        except Exception:
            self.check_failures += 1
            _log.exception(
                "airspace check failed; this message is not evaluated for it",
                extra={
                    "check": kind.value,
                    "drone_id": str(track.drone_id),
                    "lat_deg": track.lat_deg,
                    "lon_deg": track.lon_deg,
                    "check_failures": self.check_failures,
                },
            )
            return None

    def _rejects(self, track: Track, now_s: float) -> bool:
        """Whether the sample must not be evaluated: not live, or older than
        the one already held. Counted, and logged once per aircraft per run
        of rejections, so a backlog of thousands is one line, not thousands.
        """
        age_s = now_s - track.captured_at_s
        held = self.index.track(track.drone_id)
        if abs(age_s) > self.live_max_age_s:
            reason = "not live"
        elif held is not None and track.captured_at_s < held.captured_at_s:
            reason = "older than the sample held"
        else:
            self._rejected_logged.discard(track.drone_id)
            return False
        self.rejected += 1
        if track.drone_id not in self._rejected_logged:
            self._rejected_logged.add(track.drone_id)
            _log.warning(
                "telemetry not evaluated",
                extra={
                    "drone_id": str(track.drone_id),
                    "reason": reason,
                    "age_s": round(age_s, 1),
                    "live_max_age_s": self.live_max_age_s,
                    "rejected": self.rejected,
                },
            )
        return True

    def tick(self, *, now_s: float) -> Change:
        """Drop stale aircraft and clear what has resolved, with no message.

        Needed because the end of a condition can be the *absence* of
        telemetry: an aircraft that lands and disarms, or goes silent, sends
        nothing that would clear its alert.
        """
        return Change(raised=[], cleared=self._expire(now_s))

    # --- conditions ----------------------------------------------------------

    def _check_conflicts(self, track: Track, now_s: float) -> list[Alert]:
        raised: list[Alert] = []
        for other in self.index.neighbours(track.drone_id):
            if (
                abs(track.captured_at_s - other.captured_at_s)
                > self.neighbour_max_age_s
            ):
                # Too old to advance along a straight line with any meaning.
                continue
            approach = closest_approach(track, other)
            if not self.policy.is_conflict(approach):
                continue
            key = conflict_key(track.drone_id, other.drone_id)
            alert = self._conflict_alert(key, approach)
            self._last_true_s[key] = now_s
            if key not in self._active:
                raised.append(alert)
            # Refreshed either way: the numbers change as the pair closes.
            self._active[key] = alert
        return raised

    def _conflict_alert(self, key: str, approach: Approach) -> Alert:
        # Ordered by drone id, so both aircraft's messages describe the pair
        # the same way (P5-08's determinism starts here).
        a, b = sorted((approach.first, approach.second), key=str)
        return Alert(
            key=key,
            kind=AlertKind.CONFLICT,
            severity=Severity.CRITICAL,
            drone_ids=(a, b),
            labels=(self._labels.get(a), self._labels.get(b)),
            detail={
                "t_cpa_s": round(approach.t_cpa_s, 1),
                "d_cpa_horizontal_m": round(approach.d_cpa_horizontal_m, 1),
                "d_alt_at_cpa_m": round(approach.d_alt_at_cpa_m, 1),
                "d_horizontal_now_m": round(approach.d_horizontal_now_m, 1),
            },
        )

    def _check_zones(self, track: Track, now_s: float) -> list[Alert]:
        raised: list[Alert] = []
        for zone in self.zones:
            if not zone.contains(track.lat_deg, track.lon_deg, track.alt_amsl_m):
                continue
            key = zone_key(track.drone_id, zone)
            alert = Alert(
                key=key,
                kind=AlertKind.ZONE,
                severity=(
                    Severity.CRITICAL
                    if zone.type is ZoneType.NO_FLY
                    else Severity.WARNING
                ),
                drone_ids=(track.drone_id,),
                labels=(self._labels.get(track.drone_id),),
                detail={
                    "zone_id": str(zone.zone_id),
                    "zone_name": zone.name,
                    "zone_type": zone.type.value,
                    "alt_amsl_m": round(track.alt_amsl_m, 1),
                },
            )
            self._last_true_s[key] = now_s
            if key not in self._active:
                raised.append(alert)
            self._active[key] = alert
        return raised

    def _check_height(self, track: Track, now_s: float) -> list[Alert]:
        if self.terrain is None or self.max_height_agl_m is None:
            return []
        ground = self.terrain.elevation(track.lat_deg, track.lon_deg)
        if ground is None:
            return []
        height_agl_m = track.alt_amsl_m - ground.elevation_m
        if height_agl_m <= self.max_height_agl_m:
            return []
        key = height_key(track.drone_id)
        alert = Alert(
            key=key,
            kind=AlertKind.HEIGHT,
            severity=Severity.WARNING,
            drone_ids=(track.drone_id,),
            labels=(self._labels.get(track.drone_id),),
            detail={
                "height_agl_m": round(height_agl_m, 1),
                "max_height_agl_m": self.max_height_agl_m,
                "alt_amsl_m": round(track.alt_amsl_m, 1),
                "ground_elevation_m": round(ground.elevation_m, 1),
                "dataset": ground.dataset,
            },
        )
        self._last_true_s[key] = now_s
        raised = [] if key in self._active else [alert]
        # Refreshed either way: the height changes as the aircraft climbs.
        self._active[key] = alert
        return raised

    # --- clearing --------------------------------------------------------------

    def _expire(self, now_s: float) -> list[Cleared]:
        for drone_id, seen_s in list(self._last_seen_s.items()):
            if now_s - seen_s > self.stale_after_s:
                self.index.remove(drone_id)
                del self._last_seen_s[drone_id]

        tracked = set(self._last_seen_s)
        cleared: list[Cleared] = []
        for key, alert in list(self._active.items()):
            gone = not all(drone_id in tracked for drone_id in alert.drone_ids)
            shown_false_for_s = (
                self._last_false_s.get(key, 0.0) - self._last_true_s[key]
            )
            resolved = shown_false_for_s > self.clear_after_s
            if gone or resolved:
                # Evidence outranks silence when both hold at once.
                reason = ClearReason.RESOLVED if resolved else ClearReason.STALE
                cleared.append(Cleared(alert=alert, reason=reason))
                del self._active[key]
                self._last_true_s.pop(key, None)
                self._last_false_s.pop(key, None)
        return cleared

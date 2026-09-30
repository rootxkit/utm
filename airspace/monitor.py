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

Every message carries `ts`, the clock of whoever captured it: on the relay
path the ground PC's `recv_utc_ns` (relay-v1 §9: it may be wrong, drifting
or stepped); on the Remote ID path the Gateway's own receive time, since the
broadcast's `seconds_after_hour` is decoded but not yet carried, so a
Remote ID position is stamped when it reached the Gateway, not when the
aircraft measured it. `ts` is put on the monitor's clock with the source's
estimated offset (`airspace/clock.py`), and a track is placed at that
instant. A pair's CPA is computed at the later of the two capture times with
the older track advanced along its velocity (`cpa.advance`): a neighbour's
5 s old sample, used as if current, is 75 m wrong at 15 m/s against a 60 m
threshold. A neighbour older than `neighbour_max_age_s` is not advanced at
all, and the pair is not evaluated by that message: neither refreshed nor
shown clear, since silence is not evidence.

A message delivered more than `live_max_age_s` later than its source's
usual delay is a replayed backlog, and is counted and not evaluated: it
must not raise an alert about where an aircraft was minutes ago. A clock
that is merely wrong is not a backlog: skew alone never costs an alert. A
message older than the sample its own source already gave for the aircraft
is ignored as out of order; another source's sample is never compared,
since two sources' clocks agree only to their offsets.

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
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID

from airspace.clock import SourceClocks
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


def source_of(message: dict[str, Any]) -> str:
    """Whose clock `ts` came from: the ground station (`station_id`) or, for
    Remote ID, the receiver, which the Gateway also puts in `station_id`."""
    for name in ("station_id", "source"):
        value = message.get(name)
        if value is not None and str(value):
            return str(value)
    return "unknown"


def captured_at_s(message: dict[str, Any]) -> float | None:
    """The message's capture time as epoch seconds, from its ISO 8601 `ts`,
    on the source's clock (see the module docstring for what that is on
    each path); None when the message carries no `ts`.

    Both Gateway producers always send one (`gateway/publisher.py`,
    `gateway/remote_id.py`). A message without it is still evaluated, at
    its arrival time, and counted: a missed alert costs more than a
    position a second or two out of place.

    Raises ValueError for a `ts` that is not a timestamp.
    """
    ts = message.get("ts")
    if ts is None:
        return None
    if not isinstance(ts, str):
        raise ValueError(f"ts is not a timestamp: {ts!r}")
    moment = datetime.fromisoformat(ts)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.timestamp()


def track_from_telemetry(
    message: dict[str, Any], *, arrived_at_s: float
) -> Track | None:
    """A Track, or None if the message cannot place the aircraft in 3-D.
    A message without `ts` is placed at `arrived_at_s`.

    Raises ValueError for a message that has the fields but cannot be used:
    a `ts` that is not a timestamp, or a number that is not finite (an
    `inf` latitude would reach `math.floor` in the neighbour grid and
    overflow there, S-12).
    """
    needed = ("lat_deg", "lon_deg", "alt_amsl_m", "vx_ms", "vy_ms", "vz_ms")
    if any(message.get(name) is None for name in needed):
        return None
    values = {name: float(message[name]) for name in needed}
    for name, value in values.items():
        if not math.isfinite(value):
            raise ValueError(f"{name} is not finite: {value}")
    captured = captured_at_s(message)
    return Track(
        drone_id=UUID(str(message["drone_id"])),
        lat_deg=values["lat_deg"],
        lon_deg=values["lon_deg"],
        alt_amsl_m=values["alt_amsl_m"],
        vn_ms=values["vx_ms"],
        ve_ms=values["vy_ms"],
        vd_ms=values["vz_ms"],
        captured_at_s=arrived_at_s if captured is None else captured,
        source=source_of(message),
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
    clock_relax_s_per_s: float = 0.1

    index: NeighbourIndex = field(init=False)
    clocks: SourceClocks = field(init=False)
    # Messages not evaluated: a replayed backlog, or a sample older than the
    # one its source already gave. Counted so a backlog shows in the log,
    # and in the service's periodic status line, rather than in the silence.
    rejected_backlog: int = field(default=0, init=False)
    rejected_out_of_order: int = field(default=0, init=False)
    # Messages evaluated at their arrival time because they carried no
    # `ts`. No Gateway producer omits it, so a count here is a producer bug.
    without_capture_time: int = field(default=0, init=False)
    _rejected_logged: set[UUID] = field(default_factory=set, init=False)
    _without_ts_logged: set[UUID] = field(default_factory=set, init=False)
    # Alert keys the current message could not judge (B2): a pair whose
    # neighbour sample is too old to advance, or one whose CPA raised.
    _unevaluated_keys: set[str] = field(default_factory=set, init=False)
    # Checks that raised instead of answering (S-12). Each is logged with its
    # traceback; the count is here so a test, or a health report, can see it.
    check_failures: int = field(default=0, init=False)
    _labels: dict[UUID, str | None] = field(default_factory=dict, init=False)
    _last_seen_s: dict[UUID, float] = field(default_factory=dict, init=False)
    # The latest capture time each source gave for each aircraft, so a
    # sample is judged out of order only against its own source's.
    _last_by_source_s: dict[tuple[UUID, str], float] = field(
        default_factory=dict, init=False
    )
    _active: dict[str, Alert] = field(default_factory=dict, init=False)
    # When each active alert's condition was last seen true, and last seen
    # false by a message that evaluated it.
    _last_true_s: dict[str, float] = field(default_factory=dict, init=False)
    _last_false_s: dict[str, float] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        self.index = NeighbourIndex(radius_m=self.policy.neighbour_radius_m)
        self.clocks = SourceClocks(relax_s_per_s=self.clock_relax_s_per_s)

    @property
    def active(self) -> list[Alert]:
        return list(self._active.values())

    @property
    def rejected(self) -> int:
        return self.rejected_backlog + self.rejected_out_of_order

    @property
    def tracked(self) -> int:
        return len(self._last_seen_s)

    def update_policy(self, policy: SeparationPolicy) -> bool:
        """Take a re-read policy (S-13). Logs what changed and returns whether
        anything did. A new neighbour radius rebuilds the index, since the
        grid's cell size comes from it."""
        if policy == self.policy:
            return False
        _log.info(
            "separation policy changed",
            extra={"before": asdict(self.policy), "after": asdict(policy)},
        )
        if policy.neighbour_radius_m != self.policy.neighbour_radius_m:
            index = NeighbourIndex(radius_m=policy.neighbour_radius_m)
            for track in self.index.tracks():
                index.upsert(track)
            self.index = index
        self.policy = policy
        return True

    def observe(
        self, message: dict[str, Any], *, now_s: float, height_available: bool = True
    ) -> Change:
        """Take one telemetry message; return the alerts it raised or cleared.

        `now_s` is the monitor's wall clock. The message's `ts` is put on
        that clock with its source's offset and the message is evaluated at
        that instant; `now_s` itself only decides whether the message is a
        backlog, and what has gone stale. With `height_available` False the
        caller could not get the terrain under the aircraft (S-13): the
        height limit is not evaluated by this message, the other checks are.
        """
        drone_id = UUID(str(message["drone_id"]))
        self._labels[drone_id] = message.get("label")
        track = None
        if _flying(message):
            track = track_from_telemetry(message, arrived_at_s=now_s)
            if track is not None and captured_at_s(message) is None:
                self._note_missing_ts(track)

        raised: list[Alert] = []
        if track is None:
            self.index.remove(drone_id)
            self._last_seen_s.pop(drone_id, None)
            for key in [k for k in self._last_by_source_s if k[0] == drone_id]:
                del self._last_by_source_s[key]
        else:
            delay = self.clocks.observe(
                track.source, wall_s=now_s, ts_s=track.captured_at_s
            )
            track = replace(track, captured_at_s=track.captured_at_s + delay.offset_s)
            if not self._rejects(track, excess_s=delay.excess_s):
                raised = self._evaluate(track, height_available=height_available)

        cleared = self._expire(now_s)
        return Change(raised=raised, cleared=cleared)

    def _evaluate(self, track: Track, *, height_available: bool) -> list[Alert]:
        at_s = track.captured_at_s
        drone_id = track.drone_id
        self.index.upsert(track)
        self._last_seen_s[drone_id] = at_s
        self._last_by_source_s[(drone_id, track.source)] = at_s
        self._unevaluated_keys.clear()
        raised: list[Alert] = []
        # Each check on its own: a missing terrain tile must not silence
        # the conflict and zone alerts already found (S-12).
        not_evaluated: set[AlertKind] = set()
        for kind, check in (
            (AlertKind.CONFLICT, self._check_conflicts),
            (AlertKind.ZONE, self._check_zones),
            (AlertKind.HEIGHT, self._check_height),
        ):
            if kind is AlertKind.HEIGHT and not height_available:
                not_evaluated.add(kind)
                continue
            found = self._guarded(kind, check, track, at_s)
            if found is None:
                not_evaluated.add(kind)
            else:
                raised.extend(found)
        # Every active alert this aircraft is part of was just evaluated.
        # The ones not refreshed are false as of this message. A check that
        # failed, or a pair this message could not judge, evaluated nothing:
        # those alerts are neither refreshed nor shown false, so neither an
        # error nor a silent neighbour can clear one.
        for key, alert in self._active.items():
            if (
                drone_id in alert.drone_ids
                and alert.kind not in not_evaluated
                and key not in self._unevaluated_keys
                and self._last_true_s[key] != at_s
            ):
                self._last_false_s[key] = at_s
        return raised

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

    def _note_missing_ts(self, track: Track) -> None:
        self.without_capture_time += 1
        if track.drone_id not in self._without_ts_logged:
            self._without_ts_logged.add(track.drone_id)
            _log.warning(
                "telemetry has no ts; evaluated at its arrival time",
                extra={
                    "drone_id": str(track.drone_id),
                    "station_id": track.source,
                    "without_capture_time": self.without_capture_time,
                },
            )

    def _rejects(self, track: Track, *, excess_s: float) -> bool:
        """Whether the sample must not be evaluated: a backlog (delivered
        `excess_s` later than its source's usual delay), or older than the
        sample its own source already gave. Counted, and logged once per
        aircraft per run of rejections, so a backlog of thousands is one
        line, not thousands; the totals go in the service's status line.
        """
        latest_s = self._last_by_source_s.get((track.drone_id, track.source))
        if excess_s > self.live_max_age_s:
            reason = "backlog"
            self.rejected_backlog += 1
        elif latest_s is not None and track.captured_at_s < latest_s:
            reason = "older than the sample held"
            self.rejected_out_of_order += 1
        else:
            self._rejected_logged.discard(track.drone_id)
            return False
        if track.drone_id not in self._rejected_logged:
            self._rejected_logged.add(track.drone_id)
            _log.warning(
                "telemetry not evaluated",
                extra={
                    "drone_id": str(track.drone_id),
                    "station_id": track.source,
                    "reason": reason,
                    "excess_delay_s": round(excess_s, 1),
                    "clock_offset_s": round(
                        self.clocks.offset_s(track.source) or 0.0, 1
                    ),
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
            key = conflict_key(track.drone_id, other.drone_id)
            if (
                abs(track.captured_at_s - other.captured_at_s)
                > self.neighbour_max_age_s
            ):
                # Too old to advance along a straight line with any meaning,
                # and too old to say the pair is clear: not judged (B2).
                self._unevaluated_keys.add(key)
                continue
            # One neighbour's arithmetic failing must not lose the others.
            try:
                approach = closest_approach(track, other)
                conflict = self.policy.is_conflict(approach)
            except Exception:
                self._unevaluated_keys.add(key)
                self.check_failures += 1
                _log.exception(
                    "conflict check failed for a pair; the pair is not judged",
                    extra={
                        "drone_id": str(track.drone_id),
                        "other_drone_id": str(other.drone_id),
                        "check_failures": self.check_failures,
                    },
                )
                continue
            if not conflict:
                continue
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

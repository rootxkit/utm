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

## Raise once, clear with hysteresis

An alert is raised once per condition, not once per tick, and cleared only
once telemetry has *shown* the condition false for longer than
`clear_after_s`. Without the delay an aircraft sitting on a zone boundary, or
a pair hovering near the threshold, would raise and clear every second - the
kind of alert people learn to scroll past.

Silence is not evidence. A pair whose telemetry stops is not shown to be
clear of each other, so its alert stays until the aircraft are dropped as
stale, and is cleared then as "no longer tracked", not as "resolved".

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

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID

from airspace.cpa import Approach, SeparationPolicy, Track, closest_approach
from airspace.neighbours import NeighbourIndex
from airspace.zones import Zone, ZoneType
from common.terrain import Elevation


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


@dataclass(frozen=True, slots=True)
class Change:
    raised: list[Alert]
    cleared: list[Alert]


def track_from_telemetry(message: dict[str, Any]) -> Track | None:
    """A Track, or None if the message cannot place the aircraft in 3-D."""
    needed = ("lat_deg", "lon_deg", "alt_amsl_m", "vx_ms", "vy_ms", "vz_ms")
    if any(message.get(name) is None for name in needed):
        return None
    return Track(
        drone_id=UUID(str(message["drone_id"])),
        lat_deg=float(message["lat_deg"]),
        lon_deg=float(message["lon_deg"]),
        alt_amsl_m=float(message["alt_amsl_m"]),
        vn_ms=float(message["vx_ms"]),
        ve_ms=float(message["vy_ms"]),
        vd_ms=float(message["vz_ms"]),
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

    index: NeighbourIndex = field(init=False)
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
        """Take one telemetry message; return the alerts it raised or cleared."""
        drone_id = UUID(str(message["drone_id"]))
        self._labels[drone_id] = message.get("label")
        track = track_from_telemetry(message) if _flying(message) else None

        raised: list[Alert] = []
        if track is None:
            self.index.remove(drone_id)
            self._last_seen_s.pop(drone_id, None)
        else:
            self.index.upsert(track)
            self._last_seen_s[drone_id] = now_s
            raised.extend(self._check_conflicts(track, now_s))
            raised.extend(self._check_zones(track, now_s))
            raised.extend(self._check_height(track, now_s))
            # Every active alert this aircraft is part of was just evaluated.
            # The ones not refreshed are false as of this message.
            for key, alert in self._active.items():
                if drone_id in alert.drone_ids and self._last_true_s[key] != now_s:
                    self._last_false_s[key] = now_s

        cleared = self._expire(now_s)
        return Change(raised=raised, cleared=cleared)

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

    def _expire(self, now_s: float) -> list[Alert]:
        for drone_id, seen_s in list(self._last_seen_s.items()):
            if now_s - seen_s > self.stale_after_s:
                self.index.remove(drone_id)
                del self._last_seen_s[drone_id]

        tracked = set(self._last_seen_s)
        cleared: list[Alert] = []
        for key, alert in list(self._active.items()):
            gone = not all(drone_id in tracked for drone_id in alert.drone_ids)
            shown_false_for_s = (
                self._last_false_s.get(key, 0.0) - self._last_true_s[key]
            )
            resolved = shown_false_for_s > self.clear_after_s
            if gone or resolved:
                cleared.append(alert)
                del self._active[key]
                self._last_true_s.pop(key, None)
                self._last_false_s.pop(key, None)
        return cleared

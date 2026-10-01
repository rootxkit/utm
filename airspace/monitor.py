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

## Time is the Gateway's receive time, not the arrival time (S-11)

Every message carries two times (`gateway/README.md`). `ts` is the clock of
whoever captured it: on the relay path the ground PC's `recv_utc_ns`
(relay-v1 §9: it may be wrong, drifting or stepped); on the Remote ID path
the broadcast's own capture time (S-27), which the Gateway also uses for
`captured_at` when it is plausible. `rx_ts` is when the
Gateway received the batch, on the Gateway's clock: one clock for every
station. `captured_at` is where the Gateway placed the row on that clock:
`rx_ts` less how far behind its batch's newest record it was captured, so a
draining relay's 8 s frame does not land as one instant. A track is placed
at `captured_at`, so two aircraft on two stations are compared at one
instant without guessing either station's skew, and a pair's CPA is
computed at the later of the two with the older track advanced along its
velocity (`cpa.advance`): a neighbour's 5 s old sample, used as if current,
is 75 m wrong at 15 m/s against a 60 m threshold. A neighbour older than
`neighbour_max_age_s` is not advanced at all, and the pair is not evaluated
by that message: neither refreshed nor shown clear, since silence is not
evidence.

Whether a message is a replayed backlog is the Gateway's verdict, not an
estimate: `backlog` is true for records that were queued on the relay before
the session that delivered them (`newest_seq_held`, relay-v1 §5), and for
records that arrived while the relay was still draining a queue. Those are
counted and not evaluated: they must not raise an alert about where an
aircraft was minutes ago. Nothing here infers a backlog from `ts`, so a
station clock that is wrong by any amount, or a Gateway that is behind
(ADR-002), costs no alerts: the latter yields late alerts, placed at
`rx_ts`. `live_max_age_s` applies only to `wall - rx_ts`, the delay from
the Gateway to here, which is ours to control. A message without `rx_ts`
(an older Gateway, a test) is placed at its arrival time and counted.

`ts` is used for one thing: within one source, a sample older than the last
one that source gave, delivered no later than it, is out of order and
ignored. Another source's sample is never compared, since two stations'
clocks agree only by accident.

## A pressure altitude is not a vertical position (S-33)

A Remote ID broadcast's `alt_amsl_m` may be its pressure altitude
(`alt_source: "pressure"`): referenced to 1013.25 hPa, not the local QNH,
about 8 m off per hPa, some 160 m on a 20 hPa day against a 20 m vertical
minimum. Compared as AMSL it could hide a conflict or invent one. Such a
track's vertical position is unknown: a pair with one is judged on the
horizontal criteria alone, as though the vertical minimum were not met, and
the alert says `vertical_separation_known: false` with `d_alt_at_cpa_m`
null. A zone's altitude band is widened by `pressure_uncertainty_m` (250 m)
each way for it, and the height limit is exceeded only if it still is with
that margin taken off; either alert is then a warning, whatever the zone,
and says `vertical_known: false`. Zones without altitude limits are judged
as for anyone. Each such message is counted (`vertical_unknown`, in the
status line) and the first of a run per aircraft is logged.

Two Remote ID tracks with the same transmitter address, one of them
unidentified, are one radio under two ids (S-32) and are never paired. Two
identified tracks on one address are judged like any pair.

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
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
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


def source_of(message: dict[str, Any]) -> str:
    """Whose clock `ts` came from: the ground station (`station_id`) or, for
    Remote ID, the receiver, which the Gateway also puts in `station_id`."""
    for name in ("station_id", "source"):
        value = message.get(name)
        if value is not None and str(value):
            return str(value)
    return "unknown"


def time_field_s(message: dict[str, Any], name: str) -> float | None:
    """An ISO 8601 time field as epoch seconds; None when absent or null.

    Raises ValueError for a value that is not a timestamp.
    """
    value = message.get(name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{name} is not a timestamp: {value!r}")
    moment = datetime.fromisoformat(value)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.timestamp()


def captured_at_s(message: dict[str, Any]) -> float | None:
    """The station's capture time (`ts`) as epoch seconds; None when the
    message carries none. See the module docstring for whose clock it is."""
    return time_field_s(message, "ts")


def received_at_s(message: dict[str, Any]) -> float | None:
    """The Gateway's receive time (`rx_ts`) as epoch seconds; None when the
    message carries none (an older Gateway, or a test)."""
    return time_field_s(message, "rx_ts")


def placed_at_s(message: dict[str, Any]) -> float | None:
    """Where the Gateway placed the row in time (`captured_at`: `rx_ts` less
    how far behind its batch's newest record it was captured, so rows from
    one large frame are not all "now"); `rx_ts` when the message has no
    finer placement; None when it has neither."""
    placed = time_field_s(message, "captured_at")
    return received_at_s(message) if placed is None else placed


def is_backlog(message: dict[str, Any]) -> bool:
    """The Gateway's verdict that the record was queued before the session
    that delivered it (`gateway/README.md`). Absent means live."""
    return message.get("backlog") is True


def track_from_telemetry(
    message: dict[str, Any], *, arrived_at_s: float
) -> Track | None:
    """A Track, or None if the message cannot place the aircraft in 3-D.
    Placed at `rx_ts`, or at `arrived_at_s` when the message has none.

    Raises ValueError for a message that has the fields but cannot be used:
    a time that is not a timestamp, or a number that is not finite (an
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
    received = placed_at_s(message)
    return Track(
        drone_id=UUID(str(message["drone_id"])),
        lat_deg=values["lat_deg"],
        lon_deg=values["lon_deg"],
        alt_amsl_m=values["alt_amsl_m"],
        vn_ms=values["vx_ms"],
        ve_ms=values["vy_ms"],
        vd_ms=values["vz_ms"],
        captured_at_s=arrived_at_s if received is None else received,
        source=source_of(message),
        source_ts_s=captured_at_s(message),
        transmitter=transmitter_of(message),
        identified=identified_of(message),
        vertical_known=vertical_known(message),
    )


# S-33; matches `airspace.config.AirspaceSettings.pressure_uncertainty_m`.
DEFAULT_PRESSURE_UNCERTAINTY_M = 250.0

# The Gateway's `alt_source` for an AMSL altitude taken from a Remote ID
# broadcast's pressure altitude (gateway/remote_id.py, S-33).
ALT_SOURCE_PRESSURE = "pressure"


def vertical_known(message: dict[str, Any]) -> bool:
    """Whether `alt_amsl_m` places the aircraft vertically (S-33).

    A pressure altitude does not: see `Track.vertical_known`.
    """
    return message.get("alt_source") != ALT_SOURCE_PRESSURE


def identified_of(message: dict[str, Any]) -> bool | None:
    """A Remote ID broadcast's `remote_id.identified`; None otherwise.

    Only an explicit False counts as unidentified: a broadcast without the
    field is judged as identified, the side on which nothing is skipped.
    """
    if message.get("source") != "remote_id":
        return None
    remote_id = message.get("remote_id")
    if not isinstance(remote_id, dict):
        return None
    return remote_id.get("identified") is not False


def same_radio(a: Track, b: Track) -> bool:
    """Two tracks that are one transmitter under two ids (S-32).

    The Gateway publishes a transmitter without a fresh identity as an
    unidentified track keyed by its address, and under its serial once it
    has one: the same address, one side unidentified. Two identified tracks
    on one address are two claims, a spoofer on another's address among
    them, and are judged like any pair.
    """
    return (
        a.transmitter is not None
        and a.transmitter == b.transmitter
        and (a.identified is False or b.identified is False)
    )


def transmitter_of(message: dict[str, Any]) -> str | None:
    """The transmitter address of a Remote ID broadcast; None otherwise."""
    if message.get("source") != "remote_id":
        return None
    remote_id = message.get("remote_id")
    if not isinstance(remote_id, dict):
        return None
    transmitter = remote_id.get("transmitter")
    return transmitter if isinstance(transmitter, str) and transmitter else None


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
    source_state_max: int = 4096

    index: NeighbourIndex = field(init=False)
    # Messages not evaluated: the Gateway flagged a replayed backlog; the
    # Gateway-to-monitor delay exceeded `live_max_age_s`; or the sample is
    # older than the one its source already gave. Counted so each shows in
    # the log, and in the service's status line, rather than in the silence.
    rejected_backlog: int = field(default=0, init=False)
    rejected_late: int = field(default=0, init=False)
    rejected_out_of_order: int = field(default=0, init=False)
    # Messages evaluated at their arrival time because they carried no
    # `rx_ts`, and messages with no `ts` at all. Neither Gateway producer
    # omits either, so a count here is an older producer or a bug.
    without_receive_time: int = field(default=0, init=False)
    without_capture_time: int = field(default=0, init=False)
    _rejected_logged: set[UUID] = field(default_factory=set, init=False)
    _without_time_logged: set[UUID] = field(default_factory=set, init=False)
    # Alert keys the current message could not judge (B2): a pair whose
    # neighbour sample is too old to advance, or one whose CPA raised.
    _unevaluated_keys: set[str] = field(default_factory=set, init=False)
    # Checks that raised instead of answering (S-12). Each is logged with its
    # traceback; the count is here so a test, or a health report, can see it.
    check_failures: int = field(default=0, init=False)
    # Messages whose altitude is a pressure altitude (S-33): conflicts are
    # judged on the horizontal alone, and the height limit and zones with
    # altitude limits are not evaluated. Logged once per aircraft per run.
    vertical_unknown: int = field(default=0, init=False)
    # S-33. How far a pressure altitude may be from AMSL: about 8 m per hPa
    # between the local QNH and 1013.25 hPa. The default covers 30 hPa.
    pressure_uncertainty_m: float = DEFAULT_PRESSURE_UNCERTAINTY_M
    _vertical_unknown_logged: set[UUID] = field(default_factory=set, init=False)
    _labels: dict[UUID, str | None] = field(default_factory=dict, init=False)
    _last_seen_s: dict[UUID, float] = field(default_factory=dict, init=False)
    # The latest (`ts`, `rx_ts`) each source gave for each aircraft, so a
    # sample is judged out of order only against its own source's. Bounded
    # to `source_state_max` entries, least recently seen out, so a stream
    # of new station ids cannot grow it without limit.
    _last_by_source_s: OrderedDict[tuple[UUID, str], tuple[float, float]] = field(
        default_factory=OrderedDict, init=False
    )
    _active: dict[str, Alert] = field(default_factory=dict, init=False)
    # When each active alert's condition was last seen true, and last seen
    # false by a message that evaluated it.
    _last_true_s: dict[str, float] = field(default_factory=dict, init=False)
    _last_false_s: dict[str, float] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        if self.source_state_max < 1:
            raise ValueError("source_state_max must be at least 1")
        self.index = NeighbourIndex(radius_m=self.policy.neighbour_radius_m)

    @property
    def active(self) -> list[Alert]:
        return list(self._active.values())

    @property
    def rejected(self) -> int:
        return self.rejected_backlog + self.rejected_late + self.rejected_out_of_order

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

        `now_s` is the monitor's wall clock. The message is evaluated at its
        `rx_ts` (the Gateway's clock); `now_s` decides only whether it is
        late, and what has gone stale. A message the Gateway flagged as
        backlog is counted and not evaluated. With `height_available` False
        the caller could not get the terrain under the aircraft (S-13): the
        height limit is not evaluated by this message, the other checks are.
        """
        drone_id = UUID(str(message["drone_id"]))
        self._labels[drone_id] = message.get("label")
        track = (
            track_from_telemetry(message, arrived_at_s=now_s)
            if _flying(message)
            else None
        )

        raised: list[Alert] = []
        if track is None:
            self.index.remove(drone_id)
            self._last_seen_s.pop(drone_id, None)
            for key in [k for k in self._last_by_source_s if k[0] == drone_id]:
                del self._last_by_source_s[key]
        elif not self._rejects(track, message, now_s=now_s):
            self._note_missing_times(track, message)
            raised = self._evaluate(track, height_available=height_available)

        cleared = self._expire(now_s)
        return Change(raised=raised, cleared=cleared)

    def _evaluate(self, track: Track, *, height_available: bool) -> list[Alert]:
        at_s = track.captured_at_s
        drone_id = track.drone_id
        self.index.upsert(track)
        self._last_seen_s[drone_id] = at_s
        if track.source_ts_s is not None:
            key_by_source = (drone_id, track.source)
            self._last_by_source_s[key_by_source] = (track.source_ts_s, at_s)
            self._last_by_source_s.move_to_end(key_by_source)
            while len(self._last_by_source_s) > self.source_state_max:
                self._last_by_source_s.popitem(last=False)
        self._unevaluated_keys.clear()
        raised: list[Alert] = []
        # Each check on its own: a missing terrain tile must not silence
        # the conflict and zone alerts already found (S-12).
        not_evaluated: set[AlertKind] = set()
        self._note_vertical(track)
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

    def _note_vertical(self, track: Track) -> None:
        """Count a message whose vertical position is unknown (S-33), and
        log once per aircraft until it reports a known one again."""
        if track.vertical_known:
            self._vertical_unknown_logged.discard(track.drone_id)
            return
        self.vertical_unknown += 1
        if track.drone_id not in self._vertical_unknown_logged:
            self._vertical_unknown_logged.add(track.drone_id)
            _log.warning(
                "altitude is a pressure altitude; conflicts judged on the "
                "horizontal alone, zone bands and the height limit with the "
                "pressure uncertainty margin",
                extra={
                    "drone_id": str(track.drone_id),
                    "station_id": track.source,
                    "vertical_unknown": self.vertical_unknown,
                    "pressure_uncertainty_m": self.pressure_uncertainty_m,
                },
            )

    def _note_missing_times(self, track: Track, message: dict[str, Any]) -> None:
        """Count a message without `rx_ts` (placed at its arrival time) or
        without `ts` (not ordered within its source); log once per aircraft.
        Neither Gateway producer omits either, so a count is a producer bug,
        and a missed alert would cost more than a position a second out."""
        missing = []
        if "rx_ts" not in message or message.get("rx_ts") is None:
            self.without_receive_time += 1
            missing.append("rx_ts")
        if track.source_ts_s is None:
            self.without_capture_time += 1
            missing.append("ts")
        if missing and track.drone_id not in self._without_time_logged:
            self._without_time_logged.add(track.drone_id)
            _log.warning(
                "telemetry is missing a time field; evaluated at its arrival time",
                extra={
                    "drone_id": str(track.drone_id),
                    "station_id": track.source,
                    "missing": missing,
                    "without_receive_time": self.without_receive_time,
                    "without_capture_time": self.without_capture_time,
                },
            )

    def _rejects(self, track: Track, message: dict[str, Any], *, now_s: float) -> bool:
        """Whether the sample must not be evaluated: the Gateway flagged it
        as backlog; it reached here more than `live_max_age_s` after the
        Gateway received it; or it is older, on its station's clock, than
        the sample that station already gave and was not received later
        (out of order). Counted, and logged once per aircraft per run of
        rejections, so a backlog of thousands is one line, not thousands;
        the totals go in the service's status line.
        """
        # Lateness is judged on the batch's receive time, not on where the
        # row was placed within the batch.
        received = received_at_s(message)
        delay_s = now_s - (track.captured_at_s if received is None else received)
        last = self._last_by_source_s.get((track.drone_id, track.source))
        if is_backlog(message):
            reason = "backlog"
            self.rejected_backlog += 1
        elif delay_s > self.live_max_age_s:
            reason = "late"
            self.rejected_late += 1
        elif (
            last is not None
            and track.source_ts_s is not None
            and track.source_ts_s < last[0]
            and track.captured_at_s <= last[1]
        ):
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
                    "gateway_delay_s": round(delay_s, 1),
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
            if same_radio(track, other):
                # One radio under two ids: not a pair, so not judged.
                continue
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
                # None, never a number, when it is not known (S-33).
                "d_alt_at_cpa_m": (
                    round(approach.d_alt_at_cpa_m, 1)
                    if approach.vertical_known
                    else None
                ),
                "d_horizontal_now_m": round(approach.d_horizontal_now_m, 1),
                "vertical_separation_known": approach.vertical_known,
            },
        )

    def _check_zones(self, track: Track, now_s: float) -> list[Alert]:
        raised: list[Alert] = []
        for zone in self.zones:
            key = zone_key(track.drone_id, zone)
            # S-33: with the altitude a pressure altitude, the band is
            # widened by its uncertainty, conservatively, and the alert is
            # a warning that says the altitude is approximate.
            approximate = not track.vertical_known and zone.has_altitude_limits
            margin_m = self.pressure_uncertainty_m if approximate else 0.0
            if not zone.contains(
                track.lat_deg, track.lon_deg, track.alt_amsl_m, margin_m=margin_m
            ):
                continue
            detail: dict[str, Any] = {
                "zone_id": str(zone.zone_id),
                "zone_name": zone.name,
                "zone_type": zone.type.value,
                "alt_amsl_m": round(track.alt_amsl_m, 1),
            }
            if approximate:
                detail["vertical_known"] = False
                detail["pressure_uncertainty_m"] = margin_m
            alert = Alert(
                key=key,
                kind=AlertKind.ZONE,
                severity=(
                    Severity.CRITICAL
                    if zone.type is ZoneType.NO_FLY and not approximate
                    else Severity.WARNING
                ),
                drone_ids=(track.drone_id,),
                labels=(self._labels.get(track.drone_id),),
                detail=detail,
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
        # S-33: a pressure altitude is over the limit only if it still is
        # with its whole uncertainty taken off.
        margin_m = 0.0 if track.vertical_known else self.pressure_uncertainty_m
        if height_agl_m - margin_m <= self.max_height_agl_m:
            return []
        key = height_key(track.drone_id)
        detail: dict[str, Any] = {
            "height_agl_m": round(height_agl_m, 1),
            "max_height_agl_m": self.max_height_agl_m,
            "alt_amsl_m": round(track.alt_amsl_m, 1),
            "ground_elevation_m": round(ground.elevation_m, 1),
            "dataset": ground.dataset,
        }
        if not track.vertical_known:
            detail["vertical_known"] = False
            detail["pressure_uncertainty_m"] = margin_m
        alert = Alert(
            key=key,
            kind=AlertKind.HEIGHT,
            severity=Severity.WARNING,
            drone_ids=(track.drone_id,),
            labels=(self._labels.get(track.drone_id),),
            detail=detail,
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

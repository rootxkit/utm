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
null. A zone with an altitude band raises as usual when the indicated
altitude is inside the band, a no-fly zone at critical; inside the band
widened by `pressure_uncertainty_m` (250 m) each way only, it raises a
warning. The height limit is judged on the indicated height. Each of those
alerts says `vertical_known: false`, with the margin. Zones without
altitude limits are judged as for anyone. An active alert whose severity
changes is raised again under its key, never changed silently in place. Each such message is counted (`vertical_unknown`, in the
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

## A source switched off is not judged (U-15)

Each source can be switched off without a deploy (`common/sources.py`). A
message from a disabled source is counted (`rejected_source_disabled`) and
not evaluated, whatever it says. An aircraft whose track came from a source
that is switched off is dropped at once, not left to go stale, and every
alert it is part of is cleared with the reason `source_disabled`: neither
`resolved`, since nothing showed the condition false, nor `stale`, since
the aircraft was not lost but deliberately put out of the picture. The
same aircraft heard through a source still on (a relay aircraft that also
broadcasts Remote ID) is tracked from that source's next message.

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

## Geographical zones (P5-15, U-03)

Zones are ED-269 zones (`airspace/zones.py`). An aircraft is in one when the
zone applies at the track's placed time (`captured_at`, in UTC), the
aircraft is inside it horizontally, and every vertical limit that can be
judged includes it. What it raises depends on the zone's restriction:

| restriction | alert |
|---|---|
| PROHIBITED | critical |
| REQ_AUTHORISATION | warning; none for an aircraft U-05 authorised (the `authorisations` seam, empty until U-05) |
| CONDITIONAL | info or warning, as `airspace_policy.conditional_zone_severity` says |
| NO_RESTRICTION | none |

A lower AGL limit at or below the ground is met by any airborne aircraft and
needs nothing. An AGL ceiling, or an AGL floor above the ground, needs the
DEM; a limit above the ellipsoid needs the geoid. Where that is missing for
a zone the aircraft is horizontally inside:

- a PROHIBITED or REQ_AUTHORISATION zone whose only unjudged limit is above
  the ground raises a **warning** saying `vertical_known: false` and
  `limit_not_judged: true` (counted in `zone_limits_not_judged`): a false
  warning beats a missed critical. While any PROHIBITED zone needs terrain
  and none is configured, the start-up log and every status line are errors;
- otherwise (a CONDITIONAL zone, or a missing geoid) the zone is **not
  evaluated**: no alert, an active one is neither refreshed nor cleared,
  counted in `zone_checks_not_evaluated` and logged once per zone and
  aircraft until it can be judged again.

A judged limit that excludes the aircraft decides either way.

On a pressure altitude (S-33, below) every judged limit, in whatever
reference, is widened by `pressure_uncertainty_m`: inside the limits as
indicated keeps the zone's severity, inside the widened ones only is a
warning, and the alert says `vertical_known: false` and `within_band`. A
change of severity is raised again under the same key (`_refresh`).

## Identification (U-02)

Every track carries the registry's verdict on who it is
(`identification`, `gateway/identification.py`). Two alerts come of it:

- **`identification_mismatch`**, per aircraft, at
  `mismatch_severity` (warning): a registered serial broadcast with an
  operator registration number that is not its owner's, or one of our
  serials heard away from our aircraft (S-10). Raised while the track says
  `mismatch: true`, cleared with the usual hysteresis once it says false.
- **`identification`**, per aircraft and zone, at `identification_severity`
  (critical): an `unidentified` or `unknown_operator` aircraft inside a
  PROHIBITED or REQ_AUTHORISATION zone, beside that zone's own alert. It is
  **the seam where U-12 opens an incident**: the service hands every raise
  and clear of this kind to its `incidents` sink
  (`airspace/service.py`), which until U-12 counts and logs them.

Every zone alert also carries the aircraft's `identification` in its
detail. Like every other check, these judge only flying aircraft with a
position the monitor can place.

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
from airspace.ed269 import Restriction, VerticalReference
from airspace.neighbours import NeighbourIndex
from airspace.zones import Undulation, Zone, needs_terrain
from common import get_logger
from common.sources import source_of_telemetry
from common.terrain import Elevation, TerrainFileError

_log = get_logger(__name__)


class Severity(StrEnum):
    CRITICAL = "critical"
    WARNING = "warning"
    # A CONDITIONAL zone, when policy says so (U-03).
    INFO = "info"


class AlertKind(StrEnum):
    CONFLICT = "conflict"
    ZONE = "zone"
    HEIGHT = "height"
    # U-02: an unidentified or unknown-operator aircraft in a zone that
    # needs an identity; the incident seam (U-12).
    IDENTIFICATION = "identification"
    # U-02: a registered serial broadcast with another operator's number.
    IDENTIFICATION_MISMATCH = "identification_mismatch"


# `gateway.identification.INCIDENT_STATUSES`, as the bus carries them: the
# statuses that, inside a zone below, raise an `identification` alert. The
# monitor does not import the Gateway; a test pins the two together.
INCIDENT_STATUSES = frozenset({"unidentified", "unknown_operator"})
# Zones in which an aircraft must be identifiable (U-02).
_IDENTITY_ZONES = frozenset({Restriction.PROHIBITED, Restriction.REQ_AUTHORISATION})


class GroundElevation(Protocol):
    """`common.terrain.Terrain`, or anything that answers like it."""

    def elevation(self, lat_deg: float, lon_deg: float) -> Elevation | None: ...


@dataclass(frozen=True, slots=True)
class VerticalVerdict:
    """A zone's limits against one aircraft (`AirspaceMonitor._judge_vertical`)."""

    # A judged limit excludes the aircraft, even widened by the margin.
    outside: bool
    # Every judged limit includes the aircraft as indicated, not only widened.
    within_band: bool
    # References whose height is unknown here (no DEM, no geoid).
    not_judged: frozenset[VerticalReference]
    # The aircraft's height in each reference judged (None where unknown).
    heights: dict[VerticalReference, float | None]
    # The pressure-altitude margin applied (S-33); 0 when none was.
    margin_m: float


# Zones that warn rather than stay silent when an AGL limit cannot be judged
# for want of terrain: a false warning beats a missed critical (U-03 review).
_WARN_UNJUDGED = frozenset({Restriction.PROHIBITED, Restriction.REQ_AUTHORISATION})


class Authorisations(Protocol):
    """Whether an aircraft is authorised to be in a REQ_AUTHORISATION zone at
    a time. The seam for U-05's flight authorisations: until U-05 there is
    no implementation, and every such incursion is a warning."""

    def authorised(self, drone_id: UUID, zone: Zone, at: datetime) -> bool: ...


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
    # An aircraft involved came from a source that was switched off (U-15).
    SOURCE_DISABLED = "source_disabled"


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


def identification_key(drone_id: UUID, zone: Zone) -> str:
    return f"identification:{zone.zone_id}:{drone_id}"


def mismatch_key(drone_id: UUID) -> str:
    return f"identification_mismatch:{drone_id}"


def identification_of(message: dict[str, Any]) -> dict[str, Any] | None:
    """The track's `identification` (U-02); None when it carries none."""
    found = message.get("identification")
    return found if isinstance(found, dict) else None


def _limits_detail(
    zone: Zone, heights: dict[VerticalReference, float | None]
) -> dict[str, Any]:
    """The zone's limits in metres with their references, and the aircraft's
    height in each, for the operator reading the alert."""
    detail: dict[str, Any] = {}
    for name, limit in (("lower", zone.lower), ("upper", zone.upper)):
        if limit is not None:
            detail[f"{name}_limit_m"] = round(limit.value_m, 1)
            detail[f"{name}_reference"] = limit.reference.value
    names = {
        VerticalReference.AGL: "height_agl_m",
        VerticalReference.WGS84: "alt_hae_m",
    }
    for reference, height in heights.items():
        if reference in names and height is not None:
            detail[names[reference]] = round(height, 1)
    return detail


@dataclass
class AirspaceMonitor:
    policy: SeparationPolicy
    zones: list[Zone] = field(default_factory=list)
    # Both needed for the height limit; with either missing it is not checked.
    terrain: GroundElevation | None = None
    max_height_agl_m: float | None = None
    # For zone limits above the ellipsoid (WGS84); without it they are not
    # evaluated (U-03).
    geoid: Undulation | None = None
    # What a CONDITIONAL zone raises: airspace_policy.conditional_zone_severity.
    conditional_severity: Severity = Severity.WARNING
    # U-05's seam; see `Authorisations`.
    authorisations: Authorisations | None = None
    stale_after_s: float = 15.0
    clear_after_s: float = 3.0
    # S-11; the defaults match `airspace.config.AirspaceSettings`.
    live_max_age_s: float = 10.0
    neighbour_max_age_s: float = 10.0
    source_state_max: int = 4096
    # U-15. Whether a `(source_type, instance_id)` is switched on; None:
    # every source is (`common.sources.SourceControlFollower.enabled`).
    source_enabled: Callable[[str, str], bool] | None = None
    # U-02. What an `identification` and an `identification_mismatch` alert
    # raise (`airspace.config.AirspaceSettings`).
    identification_severity: Severity = Severity.CRITICAL
    mismatch_severity: Severity = Severity.WARNING

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
    # Zone checks that could not be judged: the aircraft was inside a zone
    # horizontally and a limit needed terrain or the geoid that was missing.
    zone_checks_not_evaluated: int = field(default=0, init=False)
    # Zone alerts raised at warning because an AGL limit could not be judged
    # (`limit_not_judged`), rather than left silent.
    zone_limits_not_judged: int = field(default=0, init=False)
    _zone_unevaluated_logged: set[str] = field(default_factory=set, init=False)
    # Whether the caller could read the terrain under this message (S-13).
    _height_available: bool = field(default=True, init=False)
    # Messages whose altitude is a pressure altitude (S-33): conflicts are
    # judged on the horizontal alone, and the height limit and zones with
    # altitude limits are not evaluated. Logged once per aircraft per run.
    vertical_unknown: int = field(default=0, init=False)
    # S-33. How far a pressure altitude may be from AMSL: about 8 m per hPa
    # between the local QNH and 1013.25 hPa. The default covers 30 hPa.
    pressure_uncertainty_m: float = DEFAULT_PRESSURE_UNCERTAINTY_M
    _vertical_unknown_logged: set[UUID] = field(default_factory=set, init=False)
    # U-15. Messages from a source switched off, not evaluated; and aircraft
    # dropped because their source was switched off.
    rejected_source_disabled: int = field(default=0, init=False)
    dropped_source_disabled: int = field(default=0, init=False)
    _source_disabled_logged: set[UUID] = field(default_factory=set, init=False)
    # The source each tracked aircraft's track came from.
    _track_source: dict[UUID, tuple[str, str]] = field(default_factory=dict, init=False)
    _labels: dict[UUID, str | None] = field(default_factory=dict, init=False)
    # U-02: each aircraft's latest `identification`.
    _identification: dict[UUID, dict[str, Any] | None] = field(
        default_factory=dict, init=False
    )
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
        source = source_of_telemetry(message)
        if not self._enabled(source):
            return self._refuse_disabled(drone_id, source, now_s=now_s)
        self._source_disabled_logged.discard(drone_id)
        self._labels[drone_id] = message.get("label")
        self._identification[drone_id] = identification_of(message)
        self._height_available = height_available
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
            self._track_source.pop(drone_id, None)
        elif not self._rejects(track, message, now_s=now_s):
            self._note_missing_times(track, message)
            self._track_source[drone_id] = source
            raised = self._evaluate(track, height_available=height_available)

        cleared = self._expire(now_s)
        return Change(raised=raised, cleared=cleared)

    def _enabled(self, source: tuple[str, str]) -> bool:
        return self.source_enabled is None or self.source_enabled(*source)

    def _refuse_disabled(
        self, drone_id: UUID, source: tuple[str, str], *, now_s: float
    ) -> Change:
        """A message from a source switched off: counted, not evaluated, and
        the aircraft dropped if its track came from that source."""
        self.rejected_source_disabled += 1
        if drone_id not in self._source_disabled_logged:
            self._source_disabled_logged.add(drone_id)
            _log.info(
                "telemetry not evaluated: source disabled",
                extra={
                    "drone_id": str(drone_id),
                    "source_type": source[0],
                    "station_id": source[1],
                    "rejected_source_disabled": self.rejected_source_disabled,
                },
            )
        cleared: list[Cleared] = []
        if self._track_source.get(drone_id) == source:
            cleared = self._drop_disabled({drone_id})
        return Change(raised=[], cleared=cleared + self._expire(now_s))

    def apply_sources(self, *, now_s: float) -> Change:
        """Drop every aircraft whose source is now switched off, clearing its
        alerts as `source_disabled`. Called when the switches change, and on
        every tick, so a change is applied even if its notice was missed."""
        disabled = {
            drone_id
            for drone_id, source in self._track_source.items()
            if not self._enabled(source)
        }
        cleared = self._drop_disabled(disabled) if disabled else []
        return Change(raised=[], cleared=cleared + self._expire(now_s))

    def _drop_disabled(self, drone_ids: set[UUID]) -> list[Cleared]:
        for drone_id in drone_ids:
            source = self._track_source.pop(drone_id, None)
            self._labels.pop(drone_id, None)
            self._identification.pop(drone_id, None)
            self.index.remove(drone_id)
            self._last_seen_s.pop(drone_id, None)
            for by_source in [k for k in self._last_by_source_s if k[0] == drone_id]:
                del self._last_by_source_s[by_source]
            self.dropped_source_disabled += 1
            _log.info(
                "aircraft dropped: source disabled",
                extra={
                    "drone_id": str(drone_id),
                    "source_type": None if source is None else source[0],
                    "station_id": None if source is None else source[1],
                    "dropped_source_disabled": self.dropped_source_disabled,
                },
            )
        cleared: list[Cleared] = []
        for key, alert in list(self._active.items()):
            if any(drone_id in drone_ids for drone_id in alert.drone_ids):
                cleared.append(Cleared(alert=alert, reason=ClearReason.SOURCE_DISABLED))
                del self._active[key]
                self._last_true_s.pop(key, None)
                self._last_false_s.pop(key, None)
        return cleared

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
            (AlertKind.IDENTIFICATION_MISMATCH, self._check_mismatch),
        ):
            if kind is AlertKind.HEIGHT and not height_available:
                not_evaluated.add(kind)
                continue
            found = self._guarded(kind, check, track, at_s)
            if found is None:
                not_evaluated.add(kind)
                if kind is AlertKind.ZONE:
                    # The zone check raises identification alerts too.
                    not_evaluated.add(AlertKind.IDENTIFICATION)
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

    def prohibited_without_terrain(self) -> list[str]:
        """PROHIBITED zones that cannot be judged vertically because no
        terrain is configured; the service logs them at error level."""
        if self.terrain is not None:
            return []
        return [
            zone.identifier
            for zone in self.zones
            if zone.restriction is Restriction.PROHIBITED and needs_terrain(zone)
        ]

    def zone_severity(self, zone: Zone) -> Severity | None:
        """What being in this zone raises; None for a zone that raises nothing."""
        if zone.restriction is Restriction.PROHIBITED:
            return Severity.CRITICAL
        if zone.restriction is Restriction.REQ_AUTHORISATION:
            return Severity.WARNING
        if zone.restriction is Restriction.CONDITIONAL:
            return self.conditional_severity
        return None

    def _check_zones(self, track: Track, now_s: float) -> list[Alert]:
        """`now_s` is the track's placed time; a zone's applicability is
        judged at it, in UTC, not at the monitor's wall clock."""
        raised: list[Alert] = []
        at = datetime.fromtimestamp(now_s, tz=UTC)
        for zone in self.zones:
            severity = self.zone_severity(zone)
            if severity is None:
                continue
            if not zone.applies_at(at):
                continue
            if not zone.contains_horizontally(track.lat_deg, track.lon_deg):
                continue
            key = zone_key(track.drone_id, zone)
            verdict = self._judge_vertical(zone, track)
            if verdict.outside:
                self._zone_unevaluated_logged.discard(key)
                continue
            if verdict.not_judged and not (
                zone.restriction in _WARN_UNJUDGED
                and verdict.not_judged <= {VerticalReference.AGL}
            ):
                self._zone_not_evaluated(key, zone, track, verdict.heights)
                self._unevaluated_keys.add(identification_key(track.drone_id, zone))
                continue
            self._zone_unevaluated_logged.discard(key)
            if (
                zone.restriction is Restriction.REQ_AUTHORISATION
                and self.authorisations is not None
                and self.authorisations.authorised(track.drone_id, zone, at)
            ):
                continue
            detail: dict[str, Any] = {
                "zone_id": str(zone.zone_id),
                "identifier": zone.identifier,
                "zone_name": zone.name,
                "restriction": zone.restriction.value,
                "reason": list(zone.reason),
                "message": zone.message,
                "alt_amsl_m": round(track.alt_amsl_m, 1),
                **_limits_detail(zone, verdict.heights),
            }
            if verdict.margin_m > 0:
                # S-33: a pressure altitude, judged with its uncertainty.
                detail["vertical_known"] = False
                detail["pressure_uncertainty_m"] = verdict.margin_m
                detail["within_band"] = verdict.within_band
            if verdict.not_judged:
                # A ceiling above the ground with no ground to measure from:
                # a warning, since a false one beats a missed critical.
                self.zone_limits_not_judged += 1
                detail["vertical_known"] = False
                detail["limit_not_judged"] = True
                detail["not_judged"] = sorted(r.value for r in verdict.not_judged)
            identification = self._identification.get(track.drone_id)
            if identification is not None:
                # U-02: who the aircraft in the zone is, as the registry sees it.
                detail["identification"] = identification
            alert = Alert(
                key=key,
                kind=AlertKind.ZONE,
                severity=(
                    severity
                    if verdict.within_band and not verdict.not_judged
                    else Severity.WARNING
                ),
                drone_ids=(track.drone_id,),
                labels=(self._labels.get(track.drone_id),),
                detail=detail,
            )
            raised.extend(self._refresh(alert, now_s))
            if (
                zone.restriction in _IDENTITY_ZONES
                and identification is not None
                and identification.get("status") in INCIDENT_STATUSES
            ):
                raised.extend(
                    self._refresh(
                        self._identification_alert(track, zone, identification),
                        now_s,
                    )
                )
        return raised

    def _identification_alert(
        self, track: Track, zone: Zone, identification: dict[str, Any]
    ) -> Alert:
        """U-02: an aircraft nobody can name, where one must be named. The
        seam U-12 turns into an incident (`airspace/service.py`)."""
        return Alert(
            key=identification_key(track.drone_id, zone),
            kind=AlertKind.IDENTIFICATION,
            severity=self.identification_severity,
            drone_ids=(track.drone_id,),
            labels=(self._labels.get(track.drone_id),),
            detail={
                "status": identification.get("status"),
                "reason": identification.get("reason"),
                "serial": identification.get("serial"),
                "operator_reg": identification.get("operator_reg"),
                "mismatch": identification.get("mismatch") is True,
                "zone_id": str(zone.zone_id),
                "identifier": zone.identifier,
                "zone_name": zone.name,
                "restriction": zone.restriction.value,
                "lat_deg": round(track.lat_deg, 6),
                "lon_deg": round(track.lon_deg, 6),
                "alt_amsl_m": round(track.alt_amsl_m, 1),
                # Until U-12 there is no incident to link; U-12 sets it.
                "incident_candidate": True,
            },
        )

    def _check_mismatch(self, track: Track, now_s: float) -> list[Alert]:
        """U-02: a registered serial with another operator's number."""
        identification = self._identification.get(track.drone_id)
        if identification is None or identification.get("mismatch") is not True:
            return []
        alert = Alert(
            key=mismatch_key(track.drone_id),
            kind=AlertKind.IDENTIFICATION_MISMATCH,
            severity=self.mismatch_severity,
            drone_ids=(track.drone_id,),
            labels=(self._labels.get(track.drone_id),),
            detail={
                "status": identification.get("status"),
                "reason": identification.get("reason"),
                "serial": identification.get("serial"),
                "operator_reg": identification.get("operator_reg"),
                "registered_operator_reg": identification.get(
                    "registered_operator_reg"
                ),
            },
        )
        return self._refresh(alert, now_s)

    def _judge_vertical(self, zone: Zone, track: Track) -> VerticalVerdict:
        """The zone's limits against the aircraft, each in its own reference.

        A lower AGL limit at or below the ground is met by any airborne
        aircraft and needs no terrain. With the altitude a pressure altitude
        (S-33) each judged limit is widened by `pressure_uncertainty_m`:
        inside the band as indicated is `within_band`; inside the widened
        band only is not. A limit whose height is unknown is `not_judged`.
        """
        margin_m = (
            self.pressure_uncertainty_m
            if not track.vertical_known and zone.references
            else 0.0
        )
        heights: dict[VerticalReference, float | None] = {}
        not_judged: set[VerticalReference] = set()
        within_band = True
        judged_any = False
        for limit, is_lower in ((zone.lower, True), (zone.upper, False)):
            if limit is None:
                continue
            if (
                is_lower
                and limit.reference is VerticalReference.AGL
                and limit.value_m <= 0
            ):
                continue
            if limit.reference not in heights:
                heights[limit.reference] = self._height_in(limit.reference, track)
            height_m = heights[limit.reference]
            if height_m is None:
                not_judged.add(limit.reference)
                continue
            judged_any = True
            beyond_m = (
                limit.value_m - height_m if is_lower else height_m - limit.value_m
            )
            if beyond_m > margin_m:
                return VerticalVerdict(True, False, frozenset(), heights, margin_m)
            if beyond_m > 0:
                within_band = False
        return VerticalVerdict(
            False,
            within_band,
            frozenset(not_judged),
            heights,
            margin_m if judged_any else 0.0,
        )

    def _refresh(self, alert: Alert, now_s: float) -> list[Alert]:
        """Hold `alert` active, refreshed with its latest numbers; return
        it when that is a transition the bus and audit log must carry: newly
        true, or true at another severity. A severity change is raised again
        under the same key, never changed silently in place (S-33)."""
        key = alert.key
        self._last_true_s[key] = now_s
        previous = self._active.get(key)
        self._active[key] = alert
        if previous is None:
            return [alert]
        if previous.severity is not alert.severity:
            _log.info(
                "alert severity changed",
                extra={
                    "key": key,
                    "drone_id": str(alert.drone_ids[0]),
                    "before": previous.severity.value,
                    "after": alert.severity.value,
                },
            )
            return [alert]
        return []

    def _height_in(self, reference: VerticalReference, track: Track) -> float | None:
        """The aircraft's height in a zone limit's reference, from its AMSL
        altitude; None where the terrain or the geoid needed is unknown."""
        if reference is VerticalReference.AMSL:
            return track.alt_amsl_m
        if reference is VerticalReference.AGL:
            ground = self._ground(track)
            return None if ground is None else track.alt_amsl_m - ground.elevation_m
        if self.geoid is None:
            return None
        return track.alt_amsl_m + self.geoid.undulation_m(track.lat_deg, track.lon_deg)

    def _ground(self, track: Track) -> Elevation | None:
        """The ground under the aircraft, or None when it is not known: no
        terrain, a tile the service could not read for this message, or a
        cell that was never fetched. Never zero for unknown."""
        if self.terrain is None or not self._height_available:
            return None
        try:
            return self.terrain.elevation(track.lat_deg, track.lon_deg)
        except TerrainFileError:
            return None

    def _zone_not_evaluated(
        self,
        key: str,
        zone: Zone,
        track: Track,
        heights: dict[VerticalReference, float | None],
    ) -> None:
        self._unevaluated_keys.add(key)
        self.zone_checks_not_evaluated += 1
        if key in self._zone_unevaluated_logged:
            return
        self._zone_unevaluated_logged.add(key)
        missing = sorted(ref.value for ref, height in heights.items() if height is None)
        _log.warning(
            "zone not evaluated: a limit needs a height that is not known here",
            extra={
                "drone_id": str(track.drone_id),
                "zone_id": str(zone.zone_id),
                "identifier": zone.identifier,
                "missing_references": missing,
                "needs": [
                    "terrain (TERRAIN_DIR)" if ref == "AGL" else "geoid (GEOID_PATH)"
                    for ref in missing
                ],
                "lat_deg": track.lat_deg,
                "lon_deg": track.lon_deg,
                "zone_checks_not_evaluated": self.zone_checks_not_evaluated,
            },
        )

    def _check_height(self, track: Track, now_s: float) -> list[Alert]:
        if self.terrain is None or self.max_height_agl_m is None:
            return []
        ground = self.terrain.elevation(track.lat_deg, track.lon_deg)
        if ground is None:
            return []
        height_agl_m = track.alt_amsl_m - ground.elevation_m
        # S-33: a pressure altitude is judged as indicated, and the alert
        # says it is approximate, to within the uncertainty margin.
        if height_agl_m <= self.max_height_agl_m:
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
            detail["pressure_uncertainty_m"] = self.pressure_uncertainty_m
        alert = Alert(
            key=key,
            kind=AlertKind.HEIGHT,
            severity=Severity.WARNING,
            drone_ids=(track.drone_id,),
            labels=(self._labels.get(track.drone_id),),
            detail=detail,
        )
        # Refreshed either way: the height changes as the aircraft climbs.
        return self._refresh(alert, now_s)

    # --- clearing --------------------------------------------------------------

    def _expire(self, now_s: float) -> list[Cleared]:
        for drone_id, seen_s in list(self._last_seen_s.items()):
            if now_s - seen_s > self.stale_after_s:
                self.index.remove(drone_id)
                del self._last_seen_s[drone_id]
                self._track_source.pop(drone_id, None)

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

"""Remote ID broadcasts as aircraft on the same bus as MAVLink telemetry. P1-15.

A receiver (an ESP32, a phone, a commercial unit) hears Open Drone ID
messages and forwards each one, with the address it came from, as a
`Frame`. `RemoteIdTracker` keeps the latest of each message kind per
transmitter and, whenever a Location arrives, returns an observation shaped
like the Gateway's telemetry message (`gateway/publisher.py`), so the console
and the airspace monitor take it without knowing where it came from.

## Identity

Messages are joined by the transmitter's address, as receivers see them: a
Bluetooth 4 broadcast sends Basic ID and Location separately, and only the
address ties them together.

The aircraft's id on the bus is a UUID derived from its broadcast identity
(ID type and UAS ID), not from the transmitter address, which some
transmitters randomise. The same serial number always maps to the same id.

### An identity is used only while it is fresh (S-32)

An address can be reused: by a randomising transmitter, by a module that
reboots with another identity, by a spoofer. A Location joined to the
identity of the aircraft that used the address before is a confident wrong
answer, so an identity is used only while all of these hold:

- its Basic ID was heard within `identity_ttl_s` (15 s: five of the
  standard's 3 s static periods);
- no other Basic ID of the same ID type has been heard from the address
  since. If one has, everything known about the address is dropped,
  System and Operator ID with it, and counted (`identity_changes`);
- the address has not been silent for longer than `max_gap_s` (3 s: three
  of the standard's 1 s Location periods). After such a silence, a reboot
  or another aircraft, everything known about it is dropped (`silences`).

A Location without a fresh identity is held for up to `identify_within_s`
after the address lost or never had one, since a Basic ID normally follows
within a static period; one arriving publishes it. After that, Locations
are published as an **unidentified** track of the transmitter: its id is
derived from the address, its label is the address, and `remote_id` says
`identified: false`, with an empty UAS ID and ID type 0 (none). It is never
attached to an earlier serial. One transmitter can therefore appear under
two ids, unidentified and identified, while its identity comes and goes;
the airspace monitor does not pair two Remote ID tracks of one transmitter
address with each other (`airspace/monitor.py`).

## Time (S-27)

A Location carries its own capture time, in tenths of a second after the
full UTC hour (`odid.Location.seconds_after_hour`). The hour is the one that
puts it closest to, and not after, the Gateway's receive time plus
`time_tolerance_s` and the broadcast's own timestamp accuracy: a broadcast
at xx:59:59.9 heard at (xx+1):00:00.2 is placed in hour xx, not 59 minutes
ahead.

- `ts` is that broadcast time, on the aircraft's clock.
- `rx_ts` is the Gateway's receive time.
- `captured_at`, where the airspace monitor places the aircraft, is the
  broadcast time when it is plausible: not ahead of the receive time by more
  than the tolerance, and not older than `max_latency_s`, the most a
  receiver and the network can plausibly hold a broadcast. Otherwise it is
  the receive time, and the fallback is counted in `time_fallbacks` by
  reason. A timestamp the standard marks unknown (0xFFFF, `odid`), or one
  past the hour, makes `ts` the receive time too.

`remote_id.time_source` says which: `broadcast` or `receiver`.

## Nothing here is authenticated

A broadcast can be forged by anyone with a phone. Every observation carries
`source: "remote_id"` and `authenticated: false`, and the console says so
wherever it shows one. An alert involving one is an alert about a claimed
position.

## Altitude

The broadcast's altitude is height above the WGS-84 ellipsoid (HAE); the
system's is above mean sea level. They differ by the geoid undulation
(EGM2008: 15.9 m at Tbilisi, 22.5 m at Batumi), and CLAUDE.md forbids mixing
them. `alt_amsl_m` is filled only through a geoid model. Without one it is None, and the airspace monitor,
which needs an AMSL altitude, does not consider the aircraft: a separation
computed on a 16 to 23 m error would be a confident wrong answer.

### Pressure altitude (S-33)

When the geodetic altitude is missing (the broadcast's unknown value) or
flagged inaccurate, its vertical accuracy known and worse than
`min_vertical_accuracy` (MAV_ODID_VER_ACC: default 2, under 45 m), the
broadcast's pressure altitude is used for `alt_amsl_m` instead. Pressure
altitude is referenced to the standard 1013.25 hPa, not to the local QNH,
so it is AMSL only on a standard day: off by about 8 m per hPa of
difference. That is why `alt_source` says which was used, `geodetic` or
`pressure` (None when there is no AMSL altitude), and the raw
`alt_pressure_m` is carried beside it. An unknown accuracy is not a flag:
the geodetic altitude is kept. Without a geoid, a usable geodetic altitude
still gives no AMSL altitude; pressure is a substitute for a bad geodetic
altitude, not for a missing geoid.

## Flying

MAVLink telemetry says `armed`; Remote ID says `status`. An observation
carries `airborne`, and `armed` is None, never a guess. Status "ground" is
the only one that is not airborne: an undeclared status errs towards
airborne, as the monitor errs towards counting an armed aircraft as flying.
"""

from __future__ import annotations

import math
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from uuid import UUID

from common import get_logger
from gateway import odid

_log = get_logger(__name__)

SOURCE = "remote_id"

# Fixed forever: changing it would give every Remote ID aircraft a new id.
REMOTE_ID_NAMESPACE = UUID("6f1c7d52-4a0b-5c1e-9d3a-2b8e41f07a65")

# How far ahead of the Gateway's clock a broadcast time may be. The module's
# clock is GPS time and the Gateway's NTP, both far better than this; the
# margin covers the timestamp being truncated to a tenth and clock steps.
DEFAULT_TIME_TOLERANCE_S = 1.0
# How old a broadcast may be when the Gateway receives it and still be placed
# at its own time. A receiver forwards what it hears at once; older than this
# says more about the module's clock than about the aircraft.
DEFAULT_MAX_LATENCY_S = 5.0
# The timestamp covers one hour, in tenths of a second.
_SECONDS_PER_HOUR = 3600.0
# Timestamp accuracy is in steps of 0.1 s, and 0 is unknown
# (MAV_ODID_TIME_ACC; pinned against pymavlink in the tests).
_TS_ACCURACY_STEP_S = 0.1

# S-32: see "An identity is used only while it is fresh" above.
DEFAULT_IDENTITY_TTL_S = 15.0
DEFAULT_MAX_GAP_S = 3.0
DEFAULT_IDENTIFY_WITHIN_S = 4.0

TIME_SOURCE_BROADCAST = "broadcast"
TIME_SOURCE_RECEIVER = "receiver"

# S-33. Vertical accuracy codes (MAV_ODID_VER_ACC, pinned against pymavlink
# in the tests): 0 unknown, then 1 to 6 for under 150, 45, 25, 10, 3 and
# 1 m. A geodetic altitude whose known accuracy is below this code is not
# used.
DEFAULT_MIN_VERTICAL_ACCURACY = 2
_VERTICAL_ACCURACY_UNKNOWN = 0
ALT_SOURCE_GEODETIC = "geodetic"
ALT_SOURCE_PRESSURE = "pressure"
# What a stored row names as the model behind an AMSL height taken from
# pressure altitude (`remote_id_observations.geoid_model`).
PRESSURE_ALTITUDE_MODEL = "pressure altitude, ISA 1013.25 hPa"


class Geoid(Protocol):
    """Height of the geoid above the WGS-84 ellipsoid, metres."""

    def undulation_m(self, lat_deg: float, lon_deg: float) -> float: ...


@dataclass(frozen=True, slots=True)
class Frame:
    receiver_id: str
    # How the receiver told transmitters apart: a Bluetooth or Wi-Fi address.
    transmitter: str
    received_at: datetime
    payload: bytes
    rssi_dbm: float | None = None


def aircraft_id(basic: odid.BasicId) -> UUID:
    return uuid.uuid5(REMOTE_ID_NAMESPACE, f"{basic.id_type}:{basic.ua_id}")


def unidentified_aircraft_id(transmitter: str) -> UUID:
    """The id of a transmitter heard without a fresh identity (S-32).

    Not per receiver: two receivers hearing one transmitter are one track,
    as they are for a serial.
    """
    return uuid.uuid5(REMOTE_ID_NAMESPACE, f"transmitter:{transmitter}")


def broadcast_time(
    seconds_after_hour: float, received_at: datetime, *, ahead_s: float
) -> datetime:
    """The UTC instant a Location timestamp names.

    Of the instants `seconds_after_hour` into some hour, the latest that is
    not after `received_at + ahead_s`: the closest one at which the broadcast
    can have been made.
    """
    limit = received_at.astimezone(UTC) + timedelta(seconds=ahead_s)
    hour = limit.replace(minute=0, second=0, microsecond=0)
    moment = hour + timedelta(seconds=seconds_after_hour)
    if moment > limit:
        moment -= timedelta(hours=1)
    return moment


def ts_accuracy_s(location: odid.Location) -> float | None:
    """The broadcast's own bound on its timestamp error; None if unknown."""
    if location.ts_accuracy == 0:
        return None
    return location.ts_accuracy * _TS_ACCURACY_STEP_S


def geodetic_usable(location: odid.Location, *, min_vertical_accuracy: int) -> bool:
    """A geodetic altitude is there, and its accuracy is not flagged poor."""
    if location.alt_hae_m is None:
        return False
    accuracy = location.vert_accuracy
    return accuracy == _VERTICAL_ACCURACY_UNKNOWN or accuracy >= min_vertical_accuracy


def amsl(
    location: odid.Location,
    geoid: Geoid | None,
    *,
    min_vertical_accuracy: int = DEFAULT_MIN_VERTICAL_ACCURACY,
) -> tuple[float | None, str | None]:
    """The AMSL altitude and which broadcast altitude it came from (S-33)."""
    hae_m = location.alt_hae_m
    if hae_m is not None and geodetic_usable(
        location, min_vertical_accuracy=min_vertical_accuracy
    ):
        if geoid is None or location.lat_deg is None or location.lon_deg is None:
            return None, None
        undulation_m = geoid.undulation_m(location.lat_deg, location.lon_deg)
        return hae_m - undulation_m, ALT_SOURCE_GEODETIC
    if location.alt_baro_m is not None:
        return location.alt_baro_m, ALT_SOURCE_PRESSURE
    return None, None


@dataclass(frozen=True, slots=True)
class Placement:
    """Where an observation sits in time (gateway/README.md)."""

    ts: datetime
    captured_at: datetime
    # TIME_SOURCE_BROADCAST or TIME_SOURCE_RECEIVER: what `captured_at` is.
    time_source: str
    # Why the broadcast time was not used; None when it was.
    fallback: str | None


def place(
    location: odid.Location,
    received_at: datetime,
    *,
    tolerance_s: float = DEFAULT_TIME_TOLERANCE_S,
    max_latency_s: float = DEFAULT_MAX_LATENCY_S,
) -> Placement:
    """The broadcast's capture time, where it can be believed (S-27)."""
    seconds = location.seconds_after_hour
    if seconds is None:
        return Placement(received_at, received_at, TIME_SOURCE_RECEIVER, "unknown")
    if not 0.0 <= seconds < _SECONDS_PER_HOUR:
        return Placement(received_at, received_at, TIME_SOURCE_RECEIVER, "invalid")
    accuracy_s = ts_accuracy_s(location) or 0.0
    ahead_s = tolerance_s + accuracy_s
    moment = broadcast_time(seconds, received_at, ahead_s=ahead_s)
    age_s = (received_at - moment).total_seconds()
    if age_s < -ahead_s:
        # broadcast_time never returns this; checked so that a change there
        # cannot quietly place an aircraft in the future.
        return Placement(moment, received_at, TIME_SOURCE_RECEIVER, "future")
    if age_s > max_latency_s + accuracy_s:
        return Placement(moment, received_at, TIME_SOURCE_RECEIVER, "too_old")
    return Placement(moment, moment, TIME_SOURCE_BROADCAST, None)


@dataclass
class _Identity:
    basic: odid.BasicId
    heard_s: float


@dataclass
class _Transmitter:
    # When this state began: first heard, heard again after a silence, or
    # taken by another identity.
    started_s: float
    last_heard_s: float
    # The Basic IDs heard, one per ID type: a transmitter may send two.
    identities: dict[int, _Identity] = field(default_factory=dict)
    location: odid.Location | None = None
    # The frame that carried `location`: its receive time places it.
    location_frame: Frame | None = None
    location_published: bool = False
    system: odid.System | None = None
    operator: odid.OperatorId | None = None


def _preferred(identities: list[_Identity]) -> odid.BasicId | None:
    """A serial number over any other identity, else the latest heard.

    A transmitter may send two Basic IDs. The serial is fixed to the
    airframe; a registration can move between airframes, so it is the
    weaker identity.
    """
    for identity in identities:
        if identity.basic.id_type == odid.IdType.SERIAL_NUMBER:
            return identity.basic
    if not identities:
        return None
    return max(identities, key=lambda i: i.heard_s).basic


@dataclass
class RemoteIdTracker:
    geoid: Geoid | None = None
    # S-32: see "An identity is used only while it is fresh" above.
    identity_ttl_s: float = DEFAULT_IDENTITY_TTL_S
    max_gap_s: float = DEFAULT_MAX_GAP_S
    identify_within_s: float = DEFAULT_IDENTIFY_WITHIN_S
    # S-27: see "Time" above.
    time_tolerance_s: float = DEFAULT_TIME_TOLERANCE_S
    max_latency_s: float = DEFAULT_MAX_LATENCY_S
    # S-33: see "Pressure altitude" above.
    min_vertical_accuracy: int = DEFAULT_MIN_VERTICAL_ACCURACY
    # Observations whose `captured_at` fell back to the receive time, by
    # reason: unknown, invalid, future, too_old.
    time_fallbacks: Counter[str] = field(default_factory=Counter, init=False)
    # A different Basic ID from an address with an identity (S-32).
    identity_changes: int = field(default=0, init=False)
    # Addresses forgotten after a silence longer than `max_gap_s`.
    silences: int = field(default=0, init=False)
    # Observations published without a fresh identity.
    unidentified: int = field(default=0, init=False)

    _by_transmitter: dict[tuple[str, str], _Transmitter] = field(
        default_factory=dict, init=False
    )

    def take(self, frame: Frame, *, now_s: float) -> dict[str, Any] | None:
        """Decode one frame; return an observation when it completes one.

        Raises `odid.DecodeError` for bytes that are not a valid message.
        """
        messages = odid.decode(frame.payload)
        # Before this frame counts as hearing the address: a silence ends here.
        self._forget(now_s)
        key = (frame.receiver_id, frame.transmitter)
        state = self._by_transmitter.get(key)
        if state is None:
            state = _Transmitter(started_s=now_s, last_heard_s=now_s)
            self._by_transmitter[key] = state
        state.last_heard_s = now_s
        # Identity first: a Basic ID that changes it drops what the address
        # said before, and must not drop a Location in the same pack.
        for message in messages:
            if isinstance(message, odid.BasicId) and _identified(message):
                self._learn(state, message, frame, now_s)
        for message in messages:
            if isinstance(message, odid.Location):
                state.location = message
                state.location_frame = frame
                state.location_published = False
            elif isinstance(message, odid.System):
                state.system = message
            elif isinstance(message, odid.OperatorId):
                state.operator = message
        location, location_frame = state.location, state.location_frame
        # Each Location is published once: when it arrives, or when the
        # Basic ID it was held for does.
        if location is None or location_frame is None or state.location_published:
            return None
        basic = _preferred(
            [
                identity
                for identity in state.identities.values()
                if now_s - identity.heard_s <= self.identity_ttl_s
            ]
        )
        if basic is None:
            if now_s - self._unidentified_since(state) < self.identify_within_s:
                return None
            self.unidentified += 1
        state.location_published = True
        return self._observation(basic, location, state, location_frame)

    def _learn(
        self, state: _Transmitter, basic: odid.BasicId, frame: Frame, now_s: float
    ) -> None:
        known = state.identities.get(basic.id_type)
        if known is not None and known.basic.ua_id != basic.ua_id:
            # Another aircraft on this address: nothing it said before is
            # this one's, a held Location included.
            self.identity_changes += 1
            _log.warning(
                "remote id identity changed on one transmitter address",
                extra={
                    "station_id": frame.receiver_id,
                    "transmitter": frame.transmitter,
                    "id_type": basic.id_type,
                    "previous_ua_id": known.basic.ua_id,
                    "ua_id": basic.ua_id,
                    "identity_changes": self.identity_changes,
                },
            )
            state.identities.clear()
            state.location = None
            state.location_frame = None
            state.system = None
            state.operator = None
            state.started_s = now_s
        state.identities[basic.id_type] = _Identity(basic=basic, heard_s=now_s)

    def _unidentified_since(self, state: _Transmitter) -> float:
        """Since when the address has had no fresh identity."""
        if not state.identities:
            return state.started_s
        latest_s = max(identity.heard_s for identity in state.identities.values())
        return max(state.started_s, latest_s + self.identity_ttl_s)

    def _forget(self, now_s: float) -> None:
        """Drop addresses silent for longer than `max_gap_s`: whatever is
        heard from one next is not known to be the same aircraft."""
        for key, state in list(self._by_transmitter.items()):
            if now_s - state.last_heard_s > self.max_gap_s:
                del self._by_transmitter[key]
                self.silences += 1

    def _observation(
        self,
        basic: odid.BasicId | None,
        location: odid.Location,
        state: _Transmitter,
        frame: Frame,
    ) -> dict[str, Any]:
        vn, ve, vd = _velocity_ned(location)
        alt_amsl, alt_source = amsl(
            location, self.geoid, min_vertical_accuracy=self.min_vertical_accuracy
        )
        over_takeoff = location.height_reference == odid.HeightReference.OVER_TAKEOFF
        system = state.system
        placed = place(
            location,
            frame.received_at,
            tolerance_s=self.time_tolerance_s,
            max_latency_s=self.max_latency_s,
        )
        if placed.fallback is not None:
            self.time_fallbacks[placed.fallback] += 1
        drone_id = (
            unidentified_aircraft_id(frame.transmitter)
            if basic is None
            else aircraft_id(basic)
        )
        return {
            "drone_id": str(drone_id),
            # An unidentified transmitter has no name but its address.
            "label": frame.transmitter if basic is None else basic.ua_id,
            "source": SOURCE,
            "authenticated": False,
            "link": None,
            "firmware": None,
            # See "Time" above (S-27). A broadcast is never a backlog: there
            # is no queue between the receiver and here (S-11), and one older
            # than `max_latency_s` is placed at its receive time.
            "ts": placed.ts.isoformat(),
            "rx_ts": frame.received_at.isoformat(),
            "captured_at": placed.captured_at.isoformat(),
            "backlog": False,
            "station_id": frame.receiver_id,
            "lat_deg": location.lat_deg,
            "lon_deg": location.lon_deg,
            "alt_amsl_m": alt_amsl,
            # S-33: which broadcast altitude `alt_amsl_m` came from.
            "alt_source": alt_source,
            "alt_hae_m": location.alt_hae_m,
            # Referenced to 1013.25 hPa, as broadcast: not AMSL.
            "alt_pressure_m": location.alt_baro_m,
            # Only when the broadcast's height is over the take-off point;
            # height over the ground is a different quantity (P5-00).
            "alt_above_home_m": location.height_m if over_takeoff else None,
            # Remote ID reports the track over the ground, not where the nose
            # points. The console draws the arrow from it when heading is None.
            "heading_deg": None,
            "track_deg": location.direction_deg,
            "vx_ms": vn,
            "vy_ms": ve,
            "vz_ms": vd,
            "batt_pct": None,
            "batt_voltage_v": None,
            "batt_consumed_wh": None,
            "mode": None,
            "armed": None,
            "airborne": location.status != odid.Status.GROUND,
            "gps_fix_type": None,
            "sat_count": None,
            "groundspeed_ms": location.speed_horizontal_ms,
            "climb_ms": location.speed_vertical_ms,
            "remote_id": {
                # S-32: no identity is an empty UAS ID of ID type 0 (none),
                # as the standard encodes it, never an earlier serial.
                "identified": basic is not None,
                "ua_id": "" if basic is None else basic.ua_id,
                "id_type": odid.IdType.NONE if basic is None else basic.id_type,
                "ua_type": None if basic is None else basic.ua_type,
                "status": location.status,
                "operator_id": state.operator.operator_id if state.operator else None,
                "operator_lat_deg": system.operator_lat_deg if system else None,
                "operator_lon_deg": system.operator_lon_deg if system else None,
                "transmitter": frame.transmitter,
                "rssi_dbm": frame.rssi_dbm,
                "time_source": placed.time_source,
                "ts_accuracy_s": ts_accuracy_s(location),
            },
        }


def _identified(basic: odid.BasicId | None) -> bool:
    return basic is not None and basic.id_type != odid.IdType.NONE and bool(basic.ua_id)


def _velocity_ned(
    location: odid.Location,
) -> tuple[float | None, float | None, float | None]:
    """North, east, down in m/s: MAVLink's convention, which the monitor uses."""
    vd = None if location.speed_vertical_ms is None else -location.speed_vertical_ms
    if location.speed_horizontal_ms is None or location.direction_deg is None:
        # A known speed with no direction is not a velocity; zero would claim
        # the aircraft is hovering.
        return None, None, vd
    track = math.radians(location.direction_deg)
    speed = location.speed_horizontal_ms
    return speed * math.cos(track), speed * math.sin(track), vd

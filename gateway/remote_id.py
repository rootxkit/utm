"""Remote ID broadcasts as aircraft on the same bus as MAVLink telemetry. P1-15.

A receiver (an ESP32, a phone, a commercial unit) hears Open Drone ID
messages and forwards each one, with the address it came from, as a
`Frame`. `RemoteIdTracker` keeps the latest of each message kind per
transmitter and, whenever a Location arrives from a transmitter whose
identity it knows, returns an observation shaped like the Gateway's
telemetry message (`gateway/publisher.py`), so the console and the airspace
monitor take it without knowing where it came from.

## Identity

Messages are joined by the transmitter's address, as receivers see them: a
Bluetooth 4 broadcast sends Basic ID and Location separately, and only the
address ties them together. A Location from a transmitter that has not yet
sent a Basic ID is held until one arrives; an aircraft is not shown without
an identity.

The aircraft's id on the bus is a UUID derived from its broadcast identity
(ID type and UAS ID), not from the transmitter address, which some
transmitters randomise. The same serial number always maps to the same id.

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

## Flying

MAVLink telemetry says `armed`; Remote ID says `status`. An observation
carries `airborne`, and `armed` is None, never a guess. Status "ground" is
the only one that is not airborne: an undeclared status errs towards
airborne, as the monitor errs towards counting an armed aircraft as flying.
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from gateway import odid

SOURCE = "remote_id"

# Fixed forever: changing it would give every Remote ID aircraft a new id.
REMOTE_ID_NAMESPACE = UUID("6f1c7d52-4a0b-5c1e-9d3a-2b8e41f07a65")


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


@dataclass
class _Transmitter:
    basic: odid.BasicId | None = None
    location: odid.Location | None = None
    system: odid.System | None = None
    operator: odid.OperatorId | None = None
    last_frame: Frame | None = None
    last_heard_s: float = 0.0


def _preferred(current: odid.BasicId | None, new: odid.BasicId) -> odid.BasicId:
    """A serial number over a registration id, and either over nothing.

    A transmitter may send two Basic IDs. The serial is fixed to the airframe;
    a registration can move between airframes, so it is the weaker identity.
    """
    if new.id_type == odid.IdType.NONE or not new.ua_id:
        return current if current is not None else new
    if current is None or current.id_type == odid.IdType.NONE or not current.ua_id:
        return new
    if new.id_type == odid.IdType.SERIAL_NUMBER:
        return new
    return current if current.id_type == odid.IdType.SERIAL_NUMBER else new


@dataclass
class RemoteIdTracker:
    geoid: Geoid | None = None
    # Transmitters not heard for this long are forgotten, so their partial
    # state cannot attach to a new aircraft reusing a random address.
    forget_after_s: float = 60.0

    _by_transmitter: dict[tuple[str, str], _Transmitter] = field(
        default_factory=dict, init=False
    )

    def take(self, frame: Frame, *, now_s: float) -> dict[str, Any] | None:
        """Decode one frame; return an observation when it completes one.

        Raises `odid.DecodeError` for bytes that are not a valid message.
        """
        messages = odid.decode(frame.payload)
        self._forget(now_s)
        key = (frame.receiver_id, frame.transmitter)
        state = self._by_transmitter.setdefault(key, _Transmitter())
        state.last_frame = frame
        state.last_heard_s = now_s
        new_location = False
        for message in messages:
            if isinstance(message, odid.BasicId):
                state.basic = _preferred(state.basic, message)
            elif isinstance(message, odid.Location):
                state.location = message
                new_location = True
            elif isinstance(message, odid.System):
                state.system = message
            elif isinstance(message, odid.OperatorId):
                state.operator = message
        basic, location = state.basic, state.location
        if location is None or basic is None or not _identified(basic):
            return None
        # A Basic ID completing a held Location publishes it once; after that,
        # only a new Location does.
        if not new_location and not any(isinstance(m, odid.BasicId) for m in messages):
            return None
        return self._observation(basic, location, state, frame)

    def _forget(self, now_s: float) -> None:
        for key, state in list(self._by_transmitter.items()):
            if now_s - state.last_heard_s > self.forget_after_s:
                del self._by_transmitter[key]

    def _observation(
        self,
        basic: odid.BasicId,
        location: odid.Location,
        state: _Transmitter,
        frame: Frame,
    ) -> dict[str, Any]:
        vn, ve, vd = _velocity_ned(location)
        alt_amsl = None
        if (
            self.geoid is not None
            and location.alt_hae_m is not None
            and location.lat_deg is not None
            and location.lon_deg is not None
        ):
            alt_amsl = location.alt_hae_m - self.geoid.undulation_m(
                location.lat_deg, location.lon_deg
            )
        over_takeoff = location.height_reference == odid.HeightReference.OVER_TAKEOFF
        system = state.system
        return {
            "drone_id": str(aircraft_id(basic)),
            "label": basic.ua_id,
            "source": SOURCE,
            "authenticated": False,
            "link": None,
            "firmware": None,
            # `ts` is when the Gateway received the frame, not when the
            # aircraft measured the position: the broadcast's own time
            # (`seconds_after_hour`, decoded in `gateway.odid`) is not yet
            # carried. `rx_ts` is therefore the same instant, and a broadcast
            # is never a backlog: there is no queue between the receiver and
            # here (S-11).
            "ts": frame.received_at.isoformat(),
            "rx_ts": frame.received_at.isoformat(),
            "captured_at": frame.received_at.isoformat(),
            "backlog": False,
            "station_id": frame.receiver_id,
            "lat_deg": location.lat_deg,
            "lon_deg": location.lon_deg,
            "alt_amsl_m": alt_amsl,
            "alt_hae_m": location.alt_hae_m,
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
                "ua_id": basic.ua_id,
                "id_type": basic.id_type,
                "ua_type": basic.ua_type,
                "status": location.status,
                "operator_id": state.operator.operator_id if state.operator else None,
                "operator_lat_deg": system.operator_lat_deg if system else None,
                "operator_lon_deg": system.operator_lon_deg if system else None,
                "transmitter": frame.transmitter,
                "rssi_dbm": frame.rssi_dbm,
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

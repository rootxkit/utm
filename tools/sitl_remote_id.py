"""SITL aircraft as Remote ID broadcasts, through the receiver path. U-16.

    python -m tools.sitl_remote_id --sysid 3 --serial SITLRID0003 \\
        --operator-id GEO-OP-SITL --receiver-id sitl-rx-1 \\
        --key-file local/remote-id-receivers.keys \\
        --geoid local/geoid/egm2008-2_5.pgm

Remote ID is the primary source (`docs/ARCHITECTURE.md` §2), so simulated
aircraft have to be able to enter the system through it. This bridge plays
two parts for each SITL vehicle:

- **The aircraft's Remote ID module.** It reads the vehicle's MAVLink
  (receive only: it never writes to the connection) and builds the Open
  Drone ID messages a module would broadcast: Basic ID with the serial,
  Location/Vector, System with the operator (take-off) location, and
  Operator ID. The bytes come from `gateway.odid`'s encoder, which is pinned
  to the reference library; the System encoder, which `gateway.odid` does not
  have, is here and pinned to the same library's System vectors
  (`tools/tests/test_sitl_remote_id.py`).
- **A ground receiver.** Each broadcast becomes one datagram to the Remote ID
  ingest, the JSON report an ESP32 receiver sends (`payload_hex`,
  transmitter address, `rssi_dbm`), signed with `sent_at_ms`, a nonce and an
  HMAC when `--key-file` is given (`gateway/remote_id_auth.py`).

## What comes from where

| Broadcast | MAVLink |
|---|---|
| Latitude, longitude | `GLOBAL_POSITION_INT.lat/lon` |
| Geodetic altitude (HAE) | `GLOBAL_POSITION_INT.alt` (AMSL) plus the geoid undulation: from `--geoid` (default), or with `--hae-source gps` the GPS's own, `GPS_RAW_INT.alt_ellipsoid - alt` |
| Pressure altitude | `SCALED_PRESSURE.press_abs`, ICAO standard atmosphere |
| Height over take-off | `GLOBAL_POSITION_INT.relative_alt` |
| Track, speed, vertical speed | `GLOBAL_POSITION_INT.vx/vy/vz` |
| Timestamp (tenths of a second after the hour) | `GLOBAL_POSITION_INT.time_boot_ms`, put on UTC by `SYSTEM_TIME` |
| Status | `HEARTBEAT`: armed is airborne, critical or emergency is emergency |
| Operator location | `HOME_POSITION` if the stream carries it, else where the vehicle last was while disarmed |

ArduPilot SITL reports `alt_ellipsoid` equal to `alt`: its GPS has no
geoid. With `--hae-source gps` a SITL aircraft therefore broadcasts its AMSL
height as HAE, and the ingest, which subtracts the real geoid, puts it 15 to
23 m low over Georgia. Use `--geoid` with the same grid as the ingest.

The ingest (P1-15) uses only the HAE, the height over take-off and the
velocity; pressure altitude and the broadcast timestamp are decoded and not
yet carried further. Both are sent anyway, because a real module sends them.

## Rates

Location once a second, and Basic ID, Operator ID and System every three
seconds: ASTM F3411's minimums for dynamic and static messages. Both are
flags, or keys of a TOML file given with `--config`. `--transport pack`
(default) sends what is due as one message pack per Location, as Bluetooth
5 and Wi-Fi do; `--transport single` sends each message alone, as Bluetooth 4
does, which exercises the ingest's join by transmitter address.

## Faults, for tests

`--drop-rate` drops that fraction of datagrams, `--delay-s` holds each one
back (signed when it is finally sent, as a slow receiver would), and
`--spoof-serial` broadcasts someone else's serial number (U-02).

## Several identities on one vehicle (U-02)

`--sysid` may be given more than once with the same SYSID, each time with
its own `--serial`, `--operator-id` and `--transmitter`: the vehicle then
carries several Remote ID modules, each its own radio. An empty serial
(`--serial ""`) sends no Basic ID, so that module is heard as an
unidentified transmitter (S-32); an empty operator ID sends no Operator ID
message. This is how U-02's four identification statuses are flown on
three SITL vehicles (`docs/runbooks/u02-identification.md`).

Nothing here is a flight instruction: the bridge only listens to SITL.
"""

from __future__ import annotations

import argparse
import heapq
import json
import math
import random
import secrets
import socket
import struct
import sys
import time
import tomllib
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol

from common.geoid import GeoidGrid
from gateway import odid

# The System encoder below is built from these, so it rounds, clamps and
# scales exactly as the pinned encoders in gateway.odid do.
from gateway.odid import (
    _clamp,
    _enc_alt,
    _enc_latlon,
    _header,
    _round,
)
from gateway.remote_id_auth import load_keys, sign

HaeSource = Literal["geoid", "gps"]
Transport = Literal["pack", "single"]

# ODID System message: seconds since 2019-01-01 00:00:00 UTC (gateway.odid).
ODID_EPOCH_S = datetime(2019, 1, 1, tzinfo=UTC).timestamp()

# ICAO standard atmosphere (ISO 2533), troposphere: pressure altitude is
# referenced to 1013.25 hPa, which is what the standard's "pressure altitude"
# means.
_ISA_P0_HPA = 1013.25
_ISA_T0_K = 288.15
_ISA_LAPSE_K_PER_M = 0.0065
_ISA_EXPONENT = 8.31446 * _ISA_LAPSE_K_PER_M / (9.80665 * 0.0289644)

# pymavlink's ardupilotmega dialect: MAV_MODE_FLAG_SAFETY_ARMED,
# MAV_STATE_CRITICAL, MAV_STATE_EMERGENCY, MAV_COMP_ID_AUTOPILOT1. Pinned by
# the tests against the dialect itself.
MAV_MODE_FLAG_SAFETY_ARMED = 128
MAV_STATE_CRITICAL = 5
MAV_STATE_EMERGENCY = 6
MAV_COMP_ID_AUTOPILOT1 = 1
# GLOBAL_POSITION_INT.hdg when the heading is not known.
_UNKNOWN_HDG = 65535

# Values a module fills in that the ingest does not read. The same as
# tools/remote_id_sim.py, so the two simulated sources look alike.
UA_TYPE_MULTIROTOR = 2  # helicopter or multirotor
OPERATOR_ID_TYPE = 0
OPERATOR_LOCATION_TAKEOFF = 0
HORIZ_ACCURACY = 10  # < 10 m
VERT_ACCURACY = 4  # < 10 m
SPEED_ACCURACY = 3  # < 1 m/s

DEFAULT_LOCATION_PERIOD_S = 1.0
DEFAULT_STATIC_PERIOD_S = 3.0
# No position for this long: the module has nothing to broadcast.
DEFAULT_STALE_AFTER_S = 3.0
# Below this the velocity says nothing about the track; the heading is used.
TRACK_MIN_SPEED_MS = 0.5


class Geoid(Protocol):
    def undulation_m(self, lat_deg: float, lon_deg: float) -> float: ...


# --- ODID System message ------------------------------------------------------


def encode_system(message: odid.System) -> bytes:
    """The System message's 25 bytes.

    The layout is `gateway.odid.decode_system`'s, which is read from the
    reference library: flags (location type, classification) at byte 1,
    then `<iiHBHHBHI` from byte 2, and one reserved byte. The tests re-encode
    every System message the reference library encoded and must get its
    bytes back.
    """
    flags = (message.operator_location_type & 0x03) | (
        (message.classification_type & 0x07) << 2
    )
    return (
        bytes([_header(odid.MessageType.SYSTEM), flags])
        + struct.pack(
            "<iiHBHHBHI",
            _enc_latlon(message.operator_lat_deg),
            _enc_latlon(message.operator_lon_deg),
            _clamp(message.area_count, 0, 0xFFFF),
            _clamp(_round(message.area_radius_m / 10), 0, 0xFF),
            _enc_alt(message.area_ceiling_m),
            _enc_alt(message.area_floor_m),
            ((message.category_eu & 0x0F) << 4) | (message.class_eu & 0x0F),
            _enc_alt(message.operator_alt_hae_m),
            _clamp(message.timestamp_s, 0, 0xFFFFFFFF),
        )
        + bytes(1)
    )


# --- what the vehicle says ----------------------------------------------------


@dataclass(frozen=True, slots=True)
class Position:
    time_boot_ms: int
    lat_deg: float
    lon_deg: float
    alt_amsl_m: float
    alt_above_home_m: float
    vn_ms: float
    ve_ms: float
    vd_ms: float
    heading_deg: float | None


def pressure_altitude_m(press_abs_hpa: float) -> float:
    ratio: float = math.pow(press_abs_hpa / _ISA_P0_HPA, _ISA_EXPONENT)
    return (_ISA_T0_K / _ISA_LAPSE_K_PER_M) * (1.0 - ratio)


@dataclass
class VehicleState:
    """The latest of each MAVLink message the broadcast is built from."""

    sysid: int
    position: Position | None = None
    heard_s: float | None = None
    # UTC seconds at boot, from SYSTEM_TIME. None until the vehicle knows UTC.
    boot_unix_s: float | None = None
    # The GPS's own geoid undulation, GPS_RAW_INT.alt_ellipsoid - alt.
    gps_undulation_m: float | None = None
    pressure_alt_m: float | None = None
    armed: bool = False
    system_status: int | None = None
    # Take-off point: lat, lon, AMSL.
    takeoff: tuple[float, float, float] | None = None
    home_from_vehicle: bool = False

    def update(self, msg: Any, *, now_s: float) -> bool:
        """Take one pymavlink message; True if it was this vehicle's."""
        if (
            msg.get_srcSystem() != self.sysid
            or msg.get_srcComponent() != MAV_COMP_ID_AUTOPILOT1
        ):
            return False
        kind = msg.get_type()
        if kind == "GLOBAL_POSITION_INT":
            self.position = Position(
                time_boot_ms=int(msg.time_boot_ms),
                lat_deg=msg.lat / 1e7,
                lon_deg=msg.lon / 1e7,
                alt_amsl_m=msg.alt / 1000.0,
                alt_above_home_m=msg.relative_alt / 1000.0,
                vn_ms=msg.vx / 100.0,
                ve_ms=msg.vy / 100.0,
                vd_ms=msg.vz / 100.0,
                heading_deg=None if msg.hdg == _UNKNOWN_HDG else msg.hdg / 100.0,
            )
            self.heard_s = now_s
            # Where it was while disarmed is where it takes off from; one
            # first seen in the air is placed where it was first seen.
            if self.takeoff is None or (not self.armed and not self.home_from_vehicle):
                self.takeoff = (
                    self.position.lat_deg,
                    self.position.lon_deg,
                    self.position.alt_amsl_m,
                )
        elif kind == "SYSTEM_TIME":
            if msg.time_unix_usec:
                self.boot_unix_s = msg.time_unix_usec / 1e6 - msg.time_boot_ms / 1000.0
        elif kind == "GPS_RAW_INT":
            # alt_ellipsoid is a MAVLink 2 extension: 0 when not sent.
            if msg.alt_ellipsoid:
                self.gps_undulation_m = (msg.alt_ellipsoid - msg.alt) / 1000.0
        elif kind == "SCALED_PRESSURE":
            if msg.press_abs > 0:
                self.pressure_alt_m = pressure_altitude_m(float(msg.press_abs))
        elif kind == "HEARTBEAT":
            self.armed = bool(msg.base_mode & MAV_MODE_FLAG_SAFETY_ARMED)
            self.system_status = int(msg.system_status)
        elif kind == "HOME_POSITION":
            self.takeoff = (
                msg.latitude / 1e7,
                msg.longitude / 1e7,
                msg.altitude / 1000.0,
            )
            self.home_from_vehicle = True
        return True

    def unix_s(self, time_boot_ms: int) -> float | None:
        if self.boot_unix_s is None:
            return None
        return self.boot_unix_s + time_boot_ms / 1000.0


def seconds_after_hour(unix_s: float) -> float:
    """Tenths of a second after the UTC hour, truncated: never 3600.0."""
    return math.floor((unix_s % 3600.0) * 10.0) / 10.0


def odid_status(state: VehicleState) -> int:
    if state.system_status in (MAV_STATE_CRITICAL, MAV_STATE_EMERGENCY):
        return odid.Status.EMERGENCY
    return odid.Status.AIRBORNE if state.armed else odid.Status.GROUND


def track_deg(position: Position) -> float | None:
    if math.hypot(position.vn_ms, position.ve_ms) >= TRACK_MIN_SPEED_MS:
        return math.degrees(math.atan2(position.ve_ms, position.vn_ms)) % 360.0
    return position.heading_deg


# --- the Remote ID module -----------------------------------------------------


@dataclass(frozen=True, slots=True)
class Identity:
    serial: str
    operator_id: str
    ua_type: int = UA_TYPE_MULTIROTOR


@dataclass(frozen=True, slots=True)
class Rates:
    location_period_s: float = DEFAULT_LOCATION_PERIOD_S
    static_period_s: float = DEFAULT_STATIC_PERIOD_S
    stale_after_s: float = DEFAULT_STALE_AFTER_S


@dataclass
class RidModule:
    """What one aircraft's Remote ID module broadcasts, and when."""

    state: VehicleState
    identity: Identity
    geoid: Geoid | None = None
    hae_source: HaeSource = "geoid"
    rates: Rates = field(default_factory=Rates)
    transport: Transport = "pack"
    # U-02: broadcast this serial instead of the aircraft's own.
    spoof_serial: str | None = None
    _next_location_s: float | None = field(default=None, init=False)
    _next_static_s: float | None = field(default=None, init=False)

    def undulation_m(self, lat_deg: float, lon_deg: float) -> float | None:
        if self.hae_source == "gps":
            return self.state.gps_undulation_m
        if self.geoid is None:
            return None
        return self.geoid.undulation_m(lat_deg, lon_deg)

    def basic_id(self) -> bytes:
        return odid.encode_basic_id(
            odid.BasicId(
                id_type=odid.IdType.SERIAL_NUMBER,
                ua_type=self.identity.ua_type,
                ua_id=self.spoof_serial or self.identity.serial,
            )
        )

    def operator_id(self) -> bytes:
        return odid.encode_operator_id(
            odid.OperatorId(
                operator_id_type=OPERATOR_ID_TYPE,
                operator_id=self.identity.operator_id,
            )
        )

    def location(self) -> odid.Location | None:
        position = self.state.position
        if position is None:
            return None
        undulation = self.undulation_m(position.lat_deg, position.lon_deg)
        unix_s = self.state.unix_s(position.time_boot_ms)
        return odid.Location(
            status=odid_status(self.state),
            direction_deg=track_deg(position),
            speed_horizontal_ms=math.hypot(position.vn_ms, position.ve_ms),
            # Up is positive in Remote ID, down in MAVLink.
            speed_vertical_ms=-position.vd_ms,
            lat_deg=position.lat_deg,
            lon_deg=position.lon_deg,
            alt_baro_m=self.state.pressure_alt_m,
            alt_hae_m=None if undulation is None else position.alt_amsl_m + undulation,
            height_reference=odid.HeightReference.OVER_TAKEOFF,
            height_m=position.alt_above_home_m,
            horiz_accuracy=HORIZ_ACCURACY,
            vert_accuracy=VERT_ACCURACY,
            baro_accuracy=VERT_ACCURACY if self.state.pressure_alt_m is not None else 0,
            speed_accuracy=SPEED_ACCURACY,
            ts_accuracy=0,
            seconds_after_hour=None if unix_s is None else seconds_after_hour(unix_s),
        )

    def system(self) -> odid.System | None:
        """None until the vehicle knows UTC and where it took off."""
        position, takeoff = self.state.position, self.state.takeoff
        if position is None or takeoff is None:
            return None
        unix_s = self.state.unix_s(position.time_boot_ms)
        if unix_s is None:
            return None
        lat, lon, alt_amsl = takeoff
        undulation = self.undulation_m(lat, lon)
        return odid.System(
            operator_location_type=OPERATOR_LOCATION_TAKEOFF,
            classification_type=0,
            operator_lat_deg=lat,
            operator_lon_deg=lon,
            area_count=1,
            area_radius_m=0,
            area_ceiling_m=None,
            area_floor_m=None,
            category_eu=0,
            class_eu=0,
            operator_alt_hae_m=None if undulation is None else alt_amsl + undulation,
            timestamp_s=int(unix_s - ODID_EPOCH_S),
        )

    def tick(self, now_s: float) -> list[bytes]:
        """The payloads to broadcast now: none, one pack, or single messages."""
        heard_s = self.state.heard_s
        if heard_s is None or now_s - heard_s > self.rates.stale_after_s:
            return []
        if self._next_location_s is not None and now_s < self._next_location_s:
            return []
        location = self.location()
        if location is None:
            return []
        self._next_location_s = _advance(
            self._next_location_s, self.rates.location_period_s, now_s
        )
        messages = []
        if self._next_static_s is None or now_s >= self._next_static_s:
            self._next_static_s = _advance(
                self._next_static_s, self.rates.static_period_s, now_s
            )
            # U-02: an empty serial is a module that sends no Basic ID, an
            # empty operator ID one that sends no Operator ID.
            if self.spoof_serial or self.identity.serial:
                messages.append(self.basic_id())
            system = self.system()
            if system is not None:
                messages.append(encode_system(system))
            if self.identity.operator_id:
                messages.append(self.operator_id())
        # Last, so that sent singly (Bluetooth 4) the ingest already has the
        # identity and operator when the position arrives.
        messages.append(odid.encode_location(location))
        if self.transport == "single":
            return messages
        return [odid.encode_pack(messages)]


def _advance(previous_s: float | None, period_s: float, now_s: float) -> float:
    # On a fixed schedule, but never trying to catch up a backlog.
    if previous_s is None or now_s - previous_s > period_s:
        return now_s + period_s
    return previous_s + period_s


# --- the receiver -------------------------------------------------------------


@dataclass
class Receiver:
    """Turns a broadcast into the datagram an ESP32 receiver sends."""

    receiver_id: str
    key: bytes | None = None
    rssi_dbm: int = -60
    wall_s: Callable[[], float] = time.time
    nonce: Callable[[], str] = lambda: secrets.token_hex(8)

    def datagram(self, transmitter: str, payload: bytes) -> bytes:
        report: dict[str, Any] = {
            "receiver_id": self.receiver_id,
            "transmitter": transmitter,
            "payload_hex": payload.hex(),
            "rssi_dbm": self.rssi_dbm,
        }
        if self.key is None:
            return json.dumps(report).encode()
        report["sent_at_ms"] = int(self.wall_s() * 1000)
        report["nonce"] = self.nonce()
        return sign(json.dumps(report).encode(), self.key)


@dataclass
class FaultyLink:
    """Between module and receiver: drops and delays, for tests."""

    drop_rate: float = 0.0
    delay_s: float = 0.0
    rng: random.Random = field(default_factory=random.Random)
    dropped: int = field(default=0, init=False)
    _queue: list[tuple[float, int, str, bytes]] = field(
        default_factory=list, init=False
    )
    _order: int = field(default=0, init=False)

    def submit(self, transmitter: str, payload: bytes, *, now_s: float) -> None:
        if self.drop_rate > 0 and self.rng.random() < self.drop_rate:
            self.dropped += 1
            return
        self._order += 1
        heapq.heappush(
            self._queue, (now_s + self.delay_s, self._order, transmitter, payload)
        )

    def due(self, now_s: float) -> list[tuple[str, bytes]]:
        out: list[tuple[str, bytes]] = []
        while self._queue and self._queue[0][0] <= now_s:
            _, _, transmitter, payload = heapq.heappop(self._queue)
            out.append((transmitter, payload))
        return out


def transmitter_for(sysid: int, *, spoofing: bool = False) -> str:
    """A locally administered address per SYSID, stable across runs.

    A spoofer is another radio, so it has another address. The ingest joins
    messages by address, and keeps an identity only while it is fresh: 15 s
    since its Basic ID, unchanged, and no silence over 3 s (S-32). A second
    run on the same address restarted within 3 s, whose first Basic ID is
    lost, would still be joined to the first run's serial; another address
    rules that out for spoofing runs.
    """
    third = 0x17 if spoofing else 0x16
    return f"02:55:{third:02x}:00:{(sysid >> 8) & 0xFF:02x}:{sysid & 0xFF:02x}"


# --- the bridge ---------------------------------------------------------------


class MavlinkSource(Protocol):
    """What the bridge uses of a pymavlink connection: reading, never writing."""

    def recv_match(self, *, blocking: bool) -> Any: ...

    def close(self) -> None: ...


@dataclass
class Vehicle:
    module: RidModule
    source: MavlinkSource
    transmitter: str


@dataclass
class Bridge:
    vehicles: list[Vehicle]
    receiver: Receiver
    link: FaultyLink
    send: Callable[[bytes], None]
    clock_s: Callable[[], float] = time.monotonic
    sent: int = field(default=0, init=False)

    def step(self) -> None:
        """Read everything waiting, broadcast what is due, deliver what is due."""
        now_s = self.clock_s()
        # Vehicles may share a connection (one stream carrying several
        # SYSIDs); each state keeps only its own vehicle's messages.
        for source in self._sources():
            while (msg := source.recv_match(blocking=False)) is not None:
                if msg.get_type() == "BAD_DATA":
                    continue
                for vehicle in self.vehicles:
                    if vehicle.source is source:
                        vehicle.module.state.update(msg, now_s=now_s)
        for vehicle in self.vehicles:
            for payload in vehicle.module.tick(now_s):
                self.link.submit(vehicle.transmitter, payload, now_s=now_s)
        for transmitter, payload in self.link.due(now_s):
            self.send(self.receiver.datagram(transmitter, payload))
            self.sent += 1

    def _sources(self) -> list[MavlinkSource]:
        unique: list[MavlinkSource] = []
        for vehicle in self.vehicles:
            if not any(vehicle.source is seen for seen in unique):
                unique.append(vehicle.source)
        return unique

    def close(self) -> None:
        for source in self._sources():
            source.close()


# --- command line -------------------------------------------------------------


def mavlink_address(args: argparse.Namespace, sysid: int) -> str:
    """Where `make sim` puts instance i (SYSID base + i): UDP 14560 + i, TCP 5760 + 10 i."""
    if args.mavlink is not None:
        return str(args.mavlink)
    index = sysid - args.sysid_base
    if args.link == "tcp":
        return f"tcp:{args.mavlink_host}:{args.tcp_port_base + args.tcp_stride * index}"
    return f"udpin:{args.mavlink_host}:{args.udp_port_base + index}"


def _per_vehicle(values: Sequence[str], sysids: Sequence[int], name: str) -> list[str]:
    """One value per vehicle: given once each, or once as a {sysid} template."""
    if len(values) == len(sysids):
        return [v.format(sysid=s) for v, s in zip(values, sysids, strict=True)]
    if len(values) == 1:
        return [values[0].format(sysid=s) for s in sysids]
    raise ValueError(f"give --{name} once, or once per vehicle ({len(sysids)})")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m tools.sitl_remote_id")
    parser.add_argument("--config", type=Path, help="TOML file of defaults")
    parser.add_argument("--sysid", type=int, action="append", default=[])
    parser.add_argument(
        "--count", type=int, help="SYSIDs --sysid-base .. +count-1, as `make sim`"
    )
    parser.add_argument("--sysid-base", type=int, default=1)
    parser.add_argument(
        "--serial",
        action="append",
        default=[],
        help="per vehicle, or once with {sysid}, e.g. SITLRID{sysid:04d}",
    )
    parser.add_argument(
        "--operator-id",
        action="append",
        default=[],
        help='per vehicle; "" sends no Operator ID message',
    )
    parser.add_argument(
        "--transmitter",
        action="append",
        default=[],
        help="per vehicle: its radio's address; default one per SYSID",
    )
    parser.add_argument("--ua-type", type=int, default=UA_TYPE_MULTIROTOR)
    parser.add_argument("--link", choices=("udp", "tcp"), default="udp")
    parser.add_argument(
        "--mavlink",
        help="one pymavlink address for every vehicle, e.g. udpin:127.0.0.1:14550",
    )
    parser.add_argument("--mavlink-host", default="127.0.0.1")
    parser.add_argument("--udp-port-base", type=int, default=14560)
    parser.add_argument("--tcp-port-base", type=int, default=5760)
    parser.add_argument("--tcp-stride", type=int, default=10)
    parser.add_argument("--host", default="127.0.0.1", help="the ingest")
    parser.add_argument("--port", type=int, default=14600)
    parser.add_argument("--receiver-id", default="sitl-receiver")
    parser.add_argument("--key-file", type=Path)
    parser.add_argument("--rssi-dbm", type=int, default=-60)
    parser.add_argument("--geoid", type=Path)
    parser.add_argument("--hae-source", choices=("geoid", "gps"), default="geoid")
    parser.add_argument("--transport", choices=("pack", "single"), default="pack")
    parser.add_argument(
        "--location-period-s", type=float, default=DEFAULT_LOCATION_PERIOD_S
    )
    parser.add_argument(
        "--static-period-s", type=float, default=DEFAULT_STATIC_PERIOD_S
    )
    parser.add_argument("--stale-after-s", type=float, default=DEFAULT_STALE_AFTER_S)
    parser.add_argument("--drop-rate", type=float, default=0.0)
    parser.add_argument("--delay-s", type=float, default=0.0)
    parser.add_argument("--spoof-serial")
    parser.add_argument("--seed", type=int)
    parser.add_argument("--duration-s", type=float, help="stop after this long")
    parser.add_argument("--poll-s", type=float, default=0.02)
    return parser


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = build_parser()
    first, _ = parser.parse_known_args(argv)
    if first.config is not None:
        # Keys are the flags' names with underscores: location_period_s = 1.0
        defaults = tomllib.loads(first.config.read_text(encoding="utf-8"))
        unknown = sorted(set(defaults) - set(vars(first)))
        if unknown:
            parser.error(f"{first.config}: unknown keys {', '.join(unknown)}")
        parser.set_defaults(**defaults)
    args = parser.parse_args(argv)
    if args.count is not None:
        args.sysid = [args.sysid_base + i for i in range(args.count)]
    if not args.sysid:
        parser.error("give --sysid (repeatable) or --count")
    if not args.serial:
        parser.error("give --serial")
    if not args.operator_id:
        parser.error("give --operator-id")
    if args.hae_source == "geoid" and args.geoid is None:
        parser.error(
            "--geoid is needed to broadcast HAE; give the ingest's grid, "
            "or --hae-source gps (SITL's GPS has no geoid: see the module doc)"
        )
    if not 0.0 <= args.drop_rate < 1.0:
        parser.error("--drop-rate is a fraction in [0, 1)")
    try:
        args.serial = _per_vehicle(args.serial, args.sysid, "serial")
        args.operator_id = _per_vehicle(args.operator_id, args.sysid, "operator-id")
        args.transmitter = (
            _per_vehicle(args.transmitter, args.sysid, "transmitter")
            if args.transmitter
            else [None] * len(args.sysid)
        )
    except ValueError as error:
        parser.error(str(error))
    radios = [
        t or transmitter_for(s, spoofing=args.spoof_serial is not None)
        for s, t in zip(args.sysid, args.transmitter, strict=True)
    ]
    if len(set(radios)) != len(radios):
        parser.error(
            "two modules on one transmitter address would be joined into one "
            "aircraft; give each its own --transmitter"
        )
    return args


def main(
    argv: list[str] | None = None,
    *,
    connect: Callable[[str], MavlinkSource] | None = None,
) -> int:
    args = parse_args(argv)
    key = None
    if args.key_file is not None:
        try:
            keys = load_keys(args.key_file)
        except (OSError, ValueError) as error:
            print(f"error: {error}")
            return 2
        if args.receiver_id not in keys:
            print(f"error: {args.key_file} has no key for {args.receiver_id}")
            return 2
        key = keys[args.receiver_id]
    geoid = GeoidGrid.load(args.geoid) if args.geoid is not None else None
    if connect is None:  # pragma: no cover - the tests pass their own
        from pymavlink import mavutil

        def connect(address: str) -> MavlinkSource:
            source: MavlinkSource = mavutil.mavlink_connection(address)
            return source

    rates = Rates(
        location_period_s=args.location_period_s,
        static_period_s=args.static_period_s,
        stale_after_s=args.stale_after_s,
    )
    vehicles = []
    sources: dict[str, MavlinkSource] = {}
    for sysid, serial, operator_id, radio in zip(
        args.sysid, args.serial, args.operator_id, args.transmitter, strict=True
    ):
        address = mavlink_address(args, sysid)
        module = RidModule(
            state=VehicleState(sysid=sysid),
            identity=Identity(serial, operator_id, args.ua_type),
            geoid=geoid,
            hae_source=args.hae_source,
            rates=rates,
            transport=args.transport,
            spoof_serial=args.spoof_serial,
        )
        if address not in sources:
            sources[address] = connect(address)
        transmitter = radio or transmitter_for(
            sysid, spoofing=args.spoof_serial is not None
        )
        vehicles.append(Vehicle(module, sources[address], transmitter))
        claimed = f" (spoofing {args.spoof_serial})" if args.spoof_serial else ""
        shown = serial or "(no Basic ID)"
        print(f"SYSID {sysid} on {address} -> {shown}{claimed} via {transmitter}")

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        target = (args.host, args.port)

        def send(datagram: bytes) -> None:
            sock.sendto(datagram, target)

        bridge = Bridge(
            vehicles=vehicles,
            receiver=Receiver(args.receiver_id, key, args.rssi_dbm),
            link=FaultyLink(args.drop_rate, args.delay_s, random.Random(args.seed)),
            send=send,
        )
        print(
            f"broadcasting to {args.host}:{args.port} as receiver {args.receiver_id}"
            f" ({'signed' if key else 'unsigned'})"
        )
        started = time.monotonic()
        try:
            while (
                args.duration_s is None or time.monotonic() - started < args.duration_s
            ):
                bridge.step()
                time.sleep(args.poll_s)
        except KeyboardInterrupt:
            pass
        finally:
            bridge.close()
    print(f"sent {bridge.sent} datagrams, dropped {bridge.link.dropped}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""SITL MAVLink in, Open Drone ID broadcasts through the receiver path out. U-16.

Every broadcast is decoded by `gateway.odid`, the ingest's own decoder, and
compared with the MAVLink it was built from. The System encoder, the one
piece of encoding that is not `gateway.odid`'s, is pinned to the reference
library's bytes.
"""

from __future__ import annotations

import base64
import json
import math
import random
import socket
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pymavlink.dialects.v20 import ardupilotmega as dialect

from gateway import odid
from gateway.remote_id import RemoteIdTracker
from gateway.remote_id_auth import (
    AuthenticationError,
    ReceiverAuthenticator,
    load_keys,
)
from gateway.remote_id_ingest import RemoteIdIngest, parse_datagram
from gateway.tests.rid_frames import FlatGeoid
from tools import sitl_remote_id as bridge
from tools.sitl_remote_id import (
    Bridge,
    FaultyLink,
    Identity,
    Rates,
    Receiver,
    RidModule,
    Vehicle,
    VehicleState,
    encode_system,
)

VECTORS: list[dict[str, Any]] = json.loads(
    (
        Path(__file__).parents[2] / "gateway" / "tests" / "data" / "odid_vectors.json"
    ).read_text(encoding="utf-8")
)
SYSID = 3
KEY = bytes(range(32))
# A known UTC instant for the vehicle's clock: 12:34:56.789 on 2026-10-01.
UNIX_S = datetime(2026, 10, 1, 12, 34, 56, 789000, tzinfo=UTC).timestamp()
BOOT_MS = 600_000
UNDULATION_M = FlatGeoid().undulation_m(0.0, 0.0)

_receiver = dialect.MAVLink(None)


def mav(
    name: str, *fields: Any, sysid: int = SYSID, compid: int = 1, **extensions: Any
) -> Any:
    """A MAVLink message as the bridge receives it: packed, then parsed."""
    sender = dialect.MAVLink(None, srcSystem=sysid, srcComponent=compid)
    message = getattr(sender, f"{name}_encode")(*fields, **extensions)
    return _receiver.decode(bytearray(message.pack(sender)))


def position(
    *,
    lat_e7: int = 417151000,
    lon_e7: int = 448271000,
    alt_mm: int = 635_000,
    relative_alt_mm: int = 30_000,
    vx_cms: int = 300,
    vy_cms: int = -400,
    vz_cms: int = -150,
    hdg_cdeg: int = 9000,
    time_boot_ms: int = BOOT_MS,
    sysid: int = SYSID,
) -> Any:
    return mav(
        "global_position_int",
        time_boot_ms,
        lat_e7,
        lon_e7,
        alt_mm,
        relative_alt_mm,
        vx_cms,
        vy_cms,
        vz_cms,
        hdg_cdeg,
        sysid=sysid,
    )


def system_time(unix_s: float = UNIX_S, boot_ms: int = BOOT_MS) -> Any:
    return mav("system_time", int(unix_s * 1e6), boot_ms)


def heartbeat(*, armed: bool, status: int = dialect.MAV_STATE_ACTIVE) -> Any:
    base_mode = dialect.MAV_MODE_FLAG_SAFETY_ARMED if armed else 0
    return mav(
        "heartbeat",
        dialect.MAV_TYPE_QUADROTOR,
        dialect.MAV_AUTOPILOT_ARDUPILOTMEGA,
        base_mode,
        0,
        status,
        3,
    )


def gps_raw(*, alt_mm: int, alt_ellipsoid_mm: int) -> Any:
    return mav(
        "gps_raw_int",
        0,
        3,
        417151000,
        448271000,
        alt_mm,
        100,
        100,
        0,
        0,
        10,
        alt_ellipsoid=alt_ellipsoid_mm,
    )


def module(
    *messages: Any,
    now_s: float = 0.0,
    transport: bridge.Transport = "pack",
    **kwargs: Any,
) -> RidModule:
    kwargs.setdefault("geoid", FlatGeoid())
    rid = RidModule(
        state=VehicleState(sysid=SYSID),
        identity=Identity(serial="SITLRID0003", operator_id="GEO-OP-SITL"),
        transport=transport,
        **kwargs,
    )
    for message in messages:
        rid.state.update(message, now_s=now_s)
    return rid


def flying() -> list[Any]:
    return [heartbeat(armed=True), system_time(), position()]


def decoded(payloads: list[bytes]) -> list[odid.Message]:
    return [m for p in payloads for m in odid.decode(p)]


def one[T](messages: list[odid.Message], kind: type[T]) -> T:
    found = [m for m in messages if isinstance(m, kind)]
    assert len(found) == 1, found
    return found[0]


# --- the System encoder, pinned to the reference library ---------------------


@pytest.mark.parametrize(
    "vector",
    [v for v in VECTORS if v["type"] == "system"],
    ids=lambda v: v["hex"][:12],
)
def test_system_re_encodes_to_the_reference_bytes(vector: dict[str, Any]) -> None:
    raw = bytes.fromhex(vector["hex"])
    assert encode_system(odid.decode_system(raw)).hex() == raw.hex()


def test_the_system_vectors_are_there_to_pin_against() -> None:
    assert sum(v["type"] == "system" for v in VECTORS) >= 40


def test_mavlink_constants_are_the_dialects() -> None:
    assert dialect.MAV_MODE_FLAG_SAFETY_ARMED == bridge.MAV_MODE_FLAG_SAFETY_ARMED
    assert dialect.MAV_STATE_CRITICAL == bridge.MAV_STATE_CRITICAL
    assert dialect.MAV_STATE_EMERGENCY == bridge.MAV_STATE_EMERGENCY
    assert dialect.MAV_COMP_ID_AUTOPILOT1 == bridge.MAV_COMP_ID_AUTOPILOT1


def test_the_mavlink_fields_read_exist_in_the_dialect() -> None:
    """CLAUDE.md rule 4: no invented field names."""
    used = {
        "GLOBAL_POSITION_INT": {
            "time_boot_ms",
            "lat",
            "lon",
            "alt",
            "relative_alt",
            "vx",
            "vy",
            "vz",
            "hdg",
        },
        "SYSTEM_TIME": {"time_unix_usec", "time_boot_ms"},
        "GPS_RAW_INT": {"alt", "alt_ellipsoid"},
        "SCALED_PRESSURE": {"press_abs"},
        "HEARTBEAT": {"base_mode", "system_status"},
        "HOME_POSITION": {"latitude", "longitude", "altitude"},
    }
    by_name = {c.msgname: c for c in dialect.mavlink_map.values()}
    for name, fields in used.items():
        assert fields <= set(by_name[name].fieldnames), name


# --- MAVLink to ODID, decoded by the ingest's decoder -------------------------


@pytest.mark.parametrize(
    ("vx_cms", "vy_cms"),
    [(300, -400), (-300, -400), (0, 1250), (-2000, 5), (1, 0)],
)
def test_a_position_decodes_to_what_the_vehicle_said(vx_cms: int, vy_cms: int) -> None:
    fix = position(vx_cms=vx_cms, vy_cms=vy_cms)
    rid = module(heartbeat(armed=True), system_time(), fix)

    location = one(decoded(rid.tick(0.0)), odid.Location)

    assert location.lat_deg == pytest.approx(fix.lat / 1e7, abs=1e-7)
    assert location.lon_deg == pytest.approx(fix.lon / 1e7, abs=1e-7)
    # Altitudes are in 0.5 m steps.
    assert location.alt_hae_m == pytest.approx(fix.alt / 1000 + UNDULATION_M, abs=0.25)
    assert location.height_m == pytest.approx(fix.relative_alt / 1000, abs=0.25)
    assert location.height_reference == odid.HeightReference.OVER_TAKEOFF
    speed_ms = math.hypot(vx_cms, vy_cms) / 100
    assert location.speed_horizontal_ms == pytest.approx(speed_ms, abs=0.375)
    # Up is positive in Remote ID; vz is down-positive in MAVLink.
    assert location.speed_vertical_ms == pytest.approx(-fix.vz / 100, abs=0.25)
    assert location.status == odid.Status.AIRBORNE
    assert location.direction_deg is not None
    if speed_ms >= bridge.TRACK_MIN_SPEED_MS:
        track = math.degrees(math.atan2(vy_cms, vx_cms)) % 360
        assert abs((location.direction_deg - track + 180) % 360 - 180) <= 0.5
    else:
        # Hovering: the heading, since the velocity has no direction.
        assert location.direction_deg == fix.hdg / 100


def test_the_timestamp_is_the_vehicles_time_after_the_hour() -> None:
    # 200 ms after SYSTEM_TIME was taken, by the vehicle's boot clock.
    rid = module(
        heartbeat(armed=True), system_time(), position(time_boot_ms=BOOT_MS + 200)
    )

    location = one(decoded(rid.tick(0.0)), odid.Location)

    # 34 min 56.989 s after the hour, in tenths, truncated.
    assert location.seconds_after_hour == pytest.approx(34 * 60 + 56.9)


def test_the_timestamp_never_reads_a_full_hour() -> None:
    assert bridge.seconds_after_hour(3600 * 7 - 0.01) == pytest.approx(3599.9)
    assert bridge.seconds_after_hour(3600 * 7) == 0.0


def test_without_the_vehicles_utc_the_timestamp_is_unknown_not_ours() -> None:
    rid = module(heartbeat(armed=True), position())

    messages = decoded(rid.tick(0.0))

    assert one(messages, odid.Location).seconds_after_hour is None
    # The System message carries a timestamp too: it waits for the clock.
    assert not [m for m in messages if isinstance(m, odid.System)]


def test_a_vehicle_with_no_utc_yet_gets_it_from_system_time() -> None:
    """SYSTEM_TIME with a zero time_unix_usec is 'no GPS time yet'."""
    rid = module(heartbeat(armed=True), system_time(unix_s=0.0), position())
    assert rid.state.boot_unix_s is None
    rid.state.update(system_time(), now_s=0.0)
    assert rid.state.boot_unix_s == pytest.approx(UNIX_S - BOOT_MS / 1000)


def test_system_message_carries_the_takeoff_point_and_vehicle_time() -> None:
    rid = module(
        heartbeat(armed=False),
        system_time(),
        position(lat_e7=417000000, lon_e7=448000000, alt_mm=605_000),
        heartbeat(armed=True),
        position(),
    )

    system = one(decoded(rid.tick(0.0)), odid.System)

    assert system.operator_lat_deg == pytest.approx(41.7, abs=1e-7)
    assert system.operator_lon_deg == pytest.approx(44.8, abs=1e-7)
    assert system.operator_alt_hae_m == pytest.approx(605.0 + UNDULATION_M, abs=0.25)
    assert system.operator_location_type == bridge.OPERATOR_LOCATION_TAKEOFF
    assert system.timestamp_s == int(UNIX_S - bridge.ODID_EPOCH_S)


def test_home_position_from_the_vehicle_wins_over_the_disarmed_fix() -> None:
    home = mav(
        "home_position", 410000000, 440000000, 500_000, 0, 0, 0, [1, 0, 0, 0], 0, 0, 0
    )
    rid = module(heartbeat(armed=False), system_time(), home, position())

    system = one(decoded(rid.tick(0.0)), odid.System)

    assert (system.operator_lat_deg, system.operator_lon_deg) == (41.0, 44.0)


def test_an_aircraft_first_seen_flying_takes_off_where_it_was_first_seen() -> None:
    rid = module(heartbeat(armed=True), system_time(), position())
    rid.state.update(position(lat_e7=418000000), now_s=0.0)
    assert rid.state.takeoff is not None
    assert rid.state.takeoff[0] == pytest.approx(41.7151)


@pytest.mark.parametrize(
    ("armed", "status", "expected"),
    [
        (False, dialect.MAV_STATE_STANDBY, odid.Status.GROUND),
        (True, dialect.MAV_STATE_ACTIVE, odid.Status.AIRBORNE),
        (True, dialect.MAV_STATE_CRITICAL, odid.Status.EMERGENCY),
        (True, dialect.MAV_STATE_EMERGENCY, odid.Status.EMERGENCY),
    ],
)
def test_status_follows_the_heartbeat(armed: bool, status: int, expected: int) -> None:
    rid = module(heartbeat(armed=armed, status=status), system_time(), position())
    assert one(decoded(rid.tick(0.0)), odid.Location).status == expected


def test_pressure_altitude_is_the_standard_atmospheres() -> None:
    pressure = mav("scaled_pressure", BOOT_MS, 942.6465, 0.0, 3106)
    rid = module(*flying(), pressure)

    location = one(decoded(rid.tick(0.0)), odid.Location)

    # ISA: 942.65 hPa is about 606 m; 1013.25 hPa is 0 m.
    assert location.alt_baro_m == pytest.approx(606.0, abs=1.0)
    assert bridge.pressure_altitude_m(1013.25) == pytest.approx(0.0, abs=1e-9)
    assert bridge.pressure_altitude_m(898.75) == pytest.approx(1000.0, abs=1.0)


def test_with_the_gps_undulation_hae_is_the_gps_ellipsoid_height() -> None:
    rid = module(
        *flying(),
        gps_raw(alt_mm=635_000, alt_ellipsoid_mm=651_500),
        hae_source="gps",
    )
    location = one(decoded(rid.tick(0.0)), odid.Location)
    assert location.alt_hae_m == pytest.approx(635.0 + 16.5, abs=0.25)


def test_sitls_gps_has_no_geoid_so_its_hae_is_its_amsl() -> None:
    """What --hae-source gps does against SITL: the reason it is not the default."""
    rid = module(
        *flying(), gps_raw(alt_mm=635_000, alt_ellipsoid_mm=635_000), hae_source="gps"
    )
    assert one(decoded(rid.tick(0.0)), odid.Location).alt_hae_m == 635.0


def test_without_an_undulation_the_hae_is_unknown_not_amsl() -> None:
    rid = module(*flying(), hae_source="gps")  # no GPS_RAW_INT.alt_ellipsoid
    rid.state.update(gps_raw(alt_mm=635_000, alt_ellipsoid_mm=0), now_s=0.0)
    assert one(decoded(rid.tick(0.0)), odid.Location).alt_hae_m is None
    assert module(*flying(), geoid=None).location().alt_hae_m is None  # type: ignore[union-attr]


def test_messages_from_other_vehicles_and_components_are_ignored() -> None:
    rid = module(*flying())
    other_vehicle = position(lat_e7=100000000, sysid=SYSID + 1)
    gcs = mav("heartbeat", 6, 8, 0, 0, 0, 3, sysid=255, compid=190)

    assert not rid.state.update(other_vehicle, now_s=0.0)
    assert not rid.state.update(gcs, now_s=0.0)
    assert rid.state.position is not None
    assert rid.state.position.lat_deg == pytest.approx(41.7151)


# --- rates ---------------------------------------------------------------------


def test_location_every_second_and_static_messages_every_three() -> None:
    rid = module(*flying())
    kinds: list[list[type]] = []
    for step in range(140):  # 7 s
        now_s = step * 0.05
        rid.state.heard_s = now_s  # the vehicle keeps talking
        for payload in rid.tick(now_s):
            kinds.append([type(m) for m in odid.decode(payload)])

    assert len(kinds) == 7  # t = 0 .. 6
    with_static = [i for i, k in enumerate(kinds) if odid.BasicId in k]
    assert with_static == [0, 3, 6]
    for i in with_static:
        assert set(kinds[i]) == {
            odid.BasicId,
            odid.Location,
            odid.System,
            odid.OperatorId,
        }
    assert all(
        k == [odid.Location] for i, k in enumerate(kinds) if i not in with_static
    )


def test_the_rates_are_configurable() -> None:
    rid = module(*flying(), rates=Rates(location_period_s=0.25, static_period_s=1.0))
    sent = 0
    for step in range(40):  # 2 s
        rid.state.heard_s = step * 0.05
        sent += len(rid.tick(step * 0.05))
    assert sent == 8


def test_a_silent_vehicle_broadcasts_nothing() -> None:
    rid = module(*flying(), now_s=0.0)
    assert rid.tick(0.0)
    assert rid.tick(1.0)
    assert rid.tick(3.0)
    assert rid.tick(4.0) == []
    assert module().tick(0.0) == []  # nothing heard at all


def test_single_transport_sends_each_message_alone() -> None:
    payloads = module(*flying(), transport="single").tick(0.0)
    assert [len(p) for p in payloads] == [odid.MESSAGE_SIZE] * 4
    # The static messages first, so the ingest's first observation, made
    # when the Location arrives, already has the operator.
    assert [type(odid.decode(p)[0]) for p in payloads] == [
        odid.BasicId,
        odid.System,
        odid.OperatorId,
        odid.Location,
    ]


# --- receiver and faults -------------------------------------------------------


def test_a_signed_datagram_passes_the_ingests_check() -> None:
    receiver = Receiver("sitl-rx", KEY, wall_s=lambda: 1_790_000_000.0)
    datagram = receiver.datagram("02:55:16:00:00:03", b"\x12" * 25)

    auth = ReceiverAuthenticator(keys={"sitl-rx": KEY})
    report = json.loads(auth.check(datagram, now_s=1_790_000_000.0))

    assert report["transmitter"] == "02:55:16:00:00:03"
    assert report["payload_hex"] == "12" * 25
    assert report["rssi_dbm"] == -60
    assert report["sent_at_ms"] == 1_790_000_000_000
    assert report["nonce"]
    # Every datagram has its own nonce, so none is refused as a replay.
    auth.check(
        receiver.datagram("02:55:16:00:00:03", b"\x12" * 25), now_s=1_790_000_000.0
    )


def test_a_datagram_signed_with_another_key_is_refused() -> None:
    datagram = Receiver("sitl-rx", bytes(32), wall_s=time.time).datagram("x", b"\x00")
    with pytest.raises(AuthenticationError, match="bad signature"):
        ReceiverAuthenticator(keys={"sitl-rx": KEY}).check(datagram, now_s=time.time())


def test_without_a_key_the_report_is_unsigned() -> None:
    datagram = Receiver("sitl-rx").datagram("x", b"\x01")
    assert b"sig=" not in datagram
    assert json.loads(datagram)["payload_hex"] == "01"


def test_a_drop_rate_drops_about_that_fraction() -> None:
    link = FaultyLink(drop_rate=0.3, rng=random.Random(7))
    for _ in range(1000):
        link.submit("t", b"x", now_s=0.0)
    delivered = len(link.due(0.0))
    assert link.dropped + delivered == 1000
    assert 250 < link.dropped < 350


def test_with_no_drop_rate_nothing_is_dropped() -> None:
    link = FaultyLink()
    for _ in range(100):
        link.submit("t", b"x", now_s=0.0)
    assert len(link.due(0.0)) == 100
    assert link.dropped == 0


def test_a_delay_holds_a_datagram_back_and_it_is_signed_when_sent() -> None:
    wall = [1_790_000_000.0]
    sent: list[bytes] = []
    clock = [0.0]
    rid = module(*flying())
    rid.state.heard_s = None
    source = FakeSource([heartbeat(armed=True), system_time(), position()])
    b = Bridge(
        vehicles=[Vehicle(rid, source, "02:55:16:00:00:03")],
        receiver=Receiver("sitl-rx", KEY, wall_s=lambda: wall[0]),
        link=FaultyLink(delay_s=2.0),
        send=sent.append,
        clock_s=lambda: clock[0],
    )

    b.step()
    assert sent == []
    clock[0], wall[0] = 1.9, wall[0] + 1.9
    b.step()
    assert sent == []
    clock[0], wall[0] = 2.0, wall[0] + 0.1
    b.step()

    assert len(sent) == 1
    report = json.loads(sent[0].split(b"\nsig=")[0])
    assert report["sent_at_ms"] == int(wall[0] * 1000)


def test_a_spoofed_serial_is_what_the_basic_id_claims() -> None:
    rid = module(*flying(), spoof_serial="1581F5FKD229400B4X")
    assert one(decoded(rid.tick(0.0)), odid.BasicId).ua_id == "1581F5FKD229400B4X"
    assert (
        one(decoded(module(*flying()).tick(0.0)), odid.BasicId).ua_id == "SITLRID0003"
    )


def test_every_vehicle_has_its_own_transmitter_address() -> None:
    addresses = {bridge.transmitter_for(s) for s in range(1, 300)}
    assert len(addresses) == 299
    assert bridge.transmitter_for(3) == "02:55:16:00:00:03"
    assert bridge.transmitter_for(3, spoofing=True) != bridge.transmitter_for(3)


# --- through the ingest --------------------------------------------------------


class FakeSource:
    """A pymavlink connection that yields these messages, then nothing.

    It has no `write`: the bridge calling one would fail the test.
    """

    def __init__(self, messages: list[Any]) -> None:
        self.messages = list(messages)
        self.closed = False

    def recv_match(self, *, blocking: bool) -> Any:
        assert not blocking
        return self.messages.pop(0) if self.messages else None

    def close(self) -> None:
        self.closed = True


class FakeBus:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, Any]]] = []

    async def publish(self, subject: str, payload: bytes) -> None:
        self.sent.append((subject, json.loads(payload)))


@pytest.mark.parametrize("transport", ["pack", "single"])
async def test_through_the_ingest_the_aircraft_is_where_sitl_says(
    transport: bridge.Transport,
) -> None:
    fix = position()
    datagrams: list[bytes] = []
    rid = module(transport=transport)
    b = Bridge(
        vehicles=[
            Vehicle(
                rid,
                FakeSource([heartbeat(armed=True), system_time(), fix]),
                bridge.transmitter_for(SYSID),
            )
        ],
        receiver=Receiver("sitl-rx", KEY),
        link=FaultyLink(),
        send=datagrams.append,
        clock_s=lambda: 0.0,
    )
    b.step()
    bus = FakeBus()
    ingest = RemoteIdIngest(
        tracker=RemoteIdTracker(geoid=FlatGeoid()),
        bus=bus,
        authenticator=ReceiverAuthenticator(keys={"sitl-rx": KEY}),
    )

    for datagram in datagrams:
        await ingest.on_datagram(datagram, "127.0.0.1")

    assert ingest.refused == 0
    assert len(bus.sent) == 1
    subject, seen = bus.sent[0]
    assert subject == f"telemetry.{seen['drone_id']}"
    assert_matches_mavlink(seen, fix)
    assert seen["remote_id"]["operator_id"] == "GEO-OP-SITL"
    assert seen["remote_id"]["operator_lat_deg"] == pytest.approx(fix.lat / 1e7)
    assert seen["station_id"] == "sitl-rx"


def assert_matches_mavlink(seen: dict[str, Any], fix: Any) -> None:
    assert seen["source"] == "remote_id"
    assert seen["label"] == "SITLRID0003"
    assert seen["lat_deg"] == pytest.approx(fix.lat / 1e7, abs=1e-7)
    assert seen["lon_deg"] == pytest.approx(fix.lon / 1e7, abs=1e-7)
    # HAE out through the geoid, back to AMSL through the same geoid.
    assert seen["alt_amsl_m"] == pytest.approx(fix.alt / 1000, abs=0.25)
    assert seen["alt_above_home_m"] == pytest.approx(fix.relative_alt / 1000, abs=0.25)
    assert seen["groundspeed_ms"] == pytest.approx(5.0, abs=0.125)
    assert seen["vx_ms"] == pytest.approx(fix.vx / 100, abs=0.1)
    assert seen["vy_ms"] == pytest.approx(fix.vy / 100, abs=0.1)
    assert seen["vz_ms"] == pytest.approx(fix.vz / 100, abs=0.25)
    assert seen["airborne"] is True


# --- command line --------------------------------------------------------------


def flat_geoid_pgm(path: Path, undulation_m: float) -> Path:
    """A geoid grid that is `undulation_m` everywhere (common/geoid.py format)."""
    width, height = 4, 3
    path.write_bytes(
        b"P5\n# Description flat test grid\n"
        + f"# Offset {undulation_m}\n# Scale 0.001\n{width} {height}\n65535\n".encode()
        + bytes(2 * width * height)
    )
    return path


def test_parse_args_one_serial_template_serves_every_vehicle(tmp_path: Path) -> None:
    args = bridge.parse_args(
        [
            "--count",
            "3",
            "--serial",
            "SITLRID{sysid:04d}",
            "--operator-id",
            "GEO-OP",
            "--geoid",
            str(tmp_path / "g.pgm"),
        ]
    )
    assert args.sysid == [1, 2, 3]
    assert args.serial == ["SITLRID0001", "SITLRID0002", "SITLRID0003"]
    assert args.operator_id == ["GEO-OP"] * 3
    assert [bridge.mavlink_address(args, s) for s in args.sysid] == [
        "udpin:127.0.0.1:14560",
        "udpin:127.0.0.1:14561",
        "udpin:127.0.0.1:14562",
    ]
    tcp = bridge.parse_args([*sys_args(tmp_path), "--link", "tcp", "--sysid", "3"])
    assert bridge.mavlink_address(tcp, 3) == "tcp:127.0.0.1:5780"


def sys_args(tmp_path: Path) -> list[str]:
    return ["--serial", "S{sysid}", "--operator-id", "OP", "--geoid", str(tmp_path)]


def test_rates_and_faults_come_from_a_config_file(tmp_path: Path) -> None:
    config = tmp_path / "rid.toml"
    config.write_text(
        "location_period_s = 0.5\ndrop_rate = 0.1\ntransport = 'single'\n"
    )
    args = bridge.parse_args(
        ["--config", str(config), "--sysid", "1", *sys_args(tmp_path)]
    )
    assert (args.location_period_s, args.drop_rate, args.transport) == (
        0.5,
        0.1,
        "single",
    )
    # A flag still wins over the file.
    args = bridge.parse_args(
        [
            "--config",
            str(config),
            "--sysid",
            "1",
            "--drop-rate",
            "0",
            *sys_args(tmp_path),
        ]
    )
    assert args.drop_rate == 0.0


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["--serial", "S", "--operator-id", "O", "--geoid", "g"], "--sysid"),
        (["--sysid", "1", "--operator-id", "O", "--geoid", "g"], "--serial"),
        (["--sysid", "1", "--serial", "S", "--geoid", "g"], "--operator-id"),
        (["--sysid", "1", "--serial", "S", "--operator-id", "O"], "--geoid"),
        (
            [
                "--sysid",
                "1",
                "--sysid",
                "2",
                "--serial",
                "A",
                "--serial",
                "B",
                "--serial",
                "C",
                "--operator-id",
                "O",
                "--geoid",
                "g",
            ],
            "once per vehicle",
        ),
        (
            [
                "--sysid",
                "1",
                "--serial",
                "S",
                "--operator-id",
                "O",
                "--geoid",
                "g",
                "--drop-rate",
                "1",
            ],
            "fraction",
        ),
    ],
)
def test_parse_args_refuses(
    argv: list[str], message: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit):
        bridge.parse_args(argv)
    assert message in capsys.readouterr().err


def test_a_config_file_with_an_unknown_key_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = tmp_path / "rid.toml"
    config.write_text("location_rate = 1\n")
    with pytest.raises(SystemExit):
        bridge.parse_args(
            ["--config", str(config), "--sysid", "1", *sys_args(tmp_path)]
        )
    assert "unknown keys location_rate" in capsys.readouterr().err


@pytest.fixture
def sink() -> Iterator[socket.socket]:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.settimeout(2.0)
        yield sock


def test_main_bridges_every_vehicle_signed_to_the_ingest_port(
    tmp_path: Path, sink: socket.socket
) -> None:
    keys = tmp_path / "keys"
    keys.write_text(f"sitl-rx: {base64.b64encode(KEY).decode()}\n")
    geoid = flat_geoid_pgm(tmp_path / "flat.pgm", UNDULATION_M)
    sources: dict[str, FakeSource] = {}

    def connect(address: str) -> FakeSource:
        port = int(address.rsplit(":", 1)[1])
        sysid = port - 14560 + 1
        sources[address] = FakeSource(
            [
                mav("heartbeat", 2, 3, 128, 0, 4, 3, sysid=sysid),
                mav("system_time", int(UNIX_S * 1e6), BOOT_MS, sysid=sysid),
                position(sysid=sysid, lat_e7=417151000 + sysid * 1000),
            ]
        )
        return sources[address]

    status = bridge.main(
        [
            "--count",
            "2",
            "--serial",
            "SITLRID{sysid:04d}",
            "--operator-id",
            "GEO-OP",
            "--receiver-id",
            "sitl-rx",
            "--key-file",
            str(keys),
            "--geoid",
            str(geoid),
            "--port",
            str(sink.getsockname()[1]),
            "--duration-s",
            "0.2",
        ],
        connect=connect,
    )

    assert status == 0
    assert all(source.closed for source in sources.values())
    auth = ReceiverAuthenticator(keys=load_keys(keys))
    tracker = RemoteIdTracker(geoid=FlatGeoid())
    labels = {}
    for _ in range(2):
        report = auth.check(sink.recv(4096), now_s=time.time())
        frame = parse_datagram(report, received_at=datetime.now(tz=UTC))
        observation = tracker.take(frame, now_s=0.0)
        assert observation is not None
        labels[observation["label"]] = observation["lat_deg"]
    assert labels == {
        "SITLRID0001": pytest.approx(41.7152),
        "SITLRID0002": pytest.approx(41.7153),
    }


def test_main_refuses_a_receiver_the_key_file_does_not_know(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    keys = tmp_path / "keys"
    keys.write_text(f"other: {base64.b64encode(KEY).decode()}\n")
    status = bridge.main(
        ["--sysid", "1", *sys_args(tmp_path), "--key-file", str(keys)],
        connect=lambda address: FakeSource([]),
    )
    assert status == 2
    assert "has no key for sitl-receiver" in capsys.readouterr().out


def test_main_refuses_a_key_file_that_is_not_there(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    status = bridge.main(
        ["--sysid", "1", *sys_args(tmp_path), "--key-file", str(tmp_path / "none")],
        connect=lambda address: FakeSource([]),
    )
    assert status == 2
    assert capsys.readouterr().out.startswith("error:")


def test_vehicles_sharing_one_stream_each_take_their_own_messages() -> None:
    """--mavlink: one connection carrying several SYSIDs (QGC's fan-out)."""
    shared = FakeSource(
        [
            position(sysid=1, lat_e7=411000000),
            position(sysid=2, lat_e7=412000000),
            heartbeat(armed=True),
            system_time(),
        ]
    )
    rids = {}
    for sysid in (1, 2):
        rids[sysid] = RidModule(
            state=VehicleState(sysid=sysid),
            identity=Identity(f"S{sysid}", "OP"),
            geoid=FlatGeoid(),
        )
    datagrams: list[bytes] = []
    b = Bridge(
        vehicles=[Vehicle(rids[s], shared, bridge.transmitter_for(s)) for s in (1, 2)],
        receiver=Receiver("rx"),
        link=FaultyLink(),
        send=datagrams.append,
        clock_s=lambda: 0.0,
    )

    b.step()
    b.close()

    assert shared.messages == []
    assert shared.closed
    lats = {
        one(
            odid.decode(bytes.fromhex(json.loads(d)["payload_hex"])), odid.BasicId
        ).ua_id: one(
            odid.decode(bytes.fromhex(json.loads(d)["payload_hex"])), odid.Location
        ).lat_deg
        for d in datagrams
    }
    assert lats == {"S1": pytest.approx(41.1), "S2": pytest.approx(41.2)}


def test_one_mavlink_address_serves_every_vehicle(tmp_path: Path) -> None:
    args = bridge.parse_args(
        ["--count", "2", *sys_args(tmp_path), "--mavlink", "udpin:127.0.0.1:14550"]
    )
    assert {bridge.mavlink_address(args, s) for s in args.sysid} == {
        "udpin:127.0.0.1:14550"
    }
    connected: list[str] = []

    def connect(address: str) -> FakeSource:
        connected.append(address)
        return FakeSource([])

    keys = tmp_path / "keys"
    keys.write_text(f"sitl-receiver: {base64.b64encode(KEY).decode()}\n")
    geoid = flat_geoid_pgm(tmp_path / "flat.pgm", UNDULATION_M)
    argv = ["--count", "2", "--serial", "S{sysid}", "--operator-id", "OP"]
    argv += ["--geoid", str(geoid), "--mavlink", "udpin:127.0.0.1:14550"]
    argv += ["--key-file", str(keys), "--duration-s", "0.05", "--port", "9"]
    assert bridge.main(argv, connect=connect) == 0
    assert connected == ["udpin:127.0.0.1:14550"]

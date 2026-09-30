"""Remote ID frames become aircraft on the telemetry bus. P1-15.

The frames are built with `gateway.odid`'s encoder, which tests/test_odid.py
pins to the reference library's bytes.
"""

from __future__ import annotations

import math
from typing import Any
from uuid import uuid4

import pytest

from airspace.cpa import SeparationPolicy
from airspace.monitor import AirspaceMonitor
from gateway import odid
from gateway.remote_id import Frame, RemoteIdTracker, aircraft_id
from gateway.tests.rid_frames import (
    LAT,
    LON,
    NOW,
    FlatGeoid,
    basic,
    frame,
    location,
    pack,
)


def test_a_pack_with_identity_and_position_is_one_observation() -> None:
    tracker = RemoteIdTracker(geoid=FlatGeoid())

    seen = tracker.take(frame(pack(basic(), location())), now_s=0.0)

    assert seen is not None
    assert seen["drone_id"] == str(
        aircraft_id(odid.BasicId(id_type=1, ua_type=2, ua_id="SN-RID-0001"))
    )
    assert seen["label"] == "SN-RID-0001"
    assert seen["source"] == "remote_id"
    assert seen["authenticated"] is False
    assert seen["station_id"] == "rx-1"
    # S-11: stamped when the Gateway received it, on both fields, and never
    # a backlog: nothing queues between a receiver and the Gateway.
    assert seen["ts"] == seen["rx_ts"] == NOW.isoformat()
    assert seen["backlog"] is False
    assert seen["lat_deg"] == pytest.approx(LAT)
    assert seen["lon_deg"] == pytest.approx(LON)
    assert seen["alt_hae_m"] == 520.0
    assert seen["alt_amsl_m"] == 500.0
    assert seen["alt_above_home_m"] == 30.0
    assert seen["remote_id"]["rssi_dbm"] == -71.0


def test_velocity_is_north_east_down_from_track_and_climb() -> None:
    tracker = RemoteIdTracker(geoid=FlatGeoid())

    east = tracker.take(frame(pack(basic(), location(track_deg=90.0))), now_s=0.0)
    north = tracker.take(frame(pack(basic(), location(track_deg=0.0))), now_s=1.0)

    assert east is not None and north is not None
    assert east["vx_ms"] == pytest.approx(0.0, abs=1e-9)
    assert east["vy_ms"] == pytest.approx(10.0)
    assert north["vx_ms"] == pytest.approx(10.0)
    # Climbing at 1 m/s is -1 m/s down.
    assert east["vz_ms"] == -1.0
    assert east["track_deg"] == 90.0
    assert east["heading_deg"] is None


def test_a_speed_without_a_direction_is_not_a_velocity() -> None:
    tracker = RemoteIdTracker(geoid=FlatGeoid())

    seen = tracker.take(frame(pack(basic(), location(track_deg=None))), now_s=0.0)

    assert seen is not None
    assert (seen["vx_ms"], seen["vy_ms"]) == (None, None)
    assert seen["groundspeed_ms"] == 10.0


def test_a_location_waits_for_an_identity_then_is_published() -> None:
    """Bluetooth 4 sends them separately; the address joins them."""
    tracker = RemoteIdTracker(geoid=FlatGeoid())

    held = tracker.take(frame(location()), now_s=0.0)
    completed = tracker.take(frame(basic()), now_s=0.5)

    assert held is None
    assert completed is not None
    assert completed["lat_deg"] == pytest.approx(LAT)


def test_an_identity_from_another_transmitter_does_not_complete_it() -> None:
    tracker = RemoteIdTracker(geoid=FlatGeoid())

    tracker.take(frame(location(), transmitter="AA:00:00:00:00:01"), now_s=0.0)
    other = tracker.take(frame(basic(), transmitter="AA:00:00:00:00:02"), now_s=0.5)

    assert other is None


def test_a_repeated_identity_alone_does_not_republish_a_position() -> None:
    tracker = RemoteIdTracker(geoid=FlatGeoid())

    first = tracker.take(frame(pack(basic(), location())), now_s=0.0)
    again = tracker.take(frame(basic()), now_s=0.5)
    moved = tracker.take(frame(location(lat=LAT + 0.001)), now_s=1.0)

    assert first is not None
    assert again is not None  # a Basic ID completing a held location publishes
    assert moved is not None and moved["lat_deg"] == pytest.approx(LAT + 0.001)


def test_a_forgotten_transmitter_does_not_lend_its_position() -> None:
    tracker = RemoteIdTracker(geoid=FlatGeoid(), forget_after_s=60.0)

    tracker.take(frame(location()), now_s=0.0)
    after_silence = tracker.take(frame(basic()), now_s=61.0)
    in_time = RemoteIdTracker(geoid=FlatGeoid(), forget_after_s=60.0)
    in_time.take(frame(location()), now_s=0.0)
    before_silence = in_time.take(frame(basic()), now_s=59.0)

    assert after_silence is None
    assert before_silence is not None


def test_the_serial_wins_over_a_registration_id() -> None:
    tracker = RemoteIdTracker(geoid=FlatGeoid())

    tracker.take(frame(basic("GEO-OP-77", odid.IdType.CAA_REGISTRATION_ID)), now_s=0.0)
    seen = tracker.take(frame(pack(basic("SN-9"), location())), now_s=0.5)
    later = tracker.take(
        frame(pack(basic("GEO-OP-77", odid.IdType.CAA_REGISTRATION_ID), location())),
        now_s=1.0,
    )

    assert seen is not None and later is not None
    assert seen["label"] == later["label"] == "SN-9"


def test_one_aircraft_heard_by_two_receivers_is_one_id() -> None:
    tracker = RemoteIdTracker(geoid=FlatGeoid())
    a = tracker.take(frame(pack(basic(), location())), now_s=0.0)
    b = tracker.take(
        Frame("rx-2", "11:22:33:44:55:66", NOW, pack(basic(), location()), None),
        now_s=0.0,
    )

    assert a is not None and b is not None
    assert a["drone_id"] == b["drone_id"]
    assert (a["station_id"], b["station_id"]) == ("rx-1", "rx-2")


def test_without_a_geoid_there_is_no_amsl_altitude() -> None:
    """HAE is not AMSL; the difference is the geoid, not zero."""
    seen = RemoteIdTracker(geoid=None).take(frame(pack(basic(), location())), now_s=0.0)

    assert seen is not None
    assert seen["alt_hae_m"] == 520.0
    assert seen["alt_amsl_m"] is None


@pytest.mark.parametrize(
    ("status", "airborne"),
    [
        (odid.Status.GROUND, False),
        (odid.Status.AIRBORNE, True),
        (odid.Status.EMERGENCY, True),
        (odid.Status.UNDECLARED, True),
    ],
)
def test_only_a_declared_ground_status_is_not_airborne(
    status: int, airborne: bool
) -> None:
    seen = RemoteIdTracker(geoid=FlatGeoid()).take(
        frame(pack(basic(), location(status=status))), now_s=0.0
    )

    assert seen is not None
    assert seen["airborne"] is airborne
    assert seen["armed"] is None


def test_height_over_the_ground_is_not_reported_as_above_home() -> None:
    seen = RemoteIdTracker(geoid=FlatGeoid()).take(
        frame(
            pack(basic(), location(height_reference=odid.HeightReference.OVER_GROUND))
        ),
        now_s=0.0,
    )

    assert seen is not None
    assert seen["alt_above_home_m"] is None


def test_garbage_is_refused_not_placed() -> None:
    with pytest.raises(odid.DecodeError):
        RemoteIdTracker().take(frame(b"\x12\x00"), now_s=0.0)


# --- into the airspace monitor --------------------------------------------------


def mavlink(lat: float, lon: float, alt_amsl_m: float) -> dict[str, Any]:
    """A MAVLink aircraft heading west at 10 m/s, as the Gateway publishes it."""
    return {
        "drone_id": str(uuid4()),
        "label": "SITL-01",
        "armed": True,
        "lat_deg": lat,
        "lon_deg": lon,
        "alt_amsl_m": alt_amsl_m,
        "vx_ms": 0.0,
        "vy_ms": -10.0,
        "vz_ms": 0.0,
    }


def policy() -> SeparationPolicy:
    return SeparationPolicy(
        t_cpa_max_s=60.0,
        d_horizontal_min_m=60.0,
        d_vertical_min_m=15.0,
        neighbour_radius_m=2000.0,
    )


def head_on(geoid: FlatGeoid | None) -> list[str]:
    """A Remote ID aircraft flying east at 500 m AMSL (520 m HAE) towards a
    MAVLink one 400 m east of it flying west at 500 m AMSL."""
    monitor = AirspaceMonitor(policy=policy())
    east_m = 400.0
    lon_east = LON + math.degrees(east_m / (6_371_000 * math.cos(math.radians(LAT))))
    # The monitor's clock is the wall clock the frames are stamped with:
    # the MAVLink fixture carries no time and is placed at its arrival.
    wall_s = NOW.timestamp()
    monitor.observe(mavlink(LAT, lon_east, 500.0), now_s=wall_s)
    seen = RemoteIdTracker(geoid=geoid).take(
        frame(pack(basic(), location(climb_ms=0.0))), now_s=0.0
    )
    assert seen is not None
    change = monitor.observe(seen, now_s=wall_s)
    return [alert.key for alert in change.raised]


def test_a_remote_id_aircraft_converging_on_a_mavlink_one_raises_a_conflict() -> None:
    raised = head_on(FlatGeoid())

    assert len(raised) == 1
    assert raised[0].startswith("conflict:")


def test_without_a_geoid_the_monitor_does_not_guess_a_conflict() -> None:
    """The paired absence: same geometry, no AMSL altitude, no evaluation."""
    assert head_on(None) == []


# --- System and Operator ID: carried when sent, absent when not ----------------


def system() -> bytes:
    # The encoder has no System message; the reference vectors do.
    from gateway.tests.test_odid import of_type

    for vector in of_type("system"):
        raw = bytes.fromhex(vector["hex"])
        decoded = odid.decode_system(raw)
        if decoded.operator_lat_deg is not None:
            return raw
    raise AssertionError("no system vector with an operator position")


def operator(operator_id: str = "GEO-OP-1") -> bytes:
    return odid.encode_operator_id(
        odid.OperatorId(operator_id_type=0, operator_id=operator_id)
    )


def test_the_operator_and_their_position_are_carried() -> None:
    tracker = RemoteIdTracker(geoid=FlatGeoid())
    raw_system = system()

    seen = tracker.take(
        frame(pack(basic(), location(), raw_system, operator())), now_s=0.0
    )
    bare = RemoteIdTracker(geoid=FlatGeoid()).take(
        frame(pack(basic(), location())), now_s=0
    )

    assert seen is not None and bare is not None
    expected_system = odid.decode_system(raw_system)
    assert seen["remote_id"]["operator_id"] == "GEO-OP-1"
    assert seen["remote_id"]["operator_lat_deg"] == expected_system.operator_lat_deg
    assert bare["remote_id"]["operator_id"] is None
    assert bare["remote_id"]["operator_lat_deg"] is None


def test_an_operator_id_alone_does_not_republish_a_position() -> None:
    tracker = RemoteIdTracker(geoid=FlatGeoid())

    first = tracker.take(frame(pack(basic(), location())), now_s=0.0)
    operator_only = tracker.take(frame(operator()), now_s=0.5)

    assert first is not None
    assert operator_only is None


def test_an_empty_identity_does_not_replace_a_serial() -> None:
    tracker = RemoteIdTracker(geoid=FlatGeoid())

    tracker.take(frame(basic("SN-1")), now_s=0.0)
    seen = tracker.take(frame(pack(basic("", odid.IdType.NONE), location())), now_s=0.5)

    assert seen is not None and seen["label"] == "SN-1"

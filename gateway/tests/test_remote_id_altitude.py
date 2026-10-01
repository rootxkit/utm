"""Pressure altitude where the geodetic one is missing or poor. S-33.

The frames are built with the encoder that tests/test_odid.py pins to the
reference library's bytes.
"""

from __future__ import annotations

from typing import Any

import pytest
from pymavlink.dialects.v20 import common as mavlink

from airspace.monitor import track_from_telemetry
from gateway.remote_id import (
    ALT_SOURCE_GEODETIC,
    ALT_SOURCE_PRESSURE,
    DEFAULT_MIN_VERTICAL_ACCURACY,
    PRESSURE_ALTITUDE_MODEL,
    RemoteIdTracker,
)
from gateway.remote_id_store import row_from_observation
from gateway.tests.rid_frames import NOW, FlatGeoid, basic, frame, location, pack

# FlatGeoid puts the geoid 20 m above the ellipsoid: 520 m HAE is 500 m AMSL.
HAE_M = 520.0
PRESSURE_M = 507.5


def observe(tracker: RemoteIdTracker | None = None, **fields: Any) -> dict[str, Any]:
    tracker = tracker or RemoteIdTracker(geoid=FlatGeoid())
    seen = tracker.take(frame(pack(basic(), location(**fields))), now_s=0.0)
    assert seen is not None
    return seen


def test_vertical_accuracy_codes_are_the_standards() -> None:
    """MAV_ODID_VER_ACC mirrors the ODID enum the broadcast carries."""
    names = [mavlink.enums["MAV_ODID_VER_ACC"][code].name for code in range(7)]

    assert names == [
        "MAV_ODID_VER_ACC_UNKNOWN",
        "MAV_ODID_VER_ACC_150_METER",
        "MAV_ODID_VER_ACC_45_METER",
        "MAV_ODID_VER_ACC_25_METER",
        "MAV_ODID_VER_ACC_10_METER",
        "MAV_ODID_VER_ACC_3_METER",
        "MAV_ODID_VER_ACC_1_METER",
    ]
    assert DEFAULT_MIN_VERTICAL_ACCURACY == mavlink.MAV_ODID_VER_ACC_45_METER


def test_a_good_geodetic_altitude_is_used() -> None:
    seen = observe(alt_hae_m=HAE_M, alt_baro_m=PRESSURE_M, vert_accuracy=4)

    assert seen["alt_amsl_m"] == pytest.approx(500.0)
    assert seen["alt_source"] == ALT_SOURCE_GEODETIC
    assert seen["alt_pressure_m"] == pytest.approx(PRESSURE_M)


def test_a_missing_geodetic_altitude_falls_back_to_pressure() -> None:
    """The broadcast's unknown value (-1000 m) decodes to None."""
    seen = observe(alt_hae_m=None, alt_baro_m=PRESSURE_M)

    assert seen["alt_hae_m"] is None
    assert seen["alt_amsl_m"] == pytest.approx(PRESSURE_M)
    assert seen["alt_source"] == ALT_SOURCE_PRESSURE


@pytest.mark.parametrize(
    ("accuracy", "source"),
    [
        (mavlink.MAV_ODID_VER_ACC_150_METER, ALT_SOURCE_PRESSURE),
        (mavlink.MAV_ODID_VER_ACC_45_METER, ALT_SOURCE_GEODETIC),
        (mavlink.MAV_ODID_VER_ACC_1_METER, ALT_SOURCE_GEODETIC),
        # Not known is not flagged: the geodetic altitude stays.
        (mavlink.MAV_ODID_VER_ACC_UNKNOWN, ALT_SOURCE_GEODETIC),
    ],
)
def test_a_geodetic_altitude_flagged_inaccurate_falls_back_to_pressure(
    accuracy: int, source: str
) -> None:
    seen = observe(alt_hae_m=HAE_M, alt_baro_m=PRESSURE_M, vert_accuracy=accuracy)

    assert seen["alt_source"] == source
    expected = PRESSURE_M if source == ALT_SOURCE_PRESSURE else 500.0
    assert seen["alt_amsl_m"] == pytest.approx(expected)


def test_the_threshold_is_configurable() -> None:
    strict = RemoteIdTracker(geoid=FlatGeoid(), min_vertical_accuracy=5)

    seen = observe(strict, alt_hae_m=HAE_M, alt_baro_m=PRESSURE_M, vert_accuracy=4)

    assert seen["alt_source"] == ALT_SOURCE_PRESSURE


def test_with_neither_altitude_there_is_none() -> None:
    seen = observe(alt_hae_m=None, alt_baro_m=None)

    assert seen["alt_amsl_m"] is None
    assert seen["alt_source"] is None


def test_a_flagged_geodetic_altitude_without_pressure_is_not_used() -> None:
    seen = observe(alt_hae_m=HAE_M, alt_baro_m=None, vert_accuracy=1)

    assert seen["alt_amsl_m"] is None
    assert seen["alt_source"] is None


def test_pressure_is_not_a_substitute_for_a_missing_geoid() -> None:
    seen = observe(RemoteIdTracker(geoid=None), alt_baro_m=PRESSURE_M)

    assert seen["alt_amsl_m"] is None
    assert seen["alt_source"] is None


def test_the_stored_row_names_pressure_as_its_height_model() -> None:
    payload = pack(basic(), location(alt_hae_m=None, alt_baro_m=PRESSURE_M))
    pressure = RemoteIdTracker(geoid=FlatGeoid()).take(frame(payload), now_s=0.0)
    geodetic = observe()
    assert pressure is not None

    pressure_row = row_from_observation(
        pressure, ts=NOW, payload=payload, geoid_model="EGM2008"
    )
    geodetic_row = row_from_observation(
        geodetic, ts=NOW, payload=payload, geoid_model="EGM2008"
    )

    assert pressure_row.alt_amsl_m == pytest.approx(PRESSURE_M)
    assert pressure_row.geoid_model == PRESSURE_ALTITUDE_MODEL
    assert geodetic_row.geoid_model == "EGM2008"


def test_the_monitor_evaluates_an_aircraft_on_pressure_altitude() -> None:
    """Without the fallback this aircraft had no AMSL altitude, no track."""
    without = observe(alt_hae_m=None, alt_baro_m=None)
    with_pressure = observe(alt_hae_m=None, alt_baro_m=PRESSURE_M)

    arrived_s = NOW.timestamp()
    assert track_from_telemetry(without, arrived_at_s=arrived_s) is None
    track = track_from_telemetry(with_pressure, arrived_at_s=arrived_s)
    assert track is not None
    assert track.alt_amsl_m == pytest.approx(PRESSURE_M)

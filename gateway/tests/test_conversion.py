"""MAVLink to SI.

The altitude tests are the ones with consequences. CLAUDE.md's rule is that
AGL and AMSL are never mixed and the field name always says which, and the
reason is that getting it wrong produces data that looks entirely normal and
an aircraft at the wrong height.

Expected values here are written in SI and compared against the conversion, so
a wrong scaling factor cannot hide behind an expectation written in the same
wrong units.
"""

from __future__ import annotations

import pytest
from pymavlink.dialects.v20 import ardupilotmega as mavlink

from gateway.conversion import (
    EKF_POS_HORIZ_ABS,
    JOULES_PER_WATT_HOUR,
    Position,
    air_data_from_vfr_hud,
    battery_from_battery_status,
    battery_from_sys_status,
    gps_from_gps_raw_int,
    horizontal_position_is_valid,
    position_from_global_position_int,
)
from gateway.units import INT16_MAX, UINT8_MAX, UINT16_MAX

# Tbilisi, 450 m AMSL, 60 m above home, heading 90 degrees.
TBILISI_LAT_E7 = 417_151_000
TBILISI_LON_E7 = 448_271_000


def global_position(**overrides: object) -> object:
    fields: dict[str, object] = {
        "time_boot_ms": 0,
        "lat": TBILISI_LAT_E7,
        "lon": TBILISI_LON_E7,
        "alt": 450_000,
        "relative_alt": 60_000,
        "vx": 1_000,
        "vy": -250,
        "vz": 150,
        "hdg": 9_000,
    }
    fields.update(overrides)
    return mavlink.MAVLink_global_position_int_message(**fields)


def gps_raw(**overrides: object) -> object:
    fields: dict[str, object] = {
        "time_usec": 0,
        "fix_type": 3,
        "lat": TBILISI_LAT_E7,
        "lon": TBILISI_LON_E7,
        "alt": 450_000,
        "eph": 120,
        "epv": 130,
        "vel": 1_234,
        "cog": 4_500,
        "satellites_visible": 14,
    }
    fields.update(overrides)
    return mavlink.MAVLink_gps_raw_int_message(**fields)


def sys_status(**overrides: object) -> object:
    fields: dict[str, object] = {
        "onboard_control_sensors_present": 0,
        "onboard_control_sensors_enabled": 0,
        "onboard_control_sensors_health": 0,
        "load": 500,
        "voltage_battery": 12_600,
        "current_battery": 350,
        "battery_remaining": 87,
        "drop_rate_comm": 0,
        "errors_comm": 0,
        "errors_count1": 0,
        "errors_count2": 0,
        "errors_count3": 0,
        "errors_count4": 0,
    }
    fields.update(overrides)
    return mavlink.MAVLink_sys_status_message(**fields)


def battery_status(**overrides: object) -> object:
    fields: dict[str, object] = {
        "id": 0,
        "battery_function": 0,
        "type": 0,
        "temperature": 2_500,
        # A 4S pack: four real cells, six unused, marked UINT16_MAX.
        "voltages": [3_800, 3_810, 3_790, 3_800, *([UINT16_MAX] * 6)],
        "current_battery": 350,
        "current_consumed": 1_500,
        "energy_consumed": 720,
        "battery_remaining": 87,
        "time_remaining": 0,
        "charge_state": 0,
        # voltages_ext marks unused cells with 0, NOT with UINT16_MAX.
        "voltages_ext": [0, 0, 0, 0],
    }
    fields.update(overrides)
    return mavlink.MAVLink_battery_status_message(**fields)


def vfr_hud(**overrides: object) -> object:
    fields: dict[str, object] = {
        "airspeed": 12.0,
        "groundspeed": 12.5,
        "heading": 90,
        "throttle": 45,
        "alt": 450.0,
        "climb": 1.5,
    }
    fields.update(overrides)
    return mavlink.MAVLink_vfr_hud_message(**fields)


# --- position --------------------------------------------------------------


def test_latitude_and_longitude_become_degrees() -> None:
    position = position_from_global_position_int(global_position())

    assert position.lat_deg == pytest.approx(41.7151)
    assert position.lon_deg == pytest.approx(44.8271)


def test_the_two_altitudes_are_different_values_with_different_names() -> None:
    """The conversion that would be invisible if it were wrong.

    450 m AMSL and 60 m above home are the same aircraft at one instant. A
    converter that put either number in the other field would produce a
    perfectly plausible track - and one of them is 390 m out.
    """
    position = position_from_global_position_int(global_position())

    assert position.alt_amsl_m == pytest.approx(450.0)
    assert position.alt_above_home_m == pytest.approx(60.0)
    assert position.alt_amsl_m != position.alt_above_home_m


def test_there_is_no_agl_field_because_nothing_carries_one() -> None:
    """`relative_alt` is "Altitude above home", per pymavlink's own XML.

    That equals AGL only while the terrain under the aircraft is at home's
    elevation. Naming it alt_agl_m would make an aircraft over rising ground
    look higher above it than it is, smoothly and without any signal.
    """
    assert not hasattr(
        Position(41.0, 44.0, True, 1.0, 2.0, None, None, None, None), "alt_agl_m"
    )
    assert "alt_above_home_m" in Position.__slots__


def test_velocities_become_metres_per_second_keeping_sign() -> None:
    """vz is positive *down*, as MAVLink sends it.

    Flipping it here would make every climb read as a descent somewhere
    downstream, and the number would still look reasonable.
    """
    position = position_from_global_position_int(global_position())

    assert position.vx_ms == pytest.approx(10.0)
    assert position.vy_ms == pytest.approx(-2.5)
    assert position.vz_ms == pytest.approx(1.5)


def test_heading_becomes_degrees() -> None:
    assert position_from_global_position_int(
        global_position()
    ).heading_deg == pytest.approx(90.0)


def test_an_unknown_heading_is_none_not_655_degrees() -> None:
    position = position_from_global_position_int(global_position(hdg=UINT16_MAX))
    assert position.heading_deg is None
    # Everything else still converts: one unknown field is not a broken message.
    assert position.lat_deg == pytest.approx(41.7151)


def test_an_invalid_position_is_none_never_zero_zero() -> None:
    """Unknown is not a value.

    ArduPilot sends `lat = lon = 0` before the EKF has an origin - observed on
    a real aircraft, every GLOBAL_POSITION_INT for the whole indoor session.
    Storing that writes a coordinate indistinguishable from a real one, so it
    is only wrong at the point where somebody forgets to check: the map draws
    a delivery in the Gulf of Guinea, P4 computes a distance to it.
    """
    position = position_from_global_position_int(
        global_position(lat=0, lon=0), horizontal_valid=False
    )

    assert position.lat_deg is None
    assert position.lon_deg is None
    assert position.horizontal_valid is False


def test_an_invalid_position_keeps_everything_else() -> None:
    """The aircraft is present and most of what it says is real.

    Heading comes from the compass and works indoors with no fix at all;
    altitude is a separate EKF estimate with its own flag. Dropping the row
    would discard real measurements to avoid one unknown.
    """
    position = position_from_global_position_int(
        global_position(lat=0, lon=0), horizontal_valid=False
    )

    assert position.heading_deg == pytest.approx(90.0)
    assert position.alt_amsl_m == pytest.approx(450.0)
    assert position.alt_above_home_m == pytest.approx(60.0)
    assert position.vz_ms == pytest.approx(1.5)


def test_a_real_position_at_zero_would_be_kept_when_the_ekf_says_so() -> None:
    """The paired presence test, and the reason the EKF flag is the authority.

    0,0 *is* a real place. If the EKF claims an absolute horizontal position
    there, it is stored - the rule rejects positions the aircraft says are
    invalid, not coordinates somebody finds implausible.
    """
    position = position_from_global_position_int(
        global_position(lat=0, lon=0), horizontal_valid=True
    )

    assert position.lat_deg == 0.0
    assert position.lon_deg == 0.0
    assert position.horizontal_valid is True


def test_without_an_ekf_report_an_exact_zero_is_still_refused() -> None:
    """§6.4 forbids requiring EKF_STATUS_REPORT to arrive at all.

    So the fallback is what ArduPilot actually sends with no origin, and it
    resolves to None rather than to a stored coordinate. A real position is
    unaffected.
    """
    absent = position_from_global_position_int(global_position(lat=0, lon=0))
    real = position_from_global_position_int(global_position())

    assert absent.lat_deg is None
    assert real.lat_deg == pytest.approx(41.7151)
    assert real.horizontal_valid is True


# --- gps -------------------------------------------------------------------


def test_gps_quality_converts() -> None:
    gps = gps_from_gps_raw_int(gps_raw())

    assert gps.fix_type == 3
    assert gps.satellites_visible == 14
    assert gps.hdop == pytest.approx(1.2)
    assert gps.vdop == pytest.approx(1.3)
    assert gps.ground_speed_ms == pytest.approx(12.34)
    assert gps.alt_amsl_m == pytest.approx(450.0)


def test_gps_unknowns_resolve_to_none() -> None:
    gps = gps_from_gps_raw_int(
        gps_raw(
            eph=UINT16_MAX,
            epv=UINT16_MAX,
            vel=UINT16_MAX,
            satellites_visible=UINT8_MAX,
        )
    )

    assert gps.hdop is None
    assert gps.vdop is None
    assert gps.ground_speed_ms is None
    assert gps.satellites_visible is None
    # The fix type is not optional and still arrives.
    assert gps.fix_type == 3


def test_gps_altitude_is_msl_not_above_home() -> None:
    """GPS_RAW_INT has no above-home field at all.

    Its `alt` is MSL, so it lands in alt_amsl_m and nowhere else.
    """
    gps = gps_from_gps_raw_int(gps_raw(alt=123_456))
    assert gps.alt_amsl_m == pytest.approx(123.456)


# --- battery ---------------------------------------------------------------


def test_sys_status_battery_converts() -> None:
    battery = battery_from_sys_status(sys_status())

    assert battery.remaining_pct == pytest.approx(87.0)
    assert battery.voltage_v == pytest.approx(12.6)
    assert battery.current_a == pytest.approx(3.5)


def test_sys_status_unknowns_resolve_to_none() -> None:
    battery = battery_from_sys_status(
        sys_status(voltage_battery=UINT16_MAX, current_battery=-1, battery_remaining=-1)
    )

    assert battery.voltage_v is None
    assert battery.current_a is None
    assert battery.remaining_pct is None


def test_pack_voltage_sums_only_the_cells_that_exist() -> None:
    """A 4S pack, six unused cells marked UINT16_MAX.

    Summing blindly gives about 405 V, which is not obviously wrong on a
    screen that just shows a number.
    """
    battery = battery_from_battery_status(battery_status())
    assert battery.voltage_v == pytest.approx(15.2, abs=0.01)


def test_voltages_ext_marks_unused_cells_with_zero_not_uint16_max() -> None:
    """The two arrays disagree, per MAVLink's own field descriptions.

    A single "unused" constant would be right for one array and wrong for the
    other. Here four extension cells are present and must be added.
    """
    with_extension = battery_from_battery_status(
        battery_status(voltages_ext=[3_800, 3_800, 0, 0])
    )
    assert with_extension.voltage_v == pytest.approx(22.8, abs=0.01)


def test_energy_becomes_watt_hours() -> None:
    """BATTERY_STATUS sends hecto-joules; the fleet budgets in watt-hours.

    720 hJ = 72,000 J = 20 Wh. The factor is exact by definition, so the
    expectation is computed from the definition rather than pasted.
    """
    battery = battery_from_battery_status(battery_status(energy_consumed=720))
    assert battery.consumed_wh == pytest.approx(72_000 / JOULES_PER_WATT_HOUR)
    assert battery.consumed_wh == pytest.approx(20.0)


def test_consumed_charge_becomes_amp_hours() -> None:
    battery = battery_from_battery_status(battery_status(current_consumed=1_500))
    assert battery.consumed_ah == pytest.approx(1.5)


def test_battery_temperature_converts_and_has_its_own_sentinel() -> None:
    assert battery_from_battery_status(
        battery_status()
    ).temperature_degc == pytest.approx(25.0)
    assert (
        battery_from_battery_status(
            battery_status(temperature=INT16_MAX)
        ).temperature_degc
        is None
    )


def test_percent_and_energy_are_kept_separate() -> None:
    """CLAUDE.md: "battery percent 0-100 and watt-hours separately".

    Percent is what a pilot reads; energy is what an endurance estimate needs,
    and percent depends on a discharge curve the autopilot chose.
    """
    battery = battery_from_battery_status(battery_status())
    assert battery.remaining_pct == pytest.approx(87.0)
    assert battery.consumed_wh == pytest.approx(20.0)
    assert battery.remaining_pct != battery.consumed_wh


# --- air data --------------------------------------------------------------


def test_vfr_hud_is_already_si_and_is_not_rescaled() -> None:
    """Its fields are m/s, deg, m and %: the factor is 1.

    A converter that applied a prefix rule anyway would divide a 12.5 m/s
    ground speed by a hundred and produce a plausible hover.
    """
    air = air_data_from_vfr_hud(vfr_hud())

    assert air.airspeed_ms == pytest.approx(12.0)
    assert air.groundspeed_ms == pytest.approx(12.5)
    assert air.heading_deg == pytest.approx(90.0)
    assert air.throttle_pct == pytest.approx(45.0)
    assert air.climb_ms == pytest.approx(1.5)


def test_vfr_hud_altitude_is_msl() -> None:
    """ "Current altitude (MSL)", per the XML. Not above home, not above ground."""
    assert air_data_from_vfr_hud(vfr_hud()).alt_amsl_m == pytest.approx(450.0)


def test_vfr_hud_and_global_position_agree_on_amsl() -> None:
    """Two messages, one datum, one field name.

    If either conversion put the wrong altitude in alt_amsl_m, these would
    disagree for an aircraft that is not at its home elevation - which is the
    only time it matters.
    """
    air = air_data_from_vfr_hud(vfr_hud(alt=450.0))
    position = position_from_global_position_int(global_position(alt=450_000))

    assert air.alt_amsl_m == pytest.approx(position.alt_amsl_m)
    assert position.alt_above_home_m == pytest.approx(60.0)


# --- the EKF flag that decides it ------------------------------------------


def test_the_ekf_flag_comes_from_pymavlinks_enum() -> None:
    """Derived, not written as 16.

    Cross-checked against the enum by name, because this single bit is what
    separates a real position from a placeholder; a wrong-but-plausible value
    would make the distinction silently useless.
    """
    assert EKF_POS_HORIZ_ABS == mavlink.EKF_POS_HORIZ_ABS
    assert EKF_POS_HORIZ_ABS == 1 << 4


def test_the_observed_indoor_flags_report_no_horizontal_position() -> None:
    """The exact value a real aircraft sent, from the raw archive.

    0xa7 = ATTITUDE | VELOCITY_HORIZ | VELOCITY_VERT | POS_VERT_ABS
         | CONST_POS_MODE

    POS_HORIZ_ABS clear and CONST_POS_MODE set: ArduPilot holding a constant
    position because it has no horizontal source.
    """
    observed = 0xA7

    assert horizontal_position_is_valid(observed) is False
    assert observed & mavlink.EKF_CONST_POS_MODE
    assert observed & mavlink.EKF_ATTITUDE, "heading was valid throughout"
    assert observed & mavlink.EKF_POS_VERT_ABS, "altitude was valid throughout"


def test_the_same_flags_with_horizontal_position_are_valid() -> None:
    """The paired presence test: the check must not reject everything."""
    assert horizontal_position_is_valid(0xA7 | mavlink.EKF_POS_HORIZ_ABS) is True


def test_a_gps_fix_type_is_not_the_test() -> None:
    """GLOBAL_POSITION_INT is the EKF's fused estimate, not raw GPS.

    On the observed aircraft GPS_RAW_INT reported fix_type 1 with a stale
    lat/lon in Zambia while the EKF correctly reported no horizontal position
    and sent zeroes. Reading either message as the authority on the other
    gives the wrong answer.
    """
    assert horizontal_position_is_valid(mavlink.EKF_POS_HORIZ_ABS) is True
    assert horizontal_position_is_valid(0) is False

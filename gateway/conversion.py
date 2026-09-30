"""MAVLink messages to SI values, at the parser boundary and nowhere else.

P1-03. Every scaling factor comes from `gateway/units.py`, which derives it
from pymavlink's own field metadata; nothing here writes `1e7` or `1000`.

## Altitude is the dangerous one

CLAUDE.md: *"Altitude: store both AMSL and AGL. **Always state which in the
field name.** Mission planning uses AGL. Never mix them in the same
calculation."* That rule exists because an altitude conversion error looks
entirely normal in the data and puts an aircraft at the wrong height.

Reading pymavlink's XML definitions rather than remembering what the fields
mean turns up something the rule does not cover:

| Field | pymavlink's own description | What it is |
|---|---|---|
| `GLOBAL_POSITION_INT.alt` | "Altitude (MSL)" | AMSL |
| `GLOBAL_POSITION_INT.relative_alt` | **"Altitude above home"** | above *home*, not above ground |
| `GPS_RAW_INT.alt` | "Altitude (MSL)" | AMSL |
| `GPS_RAW_INT.alt_ellipsoid` | "Altitude (above WGS84, EGM96 ellipsoid)" | a third datum again |
| `VFR_HUD.alt` | "Current altitude (MSL)" | AMSL |

**Nothing on the hot path carries height above ground.** `relative_alt` is
height above the home point, which equals AGL only while the ground under the
aircraft is at the same elevation as home. Over rising terrain it overstates
clearance, and it does so smoothly and plausibly - exactly the failure the
naming rule is written against.

So this module produces `alt_amsl_m` and `alt_above_home_m`, and it does not
produce `alt_agl_m` at all. Filling `drone_state.alt_agl_m` needs terrain
elevation under the aircraft, which is a data source this system does not yet
have. Naming the field honestly is the part that can be done today; inventing
the value is not.

## Sentinels resolve to None, before scaling

MAVLink says "unknown" in-band. A missing value is a missing value - §6.4 is
explicit that a message not arriving is not an error - and `None` is how that
reaches the rest of the pipeline. What must never happen is `UINT16_MAX`
surviving into a heading as 655.35 degrees.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

from pymavlink.dialects.v20 import ardupilotmega as mavlink

from gateway.units import is_sentinel, to_si


@dataclass(frozen=True, slots=True)
class Position:
    """A position fix, in SI, with both altitude datums named.

    `lat_deg` and `lon_deg` are `None` when the EKF has no absolute horizontal
    position. They are never `0, 0`: a coordinate that cannot be distinguished
    from a real one is worse than no coordinate, because it is only wrong at
    the point where somebody forgot to check.
    """

    lat_deg: float | None
    lon_deg: float | None
    # False when the position is absent. Carried explicitly so a consumer can
    # tell "the aircraft has not told us where it is" from "this row predates
    # the field", without inferring either from a null.
    horizontal_valid: bool
    # Above mean sea level.
    alt_amsl_m: float | None
    # Above the home point, NOT above ground. Equal to AGL only while the
    # terrain under the aircraft matches home's elevation. There is no
    # alt_agl_m here because nothing in the telemetry carries one.
    alt_above_home_m: float | None
    heading_deg: float | None
    # Ground velocity, north / east / down. Down is positive, as MAVLink
    # sends it; flipping the sign here would make every climb read as a
    # descent somewhere downstream.
    vx_ms: float | None
    vy_ms: float | None
    vz_ms: float | None


@dataclass(frozen=True, slots=True)
class GpsQuality:
    """What the GPS reports about itself."""

    fix_type: int
    satellites_visible: int | None
    # Horizontal and vertical dilution of precision. Unitless in the wire
    # format ("unitless * 100"), so the factor is applied here rather than
    # through the unit table, which only covers dimensioned fields.
    hdop: float | None
    vdop: float | None
    ground_speed_ms: float | None
    alt_amsl_m: float | None


@dataclass(frozen=True, slots=True)
class BatteryState:
    """Battery, with percent and energy kept separate.

    CLAUDE.md: "battery percent 0-100 and watt-hours separately". A percentage
    is what a pilot reads; energy is what an endurance estimate needs, and the
    two are not interchangeable because percent depends on a curve the
    autopilot chose.
    """

    remaining_pct: float | None
    voltage_v: float | None
    current_a: float | None
    consumed_ah: float | None
    consumed_wh: float | None
    temperature_degc: float | None


@dataclass(frozen=True, slots=True)
class AirData:
    """VFR_HUD: speeds and altitude as the autopilot presents them."""

    airspeed_ms: float | None
    groundspeed_ms: float | None
    heading_deg: float | None
    throttle_pct: float | None
    alt_amsl_m: float | None
    climb_ms: float | None


# Joules per watt-hour. Exact by definition (1 Wh = 3600 J), not a measured
# constant, so it is written here rather than derived: BATTERY_STATUS reports
# hecto-joules and the fleet's energy budget is in watt-hours.
JOULES_PER_WATT_HOUR = 3600.0

# GPS dilution of precision is sent as "unitless * 100" - documented in the
# field description rather than in a unit string, so `units.py` has no rule
# for it and the factor is applied explicitly here.
DOP_SCALE = 0.01

# Read from pymavlink's enum, not written as 16. The whole point of the flag is
# that it is the one signal distinguishing a real position from a placeholder,
# and a wrong-but-plausible bit would make that distinction silently useless.
EKF_POS_HORIZ_ABS: Final[int] = int(mavlink.EKF_POS_HORIZ_ABS)

# Set when ArduPilot is holding a constant position because it has no
# horizontal source. Not used as the test - POS_HORIZ_ABS is - but recorded
# because it is the corroborating flag in the observation above.
EKF_CONST_POS_MODE: Final[int] = int(mavlink.EKF_CONST_POS_MODE)


def horizontal_position_is_valid(ekf_flags: int) -> bool:
    """Whether the EKF claims an absolute horizontal position.

    `GLOBAL_POSITION_INT` is the EKF's *fused estimate*, not raw GPS, so the
    GPS fix type is the wrong test: the EKF can hold a position through a brief
    GPS outage, and it can lack one while the GPS reports a fix.
    `EKF_POS_HORIZ_ABS` is the flag that says this estimate is referenced to
    the earth.

    Observed on a real aircraft indoors on 2026-09-24, from the raw archive:

        EKF flags 0xa7 = ATTITUDE | VELOCITY_HORIZ | VELOCITY_VERT
                       | POS_VERT_ABS | CONST_POS_MODE

    `POS_HORIZ_ABS` clear, `CONST_POS_MODE` set - ArduPilot holding a constant
    position because it has no horizontal source - and every
    `GLOBAL_POSITION_INT` in that period carried `lat = lon = 0`. Meanwhile
    `GPS_RAW_INT` reported `fix_type = 1` with a stale lat/lon in Zambia, which
    is exactly why the GPS message is not the authority here.
    """
    return bool(ekf_flags & EKF_POS_HORIZ_ABS)


def position_from_global_position_int(
    message: Any, *, horizontal_valid: bool | None = None
) -> Position:
    """GLOBAL_POSITION_INT -> SI, with both altitude datums named.

    **A position that is not valid is `None`, never `0, 0`.** Unknown is not a
    value. ArduPilot sends zeroes before the EKF has an origin, and storing
    them would put a coordinate in the database that cannot be told apart from
    a real one without consulting another column - so every consumer would have
    to remember a rule the row can enforce once. The map would draw a delivery
    in the Gulf of Guinea; P4's nearest-drone query would compute a distance to
    it.

    Nothing is lost: the archive holds the raw frame either way.

    `horizontal_valid` comes from the EKF (see `horizontal_position_is_valid`).
    `None` means no `EKF_STATUS_REPORT` has been seen yet, which §6.4 requires
    be survivable - nothing may depend on a message arriving at a given rate.
    In that case the fallback is an exact `0, 0`, which is not a heuristic
    about "null island" being implausible so much as the value ArduPilot
    actually sends when it has no origin. It is used only in the absence of the
    authoritative signal, and it too resolves to `None` rather than to a
    stored coordinate.
    """
    lat_deg = to_si(message, "lat")
    lon_deg = to_si(message, "lon")

    if horizontal_valid is None:
        horizontal_valid = not (lat_deg == 0.0 and lon_deg == 0.0)

    return Position(
        lat_deg=lat_deg if horizontal_valid else None,
        lon_deg=lon_deg if horizontal_valid else None,
        horizontal_valid=horizontal_valid,
        # Vertical position is a separate estimate with its own EKF flag, and
        # it is commonly valid while horizontal is not - barometric altitude
        # needs no GPS. The observed aircraft had POS_VERT_ABS set throughout.
        alt_amsl_m=to_si(message, "alt"),
        alt_above_home_m=to_si(message, "relative_alt"),
        # Heading comes from the compass and is valid indoors with no fix at
        # all. It is the field the hardware check actually exercised.
        heading_deg=to_si(message, "hdg"),
        vx_ms=to_si(message, "vx"),
        vy_ms=to_si(message, "vy"),
        vz_ms=to_si(message, "vz"),
    )


def gps_from_gps_raw_int(message: Any) -> GpsQuality:
    """GPS_RAW_INT -> SI."""
    eph = message.eph
    epv = message.epv
    return GpsQuality(
        fix_type=int(message.fix_type),
        satellites_visible=_int_or_none(message, "satellites_visible"),
        hdop=None if _unknown(message, "eph") else float(eph) * DOP_SCALE,
        vdop=None if _unknown(message, "epv") else float(epv) * DOP_SCALE,
        ground_speed_ms=to_si(message, "vel"),
        alt_amsl_m=to_si(message, "alt"),
    )


def battery_from_sys_status(message: Any) -> BatteryState:
    """SYS_STATUS -> SI.

    The lightweight battery view. BATTERY_STATUS carries more, but SYS_STATUS
    is what arrives on every link, and §6.4 forbids depending on either.
    """
    return BatteryState(
        remaining_pct=to_si(message, "battery_remaining"),
        voltage_v=to_si(message, "voltage_battery"),
        current_a=to_si(message, "current_battery"),
        consumed_ah=None,
        consumed_wh=None,
        temperature_degc=None,
    )


def battery_from_battery_status(message: Any) -> BatteryState:
    """BATTERY_STATUS -> SI, including the energy consumed."""
    energy_j = to_si(message, "energy_consumed")
    return BatteryState(
        remaining_pct=to_si(message, "battery_remaining"),
        # `voltages` is per-cell, so pack voltage is their sum. Cells above the
        # battery's actual count are sent as UINT16_MAX, which must not be
        # added in - that would put a 65 V cell on a 4S pack.
        voltage_v=_pack_voltage(message),
        current_a=to_si(message, "current_battery"),
        consumed_ah=to_si(message, "current_consumed"),
        consumed_wh=None if energy_j is None else energy_j / JOULES_PER_WATT_HOUR,
        temperature_degc=to_si(message, "temperature"),
    )


def air_data_from_vfr_hud(message: Any) -> AirData:
    """VFR_HUD -> SI. Its altitude is MSL, not above ground."""
    return AirData(
        airspeed_ms=to_si(message, "airspeed"),
        groundspeed_ms=to_si(message, "groundspeed"),
        heading_deg=to_si(message, "heading"),
        throttle_pct=to_si(message, "throttle"),
        alt_amsl_m=to_si(message, "alt"),
        climb_ms=to_si(message, "climb"),
    )


# --- helpers ---------------------------------------------------------------

_MILLIVOLTS_PER_VOLT = 1000.0


def _pack_voltage(message: Any) -> float | None:
    """Sum the cells that are actually present.

    `voltages` is a fixed array of ten and `voltages_ext` of four, and **the
    two mark unused entries differently**: `voltages` uses UINT16_MAX,
    `voltages_ext` uses 0. That asymmetry is in MAVLink's own field
    descriptions and was found by the test that reads them, not by reading
    this code - a single "unused" constant would have been right for one array
    and wrong for the other.

    Summing blindly gives a pack voltage in the hundreds, which is not
    obviously wrong on a screen that just shows a number.

    Summing rather than taking the maximum also handles the spillover rule: a
    pack above UINT16_MAX-1 millivolts is sent as 65534 in cell 0 with the
    remainder in cell 1, and neither of those is a sentinel.
    """
    total_mv = 0
    present = False
    for field in ("voltages", "voltages_ext"):
        values = getattr(message, field, None)
        if values is None:
            continue
        for value in values:
            millivolts = int(value)
            if is_sentinel(type(message), field, millivolts):
                continue
            total_mv += millivolts
            present = True

    return total_mv / _MILLIVOLTS_PER_VOLT if present else None


def _unknown(message: Any, field_name: str) -> bool:
    return is_sentinel(type(message), field_name, getattr(message, field_name))


def _int_or_none(message: Any, field_name: str) -> int | None:
    if _unknown(message, field_name):
        return None
    return int(getattr(message, field_name))


def _require(value: float | None, field_name: str) -> float:
    if value is None:  # pragma: no cover - no sentinel is defined for these
        raise ValueError(f"{field_name} unexpectedly resolved to unknown")
    return value

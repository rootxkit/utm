"""Closest point of approach between two aircraft. P5-07, `ARCHITECTURE.md` §6.2.

    rel_pos = p2 - p1
    rel_vel = v2 - v1
    t_cpa   = -(rel_pos . rel_vel) / |rel_vel|^2     if |rel_vel| > 0
    d_cpa   = |rel_pos + rel_vel * t_cpa|

Computed in a local tangent plane in metres, not in degrees. Positions come in
as WGS84 latitude/longitude and are projected about the midpoint of the pair;
at the distances this is used for (the 800 m neighbour radius, P5-06) the
error of that projection is centimetres, far below GPS error.

**Horizontal and vertical are separate.** §6.2 alerts on horizontal distance
at CPA *and* altitude difference, so the CPA time comes from the horizontal
motion and the vertical separation is evaluated at that time. A single 3-D
CPA would let a 200 m climb hide a head-on horizontal conflict.

Altitude is AMSL throughout (CLAUDE.md, §6.1): two aircraft are only
comparable against the same datum, and `alt_above_home_m` is relative to each
aircraft's own home.

Velocities are MAVLink's `GLOBAL_POSITION_INT` convention, checked against
`common.xml`: `vx` positive north, `vy` positive east, `vz` positive **down**.

## Two edge cases, both decided rather than left to arithmetic

- **Diverging** (`t_cpa < 0`): the closest approach is in the past, so the
  closest point from now on is now. `t_cpa` is reported as 0 and the distances
  as the current ones. A negative time would read to a pilot as "already
  happened, safe", which is true only if they are also far apart now.
- **Zero relative velocity**: every time is equally close. `t_cpa` is 0 and the
  distance is the current, constant one - the formula would divide by zero.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from uuid import UUID

# WGS84 semi-major axis and flattening, for the local radii of curvature.
_WGS84_A_M = 6_378_137.0
_WGS84_F = 1 / 298.257223563
_WGS84_E2 = _WGS84_F * (2 - _WGS84_F)

# Below this relative speed the pair is treated as not closing at all. Well
# under what a GPS velocity can resolve, so it only catches true zero.
_MIN_REL_SPEED_MS = 1e-6


@dataclass(frozen=True, slots=True)
class Track:
    """One aircraft's latest state, as the airspace service holds it."""

    drone_id: UUID
    lat_deg: float
    lon_deg: float
    alt_amsl_m: float
    vn_ms: float
    ve_ms: float
    # Positive down, as MAVLink sends it.
    vd_ms: float
    # When the sample was taken, as epoch seconds on one clock for every
    # aircraft: the Gateway's receive time of the batch (`rx_ts`), which is
    # within the delivery delay of the capture and, unlike the station's own
    # clock, comparable across stations (S-11). Two tracks are only
    # comparable at one instant, so the older is advanced to the newer
    # (`advance`) before the CPA.
    captured_at_s: float
    # Who captured it: the ground station or Remote ID receiver whose clock
    # `ts` came from. Samples are ordered only within a source.
    source: str = "unknown"
    # The station's own capture clock (`ts`), for ordering within a source;
    # None when the message carried none. Never compared across sources.
    source_ts_s: float | None = None


def _radii_m(lat_deg: float) -> tuple[float, float]:
    """WGS84 meridional and prime-vertical radii of curvature at a latitude."""
    sin_phi = math.sin(math.radians(lat_deg))
    denominator = math.sqrt(1 - _WGS84_E2 * sin_phi * sin_phi)
    prime_vertical_m = _WGS84_A_M / denominator
    meridional_m = _WGS84_A_M * (1 - _WGS84_E2) / denominator**3
    return meridional_m, prime_vertical_m


def advance(track: Track, dt_s: float) -> Track:
    """The track carried `dt_s` forward (or back, for a negative `dt_s`) along
    its velocity, as a straight line.

    Where a neighbour's latest sample is older than the subject's, this is
    what the CPA is computed from: a 5 s old sample of an aircraft at 15 m/s
    is 75 m from where the aircraft is, against a 60 m threshold. A straight
    line is the same assumption the CPA itself makes.
    """
    if dt_s == 0.0:
        return track
    meridional_m, prime_vertical_m = _radii_m(track.lat_deg)
    lat_deg = track.lat_deg + math.degrees(track.vn_ms * dt_s / meridional_m)
    east_per_deg_m = prime_vertical_m * math.cos(math.radians(track.lat_deg))
    lon_deg = track.lon_deg + math.degrees(track.ve_ms * dt_s / east_per_deg_m)
    # Wrapped into [-180, 180) so a track advanced across the antimeridian is
    # still a longitude.
    lon_deg = (lon_deg + 180.0) % 360.0 - 180.0
    return Track(
        drone_id=track.drone_id,
        lat_deg=lat_deg,
        lon_deg=lon_deg,
        # Altitude up is AMSL; velocity down is positive, so it subtracts.
        alt_amsl_m=track.alt_amsl_m - track.vd_ms * dt_s,
        vn_ms=track.vn_ms,
        ve_ms=track.ve_ms,
        vd_ms=track.vd_ms,
        captured_at_s=track.captured_at_s + dt_s,
        source=track.source,
        source_ts_s=track.source_ts_s,
    )


@dataclass(frozen=True, slots=True)
class Approach:
    """The closest approach of a pair, from now on."""

    first: UUID
    second: UUID
    t_cpa_s: float
    d_cpa_horizontal_m: float
    # Absolute altitude difference at t_cpa.
    d_alt_at_cpa_m: float
    d_horizontal_now_m: float
    d_alt_now_m: float


def local_offset_m(
    lat0_deg: float, lon0_deg: float, lat_deg: float, lon_deg: float
) -> tuple[float, float]:
    """North and east offset of a point from an origin, in metres.

    Uses the WGS84 meridional and prime-vertical radii at the origin's
    latitude: a local tangent plane, accurate to far better than GPS over a
    few kilometres.
    """
    phi = math.radians(lat0_deg)
    meridional_m, prime_vertical_m = _radii_m(lat0_deg)
    north_m = math.radians(lat_deg - lat0_deg) * meridional_m
    # Longitude difference wrapped into (-180, 180], so a pair straddling the
    # antimeridian is not 40,000 km apart.
    dlon_deg = (lon_deg - lon0_deg + 180.0) % 360.0 - 180.0
    east_m = math.radians(dlon_deg) * prime_vertical_m * math.cos(phi)
    return north_m, east_m


def horizontal_distance_m(a: Track, b: Track) -> float:
    lat0 = (a.lat_deg + b.lat_deg) / 2
    lon0 = a.lon_deg
    an, ae = local_offset_m(lat0, lon0, a.lat_deg, a.lon_deg)
    bn, be = local_offset_m(lat0, lon0, b.lat_deg, b.lon_deg)
    return math.hypot(bn - an, be - ae)


def closest_approach(a: Track, b: Track) -> Approach:
    """The pair's closest approach from now, per §6.2.

    "Now" is the later of the two capture times; the older sample is advanced
    to it first (S-11). `t_cpa_s` counts from that instant.
    """
    if a.captured_at_s < b.captured_at_s:
        a = advance(a, b.captured_at_s - a.captured_at_s)
    elif b.captured_at_s < a.captured_at_s:
        b = advance(b, a.captured_at_s - b.captured_at_s)
    # Origin at the midpoint latitude, so neither aircraft is favoured by the
    # projection.
    lat0 = (a.lat_deg + b.lat_deg) / 2
    lon0 = a.lon_deg
    an, ae = local_offset_m(lat0, lon0, a.lat_deg, a.lon_deg)
    bn, be = local_offset_m(lat0, lon0, b.lat_deg, b.lon_deg)

    rel_n = bn - an
    rel_e = be - ae
    rel_vn = b.vn_ms - a.vn_ms
    rel_ve = b.ve_ms - a.ve_ms

    rel_speed_sq = rel_vn * rel_vn + rel_ve * rel_ve
    if rel_speed_sq < _MIN_REL_SPEED_MS * _MIN_REL_SPEED_MS:
        t_cpa_s = 0.0
    else:
        t_cpa_s = max(0.0, -(rel_n * rel_vn + rel_e * rel_ve) / rel_speed_sq)

    cpa_n = rel_n + rel_vn * t_cpa_s
    cpa_e = rel_e + rel_ve * t_cpa_s

    # Altitude up = AMSL; velocity down is positive, so it subtracts.
    alt_a_at = a.alt_amsl_m - a.vd_ms * t_cpa_s
    alt_b_at = b.alt_amsl_m - b.vd_ms * t_cpa_s

    return Approach(
        first=a.drone_id,
        second=b.drone_id,
        t_cpa_s=t_cpa_s,
        d_cpa_horizontal_m=math.hypot(cpa_n, cpa_e),
        d_alt_at_cpa_m=abs(alt_b_at - alt_a_at),
        d_horizontal_now_m=math.hypot(rel_n, rel_e),
        d_alt_now_m=abs(b.alt_amsl_m - a.alt_amsl_m),
    )


@dataclass(frozen=True, slots=True)
class SeparationPolicy:
    """§6.2's alert thresholds. No defaults in code: they are airspace policy,
    held in the database where a change is audited (see `airspace/policy.py`).
    """

    t_cpa_max_s: float
    d_horizontal_min_m: float
    d_vertical_min_m: float
    neighbour_radius_m: float

    def is_conflict(self, approach: Approach) -> bool:
        """Alert when the pair is inside both minima now, or will be at its
        closest approach within the window (all three of §6.2).

        The first clause is what the SITL run found missing: two aircraft
        hovering 30 m apart have a relative velocity of centimetres per
        second of GPS noise, which puts their "closest approach" hundreds of
        seconds away and outside the window, and §6.2's three-part test
        alone let the alert clear as resolved while they were still 30 m
        apart. A pair inside the minima is in conflict whatever the
        arithmetic says about when it will be closest. The same clause holds
        a diverging pair's alert until it is actually past the minimum.
        """
        inside_now = (
            approach.d_horizontal_now_m < self.d_horizontal_min_m
            and approach.d_alt_now_m < self.d_vertical_min_m
        )
        closing_inside = (
            approach.t_cpa_s < self.t_cpa_max_s
            and approach.d_cpa_horizontal_m < self.d_horizontal_min_m
            and approach.d_alt_at_cpa_m < self.d_vertical_min_m
        )
        return inside_now or closing_inside

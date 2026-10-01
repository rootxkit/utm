"""Distance on the WGS-84 ellipsoid, for circular zones. U-03.

A circular zone's radius is a distance on the ground, and PostGIS draws it
with a buffer on `geography`, which is the ellipsoid. Judging it on a
sphere instead is off by up to 0.5 %: 5 m at the edge of a 1 km zone. So
the monitor uses Vincenty's inverse formula on WGS-84 (T. Vincenty, "Direct
and inverse solutions of geodesics on the ellipsoid", Survey Review 23,
1975), accurate to well under a millimetre at these distances. It is pinned
by `airspace/tests/test_geodesy.py` against values published independently
of this code.

Vincenty's iteration fails to converge only for nearly antipodal points,
thousands of kilometres from any zone; that raises rather than answering.
"""

from __future__ import annotations

import math

# WGS-84.
A_M = 6_378_137.0
F = 1 / 298.257223563
B_M = A_M * (1 - F)

_MAX_ITERATIONS = 200
_TOLERANCE = 1e-12


class GeodesyError(ArithmeticError):
    """The inverse problem did not converge (nearly antipodal points)."""


def distance_m(
    lat1_deg: float, lon1_deg: float, lat2_deg: float, lon2_deg: float
) -> float:
    """The geodesic distance between two points on WGS-84, in metres."""
    if lat1_deg == lat2_deg and lon1_deg == lon2_deg:
        return 0.0
    u1 = math.atan((1 - F) * math.tan(math.radians(lat1_deg)))
    u2 = math.atan((1 - F) * math.tan(math.radians(lat2_deg)))
    big_l = math.radians(lon2_deg - lon1_deg)
    sin_u1, cos_u1 = math.sin(u1), math.cos(u1)
    sin_u2, cos_u2 = math.sin(u2), math.cos(u2)
    lam = big_l
    for _ in range(_MAX_ITERATIONS):
        sin_lam, cos_lam = math.sin(lam), math.cos(lam)
        sin_sigma = math.hypot(
            cos_u2 * sin_lam, cos_u1 * sin_u2 - sin_u1 * cos_u2 * cos_lam
        )
        if sin_sigma == 0.0:
            return 0.0
        cos_sigma = sin_u1 * sin_u2 + cos_u1 * cos_u2 * cos_lam
        sigma = math.atan2(sin_sigma, cos_sigma)
        sin_alpha = cos_u1 * cos_u2 * sin_lam / sin_sigma
        cos2_alpha = 1 - sin_alpha**2
        # On the equator cos2_alpha is 0 and the term is not used.
        cos_2sm = (
            cos_sigma - 2 * sin_u1 * sin_u2 / cos2_alpha if cos2_alpha != 0 else 0.0
        )
        c = F / 16 * cos2_alpha * (4 + F * (4 - 3 * cos2_alpha))
        previous = lam
        lam = big_l + (1 - c) * F * sin_alpha * (
            sigma + c * sin_sigma * (cos_2sm + c * cos_sigma * (-1 + 2 * cos_2sm**2))
        )
        if abs(lam - previous) < _TOLERANCE:
            break
    else:
        raise GeodesyError("Vincenty's inverse did not converge")
    u_sq = cos2_alpha * (A_M**2 - B_M**2) / B_M**2
    big_a = 1 + u_sq / 16384 * (4096 + u_sq * (-768 + u_sq * (320 - 175 * u_sq)))
    big_b = u_sq / 1024 * (256 + u_sq * (-128 + u_sq * (74 - 47 * u_sq)))
    delta_sigma = (
        big_b
        * sin_sigma
        * (
            cos_2sm
            + big_b
            / 4
            * (
                cos_sigma * (-1 + 2 * cos_2sm**2)
                - big_b / 6 * cos_2sm * (-3 + 4 * sin_sigma**2) * (-3 + 4 * cos_2sm**2)
            )
        )
    )
    return B_M * big_a * (sigma - delta_sigma)

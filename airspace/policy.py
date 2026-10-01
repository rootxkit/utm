"""Separation policy and the height limit from the database. P5-07, P5-19.

The thresholds are airspace policy, edited by operators and audited, so they
live in the relational database (`airspace_policy`, one row) and never in
code or the environment (`airspace/config.py`).
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine

from airspace.cpa import SeparationPolicy


class PolicyMissingError(RuntimeError):
    """No policy row. The service refuses to guess one."""


_POLICY = sa.text(
    """
    SELECT t_cpa_max_s, d_horizontal_min_m, d_vertical_min_m, neighbour_radius_m
    FROM airspace_policy WHERE id = 1
    """
)


async def load_policy(engine: AsyncEngine) -> SeparationPolicy:
    async with engine.connect() as connection:
        row = (await connection.execute(_POLICY)).one_or_none()
    if row is None:
        raise PolicyMissingError(
            "airspace_policy has no row; run the relational migrations"
        )
    return SeparationPolicy(
        t_cpa_max_s=float(row.t_cpa_max_s),
        d_horizontal_min_m=float(row.d_horizontal_min_m),
        d_vertical_min_m=float(row.d_vertical_min_m),
        neighbour_radius_m=float(row.neighbour_radius_m),
    )


_CONDITIONAL_SEVERITY = sa.text(
    "SELECT conditional_zone_severity FROM airspace_policy WHERE id = 1"
)


async def load_conditional_zone_severity(engine: AsyncEngine) -> str:
    """What a CONDITIONAL zone raises: "info" or "warning" (U-03)."""
    async with engine.connect() as connection:
        row = (await connection.execute(_CONDITIONAL_SEVERITY)).one_or_none()
    if row is None:
        raise PolicyMissingError(
            "airspace_policy has no row; run the relational migrations"
        )
    return str(row.conditional_zone_severity)


_HEIGHT_LIMIT = sa.text("SELECT max_height_agl_m FROM airspace_policy WHERE id = 1")


async def load_height_limit(engine: AsyncEngine) -> float | None:
    """The maximum height above ground, in metres; None when none is set."""
    async with engine.connect() as connection:
        row = (await connection.execute(_HEIGHT_LIMIT)).one_or_none()
    if row is None:
        raise PolicyMissingError(
            "airspace_policy has no row; run the relational migrations"
        )
    return None if row.max_height_agl_m is None else float(row.max_height_agl_m)

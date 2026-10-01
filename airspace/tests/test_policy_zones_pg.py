"""Policy and zones as the service loads them, from the real database."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine

from airspace.ed269 import Restriction, VerticalReference
from airspace.policy import (
    PolicyMissingError,
    load_conditional_zone_severity,
    load_height_limit,
    load_policy,
)
from airspace.zones import Limit, load_zones

pytestmark = pytest.mark.postgres

SQUARE = "POLYGON((44.80 41.70, 44.82 41.70, 44.82 41.72, 44.80 41.72, 44.80 41.70))"


@pytest.fixture
async def zones(relational_engine: AsyncEngine) -> AsyncIterator[None]:
    async with relational_engine.begin() as connection:
        for name, kind, restriction, identifier in (
            ("pg-no-fly", "geozone", "PROHIBITED", "PGNOFLY"),
            ("pg-info", "geozone", "NO_RESTRICTION", "PGINFO"),
            ("pg-corridor", "corridor", "NO_RESTRICTION", "PGCORR"),
        ):
            await connection.execute(
                sa.text(
                    "INSERT INTO airspace_zones "
                    "(name, type, geom, identifier, country, ed269_type, "
                    " restriction, zone_authority, applicability, uom_dimensions, "
                    " lower_limit, lower_reference, upper_limit, upper_reference) "
                    "VALUES (:n, :t, ST_GeomFromText(:w, 4326), :i, 'GEO', "
                    " 'COMMON', :r, '[]', '[{\"permanent\": \"YES\"}]', 'M', "
                    " 400, 'AMSL', 700, 'AMSL')"
                ),
                {"n": name, "t": kind, "w": SQUARE, "r": restriction, "i": identifier},
            )
    yield
    async with relational_engine.begin() as connection:
        await connection.execute(
            sa.text("DELETE FROM airspace_zones WHERE name LIKE 'pg-%'")
        )


async def test_the_seeded_policy_is_the_stage_0_policy(
    relational_engine: AsyncEngine,
) -> None:
    policy = await load_policy(relational_engine)
    assert (
        policy.t_cpa_max_s,
        policy.d_horizontal_min_m,
        policy.d_vertical_min_m,
        policy.neighbour_radius_m,
    ) == (60, 60, 20, 800)


async def test_the_seeded_height_limit_is_120_m(
    relational_engine: AsyncEngine,
) -> None:
    assert await load_height_limit(relational_engine) == 120.0


async def test_the_height_limit_can_be_unset_but_not_zero(
    relational_engine: AsyncEngine,
) -> None:
    try:
        async with relational_engine.begin() as connection:
            await connection.execute(
                sa.text("UPDATE airspace_policy SET max_height_agl_m = NULL")
            )
        assert await load_height_limit(relational_engine) is None
        with pytest.raises(sa.exc.IntegrityError):
            async with relational_engine.begin() as connection:
                await connection.execute(
                    sa.text("UPDATE airspace_policy SET max_height_agl_m = 0")
                )
    finally:
        async with relational_engine.begin() as connection:
            await connection.execute(
                sa.text("UPDATE airspace_policy SET max_height_agl_m = 120")
            )


async def test_a_second_policy_row_is_refused(relational_engine: AsyncEngine) -> None:
    with pytest.raises(sa.exc.IntegrityError):
        async with relational_engine.begin() as connection:
            await connection.execute(
                sa.text(
                    "INSERT INTO airspace_policy (id, t_cpa_max_s, d_horizontal_min_m, "
                    "d_vertical_min_m, neighbour_radius_m) VALUES (2, 30, 60, 20, 800)"
                )
            )


async def test_a_missing_policy_is_refused_rather_than_guessed(
    relational_engine: AsyncEngine,
) -> None:
    """The service must not start on invented thresholds. The row is put back
    afterwards: the test database is shared by the session."""
    async with relational_engine.begin() as connection:
        saved = (
            await connection.execute(sa.text("SELECT * FROM airspace_policy"))
        ).one()
        await connection.execute(sa.text("DELETE FROM airspace_policy"))
    try:
        with pytest.raises(PolicyMissingError):
            await load_policy(relational_engine)
        with pytest.raises(PolicyMissingError):
            await load_height_limit(relational_engine)
    finally:
        async with relational_engine.begin() as connection:
            await connection.execute(
                sa.text(
                    "INSERT INTO airspace_policy (id, t_cpa_max_s, d_horizontal_min_m, "
                    "d_vertical_min_m, neighbour_radius_m, max_height_agl_m) "
                    "VALUES (:id, :t, :h, :v, :r, :limit)"
                ),
                {
                    "id": saved.id,
                    "t": saved.t_cpa_max_s,
                    "h": saved.d_horizontal_min_m,
                    "v": saved.d_vertical_min_m,
                    "r": saved.neighbour_radius_m,
                    "limit": saved.max_height_agl_m,
                },
            )


async def test_zones_load_with_their_limits_and_only_those_that_restrict(
    relational_engine: AsyncEngine, zones: None
) -> None:
    loaded = [
        z
        for z in await load_zones(relational_engine)
        if (z.name or "").startswith("pg-")
    ]

    # NO_RESTRICTION zones and corridors are drawn, never alerted on.
    assert [z.name for z in loaded] == ["pg-no-fly"]
    zone = loaded[0]
    assert zone.restriction is Restriction.PROHIBITED
    assert zone.lower == Limit(400.0, VerticalReference.AMSL)
    assert zone.upper == Limit(700.0, VerticalReference.AMSL)
    assert zone.contains_horizontally(41.71, 44.81)
    assert not zone.contains_horizontally(41.73, 44.81)


async def test_the_conditional_zone_severity_is_seeded_warning_and_can_be_info(
    relational_engine: AsyncEngine,
) -> None:
    assert await load_conditional_zone_severity(relational_engine) == "warning"
    try:
        async with relational_engine.begin() as connection:
            await connection.execute(
                sa.text("UPDATE airspace_policy SET conditional_zone_severity = 'info'")
            )
        assert await load_conditional_zone_severity(relational_engine) == "info"
        with pytest.raises(sa.exc.IntegrityError):
            async with relational_engine.begin() as connection:
                await connection.execute(
                    sa.text(
                        "UPDATE airspace_policy "
                        "SET conditional_zone_severity = 'critical'"
                    )
                )
    finally:
        async with relational_engine.begin() as connection:
            await connection.execute(
                sa.text(
                    "UPDATE airspace_policy SET conditional_zone_severity = 'warning'"
                )
            )

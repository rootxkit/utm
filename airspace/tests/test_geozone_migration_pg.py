"""Migration 0007_geo_awareness on a populated table: up, down, up. U-03.

Zones made before U-03 must keep alerting exactly as they did, and the
downgrade must lose only what its docstring says it loses.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine

from airspace.ed269 import Restriction, VerticalReference
from airspace.zones import load_zones
from api.tests.conftest import migrate_relational

pytestmark = pytest.mark.postgres

BEFORE = "0006_source_controls"
SQUARE = "POLYGON((44.80 41.70, 44.82 41.70, 44.82 41.72, 44.80 41.72, 44.80 41.70))"
OLD_ZONES = (
    ("mig-no-fly", "no_fly", 400.0, 700.0),
    ("mig-restricted", "restricted", None, 900.0),
    ("mig-open", "no_fly", None, None),
    ("mig-corridor", "corridor", None, None),
    ("mig-base", "base", None, 50.0),
)


async def rows(engine: AsyncEngine, sql: str) -> list[dict[str, Any]]:
    async with engine.connect() as connection:
        return [
            dict(row) for row in (await connection.execute(sa.text(sql))).mappings()
        ]


async def migrate(url: str, target: str, *, down: bool = False) -> None:
    await asyncio.to_thread(migrate_relational, url, target, down=down)


async def test_existing_zones_survive_up_down_up_and_keep_alerting(
    prepared_relational_database: str, relational_engine: AsyncEngine
) -> None:
    url = prepared_relational_database
    await migrate(url, BEFORE, down=True)
    try:
        async with relational_engine.begin() as connection:
            for name, kind, low, high in OLD_ZONES:
                await connection.execute(
                    sa.text(
                        "INSERT INTO airspace_zones "
                        "(name, type, geom, min_alt_amsl_m, max_alt_amsl_m) "
                        "VALUES (:n, :t, ST_GeomFromText(:w, 4326), :low, :high)"
                    ),
                    {"n": name, "t": kind, "w": SQUARE, "low": low, "high": high},
                )

        # --- up: the safe default mapping ---------------------------------------
        await migrate(url, "head")
        mapped = {
            row["name"]: row
            for row in await rows(
                relational_engine,
                "SELECT name, type, identifier, country, restriction, "
                "lower_limit, lower_reference, upper_limit, upper_reference, "
                "uom_dimensions, applicability, zone_authority "
                "FROM airspace_zones WHERE name LIKE 'mig-%'",
            )
        }
        assert {n: (r["type"], r["restriction"]) for n, r in mapped.items()} == {
            "mig-no-fly": ("geozone", "PROHIBITED"),
            "mig-restricted": ("geozone", "REQ_AUTHORISATION"),
            "mig-open": ("geozone", "PROHIBITED"),
            "mig-corridor": ("corridor", "NO_RESTRICTION"),
            "mig-base": ("base", "NO_RESTRICTION"),
        }
        no_fly = mapped["mig-no-fly"]
        assert (no_fly["lower_limit"], no_fly["upper_limit"]) == (400.0, 700.0)
        assert no_fly["lower_reference"] == no_fly["upper_reference"] == "AMSL"
        assert no_fly["uom_dimensions"] == "M"
        assert no_fly["applicability"] == [{"permanent": "YES"}]
        assert no_fly["zone_authority"] == []
        assert no_fly["country"] == "GEO"
        assert mapped["mig-open"]["lower_limit"] is None
        identifiers = [r["identifier"] for r in mapped.values()]
        assert len(set(identifiers)) == len(identifiers)
        assert all(i.startswith("Z") and len(i) == 7 for i in identifiers)

        # They alert as before: no-fly critical, restricted warning, a zone
        # with no limits from the ground up without needing terrain.
        loaded = {
            z.name: z
            for z in await load_zones(relational_engine)
            if (z.name or "").startswith("mig-")
        }
        assert set(loaded) == {"mig-no-fly", "mig-restricted", "mig-open"}
        assert loaded["mig-no-fly"].restriction is Restriction.PROHIBITED
        assert loaded["mig-restricted"].restriction is Restriction.REQ_AUTHORISATION
        assert loaded["mig-restricted"].lower is None
        assert loaded["mig-open"].references == frozenset()
        assert loaded["mig-no-fly"].references == {VerticalReference.AMSL}

        # New zones the old model cannot fully hold.
        async with relational_engine.begin() as connection:
            for name, identifier, restriction, low, low_ref, high, high_ref, uom in (
                ("mig-info", "MIGINFO", "NO_RESTRICTION", None, "AGL", 120, "AGL", "M"),
                ("mig-cond", "MIGCOND", "CONDITIONAL", 0, "AGL", 2000, "AMSL", "FT"),
                ("mig-hae", "MIGHAE", "PROHIBITED", None, "AMSL", 600, "WGS84", "M"),
            ):
                await connection.execute(
                    sa.text(
                        "INSERT INTO airspace_zones "
                        "(name, type, geom, identifier, country, ed269_type, "
                        " restriction, zone_authority, applicability, "
                        " uom_dimensions, lower_limit, lower_reference, "
                        " upper_limit, upper_reference) "
                        "VALUES (:n, 'geozone', ST_GeomFromText(:w, 4326), :i, "
                        " 'GEO', 'COMMON', :r, '[]', "
                        ' \'[{"permanent": "NO", '
                        '    "startDateTime": "2026-01-01T00:00:00Z"}]\', '
                        " :uom, :low, :low_ref, :high, :high_ref)"
                    ),
                    {
                        "n": name,
                        "w": SQUARE,
                        "i": identifier,
                        "r": restriction,
                        "uom": uom,
                        "low": low,
                        "low_ref": low_ref,
                        "high": high,
                        "high_ref": high_ref,
                    },
                )

        # --- down: what the docstring says is lost, towards more alerting --------
        await migrate(url, BEFORE, down=True)
        old = {
            row["name"]: row
            for row in await rows(
                relational_engine,
                "SELECT name, type, min_alt_amsl_m, max_alt_amsl_m "
                "FROM airspace_zones WHERE name LIKE 'mig-%'",
            )
        }
        assert {n: r["type"] for n, r in old.items()} == {
            "mig-no-fly": "no_fly",
            "mig-restricted": "restricted",
            "mig-open": "no_fly",
            "mig-corridor": "corridor",
            "mig-base": "base",
            # NO_RESTRICTION geozone deleted; CONDITIONAL becomes a warning.
            "mig-cond": "restricted",
            "mig-hae": "no_fly",
        }
        assert (
            old["mig-no-fly"]["min_alt_amsl_m"],
            old["mig-no-fly"]["max_alt_amsl_m"],
        ) == (
            400.0,
            700.0,
        )
        # AGL floor dropped; the AMSL ceiling in feet converted.
        assert old["mig-cond"]["min_alt_amsl_m"] is None
        assert old["mig-cond"]["max_alt_amsl_m"] == pytest.approx(609.6)
        # A WGS84 ceiling dropped: the zone has none now.
        assert old["mig-hae"]["max_alt_amsl_m"] is None

        # --- up again -------------------------------------------------------------
        await migrate(url, "head")
        again = {
            row["name"]: row
            for row in await rows(
                relational_engine,
                "SELECT name, restriction, upper_limit FROM airspace_zones "
                "WHERE name LIKE 'mig-%'",
            )
        }
        assert again["mig-no-fly"]["restriction"] == "PROHIBITED"
        assert again["mig-cond"]["restriction"] == "REQ_AUTHORISATION"
        assert again["mig-cond"]["upper_limit"] == pytest.approx(609.6)
        assert len(again) == 7
    finally:
        await migrate(url, "head")
        async with relational_engine.begin() as connection:
            await connection.execute(
                sa.text("DELETE FROM airspace_zones WHERE name LIKE 'mig-%'")
            )


async def test_the_constraints_hold_the_model(relational_engine: AsyncEngine) -> None:
    """A corridor cannot restrict, and a band needs lower below upper in one
    reference; the same rows with those fixed are accepted."""
    insert = sa.text(
        "INSERT INTO airspace_zones "
        "(name, type, geom, identifier, country, ed269_type, restriction, "
        " zone_authority, applicability, uom_dimensions, lower_limit, "
        " lower_reference, upper_limit, upper_reference) "
        "VALUES ('con-zone', :t, ST_GeomFromText(:w, 4326), :i, 'GEO', 'COMMON', "
        " :r, '[]', '[{\"permanent\": \"YES\"}]', 'M', :low, :low_ref, 100, 'AMSL')"
    )
    refused: tuple[dict[str, Any], ...] = (
        {"t": "corridor", "r": "PROHIBITED", "low": None, "low_ref": "AMSL"},
        {"t": "geozone", "r": "PROHIBITED", "low": 200, "low_ref": "AMSL"},
        {"t": "geozone", "r": "BANNED", "low": None, "low_ref": "AMSL"},
        {"t": "geozone", "r": "PROHIBITED", "low": None, "low_ref": "FL"},
    )
    try:
        for n, values in enumerate(refused):
            with pytest.raises(sa.exc.IntegrityError):
                async with relational_engine.begin() as connection:
                    await connection.execute(
                        insert, {**values, "w": SQUARE, "i": f"CON{n}"}
                    )
        async with relational_engine.begin() as connection:
            await connection.execute(
                insert,
                {
                    "t": "geozone",
                    "r": "PROHIBITED",
                    # Above the AMSL ceiling, but in another reference: allowed.
                    "low": 200,
                    "low_ref": "AGL",
                    "w": SQUARE,
                    "i": "CONOK",
                },
            )
    finally:
        async with relational_engine.begin() as connection:
            await connection.execute(
                sa.text("DELETE FROM airspace_zones WHERE name = 'con-zone'")
            )

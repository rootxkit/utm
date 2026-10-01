"""Telemetry migrations 0009 and 0010 on a populated `known_drones`. U-02.

In a database of its own (its name derived from the test database's, so it
ends in `_test` too), so walking the tree down and up cannot touch the
schema the other tests share.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine

from gateway.tests.conftest import (
    MAINTENANCE_DATABASE,
    _create_extensions,
    database_name,
    require_test_database_url,
    with_database,
)

pytestmark = pytest.mark.postgres

BEFORE = "0008_archive_retention_index"
ALEMBIC_INI = (
    Path(__file__).resolve().parents[2]
    / "infra"
    / "migrations"
    / "telemetry"
    / "alembic.ini"
)


def migrate(url: str, target: str, *, down: bool = False) -> None:
    from alembic import command
    from alembic.config import Config

    config = Config(str(ALEMBIC_INI))
    previous = os.environ.get("TELEMETRY_DATABASE_URL")
    os.environ["TELEMETRY_DATABASE_URL"] = url
    try:
        (command.downgrade if down else command.upgrade)(config, target)
    finally:
        if previous is None:
            os.environ.pop("TELEMETRY_DATABASE_URL", None)
        else:
            os.environ["TELEMETRY_DATABASE_URL"] = previous


@pytest.fixture
async def own_database(test_database_url: str) -> AsyncIterator[str]:
    name = database_name(test_database_url).removesuffix("_test") + "_mig_test"
    url = require_test_database_url(with_database(test_database_url, name))
    admin = create_async_engine(
        with_database(test_database_url, MAINTENANCE_DATABASE),
        isolation_level="AUTOCOMMIT",
    )
    async with admin.connect() as connection:
        await connection.execute(sa.text(f'DROP DATABASE IF EXISTS "{name}"'))
        await connection.execute(sa.text(f'CREATE DATABASE "{name}"'))
    await _create_extensions(url)
    try:
        yield url
    finally:
        async with admin.connect() as connection:
            await connection.execute(
                sa.text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :name AND pid <> pg_backend_pid()"
                ),
                {"name": name},
            )
            await connection.execute(sa.text(f'DROP DATABASE IF EXISTS "{name}"'))
        await admin.dispose()


async def test_the_projection_migrations_keep_a_populated_known_drones(
    own_database: str,
) -> None:
    url = own_database
    await asyncio.to_thread(migrate, url, BEFORE)
    engine = create_async_engine(url)
    fleet, uas = uuid4(), uuid4()
    try:
        async with engine.begin() as connection:
            for drone_id, label, serial in (
                (fleet, "hexa-01", None),
                (uas, "uas-01", "SN-MIG-1"),
            ):
                await connection.execute(
                    sa.text(
                        "INSERT INTO known_drones (drone_id, label, serial) "
                        "VALUES (:id, :label, :serial)"
                    ),
                    {"id": drone_id, "label": label, "serial": serial},
                )

        await asyncio.to_thread(migrate, url, "head")

        async with engine.connect() as connection:
            rows = (
                await connection.execute(
                    sa.text(
                        "SELECT drone_id, label, serial, registration_status, "
                        "uas_operator_id FROM known_drones ORDER BY label"
                    )
                )
            ).all()
            assert [(r.label, r.serial) for r in rows] == [
                ("hexa-01", None),
                ("uas-01", "SN-MIG-1"),
            ]
            # Unknown until the API projects them; NULL reads as active.
            assert {(r.registration_status, r.uas_operator_id) for r in rows} == {
                (None, None)
            }
        async with engine.begin() as connection:
            await connection.execute(
                sa.text(
                    "UPDATE known_drones SET registration_status = 'unregistered' "
                    "WHERE drone_id = :id"
                ),
                {"id": fleet},
            )
            await connection.execute(
                sa.text(
                    "UPDATE known_drones SET registration_status = 'suspended' "
                    "WHERE drone_id = :id"
                ),
                {"id": uas},
            )
            with pytest.raises(sa.exc.IntegrityError):
                async with connection.begin_nested():
                    await connection.execute(
                        sa.text("UPDATE known_drones SET registration_status = 'lost'")
                    )

        # 0010 down: 'unregistered' becomes NULL, which 0009 allows.
        await asyncio.to_thread(migrate, url, "0009_uas_identity_projection", down=True)
        async with engine.connect() as connection:
            statuses: dict[object, object] = dict(
                (
                    await connection.execute(
                        sa.text(
                            "SELECT drone_id, registration_status FROM known_drones"
                        )
                    )
                ).all()
            )
        assert statuses == {fleet: None, uas: "suspended"}

        # 0009 down: the columns go, the rows stay.
        await asyncio.to_thread(migrate, url, BEFORE, down=True)
        async with engine.connect() as connection:
            count: int = (
                await connection.execute(sa.text("SELECT count(*) FROM known_drones"))
            ).scalar_one()
        assert count == 2
    finally:
        await engine.dispose()

"""The registry facts U-02 resolves against, projected by the API. U-02.

Against both real databases: a change to the registry reaches the telemetry
database in the same call, a follower in an adapter sees it within its
refresh interval, and a resynchronisation repairs a projection that drifted.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterable
from typing import Any
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine

from api.registry import ConflictError
from api.uas_registry import OperatorType, RegistrationStatus, UasRegistry
from gateway.binding import BindingResolver
from gateway.identification import IdentificationStatus, resolve
from gateway.registry_projection import (
    IdentityProjection,
    ProjectedOperator,
    ProjectedUas,
    RegistryFollower,
    load_snapshot,
)

pytestmark = pytest.mark.postgres

# How soon a registry change must reach a running follower: its refresh
# interval plus a margin for the read itself. The deployed default interval
# is REGISTRY_REFRESH_S = 5 s; the test runs the same loop faster.
REFRESH_S = 0.3
WITHIN_S = REFRESH_S + 1.5


@pytest.fixture
def registry(relational_engine: AsyncEngine, engine: AsyncEngine) -> UasRegistry:
    return UasRegistry(
        engine=relational_engine,
        projection=BindingResolver(engine=engine),
        identity=IdentityProjection(engine=engine),
    )


def number() -> str:
    return f"GEO{uuid4().hex[:13].upper()}"


def serial() -> str:
    return f"SN-{uuid4().hex[:12].upper()}"


async def operator(registry: UasRegistry, reg: str | None = None) -> dict[str, Any]:
    return await registry.create_operator(
        registration_number=reg or number(),
        legal_name="Kakheti Aerial",
        operator_type=OperatorType.LEGAL_PERSON,
    )


async def uas(registry: UasRegistry, owner: UUID, sn: str | None = None) -> UUID:
    created = await registry.register_uas(
        serial=sn or serial(), class_label=None, mtom_g=900, uas_operator_id=owner
    )
    drone_id: UUID = created["id"]
    return drone_id


async def test_a_registered_uas_and_its_operator_are_projected(
    registry: UasRegistry, engine: AsyncEngine
) -> None:
    owner = await operator(registry)
    sn = serial()
    drone_id = await uas(registry, owner["id"], sn)

    snapshot = await load_snapshot(engine)

    facts = snapshot.by_serial[sn]
    assert facts.drone_id == drone_id
    assert facts.registration_status == RegistrationStatus.ACTIVE
    assert facts.uas_operator_id == owner["id"]
    assert (
        snapshot.operators_by_id[owner["id"]].registration_number
        == (owner["registration_number"])
    )
    found = resolve(
        snapshot, serial=sn, operator_reg=owner["registration_number"].lower()
    )
    assert found.status is IdentificationStatus.REGISTERED


async def test_suspending_and_reactivating_reaches_the_projection(
    registry: UasRegistry, engine: AsyncEngine
) -> None:
    owner = await operator(registry)
    sn = serial()
    drone_id = await uas(registry, owner["id"], sn)
    reg = owner["registration_number"]

    await registry.set_operator_status(owner["id"], RegistrationStatus.SUSPENDED)
    snapshot = await load_snapshot(engine)
    assert resolve(snapshot, serial=sn, operator_reg=reg).status is (
        IdentificationStatus.SUSPENDED
    )

    await registry.set_operator_status(owner["id"], RegistrationStatus.ACTIVE)
    await registry.set_uas_status(drone_id, RegistrationStatus.SUSPENDED)
    snapshot = await load_snapshot(engine)
    found = resolve(snapshot, serial=sn, operator_reg=reg)
    assert (found.status, found.reason.value) == (
        IdentificationStatus.SUSPENDED,
        "uas_suspended",
    )

    await registry.set_uas_status(drone_id, RegistrationStatus.ACTIVE)
    snapshot = await load_snapshot(engine)
    assert resolve(snapshot, serial=sn, operator_reg=reg).status is (
        IdentificationStatus.REGISTERED
    )


async def test_a_new_owner_reaches_the_projection(
    registry: UasRegistry, engine: AsyncEngine
) -> None:
    first, second = await operator(registry), await operator(registry)
    sn = serial()
    drone_id = await uas(registry, first["id"], sn)

    await registry.update_uas(drone_id, {"uas_operator_id": second["id"]})

    snapshot = await load_snapshot(engine)
    old = resolve(snapshot, serial=sn, operator_reg=first["registration_number"])
    new = resolve(snapshot, serial=sn, operator_reg=second["registration_number"])
    assert (old.status, old.mismatch) == (IdentificationStatus.UNKNOWN_OPERATOR, True)
    assert new.status is IdentificationStatus.REGISTERED


class FailingIdentity:
    async def project_operator(self, operator: ProjectedOperator) -> None:
        raise RuntimeError("telemetry database gone")

    async def project_uas(self, uas: ProjectedUas) -> None:
        raise RuntimeError("telemetry database gone")

    async def replace_all(
        self, operators: Iterable[ProjectedOperator], uas: Iterable[ProjectedUas]
    ) -> tuple[int, int]:
        raise RuntimeError("telemetry database gone")


async def test_a_projection_that_fails_rolls_the_change_back(
    registry: UasRegistry, relational_engine: AsyncEngine
) -> None:
    """Presence of the failure path: nothing recorded that was not projected."""
    owner = await operator(registry)
    drone_id = await uas(registry, owner["id"])
    registry.identity = FailingIdentity()

    with pytest.raises(RuntimeError):
        await registry.set_uas_status(drone_id, RegistrationStatus.SUSPENDED)

    async with relational_engine.connect() as connection:
        status: str = (
            await connection.execute(
                sa.text("SELECT registration_status FROM drones WHERE id = :id"),
                {"id": str(drone_id)},
            )
        ).scalar_one()
    assert status == "active"


async def test_a_resynchronisation_repairs_a_projection_that_drifted(
    registry: UasRegistry, engine: AsyncEngine
) -> None:
    owner = await operator(registry)
    sn = serial()
    drone_id = await uas(registry, owner["id"], sn)
    async with engine.begin() as connection:
        await connection.execute(
            sa.text("DELETE FROM known_uas_operators WHERE operator_id = :id"),
            {"id": owner["id"]},
        )
        await connection.execute(
            sa.text(
                "UPDATE known_drones SET registration_status = 'revoked' "
                "WHERE drone_id = :id"
            ),
            {"id": drone_id},
        )
    drifted = await load_snapshot(engine)
    assert resolve(drifted, serial=sn, operator_reg=None).status is (
        IdentificationStatus.SUSPENDED
    )

    written, changed = await registry.sync_projection()

    assert written >= 1 and changed >= 1
    snapshot = await load_snapshot(engine)
    found = resolve(snapshot, serial=sn, operator_reg=owner["registration_number"])
    assert found.status is IdentificationStatus.REGISTERED
    # A second pass finds nothing to change: it writes only differences.
    _, again = await registry.sync_projection()
    assert again == 0


async def test_an_operator_gone_from_the_registry_leaves_the_projection(
    registry: UasRegistry, engine: AsyncEngine
) -> None:
    ghost = uuid4()
    await IdentityProjection(engine).project_operator(
        ProjectedOperator(ghost, number(), "active")
    )
    assert ghost in (await load_snapshot(engine)).operators_by_id

    await registry.sync_projection()

    assert ghost not in (await load_snapshot(engine)).operators_by_id


async def test_without_an_identity_writer_nothing_is_synchronised(
    relational_engine: AsyncEngine, engine: AsyncEngine
) -> None:
    bare = UasRegistry(
        engine=relational_engine, projection=BindingResolver(engine=engine)
    )
    assert await bare.sync_projection() == (0, 0)


async def test_a_registry_change_reaches_a_running_follower_in_time(
    registry: UasRegistry, engine: AsyncEngine
) -> None:
    """The freshness bound: a suspension made through the API is what a
    running adapter resolves within its refresh interval plus a read."""
    owner = await operator(registry)
    sn = serial()
    drone_id = await uas(registry, owner["id"], sn)
    reg = owner["registration_number"]
    follower = RegistryFollower(engine=engine, refresh_s=REFRESH_S)
    await follower.refresh()
    assert resolve(follower.snapshot, serial=sn, operator_reg=reg).status is (
        IdentificationStatus.REGISTERED
    )
    stop = asyncio.Event()
    task = asyncio.create_task(follower.run(stop))
    try:
        await registry.set_uas_status(drone_id, RegistrationStatus.SUSPENDED)
        changed_at = time.monotonic()
        while (
            resolve(follower.snapshot, serial=sn, operator_reg=reg).status
            is not IdentificationStatus.SUSPENDED
        ):
            assert time.monotonic() - changed_at < WITHIN_S, "not seen in time"
            await asyncio.sleep(0.05)
        seen_after_s = time.monotonic() - changed_at
    finally:
        stop.set()
        await task
    assert seen_after_s < WITHIN_S
    assert follower.reads >= 2


async def test_a_revoked_operator_cannot_be_reactivated_and_stays_projected(
    registry: UasRegistry, engine: AsyncEngine
) -> None:
    owner = await operator(registry)
    await registry.set_operator_status(owner["id"], RegistrationStatus.REVOKED)
    with pytest.raises(ConflictError):
        await registry.set_operator_status(owner["id"], RegistrationStatus.ACTIVE)
    snapshot = await load_snapshot(engine)
    assert snapshot.operators_by_id[owner["id"]].status == RegistrationStatus.REVOKED

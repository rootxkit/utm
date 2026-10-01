"""The fleet routes and the third-party rows that share their tables. U-01.

`drones` and `pilots` hold the fleet and, since U-01, third-party UAS and
remote pilots. The fleet routes must see and change only the fleet.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
import sqlalchemy as sa
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine

from api.tests.auth_fakes import ADMIN_HEADERS
from api.tests.test_uas_registry_pg import a_uas, an_operator, build_app

pytestmark = pytest.mark.postgres


@pytest.fixture
async def client(
    relational_engine: AsyncEngine, engine: AsyncEngine
) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(
        transport=ASGITransport(app=build_app(relational_engine, engine)),
        base_url="http://test",
        headers=ADMIN_HEADERS,
    ) as http:
        yield http


# --- the fleet routes and third-party rows -----------------------------------------


async def test_the_fleet_routes_list_the_fleet_and_not_third_party_rows(
    client: AsyncClient,
) -> None:
    operator = await an_operator(client)
    uas = await a_uas(client, operator["id"])
    pilot = (
        await client.post(
            "/uas/pilots", json={"name": uuid4().hex, "uas_operator_id": operator["id"]}
        )
    ).json()
    fleet_drone = (
        await client.post(
            "/drones", json={"serial": f"FLEET-{uuid4().hex[:8]}", "label": uuid4().hex}
        )
    ).json()
    fleet_pilot = (await client.post("/pilots", json={"name": uuid4().hex})).json()

    drones = {d["id"] for d in (await client.get("/drones")).json()}
    pilots = {p["id"] for p in (await client.get("/pilots")).json()}

    assert fleet_drone["id"] in drones
    assert uas["id"] not in drones
    assert fleet_pilot["id"] in pilots
    assert pilot["id"] not in pilots
    assert (await client.get(f"/drones/{fleet_drone['id']}")).status_code == 200
    assert (await client.get(f"/drones/{uas['id']}")).status_code == 404


async def test_fleet_writes_are_refused_on_a_uas_and_its_projection_is_kept(
    client: AsyncClient, engine: AsyncEngine
) -> None:
    operator = await an_operator(client)
    uas = await a_uas(client, operator["id"])
    pilot = (
        await client.post(
            "/uas/pilots", json={"name": uuid4().hex, "uas_operator_id": operator["id"]}
        )
    ).json()
    fleet_drone = (
        await client.post(
            "/drones", json={"serial": f"FLEET-{uuid4().hex[:8]}", "label": uuid4().hex}
        )
    ).json()

    refused = [
        await client.put(
            f"/drones/{uas['id']}/maintenance", json={"in_maintenance": True}
        ),
        await client.post(f"/drones/{uas['id']}/retire"),
        await client.put(f"/pilots/{pilot['id']}/status", json={"status": "ON_DUTY"}),
    ]
    allowed = await client.put(
        f"/drones/{fleet_drone['id']}/maintenance", json={"in_maintenance": True}
    )

    assert [r.status_code for r in refused] == [409, 409, 409]
    assert {r.json()["detail"]["code"] for r in refused} == {"not_fleet"}
    assert allowed.status_code == 200
    async with engine.connect() as connection:
        retired_at: object = (
            await connection.execute(
                sa.text("SELECT retired_at FROM known_drones WHERE drone_id = :d"),
                {"d": uas["id"]},
            )
        ).scalar_one()
    assert retired_at is None
    assert (await client.get(f"/uas/aircraft/{uas['id']}")).json()["retired_at"] is None

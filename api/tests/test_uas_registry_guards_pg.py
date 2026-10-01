"""The UAS registry's guards, against real databases. U-01 review.

Who sees contact details, what the audit log keeps of them, the fleet
routes' boundary with third-party rows, revocation being final, and the lock
that keeps a registration from racing a revocation.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

import pytest
import sqlalchemy as sa
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine

from api.registry import ConflictError
from api.tests.auth_fakes import ADMIN_HEADERS, OPERATOR_HEADERS, VIEWER_HEADERS
from api.tests.test_uas_registry_pg import a_uas, an_operator, build_app, cta_serial
from api.uas_registry import RegistrationStatus, UasRegistry
from gateway.binding import BindingResolver

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


EMAIL = "private@example.ge"
PHONE = "+995 599 12 34 56"
ADDRESS = "12 Rustaveli Ave, Tbilisi"


async def as_role(
    relational: AsyncEngine, telemetry: AsyncEngine, headers: dict[str, str], path: str
) -> dict[str, object]:
    async with AsyncClient(
        transport=ASGITransport(app=build_app(relational, telemetry)),
        base_url="http://test",
        headers=headers,
    ) as http:
        response = await http.get(path)
    assert response.status_code == 200, response.text
    body: Any = response.json()
    shown: dict[str, object] = body[0] if isinstance(body, list) else body
    return shown


# --- contact details --------------------------------------------------------------


async def test_a_viewer_gets_no_contact_details_and_an_operator_and_admin_do(
    client: AsyncClient, relational_engine: AsyncEngine, engine: AsyncEngine
) -> None:
    operator = await an_operator(
        client, contact_email=EMAIL, contact_phone=PHONE, postal_address=ADDRESS
    )
    number = operator["registration_number"]
    paths = [
        f"/uas/operators/{operator['id']}",
        f"/uas/operators/lookup?registration_number={number}",
        f"/uas/operators?q={number}",
    ]
    contact = {
        "contact_email": EMAIL,
        "contact_phone": PHONE,
        "postal_address": ADDRESS,
    }

    assert operator["contact"] == contact  # the admin who created it
    for path in paths:
        seen_by_viewer = await as_role(relational_engine, engine, VIEWER_HEADERS, path)
        assert seen_by_viewer["contact"] is None, path
        assert EMAIL not in json.dumps(seen_by_viewer), path
        seen_by_operator = await as_role(
            relational_engine, engine, OPERATOR_HEADERS, path
        )
        assert seen_by_operator["contact"] == contact, path


async def test_no_event_carries_contact_details_but_says_which_changed(
    client: AsyncClient,
) -> None:
    operator = await an_operator(
        client, contact_email=EMAIL, contact_phone=None, postal_address=ADDRESS
    )
    patched = await client.patch(
        f"/uas/operators/{operator['id']}",
        json={"contact_phone": PHONE, "legal_name": "Renamed"},
    )
    assert patched.status_code == 200

    events = (
        await client.get(
            "/events",
            params={"entity_type": "uas_operator", "entity_id": operator["id"]},
        )
    ).json()
    text = json.dumps(events)
    for secret in (EMAIL, PHONE, ADDRESS):
        assert secret not in text, secret
    registered, updated = (event["payload"] for event in events)
    assert registered["contact_fields"] == ["contact_email", "postal_address"]
    assert registered["legal_name"] == "Kartli Aerial LLC"
    assert updated["changed_fields"] == ["contact_phone", "legal_name"]
    assert updated["changes"] == {
        "legal_name": {"from": "Kartli Aerial LLC", "to": "Renamed"}
    }


# --- revoked is final for edits --------------------------------------------------------


async def test_a_revoked_operator_or_uas_is_not_edited_or_rehomed(
    client: AsyncClient,
) -> None:
    operator = await an_operator(client)
    other = await an_operator(client)
    uas = await a_uas(client, operator["id"])
    await client.post(f"/uas/aircraft/{uas['id']}/revoke")
    await client.post(f"/uas/operators/{operator['id']}/revoke")

    edits = [
        await client.patch(
            f"/uas/operators/{operator['id']}", json={"legal_name": "Changed"}
        ),
        await client.patch(
            f"/uas/aircraft/{uas['id']}", json={"uas_operator_id": other["id"]}
        ),
        await client.patch(f"/uas/aircraft/{uas['id']}", json={"mtom_g": 1234}),
    ]
    live = await client.patch(
        f"/uas/operators/{other['id']}", json={"legal_name": "Still editable"}
    )

    assert [e.status_code for e in edits] == [409, 409, 409]
    assert {e.json()["detail"]["code"] for e in edits} == {"revoked"}
    assert live.status_code == 200
    after = (await client.get(f"/uas/aircraft/{uas['id']}")).json()
    assert after["uas_operator_id"] == operator["id"]


# --- labels and order --------------------------------------------------------------


async def test_a_serial_that_is_another_aircrafts_label_is_refused_by_name(
    client: AsyncClient,
) -> None:
    operator = await an_operator(client)
    taken = f"LBL-{uuid4().hex[:8]}"
    await client.post(
        "/drones", json={"serial": f"F-{uuid4().hex[:8]}", "label": taken}
    )
    body = {"serial": taken, "class_label": None, "mtom_g": 500}

    clash = await client.post(
        "/uas/aircraft", json={**body, "uas_operator_id": operator["id"]}
    )
    named = await client.post(
        "/uas/aircraft",
        json={**body, "uas_operator_id": operator["id"], "label": f"own-{taken}"},
    )

    assert clash.status_code == 409
    assert clash.json()["detail"]["code"] == "label_taken"
    assert named.status_code == 201


async def test_operators_are_listed_in_order_ignoring_case(client: AsyncClient) -> None:
    prefix = uuid4().hex[:6].upper()
    upper = await an_operator(client, registration_number=f"GEO{prefix}C000000")
    lower = await an_operator(client, registration_number=f"GEO{prefix}b000000")

    listed = (await client.get("/uas/operators", params={"q": prefix})).json()

    assert [o["id"] for o in listed] == [lower["id"], upper["id"]]


# --- the revocation race ---------------------------------------------------------------


async def test_a_registration_waits_for_a_revocation_and_is_then_refused(
    relational_engine: AsyncEngine, engine: AsyncEngine, client: AsyncClient
) -> None:
    """While a revocation holds the operator row, registering a UAS for it
    waits, and once the revocation commits it is refused. Without the lock
    it read `active` before the revocation committed and attached the UAS."""
    operator = await an_operator(client)
    registry = UasRegistry(
        engine=relational_engine, projection=BindingResolver(engine=engine)
    )
    async with relational_engine.connect() as revoking:
        await revoking.begin()
        await revoking.execute(
            sa.text("SELECT id FROM uas_operators WHERE id = :id FOR UPDATE"),
            {"id": operator["id"]},
        )
        await revoking.execute(
            sa.text("UPDATE uas_operators SET status = 'revoked' WHERE id = :id"),
            {"id": operator["id"]},
        )
        registering = asyncio.create_task(
            registry.register_uas(
                serial=cta_serial(),
                class_label=None,
                mtom_g=900,
                uas_operator_id=operator["id"],
            )
        )
        await asyncio.sleep(0.5)
        assert not registering.done()
        await revoking.commit()

    with pytest.raises(ConflictError) as refused:
        await registering
    assert refused.value.code == "operator_revoked"
    listed = await registry.list_uas(uas_operator_id=operator["id"])
    assert listed == []
    assert (await registry.get_operator(operator["id"]))[
        "status"
    ] == RegistrationStatus.REVOKED

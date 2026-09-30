"""The UAS operator registry against real databases. U-01.

Both databases are real: a registered UAS must reach `known_drones` in the
telemetry database, as a fleet drone does, and every change must be in
`events` in the relational one.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator, Sequence
from typing import Any
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine

from api.app import create_api_app
from api.registry import FleetRegistry
from api.tests.auth_fakes import ADMIN_HEADERS, VIEWER_HEADERS, api_kwargs
from api.tests.conftest import migrate_relational
from api.uas_registry import UasRegistry
from gateway.binding import BindingResolver

pytestmark = pytest.mark.postgres


class NoLive:
    """No aircraft is live: status is not what these tests are about."""

    async def get(self, drone_id: UUID) -> dict[str, Any] | None:
        return None

    async def get_many(
        self, drone_ids: Sequence[UUID]
    ) -> dict[UUID, dict[str, Any] | None]:
        return dict.fromkeys(drone_ids)


def build_app(
    relational: AsyncEngine, telemetry: AsyncEngine, *, pattern: str | None = None
) -> Any:
    projection = BindingResolver(engine=telemetry)
    registry = FleetRegistry(engine=relational, projection=projection, live=NoLive())
    uas = UasRegistry(engine=relational, projection=projection)
    if pattern is not None:
        uas.registration_pattern = re.compile(pattern)
    return create_api_app(registry, uas=uas, **api_kwargs())


@pytest.fixture
async def client(
    relational_engine: AsyncEngine, engine: AsyncEngine
) -> AsyncIterator[AsyncClient]:
    """The API over both test databases. `engine` is the telemetry one."""
    async with AsyncClient(
        transport=ASGITransport(app=build_app(relational_engine, engine)),
        base_url="http://test",
        headers=ADMIN_HEADERS,
    ) as http:
        yield http


def registration_number() -> str:
    """A 16-character EU-shaped number: GEO and 13 letters and digits."""
    return f"GEO{uuid4().hex[:13].upper()}"


def cta_serial() -> str:
    """A CTA-2063-A serial: maker code, length C (12), 12 hex characters."""
    return f"1A2BC{uuid4().hex[:12].upper()}"


async def an_operator(client: AsyncClient, **overrides: Any) -> dict[str, Any]:
    body = {
        "registration_number": registration_number(),
        "legal_name": "Kartli Aerial LLC",
        "operator_type": "legal_person",
        "contact_email": "ops@example.ge",
        **overrides,
    }
    response = await client.post("/uas/operators", json=body)
    assert response.status_code == 201, response.text
    created: dict[str, Any] = response.json()
    return created


async def a_uas(
    client: AsyncClient, operator_id: str, **overrides: Any
) -> dict[str, Any]:
    body = {
        "serial": cta_serial(),
        "class_label": "C2",
        "mtom_g": 3_600,
        "uas_operator_id": operator_id,
        **overrides,
    }
    response = await client.post("/uas/aircraft", json=body)
    assert response.status_code == 201, response.text
    created: dict[str, Any] = response.json()
    return created


async def events_of(client: AsyncClient, entity_type: str, entity_id: str) -> list[str]:
    response = await client.get(
        "/events", params={"entity_type": entity_type, "entity_id": entity_id}
    )
    assert response.status_code == 200
    return [event["event_type"] for event in response.json()]


# --- the migration ----------------------------------------------------------------


async def test_the_migration_runs_down_and_up_again_keeping_fleet_rows(
    prepared_relational_database: str, relational_engine: AsyncEngine
) -> None:
    """0005 down to 0004 and up again. A fleet drone and pilot from before
    U-01 survive both ways and come back active, with no operator."""
    async with relational_engine.begin() as connection:
        drone_id: UUID = (
            await connection.execute(
                sa.text(
                    "INSERT INTO drones (serial, label) VALUES (:s, :l) RETURNING id"
                ),
                {"s": f"FLEET-{uuid4().hex[:8]}", "l": f"fleet-{uuid4().hex[:8]}"},
            )
        ).scalar_one()

    await asyncio.to_thread(
        migrate_relational, prepared_relational_database, "0004_height_limit", down=True
    )
    async with relational_engine.connect() as connection:
        tables: set[str] = set(
            (
                await connection.execute(
                    sa.text(
                        "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
                    )
                )
            ).scalars()
        )
    assert "uas_operators" not in tables
    assert "pilot_competencies" not in tables

    await asyncio.to_thread(migrate_relational, prepared_relational_database, "head")
    async with relational_engine.connect() as connection:
        row = (
            await connection.execute(
                sa.text(
                    "SELECT registration_status, uas_operator_id, class_label "
                    "FROM drones WHERE id = :id"
                ),
                {"id": drone_id},
            )
        ).one()
    assert (row.registration_status, row.uas_operator_id, row.class_label) == (
        "active",
        None,
        None,
    )


# --- U-01's criterion -----------------------------------------------------------


async def test_an_operator_a_pilot_and_two_uas_registered_suspended_and_found(
    client: AsyncClient, engine: AsyncEngine
) -> None:
    """U-01's done-when: register, suspend, look up by registration number and
    serial, with every change in `events`."""
    operator = await an_operator(client)
    pilot_response = await client.post(
        "/uas/pilots",
        json={
            "name": "Nino Beridze",
            "license_ref": f"GEO-RP-{uuid4().hex[:8]}",
            "uas_operator_id": operator["id"],
            "competencies": [
                {"competency": "A1_A3", "valid_until": "2030-01-01T00:00:00Z"},
                {"competency": "A2", "certificate_ref": "A2-778"},
            ],
        },
    )
    assert pilot_response.status_code == 201, pilot_response.text
    pilot = pilot_response.json()
    assert [c["competency"] for c in pilot["competencies"]] == ["A1_A3", "A2"]
    first = await a_uas(client, operator["id"])
    second = await a_uas(
        client, operator["id"], serial="HOMEBUILT/7", class_label=None, mtom_g=1_900
    )

    for path in (
        f"/uas/operators/{operator['id']}/suspend",
        f"/uas/pilots/{pilot['id']}/suspend",
        f"/uas/aircraft/{first['id']}/suspend",
        f"/uas/aircraft/{second['id']}/suspend",
    ):
        response = await client.post(path, json={"reason": "inspection"})
        assert response.status_code == 200, (path, response.text)

    found = await client.get(
        "/uas/operators/lookup",
        params={"registration_number": operator["registration_number"].lower()},
    )
    assert found.status_code == 200
    assert (found.json()["id"], found.json()["status"]) == (operator["id"], "suspended")
    for uas in (first, second):
        by_serial = await client.get(
            "/uas/aircraft/lookup", params={"serial": uas["serial"]}
        )
        assert by_serial.status_code == 200
        body = by_serial.json()
        assert body["id"] == uas["id"]
        assert body["registration_status"] == "suspended"
        assert body["operator_status"] == "suspended"
        assert body["operator_registration_number"] == operator["registration_number"]
    pilot_now = (await client.get(f"/uas/pilots/{pilot['id']}")).json()
    assert pilot_now["registration_status"] == "suspended"

    assert await events_of(client, "uas_operator", operator["id"]) == [
        "registered",
        "suspended",
    ]
    assert await events_of(client, "pilot", pilot["id"]) == ["registered", "suspended"]
    for uas in (first, second):
        assert await events_of(client, "drone", uas["id"]) == [
            "registered",
            "suspended",
        ]

    # Suspension leaves the projection alone: the broadcast is still that aircraft.
    async with engine.connect() as connection:
        projected = (
            await connection.execute(
                sa.text(
                    "SELECT serial, retired_at FROM known_drones WHERE drone_id = :d"
                ),
                {"d": first["id"]},
            )
        ).one()
    assert (projected.serial, projected.retired_at) == (first["serial"], None)


# --- status: presence and absence ------------------------------------------------


async def test_a_suspended_operator_is_listed_as_suspended_and_an_active_one_is_not(
    client: AsyncClient,
) -> None:
    suspended = await an_operator(client)
    active = await an_operator(client)
    assert (
        await client.post(f"/uas/operators/{suspended['id']}/suspend")
    ).status_code == 200

    listed = await client.get("/uas/operators", params={"status": "suspended"})
    ids = {operator["id"] for operator in listed.json()}
    assert suspended["id"] in ids
    assert active["id"] not in ids
    still_active = await client.get(f"/uas/operators/{active['id']}")
    assert still_active.json()["status"] == "active"


async def test_reactivating_restores_active_and_is_audited(client: AsyncClient) -> None:
    operator = await an_operator(client)
    await client.post(f"/uas/operators/{operator['id']}/suspend")

    response = await client.post(f"/uas/operators/{operator['id']}/reactivate")

    assert response.json()["status"] == "active"
    assert await events_of(client, "uas_operator", operator["id"]) == [
        "registered",
        "suspended",
        "reactivated",
    ]


async def test_revoked_is_final_and_a_repeated_suspension_is_refused(
    client: AsyncClient,
) -> None:
    operator = await an_operator(client)
    await client.post(f"/uas/operators/{operator['id']}/suspend")

    again = await client.post(f"/uas/operators/{operator['id']}/suspend")
    assert again.status_code == 409
    assert again.json()["detail"]["code"] == "already_suspended"

    assert (
        await client.post(f"/uas/operators/{operator['id']}/revoke")
    ).status_code == 200
    back = await client.post(f"/uas/operators/{operator['id']}/reactivate")
    assert back.status_code == 409
    assert back.json()["detail"]["code"] == "revoked"
    assert await events_of(client, "uas_operator", operator["id"]) == [
        "registered",
        "suspended",
        "revoked",
    ]


async def test_nothing_new_is_attached_to_a_revoked_operator(
    client: AsyncClient,
) -> None:
    operator = await an_operator(client)
    await client.post(f"/uas/operators/{operator['id']}/revoke")

    response = await client.post(
        "/uas/aircraft",
        json={
            "serial": cta_serial(),
            "class_label": "C1",
            "mtom_g": 800,
            "uas_operator_id": operator["id"],
        },
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "operator_revoked"


# --- registration numbers --------------------------------------------------------


@pytest.mark.parametrize(
    "number",
    [
        "geo12345678",  # the country code must be upper case
        "GEO123",  # too short
        "GEO1234567890ABCDEFGH",  # too long
        "GEO123456789ABC-x9z",  # the secret part is never registered
        "GEO 1234 5678 9ABC",  # spaces inside
    ],
)
async def test_an_invalid_registration_number_is_refused_and_writes_nothing(
    client: AsyncClient, relational_engine: AsyncEngine, number: str
) -> None:
    async with relational_engine.connect() as connection:
        before: int = (
            await connection.execute(sa.text("SELECT count(*) FROM events"))
        ).scalar_one()

    response = await client.post(
        "/uas/operators",
        json={
            "registration_number": number,
            "legal_name": "Someone",
            "operator_type": "natural_person",
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "invalid_registration_number"
    async with relational_engine.connect() as connection:
        after: int = (
            await connection.execute(sa.text("SELECT count(*) FROM events"))
        ).scalar_one()
    assert after == before


async def test_a_valid_registration_number_is_accepted_as_given(
    client: AsyncClient,
) -> None:
    """The EASA example shape: a country code and lower-case characters."""
    number = f"FIN{uuid4().hex[:13]}"
    operator = await an_operator(client, registration_number=f"  {number} ")
    assert operator["registration_number"] == number
    assert operator["source"] == "manual"


async def test_the_configured_pattern_decides(
    relational_engine: AsyncEngine, engine: AsyncEngine
) -> None:
    """Georgia's format is configuration: a pattern that requires GEO refuses
    a Finnish number the default accepts, and accepts a Georgian one."""
    app = build_app(relational_engine, engine, pattern=r"^GEO[A-Z0-9]{13}$")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", headers=ADMIN_HEADERS
    ) as client:
        foreign = await client.post(
            "/uas/operators",
            json={
                "registration_number": f"FIN{uuid4().hex[:13].upper()}",
                "legal_name": "Suomi Oy",
                "operator_type": "legal_person",
            },
        )
        local = await client.post(
            "/uas/operators",
            json={
                "registration_number": registration_number(),
                "legal_name": "Kartli",
                "operator_type": "legal_person",
            },
        )
    assert foreign.status_code == 422
    assert local.status_code == 201


async def test_a_registration_number_differing_only_in_case_is_a_duplicate(
    client: AsyncClient,
) -> None:
    number = f"GEO{uuid4().hex[:13]}"
    await an_operator(client, registration_number=number)

    response = await client.post(
        "/uas/operators",
        json={
            "registration_number": "GEO" + number[3:].upper(),
            "legal_name": "Other",
            "operator_type": "natural_person",
        },
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "duplicate"


# --- serials and class labels ---------------------------------------------------


async def test_a_class_that_broadcasts_needs_a_cta_serial_and_one_without_does_not(
    client: AsyncClient,
) -> None:
    operator = await an_operator(client)
    serial = f"DJI-{uuid4().hex[:8]}"

    refused = await client.post(
        "/uas/aircraft",
        json={
            "serial": serial,
            "class_label": "C2",
            "mtom_g": 3_000,
            "uas_operator_id": operator["id"],
        },
    )
    assert refused.status_code == 422
    assert refused.json()["detail"]["code"] == "invalid_serial"

    accepted = await a_uas(client, operator["id"], serial=serial, class_label="C0")
    assert accepted["serial_cta2063"] is False
    cta = await a_uas(client, operator["id"])
    assert cta["serial_cta2063"] is True


async def test_a_new_class_label_is_checked_against_the_serial(
    client: AsyncClient,
) -> None:
    operator = await an_operator(client)
    legacy = await a_uas(
        client, operator["id"], serial=f"OLD-{uuid4().hex[:6]}", class_label=None
    )

    response = await client.patch(
        f"/uas/aircraft/{legacy['id']}", json={"class_label": "C3"}
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "invalid_serial"
    assert await events_of(client, "drone", legacy["id"]) == ["registered"]


async def test_a_registered_uas_is_projected_with_its_serial(
    client: AsyncClient, engine: AsyncEngine
) -> None:
    """Remote ID matching reads `known_drones.serial` (P1-15)."""
    operator = await an_operator(client)
    uas = await a_uas(client, operator["id"], label="kartli-7")
    async with engine.connect() as connection:
        row = (
            await connection.execute(
                sa.text("SELECT label, serial FROM known_drones WHERE drone_id = :d"),
                {"d": uas["id"]},
            )
        ).one()
    assert (row.label, row.serial) == ("kartli-7", uas["serial"])


async def test_a_serial_lookup_ignores_case_and_an_unknown_one_is_404(
    client: AsyncClient,
) -> None:
    operator = await an_operator(client)
    uas = await a_uas(client, operator["id"])

    found = await client.get(
        "/uas/aircraft/lookup", params={"serial": uas["serial"].lower()}
    )
    missing = await client.get("/uas/aircraft/lookup", params={"serial": "NOPE-0"})

    assert found.status_code == 200
    assert found.json()["id"] == uas["id"]
    assert missing.status_code == 404
    assert missing.json()["detail"]["code"] == "not_found"


async def test_a_duplicate_serial_is_refused(client: AsyncClient) -> None:
    operator = await an_operator(client)
    uas = await a_uas(client, operator["id"])
    response = await client.post(
        "/uas/aircraft",
        json={
            "serial": uas["serial"],
            "class_label": "C2",
            "mtom_g": 3_000,
            "uas_operator_id": operator["id"],
            "label": f"other-{uuid4().hex[:6]}",
        },
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "duplicate"


async def test_the_uas_list_filters_by_operator_and_leaves_out_the_fleet(
    client: AsyncClient,
) -> None:
    mine = await an_operator(client)
    other = await an_operator(client)
    a = await a_uas(client, mine["id"])
    b = await a_uas(client, other["id"])
    fleet = await client.post(
        "/drones", json={"serial": f"FLEET-{uuid4().hex[:8]}", "label": uuid4().hex}
    )
    assert fleet.status_code == 201

    by_operator = await client.get("/uas/aircraft", params={"operator_id": mine["id"]})
    everything = await client.get(
        "/uas/aircraft", params={"include_fleet": "true", "limit": 500}
    )
    default = await client.get("/uas/aircraft", params={"limit": 500})

    assert [u["id"] for u in by_operator.json()] == [a["id"]]
    assert fleet.json()["id"] in {u["id"] for u in everything.json()}
    default_ids = {u["id"] for u in default.json()}
    assert fleet.json()["id"] not in default_ids
    assert b["id"] in default_ids


# --- editing -----------------------------------------------------------------------


async def test_an_edit_is_audited_with_what_changed_and_a_no_op_writes_nothing(
    client: AsyncClient,
) -> None:
    operator = await an_operator(client)

    changed = await client.patch(
        f"/uas/operators/{operator['id']}",
        json={"contact_phone": "+995 555 000 111", "legal_name": "Kartli Aerial"},
    )
    same = await client.patch(
        f"/uas/operators/{operator['id']}", json={"legal_name": "Kartli Aerial"}
    )

    assert changed.status_code == 200
    assert changed.json()["contact_phone"] == "+995 555 000 111"
    assert same.status_code == 200
    events = (
        await client.get(
            "/events",
            params={"entity_type": "uas_operator", "entity_id": operator["id"]},
        )
    ).json()
    assert [e["event_type"] for e in events] == ["registered", "updated"]
    assert events[1]["payload"]["changes"]["legal_name"] == {
        "from": "Kartli Aerial LLC",
        "to": "Kartli Aerial",
    }


async def test_a_competency_is_recorded_and_replaced(client: AsyncClient) -> None:
    operator = await an_operator(client)
    pilot = (
        await client.post(
            "/uas/pilots", json={"name": "Giorgi", "uas_operator_id": operator["id"]}
        )
    ).json()

    await client.put(
        f"/uas/pilots/{pilot['id']}/competencies",
        json={"competency": "STS_01", "certificate_ref": "old"},
    )
    response = await client.put(
        f"/uas/pilots/{pilot['id']}/competencies",
        json={
            "competency": "STS_01",
            "certificate_ref": "new",
            "valid_until": "2031-06-30T00:00:00Z",
        },
    )

    assert response.status_code == 200
    assert [
        (c["competency"], c["certificate_ref"]) for c in response.json()["competencies"]
    ] == [("STS_01", "new")]
    assert await events_of(client, "pilot", pilot["id"]) == [
        "registered",
        "competency_recorded",
        "competency_recorded",
    ]


# --- who may do what -----------------------------------------------------------------


async def test_a_viewer_reads_the_registry_and_changes_none_of_it(
    relational_engine: AsyncEngine, engine: AsyncEngine, client: AsyncClient
) -> None:
    operator = await an_operator(client)
    async with AsyncClient(
        transport=ASGITransport(app=build_app(relational_engine, engine)),
        base_url="http://test",
        headers=VIEWER_HEADERS,
    ) as viewer:
        read = await viewer.get(f"/uas/operators/{operator['id']}")
        writes = [
            await viewer.post(
                "/uas/operators",
                json={
                    "registration_number": registration_number(),
                    "legal_name": "x",
                    "operator_type": "natural_person",
                },
            ),
            await viewer.post(f"/uas/operators/{operator['id']}/suspend"),
            await viewer.patch(
                f"/uas/operators/{operator['id']}", json={"legal_name": "y"}
            ),
        ]
    assert read.status_code == 200
    assert [w.status_code for w in writes] == [403, 403, 403]
    assert await events_of(client, "uas_operator", operator["id"]) == ["registered"]

"""Geographical zones through the API, against the real database. U-03.

Covers the done-when's file half: an ED-269 file round-trips unchanged
through import, the database and export, and an invalid one is refused with
a named reason and writes nothing. Every change must be in `events`.
"""

from __future__ import annotations

import copy
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest
import sqlalchemy as sa
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine

from airspace.ed269 import Restriction
from airspace.tests.test_ed269 import FIXTURE
from airspace.zones import load_zones
from api.app import create_api_app
from api.registry import FleetRegistry
from api.tests.auth_fakes import (
    ADMIN,
    ADMIN_HEADERS,
    OPERATOR_HEADERS,
    VIEWER_HEADERS,
    api_kwargs,
)
from api.zone_routes import MAX_IMPORT_BYTES

pytestmark = pytest.mark.postgres

FIXTURE_IDS = ("TST001", "TST002", "TST003", "TST004")


class NoProjection:
    async def register_drone(self, *args: Any, **kwargs: Any) -> None:
        return None


class NoLive:
    async def get(self, drone_id: Any) -> None:
        return None


@pytest.fixture
async def client(relational_engine: AsyncEngine) -> AsyncIterator[AsyncClient]:
    await _clean(relational_engine)
    registry = FleetRegistry(
        engine=relational_engine,
        projection=NoProjection(),  # type: ignore[arg-type]
        live=NoLive(),  # type: ignore[arg-type]
    )
    app = create_api_app(registry, **api_kwargs())
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", headers=ADMIN_HEADERS
    ) as http:
        yield http
    await _clean(relational_engine)


async def _clean(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            sa.text(
                "DELETE FROM airspace_zones WHERE identifier LIKE 'TST%' "
                "OR identifier LIKE 'API%'"
            )
        )


def fixture_json() -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(FIXTURE.read_bytes())
    return loaded


async def events(engine: AsyncEngine, entity_id: str) -> list[dict[str, Any]]:
    async with engine.connect() as connection:
        rows = await connection.execute(
            sa.text(
                "SELECT actor_type, actor_id, event_type, payload FROM events "
                "WHERE entity_type = 'airspace_zone' AND entity_id = :id ORDER BY id"
            ),
            {"id": entity_id},
        )
        return [dict(row) for row in rows.mappings()]


async def zone_count(engine: AsyncEngine) -> int:
    async with engine.connect() as connection:
        count: int = (
            await connection.execute(sa.text("SELECT count(*) FROM airspace_zones"))
        ).scalar_one()
        return count


def editor_zone(identifier: str = "API001", **changes: Any) -> dict[str, Any]:
    """What the console's editor sends: an ED-269 zone."""
    zone: dict[str, Any] = {
        "identifier": identifier,
        "country": "GEO",
        "name": "Drawn in the editor",
        "type": "COMMON",
        "restriction": "PROHIBITED",
        "reason": ["SENSITIVE"],
        "applicability": [{"permanent": "YES"}],
        "zoneAuthority": [{"name": "GCAA", "purpose": "AUTHORIZATION"}],
        "geometry": [
            {
                "uomDimensions": "M",
                "lowerLimit": 0,
                "lowerVerticalReference": "AGL",
                "upperLimit": 120,
                "upperVerticalReference": "AGL",
                "horizontalProjection": {
                    "type": "Polygon",
                    "coordinates": [
                        [
                            [44.8, 41.7],
                            [44.81, 41.7],
                            [44.81, 41.71],
                            [44.8, 41.7],
                        ]
                    ],
                },
            }
        ],
    }
    zone.update(changes)
    return zone


# --- the file: import, export, refusal ----------------------------------------------


async def test_an_ed269_file_round_trips_unchanged_through_the_database(
    client: AsyncClient, relational_engine: AsyncEngine
) -> None:
    before = await zone_count(relational_engine)
    dry = await client.post(
        "/airspace/zones/import", params={"dry_run": True}, content=FIXTURE.read_bytes()
    )
    assert dry.status_code == 200, dry.text
    assert dry.json()["created"] == list(FIXTURE_IDS)
    assert dry.json()["dry_run"] is True
    assert await zone_count(relational_engine) == before

    imported = await client.post("/airspace/zones/import", content=FIXTURE.read_bytes())
    assert imported.status_code == 200, imported.text
    assert imported.json()["created"] == list(FIXTURE_IDS)
    assert await zone_count(relational_engine) == before + 4

    exported = await client.get("/airspace/zones/export")
    assert exported.status_code == 200
    assert "attachment" in exported.headers["content-disposition"]
    features = [
        f for f in exported.json()["features"] if f["identifier"] in FIXTURE_IDS
    ]
    assert features == fixture_json()["features"]

    # Importing it again changes nothing.
    again = await client.post("/airspace/zones/import", content=FIXTURE.read_bytes())
    assert again.json()["unchanged"] == list(FIXTURE_IDS)
    assert again.json()["created"] == again.json()["updated"] == []


async def test_an_import_replaces_a_changed_zone_and_audits_it(
    client: AsyncClient, relational_engine: AsyncEngine
) -> None:
    await client.post("/airspace/zones/import", content=FIXTURE.read_bytes())
    changed = fixture_json()
    changed["features"][1]["restriction"] = "PROHIBITED"
    data = json.dumps(changed).encode()

    report = (await client.post("/airspace/zones/import", content=data)).json()

    assert report["updated"] == ["TST002"]
    assert sorted(report["unchanged"]) == ["TST001", "TST003", "TST004"]
    listed = (await client.get("/airspace/zones")).json()
    (zone,) = [z for z in listed if z["feature"]["identifier"] == "TST002"]
    assert zone["feature"]["restriction"] == "PROHIBITED"
    trail = await events(relational_engine, str(zone["id"]))
    assert [e["event_type"] for e in trail] == ["zone_created", "zone_updated"]
    assert trail[1]["payload"]["source"] == "ed269_import"
    assert trail[1]["payload"]["before"]["restriction"] == "REQ_AUTHORISATION"
    assert trail[1]["actor_id"] == str(ADMIN.id)
    async with relational_engine.connect() as connection:
        imports: dict[str, Any] = (
            await connection.execute(
                sa.text(
                    "SELECT payload FROM events WHERE entity_type = "
                    "'airspace_zone_import' AND entity_id = :sha"
                ),
                {"sha": report["sha256"]},
            )
        ).scalar_one()
    assert imports["updated"] == ["TST002"]


async def test_an_invalid_file_is_refused_with_named_reasons_and_writes_nothing(
    client: AsyncClient, relational_engine: AsyncEngine
) -> None:
    broken = fixture_json()
    broken["features"][0]["geometry"][0]["upperVerticalReference"] = "FL"
    broken["features"][3]["identifier"] = "TOOLONG1"
    before = await zone_count(relational_engine)

    for params in ({"dry_run": True}, {}):
        response = await client.post(
            "/airspace/zones/import", params=params, content=json.dumps(broken)
        )
        assert response.status_code == 422
        detail = response.json()["detail"]
        assert detail["code"] == "invalid_ed269"
        assert {p["field"] for p in detail["problems"]} == {
            "features[0].geometry[0].upperVerticalReference",
            "features[3].identifier",
        }
        assert "upperVerticalReference" in detail["message"]
    assert await zone_count(relational_engine) == before


async def test_not_json_is_refused_with_a_reason(client: AsyncClient) -> None:
    response = await client.post("/airspace/zones/import", content=b"{oops")
    assert response.status_code == 422
    assert response.json()["detail"]["problems"][0]["reason"].startswith("not JSON")


async def test_an_import_does_not_overwrite_a_corridor(
    client: AsyncClient, relational_engine: AsyncEngine
) -> None:
    async with relational_engine.begin() as connection:
        await connection.execute(
            sa.text(
                "INSERT INTO airspace_zones (name, type, geom, identifier, country, "
                " ed269_type, restriction, zone_authority, applicability, "
                " uom_dimensions, lower_reference, upper_reference) "
                "VALUES ('corridor', 'corridor', ST_GeomFromText("
                " 'POLYGON((44 41, 44.1 41, 44.1 41.1, 44 41))', 4326), 'TST001', "
                " 'GEO', 'COMMON', 'NO_RESTRICTION', '[]', "
                " '[{\"permanent\": \"YES\"}]', 'M', 'AMSL', 'AMSL')"
            )
        )
    before = await zone_count(relational_engine)
    response = await client.post("/airspace/zones/import", content=FIXTURE.read_bytes())
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "zone_import_refused"
    assert detail["problems"][0]["field"] == "features[0].identifier"
    assert await zone_count(relational_engine) == before


# --- the editor: create, read, replace, delete ------------------------------------


async def test_a_zone_drawn_in_the_editor_is_stored_audited_and_monitored(
    client: AsyncClient, relational_engine: AsyncEngine
) -> None:
    created = await client.post("/airspace/zones", json=editor_zone())
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["type"] == "geozone"
    assert body["active_now"] is True
    assert body["feature"] == editor_zone()
    zone_id = body["id"]

    trail = await events(relational_engine, zone_id)
    assert [e["event_type"] for e in trail] == ["zone_created"]
    assert trail[0]["actor_type"] == "operator"
    assert trail[0]["payload"]["feature"]["identifier"] == "API001"

    # What the monitor loads on its next refresh.
    (monitored,) = [
        z for z in await load_zones(relational_engine) if z.identifier == "API001"
    ]
    assert monitored.restriction is Restriction.PROHIBITED
    assert monitored.contains_horizontally(41.702, 44.805)

    # An edit is what the monitor loads next.
    replaced = await client.put(
        f"/airspace/zones/{zone_id}", json=editor_zone(restriction="CONDITIONAL")
    )
    assert replaced.status_code == 200
    (monitored,) = [
        z for z in await load_zones(relational_engine) if z.identifier == "API001"
    ]
    assert monitored.restriction is Restriction.CONDITIONAL
    trail = await events(relational_engine, zone_id)
    assert [e["event_type"] for e in trail] == ["zone_created", "zone_updated"]
    assert trail[1]["payload"]["before"]["restriction"] == "PROHIBITED"
    assert trail[1]["payload"]["after"]["restriction"] == "CONDITIONAL"

    # The same zone again writes nothing.
    await client.put(
        f"/airspace/zones/{zone_id}", json=editor_zone(restriction="CONDITIONAL")
    )
    assert len(await events(relational_engine, zone_id)) == 2

    deleted = await client.delete(f"/airspace/zones/{zone_id}")
    assert deleted.status_code == 204
    assert (await client.get(f"/airspace/zones/{zone_id}")).status_code == 404
    trail = await events(relational_engine, zone_id)
    assert trail[-1]["event_type"] == "zone_deleted"
    assert trail[-1]["payload"]["feature"]["restriction"] == "CONDITIONAL"
    assert not [
        z for z in await load_zones(relational_engine) if z.identifier == "API001"
    ]


async def test_a_circle_is_kept_as_published_and_drawn_as_a_polygon(
    client: AsyncClient,
) -> None:
    circle = editor_zone("API002")
    circle["geometry"][0]["uomDimensions"] = "FT"
    circle["geometry"][0]["horizontalProjection"] = {
        "type": "Circle",
        "center": [44.79, 41.69],
        "radius": 1000,
    }
    created = await client.post("/airspace/zones", json=circle)
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["feature"]["geometry"][0]["horizontalProjection"] == {
        "type": "Circle",
        "center": [44.79, 41.69],
        "radius": 1000,
    }
    ring = body["geometry"]["coordinates"][0]
    assert body["geometry"]["type"] == "Polygon"
    assert len(ring) == 65


async def test_an_invalid_zone_is_refused_with_the_field_named(
    client: AsyncClient, relational_engine: AsyncEngine
) -> None:
    before = await zone_count(relational_engine)
    unclosed = editor_zone()
    unclosed["geometry"][0]["horizontalProjection"]["coordinates"][0].pop()
    for body, field in (
        (editor_zone("API00001"), "zone.identifier"),
        (unclosed, "zone.geometry[0].horizontalProjection.coordinates[0]"),
        (
            editor_zone(applicability=[{"permanent": "NO"}]),
            "zone.applicability[0]",
        ),
    ):
        response = await client.post("/airspace/zones", json=body)
        assert response.status_code == 422, response.text
        detail = response.json()["detail"]
        assert detail["code"] == "invalid_ed269"
        assert [p["field"] for p in detail["problems"]] == [field]
    # A value outside an enumeration is refused by the request model.
    response = await client.post(
        "/airspace/zones", json=editor_zone(restriction="FORBIDDEN")
    )
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", "restriction"]
    assert await zone_count(relational_engine) == before


async def test_a_duplicate_identifier_is_refused(client: AsyncClient) -> None:
    assert (await client.post("/airspace/zones", json=editor_zone())).status_code == 201
    again = await client.post("/airspace/zones", json=editor_zone())
    assert again.status_code == 409
    assert again.json()["detail"]["code"] == "duplicate"


async def test_a_corridor_is_not_edited_here(
    client: AsyncClient, relational_engine: AsyncEngine
) -> None:
    async with relational_engine.begin() as connection:
        corridor_id: str = (
            await connection.execute(
                sa.text(
                    "INSERT INTO airspace_zones (name, type, geom, identifier, "
                    " country, ed269_type, restriction, zone_authority, "
                    " applicability, uom_dimensions, lower_reference, "
                    " upper_reference) "
                    "VALUES ('corridor', 'corridor', ST_GeomFromText("
                    " 'POLYGON((44 41, 44.1 41, 44.1 41.1, 44 41))', 4326), "
                    " 'API009', 'GEO', 'COMMON', 'NO_RESTRICTION', '[]', "
                    " '[{\"permanent\": \"YES\"}]', 'M', 'AMSL', 'AMSL') RETURNING id"
                )
            )
        ).scalar_one()
    for response in (
        await client.put(f"/airspace/zones/{corridor_id}", json=editor_zone("API009")),
        await client.delete(f"/airspace/zones/{corridor_id}"),
    ):
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "not_a_geozone"


async def test_active_now_follows_the_applicability(client: AsyncClient) -> None:
    past = editor_zone(
        "API003",
        applicability=[
            {
                "permanent": "NO",
                "startDateTime": "2020-01-01T00:00:00Z",
                "endDateTime": "2020-02-01T00:00:00Z",
            }
        ],
    )
    now = datetime.now(UTC)
    current = editor_zone(
        "API004",
        applicability=[
            {
                "permanent": "NO",
                "startDateTime": "2020-01-01T00:00:00Z",
                "endDateTime": now.replace(year=now.year + 1).isoformat(),
            }
        ],
    )
    assert (await client.post("/airspace/zones", json=past)).json()[
        "active_now"
    ] is False
    assert (await client.post("/airspace/zones", json=current)).json()[
        "active_now"
    ] is True


# --- who may change zones -----------------------------------------------------------


async def test_a_viewer_and_an_operator_read_zones_and_change_none(
    client: AsyncClient, relational_engine: AsyncEngine
) -> None:
    created = (await client.post("/airspace/zones", json=editor_zone())).json()
    before = await zone_count(relational_engine)
    for headers in (VIEWER_HEADERS, OPERATOR_HEADERS):
        assert (await client.get("/airspace/zones", headers=headers)).status_code == 200
        assert (
            await client.get("/airspace/zones/export", headers=headers)
        ).status_code == 200
        for response in (
            await client.post(
                "/airspace/zones", json=editor_zone("API005"), headers=headers
            ),
            await client.put(
                f"/airspace/zones/{created['id']}",
                json=editor_zone(restriction="CONDITIONAL"),
                headers=headers,
            ),
            await client.delete(f"/airspace/zones/{created['id']}", headers=headers),
            await client.post(
                "/airspace/zones/import",
                content=FIXTURE.read_bytes(),
                headers=headers,
            ),
        ):
            assert response.status_code == 403
            assert response.json()["detail"]["code"] == "forbidden"
            assert "admin" in response.json()["detail"]["message"]
    assert await zone_count(relational_engine) == before
    assert len(await events(relational_engine, str(created["id"]))) == 1


async def test_the_unwritten_file_is_still_a_valid_import(client: AsyncClient) -> None:
    """The presence pair of the refusals: the base fixture imports."""
    data = copy.deepcopy(fixture_json())
    data["features"] = [f for f in data["features"] if f["identifier"] == "TST001"]
    response = await client.post("/airspace/zones/import", json=data)
    assert response.status_code == 200
    assert response.json()["created"] == ["TST001"]


# --- review findings: geometry, numbers, nesting, size ----------------------------

BOW_TIE = [[44.8, 41.7], [44.81, 41.71], [44.81, 41.7], [44.8, 41.71], [44.8, 41.7]]
COLLINEAR = [[44.8, 41.7], [44.805, 41.7], [44.81, 41.7], [44.8, 41.7]]


def with_ring(zone: dict[str, Any], ring: list[list[float]]) -> dict[str, Any]:
    zone["geometry"][0]["horizontalProjection"]["coordinates"] = [ring]
    return zone


@pytest.mark.parametrize("ring", [BOW_TIE, COLLINEAR], ids=["bow tie", "collinear"])
async def test_an_invalid_polygon_is_refused_on_create_and_import(
    client: AsyncClient, relational_engine: AsyncEngine, ring: list[list[float]]
) -> None:
    before = await zone_count(relational_engine)
    created = await client.post("/airspace/zones", json=with_ring(editor_zone(), ring))
    assert created.status_code == 422, created.text
    (problem,) = created.json()["detail"]["problems"]
    assert problem["field"] == "zone.geometry[0].horizontalProjection"
    assert problem["reason"].startswith("not a valid polygon")

    document = {"features": [with_ring(editor_zone("API010"), ring)]}
    imported = await client.post("/airspace/zones/import", json=document)
    assert imported.status_code == 422
    assert imported.json()["detail"]["problems"][0]["field"] == (
        "features[0].geometry[0].horizontalProjection"
    )
    assert await zone_count(relational_engine) == before


async def test_an_invalid_polygon_is_refused_on_replace(client: AsyncClient) -> None:
    created = (await client.post("/airspace/zones", json=editor_zone())).json()
    replaced = await client.put(
        f"/airspace/zones/{created['id']}", json=with_ring(editor_zone(), BOW_TIE)
    )
    assert replaced.status_code == 422
    kept = (await client.get(f"/airspace/zones/{created['id']}")).json()
    assert kept["feature"]["geometry"] == editor_zone()["geometry"]


async def test_a_string_where_a_number_belongs_is_refused_not_converted(
    client: AsyncClient,
) -> None:
    zone = editor_zone()
    zone["geometry"][0]["upperLimit"] = "120"
    response = await client.post("/airspace/zones", json=zone)
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"][-1] == "upperLimit"
    # The same zone with the number is accepted.
    zone["geometry"][0]["upperLimit"] = 120
    assert (await client.post("/airspace/zones", json=zone)).status_code == 201


async def test_deeply_nested_json_is_refused_with_a_reason(client: AsyncClient) -> None:
    deep = b'{"features": ' + b"[" * 100_000 + b"]" * 100_000 + b"}"
    imported = await client.post("/airspace/zones/import", content=deep)
    assert imported.status_code == 422
    assert imported.json()["detail"]["problems"][0]["reason"] == (
        "nested too deeply to be ED-269"
    )
    created = await client.post(
        "/airspace/zones",
        content=b'{"identifier": ' + b"[" * 100_000 + b"]" * 100_000 + b"}",
        headers={"Content-Type": "application/json"},
    )
    assert created.status_code == 400, created.text


async def test_an_oversized_import_is_refused_by_its_declared_length(
    client: AsyncClient,
) -> None:
    response = await client.post(
        "/airspace/zones/import",
        content=b"{}",
        headers={"Content-Length": str(MAX_IMPORT_BYTES + 1)},
    )
    assert response.status_code == 413
    assert response.json()["detail"]["code"] == "too_large"


async def test_an_oversized_import_without_a_length_is_refused_as_it_streams(
    client: AsyncClient,
) -> None:
    async def chunks() -> AsyncIterator[bytes]:
        for _ in range(MAX_IMPORT_BYTES // 65536 + 2):
            yield b" " * 65536

    response = await client.post("/airspace/zones/import", content=chunks())
    assert response.status_code == 413
    assert response.json()["detail"]["code"] == "too_large"

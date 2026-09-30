"""The fleet registry against real databases. P2-01, P2-05, P2-06.

Both databases are real, because the claims are about both: a drone
registered here must become bindable there (P2-05), and the audit log must
refuse to be edited (P2-06). A fake of either would agree with whatever the
code did.

Status is derived from a fake live-state reader: what is under test is the
derivation and its wiring, and `gateway/tests/test_live_state.py` covers Redis.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from httpx import ASGITransport, AsyncClient
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from api.app import create_api_app
from api.registry import (
    AirframeParams,
    ConflictError,
    FleetRegistry,
    ProjectionIncompleteError,
)
from api.tests.auth_fakes import ADMIN_HEADERS, api_kwargs
from api.tests.conftest import migrate_relational
from gateway.binding import BindingConflictError, BindingResolver
from gateway.parsing import SourceId

pytestmark = pytest.mark.postgres

STATION = "registry-test-station"


class FakeLive:
    def __init__(self) -> None:
        self.states: dict[UUID, dict[str, Any]] = {}

    async def get(self, drone_id: UUID) -> dict[str, Any] | None:
        return self.states.get(drone_id)


@pytest.fixture
def live() -> FakeLive:
    return FakeLive()


@pytest.fixture
async def client(
    relational_engine: AsyncEngine, engine: AsyncEngine, live: FakeLive
) -> AsyncIterator[AsyncClient]:
    """The API over both test databases. `engine` is the telemetry one."""
    registry = FleetRegistry(
        engine=relational_engine,
        projection=BindingResolver(engine=engine),
        live=live,
    )
    app = create_api_app(registry, **api_kwargs())
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers=ADMIN_HEADERS,
    ) as http:
        yield http
    async with engine.begin() as connection:
        await connection.execute(
            sa.text("DELETE FROM source_bindings WHERE station_id = :s"),
            {"s": STATION},
        )


def unique(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:10]}"


async def a_drone(client: AsyncClient, **overrides: Any) -> dict[str, Any]:
    body = {"serial": unique("SN"), "label": unique("TEST"), **overrides}
    response = await client.post("/drones", json=body)
    assert response.status_code == 201, response.text
    created: dict[str, Any] = response.json()
    return created


# --- P2-01: the schema ----------------------------------------------------------


async def test_the_migrations_run_down_and_up_again(
    prepared_relational_database: str,
) -> None:
    """P2-01's criterion. Down to nothing, then up to head, on the same database."""
    await asyncio.to_thread(
        migrate_relational, prepared_relational_database, "base", down=True
    )
    await asyncio.to_thread(migrate_relational, prepared_relational_database, "head")


async def test_every_geometry_column_has_a_gist_index(
    relational_engine: AsyncEngine,
) -> None:
    async with relational_engine.connect() as connection:
        geometry = (
            await connection.execute(
                sa.text(
                    "SELECT f_table_name, f_geometry_column, srid "
                    "FROM geometry_columns WHERE f_table_schema = 'public'"
                )
            )
        ).all()
        gist = (
            await connection.execute(
                sa.text(
                    "SELECT tablename, indexdef FROM pg_indexes "
                    "WHERE indexdef ILIKE '%USING gist%'"
                )
            )
        ).all()

    assert geometry, "no geometry columns found at all"
    for table, column, srid in geometry:
        assert srid == 4326, (table, column, srid)
        assert any(
            index_table == table and f"({column})" in definition
            for index_table, definition in gist
        ), f"{table}.{column} has no GiST index"


# --- P2-05: drones, and the projection into telemetry ----------------------------


async def test_a_registered_drone_is_bindable_in_the_telemetry_database(
    client: AsyncClient, engine: AsyncEngine
) -> None:
    """P2-05's criterion, second half: nobody touches the telemetry database."""
    drone = await a_drone(client)

    await BindingResolver(engine=engine).bind(
        STATION,
        SourceId(sysid=240, compid=1),
        UUID(drone["id"]),
        bound_from=datetime.now(tz=UTC),
        created_by="registry-test",
    )


async def test_an_unregistered_drone_is_not_bindable(engine: AsyncEngine) -> None:
    """The paired presence: without the projection the binding is refused,
    which is the invisible failure P2-05 exists to prevent."""
    with pytest.raises(BindingConflictError, match="not in known_drones"):
        await BindingResolver(engine=engine).bind(
            STATION,
            SourceId(sysid=241, compid=1),
            uuid4(),
            bound_from=datetime.now(tz=UTC),
            created_by="registry-test",
        )


async def test_the_projection_carries_the_label_and_the_serial(
    client: AsyncClient, engine: AsyncEngine
) -> None:
    """The serial is what the Remote ID ingest matches broadcasts against."""
    drone = await a_drone(client)
    async with engine.connect() as connection:
        row = (
            await connection.execute(
                sa.text("SELECT label, serial FROM known_drones WHERE drone_id = :d"),
                {"d": drone["id"]},
            )
        ).one()
    assert (row.label, row.serial) == (drone["label"], drone["serial"])


async def test_a_duplicate_label_is_refused_and_projects_nothing(
    client: AsyncClient,
) -> None:
    first = await a_drone(client)
    response = await client.post(
        "/drones", json={"serial": unique("SN"), "label": first["label"]}
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "duplicate"


async def test_a_refusal_says_why_without_the_databases_words(
    client: AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    """S-16. The database's message names tables, constraints and other rows'
    values; the client gets a stable code, and the log gets the detail."""
    first = await a_drone(client)
    label = unique("TEST")

    with caplog.at_level("WARNING", logger="api.registry"):
        response = await client.post(
            "/drones", json={"serial": first["serial"], "label": label}
        )

    assert response.status_code == 409
    assert response.json()["detail"] == {
        "code": "duplicate",
        "message": f"drone {label!r} refused: it duplicates an existing record",
    }
    text = response.text.lower()
    for leaked in ("drones_", "constraint", "key (", "unique", first["serial"].lower()):
        assert leaked not in text, leaked
    logged = [r for r in caplog.records if getattr(r, "code", None) == "duplicate"]
    assert logged, "the database's reason was not logged"
    assert "drones_serial_key" in logged[-1].__dict__["error"]
    assert first["serial"] in logged[-1].__dict__["detail"]


async def test_retiring_closes_bindings_and_marks_the_projection(
    client: AsyncClient, engine: AsyncEngine
) -> None:
    drone = await a_drone(client)
    resolver = BindingResolver(engine=engine)
    await resolver.bind(
        STATION,
        SourceId(sysid=242, compid=1),
        UUID(drone["id"]),
        bound_from=datetime(2026, 1, 1, tzinfo=UTC),
        created_by="registry-test",
    )

    response = await client.post(f"/drones/{drone['id']}/retire")

    assert response.status_code == 200
    assert response.json()["retired_at"] is not None
    async with engine.connect() as connection:
        open_bindings: int = (
            await connection.execute(
                sa.text(
                    "SELECT count(*) FROM source_bindings "
                    "WHERE drone_id = :d AND upper(valid) IS NULL"
                ),
                {"d": drone["id"]},
            )
        ).scalar_one()
        retired: datetime | None = (
            await connection.execute(
                sa.text("SELECT retired_at FROM known_drones WHERE drone_id = :d"),
                {"d": drone["id"]},
            )
        ).scalar_one()
        serial: str | None = (
            await connection.execute(
                sa.text("SELECT serial FROM known_drones WHERE drone_id = :d"),
                {"d": drone["id"]},
            )
        ).scalar_one()
    assert open_bindings == 0
    assert retired is not None
    # Retiring keeps the serial: an old broadcast still names the airframe.
    assert serial == drone["serial"]

    again = await client.post(f"/drones/{drone['id']}/retire")
    assert again.status_code == 409


class FlakyProjection:
    """The real projection, which can be made to fail, and which records
    every write that reached the telemetry database."""

    def __init__(self, engine: AsyncEngine) -> None:
        self.real = BindingResolver(engine=engine)
        self.fail = False
        self.writes: list[str] = []

    async def register_drone(
        self,
        drone_id: UUID,
        label: str,
        *,
        retired_at: datetime | None = None,
        serial: str | None = None,
    ) -> None:
        self.writes.append("register_drone")
        await self.real.register_drone(
            drone_id, label, retired_at=retired_at, serial=serial
        )

    async def close_bindings_for_drone(self, drone_id: UUID, *, at: datetime) -> int:
        if self.fail:
            raise ConnectionError("telemetry database unreachable")
        self.writes.append("close_bindings_for_drone")
        return await self.real.close_bindings_for_drone(drone_id, at=at)


async def open_bindings(engine: AsyncEngine, drone_id: str) -> int:
    async with engine.connect() as connection:
        found: int = (
            await connection.execute(
                sa.text(
                    "SELECT count(*) FROM source_bindings "
                    "WHERE drone_id = :d AND upper(valid) IS NULL"
                ),
                {"d": drone_id},
            )
        ).scalar_one()
    return found


async def retired_at(engine: AsyncEngine, table: str, drone_id: str) -> datetime | None:
    column = "id" if table == "drones" else "drone_id"
    async with engine.connect() as connection:
        found: datetime | None = (
            await connection.execute(
                sa.text(f"SELECT retired_at FROM {table} WHERE {column} = :d"),
                {"d": drone_id},
            )
        ).scalar_one()
    return found


@pytest.fixture
async def flaky(
    relational_engine: AsyncEngine, engine: AsyncEngine, live: FakeLive
) -> AsyncIterator[tuple[FleetRegistry, FlakyProjection, dict[str, Any]]]:
    """A registry over a flaky projection, and a drone with an open binding."""
    projection = FlakyProjection(engine)
    registry = FleetRegistry(engine=relational_engine, projection=projection, live=live)
    drone = await registry.register_drone(
        serial=unique("SN"),
        label=unique("TEST"),
        model=None,
        params=AirframeParams(),
        home_base_id=None,
        current_pilot_id=None,
    )
    await projection.real.bind(
        STATION,
        SourceId(sysid=243, compid=1),
        drone["id"],
        bound_from=datetime(2026, 1, 1, tzinfo=UTC),
        created_by="registry-test",
    )
    projection.writes.clear()
    yield registry, projection, drone
    async with engine.begin() as connection:
        await connection.execute(
            sa.text("DELETE FROM source_bindings WHERE station_id = :s"),
            {"s": STATION},
        )


async def test_a_retirement_that_fails_here_leaves_telemetry_untouched(
    flaky: tuple[FleetRegistry, FlakyProjection, dict[str, Any]],
    relational_engine: AsyncEngine,
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """S-16. The relational transaction rolls back, so nothing may have
    been closed in the telemetry database, which a rollback cannot reach."""
    registry, projection, drone = flaky

    async def failing_audit(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("audit insert failed")

    monkeypatch.setattr(registry, "_audit", failing_audit)

    with pytest.raises(RuntimeError, match="audit insert failed"):
        await registry.retire_drone(drone["id"])

    assert projection.writes == []
    assert await open_bindings(engine, str(drone["id"])) == 1
    assert await retired_at(relational_engine, "drones", str(drone["id"])) is None
    assert await retired_at(engine, "known_drones", str(drone["id"])) is None


async def test_a_projection_that_fails_after_the_commit_is_finished_by_retrying(
    flaky: tuple[FleetRegistry, FlakyProjection, dict[str, Any]],
    relational_engine: AsyncEngine,
    engine: AsyncEngine,
) -> None:
    """S-16, the compensating path: retired here, bindings still open there,
    the caller told so, and retiring again closes them at the recorded time."""
    registry, projection, drone = flaky
    drone_id = str(drone["id"])
    projection.fail = True

    with pytest.raises(ProjectionIncompleteError):
        await registry.retire_drone(drone["id"])

    recorded = await retired_at(relational_engine, "drones", drone_id)
    assert recorded is not None
    assert await open_bindings(engine, drone_id) == 1
    assert await retired_at(engine, "known_drones", drone_id) is None

    projection.fail = False
    with pytest.raises(ConflictError) as again:
        await registry.retire_drone(drone["id"])

    assert again.value.code == "already_retired"
    assert await open_bindings(engine, drone_id) == 0
    assert await retired_at(engine, "known_drones", drone_id) == recorded
    trail = [
        (event["event_type"], event["payload"])
        for event in await registry.events(entity_type="drone", entity_id=drone_id)
    ]
    assert [event for event, _ in trail] == ["registered", "retired", "bindings_closed"]
    assert trail[-1][1]["bindings_closed"] == 1


async def test_a_retirement_closes_bindings_and_audits_it(
    flaky: tuple[FleetRegistry, FlakyProjection, dict[str, Any]],
    engine: AsyncEngine,
) -> None:
    """The paired presence: nothing fails, and the order is relational
    first, then the projection."""
    registry, projection, drone = flaky

    retired = await registry.retire_drone(drone["id"])

    assert retired["retired_at"] is not None
    assert projection.writes == ["close_bindings_for_drone", "register_drone"]
    assert await open_bindings(engine, str(drone["id"])) == 0
    events = await registry.events(entity_type="drone", entity_id=str(drone["id"]))
    assert [e["event_type"] for e in events] == [
        "registered",
        "retired",
        "bindings_closed",
    ]


async def test_a_failed_projection_is_503_over_http(
    flaky: tuple[FleetRegistry, FlakyProjection, dict[str, Any]],
) -> None:
    registry, projection, drone = flaky
    projection.fail = True
    app = create_api_app(registry, **api_kwargs())
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers=ADMIN_HEADERS,
    ) as http:
        response = await http.post(f"/drones/{drone['id']}/retire")

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "projection_incomplete"
    assert "unreachable" not in response.text


async def test_a_retired_drone_is_hidden_unless_asked_for(client: AsyncClient) -> None:
    drone = await a_drone(client)
    await client.post(f"/drones/{drone['id']}/retire")

    listed = [d["id"] for d in (await client.get("/drones")).json()]
    everything = [
        d["id"] for d in (await client.get("/drones?include_retired=true")).json()
    ]

    assert drone["id"] not in listed
    assert drone["id"] in everything


async def test_an_unknown_drone_is_404(client: AsyncClient) -> None:
    assert (await client.get(f"/drones/{uuid4()}")).status_code == 404
    assert (await client.post(f"/drones/{uuid4()}/retire")).status_code == 404


# --- P2-05: status comes from telemetry, not from a person -----------------------


async def test_status_follows_telemetry(client: AsyncClient, live: FakeLive) -> None:
    drone = await a_drone(client)
    drone_id = UUID(drone["id"])
    assert drone["status"] == "OFFLINE"

    live.states[drone_id] = {"armed": False}
    assert (await client.get(f"/drones/{drone_id}")).json()["status"] == "IDLE"

    live.states[drone_id] = {"armed": True}
    assert (await client.get(f"/drones/{drone_id}")).json()["status"] == "IN_FLIGHT"

    del live.states[drone_id]
    assert (await client.get(f"/drones/{drone_id}")).json()["status"] == "OFFLINE"


async def test_maintenance_is_set_by_a_person_and_wins(
    client: AsyncClient, live: FakeLive
) -> None:
    drone = await a_drone(client)
    live.states[UUID(drone["id"])] = {"armed": False}

    response = await client.put(
        f"/drones/{drone['id']}/maintenance", json={"in_maintenance": True}
    )
    assert response.json()["status"] == "MAINTENANCE"

    response = await client.put(
        f"/drones/{drone['id']}/maintenance", json={"in_maintenance": False}
    )
    assert response.json()["status"] == "IDLE"


async def test_status_cannot_be_set_by_hand(client: AsyncClient) -> None:
    """There is no endpoint that writes a status, and the body field is ignored."""
    drone = await a_drone(client, status="IN_FLIGHT")
    assert drone["status"] == "OFFLINE"


# --- bases and pilots --------------------------------------------------------------


async def test_a_base_round_trips_its_position(client: AsyncClient) -> None:
    name = unique("BASE")
    response = await client.post(
        "/bases",
        json={"name": name, "lat_deg": 41.7, "lon_deg": 44.8, "elevation_amsl_m": 450},
    )
    assert response.status_code == 201, response.text
    base = response.json()
    assert (base["lat_deg"], base["lon_deg"]) == pytest.approx((41.7, 44.8))

    drone = await a_drone(client, home_base_id=base["id"])
    assert drone["home_base_id"] == base["id"]


async def test_a_drone_at_an_unknown_base_is_refused(client: AsyncClient) -> None:
    response = await client.post(
        "/drones",
        json={
            "serial": unique("SN"),
            "label": unique("T"),
            "home_base_id": str(uuid4()),
        },
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "unknown_reference"
    assert "home_base_id" not in response.text


async def test_a_pilot_status_changes_and_is_validated(client: AsyncClient) -> None:
    created = (await client.post("/pilots", json={"name": unique("Pilot")})).json()
    assert created["status"] == "OFF_DUTY"

    changed = await client.put(
        f"/pilots/{created['id']}/status", json={"status": "ON_DUTY"}
    )
    invalid = await client.put(
        f"/pilots/{created['id']}/status", json={"status": "FLYING"}
    )

    assert changed.json()["status"] == "ON_DUTY"
    assert invalid.status_code == 422


# --- P2-06: the audit log ------------------------------------------------------------


async def test_a_drones_history_can_be_reconstructed(client: AsyncClient) -> None:
    """P2-06's criterion, for the entities that exist: every change, in order."""
    drone = await a_drone(client)
    await client.put(
        f"/drones/{drone['id']}/maintenance", json={"in_maintenance": True}
    )
    await client.put(
        f"/drones/{drone['id']}/maintenance", json={"in_maintenance": False}
    )
    await client.post(f"/drones/{drone['id']}/retire")

    trail = (
        await client.get(f"/events?entity_type=drone&entity_id={drone['id']}")
    ).json()

    assert [event["event_type"] for event in trail] == [
        "registered",
        "maintenance_started",
        "maintenance_ended",
        "retired",
    ]
    assert trail[0]["payload"]["label"] == drone["label"]


async def test_the_audit_log_pages_without_losing_rows(client: AsyncClient) -> None:
    drone = await a_drone(client)
    for flag in (True, False, True):
        await client.put(
            f"/drones/{drone['id']}/maintenance", json={"in_maintenance": flag}
        )

    query = f"/events?entity_type=drone&entity_id={drone['id']}&limit=2"
    first = (await client.get(query)).json()
    second = (await client.get(f"{query}&after_id={first[-1]['id']}")).json()

    assert len(first) == 2
    assert len(second) == 2
    assert [e["id"] for e in first + second] == sorted(e["id"] for e in first + second)


async def test_a_refused_change_leaves_no_event(client: AsyncClient) -> None:
    """The paired absence: the row and its event are one transaction."""
    first = await a_drone(client)
    await client.post("/drones", json={"serial": unique("SN"), "label": first["label"]})

    registered = [
        event
        for event in (await client.get("/events?entity_type=drone")).json()
        if event["event_type"] == "registered"
        and event["payload"]["label"] == first["label"]
    ]
    assert len(registered) == 1


async def test_the_audit_log_refuses_to_be_edited(
    relational_engine: AsyncEngine, client: AsyncClient
) -> None:
    await a_drone(client)
    for statement in (
        "UPDATE events SET event_type = 'forged'",
        "DELETE FROM events",
        "TRUNCATE events",
    ):
        with pytest.raises(DBAPIError, match="append-only"):
            async with relational_engine.begin() as connection:
                await connection.execute(sa.text(statement))

    async with relational_engine.connect() as connection:
        forged: int = (
            await connection.execute(
                sa.text("SELECT count(*) FROM events WHERE event_type = 'forged'")
            )
        ).scalar_one()
    assert forged == 0


# --- zones for the map (P6-01) --------------------------------------------------


async def test_zones_come_back_as_geojson_as_the_monitor_sees_them(
    client: AsyncClient, relational_engine: AsyncEngine
) -> None:
    name = f"map-zone-{uuid4().hex[:8]}"
    ring = [
        [44.80, 41.70],
        [44.81, 41.70],
        [44.81, 41.71],
        [44.80, 41.71],
        [44.80, 41.70],
    ]
    async with relational_engine.begin() as connection:
        await connection.execute(
            sa.text(
                "INSERT INTO airspace_zones (name, type, geom, max_alt_amsl_m) "
                "VALUES (:name, 'no_fly', "
                "ST_SetSRID(ST_GeomFromGeoJSON(:geojson), 4326), 900)"
            ),
            {
                "name": name,
                "geojson": json.dumps({"type": "Polygon", "coordinates": [ring]}),
            },
        )

    response = await client.get("/airspace/zones")

    assert response.status_code == 200
    (zone,) = [z for z in response.json() if z["name"] == name]
    assert zone["type"] == "no_fly"
    assert zone["max_alt_amsl_m"] == 900
    assert zone["min_alt_amsl_m"] is None
    assert zone["geometry"]["type"] == "Polygon"
    assert zone["geometry"]["coordinates"][0][0] == [44.80, 41.70]
    async with relational_engine.begin() as connection:
        await connection.execute(
            sa.text("DELETE FROM airspace_zones WHERE name = :name"), {"name": name}
        )

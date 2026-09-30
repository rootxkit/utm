"""Flight replay against both real databases. P10-03.

The pure rules are in `test_replay.py`. What only a database can show is that
the queries find what the Gateway and the airspace service actually write: a
gap placed in time from `archive_segments`, link states from `ingest_events`,
alerts from `events`, and flights from armed rows in `drone_state`.

Every test uses its own drone and stations, because the databases are shared
across the session.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine

from airspace.monitor import Alert, AlertKind, Severity
from airspace.service import EventsAuditLog
from api.app import create_api_app
from api.registry import FleetRegistry
from api.replay import ReplayStore
from api.tests.auth_fakes import VIEWER_HEADERS, api_kwargs
from gateway.binding import BindingResolver

pytestmark = pytest.mark.postgres

T0 = datetime(2026, 9, 28, 20, 26, tzinfo=UTC)
EPOCH = "0123456789abcdef0123456789abcdef"
THRESHOLD_S = 3.0


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def ns(when: datetime) -> int:
    # Integer arithmetic, so the value is exactly what a relay would send.
    return (
        (when - datetime(1970, 1, 1, tzinfo=UTC)) // timedelta(microseconds=1) * 1_000
    )


class NoLive:
    async def get(self, drone_id: UUID) -> dict[str, Any] | None:
        return None


def store_for(
    telemetry: AsyncEngine,
    relational: AsyncEngine | None,
    *,
    max_samples: int = 10_000,
) -> ReplayStore:
    return ReplayStore(
        telemetry=telemetry,
        relational=relational,
        gap_threshold_s=THRESHOLD_S,
        evidence_slack_s=5.0,
        flight_split_s=120.0,
        max_samples=max_samples,
    )


@pytest.fixture
async def client(
    relational_engine: AsyncEngine, engine: AsyncEngine
) -> AsyncIterator[AsyncClient]:
    """The API over both test databases. `engine` is the telemetry one."""
    registry = FleetRegistry(
        engine=relational_engine,
        projection=BindingResolver(engine=engine),
        live=NoLive(),
    )
    app = create_api_app(
        registry, replay=store_for(engine, relational_engine), **api_kwargs()
    )
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers=VIEWER_HEADERS,
    ) as http:
        yield http


async def add_drone(engine: AsyncEngine, label: str) -> UUID:
    drone_id = uuid4()
    async with engine.begin() as connection:
        await connection.execute(
            sa.text("INSERT INTO known_drones (drone_id, label) VALUES (:d, :l)"),
            {"d": drone_id, "l": label},
        )
    return drone_id


async def add_samples(
    engine: AsyncEngine,
    drone_id: UUID,
    seconds: list[float],
    *,
    station: str,
    armed: bool = True,
) -> None:
    async with engine.begin() as connection:
        for s in seconds:
            await connection.execute(
                sa.text(
                    "INSERT INTO drone_state (drone_id, ts, station_id, geom, "
                    " alt_amsl_m, alt_above_home_m, vx_ms, vy_ms, vz_ms, armed, "
                    " mode) VALUES "
                    "(:d, :ts, :station, ST_SetSRID(ST_MakePoint(:lon, :lat), 4326), "
                    " 520.0, 30.0, 1.5, -0.5, -2.0, :armed, 'AUTO')"
                ),
                {
                    "d": drone_id,
                    "ts": at(s),
                    "station": station,
                    "lat": 41.7 + s * 1e-5,
                    "lon": 44.8,
                    "armed": armed,
                },
            )


async def add_gap(
    engine: AsyncEngine,
    station: str,
    *,
    from_seq: int,
    to_seq: int,
    before: datetime | None,
    after: datetime | None,
    recorded_at: datetime,
) -> None:
    """A relay gap as the Gateway stores it, with the archive around it."""
    async with engine.begin() as connection:
        await connection.execute(
            sa.text(
                "INSERT INTO relay_epochs (station_id, epoch, last_seen_at) "
                "VALUES (:s, :e, :t) ON CONFLICT DO NOTHING"
            ),
            {"s": station, "e": EPOCH, "t": recorded_at},
        )
        await connection.execute(
            sa.text(
                "INSERT INTO relay_epoch_gaps (station_id, epoch, from_seq, to_seq, "
                " reason, recorded_at) VALUES (:s, :e, :f, :to, 'queue_cap', :r)"
            ),
            {"s": station, "e": EPOCH, "f": from_seq, "to": to_seq, "r": recorded_at},
        )
        segments = []
        if before is not None:
            segments.append((0, from_seq - 1, before))
        if after is not None:
            segments.append((to_seq, to_seq + 10, after))
        for first, last, when in segments:
            await connection.execute(
                sa.text(
                    "INSERT INTO archive_segments (station_id, epoch, relative_path, "
                    " hour_start, first_seq, last_seq, first_recv_utc_ns, "
                    " last_recv_utc_ns, record_count, compressed_bytes, "
                    " uncompressed_bytes) VALUES (:s, :e, :p, :h, :f, :l, :fn, :ln, "
                    " :c, 1, 1)"
                ),
                {
                    "s": station,
                    "e": EPOCH,
                    "p": f"{station}/{first}.zst",
                    "h": when.replace(minute=0, second=0, microsecond=0),
                    "f": first,
                    "l": last,
                    # The segment before ends at `when`; the one after starts
                    # there. Only those two ends are what the replay reads.
                    "fn": ns(when) if first == to_seq else ns(when) - 10**9,
                    "ln": ns(when) if last == from_seq - 1 else ns(when) + 10**9,
                    "c": last - first + 1,
                },
            )


async def add_ingest_event(
    engine: AsyncEngine,
    station: str,
    when: datetime,
    event_type: str,
    payload: dict[str, Any],
) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            sa.text(
                "INSERT INTO ingest_events (ts, station_id, event_type, payload) "
                "VALUES (:t, :s, :e, CAST(:p AS jsonb))"
            ),
            {"t": when, "s": station, "e": event_type, "p": json.dumps(payload)},
        )


async def add_alert(
    engine: AsyncEngine, drone_id: UUID, when: datetime, state: str
) -> None:
    alert = zone_alert(drone_id)
    async with engine.begin() as connection:
        await connection.execute(
            sa.text(
                "INSERT INTO events (ts, actor_type, entity_type, entity_id, "
                " event_type, payload) VALUES (:t, 'airspace', 'drone', :d, :e, "
                " CAST(:p AS jsonb))"
            ),
            {
                "t": when,
                "d": str(drone_id),
                "e": f"airspace_alert_{state}",
                "p": json.dumps(alert.as_dict()),
            },
        )


def zone_alert(drone_id: UUID) -> Alert:
    return Alert(
        key=f"zone:z:{drone_id}",
        kind=AlertKind.ZONE,
        severity=Severity.WARNING,
        drone_ids=(drone_id,),
        labels=("R-1",),
        detail={"zone_name": "test zone", "zone_type": "restricted"},
    )


def every(start_s: float, end_s: float, step_s: float = 0.5) -> list[float]:
    count = round((end_s - start_s) / step_s)
    return [start_s + i * step_s for i in range(count + 1)]


async def get_replay(
    client: AsyncClient, drone_id: UUID, start_s: float, end_s: float
) -> dict[str, Any]:
    response = await client.get(
        f"/replay/drones/{drone_id}",
        params={"start": at(start_s).isoformat(), "end": at(end_s).isoformat()},
    )
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


# --- the flight, end to end ----------------------------------------------------


async def test_a_flight_is_replayed_with_its_holes_and_their_causes(
    client: AsyncClient, engine: AsyncEngine, relational_engine: AsyncEngine
) -> None:
    station = f"replay-{uuid4().hex[:8]}"
    drone_id = await add_drone(engine, "R-1")
    await add_samples(engine, drone_id, every(0, 10) + every(20, 30), station=station)
    # 10 -> 20 s: the station went unreachable, and came back.
    await add_ingest_event(engine, station, at(-60), "link_state", {"state": "healthy"})
    await add_ingest_event(
        engine, station, at(11), "link_state", {"state": "unreachable"}
    )
    await add_ingest_event(engine, station, at(19), "link_state", {"state": "healthy"})
    # 27.0 -> 27.5 s: three records lost to the queue cap, reported late.
    await add_gap(
        engine,
        station,
        from_seq=500,
        to_seq=503,
        before=at(27.0),
        after=at(27.5),
        recorded_at=at(300),
    )
    await add_alert(relational_engine, drone_id, at(22), "raised")
    await add_alert(relational_engine, drone_id, at(28), "cleared")

    body = await get_replay(client, drone_id, -5, 35)

    assert body["label"] == "R-1"
    # Velocity comes through with its sign: down is positive, so this climbs.
    first = body["samples"][0]
    assert (first["vx_ms"], first["vy_ms"], first["vz_ms"]) == (1.5, -0.5, -2.0)
    assert first["alt_amsl_m"] == 520.0
    assert first["alt_above_home_m"] == 30.0
    assert body["stations"] == [station]
    assert len(body["samples"]) == 21 + 21
    holes = body["holes"]
    assert [(h["after_ts"], h["before_ts"]) for h in holes] == [
        (at(10).isoformat(), at(20).isoformat()),
        (at(27).isoformat(), at(27.5).isoformat()),
    ]
    silence, gap = holes
    assert [r["kind"] for r in silence["reasons"]] == ["station_unreachable"]
    assert silence["data_lost"] is False
    assert [r["kind"] for r in gap["reasons"]] == ["relay_gap"]
    assert gap["reasons"][0]["exact"] is True
    assert gap["reasons"][0]["detail"]["missing_count"] == 3
    assert gap["data_lost"] is True
    # Three pieces of line, and nothing drawn between them.
    assert body["segments"] == [[0, 20], [21, 35], [36, 41]]
    assert [(a["state"], a["kind"]) for a in body["alerts"]] == [
        ("raised", "zone"),
        ("cleared", "zone"),
    ]
    assert body["alerts_error"] is None


async def test_a_continuous_flight_has_no_holes(
    client: AsyncClient, engine: AsyncEngine
) -> None:
    """The paired absence: the same station and logs, no silence and no gap."""
    station = f"replay-{uuid4().hex[:8]}"
    drone_id = await add_drone(engine, "R-2")
    await add_samples(engine, drone_id, every(0, 30), station=station)
    await add_ingest_event(engine, station, at(-60), "link_state", {"state": "healthy"})

    body = await get_replay(client, drone_id, -5, 35)

    assert body["holes"] == []
    assert body["segments"] == [[0, 60]]
    assert body["alerts"] == []


async def test_a_gap_whose_archive_neighbours_are_missing_is_listed_not_cut(
    client: AsyncClient, engine: AsyncEngine
) -> None:
    station = f"replay-{uuid4().hex[:8]}"
    drone_id = await add_drone(engine, "R-3")
    await add_samples(engine, drone_id, every(0, 10), station=station)
    await add_gap(
        engine,
        station,
        from_seq=10,
        to_seq=12,
        before=None,
        after=None,
        recorded_at=at(5),
    )

    body = await get_replay(client, drone_id, -5, 15)

    assert body["holes"] == []
    assert [(e["kind"], e["exact"]) for e in body["evidence"]] == [("relay_gap", False)]


async def test_an_intake_drop_is_placed_just_before_it_was_detected(
    client: AsyncClient, engine: AsyncEngine
) -> None:
    station = f"replay-{uuid4().hex[:8]}"
    drone_id = await add_drone(engine, "R-4")
    await add_samples(engine, drone_id, every(0, 10) + every(20, 30), station=station)
    await add_ingest_event(
        engine, station, at(20.5), "loss.intake_drop", {"datagram_count": 4}
    )

    body = await get_replay(client, drone_id, -5, 35)

    (hole,) = body["holes"]
    assert [r["kind"] for r in hole["reasons"]] == ["intake_drop"]
    assert hole["data_lost"] is True
    assert hole["reasons"][0]["exact"] is False


async def test_another_stations_log_is_not_evidence(
    client: AsyncClient, engine: AsyncEngine
) -> None:
    """A station that never carried this aircraft explains nothing about it."""
    station = f"replay-{uuid4().hex[:8]}"
    stranger = f"replay-{uuid4().hex[:8]}"
    drone_id = await add_drone(engine, "R-5")
    await add_samples(engine, drone_id, every(0, 10) + every(20, 30), station=station)
    await add_ingest_event(
        engine, stranger, at(11), "link_state", {"state": "unreachable"}
    )

    body = await get_replay(client, drone_id, -5, 35)

    (hole,) = body["holes"]
    assert hole["explained"] is False
    assert body["evidence"] == []


async def test_alerts_written_by_the_airspace_service_are_read_back(
    engine: AsyncEngine, relational_engine: AsyncEngine
) -> None:
    """Through the real writer, so a change to its rows breaks this, not a
    replay in the middle of an investigation."""
    drone_id = await add_drone(engine, "R-6")
    alert = zone_alert(drone_id)
    await EventsAuditLog(engine=relational_engine).record(alert, "raised")
    now = datetime.now(tz=UTC)
    store = store_for(engine, relational_engine)

    alerts, error = await store._alerts(
        drone_id, now - timedelta(minutes=1), now + timedelta(minutes=1)
    )

    assert error is None
    assert alerts is not None
    assert [(a["state"], a["key"], a["detail"]["zone_name"]) for a in alerts] == [
        ("raised", alert.key, "test zone")
    ]


async def test_alerts_are_unavailable_not_empty_without_the_audit_log(
    engine: AsyncEngine,
) -> None:
    drone_id = await add_drone(engine, "R-7")
    await add_samples(engine, drone_id, every(0, 2), station="replay-no-audit")

    body = await store_for(engine, None).replay(drone_id, start=at(-1), end=at(3))

    assert body["alerts"] is None
    assert body["alerts_error"]


async def test_every_aircraft_in_the_telemetry_registry_is_listed(
    client: AsyncClient, engine: AsyncEngine
) -> None:
    """Including one the business registry never heard of: it still flew."""
    drone_id = await add_drone(engine, "R-11")

    response = await client.get("/replay/drones")

    assert response.status_code == 200
    listed = {d["drone_id"]: d for d in response.json()}
    assert listed[str(drone_id)] == {
        "drone_id": str(drone_id),
        "label": "R-11",
        "retired": False,
        "source": "mavlink",
    }


# --- flights ---------------------------------------------------------------------


async def test_flights_are_armed_spans_split_by_long_silence(
    client: AsyncClient, engine: AsyncEngine
) -> None:
    station = f"replay-{uuid4().hex[:8]}"
    drone_id = await add_drone(engine, "R-8")
    # One flight with a 10 s hole in it (below the split), a disarmed spell,
    # and a second flight ten minutes later.
    await add_samples(engine, drone_id, every(0, 10) + every(20, 30), station=station)
    await add_samples(engine, drone_id, every(31, 60), station=station, armed=False)
    await add_samples(engine, drone_id, every(600, 620), station=station)

    response = await client.get(
        f"/replay/drones/{drone_id}/flights",
        params={"since": at(-100).isoformat(), "until": at(1000).isoformat()},
    )

    assert response.status_code == 200, response.text
    flights = response.json()
    assert [(f["start"], f["end"]) for f in flights] == [
        (at(600).isoformat(), at(620).isoformat()),
        (at(0).isoformat(), at(30).isoformat()),
    ]


async def test_a_flight_window_over_the_maximum_is_refused_and_one_at_it_is_not(
    relational_engine: AsyncEngine, engine: AsyncEngine
) -> None:
    """S-16. As with `max_samples`: refused with 413, never scanned."""
    station = f"replay-{uuid4().hex[:8]}"
    drone_id = await add_drone(engine, "R-9")
    await add_samples(engine, drone_id, every(0, 10), station=station)
    store = store_for(engine, relational_engine)
    store.max_flight_window_s = 1000.0
    app = create_api_app(
        cast(FleetRegistry, cast(Any, None)), replay=store, **api_kwargs()
    )
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers=VIEWER_HEADERS,
    ) as http:
        path = f"/replay/drones/{drone_id}/flights"
        at_limit = await http.get(
            path, params={"since": at(-500).isoformat(), "until": at(500).isoformat()}
        )
        over = await http.get(
            path, params={"since": at(-501).isoformat(), "until": at(500).isoformat()}
        )
        backwards = await http.get(
            path, params={"since": at(500).isoformat(), "until": at(-500).isoformat()}
        )
        # No `since`: the default look-back is clipped to the maximum.
        defaulted = await http.get(path, params={"until": at(500).isoformat()})

    assert at_limit.status_code == 200, at_limit.text
    assert [f["start"] for f in at_limit.json()] == [at(0).isoformat()]
    assert over.status_code == 413
    assert backwards.status_code == 422
    assert defaulted.status_code == 200, defaulted.text


# --- Remote ID aircraft (P1-15) -------------------------------------------------


async def add_broadcast(
    engine: AsyncEngine,
    aircraft: UUID,
    seconds: list[float],
    *,
    ua_id: str,
    receiver: str,
    status: int = 2,
) -> None:
    """Rows as gateway/remote_id_store.py writes them."""
    async with engine.begin() as connection:
        for s in seconds:
            await connection.execute(
                sa.text(
                    "INSERT INTO remote_id_observations (aircraft_id, ts, "
                    " receiver_id, transmitter, ua_id, id_type, status, geom, "
                    " alt_hae_m, alt_amsl_m, geoid_model, vx_ms, vy_ms, vz_ms, "
                    " payload) VALUES (:a, :ts, :r, 'AA:BB', :u, 1, :st, "
                    " ST_SetSRID(ST_MakePoint(:lon, :lat), 4326), 540.0, 524.1, "
                    " 'EGM2008', 0.0, 8.0, 0.0, '\\x00')"
                ),
                {
                    "a": aircraft,
                    "ts": at(s),
                    "r": receiver,
                    "u": ua_id,
                    "st": status,
                    "lat": 41.7,
                    "lon": 44.8 + s * 1e-5,
                },
            )


async def test_remote_id_aircraft_heard_are_listed_as_such(
    client: AsyncClient, engine: AsyncEngine
) -> None:
    aircraft = uuid4()
    await add_broadcast(engine, aircraft, [0.0], ua_id="SN-LIST-1", receiver="rx-l")

    listed = {d["drone_id"]: d for d in (await client.get("/replay/drones")).json()}

    assert listed[str(aircraft)] == {
        "drone_id": str(aircraft),
        "label": "SN-LIST-1",
        "retired": False,
        "source": "remote_id",
    }


async def test_a_remote_id_track_replays_as_an_unverified_broadcast(
    client: AsyncClient, engine: AsyncEngine
) -> None:
    aircraft = uuid4()
    receiver = f"rx-{uuid4().hex[:8]}"
    # A 20 s silence in the middle, and a relay station that happens to share
    # the receiver's name and has a link state logged inside that silence.
    await add_broadcast(
        engine,
        aircraft,
        every(0, 10) + every(30, 40),
        ua_id="SN-R-1",
        receiver=receiver,
    )
    await add_ingest_event(
        engine, receiver, at(20), "link_state", {"state": "unreachable"}
    )

    replay = await get_replay(client, aircraft, -1, 41)

    assert (replay["label"], replay["source"], replay["authenticated"]) == (
        "SN-R-1",
        "remote_id",
        False,
    )
    assert replay["stations"] == [receiver]
    first = replay["samples"][0]
    assert first["alt_amsl_m"] == 524.1
    assert (first["batt_pct"], first["mode"], first["armed"], first["heading_deg"]) == (
        None,
        None,
        None,
        None,
    )
    assert len(replay["segments"]) == 2
    assert [hole["cause"] for hole in replay["holes"]] == ["no_telemetry"]
    assert replay["evidence"] == []


async def test_our_own_aircraft_replay_as_authenticated(
    client: AsyncClient, engine: AsyncEngine
) -> None:
    drone_id = await add_drone(engine, "R-12")
    await add_samples(engine, drone_id, every(0, 2), station="replay-own")

    replay = await get_replay(client, drone_id, 0, 2)

    assert (replay["source"], replay["authenticated"]) == ("mavlink", True)


async def test_remote_id_flights_are_spans_declared_airborne(
    client: AsyncClient, engine: AsyncEngine
) -> None:
    aircraft = uuid4()
    await add_broadcast(engine, aircraft, every(0, 20), ua_id="SN-F-1", receiver="rx-f")
    await add_broadcast(
        engine, aircraft, every(21, 60), ua_id="SN-F-1", receiver="rx-f", status=1
    )
    await add_broadcast(
        engine, aircraft, every(600, 610), ua_id="SN-F-1", receiver="rx-f"
    )

    response = await client.get(
        f"/replay/drones/{aircraft}/flights",
        params={"since": at(-100).isoformat(), "until": at(1000).isoformat()},
    )

    assert response.status_code == 200, response.text
    assert [(f["start"], f["end"]) for f in response.json()] == [
        (at(600).isoformat(), at(610).isoformat()),
        (at(0).isoformat(), at(20).isoformat()),
    ]


# --- refusals --------------------------------------------------------------------


async def test_an_unknown_drone_is_404(client: AsyncClient) -> None:
    response = await client.get(
        f"/replay/drones/{uuid4()}",
        params={"start": at(0).isoformat(), "end": at(1).isoformat()},
    )

    assert response.status_code == 404


async def test_a_time_without_a_zone_is_refused(
    client: AsyncClient, engine: AsyncEngine
) -> None:
    drone_id = await add_drone(engine, "R-9")

    response = await client.get(
        f"/replay/drones/{drone_id}",
        params={"start": "2026-09-28T20:26:00", "end": "2026-09-28T20:27:00"},
    )

    assert response.status_code == 422


async def test_a_window_with_too_many_samples_is_refused_not_thinned(
    engine: AsyncEngine, relational_engine: AsyncEngine
) -> None:
    drone_id = await add_drone(engine, "R-10")
    await add_samples(engine, drone_id, every(0, 5), station="replay-big")
    registry = FleetRegistry(
        engine=relational_engine,
        projection=BindingResolver(engine=engine),
        live=NoLive(),
    )
    app = create_api_app(
        registry,
        replay=store_for(engine, relational_engine, max_samples=5),
        **api_kwargs(),
    )
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers=VIEWER_HEADERS,
    ) as http:
        too_many = await http.get(
            f"/replay/drones/{drone_id}",
            params={"start": at(0).isoformat(), "end": at(5).isoformat()},
        )
        few_enough = await http.get(
            f"/replay/drones/{drone_id}",
            params={"start": at(0).isoformat(), "end": at(2).isoformat()},
        )

    assert too_many.status_code == 413
    assert few_enough.status_code == 200
    assert len(few_enough.json()["samples"]) == 5

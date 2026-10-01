"""Network Remote ID: the F3411 Display Provider client against a fake SP. U-02.

The SP is `tools/fake_rid_sp.py`, served in-process through httpx's ASGI
transport: the same requests, the same OAuth exchange and the same JSON a
USSP would see and send, with no port and no clock of its own.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import httpx
import pytest

from common.sources import NETWORK_REMOTE_ID, Control, SourceControlState
from common.uas_identity import RegistrationStatus
from gateway.network_rid import (
    Area,
    AuthError,
    FormatError,
    ServiceProviderClient,
    TokenSource,
    flight_aircraft_id,
    parse_details,
    parse_flights,
    parse_time,
    place,
)
from gateway.network_rid_ingest import ProviderPoller
from gateway.registry_projection import OperatorFacts, RegistrySnapshot, UasFacts
from gateway.source_activity import SourceActivity
from gateway.tests.rid_frames import FlatGeoid
from tools.fake_rid_sp import Flight, FlightStore, create_app

PROVIDER = "fake-ussp"
CLIENT_ID = "utm-test"
SECRET = "s3cret-for-tests-only"
NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
LAT, LON = 41.7151, 44.8271
# 0.02 x 0.02 degrees: about 2.6 km across at Tbilisi, inside one view.
SMALL = Area(LAT - 0.01, LON - 0.01, LAT + 0.01, LON + 0.01)

SERIAL = "1581F5FKD2290002"
OPERATOR = "GEOOPERATOR0001"
REGISTERED = UUID(int=7)


def registry() -> RegistrySnapshot:
    return RegistrySnapshot(
        uas=(
            UasFacts(
                REGISTERED, "uas-02", SERIAL, RegistrationStatus.ACTIVE, UUID(int=1)
            ),
        ),
        operators=(OperatorFacts(UUID(int=1), OPERATOR, RegistrationStatus.ACTIVE),),
    )


class FakeBus:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, Any]]] = []

    async def publish(self, subject: str, payload: bytes) -> None:
        self.sent.append((subject, json.loads(payload)))


def flight(
    flight_id: str = "f-1",
    *,
    serial: str | None = SERIAL,
    operator_id: str | None = OPERATOR,
    lat: float = LAT,
    lon: float = LON,
    at: datetime = NOW - timedelta(seconds=0.5),
) -> Flight:
    return Flight(
        flight_id=flight_id,
        serial=serial,
        operator_id=operator_id,
        lat_deg=lat,
        lon_deg=lon,
        alt_hae_m=520.0,
        timestamp=at,
        track_deg=90.0,
        speed_ms=10.0,
        vertical_speed_ms=1.0,
        height_over_takeoff_m=30.0,
    )


class Sp:
    """The fake SP and a poller pointed at it."""

    def __init__(
        self,
        *,
        secret: str = SECRET,
        sp_max_km: float = 7.0,
        client_max_km: float = 7.0,
        area: Area = SMALL,
        switch: SourceControlState | None = None,
    ) -> None:
        self.store = FlightStore(clock_s=lambda: 0.0)
        self.app = create_app(
            self.store,
            client_id=CLIENT_ID,
            client_secret=SECRET,
            max_diagonal_km=sp_max_km,
            wall=lambda: NOW,
        )
        self.http = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://sp.test"
        )
        self.bus = FakeBus()
        holder = None if switch is None else _Holder(switch)
        self.sources = SourceActivity(
            source_type=NETWORK_REMOTE_ID, switch=holder, known=[PROVIDER]
        )
        self.poller = ProviderPoller(
            provider=PROVIDER,
            client=ServiceProviderClient(
                http=self.http,
                base_url="http://sp.test",
                tokens=TokenSource(
                    http=self.http,
                    token_url="http://sp.test/token",
                    client_id=CLIENT_ID,
                    client_secret=secret,
                ),
            ),
            areas=[area],
            bus=self.bus,
            registry=registry,
            sources=self.sources,
            geoid=FlatGeoid(),
            max_diagonal_km=client_max_km,
            wall=lambda: NOW,
            clock_s=lambda: 0.0,
        )

    @property
    def counters(self) -> Any:
        return self.app.state.counters

    async def close(self) -> None:
        await self.http.aclose()


class _Holder:
    def __init__(self, state: SourceControlState) -> None:
        self.state = state


def disabled(instance: str | None = PROVIDER) -> SourceControlState:
    return SourceControlState(
        version=1,
        controls=(
            Control(
                NETWORK_REMOTE_ID, instance, False, "test", "admin", NOW.isoformat()
            ),
        ),
    )


# --- polling the SP --------------------------------------------------------------


async def test_a_flight_is_fetched_with_its_details_and_published() -> None:
    sp = Sp()
    sp.store.put(flight())

    await sp.poller.poll()

    [(subject, message)] = sp.bus.sent
    assert subject == f"telemetry.{REGISTERED}"
    assert message["source"] == "network_remote_id"
    assert message["trust"] == "provider"
    assert message["authenticated"] is False
    assert message["station_id"] == PROVIDER
    assert message["label"] == "uas-02"
    assert message["lat_deg"] == pytest.approx(LAT)
    # HAE 520 m less the flat 20 m geoid.
    assert message["alt_amsl_m"] == pytest.approx(500.0)
    assert message["alt_source"] == "geodetic"
    assert message["alt_above_home_m"] == pytest.approx(30.0)
    assert message["vy_ms"] == pytest.approx(10.0)
    assert message["vz_ms"] == pytest.approx(-1.0)
    assert message["airborne"] is True
    assert message["network_rid"]["flight_id"] == "f-1"
    assert message["network_rid"]["serial"] == SERIAL
    assert message["identification"]["status"] == "registered"
    assert sp.counters.tokens_issued == 1
    assert sp.counters.details_requests == 1
    await sp.close()


async def test_time_is_placed_by_the_response_clock_not_ours() -> None:
    """The state is 0.5 s behind the SP's response time, so it is placed
    0.5 s before our receive time whatever the SP's clock says."""
    sp = Sp()
    sp.store.put(flight(at=NOW - timedelta(seconds=0.5)))

    await sp.poller.poll()

    [(_, message)] = sp.bus.sent
    assert message["rx_ts"] == NOW.isoformat()
    assert datetime.fromisoformat(message["captured_at"]) == NOW - timedelta(
        seconds=0.5
    )
    assert message["ts"] == (NOW - timedelta(seconds=0.5)).isoformat()
    assert message["backlog"] is False
    assert message["network_rid"]["time_source"] == "broadcast"
    await sp.close()


async def test_an_unchanged_state_is_published_once() -> None:
    sp = Sp()
    sp.store.put(flight())

    await sp.poller.poll()
    await sp.poller.poll()

    assert len(sp.bus.sent) == 1
    assert sp.poller.unchanged == 1
    # Details are cached: fetched once for both polls.
    assert sp.counters.details_requests == 1
    sp.store.put(flight(at=NOW))
    await sp.poller.poll()
    assert len(sp.bus.sent) == 2
    await sp.close()


async def test_a_state_older_than_the_near_real_time_period_is_not_shown() -> None:
    sp = Sp()
    sp.store.put(flight(at=NOW - timedelta(seconds=61)))

    await sp.poller.poll()

    assert sp.bus.sent == []
    assert sp.poller.too_old == 1
    await sp.close()


async def test_a_flight_outside_the_area_is_not_returned() -> None:
    sp = Sp()
    sp.store.put(flight(lat=LAT + 0.5))

    await sp.poller.poll()

    assert sp.bus.sent == []
    assert sp.poller.polls == 1
    await sp.close()


@pytest.mark.parametrize(
    ("serial", "operator", "status"),
    [
        (SERIAL, OPERATOR, "registered"),
        (SERIAL, "GEOSOMEONEELSE1", "unknown_operator"),
        ("SN-NOT-REGISTERED", OPERATOR, "unknown_operator"),
        (None, OPERATOR, "unidentified"),
    ],
)
async def test_each_flight_is_identified(
    serial: str | None, operator: str, status: str
) -> None:
    sp = Sp()
    sp.store.put(flight(serial=serial, operator_id=operator))

    await sp.poller.poll()

    [(_, message)] = sp.bus.sent
    assert message["identification"]["status"] == status
    if serial is None:
        assert message["drone_id"] == str(flight_aircraft_id(PROVIDER, "f-1"))
    assert sp.poller.status()[f"identified_{status}"] == 1
    await sp.close()


# --- paging: tiles and 413 --------------------------------------------------------


async def test_a_large_area_is_polled_in_tiles_and_a_flight_counted_once() -> None:
    # About 13 km across: two by two tiles under a 7 km diagonal.
    big = Area(LAT - 0.05, LON - 0.05, LAT + 0.05, LON + 0.05)
    sp = Sp(area=big)
    # On the shared corner of all four tiles: every tile returns it.
    sp.store.put(flight("corner", lat=LAT, lon=LON))
    sp.store.put(flight("north-east", lat=LAT + 0.04, lon=LON + 0.04, serial=None))

    await sp.poller.poll()

    assert sp.counters.flights_requests == len(big.tiles(7.0)) > 1
    assert sp.counters.too_large == 0
    flights = sorted(m["network_rid"]["flight_id"] for _, m in sp.bus.sent)
    assert flights == ["corner", "north-east"]
    await sp.close()


async def test_a_view_the_sp_refuses_as_too_large_is_split() -> None:
    """The SP's limit is smaller than the client's: 413, then quarters."""
    big = Area(LAT - 0.02, LON - 0.02, LAT + 0.02, LON + 0.02)
    sp = Sp(area=big, sp_max_km=3.0, client_max_km=7.0)
    sp.store.put(flight(lat=LAT + 0.015, lon=LON - 0.015))

    await sp.poller.poll()

    assert sp.counters.too_large == 1
    assert sp.counters.flights_requests == 5
    assert len(sp.bus.sent) == 1
    await sp.close()


async def test_a_view_still_too_large_after_splitting_is_a_counted_failure() -> None:
    sp = Sp(sp_max_km=0.01)
    sp.store.put(flight())

    await sp.poller.poll()

    assert sp.bus.sent == []
    assert sp.poller.provider_errors == 1
    await sp.close()


def test_tiles_cover_the_area_and_respect_the_limit() -> None:
    area = Area(41.0, 44.0, 41.5, 44.8)
    tiles = area.tiles(7.0)
    assert all(tile.diagonal_km() <= 7.0 for tile in tiles)
    assert min(t.lat_min for t in tiles) == area.lat_min
    assert max(t.lon_max for t in tiles) == pytest.approx(area.lon_max)
    assert SMALL.tiles(7.0) == [SMALL]
    with pytest.raises(ValueError):
        area.tiles(0)
    with pytest.raises(ValueError):
        Area(42.0, 44.0, 41.0, 45.0)


# --- auth ---------------------------------------------------------------------


async def test_a_refused_client_is_counted_and_nothing_crashes() -> None:
    sp = Sp(secret="wrong")
    sp.store.put(flight())

    await sp.poller.poll()
    await sp.poller.poll()

    assert sp.bus.sent == []
    assert sp.poller.auth_failures == 2
    assert sp.counters.tokens_refused == 2
    assert sp.counters.flights_requests == 0
    await sp.close()


async def test_a_token_the_sp_no_longer_accepts_is_replaced_once() -> None:
    sp = Sp()
    sp.store.put(flight())
    await sp.poller.poll()
    # The SP restarts and forgets every token it issued.
    restarted = create_app(
        sp.store, client_id=CLIENT_ID, client_secret=SECRET, wall=lambda: NOW
    )
    sp.http._transport = httpx.ASGITransport(app=restarted)
    sp.store.put(flight(at=NOW))

    await sp.poller.poll()

    assert len(sp.bus.sent) == 2
    assert restarted.state.counters.unauthorised == 1
    assert restarted.state.counters.tokens_issued == 1
    await sp.close()


async def test_a_token_is_reused_until_shortly_before_it_expires() -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        return httpx.Response(200, json={"access_token": "t", "expires_in": 60})

    now = [0.0]
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        tokens = TokenSource(
            http=http,
            token_url="http://auth.test/token",
            client_id="c",
            client_secret="s",
            audience="sp.test",
            clock_s=lambda: now[0],
        )
        await tokens.token()
        now[0] = 29.0
        await tokens.token()
        now[0] = 31.0
        await tokens.token()
    assert requests == ["/token", "/token"]


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, text="not json"),
        httpx.Response(200, json={"token_type": "Bearer"}),
        httpx.Response(500),
    ],
)
async def test_a_token_endpoint_that_misbehaves_is_an_auth_error(
    response: httpx.Response,
) -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: response)
    ) as http:
        tokens = TokenSource(
            http=http,
            token_url="http://auth.test/token",
            client_id="c",
            client_secret="s",
        )
        with pytest.raises(AuthError):
            await tokens.token()


# --- the provider down, or talking nonsense ------------------------------------------


async def test_a_provider_that_is_down_is_counted_and_polled_again() -> None:
    up = [False]
    sp = Sp()
    sp.store.put(flight())
    asgi = httpx.ASGITransport(app=sp.app)

    class Flaky(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            if not up[0]:
                raise httpx.ConnectError("connection refused", request=request)
            return await asgi.handle_async_request(request)

    sp.http._transport = Flaky()

    await sp.poller.poll()
    await sp.poller.poll()
    assert sp.poller.provider_errors == 2
    assert sp.bus.sent == []

    up[0] = True
    await sp.poller.poll()
    assert len(sp.bus.sent) == 1
    await sp.close()


async def test_a_malformed_response_is_counted() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            return httpx.Response(200, json={"access_token": "t"})
        return httpx.Response(200, json={"flights": [{"no": "id"}]})

    sp = Sp()
    sp.http._transport = httpx.MockTransport(handler)

    await sp.poller.poll()

    assert sp.poller.format_errors == 1
    assert sp.bus.sent == []
    await sp.close()


async def test_details_that_fail_leave_the_flight_unidentified_but_shown() -> None:
    sp = Sp()
    sp.store.put(flight())
    asgi = httpx.ASGITransport(app=sp.app)

    class NoDetails(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/details"):
                return httpx.Response(503)
            return await asgi.handle_async_request(request)

    sp.http._transport = NoDetails()

    await sp.poller.poll()

    [(_, message)] = sp.bus.sent
    assert message["identification"]["status"] == "unidentified"
    assert sp.poller.details_failures == 1
    await sp.close()


# --- U-15: a provider switched off is not polled ------------------------------------


@pytest.mark.parametrize("instance", [PROVIDER, None])
async def test_a_disabled_provider_is_not_polled(instance: str | None) -> None:
    sp = Sp(switch=disabled(instance))
    sp.store.put(flight())

    await sp.poller.poll()

    assert sp.counters.flights_requests == 0
    assert sp.counters.tokens_issued == 0
    assert sp.bus.sent == []
    assert sp.poller.skipped_disabled == 1
    assert sp.sources.refused_disabled == 1
    [row] = sp.sources.snapshot()["instances"]
    assert (row["instance_id"], row["enabled"]) == (PROVIDER, False)
    await sp.close()


async def test_another_provider_disabled_leaves_this_one_polled() -> None:
    """The presence half: a switch that is not this provider's does nothing."""
    sp = Sp(switch=disabled("another-ussp"))
    sp.store.put(flight())

    await sp.poller.poll()

    assert len(sp.bus.sent) == 1
    assert sp.sources.snapshot()["instances"][0]["accepted"] == 1
    await sp.close()


# --- parsing ---------------------------------------------------------------------


def test_v19_details_are_read_too() -> None:
    details = parse_details(
        {
            "details": {
                "id": "x",
                "operator_id": "GEOX",
                "operator_location": {"lat": 41.7, "lng": 44.8},
                "serial_number": "SN-19",
                "registration_number": "REG-19",
            }
        }
    )
    assert (details.serial, details.registration_id, details.operator_id) == (
        "SN-19",
        "REG-19",
        "GEOX",
    )
    assert (details.operator_lat_deg, details.operator_lon_deg) == (41.7, 44.8)


def test_unknown_values_are_none_not_numbers() -> None:
    at, [state] = parse_flights(
        {
            "timestamp": {"value": "2026-10-01T12:00:00Z", "format": "RFC3339"},
            "flights": [
                {
                    "id": "u",
                    "current_state": {
                        "timestamp": "2026-10-01T11:59:59.5Z",
                        "operational_status": "Ground",
                        "position": {
                            "lat": 41.7,
                            "lng": 44.8,
                            "alt": -1000,
                            "pressure_altitude": -1000,
                            "accuracy_v": "VA150mPlus",
                        },
                        "height": {"distance": 5, "reference": "GroundLevel"},
                        "track": 361,
                        "speed": 255,
                        "vertical_speed": 63,
                    },
                },
                {"id": "no-state"},
            ],
        }
    )
    assert at == NOW
    assert state.alt_hae_m is None and state.alt_pressure_m is None
    assert state.track_deg is None and state.speed_ms is None
    assert state.vertical_speed_ms is None
    assert state.vertical_accuracy == 1
    # v19's height beside the position.
    assert (state.height_m, state.height_reference) == (5.0, "GroundLevel")


@pytest.mark.parametrize(
    "body",
    [
        [],
        {"flights": {}},
        {"flights": [{"id": "x", "current_state": {"position": {"lat": 1}}}]},
        {
            "flights": [
                {
                    "id": "x",
                    "current_state": {
                        "timestamp": "2026-10-01T12:00:00",
                        "position": {"lat": 1, "lng": 2},
                    },
                }
            ]
        },
        {
            "flights": [
                {"id": "x", "current_state": {"position": {"lat": 91, "lng": 2}}}
            ]
        },
        {
            "flights": [
                {"id": "x", "current_state": {"position": {"lat": "1", "lng": 2}}}
            ]
        },
    ],
)
def test_malformed_flights_are_format_errors(body: Any) -> None:
    with pytest.raises(FormatError):
        parse_flights(body)


def test_times_must_carry_a_zone() -> None:
    assert parse_time(None) is None
    with pytest.raises(FormatError):
        parse_time({"value": 5})
    with pytest.raises(FormatError):
        parse_time("yesterday")


def test_placement_without_a_response_time_falls_back_to_our_clock() -> None:
    _, [state] = parse_flights(
        {
            "flights": [
                {
                    "id": "p",
                    "current_state": {
                        "timestamp": (NOW + timedelta(seconds=10)).isoformat(),
                        "position": {"lat": 1, "lng": 2},
                    },
                }
            ]
        }
    )
    ahead = place(state, response_at=None, received_at=NOW)
    assert ahead is not None
    assert (ahead.captured_at, ahead.note) == (NOW, "clock_ahead")
    later = place(state, response_at=None, received_at=NOW + timedelta(seconds=16))
    assert later is not None and later.note == "too_old"
    on_time = place(state, response_at=None, received_at=NOW + timedelta(seconds=10.5))
    assert on_time is not None and on_time.captured_at == state.timestamp
    clamped = place(state, response_at=NOW, received_at=NOW)
    assert clamped is not None and clamped.note == "ahead_of_response"


# --- the poller's housekeeping -------------------------------------------------------


async def test_a_state_ahead_of_its_response_is_placed_on_arrival_and_counted() -> None:
    sp = Sp()
    sp.store.put(flight(at=NOW + timedelta(seconds=2)))

    await sp.poller.poll()

    [(_, message)] = sp.bus.sent
    assert message["captured_at"] == NOW.isoformat()
    assert message["network_rid"]["time_source"] == "receiver"
    assert sp.poller.status()["time_ahead_of_response"] == 1
    await sp.close()


async def test_a_bus_failure_is_logged_and_the_next_state_still_goes() -> None:
    sp = Sp()
    sp.store.put(flight())

    class Down:
        async def publish(self, subject: str, payload: bytes) -> None:
            raise ConnectionError("bus gone")

    sp.poller.bus = Down()
    await sp.poller.poll()
    assert sp.poller.published == 0

    sp.poller.bus = sp.bus
    sp.store.put(flight(at=NOW))
    await sp.poller.poll()
    assert sp.poller.published == 1
    await sp.close()


async def test_flights_the_sp_stops_mentioning_are_forgotten() -> None:
    sp = Sp()
    now = [0.0]
    sp.poller.clock_s = lambda: now[0]
    sp.store.put(flight())
    await sp.poller.poll()
    assert sp.poller.status()["flights_held"] == 1

    sp.store.flights.clear()
    now[0] = 61.0
    await sp.poller.poll()

    assert sp.poller.status()["flights_held"] == 0
    await sp.close()


async def test_a_provider_switched_off_mid_poll_publishes_nothing() -> None:
    sp = Sp(switch=SourceControlState())
    sp.store.put(flight())
    holder = sp.sources.switch
    assert isinstance(holder, _Holder)
    real = sp.poller.client.flights

    async def then_switch_off(area: Area) -> httpx.Response:
        response = await real(area)
        holder.state = disabled()
        return response

    sp.poller.client.flights = then_switch_off  # type: ignore[method-assign]

    await sp.poller.poll()

    assert sp.bus.sent == []
    assert sp.sources.refused_disabled == 1
    await sp.close()


async def test_the_poll_loop_runs_until_stopped() -> None:
    import asyncio

    from gateway.network_rid_ingest import poll_periodically

    sp = Sp()
    stop = asyncio.Event()
    task = asyncio.create_task(poll_periodically(sp.poller, stop, every_s=0.01))
    while sp.poller.polls < 3:
        await asyncio.sleep(0.01)
    stop.set()
    await task
    assert sp.poller.polls >= 3
    await sp.close()


async def test_the_status_is_logged_per_provider(
    caplog: pytest.LogCaptureFixture,
) -> None:
    import asyncio
    import logging

    from gateway.network_rid_ingest import log_status_periodically
    from gateway.registry_projection import RegistryFollower

    sp = Sp()
    stop = asyncio.Event()
    follower = RegistryFollower(engine=None)  # type: ignore[arg-type]
    with caplog.at_level(logging.INFO):
        task = asyncio.create_task(
            log_status_periodically([sp.poller], follower, stop, every_s=0.01)
        )
        await asyncio.sleep(0.05)
        stop.set()
        await task
    assert "network remote id status" in caplog.text
    await sp.close()

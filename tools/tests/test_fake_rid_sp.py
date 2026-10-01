"""The fake F3411 SP fed from SITL MAVLink, through the ingest. U-02.

MAVLink in (pymavlink-packed, as SITL sends it), the fake SP's HTTP out,
the network Remote ID ingest's track on the bus: the aircraft is where the
vehicle said, as for the U-16 bridge.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from gateway.network_rid import Area, ServiceProviderClient, TokenSource
from gateway.network_rid_ingest import ProviderPoller
from gateway.tests.rid_frames import FlatGeoid
from tools.fake_rid_sp import (
    FlightStore,
    SitlFlight,
    build_parser,
    create_app,
    diagonal_km,
    follow_sitl,
    parse_view,
)
from tools.sitl_remote_id import VehicleState
from tools.tests.test_sitl_remote_id import (
    UNIX_S,
    FakeSource,
    heartbeat,
    position,
    system_time,
)

SYSID = 3  # the helpers' vehicle


class FakeBus:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def publish(self, subject: str, payload: bytes) -> None:
        self.sent.append(json.loads(payload))


def sitl_flight(*messages: Any) -> SitlFlight:
    return SitlFlight(
        flight_id=f"sitl-{SYSID}",
        serial="1581F5FKD2290003",
        operator_id="GEOOPERATOR0001",
        state=VehicleState(sysid=SYSID),
        source=FakeSource(list(messages)),
        geoid=FlatGeoid(),  # type: ignore[arg-type]
    )


async def fill(store: FlightStore, vehicle: SitlFlight) -> None:
    task = asyncio.create_task(follow_sitl([vehicle], store, poll_s=0.001))
    try:
        for _ in range(200):
            if store.flights:
                break
            await asyncio.sleep(0.001)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def test_a_sitl_vehicle_becomes_a_flight_where_it_said() -> None:
    fix = position()
    store = FlightStore()
    await fill(store, sitl_flight(heartbeat(armed=True), system_time(), fix))

    flight = store.flights[f"sitl-{SYSID}"]
    assert flight.lat_deg == pytest.approx(fix.lat / 1e7)
    assert flight.alt_hae_m == pytest.approx(fix.alt / 1000 + 20.0)
    assert flight.airborne is True
    assert flight.timestamp == datetime.fromtimestamp(UNIX_S, tz=UTC)
    state = flight.state()
    assert state["operational_status"] == "Airborne"
    assert state["position"]["height"] == {
        "distance": pytest.approx(fix.relative_alt / 1000),
        "reference": "TakeoffLocation",
    }


async def test_a_vehicle_with_no_position_is_not_served() -> None:
    store = FlightStore()
    vehicle = sitl_flight(heartbeat(armed=True))
    await fill(store, vehicle)
    assert store.flights == {}
    assert vehicle.flight() is None


async def test_through_the_ingest_the_flight_is_where_sitl_says() -> None:
    fix = position()
    store = FlightStore()
    await fill(store, sitl_flight(heartbeat(armed=True), system_time(), fix))
    received = datetime.fromtimestamp(UNIX_S + 0.4, tz=UTC)
    app = create_app(store, client_id="c", client_secret="s", wall=lambda: received)
    bus = FakeBus()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://sp.test"
    ) as http:
        poller = ProviderPoller(
            provider="fake-ussp",
            client=ServiceProviderClient(
                http=http,
                base_url="http://sp.test",
                tokens=TokenSource(
                    http=http,
                    token_url="http://sp.test/token",
                    client_id="c",
                    client_secret="s",
                ),
            ),
            areas=[Area(41.70, 44.81, 41.73, 44.84)],
            bus=bus,
            geoid=FlatGeoid(),
            wall=lambda: received,
        )
        await poller.poll()

    [seen] = bus.sent
    assert seen["source"] == "network_remote_id"
    assert seen["lat_deg"] == pytest.approx(fix.lat / 1e7)
    assert seen["lon_deg"] == pytest.approx(fix.lon / 1e7)
    # HAE out through the geoid, back to AMSL through the same geoid.
    assert seen["alt_amsl_m"] == pytest.approx(fix.alt / 1000, abs=0.01)
    assert seen["vx_ms"] == pytest.approx(fix.vx / 100, abs=0.01)
    assert seen["vy_ms"] == pytest.approx(fix.vy / 100, abs=0.01)
    assert seen["vz_ms"] == pytest.approx(fix.vz / 100, abs=0.01)
    assert seen["network_rid"]["serial"] == "1581F5FKD2290003"
    assert seen["network_rid"]["operator_lat_deg"] == pytest.approx(fix.lat / 1e7)
    # 0.4 s behind the SP's response, so 0.4 s behind our receive time.
    assert datetime.fromisoformat(seen["captured_at"]) == datetime.fromtimestamp(
        UNIX_S, tz=UTC
    )


def test_a_view_is_two_corners_in_either_order() -> None:
    assert parse_view("41.8,44.9,41.7,44.8") == (41.7, 44.8, 41.8, 44.9)
    for bad in ("1,2,3", "a,b,c,d", "nan,1,2,3"):
        with pytest.raises(ValueError):
            parse_view(bad)
    assert diagonal_km((41.7, 44.8, 41.8, 44.9)) == pytest.approx(13.9, abs=0.1)


def test_the_command_line_takes_one_set_of_flags_per_vehicle() -> None:
    args = build_parser().parse_args(
        [
            "--client-id",
            "c",
            "--client-secret-file",
            "local/secret",
            "--sysid",
            "2",
            "--serial",
            "SN-2",
            "--operator-id",
            "GEO2",
            "--mavlink",
            "udpin:127.0.0.1:14561",
        ]
    )
    assert (args.sysid, args.serial, args.mavlink) == (
        [2],
        ["SN-2"],
        ["udpin:127.0.0.1:14561"],
    )

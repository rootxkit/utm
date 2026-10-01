"""Network Remote ID: our own aircraft, and what an SP may cost us. U-02.

A provider flight whose serial is one of ours follows the same rule as a
direct broadcast (withheld, spoken for, or split off). An SP is
authenticated, not trusted: its bodies, flight lists, tile splits, detail
fetches and slowness are all bounded, counted and logged.
"""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from typing import Any

import httpx
import pytest

from gateway.network_rid import Area, flight_aircraft_id
from gateway.tests.test_network_rid import (
    LAT,
    LON,
    NOW,
    REGISTERED,
    SERIAL,
    SMALL,
    Sp,
    flight,
)


def relay(lat: float, lon: float) -> bytes:
    return json.dumps(
        {
            "drone_id": str(REGISTERED),
            "lat_deg": lat,
            "lon_deg": lon,
            "rx_ts": NOW.isoformat(),
            "captured_at": NOW.isoformat(),
        }
    ).encode()


# --- our own aircraft (item: the same rule as direct Remote ID) -------------------


async def test_our_flight_with_a_live_relay_nearby_is_withheld() -> None:
    sp = Sp()
    sp.store.put(flight())
    sp.poller.links.on_telemetry(relay(LAT, LON + 0.001), now_s=0.0)

    await sp.poller.poll()

    assert sp.bus.sent == []
    assert sp.poller.withheld == 1


async def test_our_flight_far_from_its_live_relay_is_split_off() -> None:
    sp = Sp()
    sp.store.put(flight())
    sp.poller.links.on_telemetry(relay(LAT, LON + 0.05), now_s=0.0)

    await sp.poller.poll()

    [(_, message)] = sp.bus.sent
    assert message["drone_id"] != str(REGISTERED)
    assert message["identification"]["reason"] == "serial_conflict"
    assert message["identification"]["mismatch"] is True
    assert sp.poller.serial_conflicts == 1


async def test_our_flight_with_a_quiet_relay_speaks_for_it() -> None:
    sp = Sp()
    sp.store.put(flight())

    await sp.poller.poll()

    [(_, message)] = sp.bus.sent
    assert message["drone_id"] == str(REGISTERED)
    assert sp.poller.withheld == sp.poller.serial_conflicts == 0


async def test_a_strangers_flight_is_its_own_aircraft() -> None:
    sp = Sp()
    sp.store.put(flight(serial="SN-STRANGER"))
    sp.poller.links.on_telemetry(relay(LAT, LON), now_s=0.0)

    await sp.poller.poll()

    [(_, message)] = sp.bus.sent
    assert message["drone_id"] not in (
        str(REGISTERED),
        str(flight_aircraft_id("x", "f-1")),
    )
    assert message["label"] == "SN-STRANGER"


# --- bounded cost ----------------------------------------------------------------------


def handler_sp(sp: Sp, flights_body: Any) -> None:
    """Answer the token and details from the fake SP, flights with this."""
    asgi = httpx.ASGITransport(app=sp.app)

    class Transport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            if request.url.path == "/uss/flights":
                answer: httpx.Response = await flights_body(request)
                return answer
            return await asgi.handle_async_request(request)

    sp.http._transport = Transport()


async def test_a_body_declared_too_large_is_refused_unread() -> None:
    sp = Sp()
    sp.poller.client.max_body_bytes = 2048

    async def huge(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-length": "999999999"}, content=b"{}"
        )

    handler_sp(sp, huge)
    await sp.poller.poll()

    assert sp.poller.oversize == 1
    assert sp.bus.sent == []
    await sp.close()


async def test_a_body_that_grows_past_the_cap_is_refused() -> None:
    sp = Sp()
    sp.poller.client.max_body_bytes = 2048

    async def stream() -> Any:
        for _ in range(10):
            yield b" " * 1000

    async def growing(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=stream())

    handler_sp(sp, growing)
    await sp.poller.poll()

    assert sp.poller.oversize == 1
    await sp.close()


async def test_flights_past_the_per_response_cap_are_dropped_and_counted(
    caplog: pytest.LogCaptureFixture,
) -> None:
    sp = Sp()
    sp.poller.max_flights_per_response = 3
    for n in range(5):
        sp.store.put(flight(f"f-{n}", serial=None))

    await sp.poller.poll()

    assert len(sp.bus.sent) == 3
    assert sp.poller.flights_dropped == 2
    assert any(getattr(r, "kind", None) == "flights" for r in caplog.records)
    await sp.close()


async def test_tiles_past_the_per_poll_cap_are_skipped_and_counted() -> None:
    big = Area(LAT - 0.1, LON - 0.1, LAT + 0.1, LON + 0.1)
    sp = Sp(area=big)
    sp.poller.max_tiles_per_poll = 2

    await sp.poller.poll()

    assert sp.counters.flights_requests == 2
    assert sp.poller.tiles_skipped == len(big.tiles(7.0)) - 2
    await sp.close()


async def test_413_splits_count_against_the_tile_cap() -> None:
    sp = Sp(sp_max_km=0.5)
    sp.poller.max_tiles_per_poll = 3

    await sp.poller.poll()

    assert sp.counters.flights_requests == 3
    assert sp.poller.tiles_skipped > 0
    await sp.close()


async def test_details_past_the_per_poll_cap_wait_for_a_later_poll() -> None:
    sp = Sp()
    sp.poller.max_details_per_poll = 2
    for n in range(5):
        sp.store.put(flight(f"f-{n}", serial=f"SN-{n}"))

    await sp.poller.poll()

    assert sp.counters.details_requests == 2
    assert sp.poller.details_deferred == 3
    # Published regardless; the three without details are unidentified now.
    statuses = sorted(m["identification"]["status"] for _, m in sp.bus.sent)
    assert statuses.count("unidentified") == 3
    await sp.close()


async def test_details_are_fetched_concurrently_within_the_bound() -> None:
    sp = Sp()
    sp.poller.details_concurrency = 2
    for n in range(6):
        sp.store.put(flight(f"f-{n}", serial=f"SN-{n}"))
    real = sp.poller.client.details
    in_flight = [0, 0]

    async def slow(flight_id: str) -> Any:
        in_flight[0] += 1
        in_flight[1] = max(in_flight[1], in_flight[0])
        await asyncio.sleep(0.02)
        try:
            return await real(flight_id)
        finally:
            in_flight[0] -= 1

    sp.poller.client.details = slow  # type: ignore[method-assign]
    await sp.poller.poll()

    assert in_flight[1] == 2
    assert sp.counters.details_requests == 6
    await sp.close()


async def test_a_slow_sp_is_cut_off_at_the_poll_deadline() -> None:
    sp = Sp()
    sp.poller.poll_deadline_s = 0.1
    sp.poller.clock_s = asyncio.get_running_loop().time

    async def stalled(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return httpx.Response(200, json={"flights": []})

    handler_sp(sp, stalled)
    started = asyncio.get_running_loop().time()
    await sp.poller.poll()

    assert asyncio.get_running_loop().time() - started < 1.0
    assert sp.poller.deadline_exceeded == 1
    assert sp.bus.sent == []
    await sp.close()


async def test_slow_details_are_cut_off_and_the_flight_still_shown() -> None:
    sp = Sp()
    sp.poller.poll_deadline_s = 0.2
    sp.poller.clock_s = asyncio.get_running_loop().time
    sp.store.put(flight())

    async def stalled(flight_id: str) -> Any:
        await asyncio.sleep(5)

    sp.poller.client.details = stalled  # type: ignore[method-assign]
    await sp.poller.poll()

    [(_, message)] = sp.bus.sent
    assert message["identification"]["status"] == "unidentified"
    assert sp.poller.deadline_exceeded == 1
    await sp.close()


async def test_each_response_is_placed_at_its_own_receive_time() -> None:
    """Two tiles answered 3 s apart: each flight carries its own rx_ts."""
    big = Area(LAT - 0.05, LON - 0.05, LAT + 0.05, LON + 0.05)
    sp = Sp(area=big)
    times = iter(NOW + timedelta(seconds=3 * n) for n in range(10))
    sp.poller.wall = lambda: next(times)
    sp.store.put(flight("south-west", lat=LAT - 0.04, lon=LON - 0.04, serial=None))
    sp.store.put(flight("north-east", lat=LAT + 0.04, lon=LON + 0.04, serial=None))

    await sp.poller.poll()

    rx = {m["network_rid"]["flight_id"]: m["rx_ts"] for _, m in sp.bus.sent}
    assert rx["south-west"] != rx["north-east"]
    await sp.close()


# --- tiles and accuracies --------------------------------------------------------


def test_a_long_thin_area_is_a_row_of_tiles() -> None:
    strip = Area(41.70, 44.00, 41.73, 45.00)  # about 3.3 km by 83 km
    tiles = strip.tiles(7.0)
    assert {t.lat_min for t in tiles} == {41.70}
    assert len(tiles) == 17
    assert all(t.diagonal_km() <= 7.0 for t in tiles)
    assert SMALL.tiles(7.0) == [SMALL]


async def test_provider_accuracies_are_carried_as_odid_codes() -> None:
    sp = Sp()
    sp.store.put(flight())

    await sp.poller.poll()

    [(_, message)] = sp.bus.sent
    # The fake SP says HA3m, VA10m and SA1mps.
    nrid = message["network_rid"]
    assert (nrid["horizontal_accuracy"], nrid["vertical_accuracy"]) == (12, 4)
    assert nrid["speed_accuracy"] == 3
    assert nrid["serial"] == SERIAL
    await sp.close()

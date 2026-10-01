"""U-15 at the relay adapter, over real sockets.

A station switched off is refused at the upgrade with 503 (never 401 or
403, which the relay takes as fatal and never retries), a session already
open is closed with 1013, records that arrive from it are neither stored nor
acknowledged, and switching it back on lets it in again. The last test runs
the real ground relay (`agent.relay.Relay`) against the real server through
all of that, because the claim that matters is the relay's behaviour: it
keeps queueing, keeps retrying, and delivers what it held once let back in.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import websockets
from websockets.asyncio.client import connect

from agent.config import RelayConfig
from agent.queue import DurableQueue
from agent.relay import Relay
from common.sources import RELAY, Control, SourceControlState
from gateway.ingest_store import InMemoryIngestStore
from gateway.relay_server import (
    CLOSE_SOURCE_DISABLED,
    SOURCE_DISABLED_STATUS,
    RelayServer,
)
from gateway.source_activity import SourceActivity
from gateway.tests.test_relay_server import (
    EPOCH,
    STATION,
    TOKEN,
    StubAuthenticator,
    auth,
    batch,
    handshake,
    hello,
    read_until,
    url,
)
from tests.ports import free_udp_port

OTHER_STATION = "kutaisi-base-2"
OTHER_TOKEN = "other-station-token"


@dataclass
class Switches:
    """Stands in for the follower: holds a state the test changes."""

    state: SourceControlState = field(default_factory=SourceControlState)
    version: int = 0

    def set(self, station_id: str | None, *, enabled: bool) -> None:
        self.version += 1
        controls = [
            c
            for c in self.state.controls
            if (c.source_type, c.instance_id) != (RELAY, station_id)
        ]
        controls.append(
            Control(
                source_type=RELAY,
                instance_id=station_id,
                enabled=enabled,
                reason="test",
                changed_by="test-admin",
                changed_at="2026-10-01T12:00:00+00:00",
            )
        )
        self.state = SourceControlState(version=self.version, controls=tuple(controls))


@contextlib.asynccontextmanager
async def running(switches: Switches, store: InMemoryIngestStore | None = None) -> Any:
    server = RelayServer(
        store=store if store is not None else InMemoryIngestStore(),
        authenticator=StubAuthenticator({TOKEN: STATION, OTHER_TOKEN: OTHER_STATION}),
        host="127.0.0.1",
        port=0,
    )
    server.sources = SourceActivity(
        source_type=RELAY,
        switch=switches,
        known=[STATION, OTHER_STATION],
        connected=server.connected_stations,
    )
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


def sources(server: RelayServer) -> SourceActivity:
    assert server.sources is not None
    return server.sources


async def test_a_disabled_station_is_refused_at_the_upgrade_with_503() -> None:
    switches = Switches()
    switches.set(STATION, enabled=False)
    async with running(switches) as server:
        with pytest.raises(websockets.InvalidStatus) as caught:
            async with connect(url(server), additional_headers=auth()):
                pass

    response = caught.value.response
    assert response.status_code == SOURCE_DISABLED_STATUS
    assert response.headers["Retry-After"] == "10"
    assert b"source disabled" in response.body
    activity = sources(server).instances[STATION]
    assert activity.refused_disabled == 1
    assert activity.last_refused_at is not None


async def test_a_whole_type_switched_off_refuses_every_station() -> None:
    switches = Switches()
    switches.set(None, enabled=False)
    async with running(switches) as server:
        for token in (TOKEN, OTHER_TOKEN):
            with pytest.raises(websockets.InvalidStatus) as caught:
                async with connect(
                    url(server), additional_headers={"Authorization": f"Bearer {token}"}
                ):
                    pass
            assert caught.value.response.status_code == SOURCE_DISABLED_STATUS


async def test_a_bad_token_is_still_401_when_the_station_is_disabled() -> None:
    """The credential is checked first: a refusal for being switched off
    says nothing to someone without one."""
    switches = Switches()
    switches.set(STATION, enabled=False)
    async with running(switches) as server:
        with pytest.raises(websockets.InvalidStatus) as caught:
            async with connect(
                url(server), additional_headers={"Authorization": "Bearer wrong"}
            ):
                pass
    assert caught.value.response.status_code == 401


async def test_switching_it_back_on_lets_it_in_again() -> None:
    switches = Switches()
    switches.set(STATION, enabled=False)
    async with running(switches) as server:
        with pytest.raises(websockets.InvalidStatus):
            async with connect(url(server), additional_headers=auth()):
                pass
        switches.set(STATION, enabled=True)
        async with connect(url(server), additional_headers=auth()) as connection:
            welcome = await handshake(connection, hello())
    assert welcome["type"] == "welcome"


async def test_an_open_session_is_closed_with_1013_when_its_station_is_disabled() -> (
    None
):
    switches = Switches()
    store = InMemoryIngestStore()
    async with (
        running(switches, store) as server,
        connect(url(server), additional_headers=auth()) as connection,
        connect(
            url(server), additional_headers={"Authorization": f"Bearer {OTHER_TOKEN}"}
        ) as other,
    ):
        await handshake(connection, hello())
        await handshake(other, hello(station_id=OTHER_STATION))
        await connection.send(batch(0, 5))
        await read_until(connection, "ack")
        assert server.connected_stations() == {STATION, OTHER_STATION}

        switches.set(STATION, enabled=False)
        closed = await server.apply_source_control()

        assert closed == 1
        with pytest.raises(websockets.ConnectionClosed) as caught:
            for _ in range(20):
                await asyncio.wait_for(connection.recv(), timeout=5.0)
        assert caught.value.rcvd is not None
        assert caught.value.rcvd.code == CLOSE_SOURCE_DISABLED
        assert caught.value.rcvd.reason == "source disabled"

        # The other station's session is untouched and still acknowledged.
        await other.send(batch(0, 3))
        ack = await read_until(other, "ack")
        assert ack["seq"] == 2
        assert server.closed_disabled == 1
        await _until(lambda: server.connected_stations() == {OTHER_STATION})


async def test_records_from_a_station_disabled_mid_session_are_not_stored() -> None:
    """Switched off between two batches, with no notice reaching the
    server: the batch itself is refused, not stored, not acknowledged, and
    the session closed, so the relay keeps those records."""
    switches = Switches()
    store = InMemoryIngestStore()
    async with (
        running(switches, store) as server,
        connect(url(server), additional_headers=auth()) as connection,
    ):
        await handshake(connection, hello())
        await connection.send(batch(0, 5))
        await read_until(connection, "ack")

        switches.set(STATION, enabled=False)
        await connection.send(batch(5, 5))
        with pytest.raises(websockets.ConnectionClosed) as caught:
            for _ in range(20):
                raw = await asyncio.wait_for(connection.recv(), timeout=5.0)
                assert json.loads(raw).get("seq", -1) < 5, "a refused batch was acked"
        assert caught.value.rcvd is not None
        assert caught.value.rcvd.code == CLOSE_SOURCE_DISABLED

    assert await store.resume_from_seq(STATION, EPOCH) == 5
    activity = sources(server).instances[STATION]
    assert (activity.accepted, activity.refused_disabled) == (5, 5)


async def _until(condition: Any, limit_s: float = 5.0) -> None:
    deadline = time.monotonic() + limit_s
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.02)


# --- the real ground relay -----------------------------------------------------


async def test_the_relay_waits_out_a_disabled_station_and_delivers_what_it_held(
    tmp_path: Path,
) -> None:
    switches = Switches()
    switches.set(STATION, enabled=False)
    store = InMemoryIngestStore()
    async with running(switches, store) as server:
        config = RelayConfig(
            station_id=STATION,
            gateway_url=url(server),  # type: ignore[arg-type]
            token_path=tmp_path / "relay.token",
            queue_path=tmp_path / "relay-queue.sqlite3",
            bind_port=free_udp_port(),
        )
        queue = DurableQueue(config.queue_path)
        relay = Relay(config, queue, TOKEN)
        udp = relay.start_intake()
        uplink = asyncio.create_task(relay.run_uplink())
        sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:

            def send(count: int) -> None:
                for n in range(count):
                    sender.sendto(
                        bytes([n % 256]) * 20, ("127.0.0.1", config.bind_port)
                    )

            send(10)
            await _until(
                lambda: sources(server).status()["refused_source_disabled"] >= 2
            )
            # Refused, and still trying: 503 is not the fatal 401.
            assert not uplink.done()
            assert queue.depth >= 10

            switches.set(STATION, enabled=True)
            await _until(lambda: _stored(store) >= 10, limit_s=20.0)

            # Switched off while connected: closed, and still not given up.
            switches.set(STATION, enabled=False)
            await server.apply_source_control()
            held_before = _stored(store)
            send(10)
            await asyncio.sleep(0.5)
            assert _stored(store) == held_before
            assert not uplink.done()

            switches.set(STATION, enabled=True)
            await _until(lambda: _stored(store) >= held_before + 10, limit_s=20.0)
        finally:
            sender.close()
            uplink.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await uplink
            relay.stop()
            udp.close()
            queue.close()


def _stored(store: InMemoryIngestStore) -> int:
    return sum(len(records) for records in store.records.values())

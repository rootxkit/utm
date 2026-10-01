"""U-15 at the Remote ID adapter, over a real UDP socket.

Signed datagrams from two receivers. Switching one receiver off drops its
datagrams, counted, before anything is stored or published, and leaves the
other alone; switching it on again takes the next one. Switching Remote ID
off as a whole drops both.
"""

from __future__ import annotations

import asyncio
import json
import socket
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from itertools import count
from typing import Any

import pytest

from common.sources import REMOTE_ID, Control, SourceControlState
from gateway.remote_id import RemoteIdTracker
from gateway.remote_id_auth import ReceiverAuthenticator, sign
from gateway.remote_id_ingest import RemoteIdIngest, listen
from gateway.remote_id_store import PendingRows, RemoteIdRow
from gateway.source_activity import SourceActivity
from gateway.tests.rid_frames import NOW, FlatGeoid, basic, location, pack

KEYS = {"rx-1": bytes(32), "rx-2": bytes([1]) * 32}
TRANSMITTERS = {"rx-1": "AA:BB:CC:00:00:01", "rx-2": "AA:BB:CC:00:00:02"}
SERIALS = {"rx-1": "1581F5FJD228400A", "rx-2": "1581F5FJD228400B"}
_nonces = count()


@dataclass
class Switches:
    state: SourceControlState = field(default_factory=SourceControlState)

    def set(self, instance_id: str | None, *, enabled: bool) -> None:
        kept = [
            c
            for c in self.state.controls
            if (c.source_type, c.instance_id) != (REMOTE_ID, instance_id)
        ]
        kept.append(
            Control(
                source_type=REMOTE_ID,
                instance_id=instance_id,
                enabled=enabled,
                reason="test",
                changed_by="test-admin",
                changed_at="2026-10-01T12:00:00+00:00",
            )
        )
        self.state = SourceControlState(
            version=self.state.version + 1, controls=tuple(kept)
        )


class Bus:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def publish(self, subject: str, payload: bytes) -> None:
        self.sent.append(json.loads(payload))

    def from_receiver(self, receiver_id: str) -> int:
        return sum(1 for m in self.sent if m.get("station_id") == receiver_id)


class Rows:
    def __init__(self) -> None:
        self.rows: list[RemoteIdRow] = []

    async def write(self, rows: list[RemoteIdRow]) -> None:
        self.rows.extend(rows)


def signed(receiver_id: str) -> bytes:
    report = {
        "receiver_id": receiver_id,
        "transmitter": TRANSMITTERS[receiver_id],
        "payload_hex": pack(basic(SERIALS[receiver_id]), location()).hex(),
        "sent_at_ms": int(NOW.timestamp() * 1000),
        "nonce": f"n-{next(_nonces)}",
    }
    return sign(json.dumps(report).encode(), KEYS[receiver_id])


@dataclass
class Running:
    service: RemoteIdIngest
    bus: Bus
    rows: Rows
    store: PendingRows
    switches: Switches
    port: int

    async def send(self, receiver_id: str, *, expect_published: bool) -> None:
        """Send one datagram, wait until the ingest has published or dropped
        it, and check which."""
        published = self.service.published
        dropped = self.service.dropped_source_disabled
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.sendto(signed(receiver_id), ("127.0.0.1", self.port))
        for _ in range(500):
            if (
                self.service.published > published
                or self.service.dropped_source_disabled > dropped
            ):
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("the datagram was neither published nor dropped")
        await self.store.flush()
        assert (self.service.published > published) is expect_published
        assert (self.service.dropped_source_disabled > dropped) is not expect_published
        if expect_published:
            assert self.bus.sent[-1]["station_id"] == receiver_id


@pytest.fixture
async def running() -> AsyncIterator[Running]:
    switches = Switches()
    bus = Bus()
    rows = Rows()
    store = PendingRows(writer=rows)
    service = RemoteIdIngest(
        tracker=RemoteIdTracker(geoid=FlatGeoid()),
        bus=bus,
        store=store,
        authenticator=ReceiverAuthenticator(keys=KEYS),
        clock_s=lambda: 0.0,
        wall=lambda: NOW,
        sources=SourceActivity(source_type=REMOTE_ID, switch=switches, known=KEYS),
    )
    transport = await listen(service, "127.0.0.1", 0)
    try:
        yield Running(
            service=service,
            bus=bus,
            rows=rows,
            store=store,
            switches=switches,
            port=transport.get_extra_info("sockname")[1],
        )
    finally:
        transport.close()


async def test_both_receivers_are_taken_while_switched_on(running: Running) -> None:
    await running.send("rx-1", expect_published=True)
    await running.send("rx-2", expect_published=True)

    assert running.bus.from_receiver("rx-1") == 1
    assert running.bus.from_receiver("rx-2") == 1
    assert running.service.dropped_source_disabled == 0


async def test_a_receiver_switched_off_is_dropped_counted_and_restored(
    running: Running,
) -> None:
    running.switches.set("rx-1", enabled=False)
    await running.send("rx-1", expect_published=False)
    await running.send("rx-1", expect_published=False)
    await running.send("rx-2", expect_published=True)

    # Dropped before the tracker, the store and the bus.
    assert running.bus.from_receiver("rx-1") == 0
    assert {row.receiver_id for row in running.rows.rows} == {"rx-2"}
    assert running.service.dropped_source_disabled == 2
    assert running.service.refused == 0
    status = running.service.status()
    assert status["dropped_source_disabled"] == 2
    snapshot = {
        i["instance_id"]: i
        for i in running.service.sources.snapshot()["instances"]  # type: ignore[union-attr]
    }
    assert snapshot["rx-1"]["enabled"] is False
    assert snapshot["rx-1"]["disabled_by"] == "instance"
    assert snapshot["rx-1"]["refused_disabled"] == 2
    assert snapshot["rx-2"]["enabled"] is True
    assert snapshot["rx-2"]["accepted"] == 1

    running.switches.set("rx-1", enabled=True)
    await running.send("rx-1", expect_published=True)
    assert running.bus.from_receiver("rx-1") == 1
    assert running.service.dropped_source_disabled == 2


async def test_remote_id_switched_off_as_a_whole_drops_every_receiver(
    running: Running,
) -> None:
    running.switches.set(None, enabled=False)
    await running.send("rx-1", expect_published=False)
    await running.send("rx-2", expect_published=False)

    assert running.bus.sent == []
    assert running.rows.rows == []
    assert running.service.dropped_source_disabled == 2

    running.switches.set(None, enabled=True)
    await running.send("rx-2", expect_published=True)
    assert running.bus.from_receiver("rx-2") == 1

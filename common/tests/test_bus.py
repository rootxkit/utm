"""`round_trip` against a fake server that records what it saw at each PING.

No broker is needed: the question is the order bytes reach the socket, which
a real server cannot report. The fake answers every PING with a PONG and
notes, for each one, which SUB lines had already arrived.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import nats
import pytest

from common.bus import round_trip

INFO = (
    b'INFO {"server_id":"fake","version":"2.10.0","proto":1,'
    b'"max_payload":1048576,"headers":true}\r\n'
)


@dataclass
class FakeServer:
    stream: bytearray = field(default_factory=bytearray)
    # For each PING answered, the SUB lines that preceded it on the wire.
    subs_before_ping: list[list[bytes]] = field(default_factory=list)
    _answered: int = 0

    async def handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            writer.write(INFO)
            await writer.drain()
            while data := await reader.read(4096):
                self.stream.extend(data)
                self._answer(writer)
                await writer.drain()
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()

    def _answer(self, writer: asyncio.StreamWriter) -> None:
        position = 0
        pings: list[int] = []
        while (found := self.stream.find(b"PING\r\n", position)) >= 0:
            pings.append(found)
            position = found + 1
        for at in pings[self._answered :]:
            before = bytes(self.stream[:at])
            self.subs_before_ping.append(
                [line for line in before.split(b"\r\n") if line.startswith(b"SUB ")]
            )
            writer.write(b"PONG\r\n")
        self._answered = len(pings)

    def forget(self) -> None:
        """Start counting from here: the connect handshake has its own PING."""
        self.subs_before_ping.clear()


@pytest.fixture
async def server() -> AsyncIterator[tuple[FakeServer, str]]:
    fake = FakeServer()
    listener = await asyncio.start_server(fake.handle, "127.0.0.1", 0)
    port = listener.sockets[0].getsockname()[1]
    try:
        yield fake, f"nats://127.0.0.1:{port}"
    finally:
        listener.close()
        # Also waits for the connection handlers, so no socket outlives the
        # test's event loop.
        await listener.wait_closed()


async def _noop(_message: object) -> None:
    return None


async def test_a_plain_flush_is_answered_before_the_subscription_arrives(
    server: tuple[FakeServer, str],
) -> None:
    """Pins the nats-py behaviour `round_trip` exists for.

    If this starts failing, nats-py now writes pending commands before its
    PING, and `round_trip` can go back to a single flush.
    """
    fake, url = server
    client = await nats.connect(url)
    try:
        fake.forget()
        await client.subscribe("telemetry.*", cb=_noop)
        await client.flush()
        assert fake.subs_before_ping == [[]]
    finally:
        await client.close()


async def test_round_trip_is_answered_only_after_the_subscription_arrives(
    server: tuple[FakeServer, str],
) -> None:
    fake, url = server
    client = await nats.connect(url)
    try:
        fake.forget()
        await client.subscribe("telemetry.*", cb=_noop)
        await client.subscribe("station.*", cb=_noop)
        await round_trip(client)
        # The PONG round_trip returned on is the last one answered.
        assert fake.subs_before_ping[-1] == [
            b"SUB telemetry.*  1",
            b"SUB station.*  2",
        ]
    finally:
        await client.close()


async def test_round_trip_covers_a_publish_too(
    server: tuple[FakeServer, str],
) -> None:
    fake, url = server
    client = await nats.connect(url)
    try:
        await client.publish("telemetry.abc", b"{}")
        await round_trip(client)
        published_at = fake.stream.find(b"PUB telemetry.abc")
        answered_at = fake.stream.rfind(b"PING\r\n")
        assert 0 <= published_at < answered_at
    finally:
        await client.close()

"""A NATS round trip that covers everything written before it.

`Client.flush()` in nats-py documents itself as "ensuring what we have written
so far has made it to the server". In 2.16.0 it does not. `subscribe` and
`publish` append to a pending buffer that a background flusher task writes
out later, while `flush` writes its PING straight to the socket. Recorded on
the wire (`common/tests/test_bus.py` does it against a fake server):

    PING\\r\\nSUB telemetry.*  1\\r\\nSUB station.*  2\\r\\n

The server answers that PING before it has seen either SUB, so the PONG that
`flush` waits for acknowledges neither. A subscriber that flushes and then
lets another connection publish has a race: usually the SUB still wins, and
under load it does not, and the message is dropped for want of interest. That
was the console feed test that failed intermittently in CI with "saw []".

## Why two flushes are a barrier

`flush` hands the pending buffer to the flusher (a queue put) before it writes
its PING, and cannot return until the read loop has processed the PONG. The
flusher was woken first, and asyncio runs ready callbacks in order, so by the
time the first PONG is read the pending bytes are already written, behind that
PING. A second PING is written behind them, so its PONG does cover them.

If nats-py ever makes `flush` write the pending buffer first, the test that
pins the wire order fails, and this can become one flush again.
"""

from __future__ import annotations

from typing import Protocol

__all__ = ["BusClient", "round_trip"]


class BusClient(Protocol):
    async def flush(self, timeout: int = ...) -> None: ...


async def round_trip(client: BusClient) -> None:
    """Return once the server has processed everything sent before the call."""
    # The first PONG may overtake the pending SUB/PUB; see the module docstring.
    await client.flush()
    await client.flush()

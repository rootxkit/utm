"""The event loop never waits for SQLite (S-03).

The writer holds the queue's lock across a FULL-synchronous commit. Anything on
the event loop that takes the same lock - building `status`, building `hello` -
stalls pings and status behind that fsync, and a slow disk then looks like a
dead link. These tests hold the lock the way a slow fsync would and check that
the event loop keeps moving, and that the counters it reports are still true.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from agent.config import RelayConfig
from agent.framing import RECORD_HEADER_BYTES
from agent.queue import DurableQueue
from agent.relay import Relay
from tests.ports import free_tcp_port, free_udp_port

HOLD_S = 1.0


def make_relay(
    tmp_path: Path, *, queue_max_bytes: int = 64 * 1024 * 1024
) -> tuple[Relay, DurableQueue]:
    config = RelayConfig(
        station_id="event-loop-test",
        gateway_url=f"ws://127.0.0.1:{free_tcp_port()}/relay/v1",  # type: ignore[arg-type]
        token_path=tmp_path / "relay.token",
        queue_path=tmp_path / "relay-queue.sqlite3",
        bind_port=free_udp_port(),
        queue_max_bytes=queue_max_bytes,
    )
    durable_queue = DurableQueue(config.queue_path, max_bytes=config.queue_max_bytes)
    return Relay(config, durable_queue, "token"), durable_queue


@contextmanager
def slow_fsync(durable_queue: DurableQueue, hold_s: float = HOLD_S) -> Iterator[None]:
    """Hold the queue's lock from another thread, as a writer mid-commit does."""
    held = threading.Event()

    def hold() -> None:
        with durable_queue._lock:
            held.set()
            time.sleep(hold_s)

    thread = threading.Thread(target=hold)
    thread.start()
    held.wait()
    try:
        yield
    finally:
        thread.join()


def test_status_is_built_without_waiting_for_the_writer(tmp_path: Path) -> None:
    relay, durable_queue = make_relay(tmp_path)
    durable_queue.append([(1, b"x" * 32)] * 5)

    with slow_fsync(durable_queue):
        started = time.monotonic()
        status = relay._status_message()
        elapsed_s = time.monotonic() - started
    durable_queue.close()

    assert elapsed_s < HOLD_S / 4, f"status waited {elapsed_s:.2f} s for the lock"
    assert status["queue_depth"] == 5


def test_status_counters_follow_every_committed_change(tmp_path: Path) -> None:
    """Cached is not stale: append, cap, ack and drops all reach `status`."""
    record_bytes = RECORD_HEADER_BYTES + 32
    relay, durable_queue = make_relay(tmp_path, queue_max_bytes=record_bytes * 4)

    durable_queue.append([(1, b"x" * 32)] * 3)
    after_append = relay._status_message()
    durable_queue.append([(1, b"x" * 32)] * 3)
    after_cap = relay._status_message()
    durable_queue.record_intake_drops(2)
    after_drops = relay._status_message()
    durable_queue.acknowledge(durable_queue.next_seq - 1)
    after_ack = relay._status_message()
    durable_queue.close()

    assert (after_append["queue_depth"], after_append["queue_bytes"]) == (
        3,
        3 * record_bytes,
    )
    assert after_append["dropped_cap_total"] == 0
    assert (after_cap["queue_depth"], after_cap["dropped_cap_total"]) == (4, 2)
    assert after_drops["dropped_intake_total"] == 2
    assert (after_ack["queue_depth"], after_ack["queue_bytes"]) == (0, 0)


class _ScriptedConnection:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, message: str) -> None:
        self.sent.append(message)

    async def recv(self) -> str:
        return json.dumps({"type": "welcome", "resume_from_seq": 0})


def test_the_handshake_does_not_block_the_event_loop(tmp_path: Path) -> None:
    relay, durable_queue = make_relay(tmp_path)
    durable_queue.append([(1, b"x" * 32)] * 3)
    connection = _ScriptedConnection()
    ticks = 0

    async def ticker(stop: asyncio.Event) -> None:
        nonlocal ticks
        while not stop.is_set():
            ticks += 1
            await asyncio.sleep(0.01)

    async def run() -> int:
        stop = asyncio.Event()
        ticking = asyncio.create_task(ticker(stop))
        # Held until released, rather than for a fixed time, so the handshake
        # is certainly still waiting when the ticks are counted.
        held = threading.Event()
        release = threading.Event()

        def hold() -> None:
            with durable_queue._lock:
                held.set()
                release.wait(HOLD_S * 5)

        holder = threading.Thread(target=hold)
        holder.start()
        held.wait()
        try:
            handshake = asyncio.create_task(
                relay._handshake(connection)  # type: ignore[arg-type]
            )
            await asyncio.sleep(HOLD_S / 2)
            ticks_while_held = ticks
            # Otherwise the lock was never in the way and this proves nothing.
            assert not handshake.done()
            release.set()
            resume_from = await handshake
        finally:
            release.set()
            holder.join()
            stop.set()
            await ticking
        assert ticks_while_held >= 10, (
            f"the event loop ticked {ticks_while_held} times in {HOLD_S / 2} s "
            "while the handshake waited for the queue"
        )
        return resume_from

    resume_from = asyncio.run(run())
    durable_queue.close()

    hello = json.loads(connection.sent[0])
    assert hello["type"] == "hello"
    assert (hello["oldest_seq_held"], hello["newest_seq_held"]) == (0, 2)
    assert resume_from == 0

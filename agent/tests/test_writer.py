"""The writer thread under a failing disk (S-02).

A SQLite error used to end the writer thread silently. Intake then filled and
dropped everything while `status` still described a healthy station. These
tests take the disk away for real - SQLite reports SQLITE_FULL once it may not
grow - and read what the relay says about it, then give the disk back and
check that nothing held in memory was lost.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from agent.config import RelayConfig
from agent.queue import DurableQueue
from agent.relay import Relay, WriterDiedError
from agent.udp import ReceiveOnlyUDPSocket
from tests.ports import free_tcp_port, free_udp_port

# Large enough that every insert needs a fresh page, so a capped page count
# refuses it.
DATAGRAM_BYTES = 3000


def make_relay(
    tmp_path: Path, *, intake_queue_size: int = 10000
) -> tuple[Relay, DurableQueue, RelayConfig]:
    config = RelayConfig(
        station_id="writer-test",
        gateway_url=f"ws://127.0.0.1:{free_tcp_port()}/relay/v1",  # type: ignore[arg-type]
        token_path=tmp_path / "relay.token",
        queue_path=tmp_path / "relay-queue.sqlite3",
        bind_port=free_udp_port(),
        intake_queue_size=intake_queue_size,
    )
    durable_queue = DurableQueue(config.queue_path, max_bytes=config.queue_max_bytes)
    return Relay(config, durable_queue, "token"), durable_queue, config


def fill_disk(durable_queue: DurableQueue) -> None:
    """Cap the file at its current size. SQLite then reports SQLITE_FULL.

    Needs at least one record already stored: a fresh database has room for a
    first record in the page it already has.
    """
    with durable_queue._lock:
        pages = durable_queue._connection.execute("PRAGMA page_count").fetchone()[0]
        durable_queue._connection.execute(f"PRAGMA max_page_count = {pages}")


def free_disk(durable_queue: DurableQueue) -> None:
    with durable_queue._lock:
        durable_queue._connection.execute("PRAGMA max_page_count = 1073741823")


def send(config: RelayConfig, count: int, *, first: int = 0) -> None:
    sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        for n in range(first, first + count):
            payload = n.to_bytes(4, "big") * (DATAGRAM_BYTES // 4)
            sender.sendto(payload, ("127.0.0.1", config.bind_port))
    finally:
        sender.close()


def wait_until(condition: Callable[[], bool], timeout_s: float = 10.0) -> None:
    deadline = time.monotonic() + timeout_s
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached in time")
        time.sleep(0.02)


@contextlib.contextmanager
def running(relay: Relay) -> Any:
    udp: ReceiveOnlyUDPSocket = relay.start_intake()
    try:
        yield udp
    finally:
        relay.stop()
        udp.close()


def test_a_healthy_writer_says_storage_is_ok(tmp_path: Path) -> None:
    """The branch that says nothing is wrong: make it run and read it."""
    relay, durable_queue, config = make_relay(tmp_path)
    with running(relay):
        send(config, 20)
        wait_until(lambda: durable_queue.next_seq == 20)
        status = relay._status_message()
        assert relay.writer_alive
    durable_queue.close()

    assert status["storage_ok"] is True
    assert status["dropped_intake_total"] == 0
    assert status["queue_depth"] == 20


def test_a_failing_disk_is_reported_and_the_held_batch_survives_it(
    tmp_path: Path,
) -> None:
    relay, durable_queue, config = make_relay(tmp_path)
    with running(relay):
        send(config, 3)
        wait_until(lambda: durable_queue.next_seq == 3)
        fill_disk(durable_queue)

        send(config, 5, first=3)
        wait_until(lambda: not relay.storage_ok)
        degraded = relay._status_message()
        # Several retries, not one: the thread is alive and still trying.
        time.sleep(0.5)
        assert relay.writer_alive
        assert durable_queue.next_seq == 3

        free_disk(durable_queue)
        wait_until(lambda: durable_queue.next_seq == 8)
        wait_until(lambda: relay.storage_ok)
        recovered = relay._status_message()

    stored = durable_queue.read_from(0, max_bytes=1 << 30)
    durable_queue.close()

    assert degraded["storage_ok"] is False
    assert recovered["storage_ok"] is True
    # Held in memory through the outage and written once the disk came back,
    # in order and without a hole: nothing about it was loss.
    assert [r.seq for r in stored] == list(range(8))
    assert [int.from_bytes(r.datagram[:4], "big") for r in stored] == list(range(8))
    assert recovered["dropped_intake_total"] == 0


def test_a_disk_that_stays_full_shows_as_intake_drops_in_status(
    tmp_path: Path,
) -> None:
    """The signal the Gateway already acts on must move during the outage."""
    relay, durable_queue, config = make_relay(tmp_path, intake_queue_size=10)
    with running(relay):
        send(config, 1)
        wait_until(lambda: durable_queue.next_seq == 1)
        fill_disk(durable_queue)
        # One datagram first, so the writer is holding a failed batch and has
        # stopped taking from intake before the burst arrives.
        send(config, 1, first=1)
        wait_until(lambda: not relay.storage_ok)

        before = relay._status_message()
        send(config, 500, first=2)
        wait_until(lambda: relay._status_message()["dropped_intake_total"] > 0)
        during = relay._status_message()
        assert relay.writer_alive

        free_disk(durable_queue)
        wait_until(lambda: relay.storage_ok)
    durable_queue.close()

    with DurableQueue(config.queue_path) as reopened:
        persisted = reopened.dropped_intake_total

    assert before["dropped_intake_total"] == 0
    assert during["storage_ok"] is False
    assert during["dropped_intake_total"] > 0
    # Written to disk once it recovered, so a restart keeps the evidence.
    assert persisted >= during["dropped_intake_total"]


def test_stopping_with_a_full_disk_ends_the_writer_and_counts_what_it_held(
    tmp_path: Path,
) -> None:
    relay, durable_queue, config = make_relay(tmp_path)
    udp = relay.start_intake()
    send(config, 1)
    wait_until(lambda: durable_queue.next_seq == 1)
    fill_disk(durable_queue)
    send(config, 4, first=1)
    wait_until(lambda: not relay.storage_ok)

    relay.stop()
    udp.close()
    reported = durable_queue.dropped_intake_total
    # The page cap refuses new rows but not an in-place counter update, so
    # close() can still persist the count, as it would on a disk with a few
    # bytes left.
    durable_queue.close()

    with DurableQueue(config.queue_path) as reopened:
        persisted = reopened.dropped_intake_total

    assert not relay.writer_alive
    # All four held back by the full disk, none silently lost.
    assert reported == 4
    assert persisted == 4


def test_a_dead_writer_stops_the_uplink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken_writer() -> None:
        raise RuntimeError("a bug outside the storage error handling")

    relay, durable_queue, _ = make_relay(tmp_path)
    monkeypatch.setattr(relay, "_writer_loop", broken_writer)

    with running(relay), pytest.raises(WriterDiedError):
        asyncio.run(asyncio.wait_for(relay.run_uplink(), timeout=10.0))
    durable_queue.close()


def test_a_live_writer_does_not_stop_the_uplink(tmp_path: Path) -> None:
    """The pair of the test above: the watchdog must not fire on a healthy relay."""
    relay, durable_queue, _ = make_relay(tmp_path)

    async def run_briefly() -> None:
        task = asyncio.create_task(relay.run_uplink())
        await asyncio.sleep(1.5)
        assert not task.done(), task
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    with running(relay):
        asyncio.run(run_briefly())
    durable_queue.close()


class _RecordingConnection:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, message: str) -> None:
        self.sent.append(message)


def test_no_status_leaves_after_the_writer_has_died(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken_writer() -> None:
        raise RuntimeError("gone")

    relay, durable_queue, _ = make_relay(tmp_path)
    monkeypatch.setattr(relay, "_writer_loop", broken_writer)
    connection = _RecordingConnection()

    with running(relay):
        wait_until(lambda: not relay.writer_alive)
        with pytest.raises(WriterDiedError):
            asyncio.run(relay._status_loop(connection))  # type: ignore[arg-type]
    durable_queue.close()

    assert connection.sent == []


class _BrokenRollbacks:
    """Commit and rollback both fail, so the queue must poison itself."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def commit(self) -> None:
        raise sqlite3.OperationalError("disk I/O error")

    def rollback(self) -> None:
        raise sqlite3.OperationalError("disk I/O error during rollback")

    def __getattr__(self, name: str) -> Any:
        return getattr(self._connection, name)


def test_a_poisoned_queue_is_reported_as_storage_not_ok(tmp_path: Path) -> None:
    relay, durable_queue, _ = make_relay(tmp_path)
    healthy = relay._status_message()
    real_connection = durable_queue._connection
    durable_queue._connection = _BrokenRollbacks(real_connection)  # type: ignore[assignment]
    with pytest.raises(sqlite3.Error):
        durable_queue.append([(1, b"x")])
    poisoned = relay._status_message()
    real_connection.close()

    assert healthy["storage_ok"] is True
    assert durable_queue.poisoned
    assert poisoned["storage_ok"] is False

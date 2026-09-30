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
import threading
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
    tmp_path: Path,
    *,
    intake_queue_size: int = 10000,
    writer_stall_timeout_s: float = 5.0,
) -> tuple[Relay, DurableQueue, RelayConfig]:
    config = RelayConfig(
        station_id="writer-test",
        gateway_url=f"ws://127.0.0.1:{free_tcp_port()}/relay/v1",  # type: ignore[arg-type]
        token_path=tmp_path / "relay.token",
        queue_path=tmp_path / "relay-queue.sqlite3",
        bind_port=free_udp_port(),
        intake_queue_size=intake_queue_size,
        writer_stall_timeout_s=writer_stall_timeout_s,
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


def count_appends(
    durable_queue: DurableQueue, monkeypatch: pytest.MonkeyPatch
) -> dict[str, int]:
    attempts = {"n": 0}
    real_append = durable_queue.append

    def counting_append(datagrams: Any) -> Any:
        attempts["n"] += 1
        return real_append(datagrams)

    monkeypatch.setattr(durable_queue, "append", counting_append)
    return attempts


def test_a_healthy_writer_says_storage_is_ok(tmp_path: Path) -> None:
    """The branch that says nothing is wrong: make it run and read it."""
    stall_timeout_s = 0.3
    relay, durable_queue, config = make_relay(
        tmp_path, writer_stall_timeout_s=stall_timeout_s
    )
    with running(relay):
        send(config, 20)
        wait_until(lambda: durable_queue.next_seq == 20)
        # Idle for well past the stall limit: an idle writer is not a stalled
        # one, because it still completes a pass every batch interval.
        idle_from = time.monotonic()
        wait_until(
            lambda: relay._writer_heartbeat_monotonic > idle_from + 2 * stall_timeout_s
        )
        status = relay._status_message()
        assert relay.writer_alive
        assert not relay.writer_stalled
    durable_queue.close()

    assert status["storage_ok"] is True
    assert status["dropped_intake_total"] == 0
    assert status["queue_depth"] == 20


def test_a_failing_disk_is_reported_and_the_held_batch_survives_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    relay, durable_queue, config = make_relay(tmp_path)
    attempts = count_appends(durable_queue, monkeypatch)
    with running(relay):
        send(config, 3)
        wait_until(lambda: durable_queue.next_seq == 3)
        fill_disk(durable_queue)

        send(config, 5, first=3)
        wait_until(lambda: not relay.storage_ok)
        degraded = relay._status_message()
        # Several retries, not one: the thread is alive and still trying.
        failed_from = attempts["n"]
        wait_until(lambda: attempts["n"] >= failed_from + 3)
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


def test_a_hung_write_is_reported_and_the_drops_it_causes_are_counted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A write that neither fails nor finishes must not look healthy.

    The fake holds the queue's lock the way a commit stuck in fsync does, so
    everything that waits on that lock waits here too.
    """
    relay, durable_queue, config = make_relay(
        tmp_path, intake_queue_size=10, writer_stall_timeout_s=0.3
    )
    entered = threading.Event()
    release = threading.Event()
    real_append = durable_queue.append

    def hanging_append(datagrams: Any) -> Any:
        with durable_queue._lock:
            entered.set()
            release.wait(30.0)
        return real_append(datagrams)

    monkeypatch.setattr(durable_queue, "append", hanging_append)

    # Every status the Gateway could have received, across the whole outage
    # and recovery. The count must never fall: a fall and a rise would be
    # reported as a second loss.
    reported: list[int] = []
    sampling = threading.Event()

    def sample() -> None:
        while not sampling.is_set():
            reported.append(relay._status_message()["dropped_intake_total"])
            time.sleep(0.005)

    sampler = threading.Thread(target=sample, daemon=True)
    with running(relay):
        sampler.start()
        try:
            try:
                send(config, 1)
                assert entered.wait(5.0)
                send(config, 500, first=1)
                wait_until(lambda: relay._status_message()["dropped_intake_total"] > 0)
                wait_until(lambda: not relay.storage_ok)

                started = time.monotonic()
                during = relay._status_message()
                status_s = time.monotonic() - started
                assert relay.writer_alive
                assert relay.writer_stalled
            finally:
                release.set()

            wait_until(lambda: relay.storage_ok)
            wait_until(lambda: durable_queue.next_seq >= 1)
            after = relay._status_message()
        finally:
            # Stopped however the test ends, or a failure above leaves it
            # running and the process never exits.
            sampling.set()
            sampler.join()
    durable_queue.close()

    with DurableQueue(config.queue_path) as reopened:
        persisted = reopened.dropped_intake_total

    assert during["storage_ok"] is False
    assert during["dropped_intake_total"] > 0
    assert status_s < 0.1, f"status waited {status_s:.2f} s behind the hung write"
    assert after["storage_ok"] is True
    assert after["dropped_intake_total"] >= during["dropped_intake_total"]
    assert reported == sorted(reported), "dropped_intake_total went backwards"
    assert persisted == after["dropped_intake_total"]


def test_a_pass_with_nothing_to_write_does_not_declare_storage_healthy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only a write that succeeds may clear `storage_ok: false`."""
    relay, durable_queue, config = make_relay(tmp_path)

    def refusing_persist() -> None:
        raise sqlite3.OperationalError("database or disk is full")

    monkeypatch.setattr(durable_queue, "persist_intake_drops", refusing_persist)
    # A drop waiting to be written, and nothing else: the writer's first pass
    # fails on the counter alone, and every pass after it has nothing to do.
    relay._pending_intake_drops = 1

    with running(relay):
        wait_until(lambda: not relay.storage_ok)
        failed_at = time.monotonic()
        wait_until(lambda: relay._writer_heartbeat_monotonic > failed_at + 0.5)
        idle = relay._status_message()

        send(config, 1)
        wait_until(lambda: durable_queue.next_seq == 1)
        wait_until(lambda: relay.storage_ok)
        written = relay._status_message()
    durable_queue.close()

    assert idle["storage_ok"] is False
    assert idle["dropped_intake_total"] == 1
    assert written["storage_ok"] is True


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

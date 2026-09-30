"""The durable queue: what it keeps, what it deletes, and what it never does.

Everything the relay promises about surviving a dropped link or a restart is
implemented in DurableQueue, so these tests are the ones that matter most in
this package.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from agent.framing import RECORD_HEADER_BYTES
from agent.queue import DurableQueue


@pytest.fixture
def queue_path(tmp_path: Path) -> Path:
    return tmp_path / "relay-queue.sqlite3"


def datagrams(
    count: int, *, size: int = 32, start_ts: int = 1_000
) -> list[tuple[int, bytes]]:
    return [(start_ts + n, bytes([n % 256]) * size) for n in range(count)]


# --- sequencing -------------------------------------------------------------


def test_sequence_starts_at_zero(queue_path: Path) -> None:
    with DurableQueue(queue_path) as queue:
        records = queue.append(datagrams(3))

    assert [r.seq for r in records] == [0, 1, 2]


def test_sequence_continues_across_appends(queue_path: Path) -> None:
    with DurableQueue(queue_path) as queue:
        queue.append(datagrams(3))
        records = queue.append(datagrams(2))

    assert [r.seq for r in records] == [3, 4]


def test_empty_append_is_a_no_op(queue_path: Path) -> None:
    with DurableQueue(queue_path) as queue:
        assert queue.append([]) == []
        assert queue.next_seq == 0


# --- persistence ------------------------------------------------------------


def test_records_survive_a_restart(queue_path: Path) -> None:
    with DurableQueue(queue_path) as queue:
        queue.append(datagrams(5))

    with DurableQueue(queue_path) as reopened:
        assert reopened.depth == 5
        assert [r.seq for r in reopened.read_from(0, max_bytes=1 << 20)] == [
            0,
            1,
            2,
            3,
            4,
        ]


def test_restart_does_not_reuse_sequence_numbers(queue_path: Path) -> None:
    """A reused seq would be silently deduplicated away by the Gateway."""
    with DurableQueue(queue_path) as queue:
        queue.append(datagrams(4))
        queue.acknowledge(3)  # queue now empty, but seq must not rewind

    with DurableQueue(queue_path) as reopened:
        assert reopened.depth == 0
        assert [r.seq for r in reopened.append(datagrams(2))] == [4, 5]


def test_epoch_is_stable_across_restarts(queue_path: Path) -> None:
    with DurableQueue(queue_path) as queue:
        first = queue.epoch

    with DurableQueue(queue_path) as reopened:
        assert reopened.epoch == first


def test_a_fresh_database_gets_a_new_epoch(tmp_path: Path) -> None:
    """The whole point of the epoch (relay-v1 §4).

    A deleted queue restarts seq at zero. Without a fresh epoch the Gateway
    would recognise those numbers as already seen and discard live telemetry.
    """
    with DurableQueue(tmp_path / "one.sqlite3") as first:
        first_epoch = first.epoch
    with DurableQueue(tmp_path / "two.sqlite3") as second:
        second_epoch = second.epoch

    assert first_epoch != second_epoch
    assert len(first_epoch) == 32
    assert int(first_epoch, 16) >= 0  # 128 bits of hex


def test_deleting_the_database_yields_a_new_epoch(queue_path: Path) -> None:
    with DurableQueue(queue_path) as queue:
        original_epoch = queue.epoch
        queue.append(datagrams(3))

    queue_path.unlink()

    with DurableQueue(queue_path) as recreated:
        assert recreated.epoch != original_epoch
        assert recreated.next_seq == 0  # legitimately restarts


# --- acknowledgement --------------------------------------------------------


def test_acknowledge_deletes_cumulatively(queue_path: Path) -> None:
    with DurableQueue(queue_path) as queue:
        queue.append(datagrams(10))

        assert queue.acknowledge(4) == 5
        assert queue.depth == 5
        assert [r.seq for r in queue.read_from(0, max_bytes=1 << 20)] == [
            5,
            6,
            7,
            8,
            9,
        ]


def test_acknowledge_is_idempotent(queue_path: Path) -> None:
    """A repeated ack after a reconnect must not delete live records."""
    with DurableQueue(queue_path) as queue:
        queue.append(datagrams(5))
        queue.acknowledge(2)

        assert queue.acknowledge(2) == 0
        assert queue.depth == 2


def test_acknowledge_beyond_the_queue_is_harmless(queue_path: Path) -> None:
    with DurableQueue(queue_path) as queue:
        queue.append(datagrams(3))

        assert queue.acknowledge(999) == 3
        assert queue.depth == 0


def test_unacknowledged_records_are_never_deleted(queue_path: Path) -> None:
    with DurableQueue(queue_path) as queue:
        queue.append(datagrams(10))
        queue.acknowledge(3)

        remaining = queue.read_from(0, max_bytes=1 << 20)

    assert [r.seq for r in remaining] == [4, 5, 6, 7, 8, 9]


# --- the cap ----------------------------------------------------------------


def test_cap_drops_oldest_first(queue_path: Path) -> None:
    record_size = RECORD_HEADER_BYTES + 32
    with DurableQueue(queue_path, max_bytes=record_size * 5) as queue:
        queue.append(datagrams(8))

        held = queue.read_from(0, max_bytes=1 << 20)

    # The five newest survive; the three oldest are gone.
    assert [r.seq for r in held] == [3, 4, 5, 6, 7]


def test_cap_drops_are_counted_and_persisted(queue_path: Path) -> None:
    record_size = RECORD_HEADER_BYTES + 32
    with DurableQueue(queue_path, max_bytes=record_size * 5) as queue:
        queue.append(datagrams(8))
        assert queue.dropped_cap_total == 3

    with DurableQueue(queue_path, max_bytes=record_size * 5) as reopened:
        assert reopened.dropped_cap_total == 3


def test_cap_never_blocks_intake(queue_path: Path) -> None:
    """Old telemetry is sacrificed for new, never the other way round."""
    record_size = RECORD_HEADER_BYTES + 32
    with DurableQueue(queue_path, max_bytes=record_size * 3) as queue:
        for _ in range(10):
            queue.append(datagrams(3))

        held = queue.read_from(0, max_bytes=1 << 20)

    assert len(held) == 3
    assert [r.seq for r in held] == [27, 28, 29]  # the most recent three


def test_oldest_seq_held_reflects_cap_drops(queue_path: Path) -> None:
    """This is what makes a gap detectable (relay-v1 §11)."""
    record_size = RECORD_HEADER_BYTES + 32
    with DurableQueue(queue_path, max_bytes=record_size * 4) as queue:
        queue.append(datagrams(10))

        assert queue.oldest_seq_held == 6
        assert queue.newest_seq_held == 9


def test_a_zero_cap_is_refused(queue_path: Path) -> None:
    with pytest.raises(ValueError, match="must be positive"):
        DurableQueue(queue_path, max_bytes=0)


# --- empty-queue boundaries -------------------------------------------------


def test_empty_queue_reports_the_documented_boundaries(queue_path: Path) -> None:
    """relay-v1 §5: empty means oldest_seq_held == newest_seq_held + 1."""
    with DurableQueue(queue_path) as queue:
        assert queue.oldest_seq_held == 0
        assert queue.newest_seq_held == -1

        queue.append(datagrams(4))
        queue.acknowledge(3)

        assert queue.oldest_seq_held == 4
        assert queue.newest_seq_held == 3


# --- reads ------------------------------------------------------------------


def test_read_from_respects_the_byte_budget(queue_path: Path) -> None:
    with DurableQueue(queue_path) as queue:
        queue.append(datagrams(100, size=32))

        batch = queue.read_from(0, max_bytes=(RECORD_HEADER_BYTES + 32) * 10)

    assert len(batch) == 10


def test_read_from_always_returns_at_least_one_record(queue_path: Path) -> None:
    """An oversized record must not wedge the queue forever."""
    with DurableQueue(queue_path) as queue:
        queue.append([(1, b"x" * 4096)])

        batch = queue.read_from(0, max_bytes=1)

    assert len(batch) == 1


def test_read_from_starts_where_asked(queue_path: Path) -> None:
    with DurableQueue(queue_path) as queue:
        queue.append(datagrams(10))

        batch = queue.read_from(7, max_bytes=1 << 20)

    assert [r.seq for r in batch] == [7, 8, 9]


def test_read_from_past_the_end_is_empty(queue_path: Path) -> None:
    with DurableQueue(queue_path) as queue:
        queue.append(datagrams(3))

        assert queue.read_from(99, max_bytes=1 << 20) == []


class _CountingCursor:
    """Counts the rows SQLite actually hands back, however they are fetched."""

    def __init__(self, cursor: sqlite3.Cursor, owner: _CountingConnection) -> None:
        self._cursor = cursor
        self._owner = owner

    def __iter__(self) -> Iterator[Any]:
        for row in self._cursor:
            self._owner.rows_fetched += 1
            yield row

    def fetchone(self) -> Any:
        row = self._cursor.fetchone()
        if row is not None:
            self._owner.rows_fetched += 1
        return row

    def fetchall(self) -> list[Any]:
        rows = self._cursor.fetchall()
        self._owner.rows_fetched += len(rows)
        return rows

    def close(self) -> None:
        self._cursor.close()


class _CountingConnection:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self.rows_fetched = 0

    def execute(self, sql: str, parameters: Any = ()) -> _CountingCursor:
        return _CountingCursor(self._connection.execute(sql, parameters), self)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._connection, name)


def _count_rows(queue: DurableQueue) -> _CountingConnection:
    counting = _CountingConnection(queue._connection)
    queue._connection = counting  # type: ignore[assignment]
    return counting


def test_read_from_a_large_backlog_fetches_only_what_the_budget_holds(
    queue_path: Path,
) -> None:
    """S-01. The whole backlog used to be fetched to return ten records."""
    with DurableQueue(queue_path) as queue:
        queue.append(datagrams(20_000, size=32))
        counting = _count_rows(queue)

        batch = queue.read_from(0, max_bytes=(RECORD_HEADER_BYTES + 32) * 10)

    assert len(batch) == 10
    # Ten that fit, plus at most the one that proved the budget was spent.
    assert counting.rows_fetched <= 11


def test_an_oversized_first_record_is_read_without_the_backlog_behind_it(
    queue_path: Path,
) -> None:
    with DurableQueue(queue_path) as queue:
        queue.append([(1, b"x" * 4096)])
        queue.append(datagrams(5_000, size=32))
        counting = _count_rows(queue)

        batch = queue.read_from(0, max_bytes=1)

    assert [r.seq for r in batch] == [0]
    assert counting.rows_fetched <= 2


def test_draining_a_backlog_fetches_each_record_about_once(
    queue_path: Path,
) -> None:
    """Draining N records must cost O(N) rows, not O(N^2)."""
    count = 5_000
    budget = (RECORD_HEADER_BYTES + 32) * 50
    with DurableQueue(queue_path) as queue:
        queue.append(datagrams(count, size=32))
        counting = _count_rows(queue)

        drained = 0
        next_seq = 0
        batches = 0
        while batch := queue.read_from(next_seq, max_bytes=budget):
            drained += len(batch)
            next_seq = batch[-1].seq + 1
            batches += 1
            queue.acknowledge(batch[-1].seq)

    assert drained == count
    # Each batch reads its own rows plus one look-ahead, and each ack reads one
    # aggregate row. The old full read fetched about count**2 / 100 rows here.
    assert counting.rows_fetched <= count + 2 * batches


def test_datagrams_are_returned_byte_identical(queue_path: Path) -> None:
    payload = bytes(range(256)) * 4
    with DurableQueue(queue_path) as queue:
        queue.append([(42, payload)])

        assert queue.read_from(0, max_bytes=1 << 20)[0].datagram == payload


# --- intake drops -----------------------------------------------------------


def test_intake_drops_are_counted_and_persisted(queue_path: Path) -> None:
    with DurableQueue(queue_path) as queue:
        queue.record_intake_drops(3)
        queue.record_intake_drops(2)
        assert queue.dropped_intake_total == 5

    with DurableQueue(queue_path) as reopened:
        assert reopened.dropped_intake_total == 5


def test_intake_drops_do_not_disturb_the_sequence(queue_path: Path) -> None:
    """They happen before a seq is assigned, so numbering stays contiguous.

    This is exactly why a gap cannot describe them, and why status carries a
    separate counter (relay-v1 §11).
    """
    with DurableQueue(queue_path) as queue:
        queue.append(datagrams(2))
        queue.record_intake_drops(100)
        records = queue.append(datagrams(2))

    assert [r.seq for r in records] == [2, 3]


def test_the_two_drop_counters_are_independent(queue_path: Path) -> None:
    record_size = RECORD_HEADER_BYTES + 32
    with DurableQueue(queue_path, max_bytes=record_size * 2) as queue:
        queue.record_intake_drops(7)
        queue.append(datagrams(5))

        assert queue.dropped_intake_total == 7
        assert queue.dropped_cap_total == 3


# --- storage failures (S-02) ------------------------------------------------


def fill_disk(queue: DurableQueue) -> None:
    """Make SQLite refuse to grow, which it reports as a genuine SQLITE_FULL."""
    pages = queue._connection.execute("PRAGMA page_count").fetchone()[0]
    queue._connection.execute(f"PRAGMA max_page_count = {pages}")


def free_disk(queue: DurableQueue) -> None:
    queue._connection.execute("PRAGMA max_page_count = 1073741823")


class _FailingCommits:
    """A connection whose commit fails, as an fsync on a dying disk would."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self.failing = True

    def commit(self) -> None:
        if self.failing:
            raise sqlite3.OperationalError("disk I/O error")
        self._connection.commit()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._connection, name)


def test_a_failed_append_raises_and_leaves_the_accounting_untouched(
    queue_path: Path,
) -> None:
    with DurableQueue(queue_path) as queue:
        queue.append(datagrams(5))
        depth, total_bytes, next_seq = queue.depth, queue.total_bytes, queue.next_seq
        fill_disk(queue)

        with pytest.raises(sqlite3.OperationalError, match="full"):
            queue.append(datagrams(50, size=3000))

        assert (queue.depth, queue.total_bytes, queue.next_seq) == (
            depth,
            total_bytes,
            next_seq,
        )
        assert [r.seq for r in queue.read_from(0, max_bytes=1 << 20)] == list(range(5))


def test_a_failed_append_can_be_retried_with_the_same_sequence_numbers(
    queue_path: Path,
) -> None:
    """The writer holds a failed batch and offers it again (S-02)."""
    batch = datagrams(50, size=3000)
    with DurableQueue(queue_path) as queue:
        queue.append(datagrams(5))
        fill_disk(queue)
        with pytest.raises(sqlite3.OperationalError):
            queue.append(batch)

        free_disk(queue)
        records = queue.append(batch)

        assert [r.seq for r in records] == list(range(5, 55))
        assert queue.depth == 55
        assert queue.total_bytes == sum(
            r.encoded_size for r in queue.read_from(0, max_bytes=1 << 30)
        )


def test_a_failed_acknowledge_keeps_the_records_and_their_accounting(
    queue_path: Path,
) -> None:
    with DurableQueue(queue_path) as queue:
        queue.append(datagrams(10))
        total_bytes = queue.total_bytes
        failing = _FailingCommits(queue._connection)
        queue._connection = failing  # type: ignore[assignment]

        with pytest.raises(sqlite3.OperationalError):
            queue.acknowledge(4)

        assert (queue.depth, queue.total_bytes) == (10, total_bytes)
        failing.failing = False
        assert queue.read_from(0, max_bytes=1 << 20)[0].seq == 0


def test_intake_drops_are_counted_even_when_the_disk_refuses_them(
    queue_path: Path,
) -> None:
    """A failing disk must not hide the loss it causes (S-02)."""
    with DurableQueue(queue_path) as queue:
        failing = _FailingCommits(queue._connection)
        queue._connection = failing  # type: ignore[assignment]

        with pytest.raises(sqlite3.OperationalError):
            queue.record_intake_drops(4)
        queue.count_intake_drops(3)

        assert queue.dropped_intake_total == 7

        # The next commit that succeeds persists the total, once.
        failing.failing = False
        queue.append(datagrams(1))
        assert queue.dropped_intake_total == 7

    with DurableQueue(queue_path) as reopened:
        assert reopened.dropped_intake_total == 7


def test_an_append_whose_commit_fails_is_rolled_back_and_retryable(
    queue_path: Path,
) -> None:
    """The insert succeeded, the fsync did not: nothing may count as stored."""
    with DurableQueue(queue_path) as queue:
        queue.append(datagrams(5))
        total_bytes = queue.total_bytes
        failing = _FailingCommits(queue._connection)
        queue._connection = failing  # type: ignore[assignment]

        with pytest.raises(sqlite3.OperationalError):
            queue.append(datagrams(3))

        assert (queue.depth, queue.total_bytes) == (5, total_bytes)
        failing.failing = False
        assert [r.seq for r in queue.append(datagrams(3))] == [5, 6, 7]

    with DurableQueue(queue_path) as reopened:
        assert reopened.depth == 8
        assert [r.seq for r in reopened.read_from(0, max_bytes=1 << 20)] == list(
            range(8)
        )


def test_a_failed_append_that_evicted_for_the_cap_restores_the_cap_count(
    queue_path: Path,
) -> None:
    """The eviction rolls back with the insert, so its count must too."""
    record_size = RECORD_HEADER_BYTES + 32
    with DurableQueue(queue_path, max_bytes=record_size * 4) as queue:
        queue.append(datagrams(4))
        failing = _FailingCommits(queue._connection)
        queue._connection = failing  # type: ignore[assignment]

        # Two more would evict the two oldest, but the commit fails.
        with pytest.raises(sqlite3.OperationalError):
            queue.append(datagrams(2))

        assert queue.dropped_cap_total == 0
        assert queue.depth == 4
        assert [r.seq for r in queue.read_from(0, max_bytes=1 << 20)] == [0, 1, 2, 3]

        # The next append evicts one record, and only that one is counted.
        failing.failing = False
        assert [r.seq for r in queue.append(datagrams(1))] == [4]
        assert queue.dropped_cap_total == 1
        assert queue.depth == 4
        assert [r.seq for r in queue.read_from(0, max_bytes=1 << 20)] == [1, 2, 3, 4]

    with DurableQueue(queue_path, max_bytes=record_size * 4) as reopened:
        assert reopened.dropped_cap_total == 1

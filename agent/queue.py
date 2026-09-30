"""The relay's durable queue: SQLite in WAL mode.

Holds forwarded datagrams until the Gateway acknowledges them. Everything the
relay promises about not losing telemetry across a dropped link or a restart is
implemented here.

Two rules govern deletion:

1. A record is deleted when a cumulative ack covers it (relay-v1 §7).
2. A record is deleted when the queue is at its size cap and it is the oldest
   (relay-v1 §11).

Rule 2 is the only way an unacknowledged record is ever discarded, and it is
deliberate: an uncapped queue is an unbounded file on the pilot's laptop, and a
full disk can take QGC down with it (P7-11). Every such deletion is counted and
surfaced as a `gap`.
"""

from __future__ import annotations

import dataclasses
import secrets
import sqlite3
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType

from agent.framing import RECORD_HEADER_BYTES, Record

__all__ = [
    "DEFAULT_QUEUE_MAX_BYTES",
    "DurableQueue",
    "QueuePoisonedError",
    "QueueStats",
]

# 1 GiB. At the ~2.8 KiB/s per aircraft measured in ADR-001, three aircraft
# fill this in roughly a day and a half of continuous disconnection.
DEFAULT_QUEUE_MAX_BYTES = 1024 * 1024 * 1024

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS records (
    seq         INTEGER PRIMARY KEY,
    recv_utc_ns INTEGER NOT NULL,
    datagram    BLOB    NOT NULL,
    nbytes      INTEGER NOT NULL
);
"""

_EPOCH = "epoch"
_NEXT_SEQ = "next_seq"
_DROPPED_INTAKE = "dropped_intake_total"
_DROPPED_CAP = "dropped_cap_total"


class QueuePoisonedError(sqlite3.Error):
    """A rollback failed, so the connection's transaction state is unknown."""


@dataclass(frozen=True, slots=True)
class QueueStats:
    """The counters `status` reports, as of the last committed change."""

    depth: int
    total_bytes: int
    dropped_intake_total: int
    dropped_cap_total: int


class DurableQueue:
    """A persistent, sequence-numbered FIFO of datagrams."""

    def __init__(
        self,
        path: Path,
        *,
        max_bytes: int = DEFAULT_QUEUE_MAX_BYTES,
    ) -> None:
        if max_bytes <= 0:
            raise ValueError(f"max_bytes must be positive, got {max_bytes}")

        self._path = path
        self._max_bytes = max_bytes
        # Guards every statement. The UDP thread never touches this object; the
        # writer thread and the sender share it, and a lock is cheaper to
        # reason about than one connection per thread.
        self._lock = threading.Lock()

        path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(path, check_same_thread=False)
        self._connection.execute("PRAGMA journal_mode=WAL")
        # FULL, not NORMAL: the Gateway is told a record is safe once it is
        # here. Under NORMAL a power cut can lose recently committed
        # transactions, which would turn "acknowledged" into a hole. Batching
        # keeps this to a handful of commits per second.
        self._connection.execute("PRAGMA synchronous=FULL")
        self._connection.executescript(_SCHEMA)

        self._epoch = self._init_meta(_EPOCH, secrets.token_hex(16))
        self._init_meta(_NEXT_SEQ, "0")
        self._init_meta(_DROPPED_INTAKE, "0")
        self._init_meta(_DROPPED_CAP, "0")
        self._connection.commit()

        # Recomputed rather than persisted: a counter that drifts after a crash
        # would silently mis-enforce the cap.
        row = self._connection.execute(
            "SELECT COUNT(*), COALESCE(SUM(nbytes), 0) FROM records"
        ).fetchone()
        self._depth: int = row[0]
        self._total_bytes: int = row[1]
        # Held in memory and written through, rather than read back from the
        # database. A drop is counted the moment it is reported even when the
        # disk refuses the write, so a failing disk cannot hide the loss it is
        # causing; the next commit that succeeds persists the total. Guarded
        # by `_stats_lock`, not `_lock`, so counting never waits for a write.
        self._dropped_intake: int = self._get_int(_DROPPED_INTAKE)
        # The total as last committed, so an ack need not rewrite it unchanged.
        self._dropped_intake_persisted: int = self._dropped_intake

        self._dropped_cap: int = self._get_int(_DROPPED_CAP)

        # The counters are also published as an immutable snapshot under a
        # lock of their own, which is never held across a statement. `status`
        # reads that, so a status message is never queued behind the writer's
        # fsync (S-03), and never sees a transaction that has not committed.
        self._stats_lock = threading.Lock()
        self._stats = QueueStats(0, 0, 0, 0)
        self._publish_stats()
        # Set, never cleared, when a rollback fails (see _rollback_locked).
        self._poisoned = False

    # --- metadata ---------------------------------------------------------

    def _init_meta(self, key: str, default: str) -> str:
        row = self._connection.execute(
            "SELECT value FROM meta WHERE key = ?", (key,)
        ).fetchone()
        if row is not None:
            return str(row[0])
        self._connection.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?)", (key, default)
        )
        return default

    def _get_int(self, key: str) -> int:
        row = self._connection.execute(
            "SELECT value FROM meta WHERE key = ?", (key,)
        ).fetchone()
        return int(row[0])

    def _set_int(self, key: str, value: int) -> None:
        self._connection.execute(
            "UPDATE meta SET value = ? WHERE key = ?", (str(value), key)
        )

    @property
    def epoch(self) -> str:
        """Random 128-bit identity, minted when this database was created.

        Without it, a deleted or restored queue would restart `seq` at zero and
        the Gateway would discard the new data as already seen (relay-v1 §4).
        """
        return self._epoch

    @property
    def path(self) -> Path:  # pragma: no cover - trivial accessor
        return self._path

    @property
    def max_bytes(self) -> int:  # pragma: no cover - trivial accessor
        return self._max_bytes

    # --- state ------------------------------------------------------------

    def _publish_stats(self) -> None:
        """Snapshot the working counters. Call with `_lock` held."""
        with self._stats_lock:
            self._stats = QueueStats(
                depth=self._depth,
                total_bytes=self._total_bytes,
                dropped_intake_total=self._dropped_intake,
                dropped_cap_total=self._dropped_cap,
            )

    def _intake_drops_total(self) -> int:
        with self._stats_lock:
            return self._dropped_intake

    def stats(self) -> QueueStats:
        """The latest committed counters. Never waits for a database write."""
        with self._stats_lock:
            return self._stats

    @property
    def depth(self) -> int:
        return self.stats().depth

    @property
    def total_bytes(self) -> int:
        return self.stats().total_bytes

    @property
    def next_seq(self) -> int:
        with self._lock:
            # These three read through the same connection as the failed
            # transaction, so a poisoned queue could report sequence numbers
            # that were never committed, and `hello` would claim them.
            self._check_usable_locked()
            return self._get_int(_NEXT_SEQ)

    @property
    def oldest_seq_held(self) -> int:
        """Lowest sequence number still on disk.

        When the queue is empty this is the next sequence to be assigned, which
        makes `oldest_seq_held > newest_seq_held` the empty case, exactly as
        relay-v1 §5 describes.
        """
        with self._lock:
            self._check_usable_locked()
            row = self._connection.execute("SELECT MIN(seq) FROM records").fetchone()
            if row[0] is None:
                return self._get_int(_NEXT_SEQ)
            return int(row[0])

    @property
    def newest_seq_held(self) -> int:
        with self._lock:
            self._check_usable_locked()
            row = self._connection.execute("SELECT MAX(seq) FROM records").fetchone()
            if row[0] is None:
                return self._get_int(_NEXT_SEQ) - 1
            return int(row[0])

    @property
    def dropped_intake_total(self) -> int:
        """Intake drops reported so far, including any not yet on disk."""
        return self.stats().dropped_intake_total

    @property
    def dropped_cap_total(self) -> int:
        return self.stats().dropped_cap_total

    # --- mutation ---------------------------------------------------------

    def record_intake_drops(self, count: int) -> None:
        """Count datagrams dropped before a sequence number was assigned.

        These cannot appear as a `gap` — the sequence remains contiguous across
        them — so the count is all the Gateway gets (relay-v1 §11).

        The count is taken in memory before the write is attempted, so
        `dropped_intake_total` includes it even if this raises. What is written
        is the absolute total, which makes a later retry, or any later commit,
        idempotent rather than a second increment.
        """
        if count <= 0:
            return
        self.count_intake_drops(count)
        self.persist_intake_drops()

    def count_intake_drops(self, count: int) -> None:
        """Count intake drops in memory only, without touching the disk.

        Takes only the counters' own lock, never the lock a write holds, so it
        cannot wait behind a hung fsync: the relay calls it while holding the
        lock its intake thread needs. The total is reported at once and
        persisted by the next commit that succeeds.
        """
        if count <= 0:
            return
        with self._stats_lock:
            self._dropped_intake += count
            self._stats = dataclasses.replace(
                self._stats, dropped_intake_total=self._dropped_intake
            )

    def persist_intake_drops(self) -> None:
        """Write the intake drop total. Raises if the disk refuses it."""
        with self._lock:
            self._check_usable_locked()
            total = self._intake_drops_total()
            try:
                self._set_int(_DROPPED_INTAKE, total)
                self._connection.commit()
            except sqlite3.Error:
                self._rollback_locked()
                raise
            self._dropped_intake_persisted = total

    def append(self, datagrams: Sequence[tuple[int, bytes]]) -> list[Record]:
        """Assign sequence numbers to (recv_utc_ns, datagram) pairs and store them.

        Returns the stored records. Enforces the size cap afterwards, dropping
        oldest first; intake is never blocked, because blocking would discard
        live telemetry to preserve old telemetry.

        All or nothing: if the write fails the transaction is rolled back, the
        in-memory accounting is restored and the error propagates, so the same
        datagrams can be offered again and receive the same sequence numbers.
        """
        if not datagrams:
            return []

        with self._lock:
            self._check_usable_locked()
            depth, total_bytes, dropped_cap = (
                self._depth,
                self._total_bytes,
                self._dropped_cap,
            )
            try:
                seq = self._get_int(_NEXT_SEQ)
                records = [
                    Record(seq=seq + offset, recv_utc_ns=recv_utc_ns, datagram=datagram)
                    for offset, (recv_utc_ns, datagram) in enumerate(datagrams)
                ]
                self._connection.executemany(
                    "INSERT INTO records (seq, recv_utc_ns, datagram, nbytes) "
                    "VALUES (?, ?, ?, ?)",
                    [
                        (r.seq, r.recv_utc_ns, r.datagram, r.encoded_size)
                        for r in records
                    ],
                )
                self._set_int(_NEXT_SEQ, seq + len(records))
                # Carries any intake drops whose own write failed.
                drops_total = self._intake_drops_total()
                self._set_int(_DROPPED_INTAKE, drops_total)
                self._depth += len(records)
                self._total_bytes += sum(r.encoded_size for r in records)
                self._enforce_cap_locked()
                self._connection.commit()
            except sqlite3.Error:
                self._depth, self._total_bytes = depth, total_bytes
                self._dropped_cap = dropped_cap
                self._rollback_locked()
                raise
            self._dropped_intake_persisted = drops_total
            self._publish_stats()

        return records

    def _rollback_locked(self) -> None:
        """Discard a failed transaction, or poison the connection trying.

        A failed commit can leave the transaction open, and the next statement
        would silently join it. If the rollback fails too, the transaction's
        fate is unknown: a later commit on this connection could make the
        failed batch durable after all, beside its retry under different
        sequence numbers. So the connection refuses all further use.
        """
        try:
            self._connection.rollback()
        except sqlite3.Error:
            with self._stats_lock:
                self._poisoned = True

    def _check_usable_locked(self) -> None:
        if self.poisoned:
            raise QueuePoisonedError(
                "a rollback failed; the durable queue refuses further use "
                "until the relay is restarted"
            )

    @property
    def poisoned(self) -> bool:
        """True once a rollback has failed. Restarting the relay clears it."""
        with self._stats_lock:
            return self._poisoned

    def _enforce_cap_locked(self) -> None:
        if self._total_bytes <= self._max_bytes:
            return

        dropped = 0
        while self._total_bytes > self._max_bytes:
            row = self._connection.execute(
                "SELECT seq, nbytes FROM records ORDER BY seq LIMIT 1"
            ).fetchone()
            if row is None:  # pragma: no cover - loop condition implies a row exists
                break
            self._connection.execute("DELETE FROM records WHERE seq = ?", (row[0],))
            self._total_bytes -= int(row[1])
            self._depth -= 1
            dropped += 1

        if dropped:
            self._dropped_cap += dropped
            self._set_int(_DROPPED_CAP, self._dropped_cap)

    def read_from(self, seq: int, *, max_bytes: int) -> list[Record]:
        """Return stored records from `seq` onward, up to a byte budget.

        At least one record is returned when any exists at or after `seq`, even
        if it alone exceeds the budget — otherwise an oversized record would
        wedge the queue permanently.

        Only the rows the budget can hold are read. Every record costs at least
        `RECORD_HEADER_BYTES`, so no batch can hold more rows than the LIMIT
        below, and the cursor is stepped one row at a time and abandoned as
        soon as the budget is spent. Reading the whole backlog here, under the
        lock the writer needs, made draining a large backlog quadratic and
        stalled intake while it ran.
        """
        row_limit = max(max_bytes // RECORD_HEADER_BYTES, 0) + 1
        records: list[Record] = []
        budget = 0
        with self._lock:
            # A poisoned connection may still see the failed batch in its
            # open transaction, and those records must never be sent.
            self._check_usable_locked()
            cursor = self._connection.execute(
                "SELECT seq, recv_utc_ns, datagram, nbytes FROM records "
                "WHERE seq >= ? ORDER BY seq LIMIT ?",
                (seq, row_limit),
            )
            try:
                for row in cursor:
                    size = int(row[3])
                    if records and budget + size > max_bytes:
                        break
                    records.append(
                        Record(
                            seq=int(row[0]),
                            recv_utc_ns=int(row[1]),
                            datagram=bytes(row[2]),
                        )
                    )
                    budget += size
            finally:
                cursor.close()
        return records

    def acknowledge(self, seq: int) -> int:
        """Delete every record up to and including `seq`. Returns how many.

        Cumulative, per relay-v1 §7. An ack for a sequence the queue has
        already passed is harmless and deletes nothing.
        """
        with self._lock:
            self._check_usable_locked()
            try:
                rows = self._connection.execute(
                    "SELECT COUNT(*), COALESCE(SUM(nbytes), 0) FROM records "
                    "WHERE seq <= ?",
                    (seq,),
                ).fetchone()
                count, freed = int(rows[0]), int(rows[1])
                if count:
                    self._connection.execute(
                        "DELETE FROM records WHERE seq <= ?", (seq,)
                    )
                # Carries any intake drops whose own write failed; on a link
                # with no new telemetry, acks are the only commits there are.
                # Only when it changed: under synchronous=FULL every commit is
                # an fsync, and acks arrive several times a second.
                drops_total = self._intake_drops_total()
                drops_changed = drops_total != self._dropped_intake_persisted
                if drops_changed:
                    self._set_int(_DROPPED_INTAKE, drops_total)
                if count or drops_changed:
                    self._connection.commit()
            except sqlite3.Error:
                self._rollback_locked()
                raise
            if drops_changed:
                self._dropped_intake_persisted = drops_total
            # Only once the delete is durable, or the cap would be enforced
            # against records that are still on disk.
            self._depth -= count
            self._total_bytes -= freed
            self._publish_stats()
        return count

    def close(self) -> None:
        """Close, writing any intake drops counted while the disk refused them.

        Best effort: if the disk still refuses, the count is lost with the
        process, which the Gateway sees as a restart (relay-v1 §11 loss #4).
        """
        with self._lock:
            if not self.poisoned:
                try:
                    self._set_int(_DROPPED_INTAKE, self._intake_drops_total())
                    self._connection.commit()
                except sqlite3.Error:
                    self._rollback_locked()
            # Closing discards whatever a poisoned connection still had open.
            self._connection.close()

    def __enter__(self) -> DurableQueue:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

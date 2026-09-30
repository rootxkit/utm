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

import secrets
import sqlite3
import threading
from collections.abc import Sequence
from pathlib import Path
from types import TracebackType

from agent.framing import RECORD_HEADER_BYTES, Record

__all__ = ["DEFAULT_QUEUE_MAX_BYTES", "DurableQueue"]

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

    @property
    def depth(self) -> int:
        with self._lock:
            return self._depth

    @property
    def total_bytes(self) -> int:
        with self._lock:
            return self._total_bytes

    @property
    def next_seq(self) -> int:
        with self._lock:
            return self._get_int(_NEXT_SEQ)

    @property
    def oldest_seq_held(self) -> int:
        """Lowest sequence number still on disk.

        When the queue is empty this is the next sequence to be assigned, which
        makes `oldest_seq_held > newest_seq_held` the empty case, exactly as
        relay-v1 §5 describes.
        """
        with self._lock:
            row = self._connection.execute("SELECT MIN(seq) FROM records").fetchone()
            if row[0] is None:
                return self._get_int(_NEXT_SEQ)
            return int(row[0])

    @property
    def newest_seq_held(self) -> int:
        with self._lock:
            row = self._connection.execute("SELECT MAX(seq) FROM records").fetchone()
            if row[0] is None:
                return self._get_int(_NEXT_SEQ) - 1
            return int(row[0])

    @property
    def dropped_intake_total(self) -> int:
        with self._lock:
            return self._get_int(_DROPPED_INTAKE)

    @property
    def dropped_cap_total(self) -> int:
        with self._lock:
            return self._get_int(_DROPPED_CAP)

    # --- mutation ---------------------------------------------------------

    def record_intake_drops(self, count: int) -> None:
        """Count datagrams dropped before a sequence number was assigned.

        These cannot appear as a `gap` — the sequence remains contiguous across
        them — so the count is all the Gateway gets (relay-v1 §11).
        """
        if count <= 0:
            return
        with self._lock:
            self._set_int(_DROPPED_INTAKE, self._get_int(_DROPPED_INTAKE) + count)
            self._connection.commit()

    def append(self, datagrams: Sequence[tuple[int, bytes]]) -> list[Record]:
        """Assign sequence numbers to (recv_utc_ns, datagram) pairs and store them.

        Returns the stored records. Enforces the size cap afterwards, dropping
        oldest first; intake is never blocked, because blocking would discard
        live telemetry to preserve old telemetry.
        """
        if not datagrams:
            return []

        with self._lock:
            seq = self._get_int(_NEXT_SEQ)
            records = [
                Record(seq=seq + offset, recv_utc_ns=recv_utc_ns, datagram=datagram)
                for offset, (recv_utc_ns, datagram) in enumerate(datagrams)
            ]
            self._connection.executemany(
                "INSERT INTO records (seq, recv_utc_ns, datagram, nbytes) "
                "VALUES (?, ?, ?, ?)",
                [(r.seq, r.recv_utc_ns, r.datagram, r.encoded_size) for r in records],
            )
            self._set_int(_NEXT_SEQ, seq + len(records))
            self._depth += len(records)
            self._total_bytes += sum(r.encoded_size for r in records)
            self._enforce_cap_locked()
            self._connection.commit()

        return records

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
            self._set_int(_DROPPED_CAP, self._get_int(_DROPPED_CAP) + dropped)

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
            rows = self._connection.execute(
                "SELECT COUNT(*), COALESCE(SUM(nbytes), 0) FROM records WHERE seq <= ?",
                (seq,),
            ).fetchone()
            count, freed = int(rows[0]), int(rows[1])
            if count:
                self._connection.execute("DELETE FROM records WHERE seq <= ?", (seq,))
                self._depth -= count
                self._total_bytes -= freed
            self._connection.commit()
        return count

    def close(self) -> None:
        with self._lock:
            self._connection.commit()
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

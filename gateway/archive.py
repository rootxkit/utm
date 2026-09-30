"""The raw archive: every received datagram, on disk, compressed.

Files rather than the database. At ~240 MB per aircraft per day before
compression, five aircraft is ~36 GB a month of opaque bytes that are never
queried by content - after an incident you read a *time range*. Rows would buy
nothing and cost an index. The index of which segment covers which interval
lives in the telemetry database; the contents live here.

Layout, partitioned by station and epoch as the decision requires:

    <root>/<station_id>/<epoch>/<YYYY>/<MM>/<DD>/<HH>.zst

Each segment holds one hour. A segment is a sequence of independent zstd
frames, one per stored batch, each framed exactly as relay-v1 §6 frames a
binary batch. Two consequences, both deliberate:

- `tools/analyze_capture.py` and any future replay read the archive with the
  decoder they already have. The archive format *is* the wire format.
- A frame per batch means a truncated segment - a machine that lost power
  mid-write - loses only the final frame. One frame for the whole segment
  would make the hour unreadable from the first damaged byte.

Note that zstd does *not* raise on a truncated tail: it returns the bytes it
managed to decode, silently. That was measured, not assumed. So a short read is
caught here instead, by the record framing failing to parse and, where the
index is available, by comparing against the byte count recorded when the
segment was written.

**Durability is the hard requirement.** relay-v1 §7 says the Gateway
acknowledges only what it has committed, and the relay deletes its own copy on
that promise. So `append` returns only after the bytes are in the file and
`fsync` has returned. A buffered write here turns a power cut into a permanent
hole in the flight record.

**Segments are partitioned by `recv_utc_ns`, the station's own clock.** That is
what a time-range query means: when the datagram was captured, not when it
reached us. relay-v1 §9 warns the clock may be wrong, so the index also records
when the Gateway stored it; a station whose clock is off shows up as a
disagreement between the two rather than as silently misfiled hours.
"""

from __future__ import annotations

import asyncio
import os
import re
import struct
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import zstandard

from gateway.relay_records import Record, decode_records

# Compression level. 3 is zstd's default and the point where throughput still
# comfortably exceeds the ~2.8 KiB/s per aircraft this ingests; raising it
# spends CPU on the one path that must not fall behind a fleet.
COMPRESSION_LEVEL: Final = 3

_HEADER: Final = struct.Struct("<QqH")

# Station ids and epochs become directory names. Anything outside this set
# could escape the archive root - `..`, an absolute path, a drive letter - so
# it is rejected rather than sanitised: a station id that needs sanitising is
# a configuration error, and quietly rewriting it would file its data
# somewhere nobody looks for it.
_SAFE_NAME: Final = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")

NANOSECONDS_PER_SECOND: Final = 1_000_000_000


class ArchiveError(RuntimeError):
    """The archive could not store a batch durably."""


@dataclass(frozen=True, slots=True)
class SegmentWrite:
    """What one `append` put on disk, for the index row that describes it."""

    relative_path: str
    hour_start: datetime
    first_seq: int
    last_seq: int
    first_recv_utc_ns: int
    last_recv_utc_ns: int
    record_count: int
    compressed_bytes: int
    uncompressed_bytes: int


def segment_hour(recv_utc_ns: int) -> datetime:
    """The UTC hour a record belongs to.

    Integer arithmetic, floored, so it is correct for negative values too - a
    station whose clock predates 1970 sends a negative `recv_utc_ns`, and
    truncating towards zero would file it in the following hour.
    """
    seconds = recv_utc_ns // NANOSECONDS_PER_SECOND
    moment = datetime.fromtimestamp(seconds, tz=UTC)
    return moment.replace(minute=0, second=0, microsecond=0)


def segment_relative_path(station_id: str, epoch: str, hour: datetime) -> str:
    """Where a segment lives, relative to the archive root."""
    return f"{station_id}/{epoch}/{hour:%Y}/{hour:%m}/{hour:%d}/{hour:%H}.zst"


@dataclass
class RawArchive:
    """Append-only, fsync-per-batch, one directory tree."""

    root: Path
    compression_level: int = COMPRESSION_LEVEL
    _station_locks: dict[str, asyncio.Lock] = field(
        default_factory=dict, init=False, repr=False, compare=False
    )

    def station_lock(self, station_id: str) -> asyncio.Lock:
        """The lock every writer and deleter of one station's tree holds.

        `append` runs on a worker thread now, so nothing serialises it any
        more: two sessions of one station - an old socket still draining
        while the relay has reconnected and is resending (relay-v1 §10) -
        could append the same range to the same hour file from two threads,
        interleaving bytes and making the rest of the hour unreadable after
        it had been acknowledged. Retention's unlink raced the same append.
        The store holds this around append, index and watermark; retention
        holds it around unlink and mark.
        """
        return self._station_locks.setdefault(station_id, asyncio.Lock())

    def append(
        self, station_id: str, epoch: str, records: list[Record]
    ) -> list[SegmentWrite]:
        """Store records durably, returning one entry per segment touched.

        A batch that straddles an hour boundary is split, because a segment
        that claims to cover 14:00-15:00 and contains a record from 15:00 makes
        the index lie about the only thing it is for.
        """
        _check_name("station_id", station_id)
        _check_name("epoch", epoch)
        if not records:
            return []

        writes: list[SegmentWrite] = []
        for hour, group in group_by_hour(records):
            writes.append(self._append_to_segment(station_id, epoch, hour, group))
        return writes

    def delete_segment(self, relative_path: str) -> int:
        """Remove one segment from disk, returning the bytes reclaimed.

        Whole segments only. A partially deleted hour is a hole in the flight
        record with nothing recording that it is a hole, which is the outcome
        this whole design exists to avoid.

        A segment already gone is not an error: retention runs repeatedly, and
        a crash between deleting the file and marking the index would otherwise
        wedge the sweep for ever.
        """
        path = self.root / relative_path
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            return 0
        except OSError as error:
            raise ArchiveError(f"could not stat {relative_path}: {error}") from error

        try:
            path.unlink()
        except FileNotFoundError:
            return 0
        except OSError as error:
            raise ArchiveError(f"could not delete {relative_path}: {error}") from error
        return size

    def read_segment(
        self,
        relative_path: str,
        *,
        expected_uncompressed_bytes: int | None = None,
    ) -> list[Record]:
        """Read one segment back.

        The segment is a concatenation of zstd frames, and zstd decodes across
        them into one byte stream - which is exactly the relay-v1 §6 record
        stream that was written, so the wire decoder reads it unchanged.

        Contiguity is not required: a segment spanning a recorded `gap` jumps,
        and refusing to read the hour either side of a hole would make the
        archive useless at the moment it matters.

        Pass `expected_uncompressed_bytes` from the index row to detect a
        truncated tail exactly. Without it, truncation is caught only when it
        lands mid-record - which is usual but not guaranteed.
        """
        path = self.root / relative_path
        try:
            raw = path.read_bytes()
        except OSError as error:
            raise ArchiveError(f"could not read {relative_path}: {error}") from error

        decompressor = zstandard.ZstdDecompressor()
        try:
            payload = decompressor.stream_reader(raw, read_across_frames=True).read()
        except zstandard.ZstdError as error:
            raise ArchiveError(
                f"{relative_path}: damaged compressed data: {error}"
            ) from error

        if (
            expected_uncompressed_bytes is not None
            and len(payload) != expected_uncompressed_bytes
        ):
            raise ArchiveError(
                f"{relative_path}: decoded {len(payload)} bytes, the index says "
                f"{expected_uncompressed_bytes} were written; the segment is "
                f"truncated or the index is wrong"
            )

        return decode_records(payload, require_contiguous=False)

    def _append_to_segment(
        self, station_id: str, epoch: str, hour: datetime, records: list[Record]
    ) -> SegmentWrite:
        relative_path = segment_relative_path(station_id, epoch, hour)
        path = self.root / relative_path
        created = not path.exists()
        path.parent.mkdir(parents=True, exist_ok=True)

        payload = _encode_frame(records)
        compressed = zstandard.ZstdCompressor(
            level=self.compression_level, write_content_size=True
        ).compress(payload)

        try:
            with path.open("ab") as handle:
                handle.write(compressed)
                handle.flush()
                # The promise the ack is about to make. Without this the bytes
                # are in the page cache and a power cut loses them after the
                # relay has already deleted its copy.
                os.fsync(handle.fileno())
            if created:
                # A new file's *directory entry* needs its own fsync, or the
                # file can survive the crash while the name pointing at it does
                # not - the data would be there and unreachable.
                _fsync_directory(path.parent)
        except OSError as error:
            raise ArchiveError(f"could not store {relative_path}: {error}") from error

        return SegmentWrite(
            relative_path=relative_path,
            hour_start=hour,
            first_seq=records[0].seq,
            last_seq=records[-1].seq,
            first_recv_utc_ns=records[0].recv_utc_ns,
            last_recv_utc_ns=records[-1].recv_utc_ns,
            record_count=len(records),
            compressed_bytes=len(compressed),
            uncompressed_bytes=len(payload),
        )


def group_by_hour(records: list[Record]) -> list[tuple[datetime, list[Record]]]:
    """Split records into the hour segments `append` would write them to."""
    groups: list[tuple[datetime, list[Record]]] = []
    for record in records:
        hour = segment_hour(record.recv_utc_ns)
        if groups and groups[-1][0] == hour:
            groups[-1][1].append(record)
        else:
            groups.append((hour, [record]))
    return groups


def _encode_frame(records: list[Record]) -> bytes:
    """relay-v1 §6 framing, so the archive reads with the wire decoder."""
    chunks: list[bytes] = []
    for record in records:
        chunks.append(
            _HEADER.pack(record.seq, record.recv_utc_ns, len(record.datagram))
        )
        chunks.append(record.datagram)
    return b"".join(chunks)


def _check_name(label: str, value: str) -> None:
    if not _SAFE_NAME.match(value):
        raise ArchiveError(
            f"{label} {value!r} is not usable as a directory name; it must be "
            f"1-64 characters of letters, digits, dot, dash or underscore"
        )


def _fsync_directory(directory: Path) -> None:
    """Persist a new directory entry. A no-op where the platform forbids it."""
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        # Windows does not allow opening a directory this way. The archive is
        # still correct there - the file's own fsync has returned - but the
        # rename-durability guarantee is weaker, which is one more reason the
        # production target is Linux.
        return
    try:
        os.fsync(fd)
    except OSError:  # pragma: no cover - platform dependent
        pass
    finally:
        os.close(fd)

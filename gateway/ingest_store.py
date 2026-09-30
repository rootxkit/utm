"""The durable store the relay-v1 server needs, as an interface.

The medium is an open question - `docs/specs/p1-02-gateway-ingest.md` §12
question 2 asks whether the raw archive is TimescaleDB alongside `drone_state`,
object storage, or files, and §12 question 3 asks whether dedupe is bounded to
a recent window. Neither is answered yet, so the transport layer is written
against this interface rather than against a schema. When the questions are
settled, an implementation lands behind it and nothing above it changes.

What is *not* negotiable, whatever the medium, is the contract:

- `resume_from_seq` is answered from durable storage, never from memory or a
  cache (spec §4.2). A Gateway that restarts must answer the same number.
- `store_records` returns only once the records are durable (spec §4.3, §7 of
  the protocol). The ack that follows is a promise the relay acts on by
  deleting its own copy, so an ack for buffered data turns a Gateway crash into
  a permanent hole in the flight record.
- A recorded gap advances the resume point (protocol §11). Without that, the
  watermark sticks at the hole for the life of the epoch and every reconnect
  re-reports the same gap.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from gateway.relay_messages import Gap
from gateway.relay_records import Record
from gateway.station_state import LinkState, LossEvent


@dataclass(frozen=True, slots=True)
class StoredBatch:
    """What `store_records` did with a batch.

    `stored` is the records that were new - the ones that are in the archive
    now and were not before. Retransmissions (§10, at-least-once) are not in
    it, so the pipeline behind the store never parses a datagram twice and
    the link-quality counters never count one twice (S-05).
    """

    # The cumulative watermark: what may be acknowledged.
    watermark: int
    stored: list[Record] = field(default_factory=list)


class StoreError(RuntimeError):
    """The store could not complete an operation durably.

    Raised rather than returned so that a caller cannot acknowledge by
    accident. Spec §12 question 5 - what the Gateway does when the database is
    slow or down - is open; what is already decided is that it must not
    acknowledge what it has not stored, which makes this an exception and not a
    boolean.
    """


@runtime_checkable
class IngestStore(Protocol):
    """Everything the relay-v1 server side needs from durable storage."""

    async def open_epoch(self, station_id: str, epoch: str) -> None:
        """Declare this epoch current for the station.

        Called once per connection, from the handshake. Protocol §4: the epoch
        changes only when the relay's queue database is created, so a station
        presenting a different one will never continue the previous epoch -
        which is what makes closing it safe, and what bounds the dedupe state.
        """
        ...

    async def resume_from_seq(self, station_id: str, epoch: str) -> int:
        """The next sequence number this Gateway wants, from durable state.

        `0` for an epoch never seen (protocol §5). Must account for recorded
        gaps: the sequence numbers a gap covers are permanently absent and
        count as satisfied.
        """
        ...

    async def store_records(
        self, station_id: str, epoch: str, records: list[Record]
    ) -> StoredBatch:
        """Store records durably; return the watermark and what was new.

        The watermark is what may be acknowledged: the highest `seq` such
        that everything up to and including it is either stored or covered by a
        recorded gap. Duplicates are ignored - at-least-once on the wire,
        exactly-once after dedupe on `(station_id, epoch, seq)` (protocol §10)
        - and are absent from `stored`, so whatever runs after the store sees
        each record once.
        """
        ...

    async def record_gap(self, station_id: str, epoch: str, gap: Gap) -> None:
        """Record a gap as an event and advance the resume point past it."""
        ...

    async def record_loss(self, station_id: str, epoch: str, loss: LossEvent) -> None:
        """Record a loss with no sequence range: an intake drop or a restart."""
        ...

    async def record_link_state(
        self, station_id: str, state: LinkState, *, at_utc_ns: int
    ) -> None:
        """Record a station link-state transition."""
        ...


@dataclass
class InMemoryIngestStore:
    """A store that keeps everything in memory. For tests and nothing else.

    Named for what it is so it cannot be mistaken for a candidate. The spec's
    §3 table lists the sink's in-memory `set[int]` of every seq as the clearest
    example of what does not survive contact with a fleet: fine for a
    twelve-minute test with 58,000 records, hopeless over months. This has the
    same shape and the same limit.
    """

    records: dict[tuple[str, str], dict[int, Record]] = field(default_factory=dict)
    gaps: dict[tuple[str, str], list[Gap]] = field(default_factory=dict)
    losses: list[tuple[str, str, LossEvent]] = field(default_factory=list)
    link_states: list[tuple[str, LinkState, int]] = field(default_factory=list)
    opened: list[tuple[str, str]] = field(default_factory=list)

    async def open_epoch(self, station_id: str, epoch: str) -> None:
        self.opened.append((station_id, epoch))

    async def resume_from_seq(self, station_id: str, epoch: str) -> int:
        return self._watermark(station_id, epoch) + 1

    async def store_records(
        self, station_id: str, epoch: str, records: list[Record]
    ) -> StoredBatch:
        held = self.records.setdefault((station_id, epoch), {})
        stored: list[Record] = []
        for record in records:
            # Dedupe: first write wins. A retransmission after a lost ack
            # carries identical bytes, so which one is kept does not matter,
            # but overwriting would hide a station sending two different
            # payloads under one seq - which would be worth seeing.
            if record.seq not in held:
                held[record.seq] = record
                stored.append(record)
        return StoredBatch(self._watermark(station_id, epoch), stored)

    async def record_gap(self, station_id: str, epoch: str, gap: Gap) -> None:
        self.gaps.setdefault((station_id, epoch), []).append(gap)

    async def record_loss(self, station_id: str, epoch: str, loss: LossEvent) -> None:
        self.losses.append((station_id, epoch, loss))

    async def record_link_state(
        self, station_id: str, state: LinkState, *, at_utc_ns: int
    ) -> None:
        self.link_states.append((station_id, state, at_utc_ns))

    def _watermark(self, station_id: str, epoch: str) -> int:
        """Highest seq such that every number up to it is stored or gapped.

        `-1` when nothing is held, so that `resume_from_seq` is `0` for an
        epoch never seen, as protocol §5 requires.
        """
        key = (station_id, epoch)
        held = self.records.get(key, {})
        covered = self.gaps.get(key, [])

        seq = -1
        while True:
            candidate = seq + 1
            if candidate in held:
                seq = candidate
                continue
            # The gap's range is [from_seq, to_seq): `to_seq` is exclusive, so
            # a gap covering [58120, 61099) satisfies up to and including
            # 61098. Reading `to_seq` as inclusive here would claim one record
            # more than was lost and leave a real hole unrequested.
            covering = next(
                (g for g in covered if g.from_seq <= candidate < g.to_seq), None
            )
            if covering is not None:
                seq = covering.to_seq - 1
                continue
            return seq

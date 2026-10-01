"""What happens to a record after the transport has stored it.

`docs/specs/p1-02-gateway-ingest.md` §4 obligation 9: **parse MAVLink only
after all of the above.** By the time anything here runs, the datagram is in
the archive, the index is written and the relay has been told it can delete its
copy. So a failure in this module loses a derived view, never the flight
record — and that asymmetry is why it catches its own errors rather than
letting them propagate back into the transport and stall a station.

The order is fixed and each step depends on the one before:

    parse      bytes -> MAVLink messages          (gateway/parsing.py)
    classify   is this a vehicle?                 (gateway/classify.py)
    resolve    which drone, at the record's time? (gateway/binding.py)
    assemble   fold into a drone_state row        (gateway/drone_state.py)
    write      into the hypertable                (gateway/state_writer.py)
    publish    onto the bus for the console       (gateway/publisher.py)

`resolve` uses the **record's** timestamp, not now. A replayed backlog crossing
a SYSID reassignment resolves each record against the binding in force when it
was captured, which is the whole reason bindings have validity.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from common import get_logger
from gateway.binding import BindingResolver, Resolution
from gateway.classify import Source, SourceKind, SourceRegistry
from gateway.drone_state import (
    DroneStateRow,
    StateAssembler,
    timestamp_from_recv_utc_ns,
)
from gateway.firmware import MESSAGE_NAME as AUTOPILOT_VERSION
from gateway.firmware import firmware_from_message
from gateway.firmware_store import FirmwareRegistry
from gateway.link_quality import LinkQualityTracker
from gateway.live_state import LiveState
from gateway.parsing import ParsedMessage, SourceId, parse_datagram
from gateway.publisher import TelemetryPublisher
from gateway.rate_limit import DEFAULT_INTERVAL_S, RateLimiter
from gateway.relay_records import Record
from gateway.stage_timing import StageTimings, shared_timings
from gateway.state_buffer import RowWriter

_log = get_logger(__name__)

# U-02: a drone_id to its `identification` (`gateway/identification.py`).
Identify = Callable[[UUID], dict[str, Any]]


@dataclass(frozen=True, slots=True)
class _Observed:
    """One parsed message, with its record's seq and capture time, and its
    source as classified at that message."""

    seq: int
    ts: datetime
    message: ParsedMessage
    source: Source
    # Queued on the relay before the session that delivered it (S-11).
    backlog: bool


def _now_utc() -> datetime:
    return datetime.now(UTC)


# S-11. The longest capture span one batch is allowed to claim. A relay frame
# is bounded at 64 KiB (relay-v1 §6), about 7.8 s of records at §10's
# 8.4 KiB/s for three aircraft; a span past this is a station clock that
# stepped inside the batch, not a real spread, and is clamped and counted so
# no row is placed minutes before its batch.
MAX_BATCH_SPAN_S = 120.0


@dataclass
class IngestPipeline:
    """Turns stored records into rows, per station.

    One instance per station: the registry and the assembler are both
    per-station state, and §8 accepts two stations relaying one vehicle as
    independent observations.
    """

    station_id: str
    resolver: BindingResolver
    writer: RowWriter
    publisher: TelemetryPublisher
    timings: StageTimings = field(default_factory=shared_timings)
    # P1-07: sources refused by station policy are reported at most once per
    # address per interval, with a count of what was suppressed in between.
    rejections: RateLimiter = field(default_factory=RateLimiter)
    # P1-05. Optional so the pipeline runs, and is tested, without Redis.
    live_state: LiveState | None = None
    # P1-11. Optional for the same reason as live state.
    firmware: FirmwareRegistry | None = None
    # U-02. The registry's verdict on a bound aircraft, put on every row
    # published; None publishes `identification: null`.
    identify: Identify | None = None
    # The Gateway's wall clock, stamped on each batch as `rx_ts` (S-11): one
    # trusted clock for every station, unlike the relays' own.
    wall: Callable[[], datetime] = _now_utc

    registry: SourceRegistry = field(init=False)
    assembler: StateAssembler = field(init=False)
    # Rows whose in-batch span exceeded `MAX_BATCH_SPAN_S` and were placed
    # at the clamp instead (S-11). Logged per batch; counted for tests.
    span_clamped: int = field(default=0, init=False)

    # Unclaimed sources are announced once per address per interval, not once
    # per datagram and not once for ever. An aircraft transmitting at 84 Hz
    # with no binding would otherwise emit 84 identical events a second,
    # which is how a genuinely useful signal becomes something operators
    # filter out; announced only once, a station whose clock made every
    # record unclaimed (S-11) read as healthy after its first minute. Each
    # repeat carries the count suppressed since the last one.
    unclaimed_interval_s: float = DEFAULT_INTERVAL_S
    monotonic: Callable[[], float] = time.monotonic
    _unclaimed_announced_at: dict[SourceId, float] = field(
        default_factory=dict, init=False
    )
    _unclaimed_suppressed: dict[SourceId, int] = field(default_factory=dict, init=False)
    _bad_frames: int = field(default=0, init=False)
    # P1-09, per source like the accumulator: this station's view of the link.
    _links: dict[SourceId, LinkQualityTracker] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        self.registry = SourceRegistry(station_id=self.station_id)
        self.assembler = StateAssembler(station_id=self.station_id)

    async def process(
        self,
        epoch: str,
        records: list[Record],
        *,
        newest_seq_held: int = -1,
        draining: bool = False,
    ) -> list[DroneStateRow]:
        """Convert a batch of stored records. Never raises into the transport.

        `newest_seq_held` is the delivering session's `hello` value
        (relay-v1 §5): a record with seq at or below it was queued before the
        connection and is published with `backlog: true`, so the airspace
        monitor does not raise live alerts from where an aircraft was during
        an outage; so is every record while the session is `draining`. Every
        published message also carries `rx_ts`, this batch's receive time on
        the Gateway's clock, and `captured_at`, the row placed in time as
        `rx_ts - (newest ts in the batch - its ts)`: a draining relay's
        frame holds seconds of capture under one `rx_ts`, and a sample from
        its start is not simultaneous with one from its end. The station's
        clock skew cancels within the batch.

        Three passes, so bindings are read once per batch rather than once per
        message (P1-13). Per message, the resolver cost one database query:
        about 3.3 ms, which capped the Gateway at about 300 records/s at any
        fleet size and was 96% of the time spent storing a batch (ADR-002).

        Nothing about *which* binding applies changes. Each message is
        resolved against its own placed time (`captured_at`, on the Gateway's
        clock), with the source as it was classified at that message. Not
        the station's `ts`: relay-v1 §9 allows that clock to be wrong, and a
        station an hour slow resolved every record against the bindings of
        an hour ago, silently, as unclaimed. `Source` is a frozen snapshot, so
        classifying the whole batch first cannot leak a later HEARTBEAT back
        into an earlier message's resolution.
        """
        rx_ts = self.wall()
        observed = self._observe(epoch, records, newest_seq_held, draining)
        if not observed:
            return []
        newest_ts = max(item.ts for item in observed)
        placed = [self._placed(rx_ts, newest_ts, item) for item in observed]

        try:
            with self.timings.measure("process.resolve"):
                resolutions = await self.resolver.resolve_batch(
                    self.station_id,
                    [
                        (item.source, at)
                        for item, at in zip(observed, placed, strict=True)
                    ],
                )
        except Exception as error:
            # The flight record is already durable (obligation 9); what is lost
            # is this batch's derived view. Logged per batch, because a failed
            # binding read fails every message in it the same way.
            _log.error(
                "could not resolve a batch",
                extra={
                    "station_id": self.station_id,
                    "epoch": epoch,
                    "first_seq": observed[0].seq,
                    "last_seq": observed[-1].seq,
                    "error": repr(error),
                },
            )
            return []

        rows: list[DroneStateRow] = []
        # Parallel to `rows`: whether each came from a backlog record (a
        # batch can straddle the `hello` boundary, so it is per row), and
        # where each is placed in time.
        backlog: list[bool] = []
        captured_at: list[datetime] = []
        # Which address each drone was seen on in this batch, for its link.
        sources_by_drone: dict[UUID, SourceId] = {}
        for item, resolution, at in zip(observed, resolutions, placed, strict=True):
            if resolution.drone_id is not None:
                sources_by_drone[resolution.drone_id] = item.source.source_id
            try:
                row = await self._assemble(epoch, item, resolution)
            except Exception as error:
                _log.error(
                    "could not convert a record",
                    extra={
                        "station_id": self.station_id,
                        "epoch": epoch,
                        "seq": item.seq,
                        "error": repr(error),
                    },
                )
                continue
            if row is not None:
                rows.append(row)
                backlog.append(item.backlog)
                captured_at.append(at)

        if rows:
            labels: dict[UUID, str] = {}
            try:
                with self.timings.measure("process.write"):
                    await self.writer.write(rows)
                with self.timings.measure("process.labels"):
                    labels = await self._labels(rows)
                with self.timings.measure("process.publish"):
                    links = {
                        drone_id: self._links[source_id].snapshot().as_dict()
                        for drone_id, source_id in sources_by_drone.items()
                    }
                    firmware = await self._firmware_summaries(rows)
                    await self.publisher.publish_rows(
                        rows,
                        labels,
                        links,
                        firmware,
                        rx_ts=rx_ts,
                        backlog=backlog,
                        captured_at=captured_at,
                        identifications=self._identifications(rows),
                    )
            except Exception as error:
                _log.error(
                    "could not write drone_state",
                    extra={"station_id": self.station_id, "error": repr(error)},
                )
            # Its own handler, after the write rather than inside it. Live
            # state is derived from the same rows but is not downstream of the
            # hypertable: a failed insert must not also make every drone look
            # link-lost, and a Redis failure must not cost the insert.
            if self.live_state is not None:
                try:
                    with self.timings.measure("process.live_state"):
                        await self.live_state.update(rows, labels)
                except Exception as error:
                    _log.error(
                        "could not update live state",
                        extra={"station_id": self.station_id, "error": repr(error)},
                    )
        return rows

    async def _labels(self, rows: list[DroneStateRow]) -> dict[UUID, str]:
        """Registry names for the console, and never a reason to lose a row.

        Isolated in its own handler rather than sharing the one around the
        write and the publish. A label is decoration on a position; if looking
        one up fails, the position must still be written and still reach the
        console unnamed. Sharing the handler would have made a registry
        hiccup cost live telemetry, which is the wrong trade by a wide margin.
        """
        try:
            return await self.resolver.labels_for({row.drone_id for row in rows})
        except Exception as error:
            _log.warning(
                "could not read drone labels",
                extra={"station_id": self.station_id, "error": repr(error)},
            )
            return {}

    def _identifications(self, rows: list[DroneStateRow]) -> dict[UUID, dict[str, Any]]:
        """U-02: each bound aircraft as the registry sees it. Like a label,
        decoration on a position: a failure costs the field, never the row."""
        if self.identify is None:
            return {}
        try:
            return {
                drone_id: self.identify(drone_id)
                for drone_id in {row.drone_id for row in rows}
            }
        except Exception as error:
            _log.warning(
                "could not identify drones",
                extra={"station_id": self.station_id, "error": repr(error)},
            )
            return {}

    def _link(self, source_id: SourceId) -> LinkQualityTracker:
        tracker = self._links.get(source_id)
        if tracker is None:
            tracker = self._links[source_id] = LinkQualityTracker()
        return tracker

    def _placed(
        self, rx_ts: datetime, newest_ts: datetime, item: _Observed
    ) -> datetime:
        """Where the row sits in time on the Gateway's clock: `rx_ts` less
        how far behind the batch's newest record it was captured."""
        behind_s = (newest_ts - item.ts).total_seconds()
        if behind_s < 0.0 or behind_s > MAX_BATCH_SPAN_S:
            self.span_clamped += 1
            _log.warning(
                "a record's capture time is out of its batch's span; clamped",
                extra={
                    "station_id": self.station_id,
                    "seq": item.seq,
                    "behind_s": round(behind_s, 1),
                    "max_batch_span_s": MAX_BATCH_SPAN_S,
                    "span_clamped": self.span_clamped,
                },
            )
            behind_s = min(max(behind_s, 0.0), MAX_BATCH_SPAN_S)
        return rx_ts - timedelta(seconds=behind_s)

    def _observe(
        self,
        epoch: str,
        records: list[Record],
        newest_seq_held: int,
        draining: bool,
    ) -> list[_Observed]:
        """Parse and classify every message, in order, with its capture time."""
        observed: list[_Observed] = []
        for record in records:
            backlog = draining or record.seq <= newest_seq_held
            try:
                with self.timings.measure("process.parse"):
                    parsed = parse_datagram(record.datagram)
                self._bad_frames += parsed.bad_frame_count

                # The record's own capture time. Used for the binding lookup
                # and for the row, so a row's identity and its place in the
                # flight come from one clock.
                ts = timestamp_from_recv_utc_ns(record.recv_utc_ns)
                for message in parsed.messages:
                    source = self.registry.observe(message)
                    self._link(source.source_id).observe(message, ts)
                    observed.append(_Observed(record.seq, ts, message, source, backlog))
            except Exception as error:
                _log.error(
                    "could not convert a record",
                    extra={
                        "station_id": self.station_id,
                        "epoch": epoch,
                        "seq": record.seq,
                        "error": repr(error),
                    },
                )
        return observed

    async def _assemble(
        self, epoch: str, item: _Observed, resolution: Resolution
    ) -> DroneStateRow | None:
        if resolution.is_rejected:
            await self._report_rejected(epoch, resolution)
            return None

        if resolution.drone_id is None:
            await self._announce_unclaimed(epoch, item.source, resolution)
            # Archived already, and deliberately not written to drone_state:
            # §7 forbids auto-registration, so an unbound aircraft stays
            # visible and unwritten until someone binds it.
            return None

        if item.message.name == AUTOPILOT_VERSION and self.firmware is not None:
            await self._record_firmware(resolution.drone_id, item)

        return self.assembler.observe(
            item.source, item.message, drone_id=resolution.drone_id, ts=item.ts
        )

    async def _record_firmware(self, drone_id: UUID, item: _Observed) -> None:
        """P1-11. Its own handler: a firmware record is worth having, and not
        worth a row of position."""
        assert self.firmware is not None
        try:
            recorded = await self.firmware.observe(
                drone_id,
                self.station_id,
                item.ts,
                firmware_from_message(item.message.payload),
            )
        except Exception as error:
            _log.error(
                "could not record firmware",
                extra={"station_id": self.station_id, "error": repr(error)},
            )
            return
        if recorded:
            _log.info(
                "firmware recorded",
                extra={"station_id": self.station_id, "drone_id": str(drone_id)},
            )

    async def _firmware_summaries(
        self, rows: list[DroneStateRow]
    ) -> dict[UUID, dict[str, str | None]]:
        """Latest firmware per drone in the batch, from the registry's cache.

        A drone with none recorded is absent here and published as null, which
        the console shows as unknown - never as a blank that reads as fine.
        """
        if self.firmware is None:
            return {}
        summaries: dict[UUID, dict[str, str | None]] = {}
        for drone_id in {row.drone_id for row in rows}:
            try:
                known = await self.firmware.latest(drone_id)
            except Exception as error:
                _log.warning(
                    "could not read firmware",
                    extra={"station_id": self.station_id, "error": repr(error)},
                )
                continue
            if known is not None:
                summaries[drone_id] = known.summary()
        return summaries

    async def _announce_unclaimed(
        self, epoch: str, source: Source, resolution: Resolution
    ) -> None:
        # A GCS or a gimbal is not an unclaimed aircraft, it is a source that
        # was never going to be one. Announcing those would bury the case that
        # matters under QGroundControl's own heartbeat.
        #
        # Judged on the source as it was at this message, not as the registry
        # holds it now. The registry has already seen the whole batch, so a
        # HEARTBEAT later in it would otherwise decide for an earlier message.
        if source.kind in {SourceKind.GCS, SourceKind.COMPONENT}:
            return
        source_id = source.source_id

        now = self.monotonic()
        announced_at = self._unclaimed_announced_at.get(source_id)
        if announced_at is not None and now - announced_at < self.unclaimed_interval_s:
            self._unclaimed_suppressed[source_id] = (
                self._unclaimed_suppressed.get(source_id, 0) + 1
            )
            return
        self._unclaimed_announced_at[source_id] = now
        suppressed = self._unclaimed_suppressed.pop(source_id, 0)

        _log.warning(
            "unclaimed source",
            extra={
                "station_id": self.station_id,
                "sysid": source_id.sysid,
                "compid": source_id.compid,
                "reason": resolution.unclaimed_reason,
                "suppressed": suppressed,
            },
        )
        await self.resolver.record_unclaimed(
            self.station_id, epoch, resolution, suppressed=suppressed
        )
        await self.publisher.publish_unclaimed(self.station_id, resolution, source_id)

    async def _report_rejected(self, epoch: str, resolution: Resolution) -> None:
        """A station presented an address bound on another station (P1-07).

        Not announced once like an unclaimed source: a refusal that goes quiet
        after its first report reads as a refusal that stopped. Reported at
        most once per address per interval instead, carrying the count
        suppressed since the last report, so it neither floods nor falls
        silent. The records themselves are already archived.
        """
        source_id = resolution.source_id
        suppressed = self.rejections.admit(source_id)
        if suppressed is None:
            return
        _log.warning(
            "rejected source: not assigned to this station",
            extra={
                "station_id": self.station_id,
                "sysid": source_id.sysid,
                "compid": source_id.compid,
                "suppressed": suppressed,
            },
        )
        await self.resolver.record_unclaimed(
            self.station_id, epoch, resolution, suppressed=suppressed
        )
        await self.publisher.publish_unclaimed(self.station_id, resolution, source_id)

    def forget_unclaimed(self, source_id: SourceId) -> None:
        """Allow an address to be announced again.

        Called when a binding appears, so that if it is later removed the
        operator is told again rather than the silence being mistaken for
        everything being fine.
        """
        self._unclaimed_announced_at.pop(source_id, None)
        self._unclaimed_suppressed.pop(source_id, None)


@dataclass
class StationPipelines:
    """One `IngestPipeline` per station, created on first sight.

    This is what the relay-v1 server calls. Pipelines are per station because
    the source registry and the state accumulator both are: spec §8 accepts
    two stations relaying one vehicle, and they are independent observations,
    not a single stream to be merged.
    """

    resolver: BindingResolver
    writer: RowWriter
    publisher: TelemetryPublisher
    live_state: LiveState | None = None
    firmware: FirmwareRegistry | None = None
    identify: Identify | None = None

    pipelines: dict[str, IngestPipeline] = field(default_factory=dict)

    def for_station(self, station_id: str) -> IngestPipeline:
        pipeline = self.pipelines.get(station_id)
        if pipeline is None:
            pipeline = IngestPipeline(
                station_id=station_id,
                resolver=self.resolver,
                writer=self.writer,
                publisher=self.publisher,
                live_state=self.live_state,
                firmware=self.firmware,
                identify=self.identify,
            )
            self.pipelines[station_id] = pipeline
        return pipeline

    async def process(
        self,
        station_id: str,
        epoch: str,
        records: list[Record],
        *,
        newest_seq_held: int = -1,
        draining: bool = False,
    ) -> None:
        await self.for_station(station_id).process(
            epoch, records, newest_seq_held=newest_seq_held, draining=draining
        )

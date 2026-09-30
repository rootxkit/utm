"""The relay-v1 server side.

Conformance is `docs/protocols/relay-v1.md` §13, restated with local meaning in
`docs/specs/p1-02-gateway-ingest.md` §4. The obligations, and where each lives:

1. Validate the bearer token on the upgrade, resolve it to a `station_id`  -> `_process_request`
2. Answer `hello` with the true `resume_from_seq` from durable storage       -> `_handshake`
3. Persist before acknowledging                                             -> `_ingest_batch`
4. Cumulative `ack` carrying the epoch, at least once per second            -> `_acknowledge`
5. Deduplicate on `(station_id, epoch, seq)`                                -> the store
6. Record every `gap` as an event, and advance the resume point past it     -> `_handle_gap`
7. Track `dropped_intake_total` deltas between `status` messages            -> `StationLinkTracker`
8. Three missed `status` messages is unreachable, distinct from radio silent -> `StationLinkTracker`
9. Parse MAVLink only after all of the above                                -> not here at all

Obligation 9 is why this module contains no MAVLink whatsoever. The transport
does not need to understand the payload, and mixing the two turns a parsing bug
into a transport failure - the datagram is carried opaquely, exactly as
protocol §6 requires.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass, field
from typing import Final, Protocol

import websockets
from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.http11 import Request, Response

from common import BoundLogger, bind, get_logger
from gateway.ingest_store import IngestStore, StoreError
from gateway.rate_limit import RateLimiter
from gateway.relay_messages import (
    ControlMessageError,
    Gap,
    Hello,
    IgnoredMessage,
    Status,
    build_ack,
    build_welcome,
    parse_control_message,
)
from gateway.relay_records import Record, RecordFramingError, decode_records
from gateway.stage_timing import StageTimings, shared_timings
from gateway.station_state import LinkState, LossEvent, StationLinkTracker

_log = get_logger(__name__)

# protocol §7: "at least once per second while data is flowing".
ACK_INTERVAL_S: Final = 1.0

# How often a connected station's live state is republished to the bus.
# Not a protocol value: §8 fixes when a station becomes unreachable, not how
# often we say so. It matches the relay's `status` cadence so the console is
# never more than one relay heartbeat behind.
STATION_REPORT_INTERVAL_S: Final = 1.0

# WebSocket close codes. 1008 is "policy violation", which is what a
# protocol-conformance failure is once the connection is already open.
_CLOSE_PROTOCOL_ERROR: Final = 1008

_AUTHORIZATION_SCHEME: Final = "Bearer "

# protocol §2 and §14: the path carries the major version. A relay speaking
# a version this Gateway does not serve is told so with 404 at the upgrade,
# before its credential is looked at, rather than being welcomed onto a
# protocol whose record layout or guarantees it may not share.
RELAY_PATH: Final = "/relay/v1"


class RecordProcessor(Protocol):
    """What happens to records once they are durably stored.

    Called *after* `store_records` returns and before the ack is sent, so
    obligation 9 holds: nothing parses MAVLink until the transport has done
    its job. The implementation must not raise - a conversion fault is not a
    transport fault - and `IngestPipeline` catches its own.
    """

    async def process(
        self, station_id: str, epoch: str, records: list[Record]
    ) -> None: ...


class StationReporter(Protocol):
    """Publishes a station's live link state to whatever is watching.

    Deliberately not the same thing as `IngestStore.record_link_state`, which
    appends to the event log. The two answer different questions: the log
    answers "when did this station change state", and this answers "what is
    this station doing now". A console attaching at 14:03 cannot be served by
    an event written at 13:58, which is why this is driven by a timer and the
    log is driven by change.
    """

    async def publish_station(
        self,
        station_id: str,
        state: LinkState,
        *,
        last_datagram_age_ms: int | None = None,
        queue_depth: int | None = None,
        losses: list[LossEvent] | None = None,
        lag_s: float | None = None,
    ) -> None: ...


class StationAuthenticator(Protocol):
    """Resolves a presented bearer token to a station identity.

    Token storage, issuance and revocation are spec §12 question 1, still open.
    This interface is the part that is already decided: protocol §3 says the
    token identifies a *ground station*, not a vehicle, and which vehicles a
    station may carry is server policy evaluated elsewhere. A station is never
    trusted to assert what it is carrying.
    """

    async def station_for_token(self, token: str) -> str | None:
        """The station id, or None if the token is unknown or revoked."""
        ...


@dataclass
class RelayServer:
    """Terminates relay-v1 connections from ground stations."""

    store: IngestStore
    authenticator: StationAuthenticator
    # Optional so the transport can be tested, and run, without a conversion
    # pipeline behind it. A Gateway with no processor still stores and
    # acknowledges correctly; it simply produces no drone_state.
    processor: RecordProcessor | None = None
    # Optional for the same reason as `processor`, and absent for the same
    # reason it was missed: the transport is complete and correct without it.
    # Nothing failed while it was unset - the link state went to `ingest_events`
    # exactly as specified - and the only symptom was a console reading
    # "Stations: none" beside a station that was connected and streaming.
    station_reporter: StationReporter | None = None
    host: str = "127.0.0.1"
    port: int = 8081

    ack_interval_s: float = ACK_INTERVAL_S
    station_report_interval_s: float = STATION_REPORT_INTERVAL_S
    unreachable_after_s: float = 3.0
    radio_silent_after_ms: int = 3_000
    # P1-14. The Gateway passes its link timeout; see StationLinkTracker.
    lagging_after_s: float = 15.0
    # P1-10: where the time of storing a batch goes. Shared with the store and
    # the pipeline so one log line shows every stage's share.
    timings: StageTimings = field(default_factory=shared_timings)
    # P1-07: refused connections are logged at most once per remote address
    # per interval, with a count of those suppressed in between. Each one
    # costs a caller nothing, so logging all of them is a way to fill a disk.
    auth_rejections: RateLimiter = field(default_factory=RateLimiter)

    def __post_init__(self) -> None:
        self._server: Server | None = None
        self._trackers: dict[str, StationLinkTracker] = {}
        # S-06: the generation of the newest session per station. A session
        # whose generation is no longer current has been superseded by a
        # reconnect and must write nothing about the station's link state.
        self._generations: dict[str, int] = {}

    @property
    def trackers(self) -> dict[str, StationLinkTracker]:
        """Live link state per station, for whoever publishes to the console."""
        return self._trackers

    def _next_generation(self, station_id: str) -> int:
        generation = self._generations.get(station_id, 0) + 1
        self._generations[station_id] = generation
        return generation

    def is_current(self, station_id: str, generation: int) -> bool:
        """Whether a session of this generation still speaks for the station.

        Trackers are shared per station, so two sessions can exist for one:
        the relay reconnects after a half-open link, and the old session
        notices only when its own pings time out, about 20 s later. Without
        this check the old session's last act was to log and publish
        `unreachable` for a station whose new session was healthy and
        streaming (S-06).
        """
        return self._generations.get(station_id) == generation

    async def start(self) -> None:
        self._server = await serve(
            self._handle,
            self.host,
            self.port,
            process_request=self._process_request,
        )

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    @property
    def port_in_use(self) -> int:
        """The bound port, which differs from `port` when 0 was requested."""
        if self._server is None:
            raise RuntimeError("the server is not running")
        return int(next(iter(self._server.sockets)).getsockname()[1])

    # --- authentication ---------------------------------------------------

    async def _process_request(
        self, connection: ServerConnection, request: Request
    ) -> Response | None:
        """Reject a bad credential at the upgrade, with HTTP 401.

        Protocol §3 is specific about the status code, and it is not
        cosmetic: the relay treats `401` as fatal and stops retrying. A
        WebSocket close instead would leave it reconnecting for ever against a
        credential that will never work, turning an operator problem into a log
        flood.

        This exact substitution has already shipped once - a test stub that
        rejected tokens with a close frame meant the relay's fatal-auth path
        never ran while its tests passed - which is why `agent/` now has a test
        that drives a real 401.
        """
        if request.path.split("?", 1)[0] != RELAY_PATH:
            self._log_rejected_connection(
                connection, f"unsupported path {request.path!r}"
            )
            return connection.respond(404, f"not found; relay-v1 is at {RELAY_PATH}\n")

        presented = request.headers.get("Authorization")
        if presented is None or not presented.startswith(_AUTHORIZATION_SCHEME):
            self._log_rejected_connection(connection, "missing bearer token")
            return connection.respond(401, "missing bearer token\n")

        token = presented[len(_AUTHORIZATION_SCHEME) :]
        station_id = await self.authenticator.station_for_token(token)
        if station_id is None:
            # The token is deliberately not logged, not even truncated.
            self._log_rejected_connection(connection, "unknown or revoked token")
            return connection.respond(401, "unknown or revoked token\n")

        # Carried on the connection so the handler does not re-authenticate.
        connection.station_id = station_id  # type: ignore[attr-defined]
        return None

    def _log_rejected_connection(
        self, connection: ServerConnection, reason: str
    ) -> None:
        remote = connection.remote_address
        host = str(remote[0]) if remote else "unknown"
        suppressed = self.auth_rejections.admit(host)
        if suppressed is None:
            return
        _log.warning(
            "rejected relay connection",
            extra={"remote": host, "reason": reason, "suppressed": suppressed},
        )

    # --- connection lifecycle ---------------------------------------------

    async def _handle(self, connection: ServerConnection) -> None:
        station_id: str = connection.station_id  # type: ignore[attr-defined]
        log = bind(_log, station_id=station_id)

        try:
            hello, resume_from_seq = await self._handshake(connection, station_id, log)
        except ControlMessageError as error:
            log.warning("handshake rejected", extra={"error": str(error)})
            await connection.close(_CLOSE_PROTOCOL_ERROR, "bad hello")
            return

        tracker = self._trackers.setdefault(
            station_id,
            StationLinkTracker(
                station_id=station_id,
                unreachable_after_s=self.unreachable_after_s,
                radio_silent_after_ms=self.radio_silent_after_ms,
                lagging_after_s=self.lagging_after_s,
            ),
        )

        session = _Session(
            server=self,
            connection=connection,
            station_id=station_id,
            epoch=hello.epoch,
            tracker=tracker,
            log=log,
            generation=self._next_generation(station_id),
            resume_from_seq=resume_from_seq,
            newest_seq_held=hello.newest_seq_held,
        )
        await session.run()

    async def _handshake(
        self, connection: ServerConnection, station_id: str, log: BoundLogger
    ) -> tuple[Hello, int]:
        """Read `hello`, answer `welcome` with the durable resume point.

        Returns the `hello` and the `resume_from_seq` that was sent, which is
        what a `gap` on this session is checked against.
        """
        raw = await connection.recv()
        if isinstance(raw, bytes):
            raise ControlMessageError(
                "the first frame must be a text `hello`, got a binary frame; "
                "protocol §5 requires `welcome` before any data frame"
            )

        message = parse_control_message(raw)
        if not isinstance(message, Hello):
            kind = getattr(message, "type", type(message).__name__)
            raise ControlMessageError(f"expected `hello`, got {kind!r}")

        if message.station_id != station_id:
            # The token is the authority on who this is. A station claiming a
            # different id in `hello` is either misconfigured or attempting to
            # write under another station's identity; either way its records
            # would land under a key its credential does not cover.
            raise ControlMessageError(
                f"hello claims station_id {message.station_id!r} but the token "
                f"resolves to {station_id!r}"
            )

        # Declared before the resume point is read, so a station that has
        # recreated its queue has the previous epoch closed first. Reading the
        # watermark first would be harmless today and wrong the moment closing
        # an epoch affects what the watermark means.
        await self.store.open_epoch(station_id, message.epoch)
        resume_from_seq = await self.store.resume_from_seq(station_id, message.epoch)

        # Protocol §11: the server asking for records that never existed is a
        # protocol error the relay will close on, and spec §4 says the Gateway
        # must make it impossible by construction. If it happens, the query is
        # keyed wrongly - two epochs or two stations confused - and shipping
        # the number anyway would put the fault on the relay's side of the log.
        if resume_from_seq > message.newest_seq_held + 1:
            raise ControlMessageError(
                f"computed resume_from_seq={resume_from_seq} exceeds the "
                f"station's newest_seq_held={message.newest_seq_held} + 1 for "
                f"epoch {message.epoch}; the Gateway has confused two epochs "
                f"or two stations"
            )

        await connection.send(build_welcome(resume_from_seq))
        return message, resume_from_seq


@dataclass
class _Session:
    """One station's connection, after a successful handshake."""

    server: RelayServer
    connection: ServerConnection
    station_id: str
    epoch: str
    tracker: StationLinkTracker
    log: BoundLogger
    generation: int = 0
    # What `welcome` said, so a `gap` can be checked against it (§11).
    resume_from_seq: int = 0
    # What `hello` declared as the newest record on the relay's disk. A gap
    # cannot end beyond it: the records past it were never assigned.
    newest_seq_held: int = -1

    def __post_init__(self) -> None:
        # The highest seq durably stored for this epoch, cumulative. -1 means
        # nothing is storable yet, which is distinct from 0 - acknowledging
        # seq 0 would claim a record that may never have arrived. Starts at
        # the durable watermark `welcome` was computed from, and is taken as
        # already acknowledged: the relay learned it from `welcome`.
        self._watermark = self.resume_from_seq - 1
        self._acked = self._watermark
        self._last_state: LinkState | None = None
        self.tracker.start_session()

    @property
    def superseded(self) -> bool:
        """A newer session for this station exists; this one must stay quiet."""
        return not self.server.is_current(self.station_id, self.generation)

    async def run(self) -> None:
        acker = asyncio.create_task(self._acknowledge_periodically())
        reporter = asyncio.create_task(self._report_periodically())
        try:
            # The state at connect, recorded and reported before anything is
            # waited for. Without it both are left to a race: whether the log
            # opens with `unreachable` depends on whether a timer tick beat
            # the station's first `status`, and a console watching a station
            # come up sees nothing until a tick has passed.
            await self._tick_logged()
            async for message in self.connection:
                if isinstance(message, bytes):
                    await self._ingest_batch(message)
                else:
                    await self._handle_control(message)
        except websockets.WebSocketException as error:
            self.log.info("relay session ended", extra={"error": str(error)})
        except (StoreError, RecordFramingError, ControlMessageError) as error:
            self.log.error("closing relay session", extra={"error": str(error)})
            with contextlib.suppress(websockets.WebSocketException):
                await self.connection.close(_CLOSE_PROTOCOL_ERROR, str(error)[:120])
        finally:
            # Each step here runs whatever the previous one did. A task that
            # died with an exception used to re-raise from `await task` and
            # skip the final ack and the disconnect report (S-06).
            for name, task in (("acker", acker), ("reporter", reporter)):
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except Exception as error:
                    self.log.error(
                        "session task died", extra={"task": name, "error": repr(error)}
                    )
            # A final ack for anything stored since the last tick. The relay
            # survives without it - protocol §5 makes a lost ack cost a
            # retransmission, never a gap - but sending it saves the station
            # resending a batch it will only be told to discard.
            await self._acknowledge()
            await self._report_disconnected()

    async def _ingest_batch(self, frame: bytes) -> None:
        """Store a batch durably. Nothing is acknowledged before this returns."""
        timings = self.server.timings
        with timings.measure("decode"):
            records = decode_records(frame)
        if not records:
            return
        with timings.measure("store"):
            stored = await self.server.store.store_records(
                self.station_id, self.epoch, records
            )
        self._watermark = stored.watermark
        if not stored.stored:
            # A retransmission, in full. Nothing new to look inside, and
            # counting it would count the same telemetry twice (S-05).
            timings.count("retransmitted_batches", 1)
            return
        self.tracker.observe_stored(max(record.recv_utc_ns for record in stored.stored))
        # Only now, with the bytes durable and the watermark advanced, does
        # anything look inside them. Obligation 9. Only the records that were
        # new: the pipeline republishes what it parses and folds it into link
        # quality, and a resent batch is not a second flight.
        if self.server.processor is not None:
            with timings.measure("process"):
                await self.server.processor.process(
                    self.station_id, self.epoch, stored.stored
                )
        timings.count("batches", 1)
        timings.count("records", len(stored.stored))
        timings.report_if_due()

    async def _handle_control(self, payload: str) -> None:
        message = parse_control_message(payload)
        now_s = time.monotonic()

        if isinstance(message, Status):
            for loss in self.tracker.observe_status(message, now_s=now_s):
                await self.server.store.record_loss(self.station_id, self.epoch, loss)
            try:
                await self._record_state_change(now_s, message.utc_ns)
            except StoreError as error:
                # Same as the timer path: the event log being unavailable is
                # not a reason to drop the connection that carries telemetry.
                # The transition is retried on the next tick.
                self.log.error(
                    "could not log the link state; retrying next tick",
                    extra={"error": str(error)},
                )
            return

        if isinstance(message, Gap):
            await self._handle_gap(message, now_s)
            return

        if isinstance(message, IgnoredMessage):
            # protocol §14: ignore, do not reject. Counted so that a relay
            # speaking a newer dialect is visible.
            self.tracker.observe_ignored_message()
            return

        # A second `hello` on an open connection. §5 sends one per connection.
        raise ControlMessageError("`hello` received on an established session")

    async def _handle_gap(self, gap: Gap, now_s: float) -> None:
        if gap.epoch != self.epoch:
            raise ControlMessageError(
                f"gap declares epoch {gap.epoch} on a session for {self.epoch}"
            )
        # §11: a gap is the relay's answer to `resume_from_seq` asking for
        # records the cap discarded, so it starts exactly where we asked and
        # ends no later than one past the newest record `hello` said the relay
        # held. Anything else is not loss but confusion about which station
        # or epoch is being discussed, and §11 says the two must never be
        # conflated: recording it would advance the resume point over records
        # that may still exist. Refused as a protocol error, which closes the
        # connection; the relay reconnects with backoff (§12) and a gap that
        # keeps failing this check is an operator problem the logs name.
        if gap.from_seq != self._watermark + 1:
            raise ControlMessageError(
                f"gap starts at {gap.from_seq} but the resume point is "
                f"{self._watermark + 1}; a gap begins where the server asked "
                f"the relay to resume"
            )
        if gap.to_seq > self.newest_seq_held + 1:
            raise ControlMessageError(
                f"gap ends at {gap.to_seq} but hello declared "
                f"newest_seq_held={self.newest_seq_held}; records past "
                f"{self.newest_seq_held + 1} were never assigned"
            )

        # Recorded before the watermark moves. §11: a recorded gap advances the
        # resume point, and the order matters on a crash - a watermark past a
        # hole with no record of the hole would mean the flight history has a
        # silent discontinuity, which is the one outcome this design exists to
        # prevent. P10-03 replay must render the hole, not interpolate across
        # it: a replay that draws a smooth track through missing data invents
        # evidence.
        await self.server.store.record_gap(self.station_id, self.epoch, gap)
        self.tracker.observe_gap(gap, now_s=now_s)
        self._watermark = (
            await self.server.store.store_records(self.station_id, self.epoch, [])
        ).watermark

    async def _acknowledge_periodically(self) -> None:
        while True:
            await asyncio.sleep(self.server.ack_interval_s)
            await self._acknowledge()

    async def _acknowledge(self) -> None:
        """Send a cumulative ack, if there is anything new to acknowledge."""
        if self._watermark < 0 or self._watermark == self._acked:
            return
        with contextlib.suppress(websockets.WebSocketException):
            await self.connection.send(build_ack(self.epoch, self._watermark))
            self._acked = self._watermark

    async def _record_state_change(self, now_s: float, at_utc_ns: int) -> LinkState:
        """Append to the event log, but only when the state actually changed.

        The log stays a log. Writing a row every second would bury the four
        transitions that matter under a heartbeat, and `ingest_events` is what
        an incident is reconstructed from.
        """
        state = self.tracker.state(now_s=now_s, now_utc_ns=time.time_ns())
        if state != self._last_state and not self.superseded:
            await self.server.store.record_link_state(
                self.station_id, state, at_utc_ns=at_utc_ns
            )
            # After the write, so a write that failed is retried on the next
            # tick rather than the transition going unlogged.
            self._last_state = state
        return state

    async def _report_periodically(self) -> None:
        """Publish live state on a timer, not only when it changes.

        Two separate things fail if this only runs on change:

        1. A console attaching after the change never learns the state. This
           is what left the stations panel empty during the SITL run - the
           station went `healthy` once, seconds before the browser opened, and
           nothing said so again.
        2. `unreachable` is defined by the *absence* of `status` messages, so
           the transition into it can only be noticed by something that runs
           when nothing is arriving. Driven from `_handle_control` alone, the
           one state §9 exists to distinguish was unreachable in both senses.
        """
        while True:
            await asyncio.sleep(self.server.station_report_interval_s)
            await self._tick_logged()

    async def _tick_logged(self) -> None:
        """One tick, with a store failure logged rather than raised.

        A `StoreError` from the event log used to end this task, and with it
        every report to the console for the rest of the session: the stations
        panel froze on whatever was last published (S-06). The transition is
        retried on the next tick because `_record_state_change` only notes
        the state once it is written.
        """
        try:
            await self._tick()
        except StoreError as error:
            self.log.error(
                "could not log the link state; retrying next tick",
                extra={"error": str(error)},
            )

    async def _tick(self) -> None:
        """Log the state if it changed, and report it either way."""
        if self.superseded:
            return
        now_s = time.monotonic()
        # `time.time_ns` because a transition detected by a timer has no
        # message to take a timestamp from.
        state = await self._record_state_change(now_s, time.time_ns())
        await self._report(state)

    async def _report_disconnected(self) -> None:
        """One last report, so a station that left does not freeze as healthy.

        The reporter dies with the session, so whatever it published last is
        what the console keeps showing. For a station that has just
        disconnected that would be `healthy`, indefinitely - a live link drawn
        to a relay that is gone.

        §9: a relay we cannot reach is presumed buffering, not losing.
        `unreachable` says that; `data_lost` stays reserved for loss that has
        actually been observed, and so is never overwritten here.

        Unless this session has been superseded. Then the station is not
        gone - it reconnected, and the new session speaks for it - and the
        one thing this must not do is write `unreachable` over a live link.
        """
        if self.superseded:
            self.log.info(
                "superseded session ended; link state left to the newer one",
                extra={"generation": self.generation},
            )
            return
        state = self.tracker.state(now_s=time.monotonic())
        if state is not LinkState.DATA_LOST:
            state = LinkState.UNREACHABLE
        # Logged as well as reported. Without the row the event log's last
        # word on a station that left mid-flight was `healthy`, and P10-03
        # replay, which explains a hole in a track from this log, had nothing
        # to say about the commonest cause of one.
        if state != self._last_state:
            self._last_state = state
            try:
                await self.server.store.record_link_state(
                    self.station_id, state, at_utc_ns=time.time_ns()
                )
            except StoreError as error:
                # The session is over either way; the report below still
                # goes out, so the console is not left showing `healthy`.
                self.log.error(
                    "could not log the disconnect", extra={"error": str(error)}
                )
        await self._report(state)

    async def _report(self, state: LinkState) -> None:
        reporter = self.server.station_reporter
        if reporter is None:
            return
        status = self.tracker.last_status
        # `last_datagram_age_ms` is carried forward unaged on purpose. It is
        # the relay's measurement of how long since *it* heard the aircraft,
        # and once the relay is unreachable we have no basis to advance it -
        # doing so would report radio silence we cannot observe, on a link
        # that may be carrying telemetry into a buffer perfectly well.
        try:
            await reporter.publish_station(
                self.station_id,
                state,
                last_datagram_age_ms=None
                if status is None
                else status.last_datagram_age_ms,
                queue_depth=None if status is None else status.queue_depth,
                losses=list(self.tracker.losses),
                lag_s=self.tracker.lag_s(now_utc_ns=time.time_ns()),
            )
        except Exception as error:
            # The console is a view of the record, never a condition of it.
            # A bus that is down must not end the reporter, let alone the
            # session that stores and acknowledges.
            self.log.error(
                "could not publish the station state", extra={"error": repr(error)}
            )

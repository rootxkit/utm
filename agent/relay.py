"""The ground relay process (P1-01), implementing relay-v1.

    UDP 127.0.0.1:14445
      -> intake thread      (never touches disk or network)
      -> bounded memory queue
      -> writer thread      -> SQLite, WAL
      -> sender task        -> WSS

The shape follows one rule: **the intake thread must never wait for anything**.
A datagram missed while a thread blocked on a disk flush or a TCP retransmit is
gone for good, and no amount of downstream reliability recovers it. So intake
does exactly two things — receive, and hand off — and if the hand-off would
block, it counts a drop and carries on.
"""

from __future__ import annotations

import asyncio
import json
import queue as queue_module
import random
import sqlite3
import ssl
import threading
import time
from typing import Any

import websockets
from websockets.asyncio.client import ClientConnection

from agent.config import RelayConfig
from agent.framing import Record, encode_records
from agent.queue import DurableQueue, QueuePoisonedError
from agent.udp import ReceiveOnlyUDPSocket
from common.logging import BoundLogger, bind, get_logger

__all__ = [
    "PROTOCOL_VERSION",
    "RELAY_VERSION",
    "ProtocolError",
    "Relay",
    "WriterDiedError",
]

PROTOCOL_VERSION = 1
RELAY_VERSION = "0.1.0"

# relay-v1 §6: flush on whichever comes first.
BATCH_INTERVAL_S = 0.1
BATCH_MAX_BYTES = 64 * 1024

# The writer's own batching: how many datagrams go into one SQLite commit.
WRITER_BATCH_MAX_RECORDS = 1000
WRITER_THREAD_NAME = "relay-writer"
INTAKE_THREAD_NAME = "relay-intake"
# How long a stopping writer waits for intake to notice the stop. Intake
# checks at each receive timeout (ReceiveOnlyUDPSocket defaults to 0.5 s).
INTAKE_JOIN_TIMEOUT_S = 2.0
# Retry cadence while the durable queue refuses writes. Short at first, since
# a transient lock or I/O error usually clears at once; capped so a full disk
# is retried, and logged, every few seconds rather than in a tight loop.
WRITER_BACKOFF_INITIAL_S = 0.1
WRITER_BACKOFF_MAX_S = 5.0
# Well inside the status cadence, so drops made during a backoff show up in the
# next `status` rather than one backoff later.
WRITER_DROP_FLUSH_INTERVAL_S = 0.25
# How often the uplink checks that the writer thread is still running.
WRITER_WATCH_INTERVAL_S = 0.5

# relay-v1 §8.
STATUS_INTERVAL_S = 1.0

# relay-v1 §12.
BACKOFF_INITIAL_S = 0.5
BACKOFF_MAX_S = 10.0
BACKOFF_FACTOR = 2.0


class ProtocolError(RuntimeError):
    """The peer said something that cannot be reconciled with our state."""


class FatalAuthError(RuntimeError):
    """The token was rejected. Retrying will not help."""


class WriterDiedError(RuntimeError):
    """The thread that writes to the durable queue has stopped."""


def _first_exception(group: BaseExceptionGroup[BaseException]) -> BaseException:
    """Pull the original failure out of a TaskGroup's ExceptionGroup.

    asyncio.TaskGroup wraps whatever ended a session, so a dropped connection
    arrives as an ExceptionGroup rather than as ConnectionClosed. Without
    unwrapping, the reconnect logic below cannot tell an ordinary disconnect
    from a real fault, and logs every dropped link as "unexpected".
    """
    for exc in group.exceptions:
        if isinstance(exc, BaseExceptionGroup):  # pragma: no cover - no nesting today
            return _first_exception(exc)
        return exc
    return group  # pragma: no cover - unreachable: an ExceptionGroup is never empty


def _now_pair() -> tuple[int, int]:
    """Sample the monotonic and wall clocks together (relay-v1 §9).

    Taken as a pair so the Gateway can tell a clock correction from elapsed
    time: the monotonic clock cannot jump, so a change in the difference
    between them is the wall clock being stepped.
    """
    return time.monotonic_ns(), time.time_ns()


class Relay:
    """Owns the intake threads and the uplink session."""

    def __init__(
        self,
        config: RelayConfig,
        durable_queue: DurableQueue,
        token: str,
        log: BoundLogger | None = None,
    ) -> None:
        self._config = config
        self._queue = durable_queue
        self._token = token
        self._log = log or bind(
            get_logger("agent.relay"),
            station_id=config.station_id,
            epoch=durable_queue.epoch,
        )

        self._intake: queue_module.Queue[tuple[int, bytes]] = queue_module.Queue(
            maxsize=config.intake_queue_size
        )
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

        self._started_monotonic = time.monotonic()
        self._last_datagram_monotonic: float | None = None
        self._pending_intake_drops = 0
        self._storage_ok = True
        self._writer_heartbeat_monotonic = time.monotonic()
        self._counters_lock = threading.Lock()

    # --- intake -----------------------------------------------------------

    def _intake_loop(self, udp: ReceiveOnlyUDPSocket) -> None:
        """Receive and hand off. Nothing else belongs in this loop."""
        while not self._stop.is_set():
            datagram = udp.receive()
            if datagram is None:
                continue

            received_at = time.time_ns()
            with self._counters_lock:
                self._last_datagram_monotonic = time.monotonic()

            try:
                self._intake.put_nowait((received_at, datagram))
            except queue_module.Full:
                # Dropping here is the correct outcome: blocking would stall
                # the socket and lose datagrams we cannot even count.
                with self._counters_lock:
                    self._pending_intake_drops += 1

    def _collect_batch(self) -> list[tuple[int, bytes]]:
        batch: list[tuple[int, bytes]] = []
        deadline = time.monotonic() + BATCH_INTERVAL_S
        while time.monotonic() < deadline and len(batch) < WRITER_BATCH_MAX_RECORDS:
            timeout = max(deadline - time.monotonic(), 0.0)
            try:
                batch.append(self._intake.get(timeout=timeout))
            except queue_module.Empty:
                break
        return batch

    def _writer_loop(self) -> None:
        """Drain the memory queue into SQLite in batches.

        A storage failure must not end this thread: if it did, intake would
        fill and drop everything while the status stream still looked healthy.
        A batch that fails to write is held and offered again - `append` is all
        or nothing, so the retry assigns the same sequence numbers - and
        nothing new is taken from intake meanwhile. That keeps memory bounded:
        once intake fills, datagrams are dropped at the socket and counted in
        `dropped_intake_total`, which the Gateway already treats as data loss.
        """
        held: list[tuple[int, bytes]] = []
        backoff_s = WRITER_BACKOFF_INITIAL_S
        while not self._stop.is_set() or not self._intake.empty() or held:
            self._beat()
            if not held:
                held = self._collect_batch()

            # Only these two: anything else is a bug, and retrying a bug
            # forever as if it were a disk outage would hide it. It ends the
            # thread instead, and the watchdog stops the relay.
            failure: sqlite3.Error | OSError | None = None
            wrote = False
            if held:
                try:
                    self._queue.append(held)
                    held = []
                    wrote = True
                except (sqlite3.Error, OSError) as error:
                    failure = error

            # Counted even when the batch above failed: the in-memory total
            # moves regardless of whether the disk accepts it, so the loss is
            # visible in `status` while storage is down.
            drops = self._transfer_pending_drops()
            if drops:
                try:
                    self._queue.persist_intake_drops()
                    wrote = True
                except (sqlite3.Error, OSError) as error:
                    failure = failure or error
                self._log.warning(
                    "intake queue full, datagrams dropped",
                    extra={"dropped": drops},
                )

            if failure is None:
                # Only a write that succeeded is evidence the disk is back; a
                # pass with nothing to write proves nothing.
                if wrote and self._set_storage_ok(True):
                    self._log.info("durable queue writable again")
                backoff_s = WRITER_BACKOFF_INITIAL_S
                continue

            self._set_storage_ok(False)
            if self._queue.poisoned:
                # No retry can succeed: only a restart gives a trustworthy
                # connection. Raised out of the thread so that the relay
                # exits instead of retrying forever while looking alive.
                abandoned = self._abandon(held)
                self._log.critical(
                    "durable queue is poisoned; the relay must restart",
                    extra={"abandoned": abandoned, "error": repr(failure)},
                )
                raise QueuePoisonedError(str(failure)) from failure
            if self._stop.is_set():
                # Shutting down with a disk that refuses writes. The held
                # batch and whatever is still waiting in intake never got a
                # sequence number, so they are intake drops by §11's
                # definition; counting them is all that can still be done.
                # close() makes a last attempt to persist the count.
                abandoned = self._abandon(held)
                self._log.error(
                    "stopping with an unwritable queue; datagrams abandoned",
                    extra={"abandoned": abandoned, "error": repr(failure)},
                )
                return
            self._log.error(
                "durable queue write failed; holding the batch and retrying",
                extra={
                    "error": repr(failure),
                    "held_datagrams": len(held),
                    "intake_backlog": self._intake.qsize(),
                    "retry_in_s": round(backoff_s, 2),
                },
            )
            self._wait_counting_drops(backoff_s)
            backoff_s = min(backoff_s * BACKOFF_FACTOR, WRITER_BACKOFF_MAX_S)

    def _abandon(self, held: list[tuple[int, bytes]]) -> int:
        """Count what the writer will never store as intake drops.

        When stopping, waits for the intake thread first: it only notices the
        stop at its next receive timeout, and anything it hands off before
        then would otherwise land in a queue nobody drains, uncounted.
        """
        if self._stop.is_set():
            for thread in self._threads:
                if thread.name == INTAKE_THREAD_NAME:
                    thread.join(timeout=INTAKE_JOIN_TIMEOUT_S)
        abandoned = len(held) + self._drain_intake()
        self._queue.count_intake_drops(abandoned)
        # Drops intake made while it was still running.
        self._transfer_pending_drops()
        return abandoned

    def _drain_intake(self) -> int:
        drained = 0
        while True:
            try:
                self._intake.get_nowait()
            except queue_module.Empty:
                return drained
            drained += 1

    def _wait_counting_drops(self, delay_s: float) -> None:
        """Sleep before a retry, still moving intake drops into the counter.

        The drops are the Gateway's evidence that the outage is costing data,
        and a status sent during a multi-second backoff must already carry
        them. Memory only: the disk has just refused a write, and the next
        commit that succeeds persists the total.
        """
        deadline = time.monotonic() + delay_s
        while not self._stop.is_set():
            remaining_s = deadline - time.monotonic()
            if remaining_s <= 0:
                break
            self._stop.wait(min(remaining_s, WRITER_DROP_FLUSH_INTERVAL_S))
            self._beat()
            self._transfer_pending_drops()

    def _beat(self) -> None:
        """Mark the writer as making progress. See `writer_stalled`."""
        with self._counters_lock:
            self._writer_heartbeat_monotonic = time.monotonic()

    @property
    def writer_stalled(self) -> bool:
        """True when a running writer has not completed a pass for too long.

        A write hung in fsync neither fails nor finishes: the thread is alive,
        no error is raised, and without this the relay would report healthy
        storage while dropping everything. A pass normally takes one batch
        interval plus one commit, and a backoff beats every
        WRITER_DROP_FLUSH_INTERVAL_S, so only a blocked call can exceed the
        limit.
        """
        if not self._threads or self._stop.is_set() or not self.writer_alive:
            return False
        with self._counters_lock:
            beat = self._writer_heartbeat_monotonic
        return time.monotonic() - beat > self._config.writer_stall_timeout_s

    def _run_writer(self) -> None:
        try:
            self._writer_loop()
        except QueuePoisonedError:
            # Already logged where it was raised. The thread ends and the
            # watchdog stops the relay.
            return
        except BaseException as error:
            # Storage errors are handled inside the loop; reaching here is a
            # bug. Logged in the service's own format rather than as a bare
            # thread traceback, and the thread ends: the uplink's watchdog
            # sees that and stops the relay.
            self._log.critical(
                "durable queue writer crashed", extra={"error": repr(error)}
            )

    def _set_storage_ok(self, value: bool) -> bool:
        """Record storage health. Returns True if it changed."""
        with self._counters_lock:
            changed = self._storage_ok != value
            self._storage_ok = value
        return changed

    @property
    def storage_ok(self) -> bool:
        """False while the durable queue refuses writes or a write hangs."""
        if self._queue.poisoned or self.writer_stalled:
            return False
        with self._counters_lock:
            return self._storage_ok

    @property
    def writer_alive(self) -> bool:
        """Whether the writer thread is running, once intake has started."""
        return any(
            thread.name == WRITER_THREAD_NAME and thread.is_alive()
            for thread in self._threads
        )

    def _transfer_pending_drops(self) -> int:
        """Move intake drops into the queue's counter. Returns how many.

        Under `_counters_lock`, which `status` also takes to read both
        figures, so no status can see the drops in neither place or in both:
        either would make the Gateway see the counter fall and rise again and
        report the same loss twice. The queue's side is memory only and never
        waits for a write.
        """
        with self._counters_lock:
            drops = self._pending_intake_drops
            self._pending_intake_drops = 0
            self._queue.count_intake_drops(drops)
        return drops

    def start_intake(self) -> ReceiveOnlyUDPSocket:
        udp = ReceiveOnlyUDPSocket(self._config.bind_host, self._config.bind_port)
        self._log.info(
            "listening for forwarded MAVLink",
            extra={"host": self._config.bind_host, "port": self._config.bind_port},
        )
        for target, name in (
            (lambda: self._intake_loop(udp), INTAKE_THREAD_NAME),
            (self._run_writer, WRITER_THREAD_NAME),
        ):
            thread = threading.Thread(target=target, name=name, daemon=True)
            thread.start()
            self._threads.append(thread)
        return udp

    def stop(self) -> None:
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=5.0)

    # --- status -----------------------------------------------------------

    def _last_datagram_age_ms(self) -> int | None:
        with self._counters_lock:
            last = self._last_datagram_monotonic
        if last is None:
            return None
        return int((time.monotonic() - last) * 1000)

    def _status_message(self) -> dict[str, Any]:
        """Built from cached counters only; runs on the event loop.

        Nothing here may touch SQLite. The writer holds the queue's lock
        across a FULL-synchronous commit, and a status queued behind that
        fsync is a status the Gateway may count as missed (S-03).
        """
        monotonic_ns, utc_ns = _now_pair()
        with self._counters_lock:
            # Read together with the drops the writer has not yet collected:
            # a hung writer never collects them, and they are the Gateway's
            # evidence that data is being lost.
            stats = self._queue.stats()
            dropped_intake_total = (
                stats.dropped_intake_total + self._pending_intake_drops
            )
        return {
            "type": "status",
            "queue_depth": stats.depth,
            "queue_bytes": stats.total_bytes,
            "dropped_intake_total": dropped_intake_total,
            "dropped_cap_total": stats.dropped_cap_total,
            "last_datagram_age_ms": self._last_datagram_age_ms(),
            "storage_ok": self.storage_ok,
            "uptime_s": int(time.monotonic() - self._started_monotonic),
            "monotonic_ns": monotonic_ns,
            "utc_ns": utc_ns,
        }

    def _hello_message(self) -> dict[str, Any]:
        """Reads SQLite, so it is called off the event loop (S-03)."""
        monotonic_ns, utc_ns = _now_pair()
        return {
            "type": "hello",
            "station_id": self._config.station_id,
            "epoch": self._queue.epoch,
            "relay_version": RELAY_VERSION,
            "protocol_version": PROTOCOL_VERSION,
            "oldest_seq_held": self._queue.oldest_seq_held,
            "newest_seq_held": self._queue.newest_seq_held,
            "monotonic_ns": monotonic_ns,
            "utc_ns": utc_ns,
        }

    # --- uplink -----------------------------------------------------------

    def _check_writer(self) -> None:
        """Raise if intake has started and its writer thread is gone.

        Without a writer nothing reaches disk, intake fills and drops every
        datagram, and a relay that kept sending `status` would be describing a
        station that no longer exists. Stopping is the honest answer: the
        Gateway then sees `unreachable`, and the restart shows as `uptime_s`
        going backwards (relay-v1 §11 loss #4).
        """
        if self._threads and not self._stop.is_set() and not self.writer_alive:
            raise WriterDiedError("the durable queue writer thread has stopped")

    async def _watch_writer(self) -> None:
        """Stop the relay if the writer dies; report it if the writer hangs.

        A hang is reported, not treated as death. Exiting would discard the
        batch the writer holds and everything waiting in intake, and the
        restarted relay would block on the same disk when it opens the queue.
        A stalled fsync can also clear by itself (a sleeping USB disk, an
        antivirus scan). Meanwhile nothing is hidden: `status` keeps flowing
        with `storage_ok: false`, and the drops intake is making still count
        in `dropped_intake_total`, which the Gateway already treats as loss.
        """
        if not self._threads:
            return
        stalled = False
        while not self._stop.is_set():
            self._check_writer()
            now_stalled = self.writer_stalled
            if now_stalled != stalled:
                stalled = now_stalled
                if stalled:
                    self._log.error(
                        "durable queue writer has stalled; reporting storage not ok",
                        extra={
                            "stall_timeout_s": self._config.writer_stall_timeout_s,
                            "intake_backlog": self._intake.qsize(),
                        },
                    )
                else:
                    self._log.info("durable queue writer is making progress again")
            await asyncio.sleep(WRITER_WATCH_INTERVAL_S)

    async def run_uplink(self) -> None:
        """Connect, ship, reconnect. Runs until cancelled.

        Raises WriterDiedError if the writer thread stops while intake is
        running, or QueuePoisonedError if the queue can no longer be trusted;
        the process must not carry on as if it were healthy.
        """
        try:
            async with asyncio.TaskGroup() as tasks:
                tasks.create_task(self._watch_writer())
                tasks.create_task(self._uplink_loop())
        except BaseExceptionGroup as group:
            failure = _first_exception(group)
            if isinstance(failure, (WriterDiedError, QueuePoisonedError)):
                self._log.critical(
                    "durable queue writer stopped; shutting the relay down",
                    extra={"error": str(failure)},
                )
            if isinstance(failure, Exception):
                raise failure from None
            raise

    async def _uplink_loop(self) -> None:
        backoff = BACKOFF_INITIAL_S
        while not self._stop.is_set():
            try:
                await self._session()
                backoff = BACKOFF_INITIAL_S
            except FatalAuthError:
                # Retrying a rejected credential just floods the log.
                self._log.error("token rejected by the Gateway; not retrying")
                raise
            except (WriterDiedError, QueuePoisonedError):
                # Reconnecting cannot help either: the sender would fail on
                # the same queue every time.
                raise
            except asyncio.CancelledError:
                raise
            except (OSError, ProtocolError, websockets.WebSocketException) as error:
                self._log.warning(
                    "uplink session ended",
                    extra={"error": str(error), "retry_in_s": round(backoff, 2)},
                )
            except Exception as error:
                self._log.error(
                    "unexpected uplink failure",
                    extra={"error": repr(error), "retry_in_s": round(backoff, 2)},
                )

            # Jitter matters once more than one station exists: without it a
            # Gateway restart brings them all back at the same instant, each
            # draining a backlog.
            #
            # The jittered delay is itself capped. relay-v1 §12 says the
            # backoff is capped at 10 s, and multiplying a 10 s backoff by a
            # factor of up to 1.5 sleeps for 15 s - which is not what the
            # document says, and is a fifteen-second hole in recovery nobody
            # budgeted for.
            await asyncio.sleep(min(backoff * (0.5 + random.random()), BACKOFF_MAX_S))
            backoff = min(backoff * BACKOFF_FACTOR, BACKOFF_MAX_S)

    def _ssl_context(self) -> ssl.SSLContext | None:
        """Trust settings for the uplink, or None for a loopback ws:// link."""
        if not self._config.uses_tls:
            return None
        if self._config.ca_path is not None:
            # A development CA, per the P1-01 hardware runbook. Certificate
            # verification stays on: the point of the CA is to keep it on.
            return ssl.create_default_context(cafile=str(self._config.ca_path))
        return ssl.create_default_context()

    async def _session(self) -> None:
        url = str(self._config.gateway_url)
        if not self._config.uses_tls:
            # The configuration validator has already established that this is
            # loopback, so nothing is crossing a network.
            self._log.info("uplink is plaintext loopback", extra={"url": url})

        try:
            connection = await websockets.connect(
                url,
                additional_headers={"Authorization": f"Bearer {self._token}"},
                ssl=self._ssl_context(),
                # A half-open uplink is only detectable from missing pongs.
                # These three decide how long that takes; see RelayConfig.
                ping_interval=self._config.uplink_ping_interval_s,
                ping_timeout=self._config.uplink_ping_timeout_s,
                close_timeout=self._config.uplink_close_timeout_s,
            )
        except websockets.InvalidStatus as error:
            if error.response.status_code in (401, 403):
                raise FatalAuthError(str(error)) from error
            raise

        async with connection:
            resume_from = await self._handshake(connection)
            self._log.info("uplink established", extra={"resume_from_seq": resume_from})

            try:
                async with asyncio.TaskGroup() as tasks:
                    tasks.create_task(self._send_loop(connection, resume_from))
                    tasks.create_task(self._status_loop(connection))
                    tasks.create_task(self._receive_loop(connection))
            except BaseExceptionGroup as group:
                failure = _first_exception(group)
                if isinstance(failure, Exception):
                    raise failure from None
                raise

    def _held_range(self) -> tuple[int, int]:
        return self._queue.oldest_seq_held, self._queue.newest_seq_held

    async def _handshake(self, connection: ClientConnection) -> int:
        # Both reads query SQLite under the lock the writer holds while it
        # syncs, so neither may run on the event loop.
        hello = await asyncio.to_thread(self._hello_message)
        await connection.send(json.dumps(hello))
        welcome = json.loads(await connection.recv())
        if welcome.get("type") != "welcome":
            raise ProtocolError(f"expected welcome, got {welcome.get('type')!r}")

        resume_from = int(welcome["resume_from_seq"])
        oldest, newest = await asyncio.to_thread(self._held_range)

        # relay-v1 §11: the server claims records we never sent. Not a gap -
        # the two ends disagree about what they are discussing, and rebasing
        # onto their number would write records whose sequence means something
        # different at each end.
        if resume_from > newest + 1:
            raise ProtocolError(
                f"server resumed from {resume_from} but this epoch has never "
                f"produced past {newest}"
            )

        # relay-v1 §11: the cap already discarded what the server wants.
        if resume_from < oldest:
            await connection.send(
                json.dumps(
                    {
                        "type": "gap",
                        "epoch": self._queue.epoch,
                        "from_seq": resume_from,
                        "to_seq": oldest,
                        "reason": "queue_cap",
                    }
                )
            )
            self._log.warning(
                "reporting an unrecoverable gap",
                extra={"from_seq": resume_from, "to_seq": oldest},
            )
            resume_from = oldest

        return resume_from

    async def _send_loop(self, connection: ClientConnection, resume_from: int) -> None:
        next_seq = resume_from
        while True:
            records: list[Record] = await asyncio.to_thread(
                self._queue.read_from, next_seq, max_bytes=BATCH_MAX_BYTES
            )
            if not records:
                await asyncio.sleep(BATCH_INTERVAL_S)
                continue

            await connection.send(encode_records(records))
            next_seq = records[-1].seq + 1

    async def _status_loop(self, connection: ClientConnection) -> None:
        while True:
            # Checked here as well as by the watchdog, so that not one more
            # status leaves after the writer has gone.
            self._check_writer()
            await connection.send(json.dumps(self._status_message()))
            await asyncio.sleep(STATUS_INTERVAL_S)

    async def _receive_loop(self, connection: ClientConnection) -> None:
        async for raw in connection:
            if isinstance(raw, bytes):
                # The Gateway has nothing binary to say to us.
                continue
            message = json.loads(raw)
            if message.get("type") != "ack":
                # Unknown types are ignored, not rejected (relay-v1 §2).
                continue
            if message.get("epoch") != self._queue.epoch:
                # A late ack from a previous epoch must not delete records it
                # does not describe (relay-v1 §7).
                continue
            deleted = await asyncio.to_thread(
                self._queue.acknowledge, int(message["seq"])
            )
            if deleted:
                self._log.debug(
                    "records acknowledged",
                    extra={"through_seq": message["seq"], "deleted": deleted},
                )

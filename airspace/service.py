"""The airspace monitor as a service: bus in, alerts out, every alert audited.

Subscribes to the Gateway's `telemetry.*`, feeds `AirspaceMonitor`, and for
each alert raised or cleared:

- publishes `alert.<key>` with `state` "raised" or "cleared" (a clear also
  says why: `reason` "resolved" or "stale"), which the console shows (P6-03),
  and republishes each active alert every tick with `state` "active", so the
  numbers a console shows are current;
- appends an `events` row in the relational database, so an incident can be
  reconstructed from the audit log (P2-06) and not only from whoever was
  watching. The rows go through a bounded queue and a background writer
  (S-13), so the database is off the telemetry path; an overflow is counted
  and logged, never silent.

A publish or audit failure is logged and never stops the monitor: the next
message must still be evaluated.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine

from airspace.monitor import AirspaceMonitor, Alert, Change, ClearReason
from common import get_logger
from common.terrain import cell_name

_log = get_logger(__name__)

ALERT_SUBJECT = "alert"
ACTOR_TYPE = "airspace"


class Bus(Protocol):
    async def publish(self, subject: str, payload: bytes) -> None: ...


class TerrainTiles(Protocol):
    """`common.terrain.Terrain`'s tile cache, as the service warms it."""

    def is_loaded(self, lat_deg: float, lon_deg: float) -> bool: ...
    def load(self, lat_deg: float, lon_deg: float) -> None: ...


class AuditLog(Protocol):
    async def record(
        self,
        alert: Alert,
        state: str,
        *,
        at: datetime,
        reason: ClearReason | None = None,
    ) -> None: ...


def alert_subject(alert: Alert) -> str:
    # NATS tokens are separated by dots; the key has none.
    return f"{ALERT_SUBJECT}.{alert.key}"


def encode_alert(
    alert: Alert, state: str, *, reason: ClearReason | None = None
) -> bytes:
    """The bus payload. `reason` is present only when `state` is "cleared":
    "resolved" or "stale" (`airspace.monitor.ClearReason`)."""
    body: dict[str, Any] = {"state": state}
    if reason is not None:
        body["reason"] = reason.value
    return json.dumps({**body, **alert.as_dict()}).encode("utf-8")


@dataclass
class EventsAuditLog:
    """One `events` row per aircraft per alert transition."""

    engine: AsyncEngine

    async def record(
        self,
        alert: Alert,
        state: str,
        *,
        at: datetime | None = None,
        reason: ClearReason | None = None,
    ) -> None:
        """One row per aircraft, stamped `at`: when the transition happened,
        not when the background writer got to it (S-13). The service always
        passes it; a direct caller without one gets the write time."""
        if at is None:
            at = datetime.now(UTC)
        payload = alert.as_dict()
        if reason is not None:
            payload["reason"] = reason.value
        async with self.engine.begin() as connection:
            for drone_id in alert.drone_ids:
                await connection.execute(
                    sa.text(
                        "INSERT INTO events (ts, actor_type, entity_type, "
                        " entity_id, event_type, payload) "
                        "VALUES (:ts, :actor, 'drone', :drone_id, :event_type, "
                        " CAST(:payload AS jsonb))"
                    ),
                    {
                        "ts": at,
                        "actor": ACTOR_TYPE,
                        "drone_id": str(drone_id),
                        "event_type": f"airspace_alert_{state}",
                        "payload": json.dumps(payload),
                    },
                )


@dataclass(frozen=True, slots=True)
class AuditEntry:
    alert: Alert
    state: str
    reason: ClearReason | None
    # When the transition happened, taken as the row is queued.
    at: datetime


@dataclass
class AirspaceService:
    monitor: AirspaceMonitor
    bus: Bus
    audit: AuditLog | None = None
    # Wall clock, on the same epoch as telemetry's `ts` (S-11): the monitor
    # compares the two to tell live telemetry from a replayed backlog.
    clock: Callable[[], float] = time.time
    # S-13. Audit rows are written by a background task from a bounded
    # queue, so a slow database does not hold up the telemetry path; the
    # publish stays inline, since the console is the one that must be
    # current. A full queue drops the row, counted and logged, never
    # silently. The default matches `airspace.config.AirspaceSettings`.
    audit_queue_size: int = 1000
    # S-13. The monitor's terrain, so the tile under a message can be read
    # in a worker thread before `observe` asks for it on the loop: a tile is
    # about 26 MB, and the old synchronous read stalled every message behind
    # it. Once cached, `observe` answers from memory.
    tiles: TerrainTiles | None = None
    # How often a cell whose tile cannot be read is logged; the count in
    # between goes on the next line. The default matches the settings.
    tile_log_every_s: float = 60.0
    _tile_failure_logged: dict[str, tuple[float | None, int]] = field(
        default_factory=dict, init=False
    )
    # How often the tick logs the running totals (rejected telemetry, check
    # failures, audit queue), so a backlog or a broken tile shows up in a
    # log that is otherwise quiet. This is a log cadence, not policy.
    status_every_s: float = 60.0
    # How long `close()` waits for queued audit rows before abandoning
    # them, counted and logged. The default matches the settings.
    close_timeout_s: float = 5.0
    audit_overflow: int = field(default=0, init=False)
    # Terrain tiles that could not be read; each leaves one message's
    # height limit unevaluated.
    tile_failures: int = field(default=0, init=False)
    # Rows the writer tried and the database refused (logged each time).
    audit_failures: int = field(default=0, init=False)
    # Rows never attempted: still queued, or in flight, when the writer
    # was stopped.
    audit_abandoned: int = field(default=0, init=False)
    _status_logged_at_s: float | None = field(default=None, init=False)
    _audit_queue: asyncio.Queue[AuditEntry] = field(init=False)
    _audit_writer: asyncio.Task[None] | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        if self.audit_queue_size < 1:
            raise ValueError("audit_queue_size must be at least 1")
        self._audit_queue = asyncio.Queue(maxsize=self.audit_queue_size)

    @property
    def audit_pending(self) -> int:
        return self._audit_queue.qsize()

    async def flush_audit(self) -> None:
        """Wait until every queued audit row has been attempted."""
        await self._audit_queue.join()

    async def close(self) -> None:
        """Write what is queued, for at most `close_timeout_s`, then stop
        the writer. Rows still queued are abandoned, counted and logged;
        the one in flight, if any, is counted by the writer as it stops."""
        try:
            await asyncio.wait_for(self.flush_audit(), timeout=self.close_timeout_s)
        except TimeoutError:
            left = self._audit_queue.qsize()
            self.audit_abandoned += left
            _log.error(
                "audit rows abandoned at close: the database did not answer in time",
                extra={
                    "close_timeout_s": self.close_timeout_s,
                    "abandoned_now": left,
                    "audit_abandoned": self.audit_abandoned,
                },
            )
        if self._audit_writer is not None:
            self._audit_writer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._audit_writer
            self._audit_writer = None

    async def on_telemetry(self, payload: bytes) -> None:
        try:
            message: dict[str, Any] = json.loads(payload)
            position = _position(message)
            height_available = True
            if position is not None and self.tiles is not None:
                height_available = await self._load_tile(message, *position)
            change = self.monitor.observe(
                message, now_s=self.clock(), height_available=height_available
            )
        except (ValueError, KeyError, TypeError) as error:
            _log.warning("unusable telemetry message", extra={"error": repr(error)})
            return
        await self._emit(change)

    async def _load_tile(
        self, message: dict[str, Any], lat_deg: float, lon_deg: float
    ) -> bool:
        """Whether the ground under the message is in memory. False means
        the tile could not be read: the monitor then leaves the height
        limit unevaluated for this message rather than reading the file
        again, synchronously, on the loop."""
        assert self.tiles is not None
        if self.tiles.is_loaded(lat_deg, lon_deg):
            return True
        try:
            await asyncio.to_thread(self.tiles.load, lat_deg, lon_deg)
        except Exception:
            self.tile_failures += 1
            self._log_tile_failure(message, lat_deg, lon_deg)
            return False
        return True

    def _log_tile_failure(
        self, message: dict[str, Any], lat_deg: float, lon_deg: float
    ) -> None:
        """One line, with its traceback, per cell per `tile_log_every_s`,
        carrying the count suppressed since the last one. Found in SITL: a
        missing tile logged a full traceback on every message, twelve a
        second, which is how an operator stops reading the log."""
        cell = cell_name(lat_deg, lon_deg)
        now_s = self.clock()
        logged_at_s, suppressed = self._tile_failure_logged.get(cell, (None, 0))
        if logged_at_s is not None and now_s - logged_at_s < self.tile_log_every_s:
            self._tile_failure_logged[cell] = (logged_at_s, suppressed + 1)
            return
        self._tile_failure_logged[cell] = (now_s, 0)
        _log.exception(
            "could not load the terrain tile; height not evaluated",
            extra={
                "cell": cell,
                "drone_id": str(message.get("drone_id")),
                "lat_deg": lat_deg,
                "lon_deg": lon_deg,
                "suppressed": suppressed,
                "tile_failures": self.tile_failures,
            },
        )

    def status(self) -> dict[str, int]:
        """The running totals, as the status line logs them."""
        return {
            "tracked": self.monitor.tracked,
            "active_alerts": len(self.monitor.active),
            "rejected_backlog": self.monitor.rejected_backlog,
            "rejected_late": self.monitor.rejected_late,
            "rejected_out_of_order": self.monitor.rejected_out_of_order,
            "without_receive_time": self.monitor.without_receive_time,
            "without_capture_time": self.monitor.without_capture_time,
            "check_failures": self.monitor.check_failures,
            "zone_checks_not_evaluated": self.monitor.zone_checks_not_evaluated,
            "tile_failures": self.tile_failures,
            "audit_pending": self.audit_pending,
            "audit_overflow": self.audit_overflow,
            "audit_failures": self.audit_failures,
            "audit_abandoned": self.audit_abandoned,
        }

    def _log_status(self, now_s: float) -> None:
        if (
            self._status_logged_at_s is not None
            and now_s - self._status_logged_at_s < self.status_every_s
        ):
            return
        self._status_logged_at_s = now_s
        _log.info("airspace monitor status", extra=self.status())

    async def on_tick(self) -> None:
        now_s = self.clock()
        await self._emit(self.monitor.tick(now_s=now_s))
        self._log_status(now_s)
        # Refresh what is still active, on the bus only. An alert's numbers
        # change as the pair closes; a console showing "closest 2.8 m in
        # 57 s" from the moment it was raised is wrong a second later. Not
        # audited: the log records transitions, not a heartbeat.
        for alert in self.monitor.active:
            await _guard(
                "publish",
                self.bus.publish(alert_subject(alert), encode_alert(alert, "active")),
            )

    async def _emit(self, change: Change) -> None:
        for alert in change.raised:
            await self._send(alert, "raised")
        for cleared in change.cleared:
            await self._send(cleared.alert, "cleared", reason=cleared.reason)

    async def _send(
        self, alert: Alert, state: str, *, reason: ClearReason | None = None
    ) -> None:
        _log.info(
            "airspace alert",
            extra={
                "state": state,
                "reason": None if reason is None else reason.value,
                "key": alert.key,
                "kind": alert.kind.value,
                "severity": alert.severity.value,
                "drone_ids": [str(d) for d in alert.drone_ids],
            },
        )
        await _guard(
            "publish",
            self.bus.publish(
                alert_subject(alert), encode_alert(alert, state, reason=reason)
            ),
        )
        if self.audit is not None:
            at = datetime.fromtimestamp(self.clock(), tz=UTC)
            self._enqueue_audit(AuditEntry(alert, state, reason, at))

    def _enqueue_audit(self, entry: AuditEntry) -> None:
        # Started on first use rather than in a `start()` a caller could
        # forget: a forgotten start would queue rows for ever and write none.
        if self._audit_writer is None or self._audit_writer.done():
            self._audit_writer = asyncio.create_task(
                self._write_audit(), name="airspace-audit-writer"
            )
        try:
            self._audit_queue.put_nowait(entry)
        except asyncio.QueueFull:
            self.audit_overflow += 1
            _log.error(
                "audit row dropped: the audit queue is full",
                extra={
                    **_entry_context(entry),
                    "audit_queue_size": self.audit_queue_size,
                    "audit_overflow": self.audit_overflow,
                },
            )

    async def _write_audit(self) -> None:
        assert self.audit is not None
        while True:
            entry = await self._audit_queue.get()
            try:
                written = await _guard(
                    "audit",
                    self.audit.record(
                        entry.alert, entry.state, at=entry.at, reason=entry.reason
                    ),
                )
            except asyncio.CancelledError:
                # Stopped mid-write: the row is lost, and is not marked done,
                # so nothing can take a join() for a promise it was written.
                self.audit_abandoned += 1
                _log.error(
                    "audit row abandoned: the writer was stopped mid-write",
                    extra={
                        **_entry_context(entry),
                        "audit_abandoned": self.audit_abandoned,
                    },
                )
                raise
            if not written:
                self.audit_failures += 1
                _log.error(
                    "audit row not written",
                    extra={
                        **_entry_context(entry),
                        "audit_failures": self.audit_failures,
                    },
                )
            self._audit_queue.task_done()


def _entry_context(entry: AuditEntry) -> dict[str, Any]:
    return {
        "key": entry.alert.key,
        "state": entry.state,
        "reason": None if entry.reason is None else entry.reason.value,
        "at": entry.at.isoformat(),
    }


def _position(message: dict[str, Any]) -> tuple[float, float] | None:
    """The message's position, or None when it has none or it is not finite
    (the monitor refuses that one itself, S-12)."""
    lat, lon = message.get("lat_deg"), message.get("lon_deg")
    if lat is None or lon is None:
        return None
    lat_deg, lon_deg = float(lat), float(lon)
    if not (math.isfinite(lat_deg) and math.isfinite(lon_deg)):
        return None
    return lat_deg, lon_deg


async def run_ticker(
    service: AirspaceService,
    *,
    stop: asyncio.Event,
    tick_s: float,
    refresh_every_s: float,
    refresh: Callable[[], Awaitable[None]],
) -> None:
    """Tick the service every `tick_s` until `stop`, and call `refresh` (the
    zones, the policy and the height limit from the database) every
    `refresh_every_s`. Neither a failing tick nor a failing refresh ends the
    loop (S-13): the ticker is what clears stale alerts, and a task that died
    silently would leave every one of them showing for ever. Both are logged
    with their traceback; a refresh failure keeps what is loaded."""
    since_refresh_s = 0.0
    while not stop.is_set():
        await asyncio.sleep(tick_s)
        try:
            await service.on_tick()
        except Exception:
            _log.exception("tick failed; the ticker continues")
        since_refresh_s += tick_s
        if since_refresh_s >= refresh_every_s:
            since_refresh_s = 0.0
            try:
                await refresh()
            except Exception:
                _log.exception(
                    "could not reload the zones, the policy or the height "
                    "limit; keeping what is loaded"
                )


async def _guard(what: str, action: Awaitable[None]) -> bool:
    """Run the delivery step; False, and a log line, when it raised."""
    try:
        await action
    except Exception as error:
        _log.error(
            "could not deliver an airspace alert",
            extra={"step": what, "error": repr(error)},
        )
        return False
    return True

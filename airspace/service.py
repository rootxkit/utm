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
from typing import Any, Protocol

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine

from airspace.monitor import AirspaceMonitor, Alert, Change, ClearReason
from common import get_logger

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
        self, alert: Alert, state: str, *, reason: ClearReason | None = None
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
        self, alert: Alert, state: str, *, reason: ClearReason | None = None
    ) -> None:
        payload = alert.as_dict()
        if reason is not None:
            payload["reason"] = reason.value
        async with self.engine.begin() as connection:
            for drone_id in alert.drone_ids:
                await connection.execute(
                    sa.text(
                        "INSERT INTO events (actor_type, entity_type, entity_id, "
                        " event_type, payload) "
                        "VALUES (:actor, 'drone', :drone_id, :event_type, "
                        " CAST(:payload AS jsonb))"
                    ),
                    {
                        "actor": ACTOR_TYPE,
                        "drone_id": str(drone_id),
                        "event_type": f"airspace_alert_{state}",
                        "payload": json.dumps(payload),
                    },
                )


AuditEntry = tuple[Alert, str, ClearReason | None]


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
    audit_overflow: int = field(default=0, init=False)
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
        """Write what is queued, then stop the writer."""
        await self.flush_audit()
        if self._audit_writer is not None:
            self._audit_writer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._audit_writer
            self._audit_writer = None

    async def on_telemetry(self, payload: bytes) -> None:
        try:
            message: dict[str, Any] = json.loads(payload)
            position = _position(message)
            if position is not None and self.tiles is not None:
                await self._load_tile(message, *position)
            change = self.monitor.observe(message, now_s=self.clock())
        except (ValueError, KeyError, TypeError) as error:
            _log.warning("unusable telemetry message", extra={"error": repr(error)})
            return
        await self._emit(change)

    async def _load_tile(
        self, message: dict[str, Any], lat_deg: float, lon_deg: float
    ) -> None:
        assert self.tiles is not None
        if self.tiles.is_loaded(lat_deg, lon_deg):
            return
        try:
            await asyncio.to_thread(self.tiles.load, lat_deg, lon_deg)
        except Exception:
            # The monitor's height check will meet the same error and log
            # it (S-12); this says the read was attempted off the loop.
            _log.exception(
                "could not load the terrain tile",
                extra={
                    "drone_id": str(message.get("drone_id")),
                    "lat_deg": lat_deg,
                    "lon_deg": lon_deg,
                },
            )

    async def on_tick(self) -> None:
        await self._emit(self.monitor.tick(now_s=self.clock()))
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
            self._enqueue_audit((alert, state, reason))

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
            alert, state, reason = entry
            self.audit_overflow += 1
            _log.error(
                "audit row dropped: the audit queue is full",
                extra={
                    "key": alert.key,
                    "state": state,
                    "reason": None if reason is None else reason.value,
                    "audit_queue_size": self.audit_queue_size,
                    "audit_overflow": self.audit_overflow,
                },
            )

    async def _write_audit(self) -> None:
        assert self.audit is not None
        while True:
            alert, state, reason = await self._audit_queue.get()
            try:
                await _guard("audit", self.audit.record(alert, state, reason=reason))
            finally:
                self._audit_queue.task_done()


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


async def _guard(what: str, action: Awaitable[None]) -> None:
    try:
        await action
    except Exception as error:
        _log.error(
            "could not deliver an airspace alert",
            extra={"step": what, "error": repr(error)},
        )

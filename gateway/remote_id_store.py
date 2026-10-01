"""Keeping Remote ID observations: `remote_id_observations`. P1-15.

The ingest publishes an observation to the bus and hands the same
observation here. Rows are written in batches, on a clock, off the datagram
path: a slow database must not delay what the console and the airspace
monitor see.

## When the database is down

There is no raw archive behind Remote ID, so rows that are never written are
gone for good. They are therefore kept and retried, up to `max_pending` rows
(about ten minutes of a busy sky). Past that the oldest are dropped, counted,
and logged as a loss. Losing data without saying so would be worse than
losing it.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from common import get_logger
from gateway.remote_id import ALT_SOURCE_PRESSURE, PRESSURE_ALTITUDE_MODEL

_log = get_logger(__name__)

DEFAULT_MAX_PENDING = 50_000
# While the database stays down, how often the failure is logged again. Every
# flush (twice a second) would bury everything else in the log.
FAILURE_LOG_INTERVAL_S = 60.0


@dataclass(frozen=True, slots=True)
class RemoteIdRow:
    aircraft_id: UUID
    ts: datetime
    receiver_id: str
    transmitter: str
    ua_id: str
    id_type: int
    ua_type: int | None
    status: int | None
    lat_deg: float | None
    lon_deg: float | None
    alt_hae_m: float | None
    alt_amsl_m: float | None
    geoid_model: str | None
    alt_above_takeoff_m: float | None
    track_deg: float | None
    vx_ms: float | None
    vy_ms: float | None
    vz_ms: float | None
    groundspeed_ms: float | None
    climb_ms: float | None
    operator_id: str | None
    operator_lat_deg: float | None
    operator_lon_deg: float | None
    rssi_dbm: float | None
    payload: bytes
    # Which of our aircraft the broadcast serial matched, if any (P1-15).
    matched_drone_id: UUID | None = None


def row_from_observation(
    observation: dict[str, Any],
    *,
    ts: datetime,
    payload: bytes,
    geoid_model: str | None,
    matched_drone_id: UUID | None = None,
) -> RemoteIdRow:
    """The row for an observation as `gateway/remote_id.py` builds it."""
    rid = observation["remote_id"]
    alt_amsl_m = observation["alt_amsl_m"]
    return RemoteIdRow(
        aircraft_id=UUID(observation["drone_id"]),
        ts=ts,
        receiver_id=observation["station_id"],
        transmitter=rid["transmitter"],
        ua_id=rid["ua_id"],
        id_type=int(rid["id_type"]),
        ua_type=None if rid["ua_type"] is None else int(rid["ua_type"]),
        status=None if rid["status"] is None else int(rid["status"]),
        lat_deg=observation["lat_deg"],
        lon_deg=observation["lon_deg"],
        alt_hae_m=observation["alt_hae_m"],
        alt_amsl_m=alt_amsl_m,
        geoid_model=_height_model(observation, geoid_model),
        alt_above_takeoff_m=observation["alt_above_home_m"],
        track_deg=observation["track_deg"],
        vx_ms=observation["vx_ms"],
        vy_ms=observation["vy_ms"],
        vz_ms=observation["vz_ms"],
        groundspeed_ms=observation["groundspeed_ms"],
        climb_ms=observation["climb_ms"],
        operator_id=rid["operator_id"],
        operator_lat_deg=rid["operator_lat_deg"],
        operator_lon_deg=rid["operator_lon_deg"],
        rssi_dbm=rid["rssi_dbm"],
        payload=payload,
        matched_drone_id=matched_drone_id,
    )


def _height_model(observation: dict[str, Any], geoid_model: str | None) -> str | None:
    """What produced the row's AMSL height; None when it has none.

    The geoid for a geodetic altitude. For a pressure altitude (S-33), the
    text `PRESSURE_ALTITUDE_MODEL`: the standard atmosphere it is referenced
    to, so the row says its height is not geodetic without a new column.
    Note that this puts a non-geoid in `geoid_model`: whoever reads the
    column must treat that text as "this AMSL height is a pressure altitude,
    vertical position unknown", never as a geoid model name. A dedicated
    `alt_source` column would need a telemetry migration.
    """
    if observation["alt_amsl_m"] is None:
        return None
    if observation.get("alt_source") == ALT_SOURCE_PRESSURE:
        return PRESSURE_ALTITUDE_MODEL
    return geoid_model


class RowWriter(Protocol):
    async def write(self, rows: list[RemoteIdRow]) -> None: ...


# ST_MakePoint takes longitude first (gateway/state_writer.py says why that
# matters). It is STRICT, so a missing coordinate makes the point NULL, not
# (0, 0).
_INSERT = sa.text(
    """
    INSERT INTO remote_id_observations (
        aircraft_id, ts, receiver_id, transmitter, ua_id, id_type, ua_type,
        status, geom, alt_hae_m, alt_amsl_m, geoid_model, alt_above_takeoff_m,
        track_deg, vx_ms, vy_ms, vz_ms, groundspeed_ms, climb_ms,
        operator_id, operator_geom, rssi_dbm, payload, matched_drone_id
    ) VALUES (
        :aircraft_id, :ts, :receiver_id, :transmitter, :ua_id, :id_type,
        :ua_type, :status,
        ST_SetSRID(ST_MakePoint(:lon_deg, :lat_deg), 4326),
        :alt_hae_m, :alt_amsl_m, :geoid_model, :alt_above_takeoff_m,
        :track_deg, :vx_ms, :vy_ms, :vz_ms, :groundspeed_ms, :climb_ms,
        :operator_id,
        ST_SetSRID(ST_MakePoint(:operator_lon_deg, :operator_lat_deg), 4326),
        :rssi_dbm, :payload, :matched_drone_id
    )
    ON CONFLICT (aircraft_id, ts, receiver_id) DO NOTHING
    """
)


@dataclass
class RemoteIdWriter:
    engine: AsyncEngine

    async def write(self, rows: list[RemoteIdRow]) -> None:
        if not rows:
            return
        async with self.engine.begin() as connection:
            await connection.execute(_INSERT, [_parameters(row) for row in rows])


def _parameters(row: RemoteIdRow) -> dict[str, object]:
    return {
        "aircraft_id": str(row.aircraft_id),
        "ts": row.ts,
        "receiver_id": row.receiver_id,
        "transmitter": row.transmitter,
        "ua_id": row.ua_id,
        "id_type": row.id_type,
        "ua_type": row.ua_type,
        "status": row.status,
        "lat_deg": row.lat_deg,
        "lon_deg": row.lon_deg,
        "alt_hae_m": row.alt_hae_m,
        "alt_amsl_m": row.alt_amsl_m,
        "geoid_model": row.geoid_model,
        "alt_above_takeoff_m": row.alt_above_takeoff_m,
        "track_deg": row.track_deg,
        "vx_ms": row.vx_ms,
        "vy_ms": row.vy_ms,
        "vz_ms": row.vz_ms,
        "groundspeed_ms": row.groundspeed_ms,
        "climb_ms": row.climb_ms,
        "operator_id": row.operator_id,
        "operator_lat_deg": row.operator_lat_deg,
        "operator_lon_deg": row.operator_lon_deg,
        "rssi_dbm": row.rssi_dbm,
        "payload": row.payload,
        "matched_drone_id": (
            None if row.matched_drone_id is None else str(row.matched_drone_id)
        ),
    }


@dataclass
class PendingRows:
    """Rows waiting to be written, and what could not be kept."""

    writer: RowWriter
    max_pending: int = DEFAULT_MAX_PENDING
    clock_s: Callable[[], float] = time.monotonic
    written: int = field(default=0, init=False)
    dropped: int = field(default=0, init=False)
    _rows: deque[RemoteIdRow] = field(default_factory=deque, init=False)
    _failing_since_s: float | None = field(default=None, init=False)
    _logged_at_s: float = field(default=0.0, init=False)

    def add(self, row: RemoteIdRow) -> None:
        if len(self._rows) >= self.max_pending:
            self._rows.popleft()
            self.dropped += 1
            if self.dropped == 1 or self.dropped % 1000 == 0:
                _log.error(
                    "remote id observations dropped: the database has been "
                    "unavailable longer than the pending limit",
                    extra={"dropped": self.dropped, "max_pending": self.max_pending},
                )
        self._rows.append(row)

    @property
    def pending(self) -> int:
        return len(self._rows)

    async def flush(self) -> None:
        """Write what is pending. On failure the rows stay for the next try."""
        if not self._rows:
            return
        batch = list(self._rows)
        self._rows.clear()
        try:
            await self.writer.write(batch)
        except (SQLAlchemyError, OSError) as error:
            self._failed(len(batch), error)
            # Back in front of anything that arrived meanwhile, in order,
            # and the limit applied again.
            arrived = list(self._rows)
            self._rows.clear()
            for row in batch + arrived:
                self.add(row)
            return
        self.written += len(batch)
        if self._failing_since_s is not None:
            _log.warning(
                "remote id observations stored again",
                extra={
                    "rows": len(batch),
                    "failing_for_s": round(self.clock_s() - self._failing_since_s, 1),
                    "dropped": self.dropped,
                },
            )
            self._failing_since_s = None

    def _failed(self, rows: int, error: Exception) -> None:
        now_s = self.clock_s()
        if self._failing_since_s is None:
            self._failing_since_s = now_s
        elif now_s - self._logged_at_s < FAILURE_LOG_INTERVAL_S:
            return
        self._logged_at_s = now_s
        _log.error(
            "could not store remote id observations; will retry",
            extra={
                "pending": rows,
                "failing_for_s": round(now_s - self._failing_since_s, 1),
                "error": repr(error),
            },
        )

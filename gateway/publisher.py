"""Publishing ingest output to NATS, for anything that wants it live.

P1-06. Three kinds of message, on three subject patterns:

    telemetry.{drone_id}    one per drone_state row
    station.{station_id}    link state, per relay-v1 §8 and spec §9
    events.{type}           losses, unclaimed sources, protocol faults

**The console subscribes to this; it never polls the hypertable.** A browser
refresh must not become a database query, and a map open on ten drones at 4 Hz
must not become forty queries a second. The database is where the flight record
lives; the bus is how the present gets to a screen.

## Station state is published, not inferred

Spec §9 is the requirement that came out of the half-open uplink work: the
Gateway declares a station unreachable after about 3 s, and the relay does not
give up for up to 25 s. For that window the relay is alive, receiving at full
rate and buffering correctly, and **nothing has been lost**.

A console that inferred station health from "telemetry stopped arriving" would
render that window as data loss. So the state is published explicitly, with
`data_lost` reserved for the three things that actually mean it - a `gap`, an
intake-drop delta, or a `uptime_s` reset - and `unreachable` carrying the fact
that the record completes on reconnect.

## Unclaimed sources are published too

An aircraft transmitting with no binding is the registration race described in
spec §7. It is recoverable, and it is also the operator's cue to create the
binding. Publishing it is what makes it actionable; leaving it in a log makes
it something discovered later.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from common import get_logger
from gateway.binding import Resolution
from gateway.drone_state import DroneStateRow
from gateway.parsing import SourceId
from gateway.station_state import LinkState, LossEvent

_log = get_logger(__name__)

TELEMETRY_SUBJECT = "telemetry"
STATION_SUBJECT = "station"
EVENTS_SUBJECT = "events"


class Bus(Protocol):
    """The part of a NATS connection this module uses.

    Narrow on purpose: a test double implements two methods rather than a
    client library, and nothing here can quietly start using JetStream.
    """

    async def publish(self, subject: str, payload: bytes) -> None: ...


def telemetry_subject(drone_id: UUID) -> str:
    return f"{TELEMETRY_SUBJECT}.{drone_id}"


def station_subject(station_id: str) -> str:
    return f"{STATION_SUBJECT}.{station_id}"


def event_subject(event_type: str) -> str:
    return f"{EVENTS_SUBJECT}.{event_type}"


def encode_row(
    row: DroneStateRow,
    label: str | None = None,
    link: dict[str, Any] | None = None,
    firmware: dict[str, Any] | None = None,
    *,
    rx_ts: datetime | None = None,
    backlog: bool = False,
    captured_at: datetime | None = None,
) -> dict[str, Any]:
    """A drone_state row as the console reads it.

    Field names match the database columns exactly, including
    `alt_above_home_m`. There is deliberately no `alt_agl_m` here either: the
    console must not be able to display a height above ground that nothing
    measured, and renaming the field on the way out would be the same silent
    error one layer further on.
    """
    return {
        "drone_id": str(row.drone_id),
        # The registry name, when the Gateway could read one. The console shows
        # this and keeps the id secondary: a pilot reads "SITL-01", not
        # "0b63df96". Carried per message rather than looked up by the console
        # so that the console stays a pure subscriber and a browser attaching
        # late is served entirely from the snapshot.
        "label": label,
        # P1-09: loss and heartbeat gaps over the last window, for the station
        # that delivered this row. None when not tracked, never zero: zero loss
        # is a measurement, and an absent one must not look like a good link.
        "link": link,
        # P1-11: the drone's latest recorded flight software. None when it has
        # never reported one, which the console shows as unknown.
        "firmware": firmware,
        "ts": row.ts.isoformat(),
        # S-11. `rx_ts` is when the Gateway received the batch, on its own
        # clock: one clock for every station, where `ts` is each relay's.
        # `backlog` says the record was queued on the relay before the
        # session that delivered it (relay-v1 §5, `newest_seq_held`): the
        # airspace monitor does not raise live alerts from it. Both are null
        # or false where the row did not come through a relay session.
        "rx_ts": None if rx_ts is None else rx_ts.isoformat(),
        # Where the row sits in time on the Gateway's clock: `rx_ts` less how
        # far behind its batch's newest record it was captured, so rows from
        # one large frame are not all "now". Null with `rx_ts`; equal to it
        # when the pipeline gave no finer placement.
        "captured_at": (
            None
            if rx_ts is None
            else (rx_ts if captured_at is None else captured_at).isoformat()
        ),
        "backlog": backlog,
        "station_id": row.station_id,
        "lat_deg": row.lat_deg,
        "lon_deg": row.lon_deg,
        "alt_amsl_m": row.alt_amsl_m,
        "alt_above_home_m": row.alt_above_home_m,
        "heading_deg": row.heading_deg,
        "vx_ms": row.vx_ms,
        "vy_ms": row.vy_ms,
        "vz_ms": row.vz_ms,
        "batt_pct": row.batt_pct,
        "batt_voltage_v": row.batt_voltage_v,
        "batt_consumed_wh": row.batt_consumed_wh,
        "mode": row.mode,
        "armed": row.armed,
        "gps_fix_type": row.gps_fix_type,
        "sat_count": row.sat_count,
        "groundspeed_ms": row.groundspeed_ms,
        "climb_ms": row.climb_ms,
    }


def encode_station(
    station_id: str,
    state: LinkState,
    *,
    last_datagram_age_ms: int | None,
    queue_depth: int | None,
    losses: list[LossEvent],
    lag_s: float | None = None,
) -> dict[str, Any]:
    """Station link state, with the §9 distinction made explicit.

    `data_is_lost` is a separate field from `state` rather than something the
    console has to infer from it. An `unreachable` station is almost certainly
    buffering and the record completes on reconnect; presenting that as loss is
    the specific mistake P6-03 says will teach a pilot to discount alerts.
    """
    return {
        "station_id": station_id,
        "state": state.value,
        # The console renders this sentence; it does not compose its own.
        "data_is_lost": state is LinkState.DATA_LOST,
        # P1-14: a lagging station is buffering too. Its backlog is safe at
        # the station; it is arriving more slowly than it is produced.
        "buffering": state in (LinkState.UNREACHABLE, LinkState.LAGGING),
        "last_datagram_age_ms": last_datagram_age_ms,
        "queue_depth": queue_depth,
        # How old the newest stored record is, in seconds. None before
        # anything is stored. Rounded: it is read by a person.
        "lag_s": None if lag_s is None else round(lag_s, 1),
        "losses": [
            {
                "kind": loss.kind.value,
                "from_seq": loss.from_seq,
                "to_seq": loss.to_seq,
                "datagram_count": loss.datagram_count,
                "detail": loss.detail,
            }
            for loss in losses
        ],
    }


def encode_unclaimed(
    station_id: str, resolution: Resolution, *, sysid: int, compid: int
) -> dict[str, Any]:
    """An aircraft nobody has bound. Actionable, so it is published."""
    return {
        "station_id": station_id,
        "sysid": sysid,
        "compid": compid,
        "reason": resolution.unclaimed_reason,
    }


@dataclass
class TelemetryPublisher:
    """Publishes ingest output. Owns no state beyond the connection."""

    bus: Bus

    async def publish_row(
        self,
        row: DroneStateRow,
        label: str | None = None,
        link: dict[str, Any] | None = None,
        firmware: dict[str, Any] | None = None,
        *,
        rx_ts: datetime | None = None,
        backlog: bool = False,
        captured_at: datetime | None = None,
    ) -> None:
        await self._send(
            telemetry_subject(row.drone_id),
            encode_row(
                row,
                label,
                link,
                firmware,
                rx_ts=rx_ts,
                backlog=backlog,
                captured_at=captured_at,
            ),
        )

    async def publish_rows(
        self,
        rows: list[DroneStateRow],
        labels: dict[UUID, str] | None = None,
        links: dict[UUID, dict[str, Any]] | None = None,
        firmware: dict[UUID, dict[str, Any]] | None = None,
        *,
        rx_ts: datetime | None = None,
        backlog: Sequence[bool] | None = None,
        captured_at: Sequence[datetime] | None = None,
    ) -> None:
        """`backlog` and `captured_at`, when given, are parallel to `rows`."""
        labels = labels or {}
        links = links or {}
        firmware = firmware or {}
        if backlog is not None and len(backlog) != len(rows):
            raise ValueError("backlog flags must be one per row")
        if captured_at is not None and len(captured_at) != len(rows):
            raise ValueError("captured_at must be one per row")
        for n, row in enumerate(rows):
            await self.publish_row(
                row,
                labels.get(row.drone_id),
                links.get(row.drone_id),
                firmware.get(row.drone_id),
                rx_ts=rx_ts,
                backlog=False if backlog is None else backlog[n],
                captured_at=None if captured_at is None else captured_at[n],
            )

    async def publish_station(
        self,
        station_id: str,
        state: LinkState,
        *,
        last_datagram_age_ms: int | None = None,
        queue_depth: int | None = None,
        losses: list[LossEvent] | None = None,
        lag_s: float | None = None,
    ) -> None:
        await self._send(
            station_subject(station_id),
            encode_station(
                station_id,
                state,
                last_datagram_age_ms=last_datagram_age_ms,
                queue_depth=queue_depth,
                losses=losses or [],
                lag_s=lag_s,
            ),
        )

    async def publish_unclaimed(
        self, station_id: str, resolution: Resolution, source_id: SourceId
    ) -> None:
        # A source refused by station policy (P1-07) has its own subject: the
        # console must not render "not assigned to this station" as "no
        # binding - register to track", which invites registering a spoof.
        await self._send(
            event_subject(
                "rejected_source" if resolution.is_rejected else "unclaimed_source"
            ),
            encode_unclaimed(
                station_id,
                resolution,
                sysid=source_id.sysid,
                compid=source_id.compid,
            ),
        )

    async def _send(self, subject: str, payload: dict[str, Any]) -> None:
        # A publish failure must not stop ingest. The bus carries the present;
        # the archive and the hypertable carry the record, and both have
        # already been written by the time anything reaches here.
        try:
            await self.bus.publish(subject, json.dumps(payload).encode("utf-8"))
        except Exception as error:
            _log.warning(
                "could not publish to the bus",
                extra={"subject": subject, "error": str(error)},
            )

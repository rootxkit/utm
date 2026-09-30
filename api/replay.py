"""Flight replay: a past flight as the record shows it, holes included. P10-03.

## What a replay is made of

- **Samples** from `drone_state` (telemetry database): every stored row for
  the aircraft in the window, from every station that relayed it. Two stations
  relaying one vehicle are two independent observations (spec §8) and both
  are kept.
- **Evidence** from the Gateway's own log (telemetry database): relay `gap`s
  with their exact sequence range (`relay_epoch_gaps`), intake drops and relay
  restarts (`ingest_events` `loss.*`), a relay queue replaced (`epoch_closed`),
  and station link states (`ingest_events` `link_state`).
- **Airspace alerts** from the business audit log (relational database,
  `events`), raised and cleared, as the airspace monitor wrote them (P5-15).

## Remote ID aircraft

An aircraft that was only ever heard broadcasting Remote ID (P1-15) is not in
`known_drones` and has no `drone_state`. Its samples come from
`remote_id_observations`, one per receiver that heard it, and the replay says
`source: "remote_id"` and `authenticated: false`, as the live console does:
the track is where the transmitter claimed to be. Receivers are not relays, so
there is no relay evidence for them, and a hole in such a track is silence.
Its "flights" are spans declared airborne, where ours are spans armed.

## Holes are shown, never drawn across

TASKS.md P10-03: a smooth line through missing data invents evidence, which in
an accident investigation is worse than showing nothing. So the track is cut
into segments, and nothing is drawn between two segments. A hole is:

1. **Silence** - two consecutive samples further apart than
   `gap_threshold_s`. Normal telemetry is several rows a second; a threshold of
   seconds is not a claim about any stream rate (spec §6.4), only a line past
   which drawing a straight segment would be inventing a path.
2. **A known loss** - a relay `gap` whose exact time bounds fall between two
   consecutive samples, *however short*. A gap of three records is below any
   silence threshold and still three records nobody will ever see. It does
   not cut the track if another station delivered this aircraft inside the
   gap: then what was lost on one link was heard on the other.
3. **No position** - telemetry that arrived without a position fix. The
   aircraft was heard; where it was is unknown, so the line stops there too.

Each silence hole is explained by the evidence around it, or reported as
unexplained. "No recorded cause" is a finding, and it is the honest one: the
alternative is a reason made up to fill the space.

## What is exact and what is not

A relay `gap` is bounded by the capture times of the last record before it and
the first after it, read from `archive_segments`, when both are indexed; it is
then `exact`. Intake drops, relay restarts and epoch changes are observed when
a `status` arrives, so their time is known only to within `evidence_slack_s`,
and they are listed on the timeline without cutting the track: where in that
window the lost datagrams belonged is not recorded anywhere.

Capture times are the station's clock (`recv_utc_ns`, relay-v1 §9, which says
it may be wrong); link states and alerts are timestamped by the Gateway and
the airspace service. On one machine they agree. Across machines they agree to
within clock sync, which is why evidence is matched with a slack rather than
to the millisecond.

## Where this runs

In the core API, which may read both databases. Not in the console: it must
never read a database (`api/telemetry_ws.py`), and a replay is nothing but
database reads.
"""

from __future__ import annotations

import bisect
import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from common import get_logger

_log = get_logger(__name__)


class ReplayError(Exception):
    """A replay that cannot be produced as asked."""


class DroneNotFoundError(ReplayError):
    pass


class WindowTooLargeError(ReplayError):
    pass


class EvidenceKind(StrEnum):
    # Data lost: relay-v1 §11 loss #3, with an exact sequence range.
    RELAY_GAP = "relay_gap"
    # Data lost: §11 loss #2. A count, and no range.
    INTAKE_DROP = "intake_drop"
    # Data lost: §11 loss #4. Neither a count nor a range.
    RELAY_RESTART = "relay_restart"
    # The relay's queue database was recreated. Anything it had not delivered
    # went with the old one.
    RELAY_QUEUE_REPLACED = "relay_queue_replaced"
    # Not loss. §9: an unreachable station is presumed to be buffering.
    STATION_UNREACHABLE = "station_unreachable"
    # The station was connected and hearing nothing from any aircraft.
    STATION_RADIO_SILENT = "station_radio_silent"
    # The station was delivering, late.
    STATION_LAGGING = "station_lagging"


# Which kinds mean telemetry is actually gone. Nothing else may say so: an
# unreachable station is buffering (spec §9), and a replay that called it loss
# would overstate exactly as P6-03 warns the console must not.
DATA_LOST_KINDS = frozenset(
    {
        EvidenceKind.RELAY_GAP,
        EvidenceKind.INTAKE_DROP,
        EvidenceKind.RELAY_RESTART,
        EvidenceKind.RELAY_QUEUE_REPLACED,
    }
)

_LINK_STATE_KINDS = {
    "unreachable": EvidenceKind.STATION_UNREACHABLE,
    "radio_silent": EvidenceKind.STATION_RADIO_SILENT,
    "lagging": EvidenceKind.STATION_LAGGING,
}

_LOSS_EVENT_KINDS = {
    "loss.intake_drop": EvidenceKind.INTAKE_DROP,
    "loss.relay_restart": EvidenceKind.RELAY_RESTART,
    "epoch_closed": EvidenceKind.RELAY_QUEUE_REPLACED,
}


class HoleCause(StrEnum):
    # No telemetry at all.
    NO_TELEMETRY = "no_telemetry"
    # Telemetry, but without a position.
    NO_POSITION = "no_position"


@dataclass(frozen=True, slots=True)
class Sample:
    ts: datetime
    station_id: str
    lat_deg: float | None
    lon_deg: float | None
    alt_amsl_m: float | None = None
    alt_above_home_m: float | None = None
    heading_deg: float | None = None
    # Ground velocity north, east, down (down positive, as MAVLink sends it).
    # What the airspace monitor's CPA is computed from, so a replay can show
    # why an alert was raised or cleared.
    vx_ms: float | None = None
    vy_ms: float | None = None
    vz_ms: float | None = None
    groundspeed_ms: float | None = None
    climb_ms: float | None = None
    batt_pct: float | None = None
    mode: str | None = None
    armed: bool | None = None

    @property
    def positioned(self) -> bool:
        return self.lat_deg is not None and self.lon_deg is not None

    def as_dict(self) -> dict[str, Any]:
        return {
            "ts": self.ts.isoformat(),
            "station_id": self.station_id,
            "lat_deg": self.lat_deg,
            "lon_deg": self.lon_deg,
            "alt_amsl_m": self.alt_amsl_m,
            "alt_above_home_m": self.alt_above_home_m,
            "heading_deg": self.heading_deg,
            "vx_ms": self.vx_ms,
            "vy_ms": self.vy_ms,
            "vz_ms": self.vz_ms,
            "groundspeed_ms": self.groundspeed_ms,
            "climb_ms": self.climb_ms,
            "batt_pct": self.batt_pct,
            "mode": self.mode,
            "armed": self.armed,
        }


@dataclass(slots=True)
class Evidence:
    kind: EvidenceKind
    station_id: str
    start: datetime
    end: datetime
    # True when the bounds are measured, not estimated.
    exact: bool
    detail: dict[str, Any] = field(default_factory=dict)
    # Set by `build_replay` for exact relay gaps: what the loss did to this
    # track. "cuts" it, was "heard_elsewhere" (another station delivered the
    # aircraft inside it), or lies "outside_track" (before the first sample or
    # after the last). Empty for everything else.
    track_effect: str = ""

    @property
    def cuts_track(self) -> bool:
        return self.track_effect == "cuts"

    @property
    def data_lost(self) -> bool:
        return self.kind in DATA_LOST_KINDS

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "station_id": self.station_id,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "exact": self.exact,
            "data_lost": self.data_lost,
            "track_effect": self.track_effect,
            "detail": self.detail,
        }


@dataclass(slots=True)
class Hole:
    cause: HoleCause
    # The last sample before the hole and the first after it, by index into
    # the replay's samples, and their times.
    after_index: int
    before_index: int
    after_ts: datetime
    before_ts: datetime
    reasons: list[Evidence] = field(default_factory=list)

    @property
    def duration_s(self) -> float:
        return (self.before_ts - self.after_ts).total_seconds()

    def as_dict(self) -> dict[str, Any]:
        explained = self.cause is HoleCause.NO_POSITION or bool(self.reasons)
        return {
            "cause": self.cause.value,
            "after_index": self.after_index,
            "before_index": self.before_index,
            "after_ts": self.after_ts.isoformat(),
            "before_ts": self.before_ts.isoformat(),
            "duration_s": round(self.duration_s, 3),
            "explained": explained,
            "data_lost": any(reason.data_lost for reason in self.reasons),
            "reasons": [reason.as_dict() for reason in self.reasons],
        }


@dataclass(slots=True)
class Replay:
    samples: list[Sample]
    # Runs of positioned samples with nothing missing between them, as
    # inclusive [first, last] indices. The only thing a map may draw as a line.
    segments: list[tuple[int, int]]
    holes: list[Hole]
    evidence: list[Evidence]


def build_replay(
    samples: Sequence[Sample],
    evidence: Iterable[Evidence],
    *,
    gap_threshold_s: float,
    evidence_slack_s: float,
) -> Replay:
    """Cut the track where the record is missing, and say why. Pure.

    `samples` must be in time order.
    """
    evidence = sorted(evidence, key=lambda item: item.start)
    times = [sample.ts for sample in samples]
    forced = _breaks_from_gaps(samples, times, evidence)

    holes: list[Hole] = []
    segments: list[tuple[int, int]] = []
    run_start: int | None = None
    last_positioned: int | None = None
    slack = timedelta(seconds=evidence_slack_s)

    for index, sample in enumerate(samples):
        if index > 0:
            silent = (times[index] - times[index - 1]).total_seconds() > gap_threshold_s
            if silent or index in forced:
                hole = Hole(
                    cause=HoleCause.NO_TELEMETRY,
                    after_index=index - 1,
                    before_index=index,
                    after_ts=times[index - 1],
                    before_ts=times[index],
                )
                hole.reasons = [
                    item
                    for item in evidence
                    if item.start <= hole.before_ts + slack
                    and item.end >= hole.after_ts - slack
                    # An exact gap that did not cut this track was heard on
                    # another link, or lies elsewhere: not a cause here. An
                    # inexact one may be, and is listed as a possibility.
                    and not (
                        item.kind is EvidenceKind.RELAY_GAP
                        and item.exact
                        and not item.cuts_track
                    )
                ]
                holes.append(hole)
                if run_start is not None and last_positioned is not None:
                    segments.append((run_start, last_positioned))
                run_start = None

        if not sample.positioned:
            if run_start is not None and last_positioned is not None:
                segments.append((run_start, last_positioned))
                run_start = None
            continue

        if run_start is None:
            # A run after positionless samples, with no telemetry hole between:
            # the aircraft was heard but not placed.
            if (
                last_positioned is not None
                and last_positioned < index - 1
                and not _hole_between(holes, last_positioned, index)
            ):
                holes.append(
                    Hole(
                        cause=HoleCause.NO_POSITION,
                        after_index=last_positioned,
                        before_index=index,
                        after_ts=times[last_positioned],
                        before_ts=times[index],
                    )
                )
            run_start = index
        last_positioned = index

    if run_start is not None and last_positioned is not None:
        segments.append((run_start, last_positioned))

    holes.sort(key=lambda hole: (hole.after_index, hole.before_index))
    return Replay(
        samples=list(samples), segments=segments, holes=holes, evidence=evidence
    )


def _hole_between(holes: list[Hole], first: int, last: int) -> bool:
    return any(
        first <= hole.after_index and hole.before_index <= last for hole in holes
    )


def _breaks_from_gaps(
    samples: Sequence[Sample],
    times: list[datetime],
    evidence: list[Evidence],
) -> set[int]:
    """Indices `i` where a relay gap falls between samples `i-1` and `i`.

    Marks each exact gap with whether it cuts this track. A gap cuts it when
    the aircraft has samples on both sides and none from another station
    inside it: the lost records may have been this aircraft's, and nothing
    else heard it then. A sample from the gap's own station cannot be inside
    it - its records in that range are the lost ones - so any sample inside is
    another link's.
    """
    forced: set[int] = set()
    for item in evidence:
        if item.kind is not EvidenceKind.RELAY_GAP or not item.exact:
            continue
        first_after = bisect.bisect_right(times, item.start)
        if first_after == 0 or first_after == len(samples):
            item.track_effect = "outside_track"
            continue
        heard_elsewhere = any(
            samples[index].station_id != item.station_id
            for index in range(first_after, bisect.bisect_left(times, item.end))
        )
        if heard_elsewhere:
            item.track_effect = "heard_elsewhere"
            continue
        item.track_effect = "cuts"
        # The break goes after the last sample at or before the gap began.
        forced.add(first_after)
    return forced


def link_state_evidence(
    transitions: Iterable[tuple[str, datetime, str]],
    *,
    window_end: datetime,
) -> list[Evidence]:
    """Intervals of non-healthy station state from `link_state` rows. Pure.

    Each transition holds until the station's next one; the last holds until
    the end of the window. Rows are (station_id, ts, state).
    """
    by_station: dict[str, list[tuple[datetime, str]]] = {}
    for station_id, ts, state in transitions:
        by_station.setdefault(station_id, []).append((ts, state))

    found: list[Evidence] = []
    for station_id, rows in by_station.items():
        rows.sort()
        for position, (ts, state) in enumerate(rows):
            kind = _LINK_STATE_KINDS.get(state)
            if kind is None:
                continue
            until = rows[position + 1][0] if position + 1 < len(rows) else window_end
            found.append(
                Evidence(
                    kind=kind,
                    station_id=station_id,
                    start=ts,
                    end=max(until, ts),
                    exact=True,
                    detail={"state": state, "ongoing": position + 1 == len(rows)},
                )
            )
    return found


def gap_evidence(
    *,
    station_id: str,
    from_seq: int,
    to_seq: int,
    reason: str,
    recorded_at: datetime,
    before_ns: int | None,
    after_ns: int | None,
) -> Evidence:
    """A relay gap in time. Exact only when both neighbours are indexed. Pure.

    `before_ns` is the capture time of record `from_seq - 1`, `after_ns` of
    record `to_seq` (to_seq is exclusive, relay-v1 §11). Without both, the
    gap is placed at the time the Gateway recorded it, which for a backlog
    delivered after a reconnect may be long after it happened: so it is
    marked inexact, listed, and never allowed to cut a track.
    """
    before = _from_ns(before_ns)
    after = _from_ns(after_ns)
    start = before or after or recorded_at
    end = after or recorded_at
    if end < start:
        start, end = end, start
    return Evidence(
        kind=EvidenceKind.RELAY_GAP,
        station_id=station_id,
        start=start,
        end=end,
        exact=before is not None and after is not None,
        detail={
            "from_seq": from_seq,
            "to_seq": to_seq,
            "missing_count": to_seq - from_seq,
            "reason": reason,
        },
    )


def _from_ns(value: int | None) -> datetime | None:
    """The same expression the Gateway stores `drone_state.ts` with
    (`gateway.drone_state.timestamp_from_recv_utc_ns`), so a gap's bound and
    the sample at that bound compare equal rather than a microsecond apart,
    which would put the cut on the wrong side of the sample."""
    if value is None:
        return None
    return datetime.fromtimestamp(value / 1_000_000_000, tz=UTC)


# --- reading -----------------------------------------------------------------

_SAMPLES = sa.text(
    """
    SELECT ts, station_id, ST_Y(geom) AS lat_deg, ST_X(geom) AS lon_deg,
           alt_amsl_m, alt_above_home_m, heading_deg, vx_ms, vy_ms, vz_ms,
           groundspeed_ms, climb_ms,
           batt_pct, mode, armed
    FROM drone_state
    WHERE drone_id = :drone_id AND ts >= :start AND ts <= :end
    ORDER BY ts, station_id
    LIMIT :limit
    """
)

_LABEL = sa.text("SELECT label FROM known_drones WHERE drone_id = :drone_id")

SOURCE_MAVLINK = "mavlink"
SOURCE_REMOTE_ID = "remote_id"

# The latest identity it broadcast. The id is derived from that identity, so
# every row agrees; the latest is taken rather than assumed.
_RID_LABEL = sa.text(
    """
    SELECT ua_id FROM remote_id_observations
    WHERE aircraft_id = :drone_id ORDER BY ts DESC LIMIT 1
    """
)

_RID_AIRCRAFT = sa.text(
    """
    SELECT DISTINCT ON (aircraft_id) aircraft_id, ua_id
    FROM remote_id_observations ORDER BY aircraft_id, ts DESC
    """
)

# Heights: the AMSL height the monitor used (NULL without a geoid). There is
# no height above home; `alt_above_takeoff_m` is the broadcast's own and is
# shown in that place, because it is the same quantity.
_RID_SAMPLES = sa.text(
    """
    SELECT ts, receiver_id AS station_id, ST_Y(geom) AS lat_deg,
           ST_X(geom) AS lon_deg, alt_amsl_m,
           alt_above_takeoff_m AS alt_above_home_m,
           -- Remote ID reports the track, not where the nose points.
           NULL::double precision AS heading_deg,
           vx_ms, vy_ms, vz_ms, groundspeed_ms, climb_ms,
           -- A broadcast carries none of these: NULL, never a default.
           NULL::double precision AS batt_pct, NULL::text AS mode,
           NULL::boolean AS armed
    FROM remote_id_observations
    WHERE aircraft_id = :drone_id AND ts >= :start AND ts <= :end
    ORDER BY ts, receiver_id
    LIMIT :limit
    """
)

# Every aircraft whose telemetry can be resolved, retired or not. The
# telemetry registry, not the business one: an aircraft registered only
# there (SITL fleets, P1-13) still flew, and still has a record to replay.
_DRONES = sa.text(
    """
    SELECT drone_id, label, retired_at FROM known_drones ORDER BY label, drone_id
    """
)

# Gaps are filtered by when they were recorded, from the window's start: a gap
# cannot be recorded before it happened, but a backlog can report one long
# after, so there is no upper bound here and the window is applied to the
# computed bounds instead.
_GAPS = sa.text(
    """
    SELECT g.station_id, g.from_seq, g.to_seq, g.reason, g.recorded_at,
      (SELECT max(a.last_recv_utc_ns) FROM archive_segments a
        WHERE a.station_id = g.station_id AND a.epoch = g.epoch
          AND a.last_seq = g.from_seq - 1) AS before_ns,
      (SELECT min(a.first_recv_utc_ns) FROM archive_segments a
        WHERE a.station_id = g.station_id AND a.epoch = g.epoch
          AND a.first_seq = g.to_seq) AS after_ns
    FROM relay_epoch_gaps g
    WHERE g.station_id = ANY(:stations) AND g.recorded_at >= :since
    """
)

_LOSS_EVENTS = sa.text(
    """
    SELECT station_id, ts, event_type, payload
    FROM ingest_events
    WHERE event_type = ANY(:types) AND station_id = ANY(:stations)
      AND ts >= :since AND ts <= :until
    ORDER BY ts
    """
)

# The transitions inside the window, and each station's state as it entered
# the window, which is its last transition before it.
_LINK_STATES = sa.text(
    """
    (SELECT station_id, ts, payload->>'state' AS state
       FROM ingest_events
      WHERE event_type = 'link_state' AND station_id = ANY(:stations)
        AND ts > :since AND ts <= :until)
    UNION ALL
    (SELECT DISTINCT ON (station_id) station_id, ts, payload->>'state' AS state
       FROM ingest_events
      WHERE event_type = 'link_state' AND station_id = ANY(:stations)
        AND ts <= :since
      ORDER BY station_id, ts DESC)
    """
)

_FLIGHTS = sa.text(
    """
    WITH armed AS (
      SELECT ts FROM drone_state
      WHERE drone_id = :drone_id AND armed IS TRUE
        AND ts >= :since AND ts < :until
    ),
    marked AS (
      SELECT ts,
        CASE WHEN lag(ts) OVER (ORDER BY ts) IS NULL
               OR ts - lag(ts) OVER (ORDER BY ts) > make_interval(secs => :split_s)
             THEN 1 ELSE 0 END AS starts_flight
      FROM armed
    ),
    numbered AS (
      SELECT ts, sum(starts_flight) OVER (ORDER BY ts) AS flight FROM marked
    )
    SELECT min(ts) AS start, max(ts) AS "end", count(*) AS armed_samples
    FROM numbered GROUP BY flight ORDER BY start DESC LIMIT :limit
    """
)

# The same split for a broadcast aircraft, on declared status: every status
# but "ground" (1) is airborne, as in gateway/remote_id.py.
_RID_FLIGHTS = sa.text(
    """
    WITH armed AS (
      SELECT ts FROM remote_id_observations
      WHERE aircraft_id = :drone_id AND status IS DISTINCT FROM 1
        AND ts >= :since AND ts < :until
    ),
    marked AS (
      SELECT ts,
        CASE WHEN lag(ts) OVER (ORDER BY ts) IS NULL
               OR ts - lag(ts) OVER (ORDER BY ts) > make_interval(secs => :split_s)
             THEN 1 ELSE 0 END AS starts_flight
      FROM armed
    ),
    numbered AS (
      SELECT ts, sum(starts_flight) OVER (ORDER BY ts) AS flight FROM marked
    )
    SELECT min(ts) AS start, max(ts) AS "end", count(*) AS armed_samples
    FROM numbered GROUP BY flight ORDER BY start DESC LIMIT :limit
    """
)

_ALERTS = sa.text(
    """
    SELECT id, ts, event_type, payload FROM events
    WHERE entity_type = 'drone' AND entity_id = :drone_id
      AND event_type IN ('airspace_alert_raised', 'airspace_alert_cleared')
      AND ts >= :since AND ts <= :until
    ORDER BY id
    """
)


@dataclass
class ReplayStore:
    """Reads a replay from both databases. Read-only by construction."""

    telemetry: AsyncEngine
    # None when this API has no relational database; alerts are then
    # reported as unavailable rather than as none.
    relational: AsyncEngine | None
    gap_threshold_s: float
    evidence_slack_s: float
    flight_split_s: float
    max_samples: int
    # S-16. The flight list scans armed telemetry across its whole window, so
    # a window is refused beyond this rather than scanned; `max_samples` does
    # the same for a replay. The default matches `REPLAY_MAX_FLIGHT_WINDOW_S`.
    max_flight_window_s: float = 90 * 86400.0

    async def drones(self) -> list[dict[str, Any]]:
        async with self.telemetry.connect() as connection:
            rows = (await connection.execute(_DRONES)).all()
            broadcast = (await connection.execute(_RID_AIRCRAFT)).all()
        ours = [
            {
                "drone_id": str(row.drone_id),
                "label": row.label,
                "retired": row.retired_at is not None,
                "source": SOURCE_MAVLINK,
            }
            for row in rows
        ]
        heard = [
            {
                "drone_id": str(row.aircraft_id),
                "label": row.ua_id,
                "retired": False,
                "source": SOURCE_REMOTE_ID,
            }
            for row in sorted(broadcast, key=lambda r: (r.ua_id, str(r.aircraft_id)))
        ]
        return ours + heard

    async def identify(self, drone_id: UUID) -> tuple[str, str]:
        """Its label and where its record is: ours, or heard broadcasting."""
        async with self.telemetry.connect() as connection:
            label = (
                await connection.execute(_LABEL, {"drone_id": drone_id})
            ).scalar_one_or_none()
            if label is not None:
                return str(label), SOURCE_MAVLINK
            ua_id = (
                await connection.execute(_RID_LABEL, {"drone_id": drone_id})
            ).scalar_one_or_none()
        if ua_id is not None:
            return str(ua_id), SOURCE_REMOTE_ID
        raise DroneNotFoundError(
            f"no drone {drone_id} in the telemetry registry or among Remote ID "
            "aircraft heard"
        )

    async def flights(
        self, drone_id: UUID, *, since: datetime, until: datetime, limit: int
    ) -> list[dict[str, Any]]:
        """Armed spans, newest first. A flight is armed telemetry with no
        silence longer than `flight_split_s` inside it."""
        if until <= since:
            raise ReplayError("the window must end after it starts")
        if (until - since).total_seconds() > self.max_flight_window_s:
            raise WindowTooLargeError(
                f"a flight list spans at most {self.max_flight_window_s:g} s; "
                "narrow the window"
            )
        _, source = await self.identify(drone_id)
        query = _RID_FLIGHTS if source == SOURCE_REMOTE_ID else _FLIGHTS
        async with self.telemetry.connect() as connection:
            rows = (
                await connection.execute(
                    query,
                    {
                        "drone_id": drone_id,
                        "since": since,
                        "until": until,
                        "split_s": self.flight_split_s,
                        "limit": limit,
                    },
                )
            ).all()
        return [
            {
                "start": row.start.isoformat(),
                "end": row.end.isoformat(),
                "duration_s": round((row.end - row.start).total_seconds(), 1),
                "armed_samples": int(row.armed_samples),
            }
            for row in rows
        ]

    async def replay(
        self, drone_id: UUID, *, start: datetime, end: datetime
    ) -> dict[str, Any]:
        if end <= start:
            raise ReplayError("the window must end after it starts")
        label, source = await self.identify(drone_id)
        samples = await self._samples(drone_id, start, end, source)
        stations = sorted({sample.station_id for sample in samples})
        # Receivers are not relays: a receiver named like a station must not
        # pick up that station's gaps.
        evidence = (
            await self._evidence(stations, start, end)
            if source == SOURCE_MAVLINK
            else []
        )
        replay = build_replay(
            samples,
            evidence,
            gap_threshold_s=self.gap_threshold_s,
            evidence_slack_s=self.evidence_slack_s,
        )
        alerts, alerts_error = await self._alerts(drone_id, start, end)
        return {
            "drone_id": str(drone_id),
            "label": label,
            "source": source,
            # A broadcast track is where the transmitter claimed to be.
            "authenticated": source == SOURCE_MAVLINK,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "gap_threshold_s": self.gap_threshold_s,
            "stations": stations,
            "samples": [sample.as_dict() for sample in replay.samples],
            "segments": [list(segment) for segment in replay.segments],
            "holes": [hole.as_dict() for hole in replay.holes],
            "evidence": [item.as_dict() for item in replay.evidence],
            "alerts": alerts,
            "alerts_error": alerts_error,
        }

    async def _samples(
        self, drone_id: UUID, start: datetime, end: datetime, source: str
    ) -> list[Sample]:
        query = _RID_SAMPLES if source == SOURCE_REMOTE_ID else _SAMPLES
        async with self.telemetry.connect() as connection:
            rows = (
                await connection.execute(
                    query,
                    {
                        "drone_id": drone_id,
                        "start": start,
                        "end": end,
                        "limit": self.max_samples + 1,
                    },
                )
            ).all()
        if len(rows) > self.max_samples:
            # Refused rather than thinned. Thinning evenly would be safe for
            # the holes - they are found before anything is dropped - but a
            # replay that quietly shows less than was recorded is the kind of
            # thing an investigation later trips over.
            raise WindowTooLargeError(
                f"more than {self.max_samples} samples in this window; narrow it"
            )
        return [
            Sample(
                ts=row.ts,
                station_id=row.station_id,
                lat_deg=row.lat_deg,
                lon_deg=row.lon_deg,
                alt_amsl_m=row.alt_amsl_m,
                alt_above_home_m=row.alt_above_home_m,
                heading_deg=row.heading_deg,
                vx_ms=row.vx_ms,
                vy_ms=row.vy_ms,
                vz_ms=row.vz_ms,
                groundspeed_ms=row.groundspeed_ms,
                climb_ms=row.climb_ms,
                batt_pct=row.batt_pct,
                mode=row.mode,
                armed=row.armed,
            )
            for row in rows
        ]

    async def _evidence(
        self, stations: list[str], start: datetime, end: datetime
    ) -> list[Evidence]:
        if not stations:
            return []
        slack = timedelta(seconds=self.evidence_slack_s)
        since, until = start - slack, end + slack
        async with self.telemetry.connect() as connection:
            gap_rows = (
                await connection.execute(_GAPS, {"stations": stations, "since": since})
            ).all()
            loss_rows = (
                await connection.execute(
                    _LOSS_EVENTS,
                    {
                        "types": list(_LOSS_EVENT_KINDS),
                        "stations": stations,
                        "since": since,
                        "until": until,
                    },
                )
            ).all()
            link_rows = (
                await connection.execute(
                    _LINK_STATES, {"stations": stations, "since": since, "until": until}
                )
            ).all()

        found: list[Evidence] = []
        for row in gap_rows:
            gap = gap_evidence(
                station_id=row.station_id,
                from_seq=row.from_seq,
                to_seq=row.to_seq,
                reason=row.reason,
                recorded_at=row.recorded_at,
                before_ns=row.before_ns,
                after_ns=row.after_ns,
            )
            if gap.end >= since and gap.start <= until:
                found.append(gap)
        for row in loss_rows:
            payload = _payload(row.payload)
            found.append(
                Evidence(
                    kind=_LOSS_EVENT_KINDS[row.event_type],
                    station_id=row.station_id,
                    # Detected at a `status`; happened since the one before.
                    start=row.ts - slack,
                    end=row.ts,
                    exact=False,
                    detail=payload,
                )
            )
        found.extend(
            link_state_evidence(
                ((row.station_id, row.ts, row.state) for row in link_rows),
                window_end=until,
            )
        )
        return found

    async def _alerts(
        self, drone_id: UUID, start: datetime, end: datetime
    ) -> tuple[list[dict[str, Any]] | None, str | None]:
        """Alerts for this aircraft, or None and why if they cannot be read.

        Unavailable is not the same as none: a replay that showed no alerts
        because the audit log was unreachable would be answering the question
        from the wrong source.
        """
        if self.relational is None:
            return None, "no relational database configured"
        slack = timedelta(seconds=self.evidence_slack_s)
        try:
            async with self.relational.connect() as connection:
                rows = (
                    await connection.execute(
                        _ALERTS,
                        {
                            "drone_id": str(drone_id),
                            "since": start - slack,
                            "until": end + slack,
                        },
                    )
                ).all()
        except (SQLAlchemyError, OSError) as error:
            _log.error(
                "could not read airspace alerts for a replay",
                extra={"drone_id": str(drone_id), "error": repr(error)},
            )
            return None, "the audit log could not be read"
        return [
            {
                "id": row.id,
                "ts": row.ts.isoformat(),
                "state": row.event_type.removeprefix("airspace_alert_"),
                **_payload(row.payload),
            }
            for row in rows
        ], None


def _payload(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        loaded = json.loads(value)
        return loaded if isinstance(loaded, dict) else {"value": loaded}
    return {}

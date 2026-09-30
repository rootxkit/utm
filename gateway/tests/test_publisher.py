"""Publishing to the bus, and the §9 distinction surviving the trip.

The station tests are the point. `unreachable` and `data_lost` mean different
things, and the payload says which explicitly rather than leaving a console to
work it out - because the console that works it out will get it wrong in the
3-to-25 second window where the Gateway and the relay disagree, and will show a
buffering station as a station losing data.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest

from gateway.binding import NOT_ASSIGNED, UNBOUND, Resolution
from gateway.drone_state import DroneStateRow
from gateway.parsing import SourceId
from gateway.publisher import (
    TelemetryPublisher,
    encode_row,
    event_subject,
    station_subject,
    telemetry_subject,
)
from gateway.station_state import LinkState, LossEvent, LossKind

NOON = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)
ADDRESS = SourceId(sysid=1, compid=1)


class RecordingBus:
    """A bus that remembers, and can be told to fail."""

    def __init__(self, *, fail: bool = False) -> None:
        self.published: list[tuple[str, dict[str, Any]]] = []
        self.fail = fail

    async def publish(self, subject: str, payload: bytes) -> None:
        if self.fail:
            raise ConnectionError("the bus is down")
        self.published.append((subject, json.loads(payload)))


def row(**overrides: object) -> DroneStateRow:
    base: dict[str, object] = {
        "drone_id": uuid4(),
        "ts": NOON,
        "station_id": "tbilisi-base-1",
        "lat_deg": 41.7151,
        "lon_deg": 44.8271,
        "alt_amsl_m": 450.0,
        "alt_above_home_m": 60.0,
        "heading_deg": 90.0,
        "batt_pct": 87.0,
    }
    base.update(overrides)
    return DroneStateRow(**base)  # type: ignore[arg-type]


# --- subjects --------------------------------------------------------------


def test_telemetry_is_published_per_drone() -> None:
    """P1-06: `telemetry.{drone_id}`.

    Per drone, so a console can subscribe to one aircraft and a fleet-wide
    subscriber can use a wildcard, without either filtering in the client.
    """
    drone_id = uuid4()
    assert telemetry_subject(drone_id) == f"telemetry.{drone_id}"
    assert station_subject("tbilisi-base-1") == "station.tbilisi-base-1"
    assert event_subject("unclaimed_source") == "events.unclaimed_source"


async def test_a_row_reaches_the_bus_on_its_own_subject() -> None:
    bus = RecordingBus()
    published = row()

    await TelemetryPublisher(bus=bus).publish_row(published)

    assert len(bus.published) == 1
    subject, payload = bus.published[0]
    assert subject == f"telemetry.{published.drone_id}"
    assert payload["lat_deg"] == pytest.approx(41.7151)
    assert payload["ts"] == NOON.isoformat()


# --- the altitudes survive the trip ----------------------------------------


def test_the_payload_carries_both_altitudes_and_no_agl() -> None:
    """The same rule one layer further out.

    Renaming `alt_above_home_m` to `alt_agl_m` on the way to the console would
    be exactly the silent error the column was removed to prevent - the console
    would display a height above ground that nothing measured.
    """
    payload = encode_row(row())

    assert payload["alt_amsl_m"] == pytest.approx(450.0)
    assert payload["alt_above_home_m"] == pytest.approx(60.0)
    assert "alt_agl_m" not in payload


def test_the_payload_says_when_the_gateway_received_it_and_whether_it_is_backlog() -> (
    None
):
    """S-11. `rx_ts` is the Gateway's clock; `backlog` is the relay session's
    verdict. A row published without either (the Redis snapshot) says so."""
    received = NOON.replace(second=7)

    live = encode_row(row(), rx_ts=received, backlog=False)
    replayed = encode_row(row(), rx_ts=received, backlog=True)
    snapshot = encode_row(row())

    assert (live["rx_ts"], live["backlog"]) == (received.isoformat(), False)
    assert (replayed["rx_ts"], replayed["backlog"]) == (received.isoformat(), True)
    assert (snapshot["rx_ts"], snapshot["backlog"]) == (None, False)
    assert live["ts"] == NOON.isoformat(), "the capture time is untouched"
    # Placed at `rx_ts` unless the pipeline placed it finer; null without.
    assert live["captured_at"] == received.isoformat()
    assert snapshot["captured_at"] is None
    placed = encode_row(row(), rx_ts=received, captured_at=received.replace(second=2))
    assert placed["captured_at"] == received.replace(second=2).isoformat()


async def test_a_batch_carries_one_backlog_flag_per_row() -> None:
    bus = RecordingBus()
    rows = [row(), row()]

    await TelemetryPublisher(bus=bus).publish_rows(
        rows, rx_ts=NOON, backlog=[True, False]
    )
    with pytest.raises(ValueError, match="one per row"):
        await TelemetryPublisher(bus=bus).publish_rows(rows, backlog=[True])

    assert [payload["backlog"] for _, payload in bus.published] == [True, False]
    assert {payload["rx_ts"] for _, payload in bus.published} == {NOON.isoformat()}


def test_an_unknown_value_is_published_as_null_not_zero() -> None:
    """A console showing 0% for a battery nobody measured is worse than one
    showing nothing: the first is a number a pilot will act on."""
    payload = encode_row(row(batt_pct=None, heading_deg=None))

    assert payload["batt_pct"] is None
    assert payload["heading_deg"] is None


# --- station state: the distinction that matters ---------------------------


async def test_an_unreachable_station_is_not_reported_as_data_loss() -> None:
    """Spec §9, and the reason this module publishes state at all.

    The relay is alive, receiving at full rate and buffering. The record
    completes on reconnect and nothing has been lost.
    """
    bus = RecordingBus()

    await TelemetryPublisher(bus=bus).publish_station(
        "tbilisi-base-1", LinkState.UNREACHABLE, last_datagram_age_ms=40
    )

    _, payload = bus.published[0]
    assert payload["state"] == "unreachable"
    assert payload["data_is_lost"] is False
    assert payload["buffering"] is True


async def test_a_station_that_lost_data_says_so() -> None:
    """The paired presence test.

    Without it, a publisher that hardcoded `data_is_lost: False` would pass
    every assertion above.
    """
    bus = RecordingBus()
    loss = LossEvent(
        kind=LossKind.QUEUE_CAP,
        from_seq=58120,
        to_seq=61099,
        datagram_count=2979,
        detail="queue_cap",
    )

    await TelemetryPublisher(bus=bus).publish_station(
        "tbilisi-base-1", LinkState.DATA_LOST, losses=[loss]
    )

    _, payload = bus.published[0]
    assert payload["state"] == "data_lost"
    assert payload["data_is_lost"] is True
    assert payload["buffering"] is False
    assert payload["losses"][0]["datagram_count"] == 2979
    assert payload["losses"][0]["kind"] == "queue_cap"


async def test_radio_silent_is_its_own_state_and_is_not_loss() -> None:
    """The station has lost the aircraft - a flight-safety event - but nothing
    the relay held has gone missing."""
    bus = RecordingBus()

    await TelemetryPublisher(bus=bus).publish_station(
        "tbilisi-base-1", LinkState.RADIO_SILENT, last_datagram_age_ms=30_000
    )

    _, payload = bus.published[0]
    assert payload["state"] == "radio_silent"
    assert payload["data_is_lost"] is False
    assert payload["buffering"] is False
    assert payload["last_datagram_age_ms"] == 30_000


async def test_a_healthy_station_is_neither() -> None:
    bus = RecordingBus()
    await TelemetryPublisher(bus=bus).publish_station(
        "tbilisi-base-1", LinkState.HEALTHY, last_datagram_age_ms=38
    )
    _, payload = bus.published[0]
    assert (payload["data_is_lost"], payload["buffering"]) == (False, False)


async def test_a_lagging_station_is_buffering_not_losing_and_says_how_far_behind() -> (
    None
):
    """P1-14. The map is showing the past, and the console is told by how much;
    nothing is lost, and the console is told that too."""
    bus = RecordingBus()

    await TelemetryPublisher(bus=bus).publish_station(
        "tbilisi-base-1", LinkState.LAGGING, queue_depth=41_000, lag_s=37.456
    )

    _, payload = bus.published[0]
    assert payload["state"] == "lagging"
    assert payload["data_is_lost"] is False
    assert payload["buffering"] is True
    assert payload["lag_s"] == 37.5


async def test_a_station_with_nothing_stored_has_no_lag() -> None:
    """None, never zero: zero would claim the map is perfectly current."""
    bus = RecordingBus()
    await TelemetryPublisher(bus=bus).publish_station(
        "tbilisi-base-1", LinkState.HEALTHY
    )
    _, payload = bus.published[0]
    assert payload["lag_s"] is None


# --- unclaimed sources -----------------------------------------------------


async def test_an_unclaimed_source_is_published_so_it_can_be_acted_on() -> None:
    """The registration race, made visible.

    An aircraft transmitting with no binding is recoverable, and publishing it
    is what turns it into something an operator can fix now rather than find
    later.
    """
    bus = RecordingBus()
    resolution = Resolution(source_id=ADDRESS, drone_id=None, unclaimed_reason=UNBOUND)

    await TelemetryPublisher(bus=bus).publish_unclaimed(
        "tbilisi-base-1", resolution, ADDRESS
    )

    subject, payload = bus.published[0]
    assert subject == "events.unclaimed_source"
    assert payload["sysid"] == 1
    assert payload["compid"] == 1
    assert payload["reason"] == UNBOUND


async def test_a_rejected_source_is_published_on_its_own_subject() -> None:
    """P1-07. The console must not show "no binding - register to track" for
    an address bound on another station: registering it would bless a spoof."""
    bus = RecordingBus()
    resolution = Resolution(
        source_id=ADDRESS, drone_id=None, unclaimed_reason=NOT_ASSIGNED
    )

    await TelemetryPublisher(bus=bus).publish_unclaimed(
        "tbilisi-base-1", resolution, ADDRESS
    )

    subject, payload = bus.published[0]
    assert subject == "events.rejected_source"
    assert payload["reason"] == NOT_ASSIGNED


# --- failure does not stop ingest ------------------------------------------


async def test_a_bus_failure_does_not_raise() -> None:
    """The bus carries the present; the archive carries the record.

    Both the archive and the hypertable have already been written by the time
    anything reaches here, so a publish failure must not propagate back into
    the ingest path and stall a station.
    """
    bus = RecordingBus(fail=True)

    await TelemetryPublisher(bus=bus).publish_row(row())
    await TelemetryPublisher(bus=bus).publish_station(
        "tbilisi-base-1", LinkState.HEALTHY
    )

    assert bus.published == []


async def test_a_batch_publishes_one_message_per_row() -> None:
    bus = RecordingBus()
    rows = [row(drone_id=uuid4()) for _ in range(3)]

    await TelemetryPublisher(bus=bus).publish_rows(rows)

    assert len(bus.published) == 3
    assert {subject for subject, _ in bus.published} == {
        telemetry_subject(r.drone_id) for r in rows
    }

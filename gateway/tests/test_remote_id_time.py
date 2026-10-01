"""Remote ID positions placed at the broadcast's own capture time. S-27.

The Location timestamp is tenths of a second after the full UTC hour; the
hour comes from the Gateway's receive time. The frames are built with the
encoder that tests/test_odid.py pins to the reference library's bytes.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pymavlink.dialects.v20 import common as mavlink

from airspace.monitor import track_from_telemetry
from gateway import odid
from gateway.remote_id import (
    TIME_SOURCE_BROADCAST,
    TIME_SOURCE_RECEIVER,
    RemoteIdTracker,
    broadcast_time,
    place,
    ts_accuracy_s,
)
from gateway.tests.rid_frames import FlatGeoid, basic, frame, location, pack

RECEIVED = datetime(2026, 10, 1, 12, 34, 56, 700_000, tzinfo=UTC)
# RECEIVED as seconds after its hour.
RECEIVED_IN_HOUR_S = 34 * 60 + 56.7


def decoded(**fields: object) -> odid.Location:
    return odid.decode_location(location(**fields))  # type: ignore[arg-type]


def observe(payload: bytes, received_at: datetime) -> dict[str, object]:
    tracker = RemoteIdTracker(geoid=FlatGeoid())
    seen = tracker.take(
        frame(pack(basic(), payload), received_at=received_at), now_s=0.0
    )
    assert seen is not None
    return seen


# --- what the fields mean, pinned against pymavlink's copy of the enums ------


def test_timestamp_accuracy_is_in_tenths_of_a_second_and_zero_is_unknown() -> None:
    """MAV_ODID_TIME_ACC mirrors the ODID enum: 0 unknown, k is k/10 s."""
    names = mavlink.enums["MAV_ODID_TIME_ACC"]
    assert names[0].name == "MAV_ODID_TIME_ACC_UNKNOWN"
    assert ts_accuracy_s(decoded(ts_accuracy=0)) is None
    for code in range(1, 16):
        assert names[code].name == (
            f"MAV_ODID_TIME_ACC_{code // 10}_{code % 10}_SECOND"
        )
        assert ts_accuracy_s(decoded(ts_accuracy=code)) == pytest.approx(code / 10)


def test_the_unknown_timestamp_sentinel_decodes_to_none() -> None:
    """0xFFFF, as gateway.odid pins it against the reference library."""
    unknown = location(seconds_after_hour=None)
    # Where the timestamp is, found by diffing two frames that differ only
    # in it (CLAUDE.md), not counted from the struct.
    zero = location(seconds_after_hour=0.0)
    late = location(seconds_after_hour=6553.4 / 2)
    offsets = [i for i, (a, b) in enumerate(zip(zero, late, strict=True)) if a != b]
    assert len(offsets) == 2
    assert unknown[offsets[0] : offsets[-1] + 1] == b"\xff\xff"
    assert decoded(seconds_after_hour=None).seconds_after_hour is None


# --- reconstruction ------------------------------------------------------------


def test_the_hour_is_taken_from_the_receive_time() -> None:
    moment = broadcast_time(RECEIVED_IN_HOUR_S - 0.4, RECEIVED, ahead_s=1.0)

    assert moment == RECEIVED - timedelta(seconds=0.4)


def test_a_broadcast_just_before_the_hour_heard_just_after_it() -> None:
    """xx:59:59.9 heard at (xx+1):00:00.2 is in hour xx, not 59 min ahead."""
    received = datetime(2026, 10, 1, 13, 0, 0, 200_000, tzinfo=UTC)

    moment = broadcast_time(3599.9, received, ahead_s=1.0)

    assert moment == datetime(2026, 10, 1, 12, 59, 59, 900_000, tzinfo=UTC)


def test_a_broadcast_just_after_the_hour_heard_just_before_it() -> None:
    """An aircraft clock a little ahead of ours, across the hour."""
    received = datetime(2026, 10, 1, 12, 59, 59, 900_000, tzinfo=UTC)

    moment = broadcast_time(0.5, received, ahead_s=1.0)

    assert moment == datetime(2026, 10, 1, 13, 0, 0, 500_000, tzinfo=UTC)


def test_a_time_ahead_by_more_than_the_tolerance_falls_in_the_previous_hour() -> None:
    moment = broadcast_time(RECEIVED_IN_HOUR_S + 3.0, RECEIVED, ahead_s=1.0)

    assert moment == RECEIVED + timedelta(seconds=3.0) - timedelta(hours=1)


# --- placement -----------------------------------------------------------------


def test_a_late_broadcast_is_placed_at_its_own_time() -> None:
    placed = place(decoded(seconds_after_hour=RECEIVED_IN_HOUR_S - 2.0), RECEIVED)

    assert placed.ts == RECEIVED - timedelta(seconds=2.0)
    assert placed.captured_at == placed.ts
    assert placed.time_source == TIME_SOURCE_BROADCAST
    assert placed.fallback is None


def test_a_broadcast_older_than_the_latency_bound_is_placed_on_arrival() -> None:
    in_time = place(
        decoded(seconds_after_hour=RECEIVED_IN_HOUR_S - 4.9),
        RECEIVED,
        max_latency_s=5.0,
    )
    too_old = place(
        decoded(seconds_after_hour=RECEIVED_IN_HOUR_S - 5.2),
        RECEIVED,
        max_latency_s=5.0,
    )

    assert in_time.time_source == TIME_SOURCE_BROADCAST
    assert too_old.fallback == "too_old"
    assert too_old.captured_at == RECEIVED
    assert too_old.time_source == TIME_SOURCE_RECEIVER
    # `ts` stays what the broadcast said.
    assert too_old.ts == RECEIVED - timedelta(seconds=5.2)


def test_the_broadcasts_own_accuracy_widens_the_bounds() -> None:
    """5.5 s late with a declared accuracy of 1.0 s is within 5 s + 1 s."""
    loose = place(
        decoded(seconds_after_hour=RECEIVED_IN_HOUR_S - 5.5, ts_accuracy=10),
        RECEIVED,
        max_latency_s=5.0,
    )
    exact = place(
        decoded(seconds_after_hour=RECEIVED_IN_HOUR_S - 5.5, ts_accuracy=1),
        RECEIVED,
        max_latency_s=5.0,
    )

    assert loose.time_source == TIME_SOURCE_BROADCAST
    assert exact.fallback == "too_old"


def test_a_clock_ahead_beyond_the_tolerance_is_not_believed() -> None:
    within = place(
        decoded(seconds_after_hour=RECEIVED_IN_HOUR_S + 0.8),
        RECEIVED,
        tolerance_s=1.0,
    )
    beyond = place(
        decoded(seconds_after_hour=RECEIVED_IN_HOUR_S + 3.0),
        RECEIVED,
        tolerance_s=1.0,
    )

    assert within.captured_at == RECEIVED + timedelta(seconds=0.8)
    assert within.time_source == TIME_SOURCE_BROADCAST
    # Ahead, not an hour old: counted as such, and `ts` is what it claims.
    assert beyond.fallback == "clock_ahead"
    assert beyond.ts == RECEIVED + timedelta(seconds=3.0)
    assert beyond.captured_at == RECEIVED
    assert beyond.time_source == TIME_SOURCE_RECEIVER


@pytest.mark.parametrize(
    ("ahead_s", "fallback"),
    [
        # Ahead of the Gateway: the module's clock fast, or ours slow.
        (2.0, "clock_ahead"),
        (29 * 60.0, "clock_ahead"),
        # Behind it beyond the latency bound: old, not ahead.
        (-10.0, "too_old"),
        (-29 * 60.0, "too_old"),
    ],
)
def test_ahead_and_too_old_are_told_apart(ahead_s: float, fallback: str) -> None:
    seconds = (RECEIVED_IN_HOUR_S + ahead_s) % 3600.0

    placed = place(decoded(seconds_after_hour=seconds), RECEIVED, tolerance_s=1.0)

    assert placed.fallback == fallback
    assert placed.captured_at == RECEIVED


def test_an_unknown_timestamp_is_placed_on_arrival() -> None:
    placed = place(decoded(seconds_after_hour=None), RECEIVED)

    assert placed.ts == RECEIVED
    assert placed.captured_at == RECEIVED
    assert placed.fallback == "unknown"


def test_a_timestamp_past_the_hour_is_invalid() -> None:
    # The encoder clamps to 36000 tenths, the largest the field may hold;
    # 3600.0 s is not inside any hour.
    placed = place(decoded(seconds_after_hour=3600.0), RECEIVED)

    assert placed.fallback == "invalid"
    assert placed.captured_at == RECEIVED


# --- on the bus and in the monitor ---------------------------------------------


def test_the_observation_carries_all_three_times() -> None:
    seen = observe(
        location(seconds_after_hour=RECEIVED_IN_HOUR_S - 1.5, ts_accuracy=3),
        RECEIVED,
    )

    broadcast = (RECEIVED - timedelta(seconds=1.5)).isoformat()
    assert seen["ts"] == broadcast
    assert seen["captured_at"] == broadcast
    assert seen["rx_ts"] == RECEIVED.isoformat()
    assert seen["backlog"] is False
    rid = seen["remote_id"]
    assert isinstance(rid, dict)
    assert rid["time_source"] == TIME_SOURCE_BROADCAST
    assert rid["ts_accuracy_s"] == pytest.approx(0.3)


def test_fallbacks_are_counted_by_reason() -> None:
    tracker = RemoteIdTracker(geoid=FlatGeoid())
    for seconds in (None, None, RECEIVED_IN_HOUR_S - 30.0):
        tracker.take(
            frame(
                pack(basic(), location(seconds_after_hour=seconds)),
                received_at=RECEIVED,
            ),
            now_s=0.0,
        )
    tracker.take(
        frame(
            pack(basic(), location(seconds_after_hour=RECEIVED_IN_HOUR_S)),
            received_at=RECEIVED,
        ),
        now_s=0.0,
    )

    assert tracker.time_fallbacks == {"unknown": 2, "too_old": 1}


def test_the_monitor_places_a_late_broadcast_at_its_broadcast_time() -> None:
    """S-27's done-when, in-process: 2.5 s of receiver latency."""
    seen = observe(location(seconds_after_hour=RECEIVED_IN_HOUR_S - 2.5), RECEIVED)

    track = track_from_telemetry(seen, arrived_at_s=RECEIVED.timestamp() + 0.1)

    assert track is not None
    assert track.captured_at_s == pytest.approx(RECEIVED.timestamp() - 2.5)

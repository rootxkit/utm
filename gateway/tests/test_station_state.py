"""Station link state: the distinction spec §9 says is most easily got wrong.

Every "is not data loss" assertion here is paired with one that makes the loss
actually happen and checks it is reported. Asserting only that a state is *not*
`data_lost` would pass just as well against a tracker that can never produce
`data_lost` at all - which is the failure this repository has shipped three
times and now has a rule about.
"""

from __future__ import annotations

from typing import Any

from gateway.relay_messages import Gap, Status
from gateway.station_state import (
    MAX_RETAINED_LOSSES,
    LinkState,
    LossKind,
    StationLinkTracker,
)

EPOCH = "9f2c1b7d4e6a58039ab1c2d3e4f50617"


def status(**overrides: Any) -> Status:
    base: dict[str, Any] = {
        "queue_depth": 0,
        "queue_bytes": 0,
        "dropped_intake_total": 0,
        "dropped_cap_total": 0,
        "last_datagram_age_ms": 38,
        "uptime_s": 100,
        "monotonic_ns": 1,
        "utc_ns": 2,
    }
    return Status(**{**base, **overrides})


def tracker() -> StationLinkTracker:
    return StationLinkTracker(station_id="tbilisi-base-1")


# --- healthy ---------------------------------------------------------------


def test_status_arriving_with_recent_datagrams_is_healthy() -> None:
    station = tracker()
    station.observe_status(status(), now_s=10.0)
    assert station.state(now_s=10.5) == LinkState.HEALTHY


def test_a_station_that_has_sent_nothing_yet_is_unreachable_not_healthy() -> None:
    assert tracker().state(now_s=0.0) == LinkState.UNREACHABLE


# --- unreachable, and what it is not ---------------------------------------


def test_three_missed_status_messages_is_unreachable() -> None:
    station = tracker()
    station.observe_status(status(), now_s=10.0)
    assert station.state(now_s=13.5) == LinkState.UNREACHABLE


def test_unreachable_is_not_data_lost() -> None:
    """The whole point of §9.

    A station that has stopped talking to us is almost certainly buffering, and
    the record completes on reconnect. Nothing is deleted at the relay, because
    no acknowledgement can arrive through a dead link.
    """
    station = tracker()
    station.observe_status(status(), now_s=10.0)

    assert station.state(now_s=60.0) == LinkState.UNREACHABLE
    assert list(station.losses) == []


def test_the_disagreement_window_never_reports_loss() -> None:
    """relay-v1 §8: the Gateway gives up at ~3 s, the relay not until 25 s.

    Across that entire window the relay is alive and buffering. Sampling every
    second and finding no loss at any point is the assertion; the paired test
    below shows the tracker is capable of reporting loss when there is some.
    """
    station = tracker()
    station.observe_status(status(), now_s=0.0)

    for elapsed_s in range(1, 26):
        assert station.state(now_s=float(elapsed_s)) in {
            LinkState.HEALTHY,
            LinkState.UNREACHABLE,
        }
    assert list(station.losses) == []


def test_a_station_that_returns_becomes_healthy_again() -> None:
    station = tracker()
    station.observe_status(status(), now_s=0.0)
    assert station.state(now_s=10.0) == LinkState.UNREACHABLE

    station.observe_status(status(uptime_s=110), now_s=10.0)
    assert station.state(now_s=10.2) == LinkState.HEALTHY
    assert list(station.losses) == []


# --- radio silent ----------------------------------------------------------


def test_a_climbing_datagram_age_is_radio_silent_not_unreachable() -> None:
    """The station is talking to us; it has lost the aircraft.

    A different failure domain from `unreachable`, and a flight-safety event
    rather than a tracking outage.
    """
    station = tracker()
    station.observe_status(status(last_datagram_age_ms=9_000), now_s=10.0)
    assert station.state(now_s=10.5) == LinkState.RADIO_SILENT


def test_radio_silent_is_not_data_lost() -> None:
    """Loss is upstream of the relay: the datagrams were never received.

    Nothing the relay held has gone missing, so the flight record has no hole
    that reconnecting could fill - but no records were destroyed either, and
    saying otherwise would attribute the radio's failure to the queue.
    """
    station = tracker()
    station.observe_status(status(last_datagram_age_ms=30_000), now_s=10.0)
    assert station.state(now_s=10.5) == LinkState.RADIO_SILENT
    assert list(station.losses) == []


def test_no_datagram_ever_received_is_not_radio_silent() -> None:
    """`last_datagram_age_ms` is null before any datagram arrives.

    That is a station sitting on the ground with no aircraft powered up, which
    is normal, not a link failure.
    """
    station = tracker()
    station.observe_status(status(last_datagram_age_ms=None), now_s=10.0)
    assert station.state(now_s=10.5) == LinkState.HEALTHY


# --- data lost: the three things that actually mean it ---------------------


def test_a_gap_is_data_lost_with_its_exact_range() -> None:
    station = tracker()
    station.observe_status(status(), now_s=10.0)

    loss = station.observe_gap(
        Gap(epoch=EPOCH, from_seq=58120, to_seq=61099, reason="queue_cap"),
        now_s=10.0,
    )

    assert station.state(now_s=10.5) == LinkState.DATA_LOST
    assert loss.kind is LossKind.QUEUE_CAP
    assert (loss.from_seq, loss.to_seq) == (58120, 61099)
    assert loss.datagram_count == 2979


def test_an_intake_drop_delta_is_data_lost_with_no_sequence_range() -> None:
    """§11 loss #2. No gap can describe it: the drop preceded numbering."""
    station = tracker()
    station.observe_status(status(dropped_intake_total=0), now_s=10.0)
    found = station.observe_status(status(dropped_intake_total=40), now_s=11.0)

    assert len(found) == 1
    assert found[0].kind is LossKind.INTAKE_DROP
    assert found[0].datagram_count == 40
    assert found[0].from_seq is None
    assert station.state(now_s=11.5) == LinkState.DATA_LOST


def test_uptime_going_backwards_is_a_relay_restart() -> None:
    """§11 loss #4. Records still in the relay's memory went with it."""
    station = tracker()
    station.observe_status(status(uptime_s=7321), now_s=10.0)
    found = station.observe_status(status(uptime_s=4), now_s=11.0)

    assert [loss.kind for loss in found] == [LossKind.RELAY_RESTART]
    assert station.state(now_s=11.5) == LinkState.DATA_LOST


def test_the_first_status_establishes_a_baseline_and_reports_nothing() -> None:
    """A non-zero counter on connect is history, not a loss happening now.

    §11 says the delta localises loss to a one-second window; it does not
    reconstruct what happened before we were listening. Reporting the absolute
    value as a fresh loss would re-report every past drop on every reconnect.
    """
    station = tracker()
    found = station.observe_status(status(dropped_intake_total=900), now_s=10.0)
    assert found == []
    assert station.state(now_s=10.5) == LinkState.HEALTHY


def test_a_steady_drop_counter_is_not_a_new_loss() -> None:
    station = tracker()
    station.observe_status(status(dropped_intake_total=900), now_s=10.0)
    found = station.observe_status(status(dropped_intake_total=900), now_s=11.0)
    assert found == []


def test_data_lost_outranks_unreachable() -> None:
    """A station can lose data and go unreachable a moment later.

    If the outage masked the loss, the console would show the reassuring half
    of the truth: "buffering, nothing lost", about a station that has already
    lost 40 datagrams.
    """
    station = tracker()
    station.observe_status(status(dropped_intake_total=0), now_s=10.0)
    station.observe_status(status(dropped_intake_total=40), now_s=11.0)

    # Well past the unreachable threshold.
    assert station.state(now_s=20.0) == LinkState.DATA_LOST


def test_the_loss_record_outlives_the_state() -> None:
    """The state field eventually returns to the link's condition.

    The loss does not go away with it: it stays in `losses` so the flight
    record still shows the hole. P10-03 replay renders it explicitly rather
    than interpolating across it.
    """
    station = tracker()
    station.observe_status(status(dropped_intake_total=0), now_s=0.0)
    station.observe_status(status(dropped_intake_total=40), now_s=1.0)
    assert station.state(now_s=2.0) == LinkState.DATA_LOST

    station.observe_status(status(dropped_intake_total=40), now_s=100.0)
    assert station.state(now_s=100.5) == LinkState.HEALTHY
    assert len(station.losses) == 1
    assert station.losses[0].datagram_count == 40


def test_ignored_messages_are_counted() -> None:
    station = tracker()
    station.observe_ignored_message()
    station.observe_ignored_message()
    assert station.ignored_message_count == 2


# --- lagging (P1-14) -------------------------------------------------------
#
# A backlog that is not clearing: the newest stored record is old AND the
# relay's queue is growing. Each condition is exercised alone, to show neither
# is enough, and together, to show the state is actually produced.

NOW_NS = 1_790_000_000_000_000_000
SECOND_NS = 1_000_000_000


def fed(depths: list[int], *, stored_lag_s: float) -> StationLinkTracker:
    """A tracker that has seen one status per second with these depths, and
    whose newest stored record is `stored_lag_s` old at the last of them."""
    station = tracker()
    for n, depth in enumerate(depths):
        station.observe_status(status(queue_depth=depth), now_s=10.0 + n)
    station.observe_stored(NOW_NS - int(stored_lag_s * SECOND_NS))
    return station


def last_s(depths: list[int]) -> float:
    return 10.0 + len(depths) - 1


GROWING = [100, 900, 1_700, 2_500, 3_300, 4_100]
CLEARING = list(reversed(GROWING))


def test_an_old_backlog_that_keeps_growing_is_lagging() -> None:
    station = fed(GROWING, stored_lag_s=40.0)
    assert station.state(now_s=last_s(GROWING), now_utc_ns=NOW_NS) is LinkState.LAGGING
    assert station.lag_s(now_utc_ns=NOW_NS) == 40.0


def test_lagging_is_not_data_lost() -> None:
    """The paired absence: a lagging station has lost nothing, and a real loss
    on the same station still wins."""
    station = fed(GROWING, stored_lag_s=40.0)
    assert station.state(now_s=last_s(GROWING), now_utc_ns=NOW_NS) is not (
        LinkState.DATA_LOST
    )

    station.observe_status(
        status(queue_depth=5_000, dropped_intake_total=3),
        now_s=last_s(GROWING) + 1,
    )
    assert (
        station.state(now_s=last_s(GROWING) + 1, now_utc_ns=NOW_NS)
        is LinkState.DATA_LOST
    )


def test_a_growing_queue_that_is_still_fresh_is_healthy() -> None:
    """The queue grows for a moment after every reconnect; that alone is not
    a backlog the Gateway is failing to clear."""
    station = fed(GROWING, stored_lag_s=2.0)
    assert station.state(now_s=last_s(GROWING), now_utc_ns=NOW_NS) is LinkState.HEALTHY


def test_an_old_backlog_that_is_clearing_is_not_lagging() -> None:
    """Catching up after an outage is the system working. It is also the
    guard against a station clock that runs slow: that makes every record look
    old, but cannot make the relay's queue grow."""
    station = fed(CLEARING, stored_lag_s=40.0)
    assert station.state(now_s=last_s(CLEARING), now_utc_ns=NOW_NS) is LinkState.HEALTHY


def test_a_station_leaves_lagging_when_its_queue_stops_growing() -> None:
    station = fed(GROWING, stored_lag_s=40.0)
    assert station.state(now_s=last_s(GROWING), now_utc_ns=NOW_NS) is LinkState.LAGGING

    for n, depth in enumerate([4_000, 3_000, 2_000, 1_000, 200]):
        station.observe_status(status(queue_depth=depth), now_s=16.0 + n)

    assert station.state(now_s=20.0, now_utc_ns=NOW_NS) is LinkState.HEALTHY


def test_a_station_leaves_lagging_when_the_stored_record_is_fresh_again() -> None:
    station = fed(GROWING, stored_lag_s=40.0)
    station.observe_stored(NOW_NS - SECOND_NS)
    assert station.state(now_s=last_s(GROWING), now_utc_ns=NOW_NS) is LinkState.HEALTHY


def test_too_few_statuses_are_not_a_trend() -> None:
    depths = GROWING[:3]
    station = fed(depths, stored_lag_s=40.0)
    assert station.state(now_s=last_s(depths), now_utc_ns=NOW_NS) is LinkState.HEALTHY


def test_nothing_stored_yet_is_not_lagging() -> None:
    station = tracker()
    for n, depth in enumerate(GROWING):
        station.observe_status(status(queue_depth=depth), now_s=10.0 + n)
    assert station.lag_s(now_utc_ns=NOW_NS) is None
    assert station.state(now_s=last_s(GROWING), now_utc_ns=NOW_NS) is LinkState.HEALTHY


def test_an_older_batch_never_moves_the_stored_record_backwards() -> None:
    station = tracker()
    station.observe_stored(NOW_NS)
    station.observe_stored(NOW_NS - 60 * SECOND_NS)
    assert station.newest_stored_utc_ns == NOW_NS


def test_radio_silent_outranks_lagging() -> None:
    """Losing the aircraft is a flight-safety event; lagging is ours."""
    station = fed(GROWING, stored_lag_s=40.0)
    station.observe_status(
        status(queue_depth=5_000, last_datagram_age_ms=10_000), now_s=16.0
    )
    assert station.state(now_s=16.0, now_utc_ns=NOW_NS) is LinkState.RADIO_SILENT


def test_a_new_session_does_not_inherit_the_old_trend() -> None:
    station = fed(GROWING, stored_lag_s=40.0)
    station.start_session()
    station.observe_status(status(queue_depth=9_000), now_s=30.0)
    assert station.state(now_s=30.0, now_utc_ns=NOW_NS) is LinkState.HEALTHY


# --- the losses a tracker keeps are bounded (S-07) -------------------------


def test_losses_are_bounded_and_keep_the_newest() -> None:
    """A station dropping datagrams every second, for the life of the
    Gateway, must not grow the tracker without limit. The event log has
    every loss; the console is shown the most recent."""
    station = tracker()
    total = MAX_RETAINED_LOSSES + 50
    for n in range(total + 1):
        station.observe_status(status(dropped_intake_total=n), now_s=float(n))

    assert len(station.losses) == MAX_RETAINED_LOSSES
    # Each status dropped one more datagram, so the newest loss is the last.
    assert station.losses[-1].datagram_count == 1
    assert station.state(now_s=float(total) + 0.5) == LinkState.DATA_LOST


def test_a_tracker_under_the_bound_keeps_every_loss() -> None:
    """The presence half: nothing is dropped until the bound is reached."""
    station = tracker()
    for n in range(MAX_RETAINED_LOSSES + 1):
        station.observe_status(status(dropped_intake_total=n), now_s=float(n))

    assert len(station.losses) == MAX_RETAINED_LOSSES

"""Station link state, derived from the relay-v1 `status` stream.

`docs/specs/p1-02-gateway-ingest.md` §9 calls this the requirement most easily
got wrong, and names the mistake: presenting "we have lost the ground station"
and "the ground station has lost the aircraft" as the same thing. They are
different failure domains (`ARCHITECTURE.md` §4) with different responses. The
first leaves the pilot flying on QGC with telemetry buffering safely at the
station; the second is a flight-safety event.

The separation this module exists to preserve:

    unreachable  -> no `status` for >3 s. Almost certainly buffering. The
                    record completes on reconnect. NOT data loss.
    radio_silent -> `status` still arriving, `last_datagram_age_ms` climbing.
                    The station has lost the aircraft. Loss is upstream of
                    everything this system controls.
    data_lost    -> a `gap`, a `dropped_intake_total` delta, or `uptime_s`
                    going backwards. Only these mean telemetry is gone.
    lagging      -> P1-14. The Gateway is storing this station's records
                    more slowly than they arrive: the newest stored record is
                    older than a threshold AND the relay's queue is growing.
                    NOT data loss - the backlog is safe at the station - but
                    the map is showing the past, and without this state it
                    would look healthy while doing so.

There is a window where the Gateway and the relay disagree. The Gateway calls a
station unreachable after about 3 s; the relay does not give up on a half-open
uplink for up to 25 s (`relay-v1.md` §8). Throughout that window the relay is
alive, receiving at full rate and buffering correctly - nothing is deleted,
because no acknowledgement can arrive through a dead link. Reporting loss
during it would be reporting loss that has not happened, and P6-03 records why
that is expensive: a pilot who learns the alerts overstate things will discount
the one that does not.

**All timing here is on the Gateway's own clock**, taken at the moment a
`status` arrives. The `monotonic_ns`/`utc_ns` pair inside the message describes
the *station's* clocks, which relay-v1 §9 says may be wrong; using it to decide
whether a station is still talking to us would let a station's broken clock
declare itself healthy.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final

from gateway.relay_messages import Gap, Status


class LinkState(StrEnum):
    """What the console renders. §9's table, one value per row."""

    HEALTHY = "healthy"
    RADIO_SILENT = "radio_silent"
    UNREACHABLE = "unreachable"
    DATA_LOST = "data_lost"
    LAGGING = "lagging"


class LossKind(StrEnum):
    """Why telemetry is actually gone. Nothing else may set `DATA_LOST`."""

    # relay-v1 §11 loss #3. The `gap` carries the exact range.
    QUEUE_CAP = "queue_cap"
    # relay-v1 §11 loss #2. No gap exists: the datagrams died before a
    # sequence number was assigned, so a delta between two `status` messages
    # is the only evidence they ever existed.
    INTAKE_DROP = "intake_drop"
    # relay-v1 §11 loss #4. `uptime_s` going backwards means the relay
    # restarted, and whatever was still in its memory went with it.
    RELAY_RESTART = "relay_restart"


@dataclass(frozen=True, slots=True)
class LossEvent:
    """One observation that telemetry is gone, with its extent where known."""

    kind: LossKind
    # Populated for a gap, where the protocol gives an exact range. An intake
    # drop knows only a count, and a relay restart knows neither - which is
    # itself the finding, and why the count is optional rather than zero.
    from_seq: int | None = None
    to_seq: int | None = None
    datagram_count: int | None = None
    detail: str = ""


@dataclass(slots=True)
class StationLinkTracker:
    """Folds a station's `status` stream into a link state.

    One instance per connected station. Fed by the relay-v1 server; queried by
    whatever publishes station state to the console.
    """

    station_id: str

    # relay-v1 §8: "three consecutive missed `status` messages", at 1 s each.
    # The protocol fixes the count; the interval is the relay's cadence.
    unreachable_after_s: float = 3.0
    # `last_datagram_age_ms` above this means the station is no longer hearing
    # the aircraft. Not fixed by the protocol - it depends on the slowest
    # stream the fleet emits - so it is a setting, not a constant.
    radio_silent_after_ms: int = 3_000
    # How long a loss keeps the published state at `data_lost`. A loss is
    # permanent; this only governs how long it dominates the state field,
    # after which the link state resumes and the loss remains in `losses`.
    loss_holds_state_for_s: float = 60.0
    # P1-14. How old the newest stored record may be before the station
    # counts as lagging. The Gateway passes its link timeout: past that age a
    # record can no longer make a drone live (P1-05), so that is exactly when
    # every aircraft on the station starts to read as link lost, and the
    # console needs to say why.
    lagging_after_s: float = 15.0
    # P1-14. The queue counts as growing when its depth now exceeds its depth
    # this many `status` messages ago. Over several seconds rather than one,
    # because depth moves in steps of a batch and a single comparison would
    # call every other second a trend.
    depth_trend_statuses: int = 5

    last_status_at_s: float | None = field(default=None, init=False)
    last_status: Status | None = field(default=None, init=False)
    losses: list[LossEvent] = field(default_factory=list, init=False)
    last_loss_at_s: float | None = field(default=None, init=False)
    ignored_message_count: int = field(default=0, init=False)
    # P1-14. `recv_utc_ns` of the newest record stored for this station, on
    # the station's clock.
    newest_stored_utc_ns: int | None = field(default=None, init=False)
    _depths: deque[int] = field(default_factory=deque, init=False)

    def observe_status(self, status: Status, *, now_s: float) -> list[LossEvent]:
        """Record a `status` and return any loss it reveals.

        Losses are detected from the *delta* against the previous `status`, so
        the first one from a connection establishes a baseline and reports
        nothing. A relay that has been dropping datagrams since before we
        connected is not something this message can tell us about; §11 is
        explicit that the delta localises loss to a one-second window, not that
        it reconstructs history.
        """
        previous = self.last_status
        found: list[LossEvent] = []

        if previous is not None:
            found.extend(self._losses_between(previous, status))

        self.last_status = status
        self.last_status_at_s = now_s
        self._depths.append(status.queue_depth)
        while len(self._depths) > self.depth_trend_statuses + 1:
            self._depths.popleft()
        for loss in found:
            self._record(loss, now_s=now_s)
        return found

    def observe_gap(self, gap: Gap, *, now_s: float) -> LossEvent:
        """Record a `gap`. This is data loss with a known extent."""
        loss = LossEvent(
            kind=LossKind.QUEUE_CAP,
            from_seq=gap.from_seq,
            to_seq=gap.to_seq,
            datagram_count=gap.missing_count,
            detail=gap.reason,
        )
        self._record(loss, now_s=now_s)
        return loss

    def observe_stored(self, newest_recv_utc_ns: int) -> None:
        """Record that records up to this capture time are durably stored."""
        if (
            self.newest_stored_utc_ns is None
            or newest_recv_utc_ns > self.newest_stored_utc_ns
        ):
            self.newest_stored_utc_ns = newest_recv_utc_ns

    def start_session(self) -> None:
        """Forget the depth trend of a previous connection.

        Depth on either side of a reconnect is not a trend: the queue grew
        because nothing was being sent, which `unreachable` already said.
        """
        self._depths.clear()

    def lag_s(self, *, now_utc_ns: int) -> float | None:
        """How far behind this station the stored record is, in seconds.

        `now_utc_ns` is the Gateway's clock and `recv_utc_ns` the station's,
        so a station clock that is wrong shifts this by its error (relay-v1
        §9). That is why lag alone never makes a station `lagging`: the queue
        must be growing too, which is measured entirely on the station's side
        and needs no clock agreement.
        """
        if self.newest_stored_utc_ns is None:
            return None
        return (now_utc_ns - self.newest_stored_utc_ns) / 1e9

    @property
    def queue_growing(self) -> bool:
        depths = self._depths
        return len(depths) > self.depth_trend_statuses and depths[-1] > depths[0]

    def observe_ignored_message(self) -> None:
        """Count a control message this Gateway does not understand.

        relay-v1 §14 requires ignoring it. Counting it means a relay speaking a
        newer dialect is visible rather than merely tolerated.
        """
        self.ignored_message_count += 1

    def state(self, *, now_s: float, now_utc_ns: int | None = None) -> LinkState:
        """The state to publish, given the time now.

        Takes the time rather than reading a clock so that "no status for 3 s"
        is a decision about elapsed time, not about when this happens to be
        called. `now_utc_ns` is needed only to judge `lagging`; without it
        that state is not considered.
        """
        if (
            self.last_loss_at_s is not None
            and now_s - self.last_loss_at_s < self.loss_holds_state_for_s
        ):
            # Deliberately ahead of `unreachable`. A station can lose data and
            # then go unreachable seconds later, and if the outage masked the
            # loss the console would show the reassuring half of the truth.
            return LinkState.DATA_LOST

        if self.last_status_at_s is None:
            # Connected, no status yet. Not healthy - nothing has been heard -
            # and not data loss either.
            return LinkState.UNREACHABLE

        if now_s - self.last_status_at_s > self.unreachable_after_s:
            return LinkState.UNREACHABLE

        status = self.last_status
        if (
            status is not None
            and status.last_datagram_age_ms is not None
            and status.last_datagram_age_ms >= self.radio_silent_after_ms
        ):
            return LinkState.RADIO_SILENT

        # After `radio_silent`, which is a flight-safety event on the station's
        # side; this one is a capacity problem on ours.
        if now_utc_ns is not None and self.queue_growing:
            lag = self.lag_s(now_utc_ns=now_utc_ns)
            if lag is not None and lag > self.lagging_after_s:
                return LinkState.LAGGING

        # `last_datagram_age_ms` is None when no datagram has ever arrived.
        # That is a station whose radio has told it nothing yet, which is the
        # normal state on the ground before an aircraft is powered up, so it
        # is not promoted to `radio_silent` here.
        return LinkState.HEALTHY

    def _losses_between(self, previous: Status, current: Status) -> list[LossEvent]:
        found: list[LossEvent] = []

        # §11 loss #4. Checked first: a restart explains a counter moving
        # oddly, and the counters are documented as persisted across restarts,
        # so a backwards `uptime_s` is the reliable signal rather than a
        # counter reset.
        if current.uptime_s < previous.uptime_s:
            found.append(
                LossEvent(
                    kind=LossKind.RELAY_RESTART,
                    detail=(
                        f"uptime_s went backwards: {previous.uptime_s} -> "
                        f"{current.uptime_s}; records still in relay memory "
                        f"were lost"
                    ),
                )
            )

        intake_delta = current.dropped_intake_total - previous.dropped_intake_total
        if intake_delta > 0:
            found.append(
                LossEvent(
                    kind=LossKind.INTAKE_DROP,
                    datagram_count=intake_delta,
                    detail=(
                        f"{intake_delta} datagram(s) dropped before a sequence "
                        f"number was assigned; no gap can describe them"
                    ),
                )
            )

        return found

    def _record(self, loss: LossEvent, *, now_s: float) -> None:
        self.losses.append(loss)
        self.last_loss_at_s = now_s


# A station that has connected but sent nothing is reported as unreachable
# rather than healthy. Exported so the console and its tests agree on it.
INITIAL_STATE: Final = LinkState.UNREACHABLE

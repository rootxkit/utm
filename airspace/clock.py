"""Per-source clock offsets, so a wrong ground-station clock costs no alerts.

A telemetry message's `ts` is the capture clock of whoever received the
frame: on the relay path the ground PC's `recv_utc_ns`, which relay-v1 §9
says may be wrong, drifting or stepped; on the Remote ID path the Gateway's
own receive time. Comparing `ts` with the monitor's wall clock therefore
measures skew plus delivery delay, and the two cannot be told apart from one
message. They can be told apart over time: skew is constant (or nearly), and
delay varies. So this keeps, per source, a running minimum of `wall - ts`
over recent messages as the source's offset, and reports only the *excess*
delay of each message above that minimum. A constant skew of any size is
absorbed; a replayed backlog, whose `ts` is far older than the source's
normal delay, stands out.

The minimum relaxes upwards at `relax_s_per_s` so a clock stepped backwards,
or a delivery path that has genuinely become slower, is absorbed within
tens of seconds to minutes rather than never. A backlog replay drains faster
than real time, so its excess falls faster than the relaxation could hide it.

The first message from a source establishes its offset, and so is never
judged a backlog: without data the monitor evaluates rather than drops.

The offset also puts every source on one clock: `ts + offset` is the capture
time as the monitor's wall clock would have read it (to within the source's
minimum delivery delay), which is what lets two aircraft on two ground
stations be advanced to a common instant (`cpa.advance`).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class Delay:
    # `wall - ts` at the source's best, the estimated clock offset.
    offset_s: float
    # This message's `wall - ts` above the offset: real delivery delay.
    excess_s: float


@dataclass
class SourceClocks:
    relax_s_per_s: float

    _offset_s: dict[str, float] = field(default_factory=dict, init=False)
    _seen_at_s: dict[str, float] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        if self.relax_s_per_s < 0:
            raise ValueError("relax_s_per_s must not be negative")

    def observe(self, source: str, *, wall_s: float, ts_s: float) -> Delay:
        observed_s = wall_s - ts_s
        previous = self._offset_s.get(source)
        if previous is None:
            offset_s = observed_s
        else:
            elapsed_s = max(0.0, wall_s - self._seen_at_s[source])
            offset_s = min(observed_s, previous + self.relax_s_per_s * elapsed_s)
        self._offset_s[source] = offset_s
        self._seen_at_s[source] = wall_s
        return Delay(offset_s=offset_s, excess_s=observed_s - offset_s)

    def offset_s(self, source: str) -> float | None:
        return self._offset_s.get(source)

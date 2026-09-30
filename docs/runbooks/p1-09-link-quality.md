# P1-09 link quality under simulated packet loss

P1-09's criterion is that *the metric degrades measurably under simulated
packet loss.* The metric is loss per vehicle from the MAVLink sequence number,
plus the longest heartbeat gap, over a 10 s window. It is published with every
telemetry message and shown on the map.

## Result, 2026-09-28

11 ArduCopter SITL aircraft ran headless and bound, through the relay and the
Gateway. Between SITL and the relay sat a UDP forwarder that dropped nothing
for 70 s, then dropped each datagram with probability 0.2 for 70 s. The first
20 s of each phase were skipped, because the metric's window is 10 s.

| Phase | Forwarder actually dropped | Measured loss, median of 11 | Per aircraft | Longest heartbeat gap |
|---|---|---|---|---|
| clean | 0 of 83,998 (0%) | **0.0%** | 0.0 - 0.36% | 1.05 s |
| lossy | 16,876 of 84,075 (20.07%) | **20.16%** | 18.8 - 21.2% | 3.98 s |

The measured loss tracks the drop rate to a tenth of a percentage point.

The clean phase also shows that SITL's and MAVProxy's sequence numbers are
continuous end to end. Had anything on the path re-sequenced frames, a healthy
link would have reported loss.

Some aircraft show 0.18-0.36% in the clean phase: one or two frames per
window, with nothing dropped by the forwarder. Loss is measured on the whole
path, so this is real loss somewhere between SITL and the Gateway, not an
artefact of the forwarder. It was not investigated further.

## Procedure

As in `p1-05-link-loss.md`, with two differences:

- `SITL_QGC_PORT=14446`;
- a forwarder from 14446 to the relay's 14445.

A NATS subscriber on `telemetry.*` collects each message's `link` object and
takes medians per phase.

## Not measured: round-trip latency

A round trip needs something sent to the aircraft and answered. The system is
receive-only, so RTT is not reported, rather than estimated in a way nothing
could check. It is dropped: the Gateway never sends.

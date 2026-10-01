# airspace

The airspace monitor (`python -m airspace`): closest point of approach between
aircraft, zone incursions and the height limit, raised as alerts to people. It
never commands an aircraft.

Advice must be deterministic — the same conflict evaluated twice produces an
identical answer, so that two operators looking at two consoles are told
compatible things. See `docs/ARCHITECTURE.md` §6.

Safety-relevant: `mypy --strict` and an 80% coverage target apply here.
Alerting changes are validated by scenarios in `sim/scenarios/`, not by unit
tests alone.

## Time (S-11)

Every telemetry message is evaluated at its capture time, `ts`, not when it
arrived. What `ts` is depends on the path:

- **Relay (MAVLink):** the ground PC's `recv_utc_ns`, the moment the relay
  received the frame. relay-v1 §9 says that clock may be wrong, drifting or
  stepped, and nothing corrects it upstream.
- **Remote ID:** the broadcast's own capture time (`gateway/remote_id.py`,
  S-27). The Gateway also sets `captured_at` to it when it is plausible, so
  a Remote ID position is placed when the aircraft measured it, not when it
  reached the Gateway.

Neither clock is trusted for placing an aircraft in time. Every message also
carries `rx_ts`, when the Gateway received the batch on its own clock;
`captured_at`, where the Gateway placed the row on that clock (behind
`rx_ts` by its spacing from the batch's newest record, so a draining
relay's frame is not one instant); and `backlog`, the Gateway's verdict
that the record is not the present, either queued on the relay before the
session that delivered it or delivered while the relay was still draining
(`gateway/README.md`, relay-v1 §5, §6, §8). A track is placed at
`captured_at`: one clock for every station, so two aircraft on two ground
stations are compared at one instant with no skew to guess. A
message flagged `backlog` is counted and not evaluated for live alerts. A
station clock that is wrong by any amount costs no alerts, and a Gateway
that is behind yields late alerts placed at `rx_ts`, not none.
`LIVE_MAX_AGE_S` applies only to `wall - rx_ts`, the Gateway-to-monitor
leg. `ts` orders samples within one source: one older than the last that
source gave, and not received later, is out of order and ignored. A message
without `rx_ts` is placed at its arrival time and counted; per-source state
is bounded by `SOURCE_STATE_MAX`. A pair whose neighbour sample is older
than `NEIGHBOUR_MAX_AGE_S` is not judged by that message: neither refreshed
nor cleared, because silence is not evidence.

Two Remote ID tracks with the same transmitter address
(`remote_id.transmitter`) are never paired: the Gateway publishes a
transmitter as unidentified while it has no fresh identity and under its
serial once it has one (S-32), so they are one radio under two ids.

Rejected messages, failed checks, unreadable tiles and audit-queue losses are
counted and logged in the `airspace monitor status` line every minute.

## Settings

`DATABASE_URL`, `NATS_URL`, `TERRAIN_DIR` as in `common/config.py`, plus,
all in `airspace/config.py` with their defaults and reasons: `LIVE_MAX_AGE_S`,
`NEIGHBOUR_MAX_AGE_S`, `SOURCE_STATE_MAX`, `AUDIT_QUEUE_SIZE`,
`AUDIT_CLOSE_TIMEOUT_S`, `TERRAIN_CACHE_TILES`. Separation minima and the
height limit are policy in the database, never here.

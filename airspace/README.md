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
- **Remote ID:** the Gateway's own receive time (`gateway/remote_id.py`).
  The broadcast's `seconds_after_hour` is decoded (`gateway/odid.py`) but not
  yet carried, so a Remote ID position is stamped when it reached the
  Gateway, not when the aircraft measured it. Carrying the broadcast time is
  a Gateway follow-up.

Neither clock is trusted as such. `airspace/clock.py` keeps, per source
(`station_id`), a running minimum of `wall - ts` as that source's offset,
relaxing at `CLOCK_RELAX_S_PER_S`; a message is a replayed backlog only when
its delay *above* that offset exceeds `LIVE_MAX_AGE_S`. A clock that is
merely wrong, by any amount, costs no alerts; nor does a steady delivery
delay. Capture times are put on the monitor's clock with the offset, so two
aircraft on two ground stations are compared at one instant. A pair whose
neighbour sample is older than `NEIGHBOUR_MAX_AGE_S` is not judged by that
message: neither refreshed nor cleared, because silence is not evidence.

Rejected messages, failed checks, unreadable tiles and audit-queue losses are
counted and logged in the `airspace monitor status` line every minute.

## Settings

`DATABASE_URL`, `NATS_URL`, `TERRAIN_DIR` as in `common/config.py`, plus,
all in `airspace/config.py` with their defaults and reasons: `LIVE_MAX_AGE_S`,
`NEIGHBOUR_MAX_AGE_S`, `CLOCK_RELAX_S_PER_S`, `AUDIT_QUEUE_SIZE`,
`AUDIT_CLOSE_TIMEOUT_S`, `TERRAIN_CACHE_TILES`. Separation minima and the
height limit are policy in the database, never here.

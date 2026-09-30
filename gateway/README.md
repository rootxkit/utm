# gateway

MAVLink and Remote ID ingest. Receive only: it never sends anything towards an
aircraft.

Receives MAVLink from operator relays (relay-v1) and over UDP, identifies
vehicles by SYSID, converts every field to SI units at the parser boundary, and
fans the result out to TimescaleDB, Redis live state and NATS. Remote ID
observations arrive through `python -m gateway.remote_id_ingest`.

Safety-relevant: `mypy --strict` and an 80% coverage target apply here.

## Time on the bus (`telemetry.<drone_id>`)

Every published telemetry message carries these time-related fields:

- `ts`: the record's capture time, on the clock of whoever captured it. On
  the relay path that is the ground PC's `recv_utc_ns`, which relay-v1 §9
  says may be wrong, drifting or stepped. On the Remote ID path it is the
  Gateway's receive time (the broadcast's own `seconds_after_hour` is decoded
  but not yet carried). `ts` orders a station's own records; it is not
  comparable across stations.
- `rx_ts`: when the Gateway received the batch, on the Gateway's clock. One
  clock for every station. `null` for a row that did not come through the
  pipeline (the Redis snapshot, whose rows carry `rx_ts: null`,
  `captured_at: null` and `backlog: false`).
- `captured_at`: where the row sits in time on the Gateway's clock. A
  draining relay's frame is bounded at 64 KiB (relay-v1 §6), which at §10's
  8.4 KiB/s holds about 8 s of capture under one `rx_ts`; a sample from the
  frame's start is not simultaneous with one from its end (117 m at 15 m/s,
  against a 60 m separation minimum). So each row is placed at
  `rx_ts - (newest ts in the batch - its ts)`: the station's clock skew
  cancels within the batch, the batch's newest record lands at `rx_ts`, and
  the rest sit behind it by their true spacing. A spacing that is negative
  or beyond 120 s (`MAX_BATCH_SPAN_S`, a clock stepped inside the batch) is
  clamped and counted. The airspace monitor places aircraft by this field;
  it uses `rx_ts` only to judge its own lag. Equal to `rx_ts` for Remote ID.
- `backlog`: `true` when the record is not the present. Two cases:
  - it was queued on the relay before the session that delivered it, that
    is, its `seq` is at or below the `newest_seq_held` the relay declared in
    that session's `hello` (relay-v1 §5; the relay then sends from
    `resume_from_seq` onward, so everything up to `newest_seq_held` is what
    it had on disk at connect);
  - it arrived while the session was **draining**: records captured while
    a relay works off a queue reach the Gateway seconds to minutes late,
    since drain barely exceeds intake (§10). The Gateway sees a drain
    without any clock: a frame at or above 32 KiB, half the §6 size bound,
    only occurs when more than 100 ms of records were waiting (three
    aircraft produce under 1 KiB per 100 ms), and a `status.queue_depth`
    (§8, records awaiting acknowledgement) above `RelayServer.drain_queue_depth`
    (default 1000, about four acknowledgement intervals of three aircraft
    at 84 Hz) says the same. Either starts the drain; it ends when a frame
    under the bound arrives and the last reported depth is under the
    threshold. Once it ends, `captured_at` is within a batch of `rx_ts`.
  Those positions are history: the airspace monitor records them but raises
  no live alert from them. Always `false` for Remote ID, which has no queue.

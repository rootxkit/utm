# gateway

MAVLink and Remote ID ingest. Receive only: it never sends anything towards an
aircraft.

Receives MAVLink from operator relays (relay-v1) and over UDP, identifies
vehicles by SYSID, converts every field to SI units at the parser boundary, and
fans the result out to TimescaleDB, Redis live state and NATS. Remote ID
observations arrive through `python -m gateway.remote_id_ingest`.

Safety-relevant: `mypy --strict` and an 80% coverage target apply here.

## Time on the bus (`telemetry.<drone_id>`)

Every published telemetry message carries three time-related fields:

- `ts`: the record's capture time, on the clock of whoever captured it. On
  the relay path that is the ground PC's `recv_utc_ns`, which relay-v1 §9
  says may be wrong, drifting or stepped. On the Remote ID path it is the
  Gateway's receive time (the broadcast's own `seconds_after_hour` is decoded
  but not yet carried). `ts` orders a station's own records; it is not
  comparable across stations.
- `rx_ts`: when the Gateway received the batch, on the Gateway's clock. One
  clock for every station: the airspace monitor places aircraft in time by
  it. `null` for a row that did not come through the pipeline (the Redis
  snapshot).
- `backlog`: `true` when the record was queued on the relay before the
  session that delivered it, that is, its `seq` is at or below the
  `newest_seq_held` the relay declared in that session's `hello` (relay-v1
  §5; the relay then sends from `resume_from_seq` onward, so everything up to
  `newest_seq_held` is what it had on disk at connect). Those positions are
  history, not the present: the airspace monitor records them but raises no
  live alert from them. Always `false` for Remote ID, which has no queue.

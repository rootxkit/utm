# gateway

MAVLink and Remote ID ingest. Receive only: it never sends anything towards an
aircraft.

Receives MAVLink from operator relays (relay-v1) and over UDP, identifies
vehicles by SYSID, converts every field to SI units at the parser boundary, and
fans the result out to TimescaleDB, Redis live state and NATS. Remote ID
observations arrive through `python -m gateway.remote_id_ingest`.

Safety-relevant: `mypy --strict` and an 80% coverage target apply here.

## Sources switched off (U-15)

Each relay station, each Remote ID receiver, and each type as a whole can be
switched off without a restart. Both adapters follow the switches the API
publishes on NATS (`common/sources.py`), never the relational database. A
disabled station is refused at the upgrade with 503 and closed with 1013;
a disabled receiver's datagrams are dropped. Both count what they refuse
and publish `source.<type>`. `docs/runbooks/u15-source-control.md` has the
whole path.

## Time on the bus (`telemetry.<drone_id>`)

Remote ID observations also say whether they are identified
(`remote_id.identified`, S-32), which altitude `alt_amsl_m` came from
(`alt_source`, S-33), and where their time came from
(`remote_id.time_source`, S-27). `docs/runbooks/p1-15-remote-id.md` says
how each is decided, and what the ingest's minutely "remote id ingest
status" line counts.

Every published telemetry message carries these time-related fields:

- `ts`: the record's capture time, on the clock of whoever captured it. On
  the relay path that is the ground PC's `recv_utc_ns`, which relay-v1 §9
  says may be wrong, drifting or stepped. On the Remote ID path it is the
  broadcast's own capture time (S-27): the Location's tenths of a second
  after the hour, on the hour that puts it closest to and not after the
  Gateway's receive time plus a tolerance, or the receive time when the
  broadcast says the time is unknown. `ts` orders a station's own records;
  it is not comparable across stations.
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
  it uses `rx_ts` only to judge its own lag. For Remote ID it is the
  broadcast time when that is plausible: no more than
  `REMOTE_ID_TIME_TOLERANCE_S` (1 s) ahead of `rx_ts` and no more than
  `REMOTE_ID_MAX_LATENCY_S` (5 s) behind it, each widened by the
  broadcast's declared timestamp accuracy. Otherwise it is `rx_ts`, the
  fallback is counted by reason (`RemoteIdTracker.time_fallbacks`), and
  `remote_id.time_source` says `receiver` instead of `broadcast`.
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
    aircraft produce under 1 KiB per 100 ms), and a real drain always
    sends one first. A reported `status.queue_depth` (§8, records awaiting
    acknowledgement) also starts it, when it exceeds
    `RelayServer.drain_start_factor` (2) times the clearing bound below,
    that is, six seconds of the session's own intake: the relay's send
    loop returns as soon as the socket buffer takes a frame, so a Gateway
    slow to process can have hundreds of KB of small, old frames in flight
    while the relay's queue grows. It ends on a frame under the size bound
    once the last reported depth is within `RelayServer.drain_clear_s`
    (3 s) of the session's record rate, measured on the records'
    `recv_utc_ns` over the last 5 s of capture, with a floor of 100 records;
    with no depth or rate known yet, a small frame clears it, and with no
    rate known, depth starts nothing. Depth is never compared with a fixed
    count: a healthy twelve-aircraft station holds over a thousand
    unacknowledged records (about 1.1 s) before each 1 s ack, and a fixed
    count would flag it for ever. A station clock stepping back resets the
    rate window (counted). Once the drain ends, `captured_at` is within a
    batch of `rx_ts`.
  Those positions are history: the airspace monitor records them but raises
  no live alert from them. Always `false` for Remote ID, which has no queue.

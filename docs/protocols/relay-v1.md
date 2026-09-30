# relay-v1 — ground relay to Gateway

- **Status:** APPROVED — reviewed 2026-09-21; implementation in progress (P1-01)
- **Version:** `1`
- **Tasks:** produced by P1-01 (relay), consumed by P1-02 (Gateway)

This is the contract between the ground relay and the Gateway. **The Gateway is
built against this document, not against the relay's source.** Either side may
be rewritten in another language or replaced entirely as long as it conforms.

## 1. Design rules

Three rules explain every decision below. Read them first; the rest follows.

**The relay is dumb.** It does not parse MAVLink. It does not know what a
vehicle is, how many there are, or whether one is airborne. It moves opaque
datagrams from a socket to a server, in order, without loss. Every question of
meaning belongs to the Gateway.

**The relay is lossless.** It forwards every datagram it receives, unmodified.
Filtering at the ground station would be an irreversible decision taken at the
point in the system with the least information about what will later matter.
The messages the live path does not want — `ATTITUDE`, `VIBRATION`,
`EKF_STATUS_REPORT`, `ESC_TELEMETRY` — are exactly what an incident
investigation reads after a crash, and P10-03 flight replay cannot reconstruct
a message that was never recorded. ADR-001 measured ~2.8 KiB/s per aircraft;
on an internet uplink that is not worth optimising against the cost of an
unexplainable accident.

**The relay is receive-only toward the aircraft.** Its UDP socket is used for
`recvfrom` and nothing else. At Stage 0 the server cannot affect flight, and
this is where that guarantee is enforced physically rather than promised.
Nothing in this protocol carries a message travelling towards a vehicle.

This prohibition is absolute and is **not** a placeholder for a future command
path. The system never commands an aircraft (`ARCHITECTURE.md` §3), so no
command channel is planned, here or anywhere else. Adding a send path to this
socket would require a protocol version bump and a deliberate re-examination of
the Stage 0 safety argument — which is exactly the friction that should stand
in the way. A capability that no code can express is
worth more than one that merely nobody currently calls.

## 2. Transport

```
wss://<gateway-host>/relay/v1
```

TLS is mandatory. One WebSocket connection per ground station.

Both WebSocket frame types are used, and the type distinguishes the two
channels:

| Frame type | Carries |
|---|---|
| **Text** | JSON control messages: `hello`, `welcome`, `ack`, `status` |
| **Binary** | Telemetry batches (§6) |

Every JSON message has a `"type"` field naming it. Unknown JSON `type` values
and unknown JSON fields **must be ignored rather than rejected**, so that a
newer relay can talk to an older Gateway within version 1.

## 3. Authentication

The relay authenticates on the HTTP upgrade request:

```
Authorization: Bearer <token>
```

Failure is rejected at the upgrade with HTTP `401`. The relay treats `401` as
fatal and does not retry with the same token: a bad credential is an operator
problem, and retrying turns it into a log flood.

**The token identifies a ground station, not a vehicle.** Vehicle identity
comes from the MAVLink SYSID inside the forwarded datagrams, and which stations
are permitted to carry which vehicles is server policy, evaluated by the
Gateway. A station is not trusted to assert what it is carrying.

> This refines **P1-07**. That task reads "per-vehicle token"; the tokens are
> per-station, and per-vehicle authorisation becomes a server-side policy check
> on `(station_id, sysid)` rather than a credential the relay holds. The
> security property is the same — a spoofed SYSID is rejected — but it is
> enforced where the policy lives, and a compromised ground station cannot mint
> vehicles it was never assigned.

## 4. Identity and sequencing

Every record is identified by the triple:

```
(station_id, epoch, seq)
```

| Field | Type | Meaning |
|---|---|---|
| `station_id` | string | Stable identity of the ground station, from its config |
| `epoch` | string, 32 lowercase hex chars | Random 128-bit value generated **when the relay's queue database is created** |
| `seq` | `u64` | Monotonically increasing within an epoch, starting at 0, never reused |

**Why `epoch` exists.** Without it, a queue file that is deleted, corrupted, or
restored from a backup restarts `seq` at zero. The Gateway, deduplicating on
`(station_id, seq)`, would recognise those numbers as already seen and discard
the new data — silently, and for as long as it took to climb past the old high
water mark. A fresh random epoch makes that case loud instead: the server sees
an identity it has never seen, and starts from zero legitimately.

The epoch changes only when the queue database is created. Restarting the relay
against an existing queue keeps the epoch and continues the sequence.

## 5. Session establishment

On connect, the relay sends `hello` and waits for `welcome` before sending any
data frame.

### `hello` — relay to server, text

```json
{
  "type": "hello",
  "station_id": "tbilisi-base-1",
  "epoch": "9f2c1b7d4e6a58039ab1c2d3e4f50617",
  "relay_version": "0.1.0",
  "protocol_version": 1,
  "oldest_seq_held": 41203,
  "newest_seq_held": 58817,
  "monotonic_ns": 992847110000000,
  "utc_ns": 1758412800123456789
}
```

`oldest_seq_held` and `newest_seq_held` describe what the relay still has on
disk. When the queue is empty, `oldest_seq_held` is the next sequence number to
be assigned and `newest_seq_held` is that value minus one.

### `welcome` — server to relay, text

```json
{
  "type": "welcome",
  "protocol_version": 1,
  "resume_from_seq": 58120
}
```

**The server is authoritative about what it holds.** The relay sends everything
from `resume_from_seq` onward, regardless of what it believes was acknowledged.
This is what makes a lost `ack` harmless: an ack that never arrived costs a
retransmission, never a gap.

For an epoch the server has never seen, `resume_from_seq` is `0`.

The relay must not assume `resume_from_seq` is at or ahead of its own
high-water mark. A server may legitimately ask for data the relay has already
sent.

## 6. Data frames — binary

A binary frame is a batch of one or more records, concatenated with no header
and no padding. All integers are **little-endian**.

```
repeated:
  u64  seq              record sequence number within the epoch
  i64  recv_utc_ns      wall-clock time the datagram was received (§9)
  u16  len              length of the datagram in bytes
  u8   datagram[len]    the UDP payload, exactly as received
```

Records within a batch are in ascending `seq` order with no gaps.

**The datagram is opaque.** It is not re-framed, not split into MAVLink
messages, not validated, and not modified. A datagram that is malformed,
truncated, or not MAVLink at all is forwarded unchanged — deciding that is the
Gateway's job, and a relay that discarded "invalid" traffic would hide exactly
the corruption worth investigating.

`len` is `u16`; a UDP payload cannot exceed 65507 bytes, so the field cannot
overflow. The relay's receive buffer must be at least 65535 bytes: sizing it to
the datagrams QGC happens to send today would silently truncate anything
larger.

### Batching

A batch is flushed on whichever comes first:

- **100 ms** since the batch opened, or
- **64 KiB** accumulated.

The time bound sets the relay's contribution to end-to-end latency, which
P1-08 budgets at under 500 ms end to end. The size bound keeps a burst — a
reconnect draining a backlog — from producing frames large enough to stall the
connection.

## 7. Acknowledgement

### `ack` — server to relay, text

```json
{
  "type": "ack",
  "epoch": "9f2c1b7d4e6a58039ab1c2d3e4f50617",
  "seq": 58904
}
```

**Cumulative**: every record up to and including `seq`, in this epoch, is
durably stored server-side. The relay may delete those records and must not
delete any record beyond `seq`.

`epoch` is included so that an `ack` arriving late, after the relay has started
a new epoch, is discarded rather than deleting records it does not describe.

The server acknowledges only what it has committed to durable storage. An `ack`
for data still in a server-side buffer would turn a Gateway crash into a hole
in the flight record, which is the exact failure this design exists to prevent.

## 8. Status

### `status` — relay to server, text, every 1 s

```json
{
  "type": "status",
  "queue_depth": 1240,
  "queue_bytes": 2310450,
  "dropped_intake_total": 0,
  "dropped_cap_total": 0,
  "last_datagram_age_ms": 38,
  "storage_ok": true,
  "uptime_s": 7321,
  "monotonic_ns": 992847110000000,
  "utc_ns": 1758412800123456789
}
```

| Field | Meaning |
|---|---|
| `queue_depth` | Records on disk awaiting acknowledgement |
| `queue_bytes` | Bytes those records occupy |
| `dropped_intake_total` | Datagrams dropped before a `seq` was assigned, because the in-memory intake queue was full. Persisted across restarts; see §11 for a disk that refuses the write |
| `dropped_cap_total` | Records discarded from disk because the queue hit its size cap. Persisted across restarts |
| `last_datagram_age_ms` | Milliseconds since a datagram last arrived on the UDP socket, or `null` if none ever has |
| `storage_ok` | **Optional.** `false` while the relay's durable queue is refusing writes (disk full, I/O error) or a write has hung for longer than the relay's `writer_stall_timeout_s`, `true` otherwise. A relay that omits it is to be read as `true`. Informational: it says loss is likely, not that it has happened — see §11 |
| `uptime_s` | Seconds since the relay started |

The two drop counters are separate because they are different failures with
different remedies, and because only one of them is visible as a `gap` — see
§11.

**`last_datagram_age_ms` is the field that matters.** `ARCHITECTURE.md` §4
separates two failure domains that a naive implementation renders identically:

| Situation | Symptom without `status` | Symptom with `status` |
|---|---|---|
| Relay or internet down | Telemetry stops | No `status` for >3 s: the station is unreachable |
| Relay up, radio silent | Telemetry stops | `status` continues, `last_datagram_age_ms` climbs |

The first is a tracking outage; the aircraft is fine and the pilot still has
QGC. The second means the ground station has lost the aircraft, which is a
flight-safety event. **The pilot console must never present these as the same
thing**, and this field is what makes them distinguishable.

The Gateway should treat three consecutive missed `status` messages as the
station being unreachable. Relying on TCP or WebSocket timeouts alone is not
sufficient: a half-open connection can survive for minutes.

### The relay's own detection, and the window where the two disagree

The Gateway reaches its verdict in about 3 s. The relay cannot: a half-open
link — bytes stop, the socket stays up, nothing is refused — looks alive until
a ping goes unanswered.

**A relay must detect an unresponsive uplink and begin reconnecting within
25 seconds.** Detection takes:

```
detection  = (time to the next ping) + ping_timeout + close_timeout
worst case = ping_interval + ping_timeout + close_timeout
```

`close_timeout` belongs in that sum because the close handshake waits for a
close frame that a dead link can never deliver. It is a third of the budget and
the term most easily forgotten.

Defaults are **10 s / 10 s / 5 s**, all three configurable. Measured against a
TCP proxy that stalls a connection without closing it: 23.0 s for these values,
against 48 s for the websockets library defaults of 20/20/10. The measurement
table is in `docs/runbooks/p1-01-test-records.md`.

**So there is a window, up to 25 s long, in which the two components hold
different beliefs about the same link:**

```
t = 0 s     the link goes dead
t ~ 3 s     Gateway: station unreachable
t ~ 25 s    relay: gives up, reconnects
t ~ 30 s    P7-01: the operator is alerted
```

That disagreement is acceptable and must be understood rather than designed
away. During it:

- **No data is at risk.** No acknowledgement can arrive through a dead link, so
  the relay deletes nothing. The queue grows, which is correct behaviour.
- **The relay is still receiving.** Intake is independent of the uplink, so
  telemetry continues to reach the disk at full rate.
- **What is lost is time**, not telemetry: up to 25 s before the relay begins
  reconnecting and the backlog starts draining.

The sum of the three settings must stay **below the 30 s link-loss threshold in
P7-01**. Not because 25 s beats an alert — the Gateway's verdict arrives long
before either — but because the relay must have finished deciding before the
operator is told. An operator alerted that a link is down while the relay still
believes it is up is a third state, and nobody has designed for it.

**This has a consequence at the console**, and it is a requirement on the
Gateway rather than on this protocol: during the window the Gateway reports the
station as unreachable while the relay is alive and buffering correctly. The
console must not imply telemetry is being lost, because it is not. See P1-02
and P6-03.

## 9. Clocks

`recv_utc_ns` comes from the ground PC's wall clock, which **may be wrong** —
unsynchronised, drifting, or stepped by NTP mid-flight. It is recorded because
it is useful, not because it is trusted.

Every `hello` and `status` carries a `(monotonic_ns, utc_ns)` pair sampled at
the same instant. This lets the Gateway estimate the station's clock offset and
detect a step: the monotonic clock cannot jump, so a change in the difference
between the two is a wall-clock correction, not elapsed time.

A later refinement can align records to GPS time using `SYSTEM_TIME`, observed
at 3 Hz in ADR-001 and carrying the autopilot's GPS-derived time. **This
document does not specify that alignment**; it notes the raw material exists.
Deciding it belongs with the Gateway's time handling, not with the transport.

## 10. Delivery guarantees

- **In order**, per `(station_id, epoch)`.
- **At least once.** A record may be delivered more than once, after a
  reconnect or a lost `ack`.
- **Deduplicated on `(station_id, epoch, seq)`** by the Gateway.

Exactly-once delivery is not offered, because it cannot be had over a link that
can fail between "stored" and "acknowledged". Dedupe on a stable key is
equivalent, and vastly simpler.

### No live-first reordering (deliberate v1 omission)

When the relay reconnects after an outage, it sends its backlog in sequence
order. It does **not** send live telemetry first and backfill the gap behind
it, even though a pilot would rather see the present than the past.

That behaviour would break cumulative acknowledgement. Acknowledging a range
with a hole in it requires selective acknowledgement, which means per-record
state on both sides and a materially more complex protocol — the part of a
transport most likely to harbour a bug that appears only under the conditions
nobody can reproduce.

The original arithmetic, kept because the correction is instructive:

```
3 aircraft x 2.8 KiB/s          =  8.4 KiB/s
30-minute outage: 1800 s x 8.4  =  ~15 MB of backlog
15 MB over a 10 Mbit/s uplink   =  ~12 seconds to drain
```

**That was bandwidth arithmetic only, and it is wrong.** The wire is not what
limits drain. A record is not drained when it crosses the uplink but when the
Gateway has stored it, and the Gateway stores far more slowly than the link
carries. Measured by `tools/ingest_capacity.py` on 2026-09-27 (P1-10; evidence
in `docs/decisions/002-drain-rate-requirement.md`):

| Sources on one station | Intake | Maximum drain | Drain / intake |
|---|---|---|---|
| 1 | 194 records/s | 290 records/s | 1.5x |
| 3 | 416 records/s | 398 records/s | 0.96x |
| 11 | 1,281 records/s | 289 records/s | 0.23x |

Drain is roughly constant at about 300 records/s whatever the load, while
intake grows with every aircraft. At one source a 60 s outage takes about
two minutes to recover; at three or more the backlog **never** clears, and at
eleven the relay falls behind with no outage at all.

The ceiling is in the Gateway, not in this protocol: 96% of the time spent
storing a batch goes to resolving each MAVLink message's source binding with
its own database query. That is a Gateway defect with its own task (P1-13), and
nothing in relay-v1 has to change to fix it.

### What this means for live-first reordering

The decision above stands, and the measurement strengthens it. Sending live
telemetry ahead of the backlog helps only when drain exceeds intake, so that
there is spare capacity to spend on the present. Where drain is below intake
there is no such capacity: live-first would show a current map while the
backlog grew without limit behind it, and the flight record would never
complete. The cure for stale telemetry here is a Gateway that stores faster,
not a protocol that chooses which records to be late with.

The trade must still be redone with numbers if drain is raised well above
intake and a stale map after an outage is still judged too long. The numbers
to redo it with are drain and intake as measured by the harness, not link
bandwidth.

### Slow drain also breaks the half-open detection in §8

At eleven sources the relay's session ended 15 times in one run with
`keepalive ping timeout`, on a link that was up throughout. A Gateway that
cannot keep up stops reading from the connection, and pings wait behind the
data. The mechanism is inferred, not measured; the reconnections are measured.
Either way, §8's detector cannot tell a slow Gateway from a dead link, so until
drain exceeds intake the station's link state is unreliable as well as late.

## 11. Loss accounting

A flight record with a hole in it is only dangerous when nobody can tell the
hole is there. This section enumerates every way a datagram can fail to reach
the Gateway, and how each one is made visible.

| # | Loss | Has a `seq`? | How it is visible |
|---|---|---|---|
| 1 | Radio or QGC never delivered it | no | No datagrams arrive; `last_datagram_age_ms` climbs (§8) |
| 2 | Intake: in-memory queue full | **no** | `dropped_intake_total` increases |
| 3 | Cap: disk queue at its size limit | yes | `gap` message, and `dropped_cap_total` increases |
| 4 | Relay crashed with records still in memory | no | `uptime_s` resets |
| 5 | Not yet acknowledged when the link dropped | yes | None — retransmitted on reconnect (§7). Not a loss |

### The queue is capped, deliberately

Default 1 GiB, configurable. At the cap the relay drops the **oldest** records
and increments `dropped_cap_total`. It never blocks intake: blocking would
discard live telemetry in order to preserve old telemetry, which is backwards.

The cap is not negotiable in either direction. An uncapped queue is an
unbounded file on the pilot's laptop, and a full disk can take QGC down with
it — **P7-11**, the most serious Stage 0 failure mode, where the pilot loses
their control surface mid-flight. Telemetry completeness is not worth buying at
the price of the pilot's ability to fly the aircraft.

### `gap` — relay to server, text

Sent when `resume_from_seq < oldest_seq_held` for the current epoch: the server
is asking for records the cap has already discarded. The relay sends this
**before** any data frame, then resumes from `oldest_seq_held`.

```json
{
  "type": "gap",
  "epoch": "9f2c1b7d4e6a58039ab1c2d3e4f50617",
  "from_seq": 58120,
  "to_seq": 61099,
  "reason": "queue_cap"
}
```

`from_seq` is **inclusive**, `to_seq` is **exclusive**. The missing records are
`[from_seq, to_seq)`, that is `from_seq` through `to_seq - 1` inclusive. With
`resume_from_seq = 58120` and `oldest_seq_held = 61099`, the relay sends
`from_seq = 58120`, `to_seq = 61099`, meaning 58120 through 61098 are gone.
They exist nowhere and will never arrive.

`reason` is `"queue_cap"`. It is an open string so later reasons can be added
without a version bump.

**A recorded gap advances the server's resume point.** Once the server has
stored a `gap`, the sequence numbers it covers are permanently absent, and the
server must treat them as satisfied when computing `resume_from_seq` for later
sessions. Computing it as "highest contiguous sequence plus one" without
accounting for gaps leaves the watermark stuck at the hole forever: every
subsequent reconnect asks for records the relay cannot supply, and the relay
answers with the same `gap` again, for the life of the epoch.


### Protocol error: the server asks for records that never existed

If `resume_from_seq > newest_seq_held + 1`, the server is claiming records the
relay never sent. This is **not** a gap — it is a protocol error, and the two
must not be conflated, because a gap says "data was lost" while this says "one
of us is confused about which epoch or station we are talking about".

The relay logs the discrepancy with both sequence numbers, closes the
connection, and does not send data. It does not silently rebase onto the
server's number: continuing would write records under sequence numbers that
mean something different at each end, which is worse than stopping.

Reconnection then proceeds with normal backoff. If the condition persists it is
an operator problem, and the logs say so.

### Intake drops cannot produce a `gap`

Loss #2 happens before a sequence number is assigned, so there is no gap in the
sequence to report — the numbering is contiguous across an intake drop, and
`gap` is structurally incapable of describing it.

**Do not solve this by pre-allocating sequence numbers at intake.** It would
mean assigning `seq` on the UDP thread and reconciling allocated-but-never-
written numbers on restart, which is a large complexity increase in the one
component whose job is not to lose data, for a loss that should not occur at
all in normal operation.

Instead, `status` carries `dropped_intake_total` every second. The Gateway
takes deltas between consecutive `status` messages, which localises any intake
loss to a one-second window. That is enough for an investigator to say "between
these two timestamps, this station dropped 40 datagrams it never got to disk",
which is the question that actually gets asked after an incident.

Loss #4 is visible the same way: `uptime_s` going backwards between two
`status` messages means the relay restarted, and any records still in its
memory at that moment were lost.

### The durable queue refuses writes

A full disk or an I/O error does not stop the relay. The writer holds the batch
that failed and retries it with backoff, taking nothing new from intake
meanwhile; the write is all or nothing, so a retry assigns the same sequence
numbers and nothing is lost while intake still has room. `status` carries
`storage_ok: false` for the duration.

Once intake fills, datagrams are dropped and counted as loss #2, exactly as if
the writer were merely slow. The count is taken in memory, so
`dropped_intake_total` in `status` moves even though the disk cannot record
it. The next write that succeeds persists it: a batch, an acknowledgement, or
at the latest the relay's shutdown, which also counts any datagrams it was
still holding. Only a disk that refuses even that last write loses the count,
and then the restart is itself visible as loss #4. **The Gateway needs nothing

new to see this loss**: its existing `dropped_intake_total` delta already
reports it. `storage_ok` only says why, and says it before the loss begins.

A write can also hang - an fsync that neither fails nor returns. The relay
cannot see an error, so it times the writer instead: once a pass has taken
longer than `writer_stall_timeout_s` (default 5 s), `status` reports
`storage_ok: false`. It keeps running and keeps sending `status`, because a
hang can clear by itself and exiting would discard what it holds in memory,
and its `dropped_intake_total` includes the drops the stuck writer has not
collected, so the loss shows exactly as above.

If the writer thread itself stops, the relay stops sending `status` and exits
with a non-zero code rather than run on, bound to the socket and looking alive
while nothing reaches disk. The Gateway sees `unreachable`, and a restart shows
as loss #4. The same happens when a failed write cannot even be rolled back:
the queue's state is then unknown, no retry can be trusted, and only a restart
reopens it cleanly.

## 12. Reconnection

On disconnect the relay reconnects with exponential backoff, **capped at 10 s**,
with jitter. Jitter matters once there is more than one ground station: without
it, a Gateway restart brings every station back simultaneously, each draining a
backlog.

`401` is fatal and is not retried (§3). Every other failure is retried
indefinitely — a relay that gives up is a relay that loses a flight.

The relay keeps accepting UDP and queueing to disk throughout. Connection state
never propagates back to the socket.

## 13. What the Gateway must implement

For P1-02, conformance means:

1. Accept the upgrade, validate the bearer token, resolve it to a `station_id`.
2. Reply to `hello` with `welcome`, carrying the true `resume_from_seq` for
   that `(station_id, epoch)` — `0` for an unknown epoch.
3. Persist records durably **before** acknowledging them.
4. Send a cumulative `ack` carrying the epoch, at least once per second while
   data is flowing.
5. Deduplicate on `(station_id, epoch, seq)`.
6. **Record every `gap` as an event** against the station, with its sequence
   range and the wall-clock window it falls in. P10-03 flight replay must
   render these holes explicitly rather than interpolating across them: a
   replay that draws a smooth track through missing data is worse than one that
   shows the track stopping, because it invents evidence.
7. Track `dropped_intake_total` deltas between consecutive `status` messages
   and record them as events too (§11). These losses have no `gap`.
8. Treat three consecutive missed `status` messages as the station being
   unreachable, and surface that **differently** from a station reporting a
   rising `last_datagram_age_ms` (§8).
9. Parse MAVLink only after all of the above. Nothing in this protocol requires
   the transport layer to understand the payload.

## 14. Versioning

The path carries the major version (`/relay/v1`). Within version 1, unknown
JSON message types and unknown fields must be ignored, so additive changes do
not require a version bump. Anything that changes the meaning of an existing
field, the binary record layout, or the delivery guarantees requires `/relay/v2`.

# P1-02 — Gateway ingest

- **Status:** DRAFT — specification only, no implementation exists
- **Task:** P1-02
- **Built against:** [`docs/protocols/relay-v1.md`](../protocols/relay-v1.md)

The Gateway is the first component that understands what a datagram means.
Everything upstream of it moves opaque bytes; everything downstream depends on
it having read them correctly.

## 0. The rule that governs this document

**The Gateway is built against `relay-v1.md`, not against the relay's source
or the sink's.** If the implementations and the document disagree, the document
is correct and the implementation has a bug. Say so, file it, and fix the
implementation — do not quietly match the code and leave the specification
describing something nobody built.

This is not hypothetical. Writing the sink from the document surfaced two real
specification gaps (§11's gap-advances-the-resume-point rule, and
`last_datagram_age_ms` being `null` before any datagram arrives). Both were
fixed in the document. A third implementation reading the same text is another
chance to find what the first two assumed.

## 1. What P1-02 is

A service that:

1. Terminates relay-v1 connections from ground stations.
2. Stores every received datagram durably, and acknowledges only what is
   stored.
3. Identifies who sent each MAVLink frame, and decides which senders are
   aircraft.
4. Converts the messages the live pipeline needs into `drone_state` rows, and
   archives the rest.
5. Publishes station and vehicle state the console can render without
   misleading a pilot.

## 2. What P1-02 is not

Stated first, because scope creep here is expensive.

- **No commanding.** Nothing in the Gateway sends anything towards an aircraft.
  The Stage 0 guarantee runs from `agent/udp.py`'s receive-only socket through
  this service and out to the console; P1-02 does not weaken it. No command
  path is planned anywhere: the system never commands an aircraft.
- **No airspace checks.** CPA, zones and advice are P5. The Gateway feeds
  them; it does not compute them.
- **No direct UDP ingest as the primary path.** A UDP listener stays for SITL
  and bench work (see §11), but the production path is relay-v1.

## 3. Relationship to `tools/relay_sink.py`

The sink already speaks the server side of relay-v1. It exists to verify the
protocol during the P1-01 hardware test, and **it must not be promoted into
production**. The differences are not incidental:

| | Sink | Gateway |
|---|---|---|
| Storage | append-only file per epoch, plus a JSON counters file | TimescaleDB hypertable and a raw archive |
| Durability | `fsync` per batch on one local file | database transaction, committed before ack |
| Dedupe | an in-memory `set[int]` of every seq | index on `(station_id, epoch, seq)` |
| Restart | rescans the whole records file | query, bounded |
| Tokens | one token, one file | many stations, rotation, revocation |
| MAVLink | never parsed | parsed, classified, converted |
| Vehicles | no such concept | `drones` registry, station-to-vehicle policy |
| Concurrency | one process, one station at a time is fine | many stations concurrently |
| Failure | crash and lose the run | supervised, restarts, keeps its data |

The sink's in-memory seq set is the clearest example: fine for a twelve-minute
test with 58,000 records, hopeless for a fleet over months. Anything that
borrows from the sink should borrow the *reading of the protocol*, not the
implementation.

The sink stays as it is. It remains the independent second implementation, and
its value is precisely that it was not written by whoever writes this one.

## 4. relay-v1 server obligations

Conformance is §13 of the protocol. Restated here with what each means in this
service:

1. **Accept the upgrade, validate the bearer token, resolve it to a
   `station_id`.** Failure is HTTP `401` on the upgrade, not a WebSocket close
   — the relay treats `401` as fatal and stops retrying, and a close frame
   would leave it reconnecting against a credential that will never work.
2. **Reply to `hello` with `welcome`** carrying the true `resume_from_seq` for
   that `(station_id, epoch)`, `0` for an epoch never seen. The value comes
   from what is **durably stored**, never from memory or a cache — a Gateway
   that restarts must answer from the database.
3. **Persist before acknowledging.** The ack is a promise the relay acts on by
   deleting its own copy. An ack for data still in a buffer turns a Gateway
   crash into a permanent hole.
4. **Cumulative `ack` carrying the epoch**, at least once per second while data
   flows. The epoch matters: a late ack arriving after the relay has started a
   new epoch must be discarded, not applied.
5. **Deduplicate on `(station_id, epoch, seq)`.** At-least-once on the wire,
   exactly-once after dedupe.
6. **Record every `gap` as an event**, with its sequence range and wall-clock
   window. A recorded gap also advances `resume_from_seq` past it — otherwise
   the same gap is re-reported on every reconnect for the life of the epoch.
7. **Track `dropped_intake_total` deltas** between consecutive `status`
   messages and record them as events. These losses have no `gap` and no
   sequence discontinuity; the delta is the only evidence they happened.
8. **Three consecutive missed `status` messages means unreachable**, surfaced
   differently from a rising `last_datagram_age_ms`. See §9.
9. **Parse MAVLink only after all of the above.** The transport layer does not
   need to understand the payload, and mixing the two makes a parsing bug into
   a transport failure.

### Protocol errors

`resume_from_seq > newest_seq_held + 1` is the relay's check, not the
Gateway's, but the Gateway is the side that can *cause* it by answering with a
sequence the station never produced. That would mean the Gateway has confused
two epochs or two stations. It must be impossible by construction: the
`welcome` value is derived from a query keyed on both.

## 5. Authentication

Per the P1-07 refinement recorded in `relay-v1.md` §3:

- **Tokens identify a ground station, not a vehicle.** A station relays
  whatever its radio hears and cannot hold one credential per aircraft.
- **Which vehicles a station may carry is server policy**, evaluated here, on
  `(station_id, sysid)`. A station is not trusted to assert what it is
  carrying.
- A frame whose SYSID is not assigned to the presenting station is rejected and
  rate-limit-logged. A compromised ground station cannot mint vehicles it was
  never assigned.
- **The policy is `source_bindings` itself** (§12 question 6, answered
  2026-09-28). A station may carry exactly the addresses bound on it; a
  handover is the same drone bound on both stations (§8). At a record's
  timestamp an address is therefore *resolved* (bound here), *unclaimed*
  (bound nowhere: announced once, as in §7), or *rejected* (bound on another
  station only). A rejection is archived like everything else, never written
  to `drone_state`, recorded as a `rejected_source` event and published on
  `events.rejected_source`, and reported at most once per address per minute
  with the count suppressed in between, so it neither floods nor goes quiet.
  Refused connections (`401`) are rate-limited the same way, per remote host.
- **What this does not catch:** a compromised station replaying a SYSID that
  *is* bound on it. Nothing in the address distinguishes that from the real
  aircraft; it needs vehicle-side signing (MAVLink 2 signing), which is
  outside Stage 0's receive-only scope.
- Direct UDP sources (§11) keep their own path and are never production.

Token storage, rotation and revocation are an open question (§12).

## 6. Source classification and the hot path

### 6.1 Classification

The relay forwards everything it hears, which includes QGC's own heartbeat and
any component that speaks on the same link. Classification is per
**`(sysid, compid)`**, from the HEARTBEAT payload:

- `type == MAV_TYPE_GCS` → **ground station**
- a non-vehicle `MAV_TYPE`, or `autopilot == MAV_AUTOPILOT_INVALID` →
  **component**
- otherwise → **vehicle**

**Only vehicles become drones.** A ground station or a component must never be
registered as one.

Three rules, each learned from a specific failure:

- **Never classify on the SYSID number.** 255 is a convention;
  `GCS_SYSTEM_ID` is a user setting sitting in `QGroundControl.ini`.
- **Never classify on message volume.** A vehicle that has just booted has sent
  one HEARTBEAT and nothing else, which is exactly when it must stay visible.
- **Ambiguity resolves to vehicle.** Registering a ground station as an
  aircraft is a nuisance; failing to register an aircraft is the direction that
  gets someone hurt.

`tools/mavlink_probe.py` has a working implementation to lift. Component IDs
matter in practice: ADR-001 observed the autopilot at `1/1` and QGC at
`255/190`, and a gimbal or companion computer may heartbeat under the vehicle's
own SYSID with a different component ID.

### 6.2 Offsets

**Derive every wire-format offset from pymavlink; never write one from
memory**, and pin it with a test that derives it the same way (CLAUDE.md,
Testing). Three offsets were wrong in one session and every one returned a
plausible answer rather than an error. The Gateway should prefer pymavlink's
own decoder where it can, and where it cannot, derive and pin.

### 6.3 Hot path versus archive

Per ADR-001. These produce `drone_state` rows:

| Message | Feeds |
|---|---|
| `HEARTBEAT` | liveness, flight mode, armed state |
| `GLOBAL_POSITION_INT` | position, altitude AGL and AMSL, heading, velocity |
| `SYS_STATUS` | battery percent, voltage |
| `BATTERY_STATUS` | energy accounting for the P4-03 budget |
| `GPS_RAW_INT` | fix type, satellite count |
| `VFR_HUD` | ground speed, climb rate |
| `EKF_STATUS_REPORT` | health alerting (P7-10) |
| `MISSION_CURRENT` | progress inference (P3-06) |
| `MISSION_ITEM_REACHED` | waypoint completion (P3-06) |
| `STATUSTEXT` | FC messages and failsafe reasons |

**Everything else is archived, not discarded.** ADR-001 measured 31 message
types on a real link; roughly 80% of the traffic is not on the hot path.
`ATTITUDE`, `VIBRATION`, `ESC_TELEMETRY_1_TO_4`, `RAW_IMU` and the rest are
exactly what an incident investigation reads after a crash, and P10-03 flight
replay cannot reconstruct what was never stored. The split is between *live
state* and *archive*, never between keep and discard.

### 6.4 No dependence on stream rates

**Nothing in the Gateway may depend on a message arriving at a particular
rate.** What arrives is decided by QGC's own `SR*_` settings and by which
screen the pilot has open; it changes between versions and between sessions.

Concretely:

- Liveness comes from Redis TTL expiry (P1-05), not from counting HEARTBEATs.
- A missing message is a missing value, not an error — unless it is absent long
  enough to expire the TTL.
- `MISSION_ITEM_REACHED` is event-driven and was **not observed** in ADR-001's
  capture, because the aircraft was parked. Its absence must never be treated
  as a fault, and P3-06 must not assume it arrives.
- Rate is a *measurement to report* (P1-09 link quality), never an input to
  correctness.

## 7. Vehicle identity: binding a source to a drone

**A SYSID is a flight-time address, not an identity.** It is set by a
parameter, it is reused across airframes, and two aircraft that never fly
together may share one for years. `drone_state.drone_id` is a fleet identity
and must not be inferred from an address.

The mapping is an explicit binding with validity:

```
(station_id, sysid, compid)  ->  drone_id
                                 bound_from   TIMESTAMPTZ
                                 bound_until  TIMESTAMPTZ NULL
```

Rules:

- **`drone_state` always stores `drone_id`**, never a raw SYSID.
- **The binding is resolved at the record's timestamp, not at ingest time.**
  This matters because of the relay: a backlog replayed after a two-hour
  outage must resolve against the binding that was in effect when the
  telemetry was *captured*, not when it happened to arrive. Resolving at
  ingest would silently attribute an hour of one airframe's flight to
  whichever drone holds the address now.
- **An unknown or unbound source is archived, never written to
  `drone_state`.** It is surfaced on the console as an *unclaimed source* so
  it is visible rather than silently dropped.
- **No auto-registration.** A misconfigured aircraft must not walk itself into
  the fleet. Binding is a deliberate act.
- **Reassigning a SYSID closes one binding and opens another.** Overlapping
  bindings for the same key are a constraint violation, enforced in the
  database rather than in application code.

An unclaimed source is a normal condition during setup and a serious one in
flight, so it is an event and a console state, not a log line.

### `known_drones` is a projection, never an authority

`source_bindings.drone_id` points at `known_drones` in the **telemetry**
database, not at `drones` in the relational one. A foreign key cannot cross
databases, and the Gateway does not connect to the relational database, so this
is what makes "a binding to a drone that does not exist" a constraint violation
rather than an application check that two concurrent writers can both pass.

`known_drones` holds identities and nothing else: `drone_id`, a label for a
human reading an event, and registration/retirement timestamps. **The
relational `drones` registry is the authority.** P2-05 owns projecting into it
when a drone is registered or retired.

**If the two ever diverge, the repair is to rebuild `known_drones` from
`drones`, never the reverse.** A projection that has been edited to match a
mistake becomes a second source of truth, and then nobody can say which
airframe a flight belonged to.

### The registration race is normal, and recoverable

An aircraft can transmit before its projection lands — powered up while the
paperwork is still being done, or a station reconnecting with a backlog that
predates the registration. The Gateway then has no binding and marks those
records **unclaimed**.

**This is not data loss and it is not an error.** The archive holds every
datagram regardless of whether it resolved, and resolution happens at the
record's timestamp, so once the drone is registered and the binding is created
with the correct `bound_from`, the affected records resolve correctly on
replay. The state is recoverable after the fact precisely because nothing was
discarded and nothing was resolved early.

It must be presented that way. The failure mode to guard against is somebody
seeing a screen full of unclaimed sources and "fixing" it with
auto-registration — which is what §7 forbids, because a misconfigured aircraft
would then walk itself into the fleet, and the resulting `drone_id` would be
one nobody chose.

## 8. Two stations relaying one vehicle

**Accepted, not rejected.** Two ground stations in radio range of one aircraft
will both forward its frames, and that is the normal state during a handover
between stations. It is also what Stage 2 looks like when an aircraft is
reachable on both the 915 MHz radio and LTE at once. Rejecting the second
station would mean deliberately severing a working link to preserve a tidy
invariant.

- **One `drone_state` row per vehicle.** Last write wins, ordered by the
  *record's* timestamp — not by arrival.
- **The archive keeps both copies**, each tagged with the station that
  delivered it. They are not duplicates: two independent observations of the
  same instant, and after an incident the difference between them is evidence
  about the links.
- Dedupe stays per station, on `(station_id, epoch, seq)`. Two stations
  relaying the same frame produce two records, correctly.

**Where this gets hard is clocks.** Two laptops do not agree, and
last-write-wins against a wrong clock silently reorders state — a stale
position from the station whose clock runs fast would overwrite a fresh one.
The material for solving it already exists: `relay-v1.md` §9's monotonic/UTC
pairs in `hello` and `status` allow each station's offset to be estimated, and
`SYSTEM_TIME` — observed at 3 Hz in ADR-001, carrying GPS-derived time — is the
eventual authority, being the one clock both stations share.

**The mechanism is deliberately not specified here.** §12 question 8 records
the constraints it has to satisfy.

## 9. Station state for the console

This is the requirement that follows from `relay-v1.md` §8, and it is the one
most easily got wrong.

The Gateway declares a station unreachable after three missed `status`
messages, about 3 s. The relay does not give up on a half-open uplink for up to
25 s. **For that window the two components hold different beliefs about the
same link**, and during it the relay is alive, receiving at full rate, and
buffering correctly.

So the Gateway must publish at least these distinct states:

| State | Meaning | Is telemetry lost? |
|---|---|---|
| `healthy` | status arriving, datagrams recent | no |
| `radio_silent` | status arriving, `last_datagram_age_ms` rising | **yes, upstream** — the station has lost the aircraft |
| `unreachable` | no status for >3 s | **no** — almost certainly buffering; the record completes on reconnect |
| `data_lost` | a `gap`, an intake-drop delta, or a `uptime_s` reset | **yes** — and the extent is known |
| `lagging` (P1-14) | status arriving, newest stored record older than the link timeout, and `queue_depth` higher than five `status` messages ago | **no** — the backlog is safe at the station; the map is behind by `lag_s` |

**`unreachable` is not `data_lost`.** Only a reported `gap`, a
`dropped_intake_total` delta, or a `uptime_s` going backwards means telemetry
is actually gone. Everything else is a tracking outage that resolves itself.

`lagging` needs both conditions. An old record alone may be a station clock
that is wrong (relay-v1 §9); a growing queue alone is the normal moment after
a reconnect. Precedence: `data_lost`, `unreachable`, `radio_silent`, `lagging`,
`healthy`.

`radio_silent` and `unreachable` are different failure domains
(`ARCHITECTURE.md` §4) and must never be presented identically: the first means
the ground station has lost the aircraft, which is a flight-safety event; the
second means we have lost the ground station, and the pilot still has QGC.

P6-03 carries the console half of this: alert text must not imply loss that has
not happened. A pilot who learns the alerts overstate things will discount the
one that does not.

## 10. Storage

- **Hot path → TimescaleDB** `drone_state` hypertable (P1-04 owns batching,
  chunking and retention).
- **Raw archive → files, not the database.** Hourly segments partitioned by
  station and epoch, zstd compressed, each segment a sequence of relay-v1 §6
  record frames so the archive format *is* the wire format and
  `tools/analyze_capture.py` reads it unchanged. Addressable by
  `(station_id, epoch, seq)` and by time range, sufficient for P10-03 replay.

  At ~240 MB per aircraft per day before compression, five aircraft is ~36 GB
  a month of opaque bytes nobody queries by content — after an incident you
  read a time range. Rows would buy nothing and cost an index. The **index** of
  which segment covers which interval lives in the telemetry database; the
  contents do not. Object storage is a later implementation behind the same
  interface, not a migration.
- **Events → `ingest_events` in the telemetry database**: gaps, intake-drop
  deltas, station state transitions, rejected SYSIDs, relay restarts.

  Deliberately *not* `ARCHITECTURE.md` §5's `events` table, which lives in the
  relational database. The Gateway does not connect to the relational database
  and keeping it that way is worth more than one shared table: ingest stays
  isolated from the business schema in both directions. §5's `events` is
  unchanged and remains the business audit log; the console reads both.

Units and conventions are not negotiable here: SI at the parser boundary
(1e7 lat/lon, mm→m, cm/s→m/s), altitudes stored separately and named, all
timestamps `TIMESTAMPTZ` in UTC, all geometry SRID 4326. P1-03 owns the
conversion and its property tests.

### Why there is no `alt_agl_m`, and why it must not be added back

`drone_state` carries `alt_amsl_m` and `alt_above_home_m`. It does **not**
carry `alt_agl_m`, and the field was removed from `ARCHITECTURE.md` §5 rather
than left nullable.

Nothing in the telemetry carries height above ground. From pymavlink's own
field descriptions:

| Field | Description | Datum |
|---|---|---|
| `GLOBAL_POSITION_INT.alt` | "Altitude (MSL)" | AMSL |
| `GLOBAL_POSITION_INT.relative_alt` | **"Altitude above home"** | above home |
| `GPS_RAW_INT.alt` | "Altitude (MSL)" | AMSL |
| `GPS_RAW_INT.alt_ellipsoid` | "Altitude (above WGS84, EGM96 ellipsoid)" | ellipsoid |
| `VFR_HUD.alt` | "Current altitude (MSL)" | AMSL |

`relative_alt` equals AGL only while the ground under the aircraft is at the
home point's elevation. Over rising terrain it overstates clearance.

**The tempting change is to rename `alt_above_home_m` to `alt_agl_m`, or to
fill a nullable `alt_agl_m` from it. Do neither.** The resulting error is
smooth, plausible and produces no signal anywhere: the track looks normal, the
numbers look normal, and the aircraft is lower over the ground than the data
says. It lands in the airspace monitor, where §6.2 alerts on `d_alt < 20 m` —
so a terrain difference of 20 m is enough to judge two aircraft as separated
when they are co-altitude, or the reverse.

That is why `ARCHITECTURE.md` §6.1 judges separation in AMSL. AGL returns when
**P5-00** provides a terrain source, and the column returns with it.

## 11. Direct UDP ingest

A UDP listener is retained for SITL and bench work, on
`MAVLINK_BIND_PORT`. It shares the parsing, classification and hot-path code
with the relay-v1 path — the same code must work against `sim_vehicle.py`
(CLAUDE.md, hard rule 2).

It is **not** a production path: it has no authentication, no durability and no
station identity. Anything it produces is attributed to a synthetic station so
it can never be confused with a real one.

Note that the Gateway's UDP socket is *read-only by the same rule as the
relay's*, and binds exclusively — two readers of one port split the stream and
neither can tell.

## 12. Open questions

Listed, not resolved. Each needs an answer before the code that depends on it.

1. **Token storage and rotation.** Where do station tokens live, how are they
   issued, how is one revoked mid-flight? Hashed at rest is the obvious
   starting point, but rotation while a station is connected is not obvious.
2. ~~**Raw archive medium.**~~ **ANSWERED (2026-09-23):** files, hourly
   segments partitioned by station and epoch, zstd compressed, with the segment
   index in the telemetry database. See §10. *Retention of the segments
   themselves is still unanswered* — only the epoch metadata has a retention
   rule so far.
3. ~~**Dedupe index cost.**~~ **ANSWERED (2026-09-23):** bounded per *epoch*,
   never by time. A time window would reject a relay replaying a two-hour
   backlog, which is the design working as intended. Per `(station_id, epoch)`
   the state is one `highest_contiguous_seq` plus a short list of permanent
   gaps — constant-size however old the replay is, and the same number
   `resume_from_seq` needs, so the two cannot drift apart.

   An epoch is closed when its station declares a different one, and closed
   epochs are dropped after a retention period. **Failure mode, deliberately
   chosen:** a relay reconnecting under a dropped epoch is treated as new, so
   it resends — duplicating data rather than losing it. The opposite, keeping
   the watermark and discarding the resend, looks identical in every log and
   silently loses a flight.
4. **Clock correction.** `relay-v1.md` §9 provides the monotonic/UTC pairs to
   estimate a station's clock offset, and notes `SYSTEM_TIME` carries GPS time
   at 3 Hz. Which timestamp is authoritative for `drone_state.ts`, and is the
   correction applied at write time or at read time?
5. **Backpressure.** If TimescaleDB is slow or down, what does the Gateway do?
   It must not acknowledge what it has not stored, so the relay's queue becomes
   the buffer — which is correct, but the behaviour should be deliberate and
   bounded rather than emergent.
6. ~~**Station-to-vehicle policy source.**~~ **ANSWERED (2026-09-28):**
   `source_bindings`, with no separate table. A station may carry what is bound
   on it; see §5. A separate assignments table was rejected because it would
   be a second record of the same fact, able to disagree with the first, and
   `home_base_id` because the Gateway cannot reach the relational registry.
7. **Does a `component` ever need a row of its own?** A gimbal's attitude is
   archived today. If P8 wants it live, where does it go — it is not a drone.
8. **Ordering writes from two stations whose clocks disagree.** §8 accepts two
   stations relaying one vehicle, with last-write-wins by the record's
   timestamp. Two laptops do not agree, so a station whose clock runs fast can
   overwrite fresh state with stale state, silently and without any signal that
   it happened. The constraints on whatever solves this:
   - `relay-v1.md` §9's monotonic/UTC pairs in `hello` and `status` allow each
     station's clock offset to be estimated, and a step to be distinguished
     from elapsed time — the monotonic clock cannot jump.
   - `SYSTEM_TIME` carries GPS-derived time and was observed at 3 Hz in
     ADR-001. It is the eventual authority: the one clock both stations share,
     because it comes from the aircraft rather than from either laptop.
   - Reordering must be detectable, not merely avoided. A write rejected as
     stale is information about a station's clock and belongs in `events`.
   Related to question 4, which asks which timestamp is authoritative at all;
   this one asks how to order two of them against each other.

## 13. Acceptance

From `TASKS.md` P1-02, plus what this specification adds:

- 10 SITL vehicles parsed concurrently without drops.
- No non-vehicle endpoint is ever registered as a drone.
- A station that goes unreachable is never reported as losing data unless a
  gap or a drop counter says so.
- An unbound `(station_id, sysid, compid)` is archived and surfaced as an
  unclaimed source, and never produces a `drone_state` row.
- A backlog replayed after an outage resolves its binding at the record's
  timestamp, so telemetry captured before a SYSID was reassigned is attributed
  to the airframe that produced it.
- A relay driven against the Gateway satisfies the same criteria as the P1-01
  hardware test: one epoch, zero missing seqs, zero drops, duplicates
  deduplicated.
- Every wire-format offset used is pinned by a test that derives it from
  pymavlink.

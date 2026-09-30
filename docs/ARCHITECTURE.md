# Architecture

## 1. Overview

A national drone-flight monitoring system. It observes every drone in the
airspace it can hear, from several independent sources, and shows them on one
map with zones, alerts and a flight record. It is an **observer only**: it never
commands an aircraft, and no part of it has a path by which it could.

Four services and one console.

```
                 ┌─────────────────────────────────────────────┐
                 │ Clients: operator console │ regulator (M-06) │
                 └──────────────────────┬──────────────────────┘
                                        │ REST / WS
      ┌─────────────────────────────────▼───────┐   ┌──────────────────────┐
      │ Core API + console feed                 │   │ Airspace monitor     │
      │ registry, zones, audit, replay, alerts  │   │ CPA, zones, height   │
      └─────────────────────────────────▲───────┘   └──────────▲───────────┘
                                        │ NATS                 │ NATS
      ┌─────────────────────────────────┴──────────────────────┴───────────┐
      │ Gateway: relay-v1 ingest, Remote ID ingest, ADS-B ingest (P1-16)   │
      │ receive only - nothing is ever sent towards an aircraft            │
      └───────▲─────────────────────────▲─────────────────────────▲────────┘
              │ TLS WebSocket           │ signed UDP datagrams    │ planned
      ┌───────┴────────┐        ┌───────┴────────┐        ┌───────┴────────┐
      │ Operator relay │        │ Remote ID      │        │ ADS-B          │
      │ (agent/)       │        │ receivers      │        │ receiver       │
      └───────▲────────┘        └───────▲────────┘        └───────▲────────┘
              │ QGC forwarding          │ broadcast               │ 1090 MHz
      operators' drones          any drone that             manned aircraft
      (MAVLink)                  broadcasts Remote ID
```

The registry (operators, drones and their serial numbers) is the fourth data
source; it carries no positions, and is what the other three are checked
against. See §2.

Shared infrastructure: PostgreSQL+PostGIS (registry, zones, audit), TimescaleDB
(telemetry), Redis (live drone state, TTL 15s), NATS (internal pub/sub).

## 2. Data sources

Every position the system shows comes from one of the position sources
below, and every aircraft is checked against the registry. They differ in
how far they can be trusted, and the console never presents them as
equivalent.

**Remote ID is the primary source; the operator relay is an optional
feature.** A deployment can run with the relay switched off entirely and lose
nothing the regulator depends on. The EU
model (2019/947, 2021/664) identifies every drone by Remote ID: direct
broadcast, received on the ground, and network Remote ID, supplied through
USSPs. That covers every manufacturer and needs nothing from the operator.
The MAVLink relay needs an ArduPilot or PX4 aircraft and an operator who
installs it, so it can never be how a regulator finds a drone that does not
want to be found. It stays because it is the richest feed for cooperative
operators (4 Hz position plus battery, mode, arming and GPS quality, which
conformance monitoring and incident investigation use), because it is in
effect a network-identification feed while Georgia has no USSP, and because
it is how SITL aircraft enter the system for every end-to-end test.

| Source | What it carries | Trust | Tasks |
|---|---|---|---|
| Direct Remote ID | ASTM F3411 / ASD-STAN EN 4709-002 broadcasts, decoded to Open Drone ID JSON by a receiver adapter | The receiver is authenticated (HMAC-signed datagrams); the broadcast itself is not, and is always marked unverified | P1-15, M-01, U-02 |
| Network Remote ID | ASTM F3411 network identification served by USSPs or an operator's app | Authenticated per provider; the content is as trustworthy as the provider | U-02 |
| Operator relays | The full MAVLink stream of an operator's aircraft, forwarded by QGC to a relay on the ground station and on to the Gateway | Authenticated per station (bearer token), and each `(station, SYSID)` checked against `source_bindings` | P1-01, P1-02, P1-07 |
| ADS-B / ADS-L | Manned aircraft positions from a receiver or a licensed aggregator feed | Unauthenticated broadcast | U-07 |
| Non-cooperative sensors | Detections from RF or radar sensors operated by security agencies | As trustworthy as the sensor; carries no identity | U-14 |
| Registry | Operators, drones and serial numbers: our own records now, the authority's register later | Authoritative for identity, never for position | P2-05, U-01 |

### 2.1 Source isolation and control

Each source is its own adapter process (its own container in production:
`gateway` for relays, `remote-id` for receivers, and one per source added
later). No adapter imports another, and the only thing they share is the
internal track format they publish on NATS. So one can be stopped,
redeployed or broken without touching the others.

Each source can also be switched off without a deploy, at two levels (U-15):

- **By type**: all operator relays, all Remote ID receivers, all network
  Remote ID providers, all ADS-B feeds.
- **By instance**: one station, one receiver, one provider, one feed.

A disabled source is refused at the adapter (connections declined,
datagrams dropped and counted), its tracks age out of the picture as
*source disabled* rather than silently disappearing, and the airspace monitor
stops judging them. Every switch is an audited `events` row with the actor
and a reason, and the console shows each source's state (enabled, disabled,
healthy, stale). An instance disabled by the authority is different from one
that is merely silent, and the console says which.

**Operator relays** are the richest source and the only one with a flight
record complete enough for incident investigation: the relay forwards every
datagram, unparsed and unfiltered, and buffers through internet outages (§3,
`docs/protocols/relay-v1.md`).

**Remote ID** reaches every drone that broadcasts, whatever its make and
whether or not its operator cooperates. A broadcast can be spoofed, so a
direct Remote ID track is shown as broadcast and unverified wherever it
appears. A broadcast whose serial matches a registered aircraft is one track
with that aircraft's relay telemetry, not two, and an unverified broadcast
never speaks for a registered aircraft (U-02).

**ADS-B** puts manned aviation on the same map, so that a drone converging on a
helicopter is alerted. Manned aircraft are never told to manoeuvre; the alert
goes to the drone's operator and the control centre.

**The registry** turns a serial number into an operator. An aircraft seen by
any source whose serial is not registered is itself a violation (M-02, U-02).

All sources converge on one internal track format on NATS, each published
by its own adapter (§2.1). The airspace monitor and the console subscribe to
it and do not care which source a track came from, except to show its trust
level and whether that source is enabled.

## 3. Link topology

The Gateway must not know or care how MAVLink reaches it. It receives MAVLink,
identifies vehicles by `SYSID_THISMAV` and the station that relayed them, and
never sends anything back.

### Operator relay over QGC forwarding

Nothing is installed on the aircraft. QGroundControl runs on the operator's
ground station and forwards the MAVLink stream it already receives.

```
Drone (ArduPilot) ──915 MHz──▶ Ground station PC
                                │
                                ├── QGroundControl (the operator flies here)
                                │     └── MAVLink Forwarding ──▶ 127.0.0.1:14445
                                │
                                └── relay process ──TLS──▶ Gateway
```

QGC setting: **Application Settings → General → MAVLink → Enable MAVLink
forwarding**, target `127.0.0.1:14445`.

**This link is telemetry-only, permanently.** QGC's forwarding is designed to
fan out the vehicle stream to observers, and the relay's socket is used for
`recvfrom` and nothing else (`relay-v1.md` §1). Mission upload, mode changes
and arming are the operator's, performed in QGC. This topology was called
Stage 0 when a command channel was still planned; there is no longer a later
stage, and "Stage 0" in the code and records means this receive-only
guarantee.

**Why a relay process and not a direct UDP forward to the server:**

- Raw UDP to a public endpoint has no authentication — anyone who finds the port
  can inject fake vehicle state.
- NAT means the server can never initiate anything, and UDP gives no delivery
  signal, so an internet dropout silently discards telemetry.
- The relay buffers during dropouts and replays on reconnect, so the flight
  record has no holes.

The relay reads UDP on 14445, frames, authenticates, and sends over a TLS
WebSocket with a disk-backed queue. It runs on the same PC as QGC.

**Constraints inherited from this topology**

| Constraint | Consequence |
|---|---|
| Range 1-3 km urban, 5-15 km open (SiK); more with RFD900x | An operator's aircraft is seen only while its ground station hears it |
| Two failure points (radio, ground PC) | Telemetry gaps are expected, not exceptional |
| Shared radio bandwidth | 2-3 vehicles per 57.6 kbps link, realistically |
| QGC on a controller (e.g. SIYI MK15, Android) | No relay can run there; a relay on the same network is needed (P1-01) |

Stream rates must be budgeted by the operator. With one vehicle, position at
4 Hz is fine. With three on one radio net, drop to 2 Hz position and 1 Hz for
everything else, or give each vehicle its own USB radio on a distinct `NETID`
(P1-01b).

Other ways of getting the stream to the relay — `mavlink-router` in place of
QGC forwarding, for example — are configuration on the operator's side. They
change nothing downstream, and they never add a path back to the aircraft.

## 4. Failure domains

The system is designed so that each layer degrades independently, and so that
no failure of the system can affect a flight.

| Failure                | Consequence                                        |
|------------------------|----------------------------------------------------|
| Server down            | Flights are unaffected: the system never commands them. Monitoring stops until it returns; relays buffer, so the record is complete afterwards |
| Internet down at a ground station | The relay buffers; the station shows as unreachable, not as losing data |
| QGC or ground PC down  | The operator loses their control surface and the flight controller acts per its `FS_OPTIONS`; that is the operator's responsibility. The station goes silent here |
| Radio link loss        | The aircraft's own failsafe acts; the relay stays up and reports the radio silent (`last_datagram_age_ms`) |
| Remote ID receiver down | Its broadcasts stop; relay-connected aircraft are unaffected |
| GPS loss on an aircraft | Positions stop or degrade; the console shows the fix type |

Rule: **the system is never in the flight path.** Every safety behaviour of an
aircraft lives on its flight controller or with its operator, because the
flight controller is the only component that cannot be disconnected from the
airframe.

## 5. Data model

```sql
bases(id, name, geom POINT, capacity, charging_slots)

drones(id, serial, model, sysid, status, max_payload_g, max_range_m,
       battery_capacity_wh, cruise_speed_ms, avg_power_w,
       home_base_id, current_pilot_id, firmware_version)

pilots(id, name, license_ref, status, max_concurrent_drones)

drone_state(drone_id, ts, geom POINT, alt_amsl_m, alt_above_home_m,
            heading_deg, vx_ms, vy_ms, vz_ms, batt_pct, batt_voltage, mode,
            gps_fix_type, sat_count, link_quality)      -- hypertable

remote_id_observations(...)   -- telemetry database, every broadcast (P1-15)

airspace_zones(id, name, geom POLYGON, min_alt_m, max_alt_m,
               type)   -- no_fly | restricted | corridor | base

events(id, ts, actor_type, actor_id, entity_type, entity_id,
       event_type, payload JSONB)   -- append-only audit
```

Planned: `incidents` (M-02) — a violation with its time, aircraft and serial,
operator, track excerpt and status (new, reviewed, closed); third-party
operators and their drones in the registry (M-03).

Indexes that matter: GiST on every geometry column and
`drone_state(drone_id, ts DESC)`.

**There is deliberately no `alt_agl_m`.** Nothing in the telemetry carries
height above ground. `GLOBAL_POSITION_INT.relative_alt` is documented by
MAVLink as *"Altitude above home"*, which equals AGL only while the terrain
under the aircraft is at home's elevation; `GPS_RAW_INT.alt` and `VFR_HUD.alt`
are both MSL, and `GPS_RAW_INT.alt_ellipsoid` is a third datum again.

The column is absent rather than nullable. A nullable `alt_agl_m` that is
always null is an invitation to fill it from `relative_alt`, and the resulting
error is smooth, plausible and silent — an aircraft over rising ground reads as
higher above it than it is. Height above ground is derived where it is needed,
from AMSL and the terrain model (P5-00), and never stored as if it were
measured.

## 6. Airspace monitoring

Three layers. No single layer is trusted alone. The system runs the first; the
other two belong to the aircraft and its operator.

### 6.1 Altitude datum

**Separation is judged in AMSL, not AGL.** Two aircraft are only as far apart
vertically as the difference in their heights measured from the *same datum*.
AGL does not provide one: two aircraft 15 m apart in AGL over terrain that
differs by 15 m are at the same height. The error is smooth, plausible and
unsignalled — and it lands in §6.2's `d_alt < 20 m` test, which is the last
check before an alert.

AGL is used for one thing: the height limit over the ground (P5-19). It is
computed as AMSL minus the terrain model's ground elevation (P5-00), and where
the ground is unknown the limit is not evaluated rather than evaluated against
zero.

Pre-flight flight authorisation — checking a declared route or operation
volume against zones and other declared flights — is not part of this system.
It would need operators to file plans with it, and no task does that. If it is
wanted, P5-17 (the OpenUTM evaluation, which covers ASTM F3548 flight
authorisation) decides whether it is adopted rather than built.

### 6.2 Tactical — in flight, server side

On each telemetry tick, find neighbours within 800 m and compute closest point of
approach:

```
rel_pos = p2 - p1
rel_vel = v2 - v1
t_cpa   = -(rel_pos · rel_vel) / |rel_vel|²        # if |rel_vel| > 0
d_cpa   = |rel_pos + rel_vel * t_cpa|
```

Alert when `t_cpa < 60 s` AND `d_cpa_horizontal < 60 m` AND `d_alt < 20 m`.
The thresholds are airspace policy in the database, not constants in code.
The 60 s lead time is wide because every response goes through a human (P5-14
measures whether it is wide enough).

**Advice must be deterministic** so that both operators are told compatible
things from the same data (P5-08, P5-09):

- Lower `drone_id` maintains course and altitude.
- Higher `drone_id` descends 20 m, or loiters for 45 s if descent is
  unavailable.
- The alert and the advice are recorded in `events`.

Never advise by "whoever the server contacts first" — that is not reproducible
and not auditable. The advice goes to the control centre's console and to the
operators (P5-16); the system never sends it to an aircraft.

Alongside CPA, the monitor raises zone incursions (P5-15) and the height limit
(P5-19), and, from wave M, unregistered aircraft and approaches to manned
traffic.

### 6.3 Onboard — last line

Outside this system, and listed so that nobody assumes the server is the only
layer:

- ArduPilot `FENCE_*` and `AVOID_*` parameters, set by the operator and
  enforced by the flight controller.
- The operator in QGC, who can act on an alert and on anything else they see.

## 7. Operator supervision model

Control-centre operators watch every aircraft the system hears, by exception
rather than by continuous watching. The console surfaces alerts; people act on
alerts — by contacting the aircraft's operator, never by commanding the
aircraft.

Alert types: airspace conflict, zone incursion, height limit, station
unreachable or lagging, link loss; from wave M, unregistered aircraft and
approaches to manned traffic. Every alert and every acknowledgement is recorded
in `events` with the operator's ID and a timestamp (P6-07).

Roles: `viewer`, `operator` (also acknowledges alerts), `admin` (also changes
the registry and accounts) (P6-08), and a read-only `regulator` (M-06).

## 8. Open decisions

Record the outcome of each in `docs/decisions/` as it is made.

- Gateway in Python vs Go — start Python, revisit if ingest exceeds ~50 drones.
- Adopt, integrate with, or continue beside OpenUTM (P5-17).
- The format in which the authority provides zones and the register (M-03,
  M-04).
- Real Remote ID and ADS-B receivers, or simulation, for the demonstration.

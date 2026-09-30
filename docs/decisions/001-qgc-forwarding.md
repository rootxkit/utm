# 001 — QGC MAVLink forwarding: what the link can and cannot do

- **Status:** PROPOSED — findings 1, 2 and 3 measured over USB; radio-port rates outstanding
- **Date:** 2026-09-21
- **Task:** P1-00

Everything in Phase 1 and Phase 3 rests on this. Do not fill it in from
assumption; run `tools/mavlink_probe.py` and paste real output.

## Test environment

| | |
|---|---|
| QGroundControl version | **QGroundControl Daily** (window title, 2026-09-28); exact build still **TO FILL** from Help → About. The executable's version resource reads `0.0.0.0`, so it cannot be read programmatically |
| Vehicle / autopilot | ArduPilot **4.6.3** (official, git `92b0cd78`), SYSID 1, component 1. Read from the aircraft's own `AUTOPILOT_VERSION` by P1-11 on 2026-09-28 |
| Radio link | **USB direct** — so the rates below are `SR0_*` on `SERIAL0`, not the radio port |
| Ground station OS | Windows 11 Pro 10.0.26200 |
| Vehicle SYSID | 1 (component 1) |
| GCS SYSID | 255 (component 190). Set by `GCS_SYSTEM_ID` in `QGroundControl.ini` — a user setting, not a fixed value |
| Test date | 2026-09-21 |
| Probe commit | `a20a3b4` |

The link being USB direct is the most important caveat in this document. Every
rate below is what `SERIAL0` is configured to emit, which is not what the radio
port will emit. See finding 3.

## Method

```
# QGC: Application Settings -> General -> MAVLink -> Enable MAVLink forwarding
#      Host: 127.0.0.1:14445

python tools/mavlink_probe.py listen --seconds 60 --json docs/decisions/001-listen.json
python tools/mavlink_probe.py roundtrip --param SYSID_THISMAV
```

Propellers removed. The aircraft does not need to fly, or even be on battery —
USB power is enough.

## Tool provenance — which probe produced these numbers

**Any roundtrip result produced before commit `c05ee6e` is void.** Record the
probe commit in the table above so a later reader can tell which implementation
the findings came from.

The first implementation of `roundtrip` read `param_id` at PARAM_VALUE payload
offset 4. It is at offset 8: MAVLink orders payload fields by descending type
size, so `param_value` (4 bytes), `param_count` (2) and `param_index` (2)
precede it. The old slice returned the count and index bytes instead of a name,
so the comparison against the requested parameter never matched under any
circumstances.

That version could therefore only ever print `TELEMETRY-ONLY`. It was not
capable of detecting a bidirectional link, and it would not have failed or
warned while being incapable of it — it returned the answer we expected, which
is why the defect survived review. **An expected result is not evidence.** The
only way to tell that version's output from a real measurement is the commit it
came from, which is why the table records one.

The same version also sliced payload bounds backwards from the end of the frame
(`frame[10:-2]`), which silently consumes signature bytes on a signed MAVLink v2
frame.

Scope of the damage, for the avoidance of doubt:

- **`roundtrip` results are void.** Both defects lived in `cmd_roundtrip`.
- **`listen` results are unaffected.** `cmd_listen` only calls `split_frames`
  and `decode_header`. `split_frames` accounted for the 13-byte signature block
  correctly from the first version, and `decode_header` reads fixed header
  offsets that signing does not move. Neither ever sliced a payload.

Fixed in `c05ee6e`, with the offset now pinned by a test that builds frames with
pymavlink rather than asserting against hand-written bytes
(`tools/tests/test_mavlink_probe.py`).

## Finding 1 — message inventory and rates

60-second capture. Full data in [`001-listen.json`](001-listen.json).

```
Duration        60.0s
Datagrams       5134  (2.8 KiB/s)
Source(s)       127.0.0.1:50459
Vehicles        [1]

SYSID 1 / COMP 1 - vehicle
  message                         count      Hz
  AHRS2                             601   10.00
  ATTITUDE                          600   10.00
  VFR_HUD                           600   10.00
  AHRS                              180    3.00
  GLOBAL_POSITION_INT               180    3.00
  SYSTEM_TIME                       180    3.00
  TERRAIN_REPORT                    180    3.00
  GIMBAL_DEVICE_ATTITUDE_STATUS     180    3.00
  EKF_STATUS_REPORT                 180    3.00
  VIBRATION                         180    3.00
  RPM                               180    3.00
  BATTERY_STATUS                    180    3.00
  ESC_TELEMETRY_1_TO_4              180    3.00
  SYS_STATUS                        120    2.00
  POWER_STATUS                      120    2.00
  MEMINFO                           120    2.00
  NAV_CONTROLLER_OUTPUT             120    2.00
  MISSION_CURRENT                   120    2.00
  SERVO_OUTPUT_RAW                  120    2.00
  RC_CHANNELS                       120    2.00
  RAW_IMU                           120    2.00
  SCALED_PRESSURE                   120    2.00
  GPS_RAW_INT                       120    2.00
  MCU_STATUS                        120    2.00
  HEARTBEAT                          60    1.00
  EXTENDED_SYS_STATE                 60    1.00
  GIMBAL_MANAGER_STATUS              12    0.20
  COMMAND_ACK                        10    0.20
  TIMESYNC                            6    0.10
  STATUSTEXT                          4    0.10
  PARAM_VALUE                         2       -
  not observed (event-driven, absence is not a finding): MISSION_ITEM_REACHED

SYSID 255 / COMP 190 - ground station
  message                    count      Hz
  HEARTBEAT                     59    1.00
```

**Every message the pipeline requires is present.** Nothing had to be enabled.

| Message | Rate (Hz) | Needed by | Present |
|---|---|---|---|
| HEARTBEAT | 1.00 | liveness, mode | yes |
| GLOBAL_POSITION_INT | 3.00 | position, tracking | yes |
| SYS_STATUS | 2.00 | battery percent | yes |
| BATTERY_STATUS | 3.00 | energy budget | yes |
| GPS_RAW_INT | 2.00 | fix quality, sat count | yes |
| VFR_HUD | 10.00 | ground speed, climb | yes |
| MISSION_CURRENT | 2.00 | progress inference (P3-06) | yes |
| MISSION_ITEM_REACHED | — | waypoint completion (P3-06) | not observed — event-driven, vehicle was parked and flew no mission |
| STATUSTEXT | 0.10 | FC messages, failsafe reasons | yes, 4 in 60 s |
| EKF_STATUS_REPORT | 3.00 | health alerting (P7-10) | yes |

No `SR*_` parameters were changed. Nothing needed raising.

`MISSION_ITEM_REACHED` is the one entry not directly confirmed. It is emitted
on waypoint completion, and this capture was of a stationary aircraft running
no mission, so its absence carries no information either way. **It must be
re-confirmed during the first SITL or live mission run**, because P3-06's
progress inference depends on it; if it turns out not to be forwarded, the
inference falls back to position proximity alone and is materially weaker.

### Endpoints observed

Two, correctly distinguished by HEARTBEAT identity rather than by SYSID:

| SYSID | Component | Classified | Note |
|---|---|---|---|
| 1 | 1 | vehicle | the autopilot |
| 255 | 190 | ground station | QGC itself; 190 is `MAV_COMP_ID_MISSIONPLANNER` |

QGC's own heartbeat is forwarded back into the stream. **The relay and the
Gateway must not register it as an aircraft.** Its SYSID comes from
`GCS_SYSTEM_ID` in `QGroundControl.ini`, so 255 is a default, not a guarantee,
and filtering on the number rather than on the HEARTBEAT `type` field would be
wrong. See P1-01.

No separate gimbal component appeared. `GIMBAL_DEVICE_ATTITUDE_STATUS` and
`GIMBAL_MANAGER_STATUS` arrive from SYSID 1 / component 1, i.e. the autopilot
is relaying them rather than the gimbal heartbeating in its own right. A
different airframe may well differ, which is the reason classification is keyed
on `(sysid, compid)` rather than SYSID alone.

## Finding 2 — is the channel bidirectional?

```
Waiting for a frame on 127.0.0.1:14445 to learn the peer...
Peer 127.0.0.1:50459, vehicle SYSID 1

Baseline: listening 15s without injecting anything.
  unsolicited PARAM_VALUE: 1 (0 matching SYSID_THISMAV)

RESULT: INCONCLUSIVE.
```

That was the first attempt, 2026-09-21. The re-run, 2026-09-28, with QGC left
idle on the flight view and no other ground station open:

```
Waiting for a frame on 127.0.0.1:14445 to learn the peer...
Peer 127.0.0.1:62184, vehicle SYSID 1

Baseline: listening 15s without injecting anything.
  unsolicited PARAM_VALUE: 0 (0 matching SYSID_THISMAV)

Injecting PARAM_REQUEST_READ for 'SYSID_THISMAV' every 5s for up to 45s.
Counting a reply only within 2s of an injection; 3 needed.
  attempts 9, correlated 0, uncorrelated 0

RESULT: TELEMETRY-ONLY.
```

**Conclusion: TELEMETRY-ONLY, for this QGC build over USB.** The baseline was
silent, so a reply would have been attributable; nine requests produced none.
It holds for the build measured: forwarding behaviour is undocumented and may
differ between QGC builds, which is one more reason the design never depends
on it either way.

What follows is the reasoning from the first attempt, kept because it is why
the re-run was valid.

The first probe refused to answer, and that refusal was the finding. `PARAM_VALUE`
arrives on the forwarded stream without anyone asking for it: once during the
15-second baseline, and twice during the 60-second capture in finding 1. QGC
requests parameters on its own schedule.

That makes the obvious experiment invalid. Inject a `PARAM_REQUEST_READ`, see a
`PARAM_VALUE`, and you cannot tell whether it is a reply or traffic that was
going to arrive regardless. The probe therefore measures a silent baseline
first and stops when the baseline is not silent, rather than producing a
number that would look like a measurement.

To resolve it, quiet the ground station first — close other GCS instances, let
QGC finish its initial parameter download, and leave it idle on the flight view
for a couple of minutes — then re-run. That is what the 2026-09-28 run did.

**This does not block anything.** The plan treats the channel as telemetry-only
by design, and every safety argument in `ARCHITECTURE.md` §4 depends on the
server *not* being able to reach the aircraft. A positive result here would not
change what we build; it would only inform P3B timing. Even if the channel
turned out to be bidirectional, it is undocumented and varies by QGC build, so
it would remain something to route around rather than to use.

Until the re-run it was deliberately not recorded as telemetry-only: a one-way
link and an untested link look identical from here, and writing down the
convenient one is how the original version of this probe came to be believed
for a whole session.

## Finding 3 — bandwidth

| | |
|---|---|
| Observed throughput | **2.8 KiB/s** for one vehicle (5134 datagrams / 60 s) |
| Link measured | **USB direct on `SERIAL0`** — these are `SR0_*` rates |
| Radio link capacity | 57.6 kbps ≈ 7 KiB/s for a SiK default, before protocol overhead |
| Headroom on a SiK link | one vehicle comfortably; two at the margin |
| Max vehicles on one radio net at these rates | **2**, and that is optimistic |

**This measurement does not describe the radio.** It was taken over USB, so it
reflects `SR0_*` on `SERIAL0`. The telemetry radio is a different port with its
own `SR1_*` or `SR2_*` parameters, which ArduPilot defaults lower. The radio
port must be measured separately before any multi-aircraft flight is planned;
until then, treat 2.8 KiB/s as an upper bound on what a radio would carry, not
as the figure itself.

### Most of this stream is not needed on the hot path

Of the 31 message types arriving, the pipeline's live path needs six streams
plus two event-driven messages. The remainder — roughly 80% of the traffic —
arrives because QGC asked for it, to drive its own instrument panels:

| Not needed on the hot path; retained in the raw archive | Rate (Hz) |
|---|---|
| ATTITUDE | 10.00 |
| AHRS2 | 10.00 |
| AHRS | 3.00 |
| RAW_IMU | 2.00 |
| VIBRATION | 3.00 |
| RPM | 3.00 |
| ESC_TELEMETRY_1_TO_4 | 3.00 |
| MEMINFO | 2.00 |
| MCU_STATUS | 2.00 |
| GIMBAL_DEVICE_ATTITUDE_STATUS | 3.00 |
| GIMBAL_MANAGER_STATUS | 0.20 |
| TERRAIN_REPORT | 3.00 |
| SERVO_OUTPUT_RAW | 2.00 |
| RC_CHANNELS | 2.00 |
| SCALED_PRESSURE | 2.00 |
| SYSTEM_TIME, POWER_STATUS, TIMESYNC, EXTENDED_SYS_STATE, NAV_CONTROLLER_OUTPUT | 1–2 each |

**None of this is dropped.** "Not needed on the hot path" is not the same as
unwanted. `ATTITUDE`, `VIBRATION`, `EKF_STATUS_REPORT` and `ESC_TELEMETRY` are
precisely what an incident investigation reads after a crash, and P10-03 flight
replay cannot reconstruct a message that was never recorded. The split is
between what drives live state and what is archived, not between keep and
discard:

- **The relay forwards everything, unmodified.** Its uplink is the ground
  station's internet connection, where 2.8 KiB/s per aircraft is negligible.
  Filtering there would be an irreversible decision taken at the point in the
  system with the least information about what will later matter.
- **The Gateway decides what enters `drone_state`** and what goes only to the
  raw archive. That decision is reversible, made where there is context, and —
  importantly — must not depend on any particular stream rate. What QGC
  requests is outside our control and changes when a pilot opens a different
  screen or a new QGC version ships.

The bandwidth constraint lives on the **radio link**, not the uplink, and its
budget belongs in the vehicle's `SR1_*`/`SR2_*` parameters and the P1-01b setup
document. If the radio-port measurement shows thin headroom, the options are to
reduce those rates at the vehicle or to give each aircraft its own USB radio on
a distinct `NETID`. Reducing rates at the vehicle is the only option that
actually saves air time.

## Consequences

- **P1-01 (relay):** lossless and dumb — forward every datagram unmodified, and
  do not parse MAVLink at all. Budget ~2.8 KiB/s per vehicle on the uplink. The
  wire contract is `docs/protocols/relay-v1.md`.
- **P1-02 (Gateway):** classify endpoints by HEARTBEAT identity per
  `(sysid, compid)`, so QGC's own heartbeat is never registered as an aircraft;
  decide what enters `drone_state` and what is archived, without depending on
  any particular stream rate.
- **P3-06 (progress inference):** `MISSION_CURRENT` arrives at 2 Hz, so the
  main inference signal is available. `MISSION_ITEM_REACHED` was not observed
  and could not be, with the aircraft parked — confirm it on the first mission
  run before relying on it.
- **P3B timing:** unchanged. Finding 2 is unresolved, and the manual loop has
  not yet been exercised enough to know whether it is painful. Bandwidth is not
  currently the binding constraint on a single aircraft.
- **P5-14 (pilot delay):** not addressed by this test. Latency was not measured
  — only rates and message presence. A separate measurement is needed.

## Decision

Build Phase 1 on the forwarded stream as a **telemetry-only** channel, which is
what the architecture already assumed; finding 2 did not change that assumption
and was not able to test it. Every message the pipeline needs is present at
usable rates over USB, so nothing is blocked on vehicle configuration. The
relay forwards the stream whole and unmodified: its uplink is internet, where
the cost is negligible, and discarding telemetry at the ground station would
throw away the data that P10-03 replay and any crash investigation depend on.
The Gateway, which has the context to decide reversibly, separates what drives
live state from what is merely archived. Before any multi-aircraft flight, the
radio port must be measured on its own `SR*_` parameters — the figures here are
USB and flatter the radio.

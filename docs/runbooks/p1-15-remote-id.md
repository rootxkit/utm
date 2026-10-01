# Remote ID

Drones that are not ours broadcast who and where they are (ASTM F3411,
ASD-STAN EN 4709-002). P1-15 puts them on the same map and in the same
airspace monitor as our MAVLink aircraft.

```
receiver ──UDP JSON──▶ gateway.remote_id_ingest ──telemetry.<id>──▶ console, airspace monitor
 (ESP32, phone,          decode (gateway/odid.py)
  commercial unit)       join by transmitter (gateway/remote_id.py)
                         HAE → AMSL (common/geoid.py)
```

## What a receiver sends

One UDP datagram per message or message pack it hears, to
`REMOTE_ID_BIND_HOST:REMOTE_ID_BIND_PORT` (default 127.0.0.1:14600):

```json
{"receiver_id": "rx-tbilisi-1", "transmitter": "AA:BB:CC:DD:EE:FF",
 "payload_hex": "f219030212...", "rssi_dbm": -71}
```

`payload_hex` is the Open Drone ID message exactly as broadcast: one
25-byte message, or a message pack. `transmitter` is the Bluetooth or Wi-Fi
address it came from; it is what joins a Basic ID to a Location when they
arrive separately (Bluetooth 4).

## Identity per transmitter address

An address can be reused, so a Basic ID names its address's Locations only
while it is fresh (S-32):

| Setting | Default | The identity is dropped when |
|---|---|---|
| `REMOTE_ID_IDENTITY_TTL_S` | 15 | its Basic ID has not been heard for this long (five 3 s static periods; it was a minute). |
| `REMOTE_ID_MAX_GAP_S` | 3 | the address is silent for longer than this (three 1 s Location periods): a reboot or another aircraft. |
| | | another Basic ID of the same ID type arrives from the address. System and Operator ID go with it. |

Freshness is per transmitter, across receivers. If receiver A hears the
Basic ID and receiver B only the Locations, B's Locations take A's fresh
identity (a receiver's own comes first): one identified track, not an
identified one beside an unidentified one.

A Location without a fresh identity on any receiver waits up to
`REMOTE_ID_IDENTIFY_WITHIN_S` (4 s) for a Basic ID. After that it is
published and stored as an **unidentified** track of the transmitter: id
derived from the address, labelled with the address, `remote_id.identified`
false, an empty UAS ID and ID type 0. It is never attached to an earlier
serial. A Location that was waiting and is overtaken by the next one is not
kept.

**When the serial arrives, the unidentified track is not merged into it.**
It is left to go stale, and drops out of the airspace monitor after its
15 s staleness horizon. Until then the transmitter is on the map under both
ids. The monitor never pairs an unidentified track with another track of the
same address, so the two do not conflict with each other. Every other check
runs on both, though. A zone, height or conflict condition met by the
aircraft can therefore be raised twice, once per id. The unidentified
track's alerts then clear as `stale`.

**One transmitter, two identities.** A Basic ID naming a second fresh
identity of the same ID type for an address, from any receiver, is an
anomaly: two radios on one address, or a spoofer using another aircraft's.
It is counted (`address_conflicts`) and logged at warning ("remote id
anomaly: one transmitter, two identities"), at most once a minute per
address with the count suppressed in between. Identity changes are logged
the same way. Two identified tracks on one address are judged by the
monitor like any pair, so the spoofer is checked against its victim.

The ingest logs its totals every minute and on stopping ("remote id ingest
status"):

- `published`, `refused`, `withheld`, `transmitters`;
- `unidentified`, `identity_changes`, `address_conflicts`, `silences`;
- `time_fallback_unknown`, `_invalid`, `_too_old` and `_clock_ahead`;
- `store_pending`, `store_written`, `store_dropped`.

**Our own aircraft.** If one of ours broadcasts without a fresh identity,
its unidentified track is not matched to our fleet (only a serial is), so
while its MAVLink telemetry is live the monitor can raise a conflict between
the two. This is accepted for now: the MAVLink path is leaving utm.

**Limit.** A different aircraft that takes over an address within
`REMOTE_ID_MAX_GAP_S`, while the old Basic ID is still fresh, and whose own
Basic ID is lost, is indistinguishable from the old aircraft losing a Basic
ID. Its Locations are joined to the old serial until its Basic ID arrives.

## Run it

```
infra/geoid/fetch_geoid.sh              # once: local/geoid/egm2008-2_5.pgm
GEOID_PATH=local/geoid/egm2008-2_5.pgm python -m gateway.remote_id_ingest
```

Without `GEOID_PATH` the ingest runs and the aircraft are on the map, but
they have no AMSL altitude and the monitor does not evaluate them. It says
so at start-up. `TELEMETRY_DATABASE_URL` is required: every observation is
kept there (below). The table comes from the telemetry migrations
(`0006_remote_id_observations`).

Test traffic, without a receiver:

```
python tools/remote_id_sim.py --start-lat <lat> --start-lon <lon> \
    --alt-amsl-m <m> --track-deg <deg> --speed-ms <m/s> --duration-s 90 \
    --geoid local/geoid/egm2008-2_5.pgm
```

## SITL aircraft as Remote ID (U-16)

`tools/sitl_remote_id.py` makes the SITL vehicles of `make sim` broadcast
Remote ID. For each vehicle it plays the aircraft's Remote ID module and a
ground receiver: it reads the vehicle's MAVLink, never writing to it, and
sends the Open Drone ID messages a module would broadcast. These are Basic
ID with a serial, Location, System with the take-off point as the operator
location, and Operator ID. They go to the ingest as signed receiver
datagrams.

```
python -m tools.remote_id_keys new sitl-rx-1 --file local/remote-id-receivers.keys
make sitl-rid N=3            # SYSID 1..3, serials SITLRID0001..0003
```

The ingest must have the same key file (`REMOTE_ID_RECEIVER_KEYS`) and the
same geoid (`GEOID_PATH`) as the bridge. `make sitl-rid` runs:

```
python -m tools.sitl_remote_id --count 3 --serial 'SITLRID{sysid:04d}' \
    --operator-id GEO-OP-SITL --receiver-id sitl-rx-1 \
    --key-file local/remote-id-receivers.keys --geoid local/geoid/egm2008-2_5.pgm
```

- **One vehicle:** use `--sysid 3 --serial <its serial>` instead of
  `--count`. For SITL to show as one of our aircraft (P1-15 matching),
  register that serial.
- **Ports:** MAVLink is read from UDP 14560+i (`udpin`), or with
  `--link tcp` from TCP 5760+10i. That UDP port has one reader, so the
  Gateway cannot read the same vehicle on it at the same time. For a vehicle
  on both sources, the bridge can read another stream instead:
  `--mavlink udpin:127.0.0.1:14550` reads QGC's fan-out, which carries every
  vehicle, and each vehicle takes its own SYSID from it. Under `make sim`
  the TCP port is already held by that instance's MAVProxy.
- **Heights:** the broadcast is HAE: the vehicle's AMSL altitude plus the
  `--geoid` undulation, which the ingest subtracts again. Pressure altitude
  (standard atmosphere, from `SCALED_PRESSURE`) and height over take-off are
  also sent. `--hae-source gps` uses the GPS's own `alt_ellipsoid` instead.
  That is not for SITL: SITL reports `alt_ellipsoid` equal to `alt`, so the
  ingest would put the aircraft 15 to 23 m low.
- **Time:** the Location timestamp is the vehicle's own clock (its
  `time_boot_ms`, put on UTC by `SYSTEM_TIME`), in tenths of a second after
  the hour. Until the vehicle has sent `SYSTEM_TIME` the timestamp is sent
  as unknown and no System message goes out. The ingest places the
  aircraft at that time (S-27, "Time" below); an unknown one is placed at
  its arrival.
- **Rates:** Location every `--location-period-s` (1 s). Basic ID, System
  and Operator ID every `--static-period-s` (3 s), sent as one message pack
  (`--transport pack`) or one message per datagram, as Bluetooth 4 does
  (`--transport single`). `--config file.toml` sets any flag by its name,
  e.g. `static_period_s = 3.0`.
- **Faults:** `--drop-rate 0.2` drops a fifth of the datagrams (`--seed`
  for a repeatable run). `--delay-s 2` delivers each datagram two seconds
  late, signed when it is sent. `--spoof-serial <serial>` broadcasts
  someone else's serial number (U-02).

## Pressure altitude

The AMSL altitude comes from the broadcast's geodetic (ellipsoid) altitude
through the geoid. When that altitude is missing, or the broadcast flags it
as poor, the pressure altitude is used instead (S-33).

- **Flagged poor:** its declared vertical accuracy is known and below
  `REMOTE_ID_MIN_VERTICAL_ACCURACY`. The codes are the standard's, from 1
  (under 150 m) to 6 (under 1 m). The default is 2, under 45 m, so only
  "under 150 m" is flagged. An unknown accuracy is not a flag.
- **Marked:** every observation carries `alt_source` (`geodetic`,
  `pressure`, or null with no AMSL altitude) and the raw `alt_pressure_m`.
- **Held:** once on pressure, a transmitter stays on it for
  `REMOTE_ID_PRESSURE_HOLD_S` (10 s) after its last poor geodetic altitude.
  An accuracy hovering at the threshold therefore does not flip the source,
  and the monitor's alerts with it, every message.
- **Pressure altitude is not AMSL.** It is referenced to 1013.25 hPa, not
  to the local QNH, and is off by about 8 m per hPa of difference: some
  160 m on a 20 hPa day, against a 20 m vertical minimum. The airspace
  monitor therefore treats such an aircraft's vertical position as
  unknown. A conflict with it is judged on the horizontal criteria alone,
  and its alert says `vertical_separation_known: false`, with no vertical
  distance. Inside a zone's altitude band as indicated, it raises as
  usual, a no-fly zone at critical. Inside the band widened by the
  monitor's `PRESSURE_UNCERTAINTY_M` (250 m) each way, but not the band
  itself, it raises a warning. The height limit is judged on the indicated
  height, as a warning. These alerts say `vertical_known: false`. Zones
  without altitude limits are judged as for anyone. When an active alert's
  severity changes, it is raised again under its key, so the console and
  the audit log see the change. The monitor counts these messages as
  `vertical_unknown` in its status line.
- **Stored:** the row's `geoid_model` says `pressure altitude, ISA
  1013.25 hPa`. That is not a geoid: read it as "no geodetic height".
- **Without a geoid**, there is still no AMSL altitude: pressure replaces a
  poor geodetic altitude, not a missing geoid.

## Time

The aircraft is placed at the time its Location says it was measured, not
when the ingest heard it (S-27). The broadcast carries tenths of a second
after the UTC hour; the ingest takes the hour that puts it closest to, and
not after, its own receive time plus a tolerance, so a broadcast at
12:59:59.9 heard at 13:00:00.2 is 12:59:59.9.

| Setting | Default | |
|---|---|---|
| `REMOTE_ID_TIME_TOLERANCE_S` | 1.0 | How far ahead of the ingest's clock a broadcast time may be. |
| `REMOTE_ID_MAX_LATENCY_S` | 5.0 | How old a broadcast may be on arrival and still be placed at its own time. |

Both are widened by the timestamp accuracy the broadcast declares. Outside
them, or with the time unknown, the aircraft is placed at its arrival:
`captured_at` is `rx_ts`, `remote_id.time_source` is `receiver`, and the
tracker counts the reason:

- `unknown` or `invalid`: no usable time;
- `too_old`: more than the latency bound behind the ingest's clock;
- `clock_ahead`: ahead of it by more than the tolerance. The hour choice
  then lands nearly an hour back, so anything over half an hour old is read
  this way, and `ts` is the time the broadcast claims. A steady count here
  usually means the ingest's own clock is behind. Stored rows
(`remote_id_observations.ts`) take the same placement.

## Signed receivers

A receiver outside this host must prove who it is. Make it a key:

```
python -m tools.remote_id_keys new rx-tbilisi-1 --file local/remote-id-receivers.keys
```

This appends `rx-tbilisi-1: <base64>` to the file and prints the key once,
for the receiver's configuration. Point the ingest at the file and open the
port:

```
REMOTE_ID_RECEIVER_KEYS=local/remote-id-receivers.keys
REMOTE_ID_BIND_HOST=0.0.0.0
```

The receiver adds `sent_at_ms` (its clock, ms since the epoch) and a unique
`nonce` to its JSON report. It then appends a line
`sig=<hex HMAC-SHA256 of the report bytes>`. The ingest refuses:

- an unsigned datagram;
- an unknown receiver;
- a wrong signature;
- a report more than `REMOTE_ID_MAX_SKEW_S` (30 s) from its own clock;
- a repeated nonce.

Receivers therefore need NTP. To revoke a receiver, delete its line and
restart the ingest. `tools/remote_id_sim.py --key-file` signs the way a
receiver does.

Without keys the ingest accepts unsigned datagrams, and it refuses to start
on anything but loopback.

## What is kept

Every observation the ingest publishes is also a row of
`remote_id_observations` in the telemetry database, written in batches every
half second. A row holds:

- the broadcast identity;
- the claimed position;
- both heights: the ellipsoid height as broadcast, and the AMSL height with
  the geoid model that produced it, or `pressure altitude, ISA 1013.25 hPa`
  when it came from the pressure altitude (below);
- the receiver and transmitter;
- the raw frame, so the decode can be checked later.

Remote ID has no raw archive, so if the database is down the rows are kept
in memory and retried. Up to 50,000 are kept, about ten minutes of a busy
sky. Past that the oldest are dropped, counted and logged.

Replay lists these aircraft as "(Remote ID)". It replays them from the
table, marked as an unverified broadcast. Their "flights" are the spans
they declared themselves airborne.

## How to read it

- A Remote ID aircraft is purple (red or orange while in an alert), with a
  dashed outline, and labelled
  "Remote ID". Its panel says the position is broadcast and not verified.
  Anyone can transmit one; an alert involving it is about a claimed
  position.
- Its arrow is the track over the ground; Remote ID has no heading.
- Its id is derived from its serial number, so the same aircraft keeps
  the same id across receivers and restarts.

## Verified 2026-09-29, on the laptop

- The decoder agrees with the reference library (opendroneid-core-c) on
  every field of 170 messages it encoded, and re-encodes them to the same
  bytes.
- The geoid reader agrees with GeographicLib's own Geoid class to 4e-13 m
  over 5,005 points. EGM96 puts the geoid 14.7 m above the ellipsoid at
  Tbilisi and 20.9 m at Batumi.
- One SITL aircraft hovered 30 m above home (475 m AMSL); a simulated Remote
  ID aircraft flew east through it at 480 m AMSL, broadcasting 494.7 m HAE.
  The monitor raised a critical conflict 475 m out (closest approach 1.2 m
  in 59.6 s, 3.2 m apart vertically), and cleared it after the pass. The
  console showed the Remote ID aircraft as broadcast and unverified, and the
  conflict line between the two.

## Verified 2026-09-30: storage and replay

- A simulated broadcast of 40 observations was sent before the table
  existed. The ingest held all 40 and kept retrying, logging each failure.
  When the migration ran it wrote them, and none was lost.
- Replay listed the aircraft as Remote ID, found one 39 s flight, and
  replayed all 40 samples:
  - one segment, no holes, no relay evidence;
  - `authenticated: false`;
  - 700.0 m AMSL, through EGM2008;
  - battery, mode and armed all empty, never zero.

## Verified 2026-10-01: SITL as Remote ID (U-16)

Three SITL vehicles were running (SYSID 1 and 2 hovering 80 m above home,
3 on the ground). The ingest ran with a receiver key, and the bridge sent
signed datagrams. Every position was compared with the same vehicle's
`GLOBAL_POSITION_INT`, read from QGC's fan-out.

- **SYSID 3 alone, 60 s:** 60 observations on `telemetry.*`, none refused.
  - Latitude and longitude identical (0.000 m apart).
  - AMSL within 0.05 m.
  - Height over take-off within 0.04 m.
  - Speed within 0.02 m/s.
- **The three at once (`make sitl-rid N=3`'s command), 40 s:** 40
  observations each, with the same agreement.
  - SYSID 1 and 2: airborne at 685.1 m AMSL and 80.0 m over take-off.
  - SYSID 3: on the ground.
- **Faults:** one message per datagram with 30% dropped, and a new serial
  on SYSID 1's transmitter address. 24 datagrams were sent and 17 dropped,
  and 16 observations were stored. The first 2 were stored under the serial
  of the run just before. The ingest joined messages by transmitter address
  and kept an identity for 60 s, so those Locations arrived before the new
  Basic ID. A real module does not change its serial, but a spoofer on a
  reused address would do the same. `--spoof-serial` therefore uses its own
  address. S-32 fixed this ("Identity per transmitter address" above).
- **What this does not check:** the real geoid model. It is not installed on
  the laptop, so both processes used a flat 15.9 m grid. The HAE to AMSL
  round trip is checked; EGM2008 itself is not.

## Our own aircraft broadcasting

Register the serial its Remote ID module broadcasts. Through the API,
`drones.serial` is projected to `known_drones.serial`. The placeholder tool
does the same:

```
python tools/register_aircraft.py --label hexa-01 --station tbilisi-base-1 \
    --sysid 1 --serial 1581F5FKD229400B4X
```

The ingest matches a broadcast whose serial number (ID type 1 only) is a
registered, unretired aircraft's. It re-reads the serials every minute.

| The aircraft's MAVLink telemetry | What happens to the broadcast |
|---|---|
| Heard within the last 5 s | Stored with `matched_drone_id` and not published. The MAVLink track is the better one. |
| Quiet | Published as that aircraft: its id and label, still marked as a broadcast. |

Either way it stays one track: it never becomes a second aircraft, and it
never conflicts with itself. If our link drops, the track stays on the map
from the broadcast.

## Verified 2026-10-01: S-27 and S-32, in-process

`tools/tests/test_sitl_remote_id_ingest.py` runs the U-16 bridge into the
ingest on simulated clocks: pymavlink-parsed MAVLink in, signed datagrams
through the bridge's own `FaultyLink`, then the ingest's authenticator,
tracker and store. There are two 30 s legs, one message per datagram, with
30% dropped. The second leg is a restart 4 s later with a new serial on the
same address. Its seed loses the restart's first Basic ID while two
Locations get through.

- With the rules before S-32 (60 s identity, 60 s memory), the run stores
  2 Locations of the restarted bridge under the old serial, as U-16 saw.
- With S-32's rules, 22 rows are stored after the restart, all under the
  new serial. Twenty more drop patterns store none under the old one
  either.
- With `--delay-s 2` and no drops, every observation is placed at its
  broadcast time, 1.9 to 2.0 s before its receive time (the field holds
  tenths).

No SITL vehicle was flown for this.

## Not yet
- No real receiver has been connected yet: the decoder is checked against
  the reference library's bytes, not yet against a broadcast in the air.

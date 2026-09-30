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
  as unknown and no System message goes out. The ingest does not use the
  timestamp yet (`ts` is arrival time).
- **Rates:** Location every `--location-period-s` (1 s). Basic ID, System
  and Operator ID every `--static-period-s` (3 s), sent as one message pack
  (`--transport pack`) or one message per datagram, as Bluetooth 4 does
  (`--transport single`). `--config file.toml` sets any flag by its name,
  e.g. `static_period_s = 3.0`.
- **Faults:** `--drop-rate 0.2` drops a fifth of the datagrams (`--seed`
  for a repeatable run). `--delay-s 2` delivers each datagram two seconds
  late, signed when it is sent. `--spoof-serial <serial>` broadcasts
  someone else's serial number (U-02).

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
  the geoid model that produced it;
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
- **Faults:** one message per datagram with 30% dropped. 24 datagrams were
  sent, 17 were dropped, and 14 observations were stored.
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

## Not yet
- No real receiver has been connected yet: the decoder is checked against
  the reference library's bytes, not yet against a broadcast in the air.

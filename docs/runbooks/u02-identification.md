# Network identification (U-02)

Every track, whatever its source, says who it is as the registry (U-01) sees
it, and network Remote ID from USSPs is a further source.

```
API (registry writes) ──same transaction──▶ known_drones.registration_status / uas_operator_id
                                            known_uas_operators            (telemetry DB)
                                                     │ read every REGISTRY_REFRESH_S (5 s)
          ┌──────────────────────────────────────────┼──────────────────────────┐
   gateway (relay)                      remote_id_ingest              network_rid_ingest
   resolve_bound(drone_id)          resolve(serial, operator ID)     resolve(serial, operator ID)
          └───────────── telemetry.<id> {..., "identification": {...}} ─────────┘
                                                     │
                                  airspace monitor: identification, identification_mismatch
                                  console: badge, legend, alerts
```

## The statuses

| status | when | reason codes |
|---|---|---|
| `registered` | serial registered and active, owner active, and the operator ID broadcast is the owner's (case-insensitive) | `matched`; `fleet` (our own aircraft, no UAS operator, matched on serial alone); `relay_binding` (a relay track) |
| `suspended` | the serial is registered and the UAS or its operator is suspended or revoked, whatever operator ID is broadcast | `uas_suspended`, `uas_revoked`, `operator_suspended`, `operator_revoked` |
| `unknown_operator` | serial not registered (even if the operator is), or operator ID absent, not registered, or not the owner's | `serial_unknown`, `not_a_serial` (a CAA registration or session id as Basic ID), `operator_absent`, `operator_mismatch`, `owner_unknown`, `not_in_registry` (in `known_drones` only, no relational aircraft; relay tracks too), `serial_conflict` (S-10) |
| `unidentified` | no serial at all: S-32's unidentified transmitters, and network flights whose details could not be read | `no_serial` |

`mismatch: true` whenever a registered serial comes with an operator ID that
is not its owner's (and for `serial_conflict`); such a track is never
`registered`. Operator numbers are compared on their public part,
case-insensitively: an EU number given with its hyphen and three secret
characters (`FIN87astrdge12k8-xyz`) is its owner's number.

`registered` rests on what was broadcast: the console's hint says "as
broadcast and unverified", and for our own fleet (`fleet`) that the serial
alone matched. The table and its edge decisions are in
`gateway/identification.py`.

**Relay tracks (our fleet).** A relay authenticates its station and a
binding names the aircraft, so a relay track is `registered`, or `suspended`
when the registry says the UAS or its operator is. Nothing broadcast is
compared.

## Where the registry facts come from

The Gateway never reads the relational database. The API projects what the
resolvers need into the telemetry database (migration
`0009_uas_identity_projection`): each aircraft's registration status and
owner on `known_drones`, and `known_uas_operators` (registration number,
status). It writes them inside the relational transaction of every registry
change (a failed projection write rolls the change back), and re-projects
everything at start and every `REGISTRY_PROJECTION_SYNC_S` (300 s), which
repairs a lost write. The re-projection marks a `known_drones` row with no
relational aircraft `unregistered` (migration 0010), never leaving it to
read as registered, and a failed pass, whatever the error, is logged and
retried at the next.

Every transaction that writes the projection takes a transaction-scoped
advisory lock (`api/registry.py`, `PROJECTION_LOCK_KEY`), and the
re-projection holds it from its read of the registry to the end of its
write: a change made while it runs waits for it, then lands, and is never
written over with the state read before it. Each adapter re-reads the projection every
`REGISTRY_REFRESH_S` (5 s); a failed read keeps what it holds.

Why not a NATS bucket, as U-15 does for the switches: the registry grows
with the country's fleet, past what one bucket value holds (1 MB), a status
taking effect within seconds is enough, `known_drones` was already every
adapter's identity read path, and replay and U-12's evidence packs can join
the projection in SQL.

Why resolve in each adapter rather than a separate service: identity is
decoded there, the track is published once with its status, and no second
subject or re-publication is needed for the console and the monitor to see
it. A separate resolver would add a hop and a process whose failure strips
identity from every source at once.

## The spoofing guard (S-10, absorbed)

While one of our aircraft's relay telemetry is live, a broadcast of its
serial is normally its own and is withheld (P1-15). Only live relay rows
count: a row the Gateway flagged `backlog`, or one captured more than 5 s
before the Gateway received it, is history (a relay draining its queue
after an outage) and neither makes the link live nor moves the position.
The same rule (`gateway/remote_id_match.py`, `judge`) applies to network
Remote ID flights. If the broadcast is
more than `REMOTE_ID_SPOOF_DISTANCE_M` (300 m) from where the relay last
placed the aircraft, it is not: it is published as a separate, unverified
track under the broadcast's own id, `unknown_operator` with `mismatch` and
reason `serial_conflict`, counted in `serial_conflicts`, and raises
`identification_mismatch`. With the link quiet, the broadcast still takes
over as that aircraft (P1-15), still marked as a broadcast.

## Alerts

| kind | when | severity (config) |
|---|---|---|
| `identification` | an `unidentified` or `unknown_operator` aircraft inside a PROHIBITED or REQ_AUTHORISATION zone, beside the zone alert | `IDENTIFICATION_ALERT_SEVERITY`, critical |
| `identification_mismatch` | a message whose identification says `mismatch`: any live message, on the ground or without an AMSL altitude too | `IDENTIFICATION_MISMATCH_SEVERITY`, warning |

Every zone alert also carries `detail.identification`. Both kinds raise
once, clear with the usual hysteresis (or `stale`, `source_disabled`), and
are published and audited like any alert.

**The incident seam (U-12).** The airspace service hands every raise and
clear of an `identification` alert to its `incidents` sink
(`airspace/service.py`, `Incidents`). Until U-12 the sink is
`CountedIncidentCandidates`: it logs "incident candidate (U-12 will open an
incident here)" and counts `incident_candidates_raised/_cleared` in the
status line. U-12 replaces it with one that opens and closes persisted,
audited incidents; the alert detail says `incident_candidate: true` until
then.

## Network Remote ID

`python -m gateway.network_rid_ingest`: an ASTM F3411 Display Provider
(`gateway/network_rid.py` has the field names, F3411-22a as InterUSS
publishes them, v19 shapes accepted, and the time rules). Configure
providers in `NETWORK_RID_PROVIDERS` (JSON; see `infra/.env.example`), with
the client secret only in your own `.env`. Plain HTTP is refused except to
this host.

- Areas are polled every `NETWORK_RID_POLL_S` (1 s) in tiles no larger than
  `NETWORK_RID_MAX_DIAGONAL_KM` (7 km); a 413 view is split in four, up to
  three times. A flight in two tiles is one flight.
- An unchanged state is not republished; one older than
  `NETWORK_RID_MAX_AGE_S` (60 s) is not shown. Details are reused for
  `NETWORK_RID_DETAILS_TTL_S` (60 s).
- Down, refused, or malformed: counted (`provider_errors`,
  `auth_failures`, `format_errors`, `details_failures`), logged once a
  minute per kind, retried at the next poll.
- U-15: `network_remote_id` as a type, or one provider, switched off is not
  polled at all (`skipped_disabled`, `refused_source_disabled`).
- An SP is authenticated, not trusted. A body over
  `NETWORK_RID_MAX_BODY_BYTES` (1 MiB) is refused unread (`oversize`);
  flights past `NETWORK_RID_MAX_FLIGHTS_PER_RESPONSE` (500), tiles past
  `NETWORK_RID_MAX_TILES_PER_POLL` (64, 413 splits included) and details
  past `NETWORK_RID_MAX_DETAILS_PER_POLL` (20) are counted and left
  (`flights_dropped`, `tiles_skipped`, `details_deferred`); details are
  fetched `NETWORK_RID_DETAILS_CONCURRENCY` (4) at a time; a poll stops at
  `NETWORK_RID_POLL_DEADLINE_S` (5 s) keeping what arrived
  (`deadline_exceeded`). Each response is placed at its own receive time.
- A flight whose serial is one of ours follows the direct Remote ID rule:
  withheld while our relay is live and agrees, split off as
  `serial_conflict` when it does not, ours while the relay is quiet.
- Tracks: `source: network_remote_id`, `trust: provider`,
  `authenticated: false`, provider as `station_id`. A flight whose serial is
  registered is published under the registry's id, so the same aircraft on
  direct and network Remote ID is one track.

U-17 (operators publishing to us) is not built. It needs nothing new in
the data model: an operator's client is one more provider instance of
`network_remote_id`, its tracks the same shape, identified the same way.

### The fake Service Provider

`python -m tools.fake_rid_sp` serves `/token`, `/uss/flights` and
`/uss/flights/{id}/details` from SITL vehicles it reads (receive only), so
network Remote ID runs without a USSP. Its header has the command line.

## SITL: four statuses from three vehicles

The U-16 bridge can carry several modules per vehicle (`--sysid` repeated,
each with its own `--serial`, `--operator-id` and `--transmitter`); an empty
`--serial` sends no Basic ID, an empty `--operator-id` no Operator ID. One
bridge reads QGC's fan-out (14550) for all of them:

```
python -m tools.sitl_remote_id --mavlink udpin:127.0.0.1:14550 --port 14600 \
  --geoid <grid> --receiver-id sitl-rx-u02 \
  --sysid 1 --serial U02REG0001 --operator-id GEOU02ACTIVE001 --transmitter 02:55:16:00:00:01 \
  --sysid 2 --serial U02SUS0002 --operator-id GEOU02ACTIVE001 --transmitter 02:55:16:00:00:02 \
  --sysid 3 --serial U02UNK0003 --operator-id GEOU02NOTREG99  --transmitter 02:55:16:00:00:03 \
  --sysid 1 --serial "" --operator-id "" --transmitter 02:55:18:00:00:01
```

Two modules on one vehicle are two radios at one point: the monitor pairs
them like any two aircraft and raises a conflict between them. That is
correct (two transmitters are two claims) and expected in this setup.

### Verified 2026-10-01

Three ArduCopter SITL vehicles in WSL, hovering in GUIDED (SYSID 1 and 3 at
80 m, 2 at 180 m above home). Own databases (`courier_u02`,
`courier_telemetry_u02`) migrated to head, own source-control bucket; a
flat 15.9 m geoid grid on both sides. Registry, through `UasRegistry` with
the identity projection: operator `GEOU02ACTIVE001` active; `U02REG0001`
and `U02UNK0003` active, `U02SUS0002` suspended, all three owned by it.
Remote ID ingest, network RID ingest, fake SP (SYSID 2 from UDP 14561),
airspace monitor, API and console feed ran from the branch; the bus was
recorded.

On the bus, one line per track:

| track | source | status | reason | mismatch |
|---|---|---|---|---|
| u02-registered (SYSID 1) | remote_id | registered | matched | false |
| u02-suspended (SYSID 2) | remote_id | suspended | uas_suspended | false |
| u02-suspended (SYSID 2) | network_remote_id (fake-ussp) | suspended | uas_suspended | false |
| u02-wrong-operator (SYSID 3) | remote_id | unknown_operator | operator_mismatch | true |
| 02:55:18:00:00:01 (SYSID 1, no Basic ID) | remote_id | unidentified | no_serial | false |

The network and direct tracks of SYSID 2 carry the same registry id, and
agree on position (AMSL 785.04 m against 785.1 m: the broadcast's 0.5 m
altitude step). `identification_mismatch` (warning) was raised for
u02-wrong-operator: "gives GEOU02NOTREG99; registered to
GEOU02ACTIVE001".

The console listed all four with their badges (legend: registered 1,
suspended 1, unknown operator 1, unidentified 1; u02-suspended marked
Network Remote ID, u02-wrong-operator with an "operator mismatch" badge)
and the mismatch alert in the alerts list.

A PROHIBITED zone `U02NFZ` (160 m square, 0-1500 m AMSL) 230 m east of
SYSID 1. SYSID 1 was flown east through it and back with the SITL harness:

| leg | raised | cleared (resolved) |
|---|---|---|
| east | 03:45:04.5: zone critical for u02-registered (`identification: registered`); zone critical and **identification critical** for the unidentified track | 03:45:23.5, all three |
| west | 03:46:26.5, the same three | 03:46:45.5, all three |

The registered track raised only its zone alert; the unidentified one
raised the identification alert beside it, which reached the incident seam
four times (raised and cleared, twice), logged as incident candidates.

U-15: switching provider `fake-ussp` off through the API at 03:47:44.75
stopped the polling at once (the fake SP's request count stayed at 224)
and no network track was published after it; SYSID 2 stayed on the map
through direct Remote ID. Switched on again, polling and network tracks
resumed within a second.

### Re-checked after review: a relay drain does not split our aircraft

SYSID 1 registered as a fleet aircraft (serial `U02FLEET01`) and bound on
station `u02-gs1`, its relay (`agent/`) reading UDP 14560 into the Gateway,
and the U-16 bridge broadcasting the same serial. With the relay live, the
broadcasts were withheld. The Gateway was stopped at 06:57:06 and SYSID 1
flown 440 m east; through the outage the broadcasts were published as our
aircraft (its id, 10 a 10 s). The Gateway was restarted at 06:59:26; the
relay resumed from sequence 1953 and drained 583 backlog rows in 3 s, up
to 438 m from where the aircraft's broadcast placed it at the moment of
delivery. No broadcast was split off: no `serial_conflict`, no
identification alert, and the broadcasts went back to withheld once live
relay rows arrived. The code before the fix took every relay row as live
and as the current position; that run was not repeated with it, so the
split it would have caused here is read from the code, not observed.

Evidence (not committed): `local/u02/` in the branch's worktree.

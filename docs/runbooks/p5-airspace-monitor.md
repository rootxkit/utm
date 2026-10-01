# Airspace monitor: conflicts, zone incursions and the height limit, live

`python -m airspace` follows the Gateway's `telemetry.*`, and for **armed**
aircraft raises:

- a **critical conflict** alert when a pair's closest point of approach
  (`ARCHITECTURE.md` §6.2) is within `t_cpa_max_s`, closer than
  `d_horizontal_min_m` horizontally and `d_vertical_min_m` vertically;
- a **zone** alert when an aircraft is inside an ED-269 zone of
  `airspace_zones` that applies at that moment (U-03, below): critical for
  PROHIBITED, a warning for REQ_AUTHORISATION, info or warning for
  CONDITIONAL (`airspace_policy.conditional_zone_severity`), nothing for
  NO_RESTRICTION;
- a **height** warning (P5-19) when an aircraft is more than
  `max_height_agl_m` above the ground under it. Height above ground is its
  AMSL altitude minus the DEM (P5-00). Where the ground elevation is unknown,
  the limit is not evaluated.

Thresholds are the single row of `airspace_policy` in the relational
database, seeded with the Stage 0 values: 60 s, 60 m, 20 m, 800 m radius,
and a height limit of 120 m (the owner's figure; there is no minimum). Remote
ID aircraft declared airborne are evaluated like armed ones.
Each alert is published on `alert.<key>`, written to `events` when raised
and when cleared, and republished every second while active so the console's
numbers are current. The console lists active alerts, sounds a tone for an
unacknowledged critical one, and shows alerts raised before it was opened.

## Running it

```
make migrate-relational          # airspace_policy, airspace_zones
python -m airspace               # needs DATABASE_URL, NATS_URL (.env)
```

The Gateway and the console must be running for anything to reach it or be
seen. `TERRAIN_DIR` (`docs/runbooks/p5-00-terrain.md`) is needed for the
height limit. Without it, the service logs at start-up that the limit will
not be evaluated. Zones and the height limit are re-read every minute:

```sql
UPDATE airspace_policy SET max_height_agl_m = 150;   -- NULL: no limit
```

## Geographical zones (U-03)

Zones follow EUROCAE ED-269 (`airspace/ed269.py`; migration
`0006_geo_awareness`). Each has an identifier, a restriction, reasons, a
message, the authority, when it applies (permanent, or dates and a weekly
schedule) and one volume: a polygon or a circle between a lower and an upper
limit, each limit with its own reference.

| Limit reference | Compared with | Needs |
|---|---|---|
| AMSL | the aircraft's AMSL altitude | nothing |
| AGL | AMSL less the ground under it (DEM) | `TERRAIN_DIR` |
| WGS84 | AMSL plus the geoid undulation (height above the ellipsoid) | `GEOID_PATH` |

A lower AGL limit at or below the ground is met by any airborne aircraft and
needs no DEM. Where a zone the aircraft is horizontally inside has a limit
whose data is missing:

- a PROHIBITED or REQ_AUTHORISATION zone whose only unjudged limit is above
  the ground raises a **warning** with `vertical_known: false` and
  `limit_not_judged: true` (`zone_limits_not_judged`): a false warning beats
  a missed critical. While any PROHIBITED zone needs terrain and
  `TERRAIN_DIR` is unset, the start-up log and every status line are at
  error level;
- otherwise (CONDITIONAL, or a WGS84 limit without the geoid) it is **not
  evaluated**: no alert, an active one is neither refreshed nor cleared, and
  `zone_checks_not_evaluated` counts it.

On a pressure altitude (S-33) each judged limit is widened by
`pressure_uncertainty_m`: inside as indicated keeps the zone's severity,
inside the widened limits only is a warning (`within_band: false`).
Applicability is judged in UTC at the track's placed time (`captured_at`).
Circles are judged on the WGS-84 ellipsoid, every zone's bounding box first,
and a ring may have at most `ZONE_MAX_RING_VERTICES` (5000) positions.

Zones are written three ways, every change audited in `events`:

- the console's **Zones** tab: draw a polygon or a circle on the map, set
  the fields, save (admin; U-13 adds the regulator);
- **ED-269 import**: `POST /airspace/zones/import?dry_run=true` with the file
  as the body reports what would be created and replaced; without
  `dry_run` it imports, all or nothing, by identifier. The console does the
  same from a file. A refusal names each field and why;
- **airspace.gov.ge**, which publishes no feed: `tools/gov_ge_zones.py`
  converts saved copies of its `points.js` and page with a rules file from
  the authority (restriction, limits and times per kind of zone, which the
  site does not publish) into an ED-269 file to import.

`GET /airspace/zones/export` writes every geozone as ED-269. The monitor
re-reads zones every 60 s (`ZONE_REFRESH_S`), so a change alerts within a
minute.

### Verified in SITL, 2026-10-01

SYSID 3 on Remote ID only, through the U-16 bridge (flat 15.9 m geoid grid
on both sides; no DEM installed), hovering at 685.1 m AMSL and flown east
and west along one line at 10 m/s. The ingest, the monitor and the API ran
from the branch. Zones were created through the API in the editor's shape,
and one was drawn and edited in the console itself. From the bus, in UTC:

| Run | Zone state | Inside U03AMSL (20 samples each run) | Alert |
|---|---|---|---|
| 1 | U03AMSL, PROHIBITED, 600-800 m AMSL, permanent | 01:22:55-01:23:14 | critical raised 01:22:54.9, cleared (resolved) 01:23:16.9 |
| 2 | the same zone, window 1-30 September | 01:25:17-01:25:37 | none |
| 3 | permanent again | 01:27:12-01:27:31 | critical raised 01:27:11.9, cleared 01:27:33.9 |
| 4 | plus U03UI, drawn in the console, CONDITIONAL, edited there to 600-800 m AMSL | 01:33:31-01:33:50 | U03UI warning raised 01:33:21.9, cleared 01:33:34.9; U03AMSL critical raised 01:33:30.9, cleared 01:33:53.9 |

A third zone, REQ_AUTHORISATION 0-120 m AGL over the same square, raised
nothing in every run and was counted as not evaluated (20 checks a pass),
with the log naming `TERRAIN_DIR`. That was the rule then; the review
changed it (above), and it was re-run after merging S-33:

- U03AGLP, PROHIBITED, 0-120 m AGL, no DEM, the same pass (20 samples
  inside, 02:05:55-02:06:13): **warning** raised 02:05:56.4 with
  `vertical_known: false`, `limit_not_judged: true`, `not_judged: ["AGL"]`;
  cleared (resolved) 02:06:19.4. Both are `events` rows. From its load
  onwards, the start-up line and every status line were errors naming it. Zones were created at 01:20:55 and loaded at
01:21:43, inside the 60 s refresh. Each transition is an `events` row.

## Result, 2026-09-29

Two ArduCopter SITL aircraft (SYSID 201, 202; homes 25 m apart), commanded by
a harness in GUIDED at 30 m, with a 120 m square restricted zone 300 m north
of home. From the monitor's log, in UTC:

| Time | What the aircraft did | Alert |
|---|---|---|
| 20:26:34 | armed and climbing, 25 m apart | conflict raised (25 m < 60 m, zero relative velocity) |
| 20:27:07 | separated, 59.4 m and opening | conflict cleared |
| 20:27:26 | SITL-01 enters the zone | zone warning raised |
| 20:27:51 | head-on, 589 m apart, closing | conflict raised: CPA 2.8 m in 57.2 s |
| 20:28:00 | SITL-01 leaves the zone | zone warning cleared |
| 20:28:27 | passed each other, 58.3 m and opening | conflict cleared |
| 20:28:46 | SITL-02 enters the zone | zone warning raised |
| 20:29:25 | both returning home, converging | conflict raised: CPA 42.7 m in 57.7 s |
| 20:29:32 | SITL-02 leaves the zone | zone warning cleared |
| 20:29:57 | SITL-02 stopped, 204 m from SITL-01 (see the last note below) | conflict cleared |

16 `events` rows: one per aircraft per transition. The console showed the
head-on alert with both labels and a critical badge.

## What the run showed that tests had not

- **The first run raised nothing, correctly.** The harness believed both
  aircraft armed; the recorded `drone_state` showed them disarmed on the
  ground throughout, so the monitor had nothing to alert on. The harness now
  checks every step against the vehicle's own messages.
- **A console opened after an alert showed its numbers from the moment of
  raising** ("in 57 s" long after). Active alerts are now republished every
  second.
- **CPA is a straight-line prediction, and the clear on the return was
  correct.** Read from the recorded tracks with the P10-03 replay on
  2026-09-29 (positions and finite-difference velocities from `drone_state`):
  SITL-01 was stationary from 20:29:15 on, 0.0 m/s. SITL-02 flew towards it at
  10 m/s from 20:29:21, which projected in a straight line to a closest
  approach of about 42 m - hence the alert at 20:29:25. It decelerated from
  20:29:51 and stopped at 20:29:56, 204 m from SITL-01, and the pair stayed
  205 m apart until the recording ended at 20:30:30. The conflict cleared
  at 20:29:57, a second after the stop. So nothing was missed: the alert was
  the conservative side of a linear prediction, which cannot know an
  aircraft will stop short. The earlier note here guessed the opposite
  (that deceleration hid a real conflict); the record does not support it.
  The case where slowing *does* hide a conflict - an aircraft slowing to
  hover near another - is still worth a scenario of its own under P5-12.

## Height limit, 2026-09-30

One SITL aircraft at Kazbegi (`local\capacity\run\58-height-limit.bat`)
held 100 m above home, 1,861 m AMSL, and flew 1 km north and back. Neither
its altitude nor its height above home changed. Only the ground did: it
falls from 1,761 m at home to about 1,720 m.

| | Time (UTC) | Height above ground | Ground |
|---|---|---|---|
| Monitor raised the warning | 06:38:03 | 120.3 m | 1,740.2 m (COP-DEM GLO-30) |
| Flight controller's own `TERRAIN_REPORT` crossed 120 m | 06:38:06 | 120.2 m | 1,740.4 m (ArduPilot terrain) |
| Flight controller back under 120 m | 06:39:36 | 119.9 m | 1,741.2 m |
| Monitor cleared the warning | 06:39:42 | | |

The monitor raised the warning within 0.3 m of the limit. The flight
controller, working from an independent terrain source, crossed the same
line 3 s (about 30 m of flight) later. The warning cleared once the
aircraft had been below the limit for longer than the hysteresis. Both
transitions are in `events`.

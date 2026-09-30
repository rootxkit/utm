# Airspace monitor: conflicts, zone incursions and the height limit, live

`python -m airspace` follows the Gateway's `telemetry.*`, and for **armed**
aircraft raises:

- a **critical conflict** alert when a pair's closest point of approach
  (`ARCHITECTURE.md` §6.2) is within `t_cpa_max_s`, closer than
  `d_horizontal_min_m` horizontally and `d_vertical_min_m` vertically;
- a **zone** alert when an aircraft is inside a `no_fly` (critical) or
  `restricted` (warning) zone of `airspace_zones`, within its AMSL band;
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

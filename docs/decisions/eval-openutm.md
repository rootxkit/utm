# Evaluation: OpenUTM Flight Blender beside this system (P5-17)

Status: **stage 1 of 2 run, 2026-09-29.** No decision yet.

## What was run

Flight Blender at commit `be709a7` (openutm/flight-blender, Apache-2.0),
built from source and run in Docker on the development laptop beside our
own stack: API, Celery worker, Celery beat, PostgreSQL 17, Valkey. Port
8090, auth bypass (local evaluation mode). Nothing of ours was changed; a
bridge outside the repository forwarded the Gateway's `telemetry.*` to
Blender's `/flight_stream/set_air_traffic`, one observation per message.

Then the P5-07 conflict flight was flown again with two ArduCopter SITL
aircraft (hover 25 m apart, head-on pass, restricted-zone entries, return),
watched by our airspace monitor and by Blender at the same time. Blender was
polled every 10 s for traffic (`get_air_traffic`) and notifications.

## Observed

| | This system | Flight Blender |
|---|---|---|
| Ingest | 2,066 telemetry messages | 1,858 accepted (201), 0 refused, 0 failed; the other 208 had no position yet and were not sent. 3,826 worker task lines, no errors |
| Live traffic | map, 4 Hz | positions and altitudes returned for both aircraft on every poll that followed new data |
| Conflicts | 4 raised / cleared transitions per aircraft (hover, head-on at t_cpa 57.2 s and CPA 3.4 m, return) | **none**: no notification during the whole flight |
| Zone entries | 2 raised / cleared per aircraft | not tested: the zone was not given to Blender (see stage 2) |
| Audit | 16 `events` rows | - |

Read from the code at the same commit, not yet run:

- There is no in-flight, track-to-track conflict detection in the API or
  the worker's task list. The README lists F3442 "alerts / near misses";
  no code for it was found by name (`F3442`, `near_miss`, `closest`,
  `proximity`, `conflict` outside strategic deconfliction).
- Conflict handling that exists is **strategic**: flight declarations and
  operational intents checked against each other before flight (ASTM
  F3548), with a pluggable deconfliction engine.
- Conformance monitoring checks a **declared** flight's telemetry against
  its 4D volume. Undeclared traffic, which is what a monitoring authority
  mostly sees, is not conformance-monitored.
- `get_air_traffic` returns only rows newer than the caller's last read,
  keyed by a session id in the URL: reads are stateful, and position,
  altitude and metadata are all it keeps of an observation. Velocity passed
  in `metadata` is stored but not used.
- The display, Flight Spotlight, needs the Flight Passport OAuth server and
  a Mapbox key; it was not run in stage 1.

## Reading so far

Blender is a standards-shaped backend for the *planning and identification*
side of U-space: registration-adjacent flight declarations, strategic
deconfliction, network Remote ID, geo-zones, conformance of declared
flights, interoperability through a DSS. It did not, in this run or in its
code, do what this system's airspace monitor does: watch live traffic,
declared or not, and alert on a predicted conflict or a zone incursion as it
happens. The two overlap less than their descriptions suggest.

## Stage 2 (not run)

1. The same restricted zone given to Blender as an ED-269 geo-zone, and both
   flights declared, to see its geo-awareness and conformance alerts on the
   same flight.
2. Flight Spotlight with Flight Passport, to judge the display an operator
   would use.
3. A decision with the owner: adopt Blender for the planning and
   identification side and keep the tactical monitor, integrate through its
   API or a DSS, or continue alone.

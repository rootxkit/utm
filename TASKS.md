# Work breakdown

Every task has an ID, a deliverable, and an acceptance criterion. A task is done
when the criterion is demonstrable, not when the code compiles. Reference the ID
in the commit message.

Status: `[ ]` todo · `[~]` in progress · `[x]` done · `[!]` blocked

---

## Phase 0 — Foundation and SITL (3-5 days)

Goal: a reproducible development environment where multiple simulated drones can
be launched and observed. No real hardware.

- [x] **P0-01** Monorepo skeleton: directories per `CLAUDE.md` layout, `README`,
      `.gitignore` (exclude `.env`, `*.bin`, `*.tlog`, `logs/`, SITL artifacts).
      *Done when:* `tree -L 2` matches the documented layout.

- [x] **P0-02** Git hygiene: `.githooks/commit-msg` strips AI attribution
      trailers, `.claude/settings.json` disables attribution, `make hooks`
      installs via `core.hooksPath`.
      *Done when:* a commit containing `Co-Authored-By: Claude` comes out clean.

- [x] **P0-03** `docker-compose.dev.yml`: PostgreSQL 16 + PostGIS 3.4,
      TimescaleDB, Redis 7, NATS. Healthchecks on all four. Named volumes.
      *Done when:* `make up` reaches healthy on all services from a cold start.

- [x] **P0-04** SITL launcher `sim/run_sitl.sh` — N instances, unique SYSID per
      instance, configurable home location, distinct UDP out ports.
      *Done when:* `make sim N=10` gives 10 vehicles with distinct SYSIDs.
      *Closed* on the first green `sitl` job (PR #5): three vehicles launched
      with distinct SYSIDs and the integration tests passed against them.
      *Fully closed* 2026-09-25: ten vehicles launched from WSL appeared in
      QGC on the Windows side as SYSIDs 201-210, in a row near Kukia Cemetery,
      Tbilisi. That is the visual confirmation CI could not give.

- [x] **P0-08** WSL2 environment for SITL per `docs/DEV_SETUP_WSL.md`:
      mirrored networking and ArduPilot SITL built locally.
      *Done when:* `make sim N=3` runs locally and all three vehicles appear in
      QGC on the Windows side.
      *Closed* 2026-09-25 with ten, not three.
      *Scope corrected while doing it:* the task said "repo on the WSL
      filesystem" and "Docker integration", and both were wrong. Only SITL
      lives in WSL. The repository, Gateway, relay agent, console, tests and
      Docker stack stay on Windows, because the relay ships to a pilot's bare
      Windows laptop and developing it anywhere else tests something we do not
      fly. `docs/DEV_SETUP_WSL.md` is rewritten around that.

- [x] **P0-05** Python tooling: `ruff`, `mypy` config, `pytest` with async
      support, shared `pyproject.toml` conventions.
      *Done when:* `make lint` and `make test` pass on an empty repo.

- [x] **P0-06** CI pipeline (GitHub Actions): lint, typecheck, unit tests on
      every push; SITL integration job on PR to `main`.
      *Done when:* CI is green on the P0 branch.
      *Closed* on PR #5, the first run where the `sitl` job built ArduPilot and
      ran the integration tests rather than failing in setup. The job is also
      dispatchable manually (`workflow_dispatch`), so the next branch can prove
      it green before opening a PR.

- [x] **P0-07** Structured logging and config loading shared library
      (`common/`): JSON logs, env-based config with validation, no bare prints.
      *Done when:* every service imports it and no service reads `os.environ`
      directly.

- [~] **P0-09** Staging server on the owner's DigitalOcean droplet: the dev
      stack plus Gateway, API, console and airspace monitor under Docker
      Compose, TLS on the owner's domain, SSH-key access only (no root
      password), nightly backups of both databases, and nothing listening
      publicly except HTTPS and the relay's WSS endpoint. The API and console
      are exposed only after P6-08.
      *Done when:* the console is reachable over HTTPS from another network,
      a relay on the laptop delivers to it, and restoring last night's backup
      into a scratch database succeeds.
      *Added* 2026-09-29 with the owner, for the monitoring direction.
      *Partial* 2026-09-30, on the droplet in Frankfurt (2 vCPU, 4 GB),
      `docs/runbooks/p0-09-staging.md`:
      - `https://utm.chikox.net` serves the console and API; Caddy holds
        Let's Encrypt certificates for it and for `ingest.chikox.net`.
      - The laptop relay delivered a SITL flight over
        `wss://ingest.chikox.net`: 318 rows and 8,847 archived records.
      - A nightly backup is taken, and a restore into throwaway databases
        read back 631 `drone_state` rows.
      - A merge to main deploys itself once CI passes.
      Still to do:
      - The owner applies the firewall and key-only SSH.
      - The backups are copied off the droplet.

---

## Phase 1 — Telemetry pipeline (1-1.5 weeks)

Goal: telemetry from many vehicles reaches the database and a browser map.

- [~] **P1-00** Verify QGC forwarding behaviour empirically before building on
      it. Run `tools/mavlink_probe.py listen` and `roundtrip` against the real
      aircraft and fill in `docs/decisions/001-qgc-forwarding.md`.
      *Done when:* the decision record contains measured output, not
      assumptions. Everything downstream depends on this being accurate.
      *Partial:* findings 1, 2 and 3 measured over USB direct. Finding 2 is
      TELEMETRY-ONLY (2026-09-28: silent baseline, 9 requests, 0 replies).
      Firmware recorded (ArduPilot 4.6.3). Outstanding — the exact QGC build
      (Help → About; the window says "Daily"); radio-port rates, which on this
      aircraft go through a SIYI MK15 whose QGC runs on the controller itself,
      so they are measured once forwarding from the controller reaches a relay
      (see P1-01). `MISSION_ITEM_REACHED` no longer needs confirming: it
      served progress inference, which left with the delivery scope.

- [~] **P1-01** Ground relay process: read UDP 14445, authenticate, forward to
      Gateway over TLS WebSocket, disk-backed queue that replays after an
      internet dropout. Runs as a Windows service or tray app on the ground PC.
      **The relay is lossless and dumb.** It forwards every datagram it
      receives, unmodified and unparsed. It does not filter, does not identify
      vehicles, and does not interpret MAVLink at all.
      Filtering here would be irreversible: the messages the hot path does not
      want — `ATTITUDE`, `VIBRATION`, `EKF_STATUS_REPORT`, `ESC_TELEMETRY` —
      are exactly the ones an incident investigation needs, and P10-03 flight
      replay cannot reconstruct what was never sent. The relay's uplink is
      internet, where ~2.8 KiB/s per aircraft is negligible; the bandwidth
      constraint is the radio link, which is a vehicle-side concern (P1-01b).
      The wire contract is [`docs/protocols/relay-v1.md`](docs/protocols/relay-v1.md).
      *Done when:* pulling the network cable for 2 minutes results in zero lost
      telemetry rows once it reconnects, and the relay conforms to relay-v1.
      *Partial:* Procedure B (stop and restart the receiver) passed on
      2026-09-22 over loopback — see `docs/runbooks/p1-01-test-records.md`. The
      sink resumed from its own disk and the relay refilled the gap exactly.
      Outstanding: Procedure A over a real LAN, which also covers TLS and the
      half-open-connection detection path that a connection refusal never
      exercises.
      QGC-to-relay integrity over USB, 2026-09-28: 10 minutes, 49,812 frames
      from the aircraft, 0 lost by MAVLink sequence, 0 relay sequence
      discontinuities, 2,857 B/s (read from the Gateway's archive).
      **Open question for the field:** the aircraft's SIYI MK15 runs QGC on the
      controller (Android), where this relay cannot run. QGC forwarding is
      plain UDP with no authentication and no buffering, so it must not be
      pointed at the internet; it needs a relay on the same network, or a
      relay on the controller. To be settled with the staging server.

- [ ] **P1-01b** QGC setup documentation: forwarding configuration, stream rate
      tuning (`SR*_` parameters), multi-vehicle SYSID assignment, radio `NETID`
      separation. Written so a pilot can follow it without help.
      Includes the **radio-link stream-rate budget**. This is where bandwidth
      is actually scarce, and it is set in the vehicle's `SR1_*`/`SR2_*`
      parameters on the telemetry port — not in software downstream. ADR-001
      measured `SR0_*` over USB at 2.8 KiB/s for one aircraft; the radio port
      has its own rates and must be measured separately before any
      multi-aircraft flight.
      *Done when:* a second person sets up a ground station from the doc alone,
      and the documented budget is backed by a measurement of the radio port.

- [x] **P1-02** Gateway ingest: async UDP listener plus the relay-v1 WebSocket
      endpoint, MAVLink parse, vehicle identification. Handle `HEARTBEAT`,
      `GLOBAL_POSITION_INT`, `SYS_STATUS`, `BATTERY_STATUS`, `GPS_RAW_INT`,
      `VFR_HUD`, `STATUSTEXT`, `EKF_STATUS_REPORT`.
      **Classify sources by HEARTBEAT identity, per `(sysid, compid)`.** The
      relay forwards everything it hears, which includes QGC's own heartbeat,
      and a gimbal or companion computer may heartbeat under the vehicle's
      SYSID with a different component ID. Never register a ground station or a
      component as a vehicle. Classify on the HEARTBEAT `type` and `autopilot`
      fields — never on the SYSID number, since `GCS_SYSTEM_ID` is a user
      setting, and never on message volume, since a just-booted aircraft has
      sent one HEARTBEAT and nothing else, which is exactly when it must stay
      visible. Ambiguity resolves to vehicle. `tools/mavlink_probe.py` has a
      working implementation to lift.
      **Decide what enters the hot path.** `drone_state` takes the messages the
      pipeline needs; everything else goes to the raw archive for P10-03 replay
      and incident investigation. Nothing here may depend on a specific stream
      rate — QGC's own settings determine what `SR0_*` emits, and they change.
      **Distinguish "unreachable" from "losing data".** The Gateway declares
      a station unreachable after three missed `status` messages, about 3 s,
      while the relay does not give up on a half-open uplink for up to 25 s
      (`relay-v1.md` §8). For that window the two disagree, and the relay is
      alive and buffering correctly throughout it — nothing is deleted,
      because no acknowledgement can arrive through a dead link. The station
      state the Gateway publishes must carry that distinction: "unreachable,
      data buffered at the station" is not "data lost". Only a `gap`, an
      intake-drop delta, or a `uptime_s` reset means telemetry is actually
      gone.
      *Done when:* 10 SITL vehicles are parsed concurrently without drops,
      no non-vehicle endpoint is ever registered as a drone, and a station
      that goes unreachable is never reported as losing data unless a gap or
      a drop counter says so.
      *Closed* 2026-09-28 on three pieces of evidence:
      - **11 SITL vehicles at once.** On 2026-09-28 (the P1-13 run),
        254,315 records were stored with zero intake or cap drops and no
        conversion or write errors. Drain exceeded intake with a steady
        baseline.
      - **No non-vehicle registered.** `test_classify.py` covers this, and
        `test_binding.py` shows that a ground station never resolves to a
        drone even when bound.
      - **Unreachable is not data loss.** `test_station_state.py` and
        `test_publisher.py` keep the two states separate.

      One transient is expected, not a fault: each aircraft is logged once as
      unclaimed at startup, before its first HEARTBEAT.

- [x] **P1-03** Unit conversion at the parser boundary: 1e7 lat/lon scaling,
      mm→m altitude, cm/s→m/s velocity. Both AGL and AMSL preserved separately.
      *Done when:* property tests confirm round-trip accuracy and no field is
      stored in raw MAVLink units.
      *Closed* 2026-09-28. `test_conversion_roundtrip.py` sends every
      `GLOBAL_POSITION_INT` field across its whole wire range (the extremes
      plus 400 seeded draws each) through a real pymavlink frame and the
      ingest pipeline into a row. It then recovers the exact wire integer
      using factors defined independently of `gateway/units.py`. A paired test
      shows no row field equals its raw value.
      Battery, GPS and VFR fields are covered by the example tests in
      `test_conversion.py`, not by property tests.

- [x] **P1-04** TimescaleDB writer: batched inserts (flush on 100 rows or 500 ms),
      hypertable with 7-day chunks, 90-day retention policy.
      *Done when:* 10 drones at 4 Hz sustain writes with insert latency p99
      under 50 ms.
      *Closed* 2026-09-28. `BufferedStateWriter` inserts on 100 rows or
      500 ms, whichever comes first (`STATE_FLUSH_ROWS`,
      `STATE_FLUSH_INTERVAL_S`). The hypertable, 7-day chunks and retention
      were already in migration 0004.
      Measured with 11 bound SITL aircraft at 4 Hz for 180 s (the criterion is
      10):
      - 331 inserts of ~24 rows each, 44 rows/s;
      - insert latency **p99 19.1 ms**, p50 11.1 ms, one outlier at 100.7 ms;
      - no errors.
      Every insert is timed and logged, so the p99 is computed over the whole
      run, not per window.

- [x] **P1-05** Redis live state: `drone:{id}:state` with 15 s TTL. Expiry is
      the definition of "link lost".
      *Done when:* killing a SITL instance flips its status within 20 s.
      *Closed* 2026-09-28. An 11-aircraft SITL run was used, and one instance
      was killed. It flipped to link-lost in 14.9 s. The other ten stayed
      live throughout. See `docs/runbooks/p1-05-link-loss.md`.
      Expiry counts from **capture** time, capped at now plus the timeout:
      - A replayed backlog never makes a lost aircraft look live.
      - An older record never overwrites a newer one; the check is a
        compare-and-set in Redis.
      - A station clock running behind fails toward "link lost".
      Correcting station clocks is still spec §12 question 4.

- [x] **P1-06** NATS publication: `telemetry.{drone_id}`, `events.{type}`.
      *Done when:* a test subscriber receives every position update.
      *Closed* 2026-09-28. `test_publisher_nats.py` drives 1,000 position
      updates through the pipeline into a real NATS broker. A separate
      subscriber receives exactly 1,000, in order, with none duplicated. A
      paired test shows a HEARTBEAT alone publishes nothing.

- [x] **P1-07** Gateway authentication: **per-station bearer token** on the
      relay-v1 upgrade request, plus a server-side policy check binding
      `(station_id, sysid)`. Unauthenticated packets dropped and
      rate-limit-logged.
      Revised from "per-vehicle token" by `docs/protocols/relay-v1.md` §3. A
      ground station relays whatever its radio hears, so it cannot hold one
      credential per aircraft; it authenticates as itself. Which vehicles a
      station may carry is policy the server evaluates, not an assertion the
      station is trusted to make. The security property is unchanged — a
      spoofed SYSID is rejected — but it is enforced where the policy lives,
      and a compromised ground station cannot mint vehicles it was never
      assigned. Direct UDP sources (SITL, bench testing) keep their own path.
      *Done when:* a spoofed SYSID is rejected, and a valid station presenting
      a vehicle it is not assigned is rejected and logged.
      *Closed* 2026-09-28. The policy is `source_bindings` (spec §12 question
      6). An address bound only on another station resolves to
      `not assigned`. It is archived, not attributed, and recorded as a
      `rejected_source` event. It is published on its own subject, so the map
      never offers to register it. It is logged at most once per minute per
      address with the suppressed count. 401s are rate-limited the same way.
      Tested against PostgreSQL in both directions: rejected elsewhere,
      resolved on the assigned station, accepted on both during a handover,
      and unclaimed when bound nowhere.
      *Not covered:* a compromised station replaying a SYSID that *is* bound
      on it. That needs MAVLink 2 signing on the vehicle, outside Stage 0.
      Token storage and rotation remain spec §12 question 1.

- [x] **P1-08** WebSocket endpoint + minimal map page: MapLibre, one marker per
      drone, heading arrow, battery label.
      *Done when:* 10 markers move in the browser with under 500 ms end-to-end
      latency.
      *Closed* 2026-09-25: 11 drones listed, 10 markers placed from SITL, and
      `hexa-01` correctly listed as present but unplaced.
      *Known gap, not blocking:* the map has no base layer. The style is
      MapLibre's demo style, whose tiles stop at zoom 6 and contain only
      country outlines, so at city zoom there is nothing to draw. Choosing a
      tile provider is its own decision, with licensing attached; see P6-01.

- [x] **P1-09** Link-quality tracking: packet loss, round-trip latency,
      heartbeat gaps per vehicle.
      *Done when:* the metric degrades measurably under simulated packet loss.
      *Closed* 2026-09-28, for packet loss and heartbeat gaps. Both are
      measured per vehicle, over a 10 s window, published with every
      telemetry message and shown on the map.
      11 SITL aircraft were run through a forwarder that first dropped
      nothing, then 20.07% of datagrams. Measured loss (median of 11):
      - clean phase: 0.0%;
      - lossy phase: 20.16%.
      The longest heartbeat gap rose from 1.05 s to 3.98 s. See
      `docs/runbooks/p1-09-link-quality.md`.
      *Round-trip latency is dropped.* It needs something sent and answered,
      and the system never sends anything to an aircraft.

- [x] **P1-10** Ingest capacity: measure intake and drain separately, find the
      bottleneck, and turn relay-v1 §10 into measured numbers.
      **Highest priority in Phase 1.** §10 claims a 30-minute outage "drains in
      seconds". That was bandwidth arithmetic only, and it is false: on
      2026-09-25, after a deliberate two-minute Gateway outage with 11 sources,
      the relay queue grew by roughly 790 records/s and never drained.
      Why it matters more than it looks. If drain rate is at or below intake
      rate, *any* outage becomes permanent lag rather than a recoverable
      backlog. Nothing reports a fault: the relay is buffering exactly as
      designed, `data_is_lost` is correctly false, the Gateway is healthy, the
      console is connected — and the fleet on screen is quietly getting older
      every second. It is the §9 failure mode one level up, where every
      component is honest and the system is still lying.
      Scope:
      - Measure intake and drain **as separate rates**, from a deliberate
        outage rather than a restart side-effect, at 1, 3 and 11 sources.
      - Locate the bottleneck by instrumentation, not by guessing. Candidates
        to measure, not to assume: the relay's SQLite commit rate, batch size
        and flush interval, the ack round trip, and the Gateway's
        persist-before-ack fsync.
      - Propose a requirement — drain rate at least N times intake at a stated
        fleet size per station — and say what the relay reports when it cannot
        meet it. **Stop with the proposal. Do not change the protocol.**
      *Done when:* intake and drain are recorded numbers at all three fleet
      sizes, the bottleneck is named with the measurement that identifies it,
      §10 is corrected, and a requirement is on the table for a ruling.
      *Closed* 2026-09-27. Drain stays near 300 records/s at 1, 3 and 11
      sources, against intake of 194, 416 and 1,281 records/s. The bottleneck
      is the Gateway resolving each MAVLink message's binding with its own
      query: 96% of the time spent storing a batch, 119,751 calls for 119,751
      records, measured by `gateway/stage_timing.py`. §10 is corrected. The
      requirement (drain ≥ 5× intake at design load, plus a `lagging` station
      state) is in `docs/decisions/002-drain-rate-requirement.md` **awaiting a
      ruling**. The fixes are P1-13 and P1-14.

- [x] **P1-11** Record `AUTOPILOT_VERSION` per vehicle, so firmware is fleet
      data rather than something read off a screen.
      The aircraft's firmware version is not in the raw archive: the message is
      only sent on request, Stage 0 is receive-only and cannot ask, and by the
      time the relay attaches QGC has already consumed the reply to its own
      request. So "the SITL that gates merges matches the aircraft" is
      currently unverifiable.
      A relay attached *before* QGC connects does see the reply, because QGC
      requests the version at every connect. That is Stage-0 compatible: the
      Gateway records what it observes and asks for nothing.
      *Done when:* connecting QGC to an aircraft results in a recorded flight
      software version for that `drone_id`, and a vehicle that never offers one
      is visibly unknown rather than silently absent.
      *Closed* 2026-09-28, for the Gateway's side. `drone_firmware` holds one
      row per change, and the console renders `fw unknown` until a version is
      recorded (unit-tested payload; the page was not watched live). Two SITL aircraft: the one asked for its version got one row
      (`4.8.0-dev`, `66c89850`), a second request added none, and the one
      never asked got none.
      MAVProxy does not request the version on connect, so a script stood in
      for QGC's request on the same path. QGC's own request was then observed
      on the real aircraft (2026-09-28): `hexa-01` recorded as ArduPilot
      4.6.3 on reconnect.
      `docs/runbooks/p1-11-firmware.md`.

- [x] **P1-12** Self-hosted base map: a Georgia PMTiles extract served by the
      console itself.
      The P1-08 map has no base layer. The demo style's tiles stop at zoom 6 and
      contain only country outlines, so at city zoom there is nothing to draw —
      it was never a street map.
      Self-hosted rather than a tile provider: no API key, no per-request
      dependency on a third party, and it works with no internet at all, which
      is the case a pilot in the field is actually in.
      **Self-hosting does not remove the attribution obligation.** The data is
      OpenStreetMap, so the map must display "© OpenStreetMap contributors"
      wherever it is shown, including on the minimal P1-08 page and in
      `web-pilot/` afterwards. Record the extract's date and source, because an
      offline basemap has no way of telling anyone it is stale.
      *Done when:* the map renders streets over the operating area with no
      network access beyond the console itself, and attribution is visible.
      *Closed* 2026-09-28. Protomaps build 20260928 (OSM data as of
      2026-09-28 04:00 UTC), bbox 39.9,41.0,46.8,43.6 at zoom 0-15: 333 MB,
      one PMTiles file, installed per machine by
      `infra/basemap/fetch_basemap.sh` and never committed. MapLibre, PMTiles
      and the style library are vendored. Watched on the development machine:
      Tbilisi streets at zoom 14, every request to 127.0.0.1 only, OSM
      attribution with the extract date visible. In Georgian the labels are
      OSM's `name` (Georgian script); the fonts were checked to carry it.
      See `docs/runbooks/p1-12-basemap.md`.

- [x] **P1-13** Resolve source bindings per batch, not per message.
      P1-10 measured this as the Gateway's ceiling. `IngestPipeline` calls
      `BindingResolver.resolve` for every MAVLink message, and each call runs
      its own `source_bindings` SELECT - about 3.3 ms, so about 300 records/s
      whatever the fleet size. `resolve_batch` already exists for this case and
      is not called.
      **Resolution stays per record's capture time.** A backlog that straddles
      a binding change must still resolve each record against the binding in
      force when it was captured; batching changes how often the bindings are
      read, not which binding applies. Test that a batch crossing a rebinding
      yields two `drone_id`s.
      *Done when:* `tools/ingest_capacity.py` at 11 SITL sources reports drain
      above intake with a steady baseline, the stage timings show `resolve`
      calls per batch rather than per record, and the drain figure is recorded
      against ADR-002's requirement.
      *Closed* 2026-09-28. At 11 SITL sources with bound aircraft, drain went
      from 289 to 1,796 records/s against intake of 1,197. The baseline settled
      in 2 s, and the backlog from a 60 s outage cleared: 72,345 records down
      to 769. That is **1.5×, short of ADR-002's proposed 5×**. `resolve` now
      runs 1,353 times for 254,315 records. Batch time is now spread across
      stages: store 41%, resolve 36%, write 11%. See ADR-002, "After P1-13".

- [x] **P1-14** A `lagging` station state: the backlog is not clearing.
      Proposed in ADR-002. Today a station whose backlog grows looks healthy -
      buffering is correct, nothing is lost, the Gateway is up - and the
      console shows an ever-older fleet. Enter `lagging` when the age of the
      newest stored record (`now - recv_utc_ns`) exceeds a configured threshold
      and `status.queue_depth` is rising; publish it with `lag_s`. No protocol
      change: both signals already reach the Gateway.
      Like `unreachable`, it does not mean telemetry is lost, and the console
      must not say it does.
      *Done when:* a test drives a station into `lagging` and back out, and the
      state reaches the console. Blocked on the ADR-002 ruling for the
      threshold.
      *Closed* 2026-09-28. Threshold ruled as the Gateway's link timeout
      (15 s by default): past that age a record can no longer make a drone
      live, so it is when every aircraft on the station starts to read as
      link lost. "Rising" is depth above its value five `status` messages
      earlier. A relay-v1 session over a real socket goes `healthy` ->
      `lagging` -> `healthy` and each is published with `lag_s`; the same old
      record with a steady queue never lags. The console shows the state and
      how far behind. Not yet produced by a live overloaded Gateway.
      ADR-002's N >= 5 is still unruled and is not needed by this state.

- [~] **P1-15** Remote ID ingest: observations from Remote ID receivers
      (ASTM F3411 / ASD-STAN EN 4709-002, decoded to Open Drone ID JSON)
      become tracks on the same map and in the same airspace monitor as
      MAVLink telemetry. Receiver-agnostic: an adapter per source (a
      commercial receiver's MQTT or webhook output, an ESP32 or phone
      receiver), one internal format. A Remote ID observation is marked as
      broadcast and unauthenticated wherever it is shown, because it can be
      spoofed. An aircraft seen both ways is one track, matched by serial.
      *Done when:* a DJI or ArduRemoteID broadcast is received, shown, and
      raises a conflict alert against a SITL aircraft.
      *Added* 2026-09-29 with the owner, for the monitoring direction.
      *Partial* 2026-09-29: Open Drone ID decoding checked against the
      reference library, a receiver datagram ingest
      (`python -m gateway.remote_id_ingest`), HAE to AMSL through a geoid
      (EGM2008 since 2026-09-30), the console marking, and a simulator. A
      simulated broadcast raised a conflict against a hovering SITL aircraft
      (`docs/runbooks/p1-15-remote-id.md`).
      2026-09-30: every observation is stored in `remote_id_observations`
      (telemetry database) and replays as an unverified broadcast. Rows are
      kept and retried while the database is down; checked by sending 40
      before the table existed.
      Receivers sign their datagrams with a per-receiver key
      (HMAC-SHA256, with a time window and a nonce against replays). An
      ingest without keys refuses to bind beyond loopback.
      A broadcast whose serial number is one of our registered aircraft's
      (`known_drones.serial`, projected from `drones.serial`) is withheld
      while that aircraft's MAVLink telemetry is live, and published as that
      aircraft when it is not: one track either way.
      Not yet: a real broadcast and receiver (the done-when above).

- [ ] **P1-16** Manned traffic: ADS-B positions (an RTL-SDR receiver, or an
      aggregator feed where licensing allows) on the map and in the airspace
      monitor, so a drone converging on a helicopter or an aircraft is
      alerted. Manned aircraft are never told to manoeuvre; the alert is to
      the drone's operator and the control centre.
      *Done when:* a replayed ADS-B track and a SITL aircraft on a converging
      path raise an alert naming both.
      *Added* 2026-09-29 with the owner, for the monitoring direction.

---

## Phase 2 — Data model and registry (1-1.5 weeks)

Goal: operators and aircraft are registered, and every change is auditable.

- [~] **P2-01** Alembic migrations for the full schema in
      `docs/ARCHITECTURE.md` §5, with GiST indexes on all geometry.
      *Done when:* `make migrate` runs clean up and down.
      *Partial* 2026-09-28: the relational tree exists with `bases`,
      `pilots`, `drones`, `airspace_zones` and `events` (GiST on every
      geometry; `events` append-only by trigger), and runs up, down and up in
      CI (`make migrate-relational`). The delivery tables were dropped from
      the design with the delivery scope (P-01); incidents (M-02) are the
      next table.

- [x] **P2-05** Drone and pilot registry API, including status transitions
      (`IDLE`, `ASSIGNED`, `IN_FLIGHT`, `CHARGING`, `MAINTENANCE`, `OFFLINE`).
      **Registering or retiring a drone must project into the telemetry
      database's `known_drones`.** The Gateway never connects to the relational
      database, so `source_bindings.drone_id` points at that projection rather
      than at `drones`, and a binding to a drone the projection has not heard
      of is refused by a foreign key. Without this step someone inserts into
      `drones`, the binding is refused or the telemetry is marked unclaimed,
      and the cause is invisible from either side: the relational registry
      looks correct and the Gateway looks broken.
      `known_drones` is a projection, never an authority — see
      `docs/specs/p1-02-gateway-ingest.md` §7.
      *Done when:* status is derived from telemetry freshness, not set by hand,
      and registering a drone makes it bindable in the telemetry database
      without anyone touching that database by hand.
      *Closed* 2026-09-28 (`python -m api`). Status is derived on every read:
      MAINTENANCE when set by a person, OFFLINE without live telemetry,
      IN_FLIGHT when armed, IDLE otherwise. ASSIGNED needed missions,
      which are out of scope, and CHARGING a charging signal, which does not
      exist, so neither is produced. Registering writes `known_drones` in
      the same transaction and a test binds the new drone; retiring closes
      its bindings. Checked live on the development machine against both
      databases.
      **The API has no operator authentication** - nothing in this file
      provides it - so it binds to loopback and must not be exposed.

- [~] **P2-06** Append-only audit log with a query API filtered by entity and
      time range.
      *Done when:* an auditor can reconstruct a drone's, a zone's or an
      operator account's full history.
      *Partial* 2026-09-28: `events` refuses UPDATE, DELETE and TRUNCATE in
      the database; every registry change writes its row in the same
      transaction; `GET /events` filters by entity and time and pages by id.
      A drone's history is reconstructed in a test.

- [ ] **P2-08** Registration check against the civil aviation authority's
      register (uas.gov.ge): an aircraft whose broadcast or declared
      registration number is not registered is flagged. Needs the
      authority's agreement and an access method; nothing is scraped.
      *Done when:* agreed access exists and an unregistered number raises a
      warning.
      *Added* 2026-09-29 with the owner, for the monitoring direction.

---

## Phase 5 — Airspace monitoring (3-4 weeks) — CRITICAL PATH

This is the phase where a missed alert means a collision nobody was warned
about. Budget the most time here.

**Only the tactical layer remains.** On 2026-09-29 the owner put the tactical
layer - what applies to every aircraft in the air, whoever flies it - first:
P5-08, P5-09, P5-16, P5-12, then P5-13 and P5-14. The strategic layer (route
generation and 4D corridor reservation, P5-01 to P5-05) served the owner's own
delivery fleet and was removed with it (P-02). The system watches aircraft
whose plans it does not know, so a conflict is found in flight: an alarm at
the control centre **and** a message to the operator's phone (P5-16),
carrying the advised action rather than only "danger". The system itself
never commands an aircraft.

- [x] **P5-00** Terrain elevation source: ground elevation AMSL for a given
      position, so that AGL becomes derivable at all.
      **A prerequisite for the height limit (P5-19), not an optional extra.**
      There is currently no source for height above ground anywhere in the
      system: `GLOBAL_POSITION_INT.relative_alt` is "Altitude above home", and
      `GPS_RAW_INT.alt` and `VFR_HUD.alt` are MSL. Separation is therefore
      judged in AMSL, and a limit on height above the ground cannot be checked
      without this.
      Candidate sources, to be evaluated rather than assumed:
      - **A DEM** — SRTM (~30 m postings, void-filled variants vary) or
        Copernicus DEM (~30 m, generally better in mountainous terrain, which
        Georgia is). Queried by position, served locally; licence and
        coverage both need checking.
      - **ArduPilot's `TERRAIN_REPORT`** (message 180), observed at 3.00 Hz in
        ADR-001's capture. It carries terrain height from the flight
        controller's own onboard terrain database, which makes it the
        aircraft's own view rather than an independent one. **Investigate, do
        not assume:** that database has its own coverage, resolution and
        loading behaviour, it can be absent or stale, and a value that is
        missing in flight is worse than one that was never offered. Whether it
        agrees with a DEM is itself worth measuring.
        **Measured 2026-09-25, and it rules `TERRAIN_REPORT` out as a sole
        source.** Read back from the raw archive, per source, so the two are
        not confused:
        - **`hexa-01`, the aircraft we fly** (SYSID 1): 3,280 messages in the
          2026-09-24 hardware run and 381 more on 2026-09-25, every one of them
          `loaded=0`, `pending=0`, `terrain_height=0.0`, `current_height=0.0`.
          The message is emitted at 3 Hz whether or not any terrain data is
          aboard. Identified as the airframe rather than a simulator by
          `GIMBAL_DEVICE_ATTITUDE_STATUS`, `GIMBAL_MANAGER_STATUS`,
          `MCU_STATUS` and `RPM`, none of which any SITL instance sent, and by
          the Siyi A8 mount announcing itself in `STATUSTEXT`.
        - **SITL** (SYSIDs 201-210): `loaded=336`, `pending=0`, and plausible
          `terrain_height` of 584-656 m. SITL fetches terrain tiles, so it has
          the data the aircraft lacks.
        So the simulator would have concealed this: an `alt_agl_m` mapped from
        `current_height` looks correct in SITL and is a flat 0.0 m on the real
        aircraft — a drone reported as on the ground for an entire flight. The
        value is not missing, it is confidently wrong, which is the failure this
        task exists to prevent. `TERRAIN_REPORT` may still be useful as a
        cross-check where tiles *are* loaded; it cannot be the source.
      **Done 2026-09-30.** Copernicus DEM (GLO-30, and GLO-90 for the
      N41 E043-E046 strip the public GLO-30 release withholds) is fetched by
      `tools.terrain_fetch`, read by `common/terrain.py`, served at
      `GET /terrain`, and shown in the console as ground elevation and
      approximate height above ground. Outside Georgia it works by fetching
      another box. `tools.terrain_compare` gives DEM minus flight controller,
      per cell, from SITL flights through the full pipeline:

      | Cell | Terrain | DEM | Reports | Median | Stdev | p95 abs | Max abs |
      |---|---|---|---|---|---|---|---|
      | N42E042, Samtredia | plain | GLO-30 | 2,091 | +1.5 m | 1.8 m | 4.3 m | 6.4 m |
      | N42E044, Kazbegi | mountain valley and slopes | GLO-30 | 3,027 | +0.4 m | 3.5 m | 6.9 m | 14.4 m |
      | N41E044, Tbilisi | city, hills | GLO-90 | 15,693 | -2.9 m | 13.4 m | 27.1 m | 27.1 m |

      The two GLO-30 cells used ArduPilot's own terrain files
      (terrain.ardupilot.org `tilesdat3`, 100 m grid). The Tbilisi file was
      filled on demand by a ground station during earlier runs; in the one
      block checked it differs from `tilesdat3` by -22 to +15 m (mean -0.9 m).
      Tbilisi's larger spread is therefore GLO-90 plus a different
      flight-controller source, not the city alone. hexa-01 sent 11,520
      reports, all `loaded=0`, and none were compared.
      The work also showed that `SITL_HOME` sat 155 m below the ground at home
      (450 m against 605 m); it is now 605 m. Details:
      `docs/runbooks/p5-00-terrain.md`.
      *Done when:* ground elevation can be queried for any point in the
      operating area, the two sources have been compared over that area, and
      the disagreement between them is a recorded number rather than an
      assumption. Any use of `TERRAIN_REPORT` must treat `loaded == 0` as "no
      answer", never as zero.

- [x] **P5-06** Neighbour lookup on each telemetry tick, 800 m radius.
      *Done when:* lookup stays under 5 ms at 100 airborne drones.
      *Closed* 2026-09-29. A latitude-longitude grid with cells at least the
      radius wide (`airspace/neighbours.py`); agrees with brute force over
      300 scattered aircraft, and a test asserts the slowest of 100 lookups
      at 100 drones is under 5 ms. The radius comes from `airspace_policy`.

- [x] **P5-07** CPA computation per `ARCHITECTURE.md` §6.2.
      *Done when:* unit tests cover head-on, crossing, overtaking, parallel, and
      the zero-relative-velocity degenerate case.
      *Closed* 2026-09-29. All five, plus diverging pairs (judged on where
      they are now), vertical separation evaluated at the CPA time, and a
      climb that closes it. Thresholds live in `airspace_policy` (relational,
      seeded with §6.2's Stage 0 values). `python -m airspace` raises a
      critical alert per conflicting pair of armed aircraft, publishes it and
      writes it to `events`. Watched live with two SITL aircraft: raised when
      hovering 25 m apart, cleared when they separated, raised 57 s before a
      head-on pass (CPA 2.8 m), cleared as they diverged. See
      `docs/runbooks/p5-airspace-monitor.md`.

- [ ] **P5-08** Deterministic advisory resolution by drone ID (descend or
      loiter), recorded in `events` against both aircraft. Advice to the
      operators, never a command to the aircraft.
      *Done when:* the same conflict evaluated twice produces the identical
      resolution.

- [ ] **P5-09** Resolution delivery as an operator instruction: critical alert
      naming both aircraft, time to closest approach, and the prescribed action.
      Threshold widened to `t_cpa < 60 s` for human reaction time. Compliance
      tracked by watching the resulting telemetry.
      *Done when:* both operators involved in a conflict receive compatible
      instructions derived from the same deterministic rule, and failure to
      comply within 20 s escalates.

- [ ] **P5-12** Scenario framework: YAML defining drones, their flight paths,
      wind, and expected outcome; runs in CI.
      *Done when:* `make scenario FILE=crossing_10.yml` produces a pass/fail
      report.

- [ ] **P5-13** **Soak test.** 15 SITL drones on scripted flight paths, one
      city square, 2 hours continuous, with scripted operator compliance
      (instruction followed after a simulated 10-20 s human delay). D-02 runs
      the same load on staging.
      *Done when:* zero separation violations under 30 m, and the log shows how
      many conflicts were detected, how each was resolved, and what the worst
      observed separation was.

- [ ] **P5-14** Pilot-delay sensitivity study: rerun the soak scenario with
      simulated reaction delays of 5, 15, 30, and 60 s.
      *Done when:* the report states the maximum tolerable human delay. This
      number determines whether the alert lead time (`t_cpa < 60 s`) leaves a
      notified operator enough time to act — it is a go/no-go input, not a
      nice-to-have.

- [x] **P5-15** In-flight zone incursion alerts: an armed aircraft inside a
      `no_fly` (critical) or `restricted` (warning) zone, within its AMSL
      band, is alerted as it happens. It watches where aircraft actually
      are, which a monitoring operator needs whether or not a flight was
      planned here.
      *Done when:* an aircraft flown into a zone raises an alert naming the
      zone, and leaving it clears the alert.
      *Closed* 2026-09-29, added with the owner's agreement to build the
      monitoring core. Watched live: both SITL aircraft raised and cleared a
      warning on entering and leaving a restricted test zone.

- [ ] **P5-16** Operator notification: a critical airspace alert, with its
      advised action (P5-09), reaches the pilot of each aircraft
      involved on their phone, not only the control centre's console.
      Needs which pilot flies which aircraft and how to reach them. The
      channel is open: SMS is universal but can take 5-30 s or more to
      arrive, against a 60 s warning; a Telegram bot is free and arrives in
      seconds. Delivery time is measured, not assumed, and a failed or late
      delivery is itself an alert at the centre. The console alarm never
      waits for this.
      *Done when:* in a SITL conflict both pilots receive their instruction
      on a phone, the delivery latency is recorded per message, and an
      undeliverable message is shown at the centre.
      *Added* 2026-09-29 with the owner, from the tactical-layer discussion.

- [ ] **P5-17** Evaluate OpenUTM (Flight Blender and Flight Spotlight,
      Apache-2.0) against this system, before building more of the same.
      The direction on 2026-09-29 is a monitoring system for every drone,
      to be put to the Ministry of Defence and the civil aviation authority,
      and to them an existing standards-compliant system is worth more than
      a new one. OpenUTM claims network Remote ID (ASTM F3411), flight
      authorisation (F3548), ED-269 geo-zones, conformance monitoring and
      traffic aggregation, and is used by Swiss FOCA and the UK national
      programme. Run it beside this stack, feed it the Gateway's
      telemetry, and establish by running it - not from its README - what
      it does, what it lacks, and where this system's parts (the relay and
      Gateway, the airspace monitor, replay) would sit in or around it.
      *Done when:* a written comparison, from what was run, and a decision
      with the owner: adopt, integrate with, or continue alone.
      *Added* 2026-09-29 with the owner.

- [ ] **P5-18** Official geo-zones: import zones in EUROCAE ED-269 format
      from the authority's published data into `airspace_zones`, keeping the
      source, version and validity period, instead of drawing them by hand.
      *Done when:* an ED-269 file imports, its zones alert as P5-15 does, and
      re-importing a new version replaces the old one with the change logged.
      *Added* 2026-09-29 with the owner, for the monitoring direction.

- [x] **P5-19** Altitude limit: alert when an aircraft is above the open
      category's height limit over the ground. The limit is configuration,
      not code; the height needs terrain (P5-00), because telemetry carries
      height above home, not above ground.
      *Done when:* a SITL aircraft climbing over the limit above sloping
      terrain raises the alert at the right point.
      *Added* 2026-09-29 with the owner, for the monitoring direction.
      **Done 2026-09-30.**
      - The limit is `airspace_policy.max_height_agl_m`, seeded with 120 m,
        the owner's figure. There is no minimum; the owner has none.
      - The airspace monitor warns when AMSL altitude minus the DEM exceeds
        the limit, for armed aircraft and for Remote ID aircraft declared
        airborne. Where the ground is unknown, the limit is not evaluated.
      - The console shows the height, the limit, the ground and the DEM.
      - SITL at Kazbegi held 1,861 m AMSL over ground falling away. The
        monitor raised the warning at 120.3 m above ground. The flight
        controller's own terrain crossed 120 m 3 s later, and the warning
        cleared on the way back.
      - Details: `docs/runbooks/p5-airspace-monitor.md`.

---

## Phase 6 — Operator console (2 weeks)

- [~] **P6-01** Map view: all active drones, zones, bases.
      Layer toggles.
      *Partial* 2026-09-29: `web-pilot/`, served by the API at `/app`. The
      self-hosted basemap, zones (corridors among them) and bases re-read
      every 30 s, every placed aircraft by heading, a line between the two
      aircraft of each conflict, toggles for zones, bases and labels.
      Checked against two SITL aircraft flying the head-on and zone-entry
      scenario: both drawn, the zone warning and the critical conflict shown
      and drawn, acknowledged, in `en` and `ka`. There are no frontend tests
      yet (S-19).
- [~] **P6-02** Per-drone detail panel: full telemetry, battery trend, link
      quality.
      *Partial* 2026-09-29: everything the feed carries, battery and
      altitude trends since the console opened, link loss and heartbeat
      gap, firmware, the aircraft's alerts, and a link to its replay.
- [~] **P6-03** Alert system with severity levels, audible cue for critical,
      acknowledge flow.
      *Partial* 2026-09-29: the console shows airspace alerts with severity,
      repeats a tone for an unacknowledged critical one, and replays active
      alerts to a console opened later. Acknowledgement is per console and
      not yet recorded (P6-07), and station and battery alerts are not yet on
      this path. In the operator console (P6-01) only operators and admins
      acknowledge; viewers see the alert and the tone.
      **Alert text must not imply loss that has not happened.** A station going
      unreachable means the ground station cannot be reached from here; the
      relay is almost certainly still receiving and buffering, and the record
      will be complete once it reconnects (`relay-v1.md` §8, P1-02). An alert
      reading "telemetry lost" there is false, and a pilot who learns the
      alerts overstate things will discount the one that does not. Reserve
      loss wording for a reported `gap` or a drop counter that moved.
- [ ] **P6-07** Operator action audit: every console action (acknowledging an
      alert, changing the registry) recorded with the operator's ID and a
      timestamp.
      *Done when:* an acknowledgement made in one console is in `events` and
      shown as acknowledged in every other console.

- [x] **P6-08** Operator authentication and roles for the API and the console:
      named accounts, no shared login; roles `viewer` (see everything),
      `operator` (also acknowledge alerts), `admin` (also change the registry
      and manage accounts). Passwords stored as scrypt hashes; server-side
      sessions that can be revoked; every login, failed login and change
      recorded in `events` against the operator's id. The console's feed,
      which must not read the database, accepts a short-lived ticket signed
      by the API instead.
      *Done when:* every API route and the console feed refuse an anonymous
      request, each role is refused what it may not do, a revoked session
      stops working, and the audit log names who did what.
      *Added* 2026-09-29 with the owner, for the monitoring direction.
      *Closed* 2026-09-29. Checked live on the laptop against the running API
      and console (`docs/runbooks/p6-08-operator-auth.md`): anonymous 401 and
      feed close 4401, viewer 403 on admin routes, cookie state change
      without `X-Courier-Request` 403, sign-out ends the session. Lockout,
      expiry, idle timeout and revocation by role, password or disable are
      checked against PostgreSQL; every documented route is walked
      anonymously and as a viewer.

---

## Phase 7 — Link failure behaviour, monitoring side (1 week)

Goal: every way a data source can fail is visible to the supervisor, and none
of them is mistaken for a quiet sky. The flight controller's own failsafes are
the operator's responsibility and are not tested here.

- [ ] **P7-01** Relay or server link loss >30 s: the station shows
      `unreachable`, the affected aircraft are marked stale rather than
      dropped, the backlog replays on reconnect, and the gap is recorded with
      its wall-clock window.
      *Done when:* a SITL run with the relay's uplink cut for 60 s shows all
      four in the console and in replay.
- [ ] **P7-11** Ground PC or QGC crash mid-flight: the station and its
      aircraft go stale with the cause visible, and a relay restart is
      detected even when the Gateway was unreachable for longer than the old
      uptime.
      *Done when:* killing QGC and then the relay in SITL shows both
      transitions, and restarting them resumes the same tracks.

---

## Phase 10 — Operations (1.5 weeks)

- [ ] **P10-01** Prometheus metrics: tracked aircraft by source, ingest rates,
      station states, alert and conflict rate.
- [ ] **P10-02** Grafana dashboards: airspace overview, per-drone health,
      ingest and alert performance.
- [ ] **P10-03** **Flight replay**: any past flight replayed on the map with
      telemetry scrubbing. Essential for incident review — do not defer this.
      **Render gaps explicitly.** Telemetry can be missing for reasons the
      system already knows about — a relay `gap` from queue cap, an intake
      drop, a station that went unreachable (`relay-v1.md` §11). Replay must
      show the track stopping and say why, never interpolate across a hole. A
      smooth line through missing data invents evidence, which in an accident
      investigation is worse than showing nothing.
      *Partial* 2026-09-29: `/replay` on the core API, see
      [`docs/runbooks/p10-03-replay.md`](docs/runbooks/p10-03-replay.md). The
      track is drawn as segments only; it is cut by silence, by any relay
      `gap` between two samples however short (unless another station heard
      the aircraft through it), and by telemetry without a position. Each
      hole carries the cause the Gateway logged or "no recorded cause";
      airspace alerts come from `events`. A departing station is now logged
      `unreachable`. Verified on the 2026-09-28 SITL airspace run.
      Outstanding: a live hole with a logged cause, which comes with P1-01
      Procedure A.
- [ ] **P10-04** Automatic `.bin` dataflash log retrieval and archival after
      each flight.

---

## Phase 11 — Regulation (in parallel, not at the end)

- [ ] **P11-01** Contact the civil aviation authority. Establish what a
      national monitoring system must receive and show, in which formats the
      authority publishes zones and the register, and who operates the system.
      **Do this before wave M is built** — the answer shapes M-03 and M-04.
- [ ] **P11-02** Remote ID requirements and hardware selection.

---

## Wave 0 — Remove the delivery scope

- [x] **P-01** Delete `dispatch/` and `app-customer/` and every build, CI and
      test reference to them.
- [x] **P-02** Rewrite this file for flight monitoring: delivery tasks
      removed, waves S, M and D added.
- [x] **P-03** README, `CLAUDE.md`, `docs/ARCHITECTURE.md` and module docs
      describe a monitor that never commands an aircraft.
- [x] **P-04** No code comment cites a removed task.
      *Done when:* a grep for the delivery scope finds only `courier_*`
      identifiers, which S-14 renames.

---

## Wave S — Stability and security (parallel, six streams)

Files are divided between the streams so that no two touch the same file.
S-09 changes relay-v1 and needs both `agent/` and `gateway/`, so it follows
S-A and S-B rather than running beside them. S-14 is optional and last.

### S-A: `agent/` (relay)

- [ ] **S-01** `read_from` reads up to the byte budget (`LIMIT` or an
      iterator) instead of loading the whole backlog (`agent/queue.py:258`).
      *Done when:* a test with a backlog far larger than the budget shows
      memory bounded by the budget, and the records returned are the oldest,
      oldest first.

- [ ] **S-02** An error in the writer thread is logged, retried with backoff,
      and raises a health flag carried in `status`, so the Gateway does not
      show the station as healthy (`agent/relay.py:152`). `__main__` checks
      that the thread is alive.
      *Done when:* a simulated disk error makes `status` report degradation
      (the presence test), and a writer that recovers clears it.

- [ ] **S-03** SQLite calls leave the event loop and run in `to_thread`
      (`agent/relay.py:197-209`).
      *Done when:* no SQLite call runs on the event loop thread, checked by a
      test.

### S-B: `gateway/`

- [ ] **S-04** Archive compression and fsync run in `to_thread`
      (`gateway/ingest_store_pg.py:301`).
      *Done when:* a slow disk no longer stalls other stations' sessions,
      shown by a test.

- [ ] **S-05** Only new records enter the pipeline, never duplicates
      (`gateway/relay_server.py:381`). Drain no longer waits on the pipeline:
      it hands off through a bounded queue.
      *Done when:* a replayed batch publishes nothing twice, and a stalled
      pipeline does not stop acknowledgements until the queue is full.

- [ ] **S-06** A `StoreError` in the reporter task does not kill a station's
      reporting (`gateway/relay_server.py:455-476`).
      *Done when:* a test injects the error and the station keeps reporting.

- [ ] **S-07** Bounds everywhere: `StationLinkTracker.losses`, eviction in
      `RateLimiter`, a semaphore on Remote ID tasks, and an O(1) `_forget`.
      *Done when:* each structure has a test that drives it past its bound.

- [ ] **S-08** An empty or duplicated token in the token file is a startup
      error. One station has one session, and the disconnect of an old
      session does not disturb the new one.
      *Done when:* each case has a test, including a reconnect racing the old
      session's teardown.

- [ ] **S-09** A gap inside a live session (a cap drop) at protocol level:
      the relay sends `gap` and the Gateway moves its watermark. This changes
      the relay-v1 spec and needs `agent/` and `gateway/` together, so it
      runs **after S-A and S-B**, on its own.
      *Done when:* relay-v1 documents it, and a cap drop during a session
      appears at the Gateway as a recorded gap with the watermark past it.

- [ ] **S-10** Remote ID spoofing: an unverified Remote ID broadcast is never
      published on a registered aircraft's `telemetry.{id}` subject.
      *Done when:* a broadcast carrying a registered serial, without that
      aircraft's verification, is shown as a separate unverified track.

### S-C: `airspace/` and `common/terrain.py`

- [ ] **S-11** `Track` carries a timestamp. CPA extrapolates the neighbour's
      position to the current time, and a neighbour older than a
      configurable limit is left out.
      *Done when:* tests show a stale neighbour excluded and a slightly old
      one extrapolated, not taken as current.

- [ ] **S-12** Checks are isolated from each other: an error in
      `_check_height` must not lose the conflict and zone alerts
      (`airspace/monitor.py:188`). Non-finite coordinates are rejected.
      *Done when:* a test makes the height check raise and the other alerts
      still arrive, and a NaN position raises no alert and is logged.

- [ ] **S-13** The policy is reloaded periodically, and the death of the
      ticker is logged. Audit and database writes leave the hot path through
      a bounded queue. Terrain reads run in `to_thread` behind an LRU cache.
      *Done when:* a policy change takes effect without a restart, and a slow
      database does not delay alerts, both shown by tests.

### S-D: `api/`

- [ ] **S-15** scrypt runs in `to_thread`. Login is rate-limited by IP and by
      username. The dummy hash uses `self.cost`. The hash is computed outside
      `FOR UPDATE`.
      *Done when:* tests cover the rate limit on both keys and the timing of
      a login for an unknown user matches a known one.

- [ ] **S-16** `/events` takes `since` and `until` in UTC. HTTP error details
      no longer expose `IntegrityError`. `retire_drone` runs its operations
      in the correct sequence. WebSocket connections check `Origin`.
      *Done when:* each has a test, including a refused foreign origin.

- [ ] **S-17** `api/` joins the mypy strict list, with a coverage threshold.
      *Done when:* `pyproject.toml`, `CLAUDE.md` and `tests/test_layout.py`
      agree, and CI enforces the threshold.

### S-E: `web-pilot/`

- [ ] **S-18** The feed reducer has a `default` branch, `JSON.parse` is
      guarded, and reconnect uses exponential backoff with jitter, with no
      tight loop on close code 4401.
      *Done when:* a malformed message and a 4401 close are both handled
      without a crash or a reconnect storm.

- [ ] **S-19** vitest, with tests for the feed reducer and reconnect. CI runs
      `npm test`.
      *Done when:* the tests run in CI and fail on a broken reducer.

### S-F: `infra/` and CI

- [ ] **S-20** The SITL job also runs on push to `main`, and deploy depends
      on it.
      *Done when:* a red SITL job on `main` blocks the deploy.

- [ ] **S-21** Images are tagged with the commit SHA. `deploy.sh` rolls back
      to the previous tag on failure, and retries.
      *Done when:* a deliberately broken deploy on staging returns to the
      previous tag by itself.

- [ ] **S-22** Development compose ports bind to `127.0.0.1`. A
      `.dockerignore`. The Node version in the Dockerfile matches CI (22).
      Base images pinned by digest. A dependency layer cache in the
      Dockerfile.
      *Done when:* nothing in the dev stack listens beyond loopback, and a
      source-only change rebuilds without reinstalling dependencies.

- [ ] **S-23** Backups cover the `archive` volume, the tokens and an
      encrypted copy of `.env`, with an offsite copy (destination to be named
      by the owner: DO Spaces, S3 or other). `restore_check` fails on a zero
      count.
      *Done when:* a restore from the offsite copy into a scratch environment
      succeeds, and an empty restore makes `restore_check` fail.

- [ ] **S-14** *(optional, last)* Rename the `courier_*` databases, users,
      volumes and environment names, with a migration runbook. Until then
      they stay as they are, because staging depends on them.
      *Done when:* staging runs under the new names with its data intact, by
      following the runbook.

---

## Wave M — Monitoring features for the ministry

Sequenced by value to the demonstration. Each is its own branch. M-03, M-04
and M-08 are superseded by Wave U, which follows the EU U-space model; they
stay here, closed as superseded, so their IDs keep resolving.

- [ ] **M-01** Finish Remote ID (P1-15): verified and unverified tracks told
      apart in the console by colour and legend, receiver state shown, and
      Remote ID tracks included in the CPA and zone checks.
      *Done when:* a simulated unverified broadcast is drawn distinctly,
      named in the legend, and raises a zone alert.

- [ ] **M-02** Violations and incidents: an unregistered drone, a zone entry,
      the height limit and a dangerous approach each become an **incident**,
      with time, drone and serial, operator, a track excerpt and a status
      (new, reviewed, closed). A table in the relational database.
      *Done when:* each violation type opens an incident in a SITL run, and
      an operator can move it through its statuses with each change in
      `events`.

- [!] **M-03** *(superseded by U-01 and U-02)* Registry (P2-08): third-party operators and drones by serial
      number. A Remote ID serial is matched against the registry, and an
      `unregistered` warning is raised when it is absent.
      *Done when:* a broadcast with an unknown serial raises `unregistered`,
      and registering that serial clears it.

- [!] **M-04** *(superseded by U-03)* Official zones (P5-18, ED-269): import and validation, with
      checks. This was unfinished work in progress and is completed here.
      *Done when:* P5-18's criterion is met and an invalid file is refused
      with a named reason.

- [ ] **M-05** Flight segmentation: a flight is take-off to landing, derived
      from telemetry. A list of flights with filters (date, operator, drone,
      region) and a link to replay (P10-03).
      *Done when:* a SITL session with two take-offs lists two flights, each
      opening its own replay.

- [ ] **M-06** A read-only `regulator` role: sees everything, changes
      nothing. The audit log records who viewed and who exported what.
      *Done when:* every changing route refuses the role, and a view and an
      export by it appear in `events`.

- [ ] **M-07** Reports: an incident report (PDF), CSV export of flights and
      violations, and a statistics dashboard (flights per day, violations by
      type and region).
      *Done when:* each report is produced from a SITL run's data and its
      figures match the database.

- [!] **M-08** *(superseded by U-07)* ADS-B (P1-16): manned aircraft on the map, and an alert when a
      drone approaches one.
      *Done when:* P1-16's criterion is met in the console.

- [ ] **M-09** Georgian as the primary UI language: terminology reviewed, map
      legend, and a printable view where needed.
      *Done when:* a Georgian-speaking reviewer signs off the console and
      reports in `ka`.

---

## Wave U — U-space services (EU model)

The target is the EU U-space framework: Regulations (EU) 2021/664, 2021/665
and 2021/666 on top of 2019/947 and 2019/945. Georgia's UAS rules (GCAA,
in force since 2021-01-01) mirror 2019/947 and the EU–Georgia Common
Aviation Area Agreement has been in force since 2020-08-02, so this is the
model a Georgian authority will be measured against.

Two invariants carry over unchanged. **The system never commands an
aircraft**: authorisation, geo-awareness and conformance act on *operators*
and on the authority's picture, never on a vehicle. **Every service is
observable in SITL** before it is done (`CLAUDE.md` hard rule 5).

The four services 2021/664 makes mandatory inside a U-space airspace are
U-02, U-03, U-05 and U-07. U-06 (conformance) and U-08 (weather) are
optional there and can be made mandatory by the authority.

- [ ] **U-15** Source isolation and control (`ARCHITECTURE.md` §2.1):
      every source is its own adapter process publishing the common track
      format, and each can be switched off without a deploy, by type and by
      instance (station, receiver, provider, feed). A disabled source is
      refused at the adapter and counted, its tracks age out as *source
      disabled*, the airspace monitor stops judging them, and the switch is
      an audited `events` row with actor and reason. The console lists every
      source with its state and lets an admin switch it. Comes before U-02,
      which adds network Remote ID as a further source.
      *Done when:* with SITL aircraft on both a relay and simulated Remote
      ID, disabling Remote ID removes only the Remote ID tracks and their
      alerts, disabling one station removes only its aircraft, both show as
      disabled rather than silent, and re-enabling restores them.

- [ ] **U-16** SITL as a Remote ID source: a bridge that turns a SITL
      aircraft's MAVLink position into Open Drone ID broadcasts through the
      simulated receiver path, so one simulated aircraft can appear on the
      relay, on Remote ID or on both. Exercises track fusion and U-15, and
      feeds `make demo` (D-01).
      *Done when:* a SITL aircraft seen on both sources is one track, and
      disabling either source leaves it visible through the other.

- [ ] **U-01** UAS operator registry (2019/947 Art. 14): operators distinct
      from console users, with a registration number, contact and status;
      remote pilots with competency records; UAS with serial, class label
      (C0-C6), MTOM and the operator that owns them. Import from a
      `uas.gov.ge` export when one is available, manual entry until then.
      *Done when:* an operator, a pilot and two UAS can be registered,
      suspended and looked up by registration number or serial, with every
      change in `events`.

- [ ] **U-02** Network identification service: every track, whatever its
      source, is resolved to *registered*, *registered but suspended*,
      *unknown operator* or *unidentified*. Direct Remote ID's operator
      registration number and serial are matched against U-01; a mismatch
      between the two is itself an alert. Network Remote ID (ASTM F3411
      network, as served by other USSPs) is ingested as a further source.
      Absorbs S-10's spoofing guard.
      *Done when:* four simulated broadcasts, one per status, show their
      status in the console, and an `unidentified` or `unknown operator`
      track in a zone opens an incident (U-12).

- [ ] **U-03** Geo-awareness: the zone model follows EUROCAE ED-269
      (identifier, restriction type, reason, vertical limits with their
      reference, applicability windows, authority), with import and export
      in ED-269 JSON, an editor in the console for the authority, and an
      import from `airspace.gov.ge`. Replaces P5-18 and absorbs its paused
      work (commit `e9cf2a2`).
      *Done when:* an ED-269 file round-trips unchanged, an invalid one is
      refused with a named reason, a zone drawn in the editor alerts in
      SITL, and a zone outside its applicability window does not.

- [ ] **U-04** Dynamic airspace reconfiguration (2021/665): the authority
      activates a temporary restriction (for example `TEMPO RESTR. AREA`),
      every console receives it at once, aircraft already inside are
      alerted, and affected authorisations (U-05) are flagged.
      *Done when:* activating a restriction over a SITL aircraft raises its
      alert within one telemetry tick and marks the authorisation.

- [ ] **U-05** UAS flight authorisation (2021/664 Art. 10): an operator
      submits an operational intent (4D volumes: polygon, altitude band
      AMSL, time window). It is checked against zones and against every
      other accepted intent (spatial, temporal and altitude overlap), then
      accepted, refused with the conflicting item named, or sent to the
      authority for decision. Priority rules for special operations. This
      brings back the strategic check removed with the delivery scope, as a
      service to operators rather than a planner.
      *Done when:* two overlapping intents submitted in either order give
      the same result, and a refusal names the zone or intent it conflicts
      with.

- [ ] **U-06** Conformance monitoring: each flight is matched to its
      authorisation and a track leaving its accepted volumes, or flying
      without one where authorisation is required, raises a
      non-conformance alert and an incident.
      *Done when:* a SITL aircraft flown out of its volume alerts, and one
      flown inside it does not.

- [ ] **U-07** Traffic information (2021/664 Art. 11; 2021/666
      e-conspicuity): manned aircraft from ADS-B (a local receiver or a
      feed) and ADS-L / FLARM where available, drawn on the map and
      included in proximity alerts with drones. Replaces P1-16.
      *Done when:* a recorded ADS-B track near a SITL drone raises a
      traffic alert, and a stale manned track ages out visibly.

- [ ] **U-08** Weather information service: wind, gusts, visibility and
      precipitation for the operating area from METAR and a forecast
      source, shown per area, with thresholds in configuration.
      *Done when:* a wind above the configured threshold raises an area
      advisory in the console.

- [ ] **U-09** Common Information Service (2021/664 Art. 5): a read API
      publishing U-space airspace boundaries, geo-zones, active dynamic
      restrictions and the list of certified USSPs, aligned with EUROCAE
      ED-318.
      *Done when:* an external client reads every published item, and a
      restriction activated in U-04 appears in the API within one second.

- [ ] **U-10** USSP interoperability: exchange operational intents and
      constraints with other USSPs using ASTM F3548 through an InterUSS DSS,
      so a flight authorised elsewhere is visible and deconflicted here.
      Deferred until Georgia has more than one USSP, but the data model in
      U-05 is chosen so this needs no migration.
      *Done when:* the InterUSS `monitoring` test suite passes against a
      local DSS.

- [ ] **U-11** Operator portal: public web pages where an operator checks
      zones, submits an operational intent (U-05) and sees its decision,
      in `ka` and `en`. The equivalent of ENAIRE Drones, DroneTower or
      B4UFLY.
      *Done when:* a test operator registers, submits an intent and
      receives the decision without console access.

- [ ] **U-12** Incidents and evidence: incidents (M-02) gain persisted,
      audited acknowledgement, assignment and closure, and an evidence
      pack per incident (track excerpt, raw Remote ID frames, zone and
      authorisation at the time, audit trail) exported as PDF and CSV with
      a content hash. Absorbs P6-07 and the incident part of M-07.
      *Done when:* an incident from a SITL run exports a pack whose hash
      verifies and whose contents match the database.

- [ ] **U-13** Authority role and oversight: a `regulator` role (read
      everything, change nothing but incidents and zones), the audit of
      views and exports (M-06), and oversight views over operators and
      USSPs.
      *Done when:* every other changing route refuses the role, and a view
      and an export by it appear in `events`.

- [ ] **U-14** Non-cooperative detection: an adapter for third-party
      sensors (RF detectors, radar) so detections appear as
      `non-cooperative` tracks correlated with cooperative ones. Hardware
      and legal authority lie with security agencies; this is the data path
      only.
      *Done when:* a recorded sensor feed produces tracks, and one
      coinciding with a Remote ID track is merged rather than duplicated.

---

## Wave D — Demo readiness

- [ ] **D-01** Demo scenario: N drones in SITL, a simulated Remote ID
      receiver, and planned violations (zone, height, approach, unregistered
      drone), started by one command (`make demo`).
      *Done when:* `make demo` on a clean checkout produces every planned
      violation on the console.

- [ ] **D-02** Soak test on staging: 15+ drones for 2 hours, checking memory,
      latency and missed alerts.
      *Done when:* the report gives memory over time, alert latency and a
      count of missed alerts, and the last is zero.

- [ ] **D-03** Staging check: HTTPS, a real test of restoring a backup, and a
      minimum of monitoring (Prometheus and Grafana, P10-01 and P10-02).
      *Done when:* each item is demonstrated and recorded in the staging
      runbook.

- [ ] **D-04** Presentation script: the steps, what is shown, and what
      happens if something fails (fallback: replay from a recording).
      *Done when:* a full rehearsal follows the script, including one
      deliberate fallback.

---

## Sequencing

```
Wave 0   Remove the delivery scope        P-01 → P-04
Wave S   Stability and security           S-A … S-F in parallel,
                                          then S-09; S-14 optional, last
Wave M   Monitoring features              M-01, M-02, M-05, M-07, M-09
Wave U   U-space services (EU model)      U-15 → U-16 → U-01 → U-02 → U-03
                                          → U-12 → U-05 → U-06
                                          → U-07 → U-04 → U-13 → U-08
                                          → U-09 → U-11; U-10, U-14 last
Wave D   Demo readiness                   D-01 → D-04

Open tasks in Phases 1-10 are taken up where a wave needs them.
Regulation (P11) runs in parallel throughout.
```

The presentation date is not yet known, so the priority is stability first,
then readiness to demonstrate, then new features, and the system must be
demonstrable at the end of every wave. Once a date is set, Wave D moves
ahead of whatever remains of Wave M.

**Working rules.** One task is one agent, in its own git worktree and on its
own branch (`fix/S-03-airspace-stale-track`, `feat/M-02-violations`).
Parallel agents in one wave never touch the same file. The main session
reviews each diff and runs ruff, mypy and pytest locally, with SITL in WSL
for anything that alerts. The main session opens the pull request, merges
it once every CI job including SITL is green, and deletes the branch. Pull
requests touching `agent/`, `gateway/` or `airspace/` get an independent
review before merge. The deploy from `main` is automatic.

**What receive-only buys.** The system cannot touch an aircraft, so no bug in
it can cause a crash. That was the Stage 0 guarantee; it is now permanent.
Conflict detection, zone and height alerts and the console are exercised
end to end against SITL with no path by which any of them could reach a
flight controller.

**What it costs.** Every resolution goes through a human: the system alerts,
the operator acts. P5-14 measures whether the alert lead time is long enough
for that.

---

## Open questions

1. The offsite destination for backups (S-23).
2. The format in which the authority provides zones and the register: its
   own data, an ED-269 file, or manual entry (M-03, M-04).
3. Whether a real Remote ID or ADS-B receiver is available for the
   demonstration, or everything is simulated (M-01, M-08, D-01).
4. Installing `gh` on the development machine so pull requests can be opened
   directly.

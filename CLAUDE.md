# CLAUDE.md

Project instructions for Claude Code. Read this before doing anything in this repo.

## Project

National drone-flight monitoring system. It observes every drone in the
airspace from operator relays (MAVLink forwarded by QGC), Remote ID receivers,
ADS-B and an operator and drone registry, and shows them on one map with zones,
violations, alerts and a replayable flight record. It is an observer only: it
never commands an aircraft.

Read `docs/ARCHITECTURE.md` for the system design and `TASKS.md` for the work
breakdown. Every task has an ID (e.g. `P1-03`). Always reference the task ID in
the commit message.

## Hard rules

1. **The system never commands an aircraft.** This is permanent, not a stage.
   Nothing here uploads a mission, changes a flight mode, arms a vehicle or
   actuates a payload, and no service or relay may send anything towards a
   vehicle: the relay's socket is receive-only (`relay-v1.md` §1). Alerts and
   advice go to people. A task that seems to need a send path is out of scope;
   stop and ask. The only thing that ever transmits is the operator-run probe
   `tools/mavlink_probe.py roundtrip`, whose read-only `PARAM_REQUEST_READ`
   tests the channel and which is never part of a running service.
2. **Never ship code that talks to real hardware without a SITL path.** The same
   code path must work against `sim_vehicle.py`.
3. **Never hardcode coordinates, altitudes, battery thresholds, or geofences.**
   All of these live in config or the database.
4. **Do not invent MAVLink message fields.** Check `pymavlink` message
   definitions before using a field name.
5. **An alert path is not done without a SITL or scenario test.** Conflict,
   zone, height and link-loss alerts are the safety-relevant output of this
   system. Each one must be raised, and cleared, by aircraft flying in SITL
   (or a scenario in `sim/scenarios/`) before it is considered done; a unit
   test alone does not count.
6. If a task is ambiguous, stop and ask. Do not guess on anything touching
   flight behaviour.

## Language

- All code, comments, docstrings, commit messages, PR descriptions, log messages,
  and variable names: **English only**.
- User-facing strings go through i18n from day one (`en`, `ka`). Never hardcode
  display text.

## Git conventions

**Commit messages must contain no AI attribution of any kind.** No
`Co-Authored-By: Claude`, no "Generated with Claude Code", no session URL
trailer, no emoji footer. A `commit-msg` hook in `.githooks/` strips them as a
safety net, but do not write them in the first place.

Format — Conventional Commits with the task ID:

```
<type>(<scope>): <subject>   [<TASK-ID>]

<optional body: what changed and why, wrapped at 72 chars>
```

Types: `feat`, `fix`, `refactor`, `test`, `docs`, `chore`, `perf`, `ci`.
Scopes: `agent`, `gateway`, `api`, `airspace`, `pilot`, `infra`, `db`.

Examples:

```
feat(gateway): parse GLOBAL_POSITION_INT into telemetry stream   [P1-02]
fix(airspace): leave stale neighbours out of the CPA check       [S-11]
test(airspace): CPA detection for head-on converging tracks      [P5-07]
```

Rules:
- One logical change per commit. Do not batch unrelated work.
- Subject line imperative mood, no trailing period, max 72 chars.
- Never commit secrets, `.env` files, `.bin` logs, or SITL artifacts.
- Never `git push --force` on `main`.
- Branch naming: `feat/M-02-violations`, `fix/P1-02-heartbeat-timeout`.

## Repository layout

```
agent/        Ground-relay MAVLink agent, receive only (Python)
gateway/      MAVLink and Remote ID ingest, receive only
api/          Core REST/WS API: registry, zones, audit log, replay, console feed
airspace/     Airspace monitor: CPA, zone incursions, height limit
common/       Shared logging, configuration and height references
web-pilot/    Operator console (React)
tools/        Operator diagnostics and measurement scripts
infra/        docker-compose, migrations, CI, deployment
sim/          SITL launch scripts and scenario definitions
docs/         Architecture, runbooks, decision records
```

## Tech stack

| Layer            | Choice                          |
|------------------|---------------------------------|
| Agent            | Python 3.12, pymavlink, MAVSDK  |
| Gateway          | Python 3.12 asyncio             |
| API              | FastAPI + SQLAlchemy 2.x        |
| Relational DB    | PostgreSQL 16 + PostGIS 3.4     |
| Telemetry store  | TimescaleDB hypertable          |
| Cache / state    | Redis 7                         |
| Message bus      | NATS                            |
| Operator console | React + TypeScript + MapLibre   |
| Containers       | Docker Compose (dev), k8s later |

Do not add a dependency without a one-line justification in the commit body.

## Code standards

**Python**
- `ruff` for lint and format, `mypy --strict` on `gateway/`, `airspace/`,
  `common/`, `agent/`, `tools/` and `api/`. Type errors there are build
  failures. The first two are safety-relevant; `common/` because they import
  it, `agent/` because the ground relay enforces the receive-only (Stage 0)
  guarantee and is where telemetry is lost for good if it is lost at all,
  `tools/` because the diagnostics produce the evidence decisions rest on, and
  `api/` because it is the sign-in boundary and the only writer of the fleet
  registry and its telemetry projection. The authoritative
  list is the strict override in `pyproject.toml`, and
  `tests/test_layout.py` fails if the two disagree.
  Note that mypy's `strict` flag is global: setting it inside a per-module
  override silently enables strict everywhere, so the bundle is expanded
  flag by flag.
- `pytest`, with `pytest-asyncio`. Target 80% coverage on the two
  safety-relevant modules above, best-effort elsewhere.
- No bare `except:`. Log with structured context (`drone_id`, `station_id`).
- All units explicit in names: `alt_m`, `dist_m`, `batt_pct`, `speed_ms`,
  `timeout_s`. Never an unqualified `alt` or `dist`.

**TypeScript**
- `eslint` + `prettier`, `strict: true`.
- API types generated from the OpenAPI schema. Never hand-write them.

**Database**
- All schema changes via Alembic migrations. Never edit a table by hand.
- All geometry columns `SRID 4326`. Distance math on `geography`, not `geometry`.
- Timestamps `TIMESTAMPTZ`, always UTC. Convert at the display layer only.
- **There are two migration trees and they must never be merged.**

  | Tree | Database | Owns |
  |---|---|---|
  | `infra/migrations/telemetry/` | TimescaleDB | ingest index, archive index, `ingest_events`, `drone_state` (P1-04), `drone_firmware` (P1-11) |
  | `infra/migrations/relational/` | PostgreSQL + PostGIS | `ARCHITECTURE.md` §5: `bases`, `drones`, `pilots`, `airspace_zones`, `events` (P2-01); `incidents` (M-02) not yet |

  They are separate databases with separate version tables
  (`alembic_version_telemetry`, `alembic_version_relational`) and separate
  heads. The Gateway reaches only the telemetry one, which is what keeps
  ingest isolated from the business schema: a slow migration on `events`
  cannot stall telemetry, and a Gateway fault cannot reach `drones`.

  Merging them looks like tidying and is not. One tree means one head, so a
  migration written for one database runs against the other, and the version
  table that would have caught it has already been unified away. If a future
  task needs a table visible to both, it gets a row in one and a read path in
  the other, not a merged tree.
- The Gateway never connects to the relational database. Ingest events go to
  `ingest_events` in the telemetry database; `ARCHITECTURE.md` §5's `events`
  table is the business audit log and is written by services that own business
  entities. The console reads both.

## Coordinate and unit conventions

- Latitude/longitude: WGS84 decimal degrees. MAVLink sends `int32` at 1e7 scale —
  convert at the parser boundary, never deeper in the stack.
- Altitude: store both AMSL and AGL. **Always state which in the field name.**
  Separation is judged in AMSL; the height limit over the ground is evaluated
  in AGL (`ARCHITECTURE.md` §6.1). Never mix them in the same calculation.
- Speed m/s, distance metres, battery percent 0-100 and watt-hours separately.
- Headings degrees true, 0-359.

## Testing

- `make sim N=<n>` launches n SITL instances with unique SYSIDs.
- Integration tests run against SITL in CI, not against hardware.
- Scenario tests live in `sim/scenarios/` as YAML: drones, flight paths, wind,
  expected outcome. Airspace monitoring work is validated by scenarios, not unit
  tests alone.
- **Test presence, not only absence.** A test that asserts a safety or
  data-loss path does *not* happen — no gap, no drop, no rejection, no send —
  must be paired with a test that makes it happen and checks the result.
  Asserting absence without ever exercising presence proves nothing about the
  code that handles presence.
  This has bitten three times: a PARAM_VALUE offset that made the probe
  structurally unable to report BIDIRECTIONAL, a stub that rejected tokens with
  a WebSocket close so the relay's fatal-auth path never ran, and a relay `gap`
  that shipped unexecuted behind `assert gaps == []`. All three had passing
  tests. Branch coverage (`make cover`) is the backstop, not the rule.
- **Run the branch that says nothing is wrong.** The generalisation of the rule
  above, and the one that keeps being missed. A test suite builds working
  conditions, so the code that reports success, reports health, or degrades
  gracefully is the code least likely to have ever executed — and it fails
  *quietly*, because its whole purpose is to be unremarkable. Exercise it
  deliberately: make the thing succeed and read what it says; take the
  dependency away and watch what happens.
  Four instances, all with green suites:
  - `stop_sitl.sh` exited 1 in silence whenever the teardown *worked*. `pgrep`
    exits 1 when nothing matches, `pipefail` propagated it, `set -e` killed the
    script before the success message. The failure path ran constantly and was
    correct; the success path had never run to completion. It broke CI.
  - The console's documented promise that an unreachable bus leaves it
    "serving but empty" was false by minutes: `nats.connect` retries the
    initial connection about sixty times, so the page hung instead of loading.
    The `except` branch and its comment had never run.
  - Station link state was evaluated only when a `status` message arrived, but
    §9 defines `unreachable` by the *absence* of `status`. The one transition
    the state machine existed to detect was structurally unreachable while a
    session was open.
  - `TelemetryPublisher.publish_station` had no caller outside its own unit
    test, and `RelayServer.trackers` documented itself as being "for whoever
    publishes to the console". A seam can be designed, tested, and have no
    consumer; nothing fails, and the console just shows nothing.
  Corollary for cleanup and teardown: a path that reports success while doing
  nothing is worse than one that fails, and a warning that is sometimes false
  is one people learn to scroll past. Verify, then report what was verified.
- **Never write a wire-format offset from memory.** Derive it from pymavlink —
  from `ordered_fieldnames`, or by diffing two frames that differ in one field
  — and pin it with a test that derives it the same way. An offset that is
  wrong in a plausible way produces confident, meaningless output rather than
  an error.
  Three times in one session: `param_id` read at PARAM_VALUE offset 4 instead
  of 8, payload bounds counted back from the end of a signed v2 frame, and the
  MAVLink sequence byte read at offset 2 for v2 frames where it is at 4 and
  offset 2 is a constant. Every one of them returned a plausible answer.
- **Never report an inference as an observation.** If a command fails, read the
  error before interpreting it: a mangled path, an escaped colon or a
  permission denial is not evidence about the thing being checked. When the
  data source is unavailable, say the question is unanswered rather than
  answering it from a proxy.
  The wire-offset bugs, the "the SITL job has never run" claim - contradicted
  by four merged pull requests - and the `git cat-file` misread, where Git Bash
  rewrote `origin/main:path` to `origin\main;path` and the resulting error was
  read as "the file is absent", were all the same mistake. So was a generated
  workflow whose shell line continuations had been silently eaten: the file was
  written, not read back.

## What not to do

- Do not build a feature that is not in `TASKS.md`. Propose it, get it added.
- Do not refactor unrelated code while doing a task.
- Do not relax an alert threshold to make a test pass.
- Do not add retry loops around commands that have side effects without
  idempotency keys.

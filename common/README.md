# common

Shared logging and configuration, and the height references more than one
service needs. Every service imports this; nothing here imports a service.

## Configuration

A service declares what it needs as a settings class composed from the mixins
in `config.py`, and gets a validated, frozen object or a refusal to start:

```python
from common import NatsSettings, PostgresSettings, RedisSettings, ServiceSettings


class AirspaceSettings(ServiceSettings, PostgresSettings, RedisSettings, NatsSettings):
    service_name: str = "airspace"
```

No service reads `os.environ`. A value read directly is a value that was never
validated, has no declared type, and fails when it is first used rather than
before the process starts — which, for a service supervising aircraft, is the
difference between a failed deploy and a surprise in flight.
`tests/test_no_direct_environ.py` enforces this.

What does **not** belong here: separation minima, height limits, battery
thresholds, geofences. Those are operational parameters that
operators change without a deploy, so they live in the database where a change
is audited. Startup configuration is infrastructure — where the database is,
what to log, which port to bind.

## Logging

One JSON object per line on stdout. Context travels as fields, never
interpolated into the message:

```python
from common import bind, get_logger

log = bind(get_logger(__name__), drone_id=drone_id, station_id=station_id)
log.warning("height limit exceeded", extra={"height_agl_m": 124.5})
```

`print()` is banned repository-wide by ruff (T20). Timestamps are UTC-aware,
matching the `TIMESTAMPTZ` convention everywhere else.

## Startup

`start_service` does both, in the order that matters — configuration validated
first, logging installed before anything else runs:

```python
from common.startup import start_service

settings, log = start_service(AirspaceSettings)
```

## Height references

Two services need the same two grids, so they are here rather than in
either:

- `geoid.py`: geoid undulation (EGM2008 by default), turning a Remote ID
  height above the ellipsoid into AMSL (P1-15; `gateway/remote_id_ingest.py`).
- `terrain.py`: ground (surface) elevation from Copernicus DEM tiles, for
  height above ground (P5-00; `GET /terrain`).

Both read GeographicLib-style PGM files through `pgm.py`. The geoid grid
covers the globe; terrain covers only the cells fetched, and answers
"unknown" rather than 0 outside them. Fetching the files is
`infra/geoid/fetch_geoid.sh` and `python -m tools.terrain_fetch`; see
`docs/runbooks/p5-00-terrain.md`.

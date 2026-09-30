# gateway

MAVLink and Remote ID ingest. Receive only: it never sends anything towards an
aircraft.

Receives MAVLink from operator relays (relay-v1) and over UDP, identifies
vehicles by SYSID, converts every field to SI units at the parser boundary, and
fans the result out to TimescaleDB, Redis live state and NATS. Remote ID
observations arrive through `python -m gateway.remote_id_ingest`.

Safety-relevant: `mypy --strict` and an 80% coverage target apply here.

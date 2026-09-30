# airspace

The airspace monitor (`python -m airspace`): closest point of approach between
aircraft, zone incursions and the height limit, raised as alerts to people. It
never commands an aircraft.

Advice must be deterministic — the same conflict evaluated twice produces an
identical answer, so that two operators looking at two consoles are told
compatible things. See `docs/ARCHITECTURE.md` §6.

Safety-relevant: `mypy --strict` and an 80% coverage target apply here.
Alerting changes are validated by scenarios in `sim/scenarios/`, not by unit
tests alone.

# sim

SITL launch scripts and scenario definitions.

`run_sitl.sh` launches N ArduCopter SITL instances with unique SYSIDs and
distinct UDP ports. `scenarios/` holds YAML scenario definitions — drones,
flight paths, wind, expected outcome — which are how airspace monitoring work
is validated (`P5-12`).

The home location is **not** baked into the script. Copy `sitl.env.example` to
`sitl.env` and set it for your test area.

# agent

Ground-relay MAVLink agent.

This is the operator relay (`P1-01`): it reads the MAVLink stream QGC forwards
to UDP `127.0.0.1:14445`, authenticates, and forwards it to the Gateway over a
TLS WebSocket with a disk-backed queue that replays after an internet dropout.
It runs on the operator's ground station PC, not on the aircraft.

Nothing here may send anything back to the vehicle, now or later — see
`docs/ARCHITECTURE.md` §3.

# Switching a source off (U-15)

`ARCHITECTURE.md` §2.1: every position source can be switched off without a
deploy, by type (all relays, all Remote ID receivers, all network Remote ID
providers, all ADS-B feeds, all sensors) or by instance (one station,
receiver, provider or feed).

## How a switch travels

```
admin --PUT /sources/...--> API --one transaction--> source_controls + events
                              |
                              +--> NATS KV bucket  source_control / key "state"   (read path)
                              +--> NATS subject    control.sources               (push)
                                        |
             gateway (relays), remote_id_ingest, airspace monitor: SourceControlFollower
```

- **The API is the only writer.** `source_controls` (relational migration
  `0006_source_controls`) holds the current switch per `(source_type,
  instance_id)`, `instance_id = '*'` for a whole type, with reason, author
  and time. Every switch is an `events` row: entity_type `source`, entity_id
  `<type>/<instance or *>`, event_type `source_disabled` or
  `source_enabled`, payload with the reason and the previous state. A switch
  to the state already held writes nothing.
- **The read path is a JetStream key-value bucket**, one key holding the
  whole state with a version (the newest `changed_at` in microseconds).
  The Gateway must never reach the relational database (CLAUDE.md), and
  every follower already holds a NATS connection; the bucket is durable on
  the broker's disk, so a follower started later, or after a broker restart,
  reads the last state without the API. The telemetry database was the
  alternative: it would make the API write a second database on every
  switch, and followers would have to poll it with no way to be told.
- **Followers** read the bucket at start and every `SOURCE_CONTROL_POLL_S`
  (5 s), and apply what is pushed on the subject in between. A state older
  than the one held is ignored. A failed read keeps the state held.
- **Repair.** The API republishes from the database at start and every
  `SOURCE_CONTROL_REPUBLISH_S` (30 s). A switch whose publish failed after
  the commit answers 503 `not_propagated` and takes effect at the next
  republish. With no bus at all the API refuses a switch (503
  `control_channel_unavailable`) and changes nothing.

## The rule

A type switched off disables every instance. Otherwise the instance's own
row decides. An instance with no row is enabled, unless the API runs with
`SOURCES_DEFAULT_DENY=true`; the flag travels in the published state, so
every follower applies the same default. Before anything is published,
every source is enabled.

## What each part does with it

| Part | A disabled source |
|---|---|
| Gateway, relays | Upgrade refused with **503** and `Retry-After: 10` (never 401/403, which the relay takes as fatal). An open session is closed with **1013** "source disabled" when the switch arrives; a batch that arrives first is neither stored nor acknowledged. The relay keeps queueing and retrying, and on switching on delivers its queue as backlog, which the monitor records and does not alert on. |
| Remote ID ingest | Datagram dropped once its receiver is established, before the tracker, the store and the bus. |
| Airspace monitor | Its messages counted (`rejected_source_disabled`) and not judged. Its aircraft dropped at once, and their alerts cleared with reason **`source_disabled`**, published and audited. |
| Console | Sources tab: each type and instance, disabled (by type, instance or default deny) / healthy / stale / enabled-never-heard, last seen, refusals, the switch's reason and author. Admins switch with a reason; viewers cannot. Aircraft from a disabled source are marked *source disabled* and faded on the map. |

Each adapter publishes `source.<type>` every 2 s (every instance it knows,
from its token or key file, or has heard: enabled, last seen, accepted,
refused while disabled, connected) and logs a `source status` line every
minute. The Remote ID ingest's status line carries `dropped_source_disabled`.

## Doing it

```
PUT /sources/remote_id                          {"enabled": false, "reason": "..."}
PUT /sources/relay/instances/tbilisi-base-1     {"enabled": false, "reason": "..."}
GET /sources
```

Or from the console's Sources tab, as an admin.

## Verified 2026-10-01 with SITL

Three SITL vehicles hovering. SYSID 1 and 2 on two relays to the Gateway,
SYSID 3 through the U-16 bridge as Remote ID, a no-fly zone over each so
each had an alert.

- Remote ID off: SYSID 3's zone alert cleared as `source_disabled` 5 ms
  after the switch was committed; no Remote ID telemetry after it; SYSID 1
  and 2 and their alerts untouched; the receiver shown disabled by type with
  its refusals rising.
- Remote ID on: telemetry back and the alert raised again within a second.
- Station 1 off: its session closed with 1013, each reconnect refused with
  503 while the relay kept retrying; only SYSID 1's alert cleared, as
  `source_disabled`.
- Station 1 on: the relay reconnected at its next retry (4 s), resumed from
  the sequence it had stopped at, drained its queue as backlog, and the
  live alert was raised again.

A viewer's switch was refused with 403. Every switch is an `events` row with
the admin and the reason.

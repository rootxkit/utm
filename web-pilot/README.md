# web-pilot

The operator console (P6-01, P6-02, P6-03). React + TypeScript + MapLibre,
`strict: true`. Built into `dist/` and served by the API at `/app`; `/`
redirects there once it is built.

- **Map:** the self-hosted basemap (P1-12), zones, bases, every aircraft with
  its heading, and a line between the two aircraft of each conflict.
  Layer toggles for zones, bases and labels.
- **Aircraft, alerts, stations, sources, unclaimed sources** in the
  sidebar. Sources (U-15) lists each source type and instance with its
  state; an admin switches one off or on with a reason.
- **Detail panel** for the selected aircraft: everything the feed carries,
  battery and altitude trends, link quality, firmware, and a link to replay.
- **Alerts:** a tone repeats while a critical alert is unacknowledged.
  Operators and admins acknowledge; viewers see. Acknowledgement is per
  console and not yet recorded (P6-07).

It reads the API (session cookie, P6-08) and the console feed (a WebSocket
with a ticket the API signs), never a database.

## Build

```
python tools/export_openapi.py   # only when the API changed
cd web-pilot
npm ci
npm run build                    # generates src/api/schema.d.ts, typechecks, builds
npm run lint
```

API types are generated from `openapi.json`, which is committed and checked
against the API by `api/tests/test_openapi_export.py`. Never hand-write them.
The feed's message types (`src/types.ts`) are the exception: the feed is not
part of the OpenAPI schema, so they mirror `gateway/publisher.py` and
`airspace/service.py`.

`npm run dev` serves on Vite's port and proxies the API from `127.0.0.1:8010`,
so the session cookie is same-origin.

User-facing strings go through `src/i18n.ts` (`en`, `ka`).

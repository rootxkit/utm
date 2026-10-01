// U-15. Which sources are switched on, and what each is doing, as the console
// shows it. The switches come from the API (GET /sources, typed from the
// schema); what each source is doing comes from its adapter on the feed
// (`source.<type>`) and, for relay stations, from the station state the
// Gateway already publishes. The rule deciding whether a source is enabled is
// the one in common/sources.py, and must stay the same.
import type { components } from "./api/schema";
import type {
  DisabledBy,
  SourceInstanceActivity,
  SourceReport,
  Station,
  StationState,
  Telemetry,
} from "./types";

export type SourcesOut = components["schemas"]["SourcesOut"];
export type SourceControl = components["schemas"]["SourceControlOut"];

// What the panel says about a source. `disabled` wins over everything: a
// source switched off is never shown as merely silent.
export type SourceState = "disabled" | "healthy" | "stale" | "enabled";

// A source not heard for this long is stale. Matches the airspace monitor's
// default `stale_after_s`, after which its aircraft are dropped.
export const STALE_AFTER_MS = 15_000;

export const RELAY = "relay";

export interface SourceRow {
  sourceType: string;
  // Null for the row that stands for the whole type.
  instanceId: string | null;
  state: SourceState;
  disabledBy: DisabledBy | null;
  // The switch on this row itself, if one was ever made.
  control: SourceControl | null;
  // Epoch milliseconds, on the adapter's clock; null when never heard.
  lastSeenMs: number | null;
  refused: number;
  // Relay stations: the Gateway's own word on the link.
  stationState: StationState | null;
}

export interface SourceGroup {
  type: SourceRow;
  instances: SourceRow[];
}

function controlFor(
  controls: SourcesOut | null,
  sourceType: string,
  instanceId: string | null,
): SourceControl | null {
  return (
    controls?.controls.find(
      (c) => c.source_type === sourceType && (c.instance_id ?? null) === instanceId,
    ) ?? null
  );
}

// common/sources.py `SourceControlState.why_disabled`, from the API's list.
export function whyDisabled(
  controls: SourcesOut | null,
  sourceType: string,
  instanceId: string | null,
): DisabledBy | null {
  if (!controls) return null;
  const whole = controlFor(controls, sourceType, null);
  if (whole && !whole.enabled) return "type";
  if (instanceId === null) return null;
  const own = controlFor(controls, sourceType, instanceId);
  if (own) return own.enabled ? null : "instance";
  return controls.default_deny ? "default_deny" : null;
}

// The source a telemetry message came from: relay telemetry has no `source`
// and names its station; Remote ID names its receiver in `station_id`.
export function sourceOf(data: Pick<Telemetry, "source" | "station_id">): [string, string] {
  return [data.source ?? RELAY, data.station_id];
}

// Whether an aircraft's last track came from a source now switched off: it
// is out of the picture as "source disabled", not lost.
export function aircraftSourceDisabled(
  data: Pick<Telemetry, "source" | "station_id">,
  controls: SourcesOut | null,
): boolean {
  const [sourceType, instanceId] = sourceOf(data);
  return whyDisabled(controls, sourceType, instanceId) !== null;
}

function parseMs(value: string | null | undefined): number | null {
  if (!value) return null;
  const ms = Date.parse(value);
  return Number.isFinite(ms) ? ms : null;
}

// Healthy, stale, or switched on and never heard; `disabled` is decided
// before this is asked.
function liveState(
  activity: SourceInstanceActivity | undefined,
  report: SourceReport | undefined,
  station: Station | undefined,
  now: number,
  staleAfterMs: number,
): SourceState {
  if (station) {
    if (station.state === "unreachable" || station.state === "data_lost") return "stale";
    if (activity?.connected !== false) return "healthy";
  }
  const seenMs = parseMs(activity?.last_seen_at);
  if (seenMs === null || !report) return station ? "stale" : "enabled";
  // Age on the adapter's clock (seen against published), plus how long ago
  // the report reached this browser: neither clock is compared to the other.
  const publishedMs = parseMs(report.data.published_at) ?? seenMs;
  const ageMs = Math.max(0, publishedMs - seenMs) + Math.max(0, now - report.receivedAt);
  return ageMs <= staleAfterMs ? "healthy" : "stale";
}

export function sourceGroups(
  controls: SourcesOut | null,
  reports: Map<string, SourceReport>,
  stations: Map<string, Station>,
  now: number,
  staleAfterMs = STALE_AFTER_MS,
): SourceGroup[] {
  const types = new Set<string>(controls?.source_types ?? []);
  for (const key of reports.keys()) types.add(key);
  if (stations.size > 0) types.add(RELAY);
  for (const c of controls?.controls ?? []) types.add(c.source_type);

  const groups: SourceGroup[] = [];
  for (const sourceType of types) {
    const report = reports.get(sourceType);
    const byInstance = new Map<string, SourceInstanceActivity>();
    for (const item of report?.data.instances ?? []) byInstance.set(item.instance_id, item);
    const names = new Set<string>(byInstance.keys());
    for (const c of controls?.controls ?? []) {
      if (c.source_type === sourceType && c.instance_id) names.add(c.instance_id);
    }
    if (sourceType === RELAY) for (const id of stations.keys()) names.add(id);

    const instances = [...names].sort().map((instanceId): SourceRow => {
      const activity = byInstance.get(instanceId);
      const station = sourceType === RELAY ? stations.get(instanceId) : undefined;
      const disabledBy = whyDisabled(controls, sourceType, instanceId);
      return {
        sourceType,
        instanceId,
        disabledBy,
        state:
          disabledBy !== null
            ? "disabled"
            : liveState(activity, report, station, now, staleAfterMs),
        control: controlFor(controls, sourceType, instanceId),
        lastSeenMs: parseMs(activity?.last_seen_at),
        refused: activity?.refused_disabled ?? 0,
        stationState: station?.state ?? null,
      };
    });

    const typeDisabled = whyDisabled(controls, sourceType, null);
    const seen = instances.map((row) => row.lastSeenMs).filter((ms) => ms !== null);
    groups.push({
      type: {
        sourceType,
        instanceId: null,
        disabledBy: typeDisabled,
        state:
          typeDisabled !== null
            ? "disabled"
            : instances.some((row) => row.state === "healthy")
              ? "healthy"
              : instances.some((row) => row.state === "stale")
                ? "stale"
                : "enabled",
        control: controlFor(controls, sourceType, null),
        lastSeenMs: seen.length > 0 ? Math.max(...seen) : null,
        refused: instances.reduce((sum, row) => sum + row.refused, 0),
        stationState: null,
      },
      instances,
    });
  }
  // Types with something to show first, then the rest, each alphabetically.
  return groups.sort(
    (a, b) =>
      Number(b.instances.length > 0) - Number(a.instances.length > 0) ||
      a.type.sourceType.localeCompare(b.type.sourceType),
  );
}

// The body of PUT /sources/... . A reason is required, and refused blank.
export function switchRequest(
  enabled: boolean,
  reason: string,
): { enabled: boolean; reason: string } | null {
  const trimmed = reason.trim();
  return trimmed ? { enabled, reason: trimmed } : null;
}

export function switchPath(sourceType: string, instanceId: string | null): string {
  const base = `/sources/${encodeURIComponent(sourceType)}`;
  return instanceId === null ? base : `${base}/instances/${encodeURIComponent(instanceId)}`;
}

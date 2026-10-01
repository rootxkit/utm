// U-02. Network identification as the console shows it: a badge per track,
// its legend, and what an identification alert says. The status itself is
// decided by the Gateway (gateway/identification.py); nothing here
// re-decides it.
import type { Key } from "./i18n";
import type { Aircraft, IdentificationStatus, Telemetry } from "./types";

// In the order the legend lists them: the expected case first, then the
// ones that need someone's attention.
export const IDENTIFICATION_STATUSES: readonly IdentificationStatus[] = [
  "registered",
  "suspended",
  "unknown_operator",
  "unidentified",
];

// The statuses that, inside a zone needing an identity, become an incident
// (airspace/monitor.py INCIDENT_STATUSES).
export const NEEDS_ATTENTION: ReadonlySet<IdentificationStatus> = new Set([
  "unknown_operator",
  "unidentified",
]);

export function identificationStatus(
  data: Pick<Telemetry, "identification">,
): IdentificationStatus | null {
  const status = data.identification?.status;
  return status && (IDENTIFICATION_STATUSES as readonly string[]).includes(status) ? status : null;
}

export function statusLabelKey(status: IdentificationStatus | null): Key {
  return status === null ? "id_none" : `id_${status}`;
}

// What a status means. A registration by our fleet's serial alone ("fleet")
// says so: the operator ID was not compared, and a broadcast is a claim.
export function statusHintKey(status: IdentificationStatus, reason?: string | null): Key {
  if (status === "registered" && reason === "fleet") return "id_registered_fleet_hint";
  return `id_${status}_hint`;
}

// The CSS class of a status badge (styles.css `.pill.id-*`).
export function badgeClass(status: IdentificationStatus | null): string {
  return `pill id-${status ?? "none"}`;
}

// A position someone claims rather than one a relay authenticated: a
// direct broadcast or a USSP's word (P1-15, U-02).
export function isClaimed(data: Pick<Telemetry, "source">): boolean {
  return data.source === "remote_id" || data.source === "network_remote_id";
}

// How many aircraft are in each status, for the legend; null as its own.
export function statusCounts(
  aircraft: Map<string, Aircraft>,
): Record<IdentificationStatus | "none", number> {
  const counts = { registered: 0, suspended: 0, unknown_operator: 0, unidentified: 0, none: 0 };
  for (const item of aircraft.values()) {
    counts[identificationStatus(item.data) ?? "none"] += 1;
  }
  return counts;
}

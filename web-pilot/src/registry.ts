// The UAS operator registry (U-01), as the console shows it. Types come from
// the generated schema; nothing here is written by hand.
import type { components } from "./api/schema";

export type UasOperator = components["schemas"]["UasOperatorOut"];
export type Uas = components["schemas"]["UasOut"];
export type RemotePilot = components["schemas"]["RemotePilotOut"];
export type Competency = components["schemas"]["CompetencyOut"];
export type RegistrationStatus = components["schemas"]["RegistrationStatus"];

// What a registration amounts to right now: its status, or `expired` when it
// is active on paper and its validity has run out. Expiry is not a stored
// status; it is read from `valid_until` against the clock.
export type Standing = RegistrationStatus | "expired";

export const STATUSES: readonly RegistrationStatus[] = ["active", "suspended", "revoked"];

export function standing(
  status: RegistrationStatus,
  validUntil: string | null | undefined,
  now: number,
): Standing {
  if (status !== "active") return status;
  if (validUntil) {
    const until = Date.parse(validUntil);
    if (Number.isFinite(until) && until <= now) return "expired";
  }
  return "active";
}

export function competencyExpired(competency: Competency, now: number): boolean {
  return standing("active", competency.valid_until, now) === "expired";
}

// A query string from filters, leaving out what is unset or blank, so an empty
// search box does not become `q=` and filter everything out.
export function registryQuery(filters: Record<string, string | number | null | undefined>): string {
  const params = new URLSearchParams();
  for (const [name, value] of Object.entries(filters)) {
    if (value === null || value === undefined) continue;
    const text = String(value).trim();
    if (text) params.set(name, text);
  }
  const query = params.toString();
  return query ? `?${query}` : "";
}

// Competency identifiers as the regulation writes them.
export function competencyName(competency: Competency["competency"]): string {
  return competency.replace("A1_A3", "A1/A3").replace("STS_", "STS-");
}

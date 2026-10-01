import { describe, expect, it } from "vitest";
import {
  type Competency,
  competencyExpired,
  competencyName,
  registryQuery,
  standing,
} from "./registry";

const NOW = Date.parse("2026-10-01T12:00:00Z");

describe("standing", () => {
  it("is suspended or revoked as recorded, whatever the validity says", () => {
    expect(standing("suspended", "2030-01-01T00:00:00Z", NOW)).toBe("suspended");
    expect(standing("revoked", null, NOW)).toBe("revoked");
  });

  it("is expired when active and the validity has run out", () => {
    expect(standing("active", "2026-10-01T11:59:59Z", NOW)).toBe("expired");
    expect(standing("active", "2026-10-01T12:00:00Z", NOW)).toBe("expired");
  });

  it("is active when active and still valid, or with no validity recorded", () => {
    expect(standing("active", "2026-10-01T12:00:01Z", NOW)).toBe("active");
    expect(standing("active", null, NOW)).toBe("active");
    expect(standing("active", undefined, NOW)).toBe("active");
  });

  it("does not call an unreadable date expired", () => {
    expect(standing("active", "not a date", NOW)).toBe("active");
  });
});

describe("competencies", () => {
  const record = (valid_until: string | null): Competency => ({
    competency: "A2",
    certificate_ref: null,
    valid_until,
    recorded_at: "2026-01-01T00:00:00Z",
  });

  it("are expired past their validity and not before", () => {
    expect(competencyExpired(record("2026-09-30T00:00:00Z"), NOW)).toBe(true);
    expect(competencyExpired(record("2027-09-30T00:00:00Z"), NOW)).toBe(false);
    expect(competencyExpired(record(null), NOW)).toBe(false);
  });

  it("are named as the regulation writes them", () => {
    expect(competencyName("A1_A3")).toBe("A1/A3");
    expect(competencyName("A2")).toBe("A2");
    expect(competencyName("STS_01")).toBe("STS-01");
  });
});

describe("registryQuery", () => {
  it("leaves out blank and unset filters", () => {
    expect(registryQuery({ q: "  ", status: null, operator_id: undefined })).toBe("");
  });

  it("encodes what is set", () => {
    expect(registryQuery({ q: " kartli & co ", status: "suspended", limit: 200 })).toBe(
      "?q=kartli+%26+co&status=suspended&limit=200",
    );
  });
});

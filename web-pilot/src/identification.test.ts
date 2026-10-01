import { describe, expect, it } from "vitest";
import { translator } from "./i18n";
import {
  IDENTIFICATION_STATUSES,
  NEEDS_ATTENTION,
  badgeClass,
  identificationStatus,
  isClaimed,
  statusCounts,
  statusHintKey,
  statusLabelKey,
} from "./identification";
import type { Aircraft, Identification, IdentificationStatus, Telemetry } from "./types";

function identification(status: IdentificationStatus, mismatch = false): Identification {
  return {
    status,
    reason: "test",
    serial: status === "unidentified" ? null : "SN-1",
    operator_reg: "GEOX",
    mismatch,
    registered_operator_reg: mismatch ? "GEOY" : null,
  };
}

function aircraft(status: IdentificationStatus | null): Aircraft {
  return {
    data: {
      drone_id: "d",
      identification: status === null ? null : identification(status),
    } as Telemetry,
    receivedAt: 0,
    history: [],
  };
}

describe("identificationStatus", () => {
  it("reads each of the four statuses the Gateway publishes", () => {
    for (const status of IDENTIFICATION_STATUSES) {
      expect(identificationStatus({ identification: identification(status) })).toBe(status);
    }
  });

  it("is null when the track carries none, or one this console does not know", () => {
    expect(identificationStatus({})).toBeNull();
    expect(identificationStatus({ identification: null })).toBeNull();
    const odd = {
      ...identification("registered"),
      status: "verified",
    } as unknown as Identification;
    expect(identificationStatus({ identification: odd })).toBeNull();
  });
});

describe("badges and their legend", () => {
  it("has a label and a hint for every status, in both languages", () => {
    for (const lang of ["en", "ka"] as const) {
      const t = translator(lang);
      for (const status of IDENTIFICATION_STATUSES) {
        expect(t(statusLabelKey(status))).not.toBe(statusLabelKey(status));
        expect(t(statusHintKey(status)).length).toBeGreaterThan(10);
      }
      expect(t(statusLabelKey(null))).toBeTruthy();
    }
  });

  it("speaks Georgian in ka, not the English fallback", () => {
    const en = translator("en");
    const ka = translator("ka");
    for (const status of IDENTIFICATION_STATUSES) {
      expect(ka(statusLabelKey(status))).not.toBe(en(statusLabelKey(status)));
    }
    expect(ka("id_mismatch")).not.toBe(en("id_mismatch"));
    expect(ka("network_rid")).not.toBe(en("network_rid"));
  });

  it("gives each status its own badge class", () => {
    const classes = new Set(IDENTIFICATION_STATUSES.map((s) => badgeClass(s)));
    expect(classes.size).toBe(4);
    expect(badgeClass(null)).toBe("pill id-none");
  });

  it("counts aircraft per status, the unidentified-yet apart", () => {
    const fleet = new Map<string, Aircraft>([
      ["a", aircraft("registered")],
      ["b", aircraft("registered")],
      ["c", aircraft("unidentified")],
      ["d", aircraft(null)],
    ]);
    expect(statusCounts(fleet)).toEqual({
      registered: 2,
      suspended: 0,
      unknown_operator: 0,
      unidentified: 1,
      none: 1,
    });
  });

  it("flags for attention exactly the statuses that open an incident in a zone", () => {
    expect([...NEEDS_ATTENTION].sort()).toEqual(["unidentified", "unknown_operator"]);
  });

  it("fills in an identification alert's sentence", () => {
    const t = translator("en");
    expect(
      t("identification_in_zone", { status: t(statusLabelKey("unidentified")), zone: "TBS-1" }),
    ).toBe("unidentified aircraft inside TBS-1");
    expect(t("identification_mismatch", { given: "GEOX", registered: "GEOY" })).toBe(
      "Gives operator GEOX; registered to GEOY",
    );
  });
});

describe("isClaimed", () => {
  it("is true for both Remote ID sources and false for the relay", () => {
    expect(isClaimed({ source: "remote_id" })).toBe(true);
    expect(isClaimed({ source: "network_remote_id" })).toBe(true);
    expect(isClaimed({})).toBe(false);
  });
});

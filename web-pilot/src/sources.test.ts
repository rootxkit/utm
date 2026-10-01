import { describe, expect, it } from "vitest";
import {
  type SourceControl,
  type SourceGroup,
  type SourcesOut,
  STALE_AFTER_MS,
  aircraftSourceDisabled,
  sourceGroups,
  switchPath,
  switchOutcome,
  switchRequest,
  whyDisabled,
} from "./sources";
import type { SourceInstanceActivity, SourceReport, Station } from "./types";

const NOW = Date.parse("2026-10-01T12:00:30Z");
const PUBLISHED = "2026-10-01T12:00:30+00:00";

function control(source_type: string, instance_id: string | null, enabled: boolean): SourceControl {
  return {
    source_type,
    instance_id,
    enabled,
    reason: "test",
    changed_by: "test-admin",
    changed_at: "2026-10-01T12:00:00Z",
  };
}

function controls(list: SourceControl[], default_deny = false): SourcesOut {
  return { default_deny, source_types: ["relay", "remote_id"], controls: list };
}

function instance(id: string, lastSeen: string | null, extra = {}): SourceInstanceActivity {
  return {
    instance_id: id,
    enabled: true,
    disabled_by: null,
    last_seen_at: lastSeen,
    accepted: 1,
    refused_disabled: 0,
    last_refused_at: null,
    connected: null,
    ...extra,
  };
}

function report(type: string, instances: SourceInstanceActivity[]): SourceReport {
  return {
    data: {
      source_type: type,
      enabled: true,
      control_version: 1,
      published_at: PUBLISHED,
      instances,
    },
    receivedAt: NOW,
  };
}

function groupOf(groups: SourceGroup[], type: string): SourceGroup {
  const found = groups.find((g) => g.type.sourceType === type);
  if (!found) throw new Error(`no ${type} group`);
  return found;
}

describe("whyDisabled, the rule of common/sources.py", () => {
  it("enables everything before any switch is known", () => {
    expect(whyDisabled(null, "relay", "s1")).toBeNull();
    expect(whyDisabled(controls([]), "relay", "s1")).toBeNull();
  });

  it("disables one instance and not its neighbours", () => {
    const c = controls([control("relay", "s1", false)]);
    expect(whyDisabled(c, "relay", "s1")).toBe("instance");
    expect(whyDisabled(c, "relay", "s2")).toBeNull();
    expect(whyDisabled(c, "remote_id", "s1")).toBeNull();
  });

  it("lets a whole type outrank an instance switched on", () => {
    const c = controls([control("remote_id", null, false), control("remote_id", "rx", true)]);
    expect(whyDisabled(c, "remote_id", "rx")).toBe("type");
    expect(whyDisabled(c, "remote_id", null)).toBe("type");
  });

  it("applies default deny to instances without a row only", () => {
    const c = controls([control("relay", "s1", true)], true);
    expect(whyDisabled(c, "relay", "s1")).toBeNull();
    expect(whyDisabled(c, "relay", "new")).toBe("default_deny");
    expect(whyDisabled(c, "relay", null)).toBeNull();
  });
});

describe("aircraftSourceDisabled", () => {
  const c = controls([control("remote_id", null, false), control("relay", "s1", false)]);

  it("marks Remote ID tracks when Remote ID is off", () => {
    expect(aircraftSourceDisabled({ source: "remote_id", station_id: "rx-1" }, c)).toBe(true);
  });

  it("marks only the disabled station's relay tracks", () => {
    expect(aircraftSourceDisabled({ station_id: "s1" }, c)).toBe(true);
    expect(aircraftSourceDisabled({ station_id: "s2" }, c)).toBe(false);
  });
});

describe("sourceGroups", () => {
  it("shows a disabled source as disabled, never as silent", () => {
    const c = controls([control("remote_id", "rx-1", false)]);
    const reports = new Map([
      ["remote_id", report("remote_id", [instance("rx-1", "2026-10-01T12:00:29Z")])],
    ]);
    const rid = groupOf(sourceGroups(c, reports, new Map(), NOW), "remote_id");
    expect(rid.instances.map((r) => [r.instanceId, r.state])).toEqual([["rx-1", "disabled"]]);
    expect(rid.instances[0]?.control?.reason).toBe("test");
    expect(rid.type.state).toBe("enabled");
  });

  it("tells healthy from stale by how long ago the adapter heard it", () => {
    const reports = new Map([
      [
        "remote_id",
        report("remote_id", [
          instance("fresh", "2026-10-01T12:00:28Z"),
          instance("old", "2026-10-01T11:59:00Z"),
          instance("never", null),
        ]),
      ],
    ]);
    const rid = groupOf(sourceGroups(controls([]), reports, new Map(), NOW), "remote_id");
    expect(Object.fromEntries(rid.instances.map((r) => [r.instanceId, r.state]))).toEqual({
      fresh: "healthy",
      never: "enabled",
      old: "stale",
    });
    expect(rid.type.state).toBe("healthy");
  });

  it("goes stale when the adapter itself stops reporting", () => {
    const quiet = report("remote_id", [instance("rx", PUBLISHED)]);
    quiet.receivedAt = NOW - STALE_AFTER_MS - 1000;
    const rid = groupOf(
      sourceGroups(controls([]), new Map([["remote_id", quiet]]), new Map(), NOW),
      "remote_id",
    );
    expect(rid.instances[0]?.state).toBe("stale");
  });

  it("uses the station's own state for relays, and lists stations never reported", () => {
    const stations = new Map<string, Station>([
      ["s1", { station_id: "s1", state: "healthy" } as Station],
      ["s2", { station_id: "s2", state: "unreachable" } as Station],
    ]);
    const c = controls([control("relay", "s3", false)]);
    const relay = groupOf(sourceGroups(c, new Map(), stations, NOW), "relay");
    expect(relay.instances.map((r) => [r.instanceId, r.state, r.stationState])).toEqual([
      ["s1", "healthy", "healthy"],
      ["s2", "stale", "unreachable"],
      ["s3", "disabled", null],
    ]);
  });

  it("disables every instance of a type switched off, and the type row", () => {
    const c = controls([control("relay", null, false)]);
    const stations = new Map([["s1", { station_id: "s1", state: "unreachable" } as Station]]);
    const relay = groupOf(sourceGroups(c, new Map(), stations, NOW), "relay");
    expect(relay.type.state).toBe("disabled");
    expect(relay.instances[0]?.disabledBy).toBe("type");
  });

  it("adds up refusals onto the type row", () => {
    const reports = new Map([
      [
        "remote_id",
        report("remote_id", [
          instance("a", null, { refused_disabled: 3 }),
          instance("b", null, { refused_disabled: 4 }),
        ]),
      ],
    ]);
    const rid = groupOf(sourceGroups(controls([]), reports, new Map(), NOW), "remote_id");
    expect(rid.type.refused).toBe(7);
  });

  it("puts types with instances first", () => {
    const reports = new Map([["remote_id", report("remote_id", [instance("rx", null)])]]);
    const types = sourceGroups(controls([]), reports, new Map(), NOW).map((g) => g.type.sourceType);
    expect(types).toEqual(["remote_id", "relay"]);
  });
});

describe("switchOutcome", () => {
  it("reads not_propagated as recorded and pending, not as refused", () => {
    expect(switchOutcome("not_propagated")).toBe("pending");
  });

  it("reads an unavailable channel as nothing changed", () => {
    expect(switchOutcome("control_channel_unavailable")).toBe("unchanged");
  });

  it("reads anything else as refused", () => {
    expect(switchOutcome("reason_required")).toBe("refused");
    expect(switchOutcome("403")).toBe("refused");
  });
});

describe("switching", () => {
  it("refuses a blank reason and trims one given", () => {
    expect(switchRequest(false, "   ")).toBeNull();
    expect(switchRequest(false, " spoofing ")).toEqual({ enabled: false, reason: "spoofing" });
  });

  it("addresses a type or one instance", () => {
    expect(switchPath("remote_id", null)).toBe("/sources/remote_id");
    expect(switchPath("relay", "gs:1")).toBe("/sources/relay/instances/gs%3A1");
  });
});

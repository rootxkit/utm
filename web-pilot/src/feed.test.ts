import { describe, expect, it } from "vitest";
import { apply, nextAttempt, reconnectDelay, STABLE_AFTER_MS, type FeedState } from "./feed";
import type { Alert, FeedMessage, Station, Telemetry } from "./types";

const empty = (): FeedState => ({
  aircraft: new Map(),
  stations: new Map(),
  alerts: new Map(),
  unclaimed: new Map(),
});

const telemetry = { drone_id: "d1", batt_pct: 80, alt_above_home_m: 12 } as Telemetry;
const station = { station_id: "s1", state: "healthy" } as Station;
const alert = (state: Alert["state"]) =>
  ({ key: "k1", state, kind: "zone", severity: "warning", drone_ids: ["d1"] }) as Alert;

describe("apply", () => {
  it("stores telemetry with a history point", () => {
    const next = apply(empty(), { kind: "telemetry", name: "t", data: telemetry });
    const aircraft = next.aircraft.get("d1");
    expect(aircraft?.data).toBe(telemetry);
    expect(aircraft?.history).toHaveLength(1);
    expect(aircraft?.history[0]).toMatchObject({ batt: 80, alt: 12 });
  });

  it("stores a station", () => {
    const next = apply(empty(), { kind: "station", name: "s", data: station });
    expect(next.stations.get("s1")).toBe(station);
  });

  it("raises and clears an alert", () => {
    const raised = apply(empty(), { kind: "alert", name: "a", data: alert("raised") });
    expect(raised.alerts.has("k1")).toBe(true);
    const cleared = apply(raised, { kind: "alert", name: "a", data: alert("cleared") });
    expect(cleared.alerts.has("k1")).toBe(false);
  });

  it("records unclaimed and rejected sources, ignores other events", () => {
    const data = { station_id: "s1", sysid: 1, compid: 2, reason: "x" };
    const one = apply(empty(), { kind: "events", name: "unclaimed_source", data });
    expect(one.unclaimed.get("s1/1/2")?.rejected).toBe(false);
    const two = apply(one, { kind: "events", name: "rejected_source", data });
    expect(two.unclaimed.get("s1/1/2")?.rejected).toBe(true);
    expect(apply(two, { kind: "events", name: "other", data })).toBe(two);
  });

  it("leaves the state unchanged for an unknown kind", () => {
    const state = empty();
    const next = apply(state, { kind: "from_the_future", data: {} } as unknown as FeedMessage);
    expect(next).toBe(state);
  });
});

describe("reconnectDelay", () => {
  it("grows exponentially with the attempt", () => {
    expect(reconnectDelay(0, 1)).toBe(1000);
    expect(reconnectDelay(1, 1)).toBe(2000);
    expect(reconnectDelay(2, 1)).toBe(4000);
    expect(reconnectDelay(3, 0)).toBe(4000);
  });

  it("is capped at 30 s", () => {
    expect(reconnectDelay(10, 1)).toBe(30000);
    expect(reconnectDelay(1000, 1)).toBe(30000);
    expect(reconnectDelay(1000, 0)).toBe(15000);
  });

  it("jitters within half to full of the ceiling", () => {
    expect(reconnectDelay(2, 0)).toBe(2000);
    expect(reconnectDelay(2, 0.5)).toBe(3000);
  });

  it("has a floor of 500 ms, also after 4401", () => {
    expect(reconnectDelay(0, 0)).toBeGreaterThanOrEqual(500);
    expect(reconnectDelay(0, 0, 4401)).toBe(500);
  });

  it("backs off on repeated 4401", () => {
    expect(reconnectDelay(3, 0, 4401)).toBe(4000);
  });
});

describe("nextAttempt", () => {
  it("keeps counting after a short or failed connection", () => {
    expect(nextAttempt(3, null)).toBe(4);
    expect(nextAttempt(3, STABLE_AFTER_MS - 1)).toBe(4);
  });

  it("restarts after a stable connection", () => {
    expect(nextAttempt(7, STABLE_AFTER_MS)).toBe(1);
  });
});

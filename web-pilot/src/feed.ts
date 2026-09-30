// The console feed: one WebSocket to the console service, with a ticket the
// API signs (P6-08). Before every connect the page asks the API for a fresh
// ticket; the feed closes with 4401 when it runs out, and the page renews it
// and reconnects. A 401 from the API means the session itself is gone.
import { useEffect, useReducer, useState } from "react";
import { apiPost } from "./api/client";
import type { Aircraft, Alert, FeedMessage, Station, Unclaimed } from "./types";

const SIGN_IN_REQUIRED = 4401;
const HISTORY_POINTS = 600;
const HISTORY_EVERY_MS = 2000;

export type FeedStatus = "connecting" | "live" | "down";

export interface FeedState {
  aircraft: Map<string, Aircraft>;
  stations: Map<string, Station>;
  alerts: Map<string, Alert>;
  unclaimed: Map<string, Unclaimed>;
}

function empty(): FeedState {
  return { aircraft: new Map(), stations: new Map(), alerts: new Map(), unclaimed: new Map() };
}

export function apply(state: FeedState, message: FeedMessage): FeedState {
  const now = Date.now();
  switch (message.kind) {
    case "telemetry": {
      const aircraft = new Map(state.aircraft);
      const previous = aircraft.get(message.data.drone_id);
      let history = previous?.history ?? [];
      const last = history[history.length - 1];
      if (!last || now - last.t >= HISTORY_EVERY_MS) {
        history = [
          ...history.slice(-(HISTORY_POINTS - 1)),
          { t: now, batt: message.data.batt_pct, alt: message.data.alt_above_home_m },
        ];
      }
      aircraft.set(message.data.drone_id, { data: message.data, receivedAt: now, history });
      return { ...state, aircraft };
    }
    case "station": {
      const stations = new Map(state.stations);
      stations.set(message.data.station_id, message.data);
      return { ...state, stations };
    }
    case "alert": {
      const alerts = new Map(state.alerts);
      if (message.data.state === "cleared") alerts.delete(message.data.key);
      else alerts.set(message.data.key, message.data);
      return { ...state, alerts };
    }
    case "events": {
      if (message.name !== "unclaimed_source" && message.name !== "rejected_source") return state;
      const unclaimed = new Map(state.unclaimed);
      const d = message.data;
      unclaimed.set(`${d.station_id}/${d.sysid}/${d.compid}`, {
        ...d,
        rejected: message.name === "rejected_source",
      });
      return { ...state, unclaimed };
    }
    default:
      // A kind this console does not know (a newer server): ignore it.
      return state;
  }
}

const BACKOFF_BASE_MS = 1000;
const BACKOFF_CAP_MS = 30000;
const BACKOFF_MIN_MS = 500;
export const STABLE_AFTER_MS = 10000;

// Delay before reconnect attempt number `attempt` (0 for the first retry):
// exponential from 1 s, capped at 30 s, with "equal jitter" (half fixed, half
// random) so a fleet of consoles does not reconnect in lockstep. Never below
// 500 ms, so a 4401 close cannot spin. A first 4401 (the ticket simply ran
// out) renews at the minimum; repeated ones back off like any failure.
// `random` is in [0, 1).
export function reconnectDelay(attempt: number, random: number, code?: number): number {
  if (code === SIGN_IN_REQUIRED && attempt === 0) return BACKOFF_MIN_MS;
  const ceiling = Math.min(BACKOFF_CAP_MS, BACKOFF_BASE_MS * 2 ** Math.max(0, attempt));
  return Math.max(BACKOFF_MIN_MS, Math.round(ceiling / 2 + (ceiling / 2) * random));
}

// The attempt counter after a connection ended having been up for `upMs`
// (null: it never opened): a stable connection starts over at 1, since the
// retry that follows it is the first, anything else keeps counting.
export function nextAttempt(attempt: number, upMs: number | null): number {
  return upMs !== null && upMs >= STABLE_AFTER_MS ? 1 : attempt + 1;
}

// The feed address may be relative ("/ws/telemetry", behind a same-origin
// front) or absolute. Resolved against the page, with the scheme to match.
export function feedAddress(url: string, page: string): string {
  const resolved = new URL(url, page);
  if (resolved.protocol === "http:") resolved.protocol = "ws:";
  if (resolved.protocol === "https:") resolved.protocol = "wss:";
  return resolved.toString();
}

export function useFeed(feedUrl: string | null): { state: FeedState; status: FeedStatus } {
  const [state, dispatch] = useReducer(apply, undefined, empty);
  const [status, setStatus] = useState<FeedStatus>("connecting");
  useEffect(() => {
    if (!feedUrl) return;
    // Per run of this effect, not a ref: a connect still awaiting its ticket
    // when the effect is torn down must not open a second socket.
    let stopped = false;
    let socket: WebSocket | null = null;
    let timer: number | undefined;
    let attempt = 0;
    let openedAt: number | null = null;
    let warnedParse = false;

    const retry = (code?: number, upMs: number | null = null) => {
      const stable = upMs !== null && upMs >= STABLE_AFTER_MS;
      const delay = reconnectDelay(stable ? 0 : attempt, Math.random(), code);
      attempt = nextAttempt(attempt, upMs);
      timer = window.setTimeout(connect, delay);
    };

    const connect = async () => {
      if (stopped) return;
      setStatus("connecting");
      try {
        const ticket = await apiPost("/auth/feed-ticket");
        if (!ticket.ok) throw new Error(`ticket ${ticket.status}`);
      } catch {
        if (stopped) return;
        setStatus("down");
        retry();
        return;
      }
      if (stopped) return;
      socket = new WebSocket(feedAddress(feedUrl, location.href));
      socket.onopen = () => {
        openedAt = Date.now();
        setStatus("live");
      };
      socket.onmessage = (event) => {
        let message: FeedMessage;
        try {
          message = JSON.parse(String(event.data)) as FeedMessage;
        } catch {
          if (!warnedParse) {
            warnedParse = true;
            console.warn("feed: ignoring a message that is not JSON");
          }
          return;
        }
        dispatch(message);
      };
      socket.onclose = (event) => {
        if (stopped) return;
        setStatus(event.code === SIGN_IN_REQUIRED ? "connecting" : "down");
        const upMs = openedAt === null ? null : Date.now() - openedAt;
        openedAt = null;
        retry(event.code, upMs);
      };
    };
    void connect();
    return () => {
      stopped = true;
      window.clearTimeout(timer);
      socket?.close();
    };
  }, [feedUrl]);

  return { state, status };
}

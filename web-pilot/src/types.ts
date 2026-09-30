// Messages on the console feed (api/telemetry_ws.py). The feed is a
// WebSocket, not part of the OpenAPI schema, so these mirror the encoders in
// gateway/publisher.py and airspace/service.py by hand. Every measurement is
// nullable there, and here: null means "not measured", never zero.

export interface LinkQuality {
  window_s: number;
  loss_pct: number | null;
  heartbeat_gap_max_s: number | null;
}

export interface Firmware {
  version: string | null;
  git_hash: string | null;
}

// P1-15. What a Remote ID broadcast adds. A broadcast is not authenticated:
// anyone can transmit one, so it is a claim, never a verified position.
export interface RemoteIdInfo {
  ua_id: string;
  id_type: number;
  ua_type: number;
  status: number;
  operator_id: string | null;
  operator_lat_deg: number | null;
  operator_lon_deg: number | null;
  transmitter: string;
  rssi_dbm: number | null;
}

export interface Telemetry {
  drone_id: string;
  // Absent on MAVLink telemetry; "remote_id" on a broadcast (P1-15).
  source?: "remote_id";
  authenticated?: boolean;
  // Remote ID reports track over the ground, not heading.
  track_deg?: number | null;
  // Height above the WGS-84 ellipsoid, as broadcast. Not AMSL.
  alt_hae_m?: number | null;
  airborne?: boolean;
  remote_id?: RemoteIdInfo;
  label: string | null;
  link: LinkQuality | null;
  firmware: Firmware | null;
  ts: string;
  station_id: string;
  lat_deg: number | null;
  lon_deg: number | null;
  alt_amsl_m: number | null;
  // Above the home point, not the ground. There is no height above ground.
  alt_above_home_m: number | null;
  heading_deg: number | null;
  // North, east, down: down is positive, so a climb is negative.
  vx_ms: number | null;
  vy_ms: number | null;
  vz_ms: number | null;
  batt_pct: number | null;
  batt_voltage_v: number | null;
  batt_consumed_wh: number | null;
  mode: string | null;
  armed: boolean | null;
  gps_fix_type: number | null;
  sat_count: number | null;
  groundspeed_ms: number | null;
  climb_ms: number | null;
}

export type StationState = "healthy" | "radio_silent" | "unreachable" | "data_lost" | "lagging";

export interface Station {
  station_id: string;
  state: StationState;
  data_is_lost: boolean;
  buffering: boolean;
  last_datagram_age_ms: number | null;
  queue_depth: number | null;
  lag_s: number | null;
  losses: { kind: string; datagram_count: number | null; detail: string }[];
}

export interface Alert {
  state: "raised" | "active" | "cleared";
  key: string;
  kind: "conflict" | "zone" | "height";
  severity: "critical" | "warning";
  drone_ids: string[];
  labels: (string | null)[];
  detail: {
    t_cpa_s?: number;
    d_cpa_horizontal_m?: number;
    d_alt_at_cpa_m?: number;
    d_horizontal_now_m?: number;
    zone_name?: string;
    zone_type?: string;
    alt_amsl_m?: number;
    // From an authority's ED-269 file (P5-18).
    external_id?: string;
    restriction?: string;
    message?: string | null;
    height_agl_m?: number;
    max_height_agl_m?: number;
    ground_elevation_m?: number;
    dataset?: string;
  };
}

export interface Unclaimed {
  station_id: string;
  sysid: number;
  compid: number;
  reason: string;
  rejected: boolean;
}

export type FeedMessage =
  | { kind: "telemetry"; name: string; data: Telemetry }
  | { kind: "station"; name: string; data: Station }
  | { kind: "alert"; name: string; data: Alert }
  | { kind: "events"; name: string; data: Omit<Unclaimed, "rejected"> };

export interface Aircraft {
  data: Telemetry;
  // Browser clock when the last message arrived: link age is measured here,
  // because the capture time is the station's clock.
  receivedAt: number;
  // Recent battery and altitude, for the sparklines. Bounded.
  history: { t: number; batt: number | null; alt: number | null }[];
}

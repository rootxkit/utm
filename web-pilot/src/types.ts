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
  // S-32: false for a transmitter heard without a fresh identity. Its
  // ua_id is then "", id_type 0 and ua_type null, and the track's label is
  // the transmitter address.
  identified?: boolean;
  ua_id: string;
  id_type: number;
  ua_type: number | null;
  status: number;
  operator_id: string | null;
  operator_lat_deg: number | null;
  operator_lon_deg: number | null;
  transmitter: string;
  rssi_dbm: number | null;
  // S-27: what the track's captured_at is, the broadcast's own time or
  // the Gateway's receive time; and the broadcast's timestamp accuracy.
  time_source?: "broadcast" | "receiver";
  ts_accuracy_s?: number | null;
}

// U-02. Who a track is, as the registry sees it (gateway/identification.py).
// Every source carries it; null where the adapter had no registry to ask.
export type IdentificationStatus = "registered" | "suspended" | "unknown_operator" | "unidentified";

export interface Identification {
  status: IdentificationStatus;
  // A stable code for which rule applied, e.g. "operator_mismatch".
  reason: string;
  serial: string | null;
  operator_reg: string | null;
  // A registered serial given with an operator that is not its owner.
  mismatch: boolean;
  // Only on a mismatch: the operator the registry has for the serial.
  registered_operator_reg: string | null;
}

// U-02. What network Remote ID (an ASTM F3411 USSP) adds.
export interface NetworkRidInfo {
  provider: string;
  flight_id: string;
  aircraft_type: string | null;
  simulated: boolean;
  // The provider projected this position forward.
  extrapolated: boolean;
  serial: string | null;
  registration_id: string | null;
  operator_id: string | null;
  operator_lat_deg: number | null;
  operator_lon_deg: number | null;
  time_source: "broadcast" | "receiver";
  ts_accuracy_s: number | null;
}

export interface Telemetry {
  drone_id: string;
  // Absent on MAVLink telemetry; "remote_id" on a broadcast (P1-15);
  // "network_remote_id" from a USSP (U-02).
  source?: "remote_id" | "network_remote_id";
  // U-02: "provider" for network Remote ID, as trustworthy as the USSP.
  trust?: "provider";
  authenticated?: boolean;
  identification?: Identification | null;
  network_rid?: NetworkRidInfo;
  // Remote ID reports track over the ground, not heading.
  track_deg?: number | null;
  // Height above the WGS-84 ellipsoid, as broadcast. Not AMSL.
  alt_hae_m?: number | null;
  // S-33: which broadcast altitude alt_amsl_m came from. "pressure" is
  // referenced to 1013.25 hPa, not AMSL: vertical position unknown.
  alt_source?: "geodetic" | "pressure" | null;
  // Pressure altitude as broadcast. Not AMSL.
  alt_pressure_m?: number | null;
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
  // U-02: "identification" is an unidentified or unknown-operator aircraft in
  // a zone that needs an identity (the incident seam); "identification_mismatch"
  // a registered serial given with another operator's number.
  kind: "conflict" | "zone" | "height" | "identification" | "identification_mismatch";
  // "info" only for a CONDITIONAL zone when policy says so (U-03).
  severity: "critical" | "warning" | "info";
  drone_ids: string[];
  labels: (string | null)[];
  detail: {
    t_cpa_s?: number;
    d_cpa_horizontal_m?: number;
    // Null when either aircraft's vertical position is unknown (S-33).
    d_alt_at_cpa_m?: number | null;
    vertical_separation_known?: boolean;
    // Zone and height alerts on a pressure altitude (S-33): approximate to
    // within this margin; within_band false when only inside the widened
    // band, which is a warning.
    vertical_known?: boolean;
    pressure_uncertainty_m?: number;
    within_band?: boolean;
    // A zone limit above the ground with no terrain to judge it (U-03): the
    // alert is a warning that may be false, never a missed critical.
    limit_not_judged?: boolean;
    not_judged?: string[];
    d_horizontal_now_m?: number;
    // Zones (U-03): the ED-269 zone and the aircraft's height in each
    // reference its limits use.
    zone_id?: string;
    identifier?: string;
    zone_name?: string | null;
    restriction?: "PROHIBITED" | "REQ_AUTHORISATION" | "CONDITIONAL" | "NO_RESTRICTION";
    reason?: string[];
    message?: string | null;
    lower_limit_m?: number;
    lower_reference?: string;
    upper_limit_m?: number;
    upper_reference?: string;
    alt_hae_m?: number;
    alt_amsl_m?: number;
    height_agl_m?: number;
    max_height_agl_m?: number;
    ground_elevation_m?: number;
    dataset?: string;
    // U-02: identification alerts, and every zone alert's aircraft.
    status?: IdentificationStatus;
    serial?: string | null;
    operator_reg?: string | null;
    registered_operator_reg?: string | null;
    mismatch?: boolean;
    incident_candidate?: boolean;
    identification_reason?: string;
    identification?: Identification;
  };
}

const SEVERITY_RANK: Record<Alert["severity"], number> = { info: 0, warning: 1, critical: 2 };

// The more severe of two, for an aircraft in several alerts at once.
export function worse(a: Alert["severity"] | undefined, b: Alert["severity"]): Alert["severity"] {
  return a !== undefined && SEVERITY_RANK[a] >= SEVERITY_RANK[b] ? a : b;
}

export function bySeverity(a: Alert, b: Alert): number {
  return SEVERITY_RANK[b.severity] - SEVERITY_RANK[a.severity];
}

// U-15. What one adapter says about its sources, every few seconds
// (gateway/source_activity.py, on `source.<type>`). Times are the adapter's.
export type DisabledBy = "type" | "instance" | "default_deny";

export interface SourceInstanceActivity {
  instance_id: string;
  enabled: boolean;
  disabled_by: DisabledBy | null;
  last_seen_at: string | null;
  accepted: number;
  refused_disabled: number;
  last_refused_at: string | null;
  // Null for an adapter without connections (Remote ID receivers).
  connected: boolean | null;
}

export interface SourceActivity {
  source_type: string;
  enabled: boolean;
  control_version: number;
  published_at: string;
  instances: SourceInstanceActivity[];
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
  | { kind: "events"; name: string; data: Omit<Unclaimed, "rejected"> }
  | { kind: "source"; name: string; data: SourceActivity };

export interface SourceReport {
  data: SourceActivity;
  // Browser clock when it arrived: an adapter that stopped reporting is stale.
  receivedAt: number;
}

export interface Aircraft {
  data: Telemetry;
  // Browser clock when the last message arrived: link age is measured here,
  // because the capture time is the station's clock.
  receivedAt: number;
  // Recent battery and altitude, for the sparklines. Bounded.
  history: { t: number; batt: number | null; alt: number | null }[];
}

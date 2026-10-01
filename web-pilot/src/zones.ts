// Geographical zones (U-03) as the console draws and edits them. Types come
// from the generated schema; a zone is sent as an ED-269 zone, exactly what
// an export contains. The API checks everything again with the same strict
// reader as an import; the checks here only spare a round trip and say which
// field to fix.
import type { components } from "./api/schema";

export type ZoneOut = components["schemas"]["ZoneOut"];
export type Ed269Zone = components["schemas"]["Ed269Zone"];
export type Restriction = components["schemas"]["Restriction"];
export type Reason = components["schemas"]["Reason"];
export type VerticalReference = components["schemas"]["VerticalReference"];
export type Uom = components["schemas"]["Uom"];
export type Purpose = components["schemas"]["Purpose"];
export type ImportReport = components["schemas"]["ImportReportOut"];
type Day = components["schemas"]["DailyPeriod"]["day"][number];
export type Weekday = Exclude<Day, "ANY">;

export const RESTRICTIONS: readonly Restriction[] = [
  "PROHIBITED",
  "REQ_AUTHORISATION",
  "CONDITIONAL",
  "NO_RESTRICTION",
];
export const REASONS: readonly Reason[] = [
  "AIR_TRAFFIC",
  "SENSITIVE",
  "PRIVACY",
  "POPULATION",
  "NATURE",
  "NOISE",
  "FOREIGN_TERRITORY",
  "EMERGENCY",
  "OTHER",
];
export const REFERENCES: readonly VerticalReference[] = ["AGL", "AMSL", "WGS84"];
export const PURPOSES: readonly Purpose[] = ["AUTHORIZATION", "NOTIFICATION", "INFORMATION"];
export const DAYS: readonly Weekday[] = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"];

// [longitude, latitude], as GeoJSON and ED-269 write positions.
export type LonLat = [number, number];

export type Shape =
  // The outer ring open, as drawn; holes as published (not drawn here).
  | { kind: "polygon"; points: LonLat[]; holes?: LonLat[][] }
  | { kind: "circle"; center: LonLat; radius: number };

// The editor's fields, as typed. Times are UTC: the inputs say so.
export interface ZoneForm {
  identifier: string;
  country: string;
  name: string;
  restriction: Restriction;
  reasons: Reason[];
  message: string;
  uom: Uom;
  lowerLimit: string;
  lowerReference: VerticalReference;
  upperLimit: string;
  upperReference: VerticalReference;
  permanent: boolean;
  // "YYYY-MM-DDTHH:MM", as a datetime-local input gives it; read as UTC.
  start: string;
  end: string;
  scheduleDays: Weekday[];
  // "HH:MM", UTC.
  scheduleStart: string;
  scheduleEnd: string;
  authorityName: string;
  authorityContact: string;
  authorityEmail: string;
  authorityPhone: string;
  authorityPurpose: Purpose | "";
  // Published fields the editor does not show, kept as they were.
  kept: Partial<Ed269Zone>;
}

export type FormErrorKey =
  | "zone_error_identifier"
  | "zone_error_country"
  | "zone_error_number"
  | "zone_error_band"
  | "zone_error_no_shape"
  | "zone_error_polygon"
  | "zone_error_self_intersecting"
  | "zone_error_radius"
  | "zone_error_window"
  | "zone_error_schedule"
  | "zone_error_never";

export interface FormError {
  // What is wrong (an i18n key), and the form field it is about.
  key: FormErrorKey;
  field: string;
}

export function emptyForm(country: string): ZoneForm {
  return {
    identifier: "",
    country,
    name: "",
    restriction: "PROHIBITED",
    reasons: [],
    message: "",
    uom: "M",
    lowerLimit: "0",
    lowerReference: "AGL",
    upperLimit: "120",
    upperReference: "AGL",
    permanent: true,
    start: "",
    end: "",
    scheduleDays: [],
    scheduleStart: "",
    scheduleEnd: "",
    authorityName: "",
    authorityContact: "",
    authorityEmail: "",
    authorityPhone: "",
    authorityPurpose: "",
    kept: {},
  };
}

// A UTC timestamp as a datetime-local value ("2026-10-01T10:00"), or "".
function localValue(iso: string | null | undefined): string {
  if (!iso) return "";
  const at = new Date(iso);
  return Number.isNaN(at.getTime()) ? "" : at.toISOString().slice(0, 16);
}

function clockValue(time: string | undefined): string {
  return time ? time.slice(0, 5) : "";
}

// The form for an existing zone. What the editor cannot show (a second
// period, a second authority, the published extras) is kept and sent back.
export function formFromFeature(feature: Ed269Zone): { form: ZoneForm; shape: Shape } {
  const volume = feature.geometry[0];
  const period = feature.applicability[0];
  const daily = period?.schedule?.[0];
  const authority = feature.zoneAuthority[0];
  const {
    identifier,
    country,
    name,
    restriction,
    reason,
    message,
    geometry,
    applicability,
    zoneAuthority,
    type,
    ...extras
  } = feature;
  void geometry;
  const kept: Partial<Ed269Zone> = { ...extras, type };
  if (applicability.length > 1) kept.applicability = applicability;
  if (zoneAuthority.length > 1) kept.zoneAuthority = zoneAuthority;
  const projection = volume?.horizontalProjection;
  const shape: Shape =
    projection?.type === "Circle"
      ? {
          kind: "circle",
          center: [projection.center[0] ?? 0, projection.center[1] ?? 0],
          radius: projection.radius,
        }
      : {
          kind: "polygon",
          points: (projection?.coordinates[0] ?? [])
            .slice(0, -1)
            .map((p) => [p[0] ?? 0, p[1] ?? 0] as LonLat),
          holes: (projection?.coordinates.slice(1) ?? []).map((ring) =>
            ring.map((p) => [p[0] ?? 0, p[1] ?? 0] as LonLat),
          ),
        };
  return {
    shape,
    form: {
      identifier,
      country,
      name: name ?? "",
      restriction,
      reasons: reason ?? [],
      message: message ?? "",
      uom: volume?.uomDimensions ?? "M",
      lowerLimit: volume?.lowerLimit == null ? "" : String(volume.lowerLimit),
      lowerReference: volume?.lowerVerticalReference ?? "AGL",
      upperLimit: volume?.upperLimit == null ? "" : String(volume.upperLimit),
      upperReference: volume?.upperVerticalReference ?? "AGL",
      permanent: period?.permanent !== "NO",
      start: localValue(period?.startDateTime),
      end: localValue(period?.endDateTime),
      scheduleDays: (daily?.day ?? []).flatMap((d) => (d === "ANY" ? [...DAYS] : [d])),
      scheduleStart: clockValue(daily?.startTime),
      scheduleEnd: clockValue(daily?.endTime),
      authorityName: authority?.name ?? "",
      authorityContact: authority?.contactName ?? "",
      authorityEmail: authority?.email ?? "",
      authorityPhone: authority?.phone ?? "",
      authorityPurpose: authority?.purpose ?? "",
      kept,
    },
  };
}

function limit(text: string): number | null | undefined {
  const trimmed = text.trim();
  if (!trimmed) return null;
  const value = Number(trimmed);
  return Number.isFinite(value) ? value : undefined;
}

// The ED-269 zone the form and the drawn shape describe, or what is wrong.
export function featureFromForm(
  form: ZoneForm,
  shape: Shape | null,
): { feature: Ed269Zone; errors: [] } | { feature: null; errors: FormError[] } {
  const errors: FormError[] = [];
  const identifier = form.identifier.trim();
  if (identifier.length < 1 || identifier.length > 7) {
    errors.push({ key: "zone_error_identifier", field: "identifier" });
  }
  const country = form.country.trim();
  if (!/^[A-Z]{3}$/.test(country)) errors.push({ key: "zone_error_country", field: "country" });
  const lower = limit(form.lowerLimit);
  const upper = limit(form.upperLimit);
  if (lower === undefined) errors.push({ key: "zone_error_number", field: "lowerLimit" });
  if (upper === undefined) errors.push({ key: "zone_error_number", field: "upperLimit" });
  if (
    lower != null &&
    upper != null &&
    form.lowerReference === form.upperReference &&
    !(lower < upper)
  ) {
    errors.push({ key: "zone_error_band", field: "upperLimit" });
  }
  if (!shape) {
    errors.push({ key: "zone_error_no_shape", field: "geometry" });
  } else if (shape.kind === "polygon" && distinct(shape.points) < 3) {
    errors.push({ key: "zone_error_polygon", field: "geometry" });
  } else if (shape.kind === "polygon" && !simpleRing(shape.points)) {
    errors.push({ key: "zone_error_self_intersecting", field: "geometry" });
  } else if (shape.kind === "circle" && !(shape.radius > 0)) {
    errors.push({ key: "zone_error_radius", field: "geometry" });
  }
  const period = applicability(form, errors);
  if (errors.length > 0 || !shape) return { feature: null, errors };

  const volume: Ed269Zone["geometry"][number] = {
    uomDimensions: form.uom,
    lowerVerticalReference: form.lowerReference,
    upperVerticalReference: form.upperReference,
    horizontalProjection:
      shape.kind === "circle"
        ? { type: "Circle", center: shape.center, radius: shape.radius }
        : { type: "Polygon", coordinates: [closeRing(shape.points), ...(shape.holes ?? [])] },
  };
  if (lower != null) volume.lowerLimit = lower;
  if (upper != null) volume.upperLimit = upper;

  const authority: NonNullable<Ed269Zone["zoneAuthority"]>[number] = {};
  if (form.authorityName.trim()) authority.name = form.authorityName.trim();
  if (form.authorityContact.trim()) authority.contactName = form.authorityContact.trim();
  if (form.authorityEmail.trim()) authority.email = form.authorityEmail.trim();
  if (form.authorityPhone.trim()) authority.phone = form.authorityPhone.trim();
  if (form.authorityPurpose) authority.purpose = form.authorityPurpose;

  const feature: Ed269Zone = {
    type: "COMMON",
    ...form.kept,
    identifier,
    country,
    restriction: form.restriction,
    applicability: form.kept.applicability ?? [period],
    zoneAuthority:
      form.kept.zoneAuthority ?? (Object.keys(authority).length > 0 ? [authority] : []),
    geometry: [volume],
  };
  if (form.name.trim()) feature.name = form.name.trim();
  else delete feature.name;
  if (form.reasons.length > 0) feature.reason = [...form.reasons];
  else delete feature.reason;
  if (form.message.trim()) feature.message = form.message.trim();
  else delete feature.message;
  return { feature, errors: [] };
}

function applicability(form: ZoneForm, errors: FormError[]): Ed269Zone["applicability"][number] {
  if (form.permanent) return { permanent: "YES" };
  const period: Ed269Zone["applicability"][number] = { permanent: "NO" };
  if (form.start) period.startDateTime = `${form.start}:00Z`;
  if (form.end) period.endDateTime = `${form.end}:00Z`;
  if (form.start && form.end && !(form.start < form.end)) {
    errors.push({ key: "zone_error_window", field: "end" });
  }
  const anySchedule = form.scheduleDays.length > 0 || form.scheduleStart || form.scheduleEnd;
  if (anySchedule) {
    if (form.scheduleDays.length === 0 || !form.scheduleStart || !form.scheduleEnd) {
      errors.push({ key: "zone_error_schedule", field: "schedule" });
    } else if (form.scheduleStart === form.scheduleEnd) {
      errors.push({ key: "zone_error_schedule", field: "schedule" });
    } else {
      period.schedule = [
        {
          day:
            form.scheduleDays.length === 7
              ? ["ANY"]
              : DAYS.filter((d) => form.scheduleDays.includes(d)),
          startTime: `${form.scheduleStart}Z`,
          endTime: `${form.scheduleEnd}Z`,
        },
      ];
    }
  }
  if (!form.start && !form.end && !anySchedule) {
    errors.push({ key: "zone_error_never", field: "applicability" });
  }
  return period;
}

// Whether an open ring (as drawn) encloses an area without crossing itself:
// no zero area (collinear corners) and no two non-adjacent edges touching (a
// bow tie). The API checks again with PostGIS; this says so before a save.
export function simpleRing(points: LonLat[]): boolean {
  const ring = closeRing(points);
  const n = ring.length - 1;
  if (n < 3) return false;
  let twiceArea = 0;
  for (let i = 0; i < n; i++) {
    const [x1, y1] = ring[i] as LonLat;
    const [x2, y2] = ring[i + 1] as LonLat;
    twiceArea += x1 * y2 - x2 * y1;
  }
  if (Math.abs(twiceArea) < 1e-14) return false;
  for (let i = 0; i < n; i++) {
    for (let j = i + 1; j < n; j++) {
      // Edges sharing a corner touch there by construction.
      if (j === i + 1 || (i === 0 && j === n - 1)) continue;
      const [a, b, c, d] = [ring[i], ring[i + 1], ring[j], ring[j + 1]];
      if (a && b && c && d && segmentsMeet(a, b, c, d)) return false;
    }
  }
  return true;
}

function orientation(a: LonLat, b: LonLat, c: LonLat): number {
  const value = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0]);
  return value > 0 ? 1 : value < 0 ? -1 : 0;
}

function onSegment(a: LonLat, b: LonLat, p: LonLat): boolean {
  return (
    Math.min(a[0], b[0]) <= p[0] &&
    p[0] <= Math.max(a[0], b[0]) &&
    Math.min(a[1], b[1]) <= p[1] &&
    p[1] <= Math.max(a[1], b[1])
  );
}

function segmentsMeet(p1: LonLat, p2: LonLat, q1: LonLat, q2: LonLat): boolean {
  const o1 = orientation(p1, p2, q1);
  const o2 = orientation(p1, p2, q2);
  const o3 = orientation(q1, q2, p1);
  const o4 = orientation(q1, q2, p2);
  if (o1 !== o2 && o3 !== o4) return true;
  return (
    (o1 === 0 && onSegment(p1, p2, q1)) ||
    (o2 === 0 && onSegment(p1, p2, q2)) ||
    (o3 === 0 && onSegment(q1, q2, p1)) ||
    (o4 === 0 && onSegment(q1, q2, p2))
  );
}

function distinct(points: LonLat[]): number {
  return new Set(points.map((p) => `${p[0]},${p[1]}`)).size;
}

// A ring closed as GeoJSON and ED-269 require: the last position repeats the first.
export function closeRing(points: LonLat[]): LonLat[] {
  const first = points[0];
  const last = points[points.length - 1];
  if (!first || !last) return [];
  return first[0] === last[0] && first[1] === last[1] ? [...points] : [...points, first];
}

const EARTH_RADIUS_M = 6_371_008.8;
const FEET_M = 0.3048;

export function toMetres(value: number, uom: Uom): number {
  return uom === "FT" ? value * FEET_M : value;
}

// Great-circle distance in metres, for a circle's radius from two clicks.
export function distanceM(a: LonLat, b: LonLat): number {
  const rad = Math.PI / 180;
  const dLat = (b[1] - a[1]) * rad;
  const dLon = (b[0] - a[0]) * rad;
  const h =
    Math.sin(dLat / 2) ** 2 + Math.cos(a[1] * rad) * Math.cos(b[1] * rad) * Math.sin(dLon / 2) ** 2;
  return 2 * EARTH_RADIUS_M * Math.asin(Math.min(1, Math.sqrt(h)));
}

// A circle drawn as a closed polygon of `sides` vertices on it, for the
// preview. The stored zone keeps the circle itself.
export function circleRing(center: LonLat, radiusM: number, sides = 64): LonLat[] {
  const rad = Math.PI / 180;
  const lat1 = center[1] * rad;
  const lon1 = center[0] * rad;
  const d = radiusM / EARTH_RADIUS_M;
  const ring: LonLat[] = [];
  for (let k = 0; k < sides; k++) {
    const bearing = (2 * Math.PI * k) / sides;
    const lat2 = Math.asin(
      Math.sin(lat1) * Math.cos(d) + Math.cos(lat1) * Math.sin(d) * Math.cos(bearing),
    );
    const lon2 =
      lon1 +
      Math.atan2(
        Math.sin(bearing) * Math.sin(d) * Math.cos(lat1),
        Math.cos(d) - Math.sin(lat1) * Math.sin(lat2),
      );
    ring.push([lon2 / rad, lat2 / rad]);
  }
  return closeRing(ring);
}

// What to draw for a shape being edited: its outline once it encloses
// anything, and the corners (or the circle's centre) as points.
export function draftGeometry(
  shape: Shape | null,
  uom: Uom,
  circleCenter: LonLat | null = null,
): { ring: LonLat[] | null; line: LonLat[] | null; points: LonLat[] } {
  if (!shape) return { ring: null, line: null, points: circleCenter ? [circleCenter] : [] };
  if (shape.kind === "circle") {
    const radiusM = toMetres(shape.radius, uom);
    return {
      ring: radiusM > 0 ? circleRing(shape.center, radiusM) : null,
      line: null,
      points: [shape.center],
    };
  }
  const points = shape.points;
  return {
    ring: distinct(points) >= 3 ? closeRing(points) : null,
    line: points.length === 2 ? points : null,
    points,
  };
}

// Colours by restriction: not flight data, a display choice. Corridors and
// bases keep their own.
const RESTRICTION_COLOURS: Record<Restriction, string> = {
  PROHIBITED: "#c62828",
  REQ_AUTHORISATION: "#ef6c00",
  CONDITIONAL: "#f9a825",
  NO_RESTRICTION: "#2e7d32",
};
const TYPE_COLOURS: Record<string, string> = { corridor: "#2e7d32", base: "#1565c0" };

export interface ZoneStyle {
  colour: string;
  fillOpacity: number;
  // An inactive zone is drawn dashed and faint: it is there, and not alerted on now.
  dashed: boolean;
}

export function zoneStyle(zone: Pick<ZoneOut, "type" | "active_now" | "feature">): ZoneStyle {
  const colour =
    zone.type === "geozone"
      ? RESTRICTION_COLOURS[zone.feature.restriction]
      : (TYPE_COLOURS[zone.type] ?? "#616161");
  return zone.active_now
    ? { colour, fillOpacity: 0.18, dashed: false }
    : { colour, fillOpacity: 0.04, dashed: true };
}

// What a refusal from the API says, one line per problem: the zone routes'
// `{code, message, problems: [{field, reason}]}`, the request model's
// `[{loc, msg}]`, or a plain message.
export function refusalLines(body: unknown): string[] {
  const detail = (body as { detail?: unknown } | null)?.detail;
  if (typeof detail === "string") return [detail];
  if (Array.isArray(detail)) {
    return detail.map(
      (item: { loc?: unknown[]; msg?: string }) =>
        `${(item.loc ?? []).filter((part) => part !== "body").join(".")}: ${item.msg ?? ""}`,
    );
  }
  if (detail && typeof detail === "object") {
    const { problems, message, more } = detail as {
      problems?: { field: string; reason: string }[];
      message?: string;
      more?: number;
    };
    if (problems && problems.length > 0) {
      const lines = problems.map((p) => `${p.field}: ${p.reason}`);
      return more ? [...lines, `+${more}`] : lines;
    }
    if (message) return [message];
  }
  return [];
}

// When a zone applies, for its detail: the period's parts, untranslated
// values only (dates, days, times); the caller adds the words.
export function periodParts(period: Ed269Zone["applicability"][number]): {
  permanent: boolean;
  from: string | null;
  until: string | null;
  schedule: { days: string; times: string }[];
} {
  return {
    permanent: period.permanent === "YES",
    from: period.startDateTime ?? null,
    until: period.endDateTime ?? null,
    schedule: (period.schedule ?? []).map((daily) => ({
      days: daily.day.join(" "),
      times: `${daily.startTime}–${daily.endTime}`,
    })),
  };
}

// The zone's vertical extent in words-free form: "0 m AGL – 120 m AGL".
export function limitsText(
  volume: Ed269Zone["geometry"][number] | undefined,
  surface: string,
  unlimited: string,
): string {
  if (!volume) return "";
  const unit = volume.uomDimensions === "FT" ? "ft" : "m";
  const lower =
    volume.lowerLimit == null
      ? surface
      : `${volume.lowerLimit} ${unit} ${volume.lowerVerticalReference}`;
  const upper =
    volume.upperLimit == null
      ? unlimited
      : `${volume.upperLimit} ${unit} ${volume.upperVerticalReference}`;
  return `${lower} – ${upper}`;
}

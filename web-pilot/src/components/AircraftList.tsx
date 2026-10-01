import { ageSeconds, num, shortId } from "../format";
import { useT } from "../i18n";
import { type Aircraft, type Alert, worse } from "../types";

interface Props {
  aircraft: Map<string, Aircraft>;
  alerts: Map<string, Alert>;
  selected: string | null;
  now: number;
  onSelect: (droneId: string) => void;
}

// The registry label leads and the id follows: a pilot knows "SITL-01", and
// the id is what everything else is keyed on.
export function sortedAircraft(aircraft: Map<string, Aircraft>): [string, Aircraft][] {
  return [...aircraft.entries()].sort(([a, x], [b, y]) =>
    (x.data.label ?? `~${a}`).localeCompare(y.data.label ?? `~${b}`),
  );
}

export function AircraftList({ aircraft, alerts, selected, now, onSelect }: Props) {
  const t = useT();
  if (aircraft.size === 0) return <p className="muted pad">{t("none")}</p>;
  const severity = new Map<string, Alert["severity"]>();
  for (const alert of alerts.values()) {
    for (const id of alert.drone_ids) {
      severity.set(id, worse(severity.get(id), alert.severity));
    }
  }
  return (
    <ul className="list">
      {sortedAircraft(aircraft).map(([id, item]) => {
        const d = item.data;
        const placed = d.lat_deg !== null && d.lon_deg !== null;
        const alert = severity.get(id);
        return (
          <li key={id}>
            <button
              type="button"
              className={`row${id === selected ? " selected" : ""}${alert ? ` alert-${alert}` : ""}`}
              onClick={() => onSelect(id)}
            >
              <div className="row-head">
                <strong>{d.label ?? <span className="muted">{t("unnamed")}</span>}</strong>
                <span className="muted mono small">{shortId(id)}</span>
              </div>
              <div className="row-body small">
                {d.source === "remote_id" ? (
                  <>
                    <span className="pill remote-id" title={t("remote_id_unverified")}>
                      {t("remote_id")}
                    </span>
                    <span>{d.airborne ? t("airborne") : t("on_ground")}</span>
                  </>
                ) : (
                  <>
                    <span className={`pill ${d.armed ? "armed" : ""}`}>
                      {d.armed === null ? t("unknown") : d.armed ? t("armed") : t("disarmed")}
                    </span>
                    <span>{d.mode ?? "—"}</span>
                    <span title={t("battery")}>{num(d.batt_pct, 0, "%")}</span>
                  </>
                )}
                <span title={t("altitude_home")}>{num(d.alt_above_home_m, 0, "m")}</span>
                <span className="muted">
                  {t("seconds_ago", { n: ageSeconds(item.receivedAt, now) })}
                </span>
                {!placed && <span className="pill unplaced">{t("no_position")}</span>}
              </div>
            </button>
          </li>
        );
      })}
    </ul>
  );
}

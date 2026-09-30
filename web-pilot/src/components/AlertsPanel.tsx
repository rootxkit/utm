// P6-03. Active airspace alerts. Acknowledging silences the tone on this
// console only; it does not clear the alert, which only the airspace monitor
// does when the condition is gone, and it is not yet recorded (P6-07).
import { DASH, num, shortId } from "../format";
import { useT } from "../i18n";
import type { Aircraft, Alert } from "../types";

interface Props {
  alerts: Map<string, Alert>;
  // To mark an aircraft whose position is only a Remote ID broadcast (P1-15).
  aircraft: Map<string, Aircraft>;
  acknowledged: Set<string>;
  canAcknowledge: boolean;
  onAcknowledge: (key: string) => void;
  onSelect: (droneId: string) => void;
}

function name(alert: Alert, index: number): string {
  return alert.labels[index] ?? shortId(alert.drone_ids[index] ?? DASH);
}

export function AlertsPanel({
  alerts,
  aircraft,
  acknowledged,
  canAcknowledge,
  onAcknowledge,
  onSelect,
}: Props) {
  const t = useT();
  // A party known only from a Remote ID broadcast is marked where the alert
  // names it: the alert is about a claimed position.
  const broadcastOnly = (droneId: string | undefined) =>
    droneId && aircraft.get(droneId)?.data.source === "remote_id" ? (
      <span className="pill remote-id" title={t("remote_id_unverified")}>
        {t("remote_id")}
      </span>
    ) : null;
  if (alerts.size === 0) return <p className="muted pad">{t("none")}</p>;
  const ordered = [...alerts.values()].sort(
    (a, b) => Number(b.severity === "critical") - Number(a.severity === "critical"),
  );
  return (
    <ul className="list">
      {ordered.map((alert) => {
        const d = alert.detail;
        const acked = acknowledged.has(alert.key);
        return (
          <li key={alert.key} className={`alert alert-${alert.severity}${acked ? " acked" : ""}`}>
            <div className="row-head">
              <strong>{t(alert.severity)}</strong>
              {acked && <span className="muted small">{t("acknowledged")}</span>}
            </div>
            {alert.kind === "conflict" ? (
              <>
                <div>
                  <button
                    type="button"
                    className="link"
                    onClick={() => onSelect(alert.drone_ids[0] ?? "")}
                  >
                    {name(alert, 0)}
                  </button>
                  {broadcastOnly(alert.drone_ids[0])}
                  {" ↔ "}
                  <button
                    type="button"
                    className="link"
                    onClick={() => onSelect(alert.drone_ids[1] ?? "")}
                  >
                    {name(alert, 1)}
                  </button>
                  {broadcastOnly(alert.drone_ids[1])}
                </div>
                <div className="small muted">
                  {t("conflict_detail", {
                    d: num(d.d_cpa_horizontal_m, 0),
                    t: num(d.t_cpa_s, 0),
                    v: num(d.d_alt_at_cpa_m, 0),
                  })}
                </div>
              </>
            ) : alert.kind === "height" ? (
              <>
                <div>
                  <button
                    type="button"
                    className="link"
                    onClick={() => onSelect(alert.drone_ids[0] ?? "")}
                  >
                    {name(alert, 0)}
                  </button>
                  {broadcastOnly(alert.drone_ids[0])} {t("above_height_limit")}
                </div>
                <div className="small muted">
                  {t("height_detail", {
                    h: num(d.height_agl_m, 0),
                    limit: num(d.max_height_agl_m, 0),
                    g: num(d.ground_elevation_m, 0),
                    dataset: d.dataset ?? DASH,
                  })}
                </div>
              </>
            ) : (
              <>
                <div>
                  <button
                    type="button"
                    className="link"
                    onClick={() => onSelect(alert.drone_ids[0] ?? "")}
                  >
                    {name(alert, 0)}
                  </button>
                  {broadcastOnly(alert.drone_ids[0])} {t("in_zone", { zone: d.zone_name ?? DASH })}
                </div>
                <div className="small muted">
                  {d.zone_type === "no_fly" ||
                  d.zone_type === "restricted" ||
                  d.zone_type === "corridor"
                    ? t(d.zone_type)
                    : (d.zone_type ?? "")}
                  {d.alt_amsl_m !== undefined && ` · ${num(d.alt_amsl_m, 0, "m AMSL")}`}
                </div>
              </>
            )}
            {!acked &&
              (canAcknowledge ? (
                <button
                  type="button"
                  className="button small"
                  onClick={() => onAcknowledge(alert.key)}
                >
                  {t("acknowledge")}
                </button>
              ) : (
                <div className="small muted">{t("viewer_cannot_ack")}</div>
              ))}
          </li>
        );
      })}
    </ul>
  );
}

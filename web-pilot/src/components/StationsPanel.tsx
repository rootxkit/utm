// Ground stations and what they know about their links (p1-02 spec §9).
import { num } from "../format";
import { useT } from "../i18n";
import type { Station, Unclaimed } from "../types";

export function StationsPanel({ stations }: { stations: Map<string, Station> }) {
  const t = useT();
  if (stations.size === 0) return <p className="muted pad">{t("none")}</p>;
  return (
    <ul className="list">
      {[...stations.values()]
        .sort((a, b) => a.station_id.localeCompare(b.station_id))
        .map((s) => (
          <li key={s.station_id} className={`station state-${s.state}`}>
            <div className="row-head">
              <strong className="mono">{s.station_id}</strong>
              <span className={`pill state-${s.state}`}>{t(s.state)}</span>
            </div>
            <div className="small muted">
              {s.lag_s !== null && s.lag_s > 0 && (
                <span>{t("behind", { n: num(s.lag_s, 0) })} · </span>
              )}
              {s.queue_depth !== null && <span>{t("queue", { n: s.queue_depth })} · </span>}
              {s.last_datagram_age_ms !== null && (
                <span>{t("seconds_ago", { n: num(s.last_datagram_age_ms / 1000, 0) })}</span>
              )}
            </div>
            {s.losses.length > 0 && (
              <ul className="small">
                {s.losses.map((loss, index) => (
                  <li key={index}>
                    {loss.kind}
                    {loss.datagram_count !== null && ` · ${loss.datagram_count}`} — {loss.detail}
                  </li>
                ))}
              </ul>
            )}
          </li>
        ))}
    </ul>
  );
}

export function UnclaimedPanel({ unclaimed }: { unclaimed: Map<string, Unclaimed> }) {
  const t = useT();
  if (unclaimed.size === 0) return <p className="muted pad">{t("none")}</p>;
  return (
    <ul className="list">
      {[...unclaimed.entries()].map(([key, u]) => (
        <li key={key}>
          <div className="mono">
            {u.station_id} · sysid {u.sysid} · compid {u.compid}
          </div>
          <div className="small muted">{u.rejected ? t("rejected") : t("no_binding")}</div>
        </li>
      ))}
    </ul>
  );
}

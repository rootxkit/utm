// U-03. The zones list: every zone styled by restriction and marked active or
// inactive now, the ED-269 export for everyone, and for those who may change
// zones a new zone and an ED-269 import, checked by a dry run first.
import { useState } from "react";
import { apiSend } from "../api/client";
import { useT } from "../i18n";
import { type ImportReport, type ZoneOut, refusalLines, zoneStyle } from "../zones";

interface Props {
  zones: ZoneOut[];
  selected: string | null;
  canWrite: boolean;
  onSelect: (zoneId: string) => void;
  onNew: () => void;
  onChanged: () => void;
}

interface Pending {
  name: string;
  text: string;
  report: ImportReport | null;
  refused: string[];
}

export function ZonesPanel({ zones, selected, canWrite, onSelect, onNew, onChanged }: Props) {
  const t = useT();
  const [pending, setPending] = useState<Pending | null>(null);
  const [busy, setBusy] = useState(false);

  const send = async (text: string, dryRun: boolean) => {
    setBusy(true);
    try {
      const response = await apiSend(
        "POST",
        `/airspace/zones/import${dryRun ? "?dry_run=true" : ""}`,
        text,
      );
      const body: unknown = await response.json().catch(() => null);
      return response.ok
        ? { report: body as ImportReport, refused: [] }
        : { report: null, refused: refusalLines(body) };
    } finally {
      setBusy(false);
    }
  };

  const check = async (file: File) => {
    const text = await file.text();
    const result = await send(text, true);
    setPending({ name: file.name, text, ...result });
  };

  const apply = async () => {
    if (!pending) return;
    const result = await send(pending.text, false);
    if (result.report) {
      setPending(null);
      onChanged();
    } else {
      setPending({ ...pending, ...result });
    }
  };

  return (
    <div className="zones-panel">
      <div className="zone-actions">
        {canWrite && (
          <button type="button" className="button small" onClick={onNew}>
            {t("zone_new")}
          </button>
        )}
        <a className="button small" href="/airspace/zones/export" download>
          {t("zone_export")}
        </a>
        {canWrite && (
          <label className="button small file">
            {t("zone_import")}
            <input
              type="file"
              accept=".json,application/json"
              onChange={(event) => {
                const file = event.target.files?.[0];
                event.target.value = "";
                if (file) void check(file);
              }}
            />
          </label>
        )}
      </div>
      {pending && (
        <div className="zone-import">
          <div className="small">{pending.name}</div>
          {pending.report ? (
            <>
              <div>
                {t("zone_import_report", {
                  created: pending.report.created.length,
                  updated: pending.report.updated.length,
                  unchanged: pending.report.unchanged.length,
                })}
              </div>
              <button
                type="button"
                className="button small"
                disabled={busy}
                onClick={() => void apply()}
              >
                {t("zone_import_apply")}
              </button>
            </>
          ) : (
            <>
              <div className="error">{t("zone_import_refused")}</div>
              <ul className="problems small">
                {pending.refused.map((line) => (
                  <li key={line}>{line}</li>
                ))}
              </ul>
            </>
          )}
          <button type="button" className="link small" onClick={() => setPending(null)}>
            {t("close")}
          </button>
        </div>
      )}
      {zones.length === 0 ? (
        <p className="muted pad">{t("none")}</p>
      ) : (
        <ul className="list">
          {zones.map((zone) => {
            const style = zoneStyle(zone);
            const kind =
              zone.type === "geozone"
                ? t(`restriction_${zone.feature.restriction}`)
                : t(zone.type === "base" ? "base" : "corridor");
            return (
              <li key={zone.id}>
                <button
                  type="button"
                  className={`row${zone.id === selected ? " selected" : ""}`}
                  onClick={() => onSelect(zone.id)}
                >
                  <div className="row-head">
                    <span>
                      <span
                        className={`swatch${style.dashed ? " dashed" : ""}`}
                        style={{ borderColor: style.colour }}
                      />
                      <strong>{zone.feature.identifier}</strong> {zone.feature.name ?? ""}
                    </span>
                    <span className={`pill ${zone.active_now ? "active" : "inactive"}`}>
                      {t(zone.active_now ? "zone_active" : "zone_inactive")}
                    </span>
                  </div>
                  <div className="small muted">{kind}</div>
                </button>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}

// U-15. Every source, its switch and what it is doing. An admin switches a
// type or one instance with a reason; everyone else sees, and cannot switch.
import { useState } from "react";
import { ageSeconds } from "../format";
import { useT } from "../i18n";
import type { SourceGroup, SourceRow } from "../sources";

export type SwitchSource = (
  sourceType: string,
  instanceId: string | null,
  enabled: boolean,
  reason: string,
) => Promise<string | null>;

interface Props {
  groups: SourceGroup[];
  isAdmin: boolean;
  // The switches could not be read: the states shown may be out of date.
  controlsFailed: boolean;
  now: number;
  onSwitch: SwitchSource;
}

function typeName(t: ReturnType<typeof useT>, sourceType: string): string {
  switch (sourceType) {
    case "relay":
      return t("source_type_relay");
    case "remote_id":
      return t("source_type_remote_id");
    case "network_remote_id":
      return t("source_type_network_remote_id");
    case "adsb":
      return t("source_type_adsb");
    case "sensor":
      return t("source_type_sensor");
    default:
      return sourceType;
  }
}

function stateText(t: ReturnType<typeof useT>, row: SourceRow): string {
  switch (row.state) {
    case "disabled":
      return row.disabledBy === "type"
        ? t("source_disabled_by_type")
        : row.disabledBy === "default_deny"
          ? t("source_disabled_by_default")
          : t("source_disabled");
    case "healthy":
      return t("source_healthy");
    case "stale":
      return t("source_stale");
    default:
      return t("source_enabled");
  }
}

function SwitchControl({ row, onSwitch }: { row: SourceRow; onSwitch: SwitchSource }) {
  const t = useT();
  const [open, setOpen] = useState(false);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [refused, setRefused] = useState<string | null>(null);
  // A row switched off by its type is switched on at the type, not here.
  const enable = row.instanceId === null ? row.state === "disabled" : row.disabledBy !== null;
  if (row.instanceId !== null && row.disabledBy === "type") return null;

  const submit = async () => {
    setBusy(true);
    const code = await onSwitch(row.sourceType, row.instanceId, enable, reason);
    setBusy(false);
    if (code === null) {
      setOpen(false);
      setReason("");
      setRefused(null);
    } else {
      setRefused(code);
    }
  };

  if (!open) {
    return (
      <button type="button" className="button small" onClick={() => setOpen(true)}>
        {enable ? t("source_enable") : t("source_disable")}
      </button>
    );
  }
  return (
    <form
      className="source-switch"
      onSubmit={(event) => {
        event.preventDefault();
        void submit();
      }}
    >
      <input
        type="text"
        value={reason}
        maxLength={500}
        placeholder={t("source_reason")}
        aria-label={t("source_reason")}
        onChange={(event) => setReason(event.target.value)}
        autoFocus
      />
      <button type="submit" className="button small" disabled={busy || reason.trim() === ""}>
        {enable ? t("source_enable") : t("source_disable")}
      </button>
      <button type="button" className="button small" onClick={() => setOpen(false)}>
        {t("cancel")}
      </button>
      {refused && <span className="small muted">{t("reg_refused", { code: refused })}</span>}
    </form>
  );
}

function Row({
  row,
  isAdmin,
  now,
  onSwitch,
}: {
  row: SourceRow;
  isAdmin: boolean;
  now: number;
  onSwitch: SwitchSource;
}) {
  const t = useT();
  return (
    <li className={`source source-${row.state}`}>
      <div className="row-head">
        <strong className={row.instanceId === null ? "" : "mono"}>
          {row.instanceId ?? t("source_all", { type: typeName(t, row.sourceType) })}
        </strong>
        <span className={`pill source-${row.state}`}>{stateText(t, row)}</span>
      </div>
      <div className="small muted">
        {row.lastSeenMs !== null ? (
          <span>
            {t("last_seen")}: {t("seconds_ago", { n: ageSeconds(row.lastSeenMs, now) })}
          </span>
        ) : (
          row.instanceId !== null && <span>{t("source_never_heard")}</span>
        )}
        {row.stationState && <span> · {t(row.stationState)}</span>}
        {row.refused > 0 && <span> · {t("source_refused", { n: row.refused })}</span>}
      </div>
      {row.control && (
        <div className="small muted">
          {row.control.enabled ? t("source_enabled_by") : t("source_disabled_by")}{" "}
          {row.control.changed_by} · {new Date(row.control.changed_at).toLocaleString()} —{" "}
          {row.control.reason}
        </div>
      )}
      {isAdmin && <SwitchControl row={row} onSwitch={onSwitch} />}
    </li>
  );
}

export function SourcesPanel({ groups, isAdmin, controlsFailed, now, onSwitch }: Props) {
  const t = useT();
  return (
    <div className="sources">
      {controlsFailed && <p className="small muted pad">{t("sources_unavailable")}</p>}
      {!isAdmin && <p className="small muted pad">{t("sources_admin_only")}</p>}
      {groups.map((group) => (
        <section key={group.type.sourceType}>
          <h3 className="source-type">{typeName(t, group.type.sourceType)}</h3>
          <ul className="list">
            <Row row={group.type} isAdmin={isAdmin} now={now} onSwitch={onSwitch} />
            {group.instances.map((row) => (
              <Row key={row.instanceId} row={row} isAdmin={isAdmin} now={now} onSwitch={onSwitch} />
            ))}
          </ul>
        </section>
      ))}
    </div>
  );
}

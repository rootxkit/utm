// The UAS operator registry (U-01): list, filter and look up operators, their
// aircraft and remote pilots. Reads the API; an admin may also suspend and
// reactivate. Registering is done through the API or the import tool.
import { type FormEvent, Fragment, useCallback, useEffect, useState } from "react";
import { apiGet, apiLookup, apiPost } from "../api/client";
import { DASH } from "../format";
import { useI18n } from "../i18n";
import {
  type RegistrationStatus,
  type RemotePilot,
  STATUSES,
  type Standing,
  type Uas,
  type UasOperator,
  competencyExpired,
  competencyName,
  registryQuery,
  standing,
} from "../registry";

type Kind = "operators" | "aircraft" | "pilots";
const PAGE = 200;
// How long typing must pause before the list is asked for again.
const SEARCH_DEBOUNCE_MS = 300;

function useDebounced<T>(value: T, delayMs: number): T {
  const [settled, setSettled] = useState(value);
  useEffect(() => {
    const timer = window.setTimeout(() => setSettled(value), delayMs);
    return () => window.clearTimeout(timer);
  }, [value, delayMs]);
  return settled;
}

function StatusPill({ value }: { value: Standing }) {
  const { t } = useI18n();
  return <span className={`pill reg-${value}`}>{t(`status_${value}`)}</span>;
}

function useDate() {
  const { lang } = useI18n();
  return (value: string | null | undefined) =>
    value ? new Date(value).toLocaleDateString(lang === "ka" ? "ka-GE" : "en-GB") : DASH;
}

interface Props {
  isAdmin: boolean;
  now: number;
}

export function RegistryView({ isAdmin, now }: Props) {
  const { t } = useI18n();
  const [kind, setKind] = useState<Kind>("operators");
  const [query, setQuery] = useState("");
  const [status, setStatus] = useState<RegistrationStatus | "">("");
  const [operators, setOperators] = useState<UasOperator[]>([]);
  const [aircraft, setAircraft] = useState<Uas[]>([]);
  const [pilots, setPilots] = useState<RemotePilot[]>([]);
  const [failed, setFailed] = useState(false);
  const [selected, setSelected] = useState<UasOperator | null>(null);
  const [revision, setRevision] = useState(0);

  const reload = useCallback(() => setRevision((n) => n + 1), []);
  const settledQuery = useDebounced(query, SEARCH_DEBOUNCE_MS);

  useEffect(() => {
    // A slower, older answer must not overwrite a newer one.
    let current = true;
    const filters = registryQuery({ q: settledQuery, status, limit: PAGE });
    const load =
      kind === "operators"
        ? apiGet("/uas/operators", filters).then((rows) => current && setOperators(rows))
        : kind === "aircraft"
          ? apiGet("/uas/aircraft", filters).then((rows) => current && setAircraft(rows))
          : apiGet("/uas/pilots", filters).then((rows) => current && setPilots(rows));
    load.then(() => current && setFailed(false)).catch(() => current && setFailed(true));
    return () => {
      current = false;
    };
  }, [kind, settledQuery, status, revision]);

  return (
    <div className="registry">
      <div className="registry-main">
        <h2>{t("registry")}</h2>
        <Lookup onOperator={setSelected} />
        <div className="tabs" role="tablist">
          {(["operators", "aircraft", "pilots"] as const).map((id) => (
            <button
              key={id}
              type="button"
              role="tab"
              aria-selected={kind === id}
              className={`tab${kind === id ? " active" : ""}`}
              onClick={() => setKind(id)}
            >
              {t(`reg_${id}`)}
            </button>
          ))}
        </div>
        <div className="registry-filters">
          <input
            type="search"
            value={query}
            placeholder={t("reg_filter")}
            aria-label={t("reg_filter")}
            onChange={(event) => setQuery(event.target.value)}
          />
          <select
            value={status}
            aria-label={t("reg_status")}
            onChange={(event) => setStatus(event.target.value as RegistrationStatus | "")}
          >
            <option value="">{t("reg_all_statuses")}</option>
            {STATUSES.map((value) => (
              <option key={value} value={value}>
                {t(`status_${value}`)}
              </option>
            ))}
          </select>
        </div>
        {failed && <p className="muted">{t("reg_unavailable")}</p>}
        {/* After a failed read the last list is dimmed: it no longer answers the filter. */}
        <div className={failed ? "stale" : undefined} aria-busy={failed}>
          {kind === "operators" && (
            <OperatorTable operators={operators} now={now} onSelect={setSelected} />
          )}
          {kind === "aircraft" && (
            <AircraftTable aircraft={aircraft} isAdmin={isAdmin} onChanged={reload} />
          )}
          {kind === "pilots" && (
            <PilotTable pilots={pilots} now={now} isAdmin={isAdmin} onChanged={reload} />
          )}
        </div>
      </div>
      {selected && (
        <OperatorDetail
          key={selected.id}
          operator={selected}
          now={now}
          isAdmin={isAdmin}
          onChanged={(changed) => {
            setSelected(changed);
            reload();
          }}
          onClose={() => setSelected(null)}
        />
      )}
    </div>
  );
}

// One box for either identifier: a registration number and a serial cannot
// be told apart by shape while Georgia's format is unconfirmed, so both are
// looked up.
function Lookup({ onOperator }: { onOperator: (operator: UasOperator) => void }) {
  const { t } = useI18n();
  const [value, setValue] = useState("");
  const [found, setFound] = useState<{ q: string; uas: Uas | null; missing: boolean } | null>(null);
  const [failed, setFailed] = useState(false);

  const submit = (event: FormEvent) => {
    event.preventDefault();
    const q = value.trim();
    if (!q) return;
    Promise.all([
      apiLookup("/uas/operators/lookup", registryQuery({ registration_number: q })),
      apiLookup("/uas/aircraft/lookup", registryQuery({ serial: q })),
    ])
      .then(([operator, uas]) => {
        setFailed(false);
        if (operator) onOperator(operator);
        setFound({ q, uas, missing: !operator && !uas });
      })
      .catch(() => setFailed(true));
  };

  return (
    <form className="registry-lookup" onSubmit={submit}>
      <label className="small">
        {t("reg_lookup")}{" "}
        <input
          className="mono"
          value={value}
          onChange={(event) => setValue(event.target.value)}
          aria-label={t("reg_lookup")}
        />
      </label>{" "}
      <button type="submit" className="button">
        {t("reg_lookup_button")}
      </button>
      {failed && <span className="muted small"> {t("reg_unavailable")}</span>}
      {found?.missing && <p className="muted">{t("reg_not_found", { q: found.q })}</p>}
      {found?.uas && <AircraftTable aircraft={[found.uas]} isAdmin={false} onChanged={() => {}} />}
    </form>
  );
}

function OperatorTable({
  operators,
  now,
  onSelect,
}: {
  operators: UasOperator[];
  now: number;
  onSelect: (operator: UasOperator) => void;
}) {
  const { t } = useI18n();
  const date = useDate();
  if (operators.length === 0) return <p className="muted">{t("none")}</p>;
  return (
    <table className="registry-table">
      <thead>
        <tr>
          <th>{t("reg_registration_number")}</th>
          <th>{t("reg_legal_name")}</th>
          <th>{t("reg_type")}</th>
          <th>{t("reg_valid_until")}</th>
          <th>{t("reg_status")}</th>
        </tr>
      </thead>
      <tbody>
        {operators.map((operator) => (
          <tr key={operator.id} className="clickable" onClick={() => onSelect(operator)}>
            <td className="mono">{operator.registration_number}</td>
            <td>{operator.legal_name}</td>
            <td>{t(`type_${operator.operator_type}`)}</td>
            <td>{date(operator.valid_until)}</td>
            <td>
              <StatusPill value={standing(operator.status, operator.valid_until, now)} />
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function StatusActions({
  path,
  status,
  onChanged,
}: {
  path: string;
  status: RegistrationStatus;
  onChanged: (body: unknown) => void;
}) {
  const { t } = useI18n();
  const [refused, setRefused] = useState<string | null>(null);
  if (status === "revoked") return null;
  const action = status === "active" ? "suspend" : "reactivate";
  const run = async () => {
    const response = await apiPost(`${path}/${action}`);
    const body: unknown = await response.json();
    if (response.ok) {
      setRefused(null);
      onChanged(body);
    } else {
      const detail = (body as { detail?: { code?: string } }).detail;
      setRefused(detail?.code ?? String(response.status));
    }
  };
  return (
    <>
      <button type="button" className="button" onClick={() => void run()}>
        {t(`reg_${action}`)}
      </button>
      {refused && <span className="small muted"> {t("reg_refused", { code: refused })}</span>}
    </>
  );
}

function AircraftTable({
  aircraft,
  isAdmin,
  onChanged,
}: {
  aircraft: Uas[];
  isAdmin: boolean;
  onChanged: () => void;
}) {
  const { t } = useI18n();
  if (aircraft.length === 0) return <p className="muted">{t("none")}</p>;
  return (
    <table className="registry-table">
      <thead>
        <tr>
          <th>{t("reg_serial")}</th>
          <th>{t("reg_class")}</th>
          <th>{t("reg_mtom")}</th>
          <th>{t("reg_model")}</th>
          <th>{t("reg_operator")}</th>
          <th>{t("reg_status")}</th>
          {isAdmin && <th />}
        </tr>
      </thead>
      <tbody>
        {aircraft.map((uas) => (
          <tr key={uas.id}>
            <td className="mono">
              {uas.serial}
              {uas.serial_cta2063 && <div className="small muted">{t("reg_cta_serial")}</div>}
            </td>
            <td>{uas.class_label ?? t("reg_no_class")}</td>
            <td>{uas.mtom_g === null ? DASH : t("reg_grams", { n: uas.mtom_g })}</td>
            <td>{uas.model ?? DASH}</td>
            <td className="mono">
              {uas.operator_registration_number ?? DASH}
              {uas.operator_status && uas.operator_status !== "active" && (
                <div className="small">
                  {t("reg_operator_is", { status: t(`status_${uas.operator_status}`) })}
                </div>
              )}
            </td>
            <td>
              <StatusPill value={uas.registration_status} />
              {uas.retired_at && <div className="small muted">{t("reg_retired")}</div>}
            </td>
            {isAdmin && (
              <td>
                <StatusActions
                  path={`/uas/aircraft/${uas.id}`}
                  status={uas.registration_status}
                  onChanged={onChanged}
                />
              </td>
            )}
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function PilotTable({
  pilots,
  now,
  isAdmin,
  onChanged,
}: {
  pilots: RemotePilot[];
  now: number;
  isAdmin: boolean;
  onChanged: () => void;
}) {
  const { t } = useI18n();
  const date = useDate();
  if (pilots.length === 0) return <p className="muted">{t("none")}</p>;
  return (
    <table className="registry-table">
      <thead>
        <tr>
          <th>{t("reg_name")}</th>
          <th>{t("reg_certificate")}</th>
          <th>{t("reg_competencies")}</th>
          <th>{t("reg_operator")}</th>
          <th>{t("reg_status")}</th>
          {isAdmin && <th />}
        </tr>
      </thead>
      <tbody>
        {pilots.map((pilot) => (
          <tr key={pilot.id}>
            <td>{pilot.name}</td>
            <td className="mono">{pilot.license_ref ?? DASH}</td>
            <td>
              {pilot.competencies.length === 0
                ? DASH
                : pilot.competencies.map((c) => (
                    <div key={c.competency} className="small">
                      {competencyName(c.competency)} · {date(c.valid_until)}
                      {competencyExpired(c, now) && (
                        <>
                          {" "}
                          <StatusPill value="expired" />
                        </>
                      )}
                    </div>
                  ))}
            </td>
            <td className="mono">{pilot.operator_registration_number ?? DASH}</td>
            <td>
              <StatusPill value={pilot.registration_status} />
            </td>
            {isAdmin && (
              <td>
                <StatusActions
                  path={`/uas/pilots/${pilot.id}`}
                  status={pilot.registration_status}
                  onChanged={onChanged}
                />
              </td>
            )}
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function OperatorDetail({
  operator,
  now,
  isAdmin,
  onChanged,
  onClose,
}: {
  operator: UasOperator;
  now: number;
  isAdmin: boolean;
  onChanged: (operator: UasOperator) => void;
  onClose: () => void;
}) {
  const { t } = useI18n();
  const date = useDate();
  const [aircraft, setAircraft] = useState<Uas[]>([]);
  const [pilots, setPilots] = useState<RemotePilot[]>([]);
  const [revision, setRevision] = useState(0);

  useEffect(() => {
    const filters = registryQuery({ operator_id: operator.id, limit: PAGE });
    apiGet("/uas/aircraft", filters)
      .then(setAircraft)
      .catch(() => setAircraft([]));
    apiGet("/uas/pilots", filters)
      .then(setPilots)
      .catch(() => setPilots([]));
  }, [operator.id, revision]);

  const reload = () => setRevision((n) => n + 1);
  const rows: [string, string][] = [
    [t("reg_legal_name"), operator.legal_name],
    [t("reg_type"), t(`type_${operator.operator_type}`)],
    [
      t("reg_contact"),
      operator.contact
        ? [operator.contact.contact_email, operator.contact.contact_phone]
            .filter(Boolean)
            .join(" · ") || DASH
        : t("reg_contact_hidden"),
    ],
    [
      t("reg_address"),
      operator.contact ? (operator.contact.postal_address ?? DASH) : t("reg_contact_hidden"),
    ],
    [t("reg_valid_until"), date(operator.valid_until)],
    [t("reg_source"), t(`source_${operator.source}`)],
  ];

  return (
    <aside className="registry-detail">
      <div className="row-head">
        <strong className="mono">{operator.registration_number}</strong>
        <button type="button" className="button" onClick={onClose}>
          {t("close")}
        </button>
      </div>
      <p>
        <StatusPill value={standing(operator.status, operator.valid_until, now)} />{" "}
        {isAdmin && (
          <StatusActions
            path={`/uas/operators/${operator.id}`}
            status={operator.status}
            onChanged={(body) => onChanged(body as UasOperator)}
          />
        )}
      </p>
      <dl className="fields">
        {rows.map(([label, value]) => (
          <Fragment key={label}>
            <dt>{label}</dt>
            <dd>{value}</dd>
          </Fragment>
        ))}
      </dl>
      <h3>{t("reg_aircraft")}</h3>
      <AircraftTable aircraft={aircraft} isAdmin={isAdmin} onChanged={reload} />
      <h3>{t("reg_pilots")}</h3>
      <PilotTable pilots={pilots} now={now} isAdmin={isAdmin} onChanged={reload} />
    </aside>
  );
}

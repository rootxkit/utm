// U-03. A zone's detail, and the editor the authority draws and describes a
// zone with. Drawing happens on the map (App passes clicks in); this panel
// holds the fields, the drawing controls and the save.
import { useT } from "../i18n";
import {
  DAYS,
  type FormError,
  PURPOSES,
  REASONS,
  REFERENCES,
  RESTRICTIONS,
  type Shape,
  type ZoneForm,
  type ZoneOut,
  limitsText,
  periodParts,
} from "../zones";

export type Drawing = "polygon" | "circle" | null;

export interface EditorState {
  // null: a new zone.
  zoneId: string | null;
  form: ZoneForm;
  shape: Shape | null;
  drawing: Drawing;
  // A circle's centre while waiting for its edge.
  circleCenter: [number, number] | null;
  errors: FormError[];
  refused: string[];
  saving: boolean;
}

interface EditorProps {
  state: EditorState;
  onChange: (state: EditorState) => void;
  onSave: () => void;
  onCancel: () => void;
}

export function ZoneEditor({ state, onChange, onSave, onCancel }: EditorProps) {
  const t = useT();
  const { form, shape, drawing } = state;
  const set = (changes: Partial<ZoneForm>) => onChange({ ...state, form: { ...form, ...changes } });
  const errorFor = (field: string) => {
    const error = state.errors.find((e) => e.field === field);
    return error ? <div className="error small">{t(error.key)}</div> : null;
  };
  const draw = (kind: Exclude<Drawing, null>) =>
    onChange({
      ...state,
      drawing: kind,
      circleCenter: null,
      shape: kind === "polygon" ? { kind: "polygon", points: [] } : null,
    });
  const text = (
    label: string,
    value: string,
    onValue: (value: string) => void,
    field?: string,
    props: Record<string, unknown> = {},
  ) => (
    <label className="field">
      <span>{label}</span>
      <input value={value} onChange={(event) => onValue(event.target.value)} {...props} />
      {field && errorFor(field)}
    </label>
  );

  return (
    <aside className="detail pad zone-editor">
      <div className="detail-head">
        <h2>{state.zoneId ? form.identifier || t("zone_edit") : t("zone_new")}</h2>
        <button type="button" className="link" onClick={onCancel}>
          {t("zone_cancel")}
        </button>
      </div>

      <fieldset>
        <legend>{t("zone_shape")}</legend>
        <div className="zone-actions">
          <button
            type="button"
            className={`button small${drawing === "polygon" ? " active" : ""}`}
            onClick={() => draw("polygon")}
          >
            {t("zone_draw_polygon")}
          </button>
          <button
            type="button"
            className={`button small${drawing === "circle" ? " active" : ""}`}
            onClick={() => draw("circle")}
          >
            {t("zone_draw_circle")}
          </button>
          {drawing === "polygon" && (
            <button
              type="button"
              className="button small"
              onClick={() => onChange({ ...state, drawing: null })}
            >
              {t("zone_finish")}
            </button>
          )}
        </div>
        {drawing && (
          <div className="small muted">
            {t(drawing === "polygon" ? "zone_draw_hint_polygon" : "zone_draw_hint_circle")}
          </div>
        )}
        {shape?.kind === "polygon" && (
          <div className="small">{t("zone_vertices", { n: shape.points.length })}</div>
        )}
        {shape?.kind === "circle" && (
          <label className="field">
            <span>
              {t("zone_radius")} ({form.uom === "FT" ? "ft" : "m"})
            </span>
            <input
              type="number"
              min="0"
              value={shape.radius}
              onChange={(event) =>
                onChange({ ...state, shape: { ...shape, radius: Number(event.target.value) } })
              }
            />
          </label>
        )}
        {errorFor("geometry")}
      </fieldset>

      {text(t("zone_identifier"), form.identifier, (v) => set({ identifier: v }), "identifier", {
        maxLength: 7,
      })}
      {text(t("zone_country"), form.country, (v) => set({ country: v.toUpperCase() }), "country", {
        maxLength: 3,
      })}
      {text(t("zone_name"), form.name, (v) => set({ name: v }), undefined, { maxLength: 200 })}
      <label className="field">
        <span>{t("zone_restriction")}</span>
        <select
          value={form.restriction}
          onChange={(event) => set({ restriction: event.target.value as ZoneForm["restriction"] })}
        >
          {RESTRICTIONS.map((r) => (
            <option key={r} value={r}>
              {t(`restriction_${r}`)}
            </option>
          ))}
        </select>
      </label>
      <fieldset>
        <legend>{t("zone_reasons")}</legend>
        <div className="checks">
          {REASONS.map((reason) => (
            <label key={reason}>
              <input
                type="checkbox"
                checked={form.reasons.includes(reason)}
                onChange={(event) =>
                  set({
                    reasons: event.target.checked
                      ? [...form.reasons, reason]
                      : form.reasons.filter((r) => r !== reason),
                  })
                }
              />{" "}
              {t(`reason_${reason}`)}
            </label>
          ))}
        </div>
      </fieldset>
      {text(t("zone_message"), form.message, (v) => set({ message: v }), undefined, {
        maxLength: 200,
      })}

      <fieldset>
        <legend>{t("zone_limits")}</legend>
        <label className="field">
          <span>{t("zone_unit")}</span>
          <select
            value={form.uom}
            onChange={(event) => set({ uom: event.target.value as ZoneForm["uom"] })}
          >
            <option value="M">m</option>
            <option value="FT">ft</option>
          </select>
        </label>
        {(["lower", "upper"] as const).map((which) => (
          <div key={which} className="field-row">
            {text(
              t(which === "lower" ? "zone_lower" : "zone_upper"),
              form[`${which}Limit`],
              (v) => set({ [`${which}Limit`]: v }),
              `${which}Limit`,
              { inputMode: "decimal", placeholder: t("zone_limit_blank") },
            )}
            <select
              aria-label={t("zone_reference")}
              value={form[`${which}Reference`]}
              onChange={(event) =>
                set({ [`${which}Reference`]: event.target.value as ZoneForm["lowerReference"] })
              }
            >
              {REFERENCES.map((r) => (
                <option key={r} value={r}>
                  {r}
                </option>
              ))}
            </select>
          </div>
        ))}
      </fieldset>

      <fieldset>
        <legend>{t("zone_applicability")}</legend>
        <label>
          <input
            type="checkbox"
            checked={form.permanent}
            onChange={(event) => set({ permanent: event.target.checked })}
          />{" "}
          {t("zone_permanent")}
        </label>
        {!form.permanent && (
          <>
            {text(t("zone_from"), form.start, (v) => set({ start: v }), undefined, {
              type: "datetime-local",
            })}
            {text(t("zone_until"), form.end, (v) => set({ end: v }), "end", {
              type: "datetime-local",
            })}
            <div className="checks">
              {DAYS.map((day) => (
                <label key={day}>
                  <input
                    type="checkbox"
                    checked={form.scheduleDays.includes(day)}
                    onChange={(event) =>
                      set({
                        scheduleDays: event.target.checked
                          ? [...form.scheduleDays, day]
                          : form.scheduleDays.filter((d) => d !== day),
                      })
                    }
                  />{" "}
                  {t(`day_${day}`)}
                </label>
              ))}
            </div>
            <div className="field-row">
              {text(
                t("zone_daily_from"),
                form.scheduleStart,
                (v) => set({ scheduleStart: v }),
                undefined,
                {
                  type: "time",
                },
              )}
              {text(
                t("zone_daily_until"),
                form.scheduleEnd,
                (v) => set({ scheduleEnd: v }),
                undefined,
                {
                  type: "time",
                },
              )}
            </div>
            {errorFor("schedule")}
            {errorFor("applicability")}
          </>
        )}
      </fieldset>

      <fieldset>
        <legend>{t("zone_authority")}</legend>
        {text(t("zone_name"), form.authorityName, (v) => set({ authorityName: v }))}
        {text(t("zone_contact"), form.authorityContact, (v) => set({ authorityContact: v }))}
        {text(t("zone_email"), form.authorityEmail, (v) => set({ authorityEmail: v }))}
        {text(t("zone_phone"), form.authorityPhone, (v) => set({ authorityPhone: v }))}
        <label className="field">
          <span>{t("zone_purpose")}</span>
          <select
            value={form.authorityPurpose}
            onChange={(event) =>
              set({ authorityPurpose: event.target.value as ZoneForm["authorityPurpose"] })
            }
          >
            <option value="">—</option>
            {PURPOSES.map((p) => (
              <option key={p} value={p}>
                {t(`purpose_${p}`)}
              </option>
            ))}
          </select>
        </label>
      </fieldset>

      {state.refused.length > 0 && (
        <div>
          <div className="error">{t("zone_refused")}</div>
          <ul className="problems small">
            {state.refused.map((line) => (
              <li key={line}>{line}</li>
            ))}
          </ul>
        </div>
      )}
      <button type="button" className="button" disabled={state.saving} onClick={onSave}>
        {t("zone_save")}
      </button>
    </aside>
  );
}

interface DetailProps {
  zone: ZoneOut;
  canWrite: boolean;
  onEdit: () => void;
  onDelete: () => void;
  onClose: () => void;
}

export function ZoneDetail({ zone, canWrite, onEdit, onDelete, onClose }: DetailProps) {
  const t = useT();
  const f = zone.feature;
  const editable = canWrite && zone.type === "geozone";
  return (
    <aside className="detail pad">
      <div className="detail-head">
        <h2>
          {f.identifier} {f.name ?? ""}
        </h2>
        <button type="button" className="link" onClick={onClose}>
          {t("close")}
        </button>
      </div>
      <span className={`pill ${zone.active_now ? "active" : "inactive"}`}>
        {t(zone.active_now ? "zone_active" : "zone_inactive")}
      </span>
      <dl className="fields">
        <dt>{t("zone_restriction")}</dt>
        <dd>
          {zone.type === "geozone"
            ? t(`restriction_${f.restriction}`)
            : t(zone.type === "base" ? "base" : "corridor")}
        </dd>
        {f.reason && f.reason.length > 0 && (
          <>
            <dt>{t("zone_reasons")}</dt>
            <dd>{f.reason.map((r) => t(`reason_${r}`)).join(", ")}</dd>
          </>
        )}
        {f.message && (
          <>
            <dt>{t("zone_message")}</dt>
            <dd>{f.message}</dd>
          </>
        )}
        <dt>{t("zone_limits")}</dt>
        <dd>{limitsText(f.geometry[0], t("zone_surface"), t("zone_unlimited"))}</dd>
        <dt>{t("zone_applicability")}</dt>
        <dd>
          {f.applicability.map((period, index) => {
            const parts = periodParts(period);
            return (
              <div key={index}>
                {parts.permanent
                  ? t("zone_permanent")
                  : [
                      parts.from && `${t("zone_from")} ${parts.from}`,
                      parts.until && `${t("zone_until")} ${parts.until}`,
                      ...parts.schedule.map((s) => `${s.days} ${s.times}`),
                    ]
                      .filter(Boolean)
                      .join(" · ")}
              </div>
            );
          })}
        </dd>
        {f.zoneAuthority.map((authority, index) => (
          <div key={index} className="contents">
            <dt>{t("zone_authority")}</dt>
            <dd>
              {[authority.name, authority.contactName, authority.email, authority.phone]
                .filter(Boolean)
                .join(" · ")}
              {authority.purpose && ` (${t(`purpose_${authority.purpose}`)})`}
            </dd>
          </div>
        ))}
      </dl>
      {editable && (
        <div className="zone-actions">
          <button type="button" className="button small" onClick={onEdit}>
            {t("zone_edit")}
          </button>
          <button type="button" className="button small danger" onClick={onDelete}>
            {t("zone_delete")}
          </button>
        </div>
      )}
    </aside>
  );
}

// U-02. What each identification badge means, with how many aircraft carry it.
import {
  IDENTIFICATION_STATUSES,
  badgeClass,
  statusCounts,
  statusHintKey,
  statusLabelKey,
} from "../identification";
import { useT } from "../i18n";
import type { Aircraft } from "../types";

export function IdentificationLegend({ aircraft }: { aircraft: Map<string, Aircraft> }) {
  const t = useT();
  const counts = statusCounts(aircraft);
  return (
    <section className="id-legend small" aria-label={t("identification")}>
      <strong>{t("identification")}</strong>
      <ul>
        {IDENTIFICATION_STATUSES.map((status) => (
          <li key={status} title={t(statusHintKey(status))}>
            <span className={badgeClass(status)}>{t(statusLabelKey(status))}</span>{" "}
            <span className="muted">{counts[status]}</span>
          </li>
        ))}
      </ul>
    </section>
  );
}

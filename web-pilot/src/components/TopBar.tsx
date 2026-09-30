import type { GetResponse } from "../api/client";
import type { FeedStatus } from "../feed";
import { type Lang, useI18n } from "../i18n";

export type Me = GetResponse<"/auth/me">;
export type View = "map" | "registry";

interface Props {
  me: Me;
  status: FeedStatus;
  view: View;
  onView: (view: View) => void;
  onLang: (lang: Lang) => void;
  onSignOut: () => void;
}

export function TopBar({ me, status, view, onView, onLang, onSignOut }: Props) {
  const { lang, t } = useI18n();
  return (
    <header className="topbar">
      <div className="brand">
        courier <span className="muted">{t("console")}</span>
      </div>
      <span className={`feed feed-${status}`}>{t(`feed_${status}`)}</span>
      <nav>
        <button
          type="button"
          className="link"
          onClick={() => onView(view === "map" ? "registry" : "map")}
        >
          {view === "map" ? t("registry") : t("map_view")}
        </button>
        <a href="/replay">{t("replay")}</a>
        <a href="/map">{t("old_map")}</a>
      </nav>
      <div className="spacer" />
      <label className="small">
        {t("language")}{" "}
        <select value={lang} onChange={(event) => onLang(event.target.value as Lang)}>
          <option value="en">English</option>
          <option value="ka">ქართული</option>
        </select>
      </label>
      <span className="who">
        {me.display_name} <span className="muted small">({t(`role_${me.role}`)})</span>
      </span>
      <button type="button" className="button" onClick={onSignOut}>
        {t("sign_out")}
      </button>
    </header>
  );
}

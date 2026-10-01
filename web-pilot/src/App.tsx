// The operator console (P6-01, P6-02, P6-03): map, aircraft, alerts, stations.
// It reads the console feed and the API, never a database.
import { useCallback, useEffect, useMemo, useState } from "react";
import { SignInRequired, apiGet, apiPost, apiPut } from "./api/client";
import { AircraftList } from "./components/AircraftList";
import { AlertsPanel } from "./components/AlertsPanel";
import { DronePanel } from "./components/DronePanel";
import { RegistryView } from "./components/RegistryView";
import { SourcesPanel } from "./components/SourcesPanel";
import { StationsPanel, UnclaimedPanel } from "./components/StationsPanel";
import { type Me, TopBar, type View } from "./components/TopBar";
import { useFeed } from "./feed";
import { I18n, type Lang, translator } from "./i18n";
import { type Base, type Layers, MapView, type Zone } from "./map/MapView";
import {
  type SourcesOut,
  aircraftSourceDisabled,
  sourceGroups,
  switchPath,
  switchRequest,
} from "./sources";

type Tab = "aircraft" | "alerts" | "stations" | "sources" | "unclaimed";

const LANG_KEY = "courier.lang";
// How often zones and bases are re-read. A display choice, not flight data.
const REGISTRY_REFRESH_MS = 30_000;
// U-15. How often the source switches are re-read. A switch made from another
// console shows here within this; one made here shows at once.
const SOURCES_REFRESH_MS = 5_000;

function storedLang(): Lang {
  try {
    return localStorage.getItem(LANG_KEY) === "ka" ? "ka" : "en";
  } catch {
    return "en";
  }
}

// An audible cue for an unacknowledged critical alert, repeated while one
// exists. Generated, so there is no sound file to ship. Browsers allow audio
// only after the page has been clicked once; until then the alert is shown
// without sound.
let audio: AudioContext | null = null;
function beep(): void {
  try {
    audio ??= new AudioContext();
    const oscillator = audio.createOscillator();
    const gain = audio.createGain();
    oscillator.frequency.value = 880;
    gain.gain.value = 0.15;
    oscillator.connect(gain).connect(audio.destination);
    oscillator.start();
    oscillator.stop(audio.currentTime + 0.25);
  } catch {
    // No audio: the visual alert stands on its own.
  }
}

function useNow(intervalMs: number): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), intervalMs);
    return () => window.clearInterval(timer);
  }, [intervalMs]);
  return now;
}

export function App() {
  const [lang, setLang] = useState<Lang>(storedLang);
  const [view, setView] = useState<View>(() =>
    location.hash === "#registry" ? "registry" : "map",
  );
  const t = useMemo(() => translator(lang), [lang]);
  const [me, setMe] = useState<Me | null>(null);
  const [feedUrl, setFeedUrl] = useState<string | null>(null);
  const [zones, setZones] = useState<Zone[]>([]);
  const [zonesFailed, setZonesFailed] = useState(false);
  const [bases, setBases] = useState<Base[]>([]);
  const [controls, setControls] = useState<SourcesOut | null>(null);
  const [controlsFailed, setControlsFailed] = useState(false);
  const [tab, setTab] = useState<Tab>("aircraft");
  const [selected, setSelected] = useState<string | null>(null);
  const [acknowledged, setAcknowledged] = useState<Set<string>>(new Set());
  const [layers, setLayers] = useState<Layers>({ zones: true, bases: true, labels: true });
  const { state, status } = useFeed(me ? feedUrl : null);
  const now = useNow(1000);

  useEffect(() => {
    document.documentElement.lang = lang;
    try {
      localStorage.setItem(LANG_KEY, lang);
    } catch {
      // Not remembered; the choice still applies to this page.
    }
  }, [lang]);

  // The registry view is linkable (/app/#registry) and survives a reload.
  useEffect(() => {
    const target = view === "registry" ? "#registry" : location.pathname + location.search;
    history.replaceState(null, "", target);
  }, [view]);

  // Signed in? A 401 here goes to /login and comes back.
  useEffect(() => {
    (async () => {
      setMe(await apiGet("/auth/me"));
      const config = (await (await fetch("/config.json")).json()) as { console_feed_url: string };
      setFeedUrl(config.console_feed_url);
    })().catch((error: unknown) => {
      // Already on the way to /login.
      if (!(error instanceof SignInRequired)) throw error;
    });
  }, []);

  // Zones and bases change while the console is open (an admin adds a zone),
  // so they are re-read. A failed read keeps what was drawn and says so.
  useEffect(() => {
    if (!me) return;
    const load = () => {
      apiGet("/airspace/zones")
        .then((z) => {
          setZones(z);
          setZonesFailed(false);
        })
        .catch(() => setZonesFailed(true));
      apiGet("/bases")
        .then(setBases)
        .catch(() => setZonesFailed(true));
    };
    load();
    const timer = window.setInterval(load, REGISTRY_REFRESH_MS);
    return () => window.clearInterval(timer);
  }, [me]);

  const loadSources = useCallback(async () => {
    try {
      setControls(await apiGet("/sources"));
      setControlsFailed(false);
    } catch (error) {
      if (!(error instanceof SignInRequired)) setControlsFailed(true);
    }
  }, []);

  useEffect(() => {
    if (!me) return;
    void loadSources();
    const timer = window.setInterval(() => void loadSources(), SOURCES_REFRESH_MS);
    return () => window.clearInterval(timer);
  }, [me, loadSources]);

  const switchSource = useCallback(
    async (
      sourceType: string,
      instanceId: string | null,
      enabled: boolean,
      reason: string,
    ): Promise<string | null> => {
      const body = switchRequest(enabled, reason);
      if (!body) return "reason_required";
      const response = await apiPut(switchPath(sourceType, instanceId), body);
      // Re-read either way: what is shown must be what the API holds,
      // whether or not this switch took.
      await loadSources();
      if (response.ok) return null;
      const detail = ((await response.json().catch(() => ({}))) as { detail?: unknown }).detail;
      return typeof detail === "object" && detail !== null && "code" in detail
        ? String((detail as { code: unknown }).code)
        : String(response.status);
    },
    [loadSources],
  );

  const sourceDisabled = useMemo(
    () =>
      new Set(
        [...state.aircraft.entries()]
          .filter(([, item]) => aircraftSourceDisabled(item.data, controls))
          .map(([id]) => id),
      ),
    [state.aircraft, controls],
  );

  // Acknowledgements of alerts that have cleared are forgotten, so the same
  // pair converging again sounds again.
  const activeAcks = useMemo(
    () => new Set([...acknowledged].filter((key) => state.alerts.has(key))),
    [acknowledged, state.alerts],
  );

  const unacknowledgedCritical = [...state.alerts.values()].some(
    (alert) => alert.severity === "critical" && !activeAcks.has(alert.key),
  );
  useEffect(() => {
    if (!unacknowledgedCritical) return;
    beep();
    const timer = window.setInterval(beep, 2000);
    return () => window.clearInterval(timer);
  }, [unacknowledgedCritical]);

  const acknowledge = useCallback((key: string) => {
    setAcknowledged((previous) => new Set(previous).add(key));
  }, []);

  const select = useCallback((droneId: string) => {
    setSelected(droneId);
  }, []);

  const signOut = async () => {
    await apiPost("/auth/logout");
    location.replace("/login?next=/app/");
  };

  if (!me) return <div className="loading">{t("product_name")}…</div>;

  const canAcknowledge = me.role === "operator" || me.role === "admin";
  const selectedAlerts = selected
    ? [...state.alerts.values()].filter((a) => a.drone_ids.includes(selected))
    : [];
  const groups = sourceGroups(controls, state.sources, state.stations, now);
  const disabledSources = groups
    .flatMap((group) => [group.type, ...group.instances])
    .filter((row) => row.state === "disabled").length;
  const tabs: [Tab, string, number][] = [
    ["aircraft", t("aircraft"), state.aircraft.size],
    ["alerts", t("alerts"), state.alerts.size],
    ["stations", t("stations"), state.stations.size],
    // U-15: the count is of what is switched off, the thing to notice.
    ["sources", t("sources"), disabledSources],
    ["unclaimed", t("unclaimed"), state.unclaimed.size],
  ];

  return (
    <I18n.Provider value={{ lang, t }}>
      <div className={`shell${unacknowledgedCritical ? " critical" : ""}`}>
        <TopBar
          me={me}
          status={status}
          view={view}
          onView={setView}
          onLang={setLang}
          onSignOut={() => void signOut()}
        />
        {view === "registry" ? (
          <RegistryView isAdmin={me.role === "admin"} now={now} />
        ) : (
          <>
            <nav className="sidebar">
              <div className="tabs" role="tablist">
                {tabs.map(([id, label, count]) => (
                  <button
                    key={id}
                    type="button"
                    role="tab"
                    aria-selected={tab === id}
                    className={`tab${tab === id ? " active" : ""}${id === "alerts" && count > 0 ? " has-alerts" : ""}`}
                    onClick={() => setTab(id)}
                  >
                    {label} <span className="count">{count}</span>
                  </button>
                ))}
              </div>
              <div className="tab-body">
                {tab === "aircraft" && (
                  <AircraftList
                    aircraft={state.aircraft}
                    alerts={state.alerts}
                    selected={selected}
                    now={now}
                    onSelect={select}
                    sourceDisabled={sourceDisabled}
                  />
                )}
                {tab === "alerts" && (
                  <AlertsPanel
                    alerts={state.alerts}
                    aircraft={state.aircraft}
                    acknowledged={activeAcks}
                    canAcknowledge={canAcknowledge}
                    onAcknowledge={acknowledge}
                    onSelect={select}
                  />
                )}
                {tab === "stations" && <StationsPanel stations={state.stations} />}
                {tab === "sources" && (
                  <SourcesPanel
                    groups={groups}
                    isAdmin={me.role === "admin"}
                    controlsFailed={controlsFailed}
                    now={now}
                    onSwitch={switchSource}
                  />
                )}
                {tab === "unclaimed" && <UnclaimedPanel unclaimed={state.unclaimed} />}
              </div>
              <fieldset className="layers">
                <legend>{t("layers")}</legend>
                {(["zones", "bases", "labels"] as const).map((key) => (
                  <label key={key}>
                    <input
                      type="checkbox"
                      checked={layers[key]}
                      onChange={(event) => setLayers({ ...layers, [key]: event.target.checked })}
                    />{" "}
                    {t(key)}
                  </label>
                ))}
                {zonesFailed && <div className="small muted">{t("zones_unavailable")}</div>}
              </fieldset>
            </nav>
            <main className="main">
              <MapView
                aircraft={state.aircraft}
                alerts={state.alerts}
                zones={zones}
                bases={bases}
                layers={layers}
                selected={selected}
                onSelect={select}
                sourceDisabled={sourceDisabled}
              />
            </main>
            {selected ? (
              <DronePanel
                droneId={selected}
                aircraft={state.aircraft.get(selected)}
                alerts={selectedAlerts}
                now={now}
                onClose={() => setSelected(null)}
                sourceDisabled={sourceDisabled.has(selected)}
              />
            ) : (
              <aside className="detail muted pad">{t("select_hint")}</aside>
            )}
          </>
        )}
      </div>
    </I18n.Provider>
  );
}

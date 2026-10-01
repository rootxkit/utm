// The operator console (P6-01, P6-02, P6-03): map, aircraft, alerts, stations,
// and the zones with their editor (U-03). It reads the console feed and the
// API, never a database.
import { useCallback, useEffect, useMemo, useState } from "react";
import { SignInRequired, apiGet, apiPost, apiSend } from "./api/client";
import { AircraftList } from "./components/AircraftList";
import { AlertsPanel } from "./components/AlertsPanel";
import { DronePanel } from "./components/DronePanel";
import { RegistryView } from "./components/RegistryView";
import { StationsPanel, UnclaimedPanel } from "./components/StationsPanel";
import { type Me, TopBar, type View } from "./components/TopBar";
import { type EditorState, ZoneDetail, ZoneEditor } from "./components/ZoneEditor";
import { ZonesPanel } from "./components/ZonesPanel";
import { useFeed } from "./feed";
import { I18n, type Lang, translator } from "./i18n";
import { type Base, type Layers, MapView, type Zone } from "./map/MapView";
import {
  type LonLat,
  type ZoneOut,
  distanceM,
  draftGeometry,
  emptyForm,
  featureFromForm,
  formFromFeature,
  refusalLines,
} from "./zones";

type Tab = "aircraft" | "alerts" | "zones" | "stations" | "unclaimed";

// Who may change zones: the API's ZONE_WRITERS (api/zone_routes.py), which
// decides; this only hides what would be refused. U-13 adds the regulator.
const ZONE_WRITERS = new Set(["admin"]);
const FEET_M = 0.3048;

function newEditor(zone: ZoneOut | null, country: string): EditorState {
  const opened = zone ? formFromFeature(zone.feature) : null;
  return {
    zoneId: zone?.id ?? null,
    form: opened?.form ?? emptyForm(country),
    shape: opened?.shape ?? null,
    drawing: null,
    circleCenter: null,
    errors: [],
    refused: [],
    saving: false,
  };
}

const LANG_KEY = "courier.lang";
// How often zones and bases are re-read. A display choice, not flight data.
const REGISTRY_REFRESH_MS = 30_000;

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
  const [tab, setTab] = useState<Tab>("aircraft");
  const [selected, setSelected] = useState<string | null>(null);
  const [selectedZone, setSelectedZone] = useState<string | null>(null);
  const [editor, setEditor] = useState<EditorState | null>(null);
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
  const loadZones = useCallback(() => {
    apiGet("/airspace/zones")
      .then((z) => {
        setZones(z);
        setZonesFailed(false);
      })
      .catch(() => setZonesFailed(true));
  }, []);
  useEffect(() => {
    if (!me) return;
    const load = () => {
      loadZones();
      apiGet("/bases")
        .then(setBases)
        .catch(() => setZonesFailed(true));
    };
    load();
    const timer = window.setInterval(load, REGISTRY_REFRESH_MS);
    return () => window.clearInterval(timer);
  }, [me, loadZones]);

  // A click on the map: a corner or a circle while drawing, else the zone
  // under it is selected.
  const mapClick = useCallback(
    (point: LonLat, zoneId: string | null) => {
      if (editor?.drawing === "polygon") {
        const points = editor.shape?.kind === "polygon" ? editor.shape.points : [];
        setEditor({ ...editor, shape: { kind: "polygon", points: [...points, point] } });
      } else if (editor?.drawing === "circle") {
        if (!editor.circleCenter) {
          setEditor({ ...editor, circleCenter: point, shape: null });
        } else {
          const metres = distanceM(editor.circleCenter, point);
          const radius = editor.form.uom === "FT" ? metres / FEET_M : metres;
          setEditor({
            ...editor,
            drawing: null,
            circleCenter: null,
            shape: { kind: "circle", center: editor.circleCenter, radius: Math.round(radius) },
          });
        }
      } else if (zoneId && !editor) {
        setSelectedZone(zoneId);
        setTab("zones");
      }
    },
    [editor],
  );

  const saveZone = async () => {
    if (!editor) return;
    const built = featureFromForm(editor.form, editor.shape);
    if (!built.feature) {
      setEditor({ ...editor, errors: built.errors, refused: [] });
      return;
    }
    setEditor({ ...editor, errors: [], refused: [], saving: true });
    const response = await apiSend(
      editor.zoneId ? "PUT" : "POST",
      editor.zoneId ? `/airspace/zones/${editor.zoneId}` : "/airspace/zones",
      JSON.stringify(built.feature),
    );
    const body: unknown = await response.json().catch(() => null);
    if (!response.ok) {
      setEditor({ ...editor, errors: [], refused: refusalLines(body), saving: false });
      return;
    }
    setEditor(null);
    setSelectedZone((body as ZoneOut).id);
    loadZones();
  };

  const deleteZone = async (zone: ZoneOut) => {
    if (!window.confirm(t("zone_delete_confirm", { id: zone.feature.identifier }))) return;
    const response = await apiSend("DELETE", `/airspace/zones/${zone.id}`);
    if (response.ok) {
      setSelectedZone(null);
      loadZones();
    } else {
      window.alert(refusalLines(await response.json().catch(() => null)).join("\n"));
    }
  };

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
  const canWriteZones = ZONE_WRITERS.has(me.role);
  const zoneShown = zones.find((zone) => zone.id === selectedZone) ?? null;
  const defaultCountry = zones.find((zone) => zone.type === "geozone")?.feature.country ?? "";
  const selectedAlerts = selected
    ? [...state.alerts.values()].filter((a) => a.drone_ids.includes(selected))
    : [];
  const tabs: [Tab, string, number][] = [
    ["aircraft", t("aircraft"), state.aircraft.size],
    ["alerts", t("alerts"), state.alerts.size],
    ["zones", t("geozones"), zones.length],
    ["stations", t("stations"), state.stations.size],
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
                {tab === "zones" && (
                  <ZonesPanel
                    zones={zones}
                    selected={selectedZone}
                    canWrite={canWriteZones}
                    onSelect={(id) => {
                      setEditor(null);
                      setSelectedZone(id);
                    }}
                    onNew={() => {
                      setSelectedZone(null);
                      setEditor(newEditor(null, defaultCountry));
                    }}
                    onChanged={loadZones}
                  />
                )}
                {tab === "stations" && <StationsPanel stations={state.stations} />}
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
                selectedZone={selectedZone}
                draft={
                  editor ? draftGeometry(editor.shape, editor.form.uom, editor.circleCenter) : null
                }
                drawing={Boolean(editor?.drawing)}
                onMapClick={mapClick}
              />
            </main>
            {editor ? (
              <ZoneEditor
                state={editor}
                onChange={setEditor}
                onSave={() => void saveZone()}
                onCancel={() => setEditor(null)}
              />
            ) : zoneShown && tab === "zones" ? (
              <ZoneDetail
                zone={zoneShown}
                canWrite={canWriteZones}
                onEdit={() => setEditor(newEditor(zoneShown, defaultCountry))}
                onDelete={() => void deleteZone(zoneShown)}
                onClose={() => setSelectedZone(null)}
              />
            ) : selected ? (
              <DronePanel
                droneId={selected}
                aircraft={state.aircraft.get(selected)}
                alerts={selectedAlerts}
                now={now}
                onClose={() => setSelected(null)}
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

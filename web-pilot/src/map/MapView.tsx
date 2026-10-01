// P6-01. The live map: basemap, zones, bases, aircraft and the conflicts
// between them. U-03: zones styled by restriction, faint and dashed while
// they do not apply, and the shape being drawn in the zone editor. The map is created once; everything drawn on it is updated
// from props. A style change (basemap arriving, language, dark mode) drops
// every source, so the overlays are re-added on each `style.load`.
import maplibregl, { type GeoJSONSource, type Map as MapLibre } from "maplibre-gl";
import { PMTiles, Protocol } from "pmtiles";
import { useEffect, useRef, useState } from "react";
import type { Feature, FeatureCollection, Geometry } from "geojson";
import type { GetResponse } from "../api/client";
import { useI18n } from "../i18n";
import { type Aircraft, type Alert, worse } from "../types";
import { type LonLat, zoneStyle } from "../zones";
import { BASEMAP_URL, type BasemapInfo, basemapStyle } from "./basemap";

export type Zone = GetResponse<"/airspace/zones">[number];
export type Base = GetResponse<"/bases">[number];

export interface Layers {
  zones: boolean;
  bases: boolean;
  labels: boolean;
}

// The shape being drawn in the zone editor, ready to draw.
export interface Draft {
  ring: LonLat[] | null;
  line: LonLat[] | null;
  points: LonLat[];
}

interface Props {
  aircraft: Map<string, Aircraft>;
  alerts: Map<string, Alert>;
  zones: Zone[];
  bases: Base[];
  layers: Layers;
  selected: string | null;
  onSelect: (droneId: string) => void;
  selectedZone: string | null;
  draft: Draft | null;
  // While drawing, a click places a point instead of selecting a zone.
  drawing: boolean;
  // A click on the map, with the zone under it (if any).
  onMapClick: (lngLat: LonLat, zoneId: string | null) => void;
}

let protocolAdded = false;
function addPmtilesProtocol(): void {
  if (protocolAdded) return;
  maplibregl.addProtocol("pmtiles", new Protocol().tile);
  protocolAdded = true;
}

const darkQuery = window.matchMedia("(prefers-color-scheme: dark)");

type Collection = FeatureCollection;

function zonesGeoJson(zones: Zone[], selectedZone: string | null): Collection {
  return {
    type: "FeatureCollection",
    features: zones.map((zone) => {
      const style = zoneStyle(zone);
      return {
        type: "Feature",
        geometry: zone.geometry as unknown as Geometry,
        properties: {
          id: zone.id,
          name: zone.feature.name ?? zone.feature.identifier,
          colour: style.colour,
          opacity: style.fillOpacity,
          dashed: style.dashed,
          selected: zone.id === selectedZone,
        },
      };
    }),
  };
}

function draftGeoJson(draft: Draft | null): Collection {
  if (!draft) return EMPTY;
  const features: Feature[] = draft.points.map((point) => ({
    type: "Feature",
    geometry: { type: "Point", coordinates: point },
    properties: {},
  }));
  if (draft.ring) {
    features.push({
      type: "Feature",
      geometry: { type: "Polygon", coordinates: [draft.ring] },
      properties: {},
    });
  } else if (draft.line) {
    features.push({
      type: "Feature",
      geometry: { type: "LineString", coordinates: draft.line },
      properties: {},
    });
  }
  return { type: "FeatureCollection", features };
}

function basesGeoJson(bases: Base[]): Collection {
  return {
    type: "FeatureCollection",
    features: bases.map((base) => ({
      type: "Feature",
      geometry: { type: "Point", coordinates: [base.lon_deg, base.lat_deg] },
      properties: { name: base.name },
    })),
  };
}

function position(aircraft: Aircraft | undefined): [number, number] | null {
  if (!aircraft) return null;
  const { lat_deg, lon_deg } = aircraft.data;
  return lat_deg === null || lon_deg === null ? null : [lon_deg, lat_deg];
}

// A line between the two aircraft of each conflict alert, where both are placed.
function conflictsGeoJson(alerts: Map<string, Alert>, aircraft: Map<string, Aircraft>): Collection {
  const features: Feature[] = [];
  for (const alert of alerts.values()) {
    if (alert.kind !== "conflict" || alert.drone_ids.length < 2) continue;
    const a = position(aircraft.get(alert.drone_ids[0] ?? ""));
    const b = position(aircraft.get(alert.drone_ids[1] ?? ""));
    if (!a || !b) continue;
    features.push({
      type: "Feature",
      geometry: { type: "LineString", coordinates: [a, b] },
      properties: { severity: alert.severity },
    });
  }
  return { type: "FeatureCollection", features };
}

const EMPTY: Collection = { type: "FeatureCollection", features: [] };

function addOverlays(map: MapLibre): void {
  if (map.getLayer("conflicts")) return;
  for (const id of ["zones", "bases", "conflicts", "draft"]) {
    if (!map.getSource(id)) map.addSource(id, { type: "geojson", data: EMPTY });
  }
  const labels = Boolean(map.getStyle().glyphs);
  map.addLayer({
    id: "zones-fill",
    type: "fill",
    source: "zones",
    paint: { "fill-color": ["get", "colour"], "fill-opacity": ["get", "opacity"] },
  });
  map.addLayer({
    id: "zones-line",
    type: "line",
    source: "zones",
    filter: ["!", ["get", "dashed"]],
    paint: { "line-color": ["get", "colour"], "line-width": ["case", ["get", "selected"], 4, 2] },
  });
  // A zone that does not apply now: dashed, so "not in force" does not rest
  // on colour alone.
  map.addLayer({
    id: "zones-line-inactive",
    type: "line",
    source: "zones",
    filter: ["get", "dashed"],
    paint: {
      "line-color": ["get", "colour"],
      "line-width": ["case", ["get", "selected"], 4, 2],
      "line-dasharray": [3, 3],
    },
  });
  map.addLayer({
    id: "bases",
    type: "circle",
    source: "bases",
    paint: {
      "circle-radius": 6,
      "circle-color": "#1565c0",
      "circle-stroke-color": "#fff",
      "circle-stroke-width": 2,
    },
  });
  // Labels need glyphs, which only the installed basemap provides.
  if (labels) {
    map.addLayer({
      id: "zones-label",
      type: "symbol",
      source: "zones",
      layout: {
        "text-field": ["get", "name"],
        "text-font": ["Noto Sans Regular"],
        "text-size": 12,
      },
      paint: { "text-color": ["get", "colour"], "text-halo-color": "#fff", "text-halo-width": 1 },
    });
    map.addLayer({
      id: "bases-label",
      type: "symbol",
      source: "bases",
      layout: {
        "text-field": ["get", "name"],
        "text-font": ["Noto Sans Regular"],
        "text-size": 12,
        "text-offset": [0, 1.2],
        "text-anchor": "top",
      },
      paint: { "text-color": "#1565c0", "text-halo-color": "#fff", "text-halo-width": 1 },
    });
  }
  map.addLayer({
    id: "draft-fill",
    type: "fill",
    source: "draft",
    filter: ["==", ["geometry-type"], "Polygon"],
    paint: { "fill-color": "#6a1b9a", "fill-opacity": 0.2 },
  });
  map.addLayer({
    id: "draft-line",
    type: "line",
    source: "draft",
    filter: ["!=", ["geometry-type"], "Point"],
    paint: { "line-color": "#6a1b9a", "line-width": 2, "line-dasharray": [2, 1] },
  });
  map.addLayer({
    id: "draft-points",
    type: "circle",
    source: "draft",
    filter: ["==", ["geometry-type"], "Point"],
    paint: {
      "circle-radius": 5,
      "circle-color": "#6a1b9a",
      "circle-stroke-color": "#fff",
      "circle-stroke-width": 2,
    },
  });
  map.addLayer({
    id: "conflicts",
    type: "line",
    source: "conflicts",
    paint: {
      "line-color": ["match", ["get", "severity"], "critical", "#d50000", "#ff8f00"],
      "line-width": 3,
      "line-dasharray": [2, 1],
    },
  });
}

function setVisible(map: MapLibre, ids: string[], visible: boolean): void {
  for (const id of ids) {
    if (map.getLayer(id)) map.setLayoutProperty(id, "visibility", visible ? "visible" : "none");
  }
}

function setData(map: MapLibre, id: string, data: Collection): void {
  (map.getSource(id) as GeoJSONSource | undefined)?.setData(data);
}

interface MarkerEntry {
  marker: maplibregl.Marker;
  arrow: SVGSVGElement;
  label: HTMLDivElement;
  element: HTMLButtonElement;
}

function makeMarker(droneId: string, onSelect: (id: string) => void): MarkerEntry {
  const element = document.createElement("button");
  element.className = "drone-marker";
  element.type = "button";
  element.innerHTML =
    '<svg width="26" height="26" viewBox="0 0 26 26" aria-hidden="true">' +
    '<polygon points="13,2 21,23 13,18 5,23" stroke="#fff" stroke-width="1.5"/></svg>' +
    '<div class="drone-label"></div>';
  element.addEventListener("click", (event) => {
    event.stopPropagation();
    onSelect(droneId);
  });
  return {
    marker: new maplibregl.Marker({ element }),
    arrow: element.querySelector("svg") as SVGSVGElement,
    label: element.querySelector(".drone-label") as HTMLDivElement,
    element,
  };
}

export function MapView({
  aircraft,
  alerts,
  zones,
  bases,
  layers,
  selected,
  onSelect,
  selectedZone,
  draft,
  drawing,
  onMapClick,
}: Props) {
  const { lang, t } = useI18n();
  const container = useRef<HTMLDivElement>(null);
  const mapRef = useRef<MapLibre | null>(null);
  const markers = useRef(new Map<string, MarkerEntry>());
  const fittedToAircraft = useRef(false);
  const [basemap, setBasemap] = useState<BasemapInfo | null | undefined>(undefined);
  const [dark, setDark] = useState(darkQuery.matches);
  const [styleEpoch, setStyleEpoch] = useState(0);
  const onSelectRef = useRef(onSelect);
  useEffect(() => {
    onSelectRef.current = onSelect;
  }, [onSelect]);
  const onMapClickRef = useRef(onMapClick);
  useEffect(() => {
    onMapClickRef.current = onMapClick;
  }, [onMapClick]);

  // Is a basemap installed? The PMTiles header answers, and carries its bounds.
  useEffect(() => {
    addPmtilesProtocol();
    let cancelled = false;
    (async () => {
      try {
        const header = await new PMTiles(`${location.origin}${BASEMAP_URL}`).getHeader();
        let osmDataAsOf: string | null = null;
        try {
          const response = await fetch("/basemap/SOURCE.json");
          if (response.ok) {
            const source = (await response.json()) as { osm_data_as_of?: string };
            osmDataAsOf = source.osm_data_as_of ?? null;
          }
        } catch {
          osmDataAsOf = null;
        }
        if (!cancelled) {
          setBasemap({
            bounds: [
              [header.minLon, header.minLat],
              [header.maxLon, header.maxLat],
            ],
            osmDataAsOf,
          });
        }
      } catch {
        if (!cancelled) setBasemap(null);
      }
    })();
    const onScheme = () => setDark(darkQuery.matches);
    darkQuery.addEventListener("change", onScheme);
    return () => {
      cancelled = true;
      darkQuery.removeEventListener("change", onScheme);
    };
  }, []);

  // Create the map once.
  useEffect(() => {
    if (!container.current) return;
    const map = new maplibregl.Map({
      container: container.current,
      style: basemapStyle(null, "en", darkQuery.matches),
      // No coordinates in code (CLAUDE.md): the view is fitted to the
      // installed extract's own bounds, and then to the first aircraft.
      center: [0, 0],
      zoom: 1,
      attributionControl: { compact: false },
    });
    map.addControl(new maplibregl.NavigationControl(), "top-right");
    map.addControl(new maplibregl.ScaleControl({ unit: "metric" }), "bottom-right");
    map.on("style.load", () => setStyleEpoch((epoch) => epoch + 1));
    map.on("click", (event) => {
      const zone = map.getLayer("zones-fill")
        ? map.queryRenderedFeatures(event.point, { layers: ["zones-fill"] })[0]
        : undefined;
      const zoneId: unknown = zone?.properties?.id;
      onMapClickRef.current(
        [event.lngLat.lng, event.lngLat.lat],
        typeof zoneId === "string" ? zoneId : null,
      );
    });
    mapRef.current = map;
    const current = markers.current;
    return () => {
      current.clear();
      map.remove();
      mapRef.current = null;
    };
  }, []);

  // Basemap, language and colour scheme decide the style.
  useEffect(() => {
    const map = mapRef.current;
    if (!map || basemap === undefined) return;
    // A full reload, not a diff: a diff would drop the overlays without a
    // `style.load` to put them back.
    map.setStyle(basemapStyle(basemap, lang, dark), { diff: false });
  }, [basemap, lang, dark]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !basemap || fittedToAircraft.current) return;
    map.fitBounds(basemap.bounds, { padding: 20, animate: false });
  }, [basemap]);

  // Overlays, after every style load.
  useEffect(() => {
    const map = mapRef.current;
    if (!map || styleEpoch === 0) return;
    addOverlays(map);
  }, [styleEpoch]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || styleEpoch === 0) return;
    setData(map, "zones", zonesGeoJson(zones, selectedZone));
    setData(map, "bases", basesGeoJson(bases));
    setVisible(map, ["zones-fill", "zones-line", "zones-line-inactive"], layers.zones);
    setVisible(map, ["bases", "bases-label"], layers.bases);
    setVisible(map, ["zones-label"], layers.zones && layers.labels);
    setVisible(map, ["bases-label"], layers.bases && layers.labels);
  }, [styleEpoch, zones, bases, layers, selectedZone]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || styleEpoch === 0) return;
    setData(map, "draft", draftGeoJson(draft));
  }, [styleEpoch, draft]);

  // A zone selected in the list is brought into view, once per selection.
  const zonesRef = useRef(zones);
  zonesRef.current = zones;
  useEffect(() => {
    const map = mapRef.current;
    const zone = zonesRef.current.find((z) => z.id === selectedZone);
    if (!map || !zone) return;
    const ring = (zone.geometry as { coordinates?: number[][][] }).coordinates?.[0] ?? [];
    const lons = ring.map((p) => p[0] ?? 0);
    const lats = ring.map((p) => p[1] ?? 0);
    if (lons.length === 0) return;
    map.fitBounds(
      [
        [Math.min(...lons), Math.min(...lats)],
        [Math.max(...lons), Math.max(...lats)],
      ],
      { padding: 60, maxZoom: 15 },
    );
  }, [selectedZone]);

  // While drawing, a double click must not zoom, and the cursor says so.
  useEffect(() => {
    const map = mapRef.current;
    if (!map) return;
    if (drawing) map.doubleClickZoom.disable();
    else map.doubleClickZoom.enable();
    map.getCanvas().style.cursor = drawing ? "crosshair" : "";
  }, [drawing]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || styleEpoch === 0) return;
    setData(map, "conflicts", conflictsGeoJson(alerts, aircraft));
  }, [styleEpoch, alerts, aircraft]);

  // Aircraft markers. An aircraft with no position is listed, never drawn:
  // a marker left at the last known place would present stale as current.
  useEffect(() => {
    const map = mapRef.current;
    if (!map) return;
    const inConflict = new Map<string, Alert["severity"]>();
    for (const alert of alerts.values()) {
      for (const id of alert.drone_ids) {
        inConflict.set(id, worse(inConflict.get(id), alert.severity));
      }
    }
    for (const [id, entry] of markers.current) {
      if (!position(aircraft.get(id))) {
        entry.marker.remove();
        markers.current.delete(id);
      }
    }
    for (const [id, item] of aircraft) {
      const lngLat = position(item);
      if (!lngLat) continue;
      let entry = markers.current.get(id);
      if (!entry) {
        entry = makeMarker(id, (droneId) => onSelectRef.current(droneId));
        entry.marker.setLngLat(lngLat).addTo(map);
        markers.current.set(id, entry);
      }
      entry.marker.setLngLat(lngLat);
      // null heading means not reported: leave the arrow where it last was
      // rather than snapping north. 0 and "unknown" are different things.
      // Remote ID has no heading, only the track over the ground (P1-15).
      const pointing = item.data.heading_deg ?? item.data.track_deg ?? null;
      if (pointing !== null) entry.arrow.style.transform = `rotate(${pointing}deg)`;
      entry.element.dataset.source = item.data.source ?? "mavlink";
      entry.label.textContent = layers.labels ? (item.data.label ?? id.slice(0, 8)) : "";
      entry.element.dataset.alert = inConflict.get(id) ?? "";
      entry.element.dataset.selected = String(id === selected);
      entry.element.title = item.data.label ?? t("unnamed");
      if (!fittedToAircraft.current) {
        map.easeTo({ center: lngLat, zoom: 14 });
        fittedToAircraft.current = true;
      }
    }
  }, [aircraft, alerts, selected, layers.labels, t]);

  return (
    <div className="map-wrap">
      <div ref={container} className="map" />
      {basemap === null && <div className="map-notice">{t("no_basemap")}</div>}
    </div>
  );
}

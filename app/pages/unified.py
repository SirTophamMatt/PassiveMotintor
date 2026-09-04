"""Unified Map (public, read-only): every located layer on ONE map.

Pulls straight from each module's data layer and reuses the fire/roads rendering
helpers so styling matches the per-hazard pages. Layers are toggled with a
checklist (and Plotly's own legend). Flood gauges are intentionally absent —
they have no lat/lons yet (BoM KiWIS getStationList backlog item); everything
else with coordinates is here.

The map's `uirevision` is pinned so the 60-second auto-refresh never resets the
user's pan/zoom — you can sit zoomed on a fireground while data updates under you.
"""
import json
import logging

import pandas as pd
import plotly.graph_objects as go
from dash import Input, Output, dcc, html

from app import ui
from app.modules.fire import data as fire_data
from app.modules.flood import data as flood_data
from app.modules.power import data as power_data
from app.modules.roads import data as roads_data
from app.modules.storm import data as storm_data
from app.modules.weather import data as weather_data
from app.pages import fire as fire_page
from app.pages import roads as roads_page

log = logging.getLogger(__name__)

VIC_CENTER = {"lat": -37.0, "lon": 145.0}

LAYER_OPTIONS = [
    {"label": " Fire & warnings", "value": "fire"},
    {"label": " Flood gauges", "value": "flood"},
    {"label": " Road disruptions", "value": "roads"},
    {"label": " Storm cells", "value": "storm"},
    {"label": " Power outages", "value": "power"},
    {"label": " Rainfall (AWS)", "value": "rain"},
    {"label": " Weather observations (gusts)", "value": "wind"},
]
# Rainfall and weather observations are off by default — drawing all ~104 AWS
# stations at once buries the hazard layers this map exists to show. Both are
# one click away when wind or rain is the thing being briefed.
DEFAULT_LAYERS = ["fire", "flood", "roads", "storm", "power"]
# Only stations gusting at least this hard are drawn, so switching the layer on
# during a wind event highlights where it's actually blowing rather than
# stippling the whole state with calm sites.
WIND_LAYER_MIN_GUST_KMH = 40


def _val(row, col):
    """Scalar string for a cell, or None for missing/NaN — keeps hover clean."""
    v = row.get(col)
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    s = str(v).strip()
    return s if s and s.lower() not in ("nan", "none") else None


# --- per-layer RENDERERS ------------------------------------------------------
# Each takes a DataFrame and returns (list[Scattermap], list[fill layer]).
#
# They deliberately do NOT fetch. That is what lets Event Replay draw the same
# map from `history.state_at(t)` instead of from the current tables, without a
# second copy of the rendering code — the live map and the replay map are the
# same renderer fed different frames. Keep them pure: a fetch in here would
# silently make replay show live data.

def render_fire(df):
    if df is None or df.empty:
        return [], []
    df = df.copy()
    df["Kind"] = df.apply(fire_page._kind, axis=1)
    traces, fills = [], []
    located = df.dropna(subset=["latitude", "longitude"])
    for kind, colour in fire_page.KIND_COLOURS.items():
        sub = located[located["Kind"] == kind]
        if sub.empty:
            continue
        traces.append(go.Scattermap(
            mode="markers", lat=sub["latitude"], lon=sub["longitude"],
            name=kind, legendgroup="fire", marker=dict(size=11, color=colour),
            hoverinfo="text", text=[_fire_hover(r) for _, r in sub.iterrows()]))
    if "geometry" in df.columns:
        for kind, colour in fire_page.KIND_COLOURS.items():
            geoms = df.loc[df["Kind"] == kind, "geometry"].dropna().tolist()
            layer = fire_page._fill_layer(geoms, colour, 0.2)
            if layer:
                fills.append(layer)
    return traces, fills


def _fire_hover(r):
    txt = "<b>%s</b>" % (_val(r, "location") or _val(r, "headline") or "Incident")
    bits = [b for b in (_val(r, "category1"), _val(r, "status"), _val(r, "size")) if b]
    if bits:
        txt += "<br>" + " · ".join(bits)
    return txt


def render_flood(df):
    if df is None or df.empty:
        return [], []
    traces = []
    # Most severe first; below-flood gauges shown small/grey for network context.
    tiers = [("Major flooding", "#d62728", 13, "Gauge: Major"),
             ("Moderate flooding", "#ff7f0e", 12, "Gauge: Moderate"),
             ("Minor flooding", "#e6c700", 11, "Gauge: Minor"),
             ("Below flood level", "#5b8def", 6, "Gauge: below level")]
    for label, colour, size, legend in tiers:
        sub = df[df["label"] == label]
        if sub.empty:
            continue
        traces.append(go.Scattermap(
            mode="markers", lat=sub["latitude"], lon=sub["longitude"],
            name=legend, legendgroup="flood",
            marker=dict(size=size, color=colour),
            hoverinfo="text", text=[_flood_hover(r) for _, r in sub.iterrows()]))
    return traces, []


def _flood_hover(r):
    txt = "<b>%s</b>" % (_val(r, "station_name") or "Gauge")
    if pd.notna(r.get("height_m")):
        txt += "<br>%.2f m" % r["height_m"]
    if _val(r, "label"):
        txt += " · %s" % r["label"]
    return txt


def render_roads(df):
    if df is None or df.empty:
        return [], []
    df = df.copy()
    df["Kind"] = df["is_closure"].apply(roads_page._kind)
    traces = []
    for kind, colour in roads_page.KIND_COLOURS.items():
        sub = df[df["Kind"] == kind]
        if sub.empty:
            continue
        lats, lons, texts = [], [], []
        for _, r in sub.iterrows():
            raw = r.get("geometry")
            if not raw:
                continue
            try:
                geom = json.loads(raw)
            except (ValueError, TypeError):
                continue
            label = roads_page._hover(r)
            for seg in roads_page._line_segments(geom):
                for lon, lat in seg:
                    lons.append(lon); lats.append(lat); texts.append(label)
                lons.append(None); lats.append(None); texts.append(None)
        has_lines = bool(lats)
        if has_lines:
            traces.append(go.Scattermap(
                mode="lines", lat=lats, lon=lons, name="Road: %s" % kind,
                legendgroup="roads", line=dict(color=colour,
                width=5 if kind == "Closure" else 3),
                hoverinfo="text", text=texts))
        pts = sub[sub["geometry"].isna() | (sub["geometry"] == "")]
        pts = pts.dropna(subset=["latitude", "longitude"])
        if not pts.empty:
            traces.append(go.Scattermap(
                mode="markers", lat=pts["latitude"], lon=pts["longitude"],
                name="Road: %s" % kind, legendgroup="roads",
                showlegend=not has_lines, marker=dict(size=8, color=colour),
                hoverinfo="text",
                text=[roads_page._hover(r) for _, r in pts.iterrows()]))
    return traces, []


def render_storm(df):
    if df is None or df.empty:
        return [], []
    source_df = df
    df = df.dropna(subset=["latitude", "longitude"])
    traces, fills = [], []
    for cls, (_, colour) in storm_data.CLASS_STYLE.items():
        sub = df[df["classification"] == cls]
        if sub.empty:
            continue
        sizes = [max(10, min(30, (a ** 0.5) + 8)) if pd.notna(a) else 12
                 for a in sub["area_km2"]]
        traces.append(go.Scattermap(
            mode="markers", lat=sub["latitude"], lon=sub["longitude"],
            name="Storm: %s" % cls, legendgroup="storm",
            marker=dict(size=sizes, color=colour),
            hoverinfo="text", text=[_storm_hover(r) for _, r in sub.iterrows()]))
    # Impact polygons come from the frame's own column rather than a fresh
    # impact_featurecollection() call, so a replayed frame draws the polygons
    # that existed at that moment instead of today's.
    features = []
    if "impact_geojson" in source_df.columns:
        for raw in source_df["impact_geojson"].dropna():
            try:
                features.append(json.loads(raw))
            except (ValueError, TypeError):
                continue
    if features:
        fills.append({"sourcetype": "geojson", "type": "fill", "below": "traces",
                      "color": "#d62728", "opacity": 0.15,
                      "source": {"type": "FeatureCollection",
                                 "features": features}})
    return traces, fills


def _storm_hover(r):
    txt = "<b>Storm cell %s</b>" % (_val(r, "cell_id") or "")
    bits = []
    if _val(r, "classification"):
        bits.append(str(r["classification"]).upper())
    if pd.notna(r.get("intensity_score")):
        bits.append("score %.0f" % r["intensity_score"])
    if pd.notna(r.get("area_km2")):
        bits.append("~%.0f km²" % r["area_km2"])
    if bits:
        txt += "<br>" + " · ".join(bits)
    return txt


def render_power(df):
    if df is None or df.empty:
        return [], []
    df = df.dropna(subset=["latitude", "longitude"])
    if df.empty:
        return [], []
    sizes = [max(7, min(30, (float(c) ** 0.5))) if pd.notna(c) and c else 7
             for c in df["customers_off"]]
    text = ["<b>%s</b><br>%s off supply%s" % (
                _val(r, "location") or "Outage",
                "{:,}".format(int(r["customers_off"])) if pd.notna(r.get("customers_off")) else "?",
                (" · " + _val(r, "type")) if _val(r, "type") else "")
            for _, r in df.iterrows()]
    return [go.Scattermap(
        mode="markers", lat=df["latitude"], lon=df["longitude"],
        name="Power outage", legendgroup="power",
        marker=dict(size=sizes, color="#7048e8"), hoverinfo="text",
        text=text)], []


def render_rain(df):
    if df is None or df.empty:
        return [], []
    df = df.dropna(subset=["latitude", "longitude"])
    df = df[df["rain_since_9am_mm"].fillna(0) > 0]
    if df.empty:
        return [], []
    cmax = max(10, float(df["rain_since_9am_mm"].max()))
    text = ["<b>%s</b><br>%.1f mm since 9am" % (_val(r, "name") or "AWS",
                                                r["rain_since_9am_mm"])
            for _, r in df.iterrows()]
    return [go.Scattermap(
        mode="markers", lat=df["latitude"], lon=df["longitude"],
        name="Rain since 9am", legendgroup="rain",
        marker=dict(size=9, color=df["rain_since_9am_mm"], colorscale="Blues",
                    cmin=0, cmax=cmax, showscale=False),
        hoverinfo="text", text=text)], []


def render_wind(df):
    """AWS wind gusts — the observation that matters alongside storms, fires
    and warnings. Uses the AWS station coordinates already in the registry; no
    new geocoding."""
    if df is None or df.empty or "wind_gust_kmh" not in df.columns:
        return [], []
    df = df.dropna(subset=["latitude", "longitude", "wind_gust_kmh"])
    df = df[df["wind_gust_kmh"] >= WIND_LAYER_MIN_GUST_KMH]
    if df.empty:
        return [], []
    cmax = max(80.0, float(df["wind_gust_kmh"].max()))
    text = ["<b>%s</b><br>Gust %.0f km/h%s<br>Observed %s" % (
                _val(r, "name") or "AWS", r["wind_gust_kmh"],
                (" · wind %s %.0f km/h" % (r["wind_direction"], r["wind_speed_kmh"]))
                if _val(r, "wind_direction") and pd.notna(r.get("wind_speed_kmh"))
                else "",
                _val(r, "obs_time") or "—")
            for _, r in df.iterrows()]
    sizes = [max(8, min(26, 8 + (float(g) - WIND_LAYER_MIN_GUST_KMH) * 0.35))
             for g in df["wind_gust_kmh"]]
    return [go.Scattermap(
        mode="markers", lat=df["latitude"], lon=df["longitude"],
        name="Wind gust ≥ %d km/h" % WIND_LAYER_MIN_GUST_KMH,
        legendgroup="wind",
        marker=dict(size=sizes, color=df["wind_gust_kmh"], colorscale="YlOrRd",
                    cmin=WIND_LAYER_MIN_GUST_KMH, cmax=cmax, showscale=False),
        hoverinfo="text", text=text)], []


# Draw order (back to front) and the renderer for each layer.
RENDERERS = {
    "fire": render_fire,
    "flood": render_flood,
    "roads": render_roads,
    "storm": render_storm,
    "power": render_power,
    "rain": render_rain,
    "wind": render_wind,
}
LAYER_ORDER = ("fire", "flood", "roads", "storm", "power", "rain", "wind")

# Where the LIVE map gets each layer's data. Replay passes its own provider
# returning historical frames for the same keys.
LIVE_SOURCES = {
    "fire": fire_data.active_incidents,
    "flood": flood_data.map_gauges,
    "roads": roads_data.active_disruptions,
    "storm": storm_data.active_cells,
    "power": power_data.active_outages,
    "rain": weather_data.latest_aws_rainfall,
    "wind": weather_data.latest_aws_observations,
}


def live_source(key):
    """Current data for one layer key."""
    fetch = LIVE_SOURCES.get(key)
    return fetch() if fetch else None


def build_layers(on, source=live_source):
    """(traces, fills) for the enabled layers, in draw order.

    `source(key)` supplies each layer's DataFrame — current state for the live
    map, `history.state_at(t)` for replay. Only enabled layers are fetched, and
    one layer failing never costs the rest of the map.
    """
    traces, fills = [], []
    for key in LAYER_ORDER:
        if key not in on:
            continue
        try:
            df = source(key)
        except Exception:
            log.exception("Unified map: %s layer data unavailable", key)
            continue
        try:
            layer_traces, layer_fills = RENDERERS[key](df)
        except Exception:
            log.exception("Unified map: %s layer failed to render", key)
            continue
        traces += layer_traces
        fills += layer_fills
    return traces, fills


def map_figure(on, dark, source=live_source, center=None, zoom=5.4,
               uirevision="unified-map"):
    """The shared map figure. Live and replay differ only in `source`."""
    fig = go.Figure()
    traces, fills = build_layers(on, source)
    for trace in traces:
        fig.add_trace(trace)
    if not traces:
        # With no map traces Plotly falls back to a cartesian plot and draws
        # bare numbered axes where the map should be. One empty Scattermap
        # keeps it a map — which is what "nothing was happening here" should
        # look like, and Replay hits this on every quiet moment.
        fig.add_trace(go.Scattermap(lat=[], lon=[], mode="markers",
                                       showlegend=False, hoverinfo="skip"))
    map_cfg = dict(style="open-street-map", center=center or VIC_CENTER, zoom=zoom)
    if fills:
        map_cfg["layers"] = fills
    fig.update_layout(
        map=map_cfg,
        legend=dict(orientation="h", y=1.02, font=dict(size=11)),
        # Pin the view across auto-refreshes so panning/zooming isn't reset.
        uirevision=uirevision)
    return ui.apply_theme(fig, dark)


def layout():
    return html.Div([
        html.H2("Unified Map"),
        html.Div([
            html.Div([
                html.H4("Layers"),
                dcc.Checklist(id="unified-layers", options=LAYER_OPTIONS,
                              value=DEFAULT_LAYERS,
                              labelStyle={"display": "block"}),
                html.Div("Legend entries are also clickable to isolate a layer. "
                         "Flood gauges are matched to BoM Water Data Online "
                         "coordinates (~77% of gauges; the rest are being "
                         "filled in).", className="muted",
                         style={"marginTop": "8px", "fontSize": "12px"}),
            ], className="panel"),
        ], className="panel-row"),
        dcc.Interval(id="unified-interval", interval=60_000, n_intervals=0),
        html.Div(id="unified-summary", className="muted", style={"margin": "10px 0"}),
        html.Div(id="unified-kpis", className="kpi-row"),
        html.Div(dcc.Graph(id="unified-map", style={"height": "78vh"},
                           config=ui.MAP_CONFIG),
                 className="graph-card"),
    ])


def register_callbacks(app):
    @app.callback(
        Output("unified-summary", "children"),
        Output("unified-kpis", "children"),
        Output("unified-map", "figure"),
        Input("unified-interval", "n_intervals"),
        Input("unified-layers", "value"),
        Input("theme-store", "data"))
    def refresh(_, on, dark):
        on = on if on is not None else DEFAULT_LAYERS
        dark = bool(dark)

        fire_c = fire_data.latest_counts()
        roads_c = roads_data.latest_counts()
        storm_c = storm_data.latest_counts()
        power_t = power_data.latest_totals() or {}
        warnings = fire_c["emergency"] + fire_c["watch_act"] + fire_c["advice"]
        customers_off = power_t.get("customers_off")
        flooding = flood_data.flooding_station_count()

        kpis = [
            ui.kpi_card("Active Fires", str(fire_c["active_fires"]),
                        "#ff5722" if fire_c["active_fires"] else "#2ca02c"),
            ui.kpi_card("Fire Warnings", str(warnings),
                        "#d62728" if fire_c["emergency"] else
                        ("#ff7f0e" if warnings else None)),
            ui.kpi_card("Gauges ≥ Minor", str(flooding),
                        "#e6c700" if flooding else None),
            ui.kpi_card("Road Closures", str(roads_c["closures"]),
                        "#d62728" if roads_c["closures"] else None),
            ui.kpi_card("Storm Cells (strong)", str(storm_c["strong"]),
                        "#d62728" if storm_c["strong"] else None),
            ui.kpi_card("Customers Off Supply",
                        "{:,}".format(int(customers_off)) if customers_off is not None else "—",
                        "#7048e8" if customers_off else None),
        ]

        summary = ("One map, live layers: %d fire/incident event(s), %d gauge(s) "
                   "at/above minor, %d road disruption(s), %d storm cell(s)." % (
                       fire_c["total"], flooding, roads_c["total"], storm_c["total"]))
        return summary, kpis, map_figure(on, dark)

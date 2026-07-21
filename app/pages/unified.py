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

import pandas as pd
import plotly.graph_objects as go
from dash import Input, Output, dcc, html

from app import ui
from app.modules.fire import data as fire_data
from app.modules.power import data as power_data
from app.modules.roads import data as roads_data
from app.modules.storm import data as storm_data
from app.modules.weather import data as weather_data
from app.pages import fire as fire_page
from app.pages import roads as roads_page

VIC_CENTER = {"lat": -37.0, "lon": 145.0}

LAYER_OPTIONS = [
    {"label": " Fire & warnings", "value": "fire"},
    {"label": " Road disruptions", "value": "roads"},
    {"label": " Storm cells", "value": "storm"},
    {"label": " Power outages", "value": "power"},
    {"label": " Rainfall (AWS)", "value": "rain"},
]
# Rainfall is off by default (it's the busiest layer); the rest are on.
DEFAULT_LAYERS = ["fire", "roads", "storm", "power"]


def _val(row, col):
    """Scalar string for a cell, or None for missing/NaN — keeps hover clean."""
    v = row.get(col)
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    s = str(v).strip()
    return s if s and s.lower() not in ("nan", "none") else None


# --- per-layer builders: each returns (list[Scattermapbox], list[fill layer]) --

def _fire_layer(on):
    if "fire" not in on:
        return [], []
    df = fire_data.active_incidents()
    if df.empty:
        return [], []
    df = df.copy()
    df["Kind"] = df.apply(fire_page._kind, axis=1)
    traces, fills = [], []
    located = df.dropna(subset=["latitude", "longitude"])
    for kind, colour in fire_page.KIND_COLOURS.items():
        sub = located[located["Kind"] == kind]
        if sub.empty:
            continue
        traces.append(go.Scattermapbox(
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


def _roads_layer(on):
    if "roads" not in on:
        return [], []
    df = roads_data.active_disruptions()
    if df.empty:
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
            traces.append(go.Scattermapbox(
                mode="lines", lat=lats, lon=lons, name="Road: %s" % kind,
                legendgroup="roads", line=dict(color=colour,
                width=5 if kind == "Closure" else 3),
                hoverinfo="text", text=texts))
        pts = sub[sub["geometry"].isna() | (sub["geometry"] == "")]
        pts = pts.dropna(subset=["latitude", "longitude"])
        if not pts.empty:
            traces.append(go.Scattermapbox(
                mode="markers", lat=pts["latitude"], lon=pts["longitude"],
                name="Road: %s" % kind, legendgroup="roads",
                showlegend=not has_lines, marker=dict(size=8, color=colour),
                hoverinfo="text",
                text=[roads_page._hover(r) for _, r in pts.iterrows()]))
    return traces, []


def _storm_layer(on):
    if "storm" not in on:
        return [], []
    df = storm_data.active_cells()
    if df.empty:
        return [], []
    df = df.dropna(subset=["latitude", "longitude"])
    traces, fills = [], []
    for cls, (_, colour) in storm_data.CLASS_STYLE.items():
        sub = df[df["classification"] == cls]
        if sub.empty:
            continue
        sizes = [max(10, min(30, (a ** 0.5) + 8)) if pd.notna(a) else 12
                 for a in sub["area_km2"]]
        traces.append(go.Scattermapbox(
            mode="markers", lat=sub["latitude"], lon=sub["longitude"],
            name="Storm: %s" % cls, legendgroup="storm",
            marker=dict(size=sizes, color=colour),
            hoverinfo="text", text=[_storm_hover(r) for _, r in sub.iterrows()]))
    fc = storm_data.impact_featurecollection()
    if fc.get("features"):
        fills.append({"sourcetype": "geojson", "type": "fill", "below": "traces",
                      "color": "#d62728", "opacity": 0.15, "source": fc})
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


def _power_layer(on):
    if "power" not in on:
        return [], []
    df = power_data.active_outages()
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
    return [go.Scattermapbox(
        mode="markers", lat=df["latitude"], lon=df["longitude"],
        name="Power outage", legendgroup="power",
        marker=dict(size=sizes, color="#7048e8"), hoverinfo="text",
        text=text)], []


def _rain_layer(on):
    if "rain" not in on:
        return [], []
    df = weather_data.latest_aws_rainfall()
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
    return [go.Scattermapbox(
        mode="markers", lat=df["latitude"], lon=df["longitude"],
        name="Rain since 9am", legendgroup="rain",
        marker=dict(size=9, color=df["rain_since_9am_mm"], colorscale="Blues",
                    cmin=0, cmax=cmax, showscale=False),
        hoverinfo="text", text=text)], []


_BUILDERS = (_fire_layer, _roads_layer, _storm_layer, _power_layer, _rain_layer)


def _map_figure(on, dark):
    fig = go.Figure()
    fills = []
    for build in _BUILDERS:
        traces, layer_fills = build(on)
        for t in traces:
            fig.add_trace(t)
        fills += layer_fills
    mapbox = dict(style="open-street-map", center=VIC_CENTER, zoom=5.4)
    if fills:
        mapbox["layers"] = fills
    fig.update_layout(
        mapbox=mapbox,
        legend=dict(orientation="h", y=1.02, font=dict(size=11)),
        # Pin the view across auto-refreshes so panning/zooming isn't reset.
        uirevision="unified-map")
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
                         "Flood gauges aren't shown yet (no coordinates in the "
                         "BoM feed).", className="muted",
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

        kpis = [
            ui.kpi_card("Active Fires", str(fire_c["active_fires"]),
                        "#ff5722" if fire_c["active_fires"] else "#2ca02c"),
            ui.kpi_card("Fire Warnings", str(warnings),
                        "#d62728" if fire_c["emergency"] else
                        ("#ff7f0e" if warnings else None)),
            ui.kpi_card("Road Closures", str(roads_c["closures"]),
                        "#d62728" if roads_c["closures"] else None),
            ui.kpi_card("Storm Cells (strong)", str(storm_c["strong"]),
                        "#d62728" if storm_c["strong"] else None),
            ui.kpi_card("Customers Off Supply",
                        "{:,}".format(int(customers_off)) if customers_off is not None else "—",
                        "#7048e8" if customers_off else None),
        ]

        summary = ("One map, live layers: %d fire/incident event(s), %d road "
                   "disruption(s), %d storm cell(s)." % (
                       fire_c["total"], roads_c["total"], storm_c["total"]))
        return summary, kpis, _map_figure(on, dark)

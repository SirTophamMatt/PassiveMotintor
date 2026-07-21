"""Road Disruptions page (public, read-only): VicRoads / Transport Victoria.

Shows headline counts, a state map (closure/other markers + line/area overlays),
a table of active disruptions, and a counts-over-time trend. Collection is
always-on and managed from the Admin page; it stays idle (log-and-skip) until an
API key + feed URL are set in config/Settings.
"""
import json

import pandas as pd
import plotly.express as px
from dash import Input, Output, dash_table, dcc, html

from app import ui
from app.collector import manager
from app.modules.roads import data as roads_data

VIC_CENTER = {"lat": -37.0, "lon": 145.0}

KIND_COLOURS = {"Closure": "#d62728", "Other disruption": "#ff7f0e"}

TABLE_COLUMNS = [
    ("road_name", "Road"), ("location", "Location"),
    ("disruption_type", "Type"), ("direction", "Direction"),
    ("lanes_affected", "Lanes"), ("ses_region", "SES Region"),
    ("status", "Status"), ("updated", "Updated"),
]


def _kind(is_closure):
    return "Closure" if is_closure else "Other disruption"


def layout():
    return html.Div([
        html.H2("Road Disruptions"),
        html.Div([
            html.Div([
                html.H4("Collector"),
                html.Div(id="roads-collector-status"),
                html.Div("Needs a VicRoads Data Exchange API key + feed URL "
                         "(Settings). Idle until both are set.",
                         className="muted",
                         style={"marginTop": "8px", "fontSize": "12px"}),
            ], className="panel"),
            html.Div([
                html.H4("Filters"),
                dcc.Checklist(
                    id="roads-closures-only",
                    options=[{"label": " Full closures only", "value": "yes"}],
                    value=[]),
            ], className="panel"),
        ], className="panel-row"),
        dcc.Interval(id="roads-interval", interval=60_000, n_intervals=0),
        html.Div(id="roads-summary", className="muted", style={"margin": "10px 0"}),
        html.Div(id="roads-kpis", className="kpi-row"),
        html.Div(dcc.Graph(id="roads-map", style={"height": "600px"},
                           config=ui.MAP_CONFIG),
                 className="graph-card"),
        html.H3("Active Disruptions", style={"marginTop": "16px"}),
        dash_table.DataTable(
            id="roads-table",
            page_size=15,
            filter_action="native",
            sort_action="native",
        ),
        html.Div(dcc.Graph(id="roads-trend"), className="graph-card",
                 style={"marginTop": "16px"}),
    ])


def _geom_layers(df):
    """Line overlays for LineString disruptions + fill overlays for polygons,
    coloured by kind. Sits below the marker traces."""
    if df.empty or "geometry" not in df.columns:
        return []
    line_feats = {k: [] for k in KIND_COLOURS}
    fill_feats = {k: [] for k in KIND_COLOURS}
    for _, row in df.iterrows():
        raw = row.get("geometry")
        if not raw:
            continue
        try:
            geom = json.loads(raw)
        except (ValueError, TypeError):
            continue
        kind = _kind(row.get("is_closure"))
        feat = {"type": "Feature", "properties": {}, "geometry": geom}
        t = (geom or {}).get("type")
        if t in ("LineString", "MultiLineString"):
            line_feats[kind].append(feat)
        elif t in ("Polygon", "MultiPolygon"):
            fill_feats[kind].append(feat)

    layers = []
    for kind, colour in KIND_COLOURS.items():
        if fill_feats[kind]:
            layers.append({
                "sourcetype": "geojson", "type": "fill", "below": "traces",
                "color": colour, "opacity": 0.25,
                "source": {"type": "FeatureCollection", "features": fill_feats[kind]}})
        if line_feats[kind]:
            layers.append({
                "sourcetype": "geojson", "type": "line", "below": "traces",
                "color": colour, "opacity": 0.85, "line": {"width": 4},
                "source": {"type": "FeatureCollection", "features": line_feats[kind]}})
    return layers


def _map_figure(df, dark):
    located = df.dropna(subset=["latitude", "longitude"]) if not df.empty else df
    if located is None or located.empty:
        fig = px.scatter_mapbox(
            pd.DataFrame({"latitude": [], "longitude": []}),
            lat="latitude", lon="longitude", zoom=5.2, center=VIC_CENTER,
            mapbox_style="open-street-map", title="Active Road Disruptions")
    else:
        plot = located.copy()
        plot["Kind"] = plot["is_closure"].apply(_kind)
        fig = px.scatter_mapbox(
            plot, lat="latitude", lon="longitude", color="Kind",
            color_discrete_map=KIND_COLOURS,
            category_orders={"Kind": ["Closure", "Other disruption"]},
            hover_name="road_name",
            hover_data={"location": True, "disruption_type": True,
                        "status": True, "latitude": False, "longitude": False,
                        "Kind": False},
            zoom=5.2, center=VIC_CENTER, mapbox_style="open-street-map",
            title="Active Road Disruptions")
        fig.update_traces(marker=dict(size=11))
    layers = _geom_layers(df)
    if layers:
        fig.update_layout(mapbox_layers=layers)  # magic-underscore: keeps style/zoom
    fig.update_layout(legend=dict(orientation="h", y=1.02))
    return ui.apply_theme(fig, dark)


def _trend_figure(df, dark):
    if df.empty:
        return ui.apply_theme(
            px.line(title="Active disruptions over time (no history yet)"), dark)
    series = {"total_active": "All disruptions", "closures": "Full closures",
              "other_disruptions": "Other"}
    colours = {"All disruptions": "#5b8def", "Full closures": "#d62728",
               "Other": "#ff7f0e"}
    fig = px.line(df, x="timestamp", y=list(series),
                  title="Road disruptions over time",
                  labels={"timestamp": "Time", "value": "Count", "variable": ""})
    fig.for_each_trace(lambda t: t.update(name=series.get(t.name, t.name)))
    for tr in fig.data:
        if tr.name in colours:
            tr.update(line=dict(color=colours[tr.name]))
    fig.update_layout(height=320, legend=dict(orientation="h", y=1.12))
    return ui.apply_theme(fig, dark)


def register_callbacks(app):
    @app.callback(
        Output("roads-collector-status", "children"),
        Input("roads-interval", "n_intervals"))
    def collector_status(_):
        s = manager.status()["roads"]
        parts = [html.Strong("Status: "), ui.status_pill(s["running"])]
        if s.get("last_run"):
            parts.append(html.Span(
                f" — last cycle {s['last_run']} ({s.get('runs', 0)} total)"))
        if s.get("last_error"):
            parts.append(html.Div(f"⚠ {s['last_error']}", className="error-text",
                                  style={"marginTop": "4px"}))
        return html.Div(parts)

    @app.callback(
        Output("roads-summary", "children"),
        Output("roads-kpis", "children"),
        Output("roads-map", "figure"),
        Output("roads-table", "data"),
        Output("roads-table", "columns"),
        Output("roads-table", "style_table"),
        Output("roads-table", "style_cell"),
        Output("roads-table", "style_header"),
        Output("roads-table", "style_data"),
        Output("roads-trend", "figure"),
        Input("roads-interval", "n_intervals"),
        Input("roads-closures-only", "value"),
        Input("theme-store", "data"))
    def refresh(_, closures_only, dark):
        dark = bool(dark)
        styles = ui.table_styles(dark)
        style_out = (styles["style_table"], styles["style_cell"],
                     styles.get("style_header", {}), styles.get("style_data", {}))

        counts = roads_data.latest_counts()
        kpis = [
            ui.kpi_card("Full Closures", str(counts["closures"]),
                        "#d62728" if counts["closures"] else "#2ca02c"),
            ui.kpi_card("Other Disruptions", str(counts["other"]),
                        "#ff7f0e" if counts["other"] else None),
            ui.kpi_card("Total Active", str(counts["total"])),
        ]

        df = roads_data.active_disruptions(
            closures_only=bool(closures_only))
        map_fig = _map_figure(df, dark)
        trend_fig = _trend_figure(roads_data.load_road_timeseries(), dark)

        cycles, last_hb = roads_data.heartbeat_summary()
        summary = f"{counts['total']} active disruption(s) state-wide. "
        if cycles:
            summary += f"Monitor ran {cycles} cycle(s), last {last_hb}."
        else:
            summary += ("No cycles yet — set the VicRoads API key + feed URL in "
                        "Settings to begin collecting.")

        columns = [{"name": name, "id": col} for col, name in TABLE_COLUMNS]
        if df.empty:
            table_data = []
        else:
            table_df = df[[c for c, _ in TABLE_COLUMNS]].copy()
            table_df["updated"] = pd.to_datetime(
                table_df["updated"], errors="coerce").dt.strftime("%d %b %H%Mhrs")
            table_data = table_df.to_dict("records")
        return (summary, kpis, map_fig, table_data, columns, *style_out,
                trend_fig)

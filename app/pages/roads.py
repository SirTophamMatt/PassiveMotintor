"""Road Disruptions page (public, read-only): VicRoads / Transport Victoria.

Shows headline counts, a state map (closure/other markers + line/area overlays),
a table of active disruptions, and a counts-over-time trend. Collection is
always-on and managed from the Admin page; it stays idle (log-and-skip) until an
API key + feed URL are set in config/Settings.
"""
import json

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
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


def _line_segments(geom):
    """Yield each LineString part's [[lon, lat], ...] from a v3 geometry
    (LineString, or defensively MultiLineString)."""
    t = (geom or {}).get("type")
    if t == "LineString":
        yield geom.get("coordinates") or []
    elif t == "MultiLineString":
        for part in geom.get("coordinates") or []:
            yield part


def _hover(row):
    """Hover label so a highlighted road is still identifiable without a marker."""
    txt = "<b>%s</b>" % (row.get("road_name") or "Road")
    if row.get("location"):
        txt += "<br>%s" % row["location"]
    bits = [str(row[c]) for c in ("disruption_type", "status", "direction")
            if row.get(c)]
    if bits:
        txt += "<br>%s" % " · ".join(bits)
    if row.get("ses_region"):
        txt += "<br>SES: %s" % row["ses_region"]
    return txt


def _map_figure(df, dark):
    """Highlight each impacted road as a coloured LINE (closures thicker/red,
    other disruptions amber). Only Point-only disruptions — which have no road
    segment in the feed — fall back to a marker dot."""
    fig = go.Figure()
    if df is not None and not df.empty:
        df = df.copy()
        df["Kind"] = df["is_closure"].apply(_kind)
        for kind, colour in KIND_COLOURS.items():
            sub = df[df["Kind"] == kind]
            if sub.empty:
                continue

            # --- impacted road segments (LineString geometry) -> highlight ----
            lats, lons, texts = [], [], []
            for _, r in sub.iterrows():
                raw = r.get("geometry")
                if not raw:
                    continue
                try:
                    geom = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                label = _hover(r)
                for seg in _line_segments(geom):
                    for lon, lat in seg:
                        lons.append(lon); lats.append(lat); texts.append(label)
                    lons.append(None); lats.append(None); texts.append(None)
            has_lines = bool(lats)
            if has_lines:
                fig.add_trace(go.Scattermapbox(
                    mode="lines", lat=lats, lon=lons, name=kind,
                    legendgroup=kind, line=dict(color=colour,
                    width=5 if kind == "Closure" else 3),
                    hoverinfo="text", text=texts))

            # --- point-only disruptions (no road segment) -> marker dot -------
            pts = sub[sub["geometry"].isna() | (sub["geometry"] == "")]
            pts = pts.dropna(subset=["latitude", "longitude"])
            if not pts.empty:
                fig.add_trace(go.Scattermapbox(
                    mode="markers", lat=pts["latitude"], lon=pts["longitude"],
                    name=kind, legendgroup=kind, showlegend=not has_lines,
                    marker=dict(size=10, color=colour), hoverinfo="text",
                    text=[_hover(r) for _, r in pts.iterrows()]))

    fig.update_layout(
        title="Active Road Disruptions",
        mapbox=dict(style="open-street-map", center=VIC_CENTER, zoom=5.2),
        legend=dict(orientation="h", y=1.02))
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

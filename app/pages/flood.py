"""Flood Monitor page (public, read-only): observation table and station graphs.

Collection is always-on and controlled from the Admin page. This page views the
data by event tag (a named date range) or the live window.
"""
from datetime import datetime, timedelta

import pandas as pd
import plotly.graph_objects as go
from dash import Input, Output, dash_table, dcc, html

from app import tags as tag_store
from app import ui
from app.modules.flood import data as flood_data
from app.pages import station as station_page

LIVE_DAYS = 7  # how much recent data the "Live" view shows

TABLE_COLUMNS = [
    ("catchment", "Catchment"), ("station_name", "Station"),
    ("time_day", "Time/Day"), ("height_m", "Height (m)"),
    ("tendency", "Tendency"), ("classification", "Classification"),
]

# Rendering hundreds of Plotly graphs at once locks the browser, so graphs are
# capped; flooding stations always sort first and are never cut off in practice.
MAX_GRAPHS = 24


def _tag_options():
    """Selector options: the live window first, then each event tag."""
    opts = [{"label": f"Live — last {LIVE_DAYS} days", "value": "live"}]
    for tag in tag_store.list_tags():
        span = tag["start_ts"][:10] + " → " + (
            tag["end_ts"][:10] if tag.get("end_ts") else "ongoing")
        opts.append({"label": f"{tag['name']}  ({span})", "value": str(tag["id"])})
    return opts


def _resolve_range(value):
    """Map a selector value to (start_ts, end_ts) strings."""
    if value and value != "live":
        tag = tag_store.get_tag(int(value))
        if tag:
            return tag_store.resolve_range(tag)
    end = datetime.now()
    start = end - timedelta(days=LIVE_DAYS)
    fmt = "%Y-%m-%d %H:%M:%S"
    return start.strftime(fmt), end.strftime(fmt)


def layout():
    return html.Div([
        html.H2("Flood Monitor"),
        html.Div([
            html.Div([
                html.H4("View"),
                html.Label("Event / range"),
                dcc.Dropdown(id="flood-event-selector",
                             options=_tag_options(), value="live",
                             clearable=False, className="dropdown"),
                html.Label("Catchment"),
                dcc.Dropdown(id="flood-catchment-filter", placeholder="All catchments",
                             clearable=True, className="dropdown"),
                dcc.Checklist(id="flood-only-toggle",
                              options=[{"label": " Show flooding stations only", "value": "flooding"}],
                              value=[]),
                html.Div("Collection runs automatically — manage it and create "
                         "event tags on the Admin page.", className="muted",
                         style={"marginTop": "8px", "fontSize": "12px"}),
            ], className="panel"),
        ], className="panel-row"),
        dcc.Interval(id="flood-interval", interval=60_000, n_intervals=0),
        html.Div(id="flood-summary", className="muted", style={"margin": "10px 0"}),
        dash_table.DataTable(
            id="flood-table",
            page_size=15,
            filter_action="native",
            sort_action="native",
        ),
        html.Div(id="flood-graphs", className="graph-grid"),
    ])


def _station_figure(station_df, station, label, levels, dark):
    fig = go.Figure()
    # lines+markers so a station with a single observation still shows a dot
    # (a line needs two points to be visible).
    fig.add_trace(go.Scatter(
        x=station_df["timestamp"], y=station_df["height_m"],
        mode="lines+markers", name="Observed", line_shape="spline"))
    if levels:
        for key, colour in (("minor", "#e6c700"), ("moderate", "#ff7f0e"), ("major", "#d62728")):
            value = levels.get(key)
            if pd.notna(value):
                fig.add_hline(y=value, line=dict(color=colour, dash="dash"),
                              annotation_text=key.title(),
                              annotation_position="top left")
    fig.update_layout(
        title=f"{station} — {label}",
        xaxis_title="Time", yaxis_title="Height (m)",
        height=380, showlegend=False)
    # A single point makes Plotly zoom to a sub-second window; give it a
    # readable ±1h range instead.
    if len(station_df) == 1:
        ts = pd.to_datetime(station_df["timestamp"].iloc[0])
        fig.update_xaxes(range=[ts - pd.Timedelta(hours=1),
                                ts + pd.Timedelta(hours=1)])
    return ui.apply_theme(fig, dark)


def register_callbacks(app):
    @app.callback(
        Output("flood-catchment-filter", "options"),
        Input("flood-event-selector", "value"))
    def update_catchments(value):
        start, end = _resolve_range(value)
        return [{"label": c, "value": c}
                for c in flood_data.get_catchments(start, end)]

    @app.callback(
        Output("flood-summary", "children"),
        Output("flood-table", "data"),
        Output("flood-table", "columns"),
        Output("flood-table", "style_table"),
        Output("flood-table", "style_cell"),
        Output("flood-table", "style_header"),
        Output("flood-table", "style_data"),
        Output("flood-graphs", "children"),
        Input("flood-interval", "n_intervals"),
        Input("flood-event-selector", "value"),
        Input("flood-catchment-filter", "value"),
        Input("flood-only-toggle", "value"),
        Input("theme-store", "data"))
    def refresh(_, value, catchment, flooding_only, dark):
        styles = ui.table_styles(bool(dark))
        style_out = (styles["style_table"], styles["style_cell"],
                     styles.get("style_header", {}), styles.get("style_data", {}))
        start, end = _resolve_range(value)
        df = flood_data.load_observations(start, end, catchment)
        if df.empty:
            return ("No observations recorded in this range yet.",
                    [], [], *style_out, [])

        levels_map = flood_data.load_flood_levels()

        # latest reading per station for the table; station names link to
        # their detail page.
        latest = df.sort_values("timestamp").groupby("station_name").tail(1)
        table_df = latest[[c for c, _ in TABLE_COLUMNS]].copy()
        table_df["station_name"] = table_df["station_name"].map(
            lambda s: f"[{s}]({station_page.path_for(s)})")
        columns = [{"name": name, "id": col,
                    **({"presentation": "markdown"} if col == "station_name" else {})}
                   for col, name in TABLE_COLUMNS]

        graphs = []
        for station in df["station_name"].dropna().unique():
            station_df = df[df["station_name"] == station].dropna(subset=["height_m"])
            if station_df.empty:
                continue
            station_df = station_df.sort_values("timestamp")
            levels = levels_map.get(str(station).strip().lower())
            latest_height = station_df["height_m"].iloc[-1]
            priority, label, colour = flood_data.classify_station(latest_height, levels)
            if flooding_only and priority >= 4:
                continue
            graphs.append((priority, station, html.Div([
                dcc.Link("Gauge details, impacts & briefing →",
                         href=station_page.path_for(station),
                         className="gauge-link"),
                dcc.Graph(figure=_station_figure(station_df, station, label, levels, dark)),
            ], className="graph-card",
                style={"border": f"3px solid {colour}"})))

        graphs.sort(key=lambda item: (item[0], item[1]))
        flooding_count = sum(1 for p, _, _ in graphs if p < 4)
        summary = (f"{df['station_name'].nunique()} stations, "
                   f"{len(df):,} observations — {flooding_count} at or above flood level. ")
        cycles, last_hb = flood_data.heartbeat_summary(start, end)
        if cycles:
            summary += f"Monitor ran {cycles} cycle(s), last {last_hb}. "
        if len(graphs) > MAX_GRAPHS:
            summary += (f"Showing {MAX_GRAPHS} of {len(graphs)} station graphs "
                        "(flooding stations first) — filter by catchment or use "
                        "'flooding only' to narrow. ")
            graphs = graphs[:MAX_GRAPHS]
        if not levels_map:
            summary += "No flood levels loaded yet (import Flood Levels.xlsx on the Import page)."

        return (summary, table_df.to_dict("records"), columns, *style_out,
                [g for _, _, g in graphs])

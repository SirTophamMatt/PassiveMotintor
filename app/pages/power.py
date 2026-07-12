"""Power Outage page (public, read-only): KPIs, trends, map, durations.

Collection is controlled from the Admin page; this page shows a read-only
collector status line.
"""
import pandas as pd
import plotly.express as px
from dash import Input, Output, dcc, html

from app import ui
from app.collector import manager
from app.modules.power import data as power_data

TREND_WINDOWS = [
    ("Past Hour", pd.Timedelta(hours=1)),
    ("Past 6 Hours", pd.Timedelta(hours=6)),
    ("Past 24 Hours", pd.Timedelta(hours=24)),
    ("Past Week", pd.Timedelta(days=7)),
]

SERIES_COLOURS = {
    "customers_off": "#1f77b4",
    "planned": "#2ca02c",
    "unplanned": "#d62728",
}
SERIES_LABELS = {
    "customers_off": "Customers Off",
    "planned": "Planned",
    "unplanned": "Unplanned",
}


def layout():
    return html.Div([
        html.H2("Power Outages"),
        html.Div([
            html.Div([
                html.H4("Collector"),
                html.Div(id="power-collector-status"),
                html.Div("Collection is managed on the Admin page.",
                         className="muted",
                         style={"marginTop": "8px", "fontSize": "12px"}),
            ], className="panel"),
            html.Div([
                html.H4("Filters"),
                dcc.Checklist(id="power-filters", value=[], options=[
                    {"label": " Hide planned outages from map", "value": "hide_planned"},
                    {"label": " Only outages with 50+ customers", "value": "large_only"},
                ]),
            ], className="panel"),
        ], className="panel-row"),
        dcc.Interval(id="power-interval", interval=60_000, n_intervals=0),
        html.Div(id="power-kpis", className="kpi-row"),
        html.Div(id="power-trend-graphs", className="graph-grid"),
        html.Div([
            html.Div(dcc.Graph(id="power-map", style={"height": "650px"}),
                     style={"flex": "3"}),
            html.Div(dcc.Graph(id="power-durations", style={"height": "650px"}),
                     style={"flex": "2"}),
        ], style={"display": "flex", "gap": "16px", "flexWrap": "wrap"}),
    ])


def _trend_figure(df, title, dark):
    fig = px.line(
        df, x="timestamp", y=list(SERIES_COLOURS),
        title=title, labels={"timestamp": "Time", "value": "Customers", "variable": ""},
        color_discrete_map=SERIES_COLOURS)
    fig.for_each_trace(lambda t: t.update(name=SERIES_LABELS.get(t.name, t.name)))
    if not df.empty and df["customers_off"].notna().any():
        peak_idx = df["customers_off"].idxmax()
        peak_val = df.at[peak_idx, "customers_off"]
        peak_time = df.at[peak_idx, "timestamp"]
        label = f"Peak {peak_val:,.0f}"
        if pd.notna(peak_time):
            # 24-hour "hrs" style; date included so the Past Week peak is clear.
            label += f" at {peak_time:%H%Mhrs, %d %b}"
        fig.add_scatter(
            x=[peak_time], y=[peak_val],
            mode="markers+text", marker=dict(size=11, symbol="diamond", color="orange"),
            text=[label], textposition="top center", name="Peak")
    fig.update_layout(height=320, legend=dict(orientation="h", y=1.12))
    return ui.apply_theme(fig, dark)


def _map_figure(outages, dark):
    if outages.empty or outages[["latitude", "longitude"]].dropna().empty:
        fig = px.scatter_mapbox(
            pd.DataFrame({"latitude": [], "longitude": []}),
            lat="latitude", lon="longitude", zoom=6,
            center={"lat": -37.8136, "lon": 144.9631}, mapbox_style="open-street-map",
            title="Active Outages (no geocoded locations yet)")
        return ui.apply_theme(fig, dark)
    df = outages.dropna(subset=["latitude", "longitude"]).copy()
    df["Duration"] = df["duration_mins"].apply(power_data.duration_bucket)
    df["customers_off"] = df["customers_off"].fillna(0).clip(lower=1)
    fig = px.scatter_mapbox(
        df, lat="latitude", lon="longitude",
        size="customers_off", size_max=30,
        color="Duration", color_discrete_map=power_data.DURATION_COLOUR_MAP,
        hover_name="location",
        hover_data={"customers_off": True, "duration_mins": True, "type": True,
                    "latitude": False, "longitude": False, "Duration": False},
        zoom=6, center={"lat": -37.8136, "lon": 144.9631},
        mapbox_style="open-street-map",
        title="Active Outages by Location")
    return ui.apply_theme(fig, dark)


def _durations_figure(outages, dark):
    df = outages[~outages["type"].fillna("").str.strip().str.lower().eq("planned")].copy()
    if df.empty:
        fig = px.bar(title="No active unplanned outages")
        return ui.apply_theme(fig, dark)
    df["Duration"] = df["duration_mins"].apply(power_data.duration_bucket)
    df = df.sort_values("duration_mins", ascending=False).head(40)
    fig = px.bar(
        df, y="location", x="duration_mins", orientation="h",
        color="Duration", color_discrete_map=power_data.DURATION_COLOUR_MAP,
        labels={"duration_mins": "Duration (minutes)", "location": ""},
        hover_data=["customers_off"],
        title="Unplanned Outage Durations")
    fig.update_layout(yaxis=dict(autorange="reversed"), legend_title_text="Duration")
    return ui.apply_theme(fig, dark)


def register_callbacks(app):
    @app.callback(
        Output("power-collector-status", "children"),
        Input("power-interval", "n_intervals"))
    def collector_status(_):
        s = manager.status()["power"]
        parts = [html.Strong("Status: "), ui.status_pill(s["running"])]
        if s.get("last_run"):
            parts.append(html.Span(
                f" — last cycle {s['last_run']} ({s.get('runs', 0)} total)"))
        if s.get("last_error"):
            parts.append(html.Div(f"⚠ {s['last_error']}", className="error-text",
                                  style={"marginTop": "4px"}))
        return html.Div(parts)

    @app.callback(
        Output("power-kpis", "children"),
        Output("power-trend-graphs", "children"),
        Output("power-map", "figure"),
        Output("power-durations", "figure"),
        Input("power-interval", "n_intervals"),
        Input("power-filters", "value"),
        Input("theme-store", "data"))
    def refresh(_, filters, dark):
        df = power_data.load_timeseries()
        dark = bool(dark)

        if df.empty:
            kpis = [ui.kpi_card("Power Data", "No data yet — start collection")]
            trends = []
        else:
            latest = df.iloc[-1]

            def fmt(value):
                return f"{value:,.0f}" if pd.notna(value) else "—"

            kpis = [
                ui.kpi_card("Customers Off", fmt(latest["customers_off"]), "#1f77b4"),
                ui.kpi_card("Power Dependant Off", fmt(latest["power_dependant_off"])),
                ui.kpi_card("Planned", fmt(latest["planned"]), "#2ca02c"),
                ui.kpi_card("Unplanned", fmt(latest["unplanned"]), "#d62728"),
            ]
            now = df["timestamp"].max()
            trends = [
                html.Div(dcc.Graph(figure=_trend_figure(
                    df[df["timestamp"] >= now - window], title, dark)),
                    className="graph-card")
                for title, window in TREND_WINDOWS
            ]

        outages = power_data.active_outages(
            include_planned="hide_planned" not in (filters or []),
            min_customers=50 if "large_only" in (filters or []) else 0)
        return (kpis, trends,
                _map_figure(outages, dark),
                _durations_figure(outages, dark))

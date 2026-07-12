"""Fire / Incidents page (public, read-only): VicEmergency incidents & warnings.

Shows headline counts, a state map coloured by severity, a table of active
events, and a counts-over-time trend. Fire is presented first, but every live
category from the feed is available via the filter. Collection is always-on and
managed from the Admin page.
"""
import json

import pandas as pd
import plotly.express as px
from dash import Input, Output, dash_table, dcc, html

from app import ui
from app.collector import manager
from app.modules.fire import data as fire_data

MELB_CENTER = {"lat": -37.0, "lon": 145.0}

# Map/marker colours by event "kind" (warnings by level, then fire, then rest).
KIND_COLOURS = {
    "Emergency Warning": "#d62728",
    "Watch and Act": "#ff7f0e",
    "Advice": "#e6c700",
    "Fire": "#ff5722",
    "Other incident": "#5b8def",
}
BURN_COLOUR = "#8d6e63"  # historical burn-area footprints

TABLE_COLUMNS = [
    ("category1", "Category"), ("location", "Location"), ("status", "Status"),
    ("warning_level", "Warning"), ("severity", "Severity"), ("size", "Size"),
    ("updated", "Updated"),
]


def _kind(row):
    if row.get("warning_level") in KIND_COLOURS:
        return row["warning_level"]
    if str(row.get("category1") or "").strip().lower() == "fire":
        return "Fire"
    return "Other incident"


def layout():
    return html.Div([
        html.Div([
            html.H2("Fire / Incidents", style={"display": "inline-block"}),
            html.Button("⤓ Situation Report PDF", id="fire-pdf-btn",
                        className="btn btn-primary",
                        style={"float": "right", "marginTop": "6px"}),
            html.Div(id="fire-pdf-status", className="muted",
                     style={"clear": "both"}),
            dcc.Download(id="fire-pdf-download"),
        ]),
        html.Div([
            html.Div([
                html.H4("Collector"),
                html.Div(id="fire-collector-status"),
                html.Div("Collection is managed on the Admin page.",
                         className="muted",
                         style={"marginTop": "8px", "fontSize": "12px"}),
            ], className="panel"),
            html.Div([
                html.H4("Filters"),
                html.Label("Category"),
                dcc.Dropdown(id="fire-category-filter", placeholder="All categories",
                             clearable=True, className="dropdown"),
                dcc.Checklist(id="fire-warnings-toggle",
                              options=[{"label": " Community warnings only",
                                        "value": "warnings"}],
                              value=[]),
                dcc.Checklist(id="fire-layer-toggle",
                              options=[{"label": " Show burn areas (historical)",
                                        "value": "burn"}],
                              value=[], style={"marginTop": "4px"}),
            ], className="panel"),
        ], className="panel-row"),
        dcc.Interval(id="fire-interval", interval=60_000, n_intervals=0),
        html.Div(id="fire-summary", className="muted", style={"margin": "10px 0"}),
        html.Div(id="fire-kpis", className="kpi-row"),
        html.Div(dcc.Graph(id="fire-map", style={"height": "620px"}),
                 className="graph-card"),
        dash_table.DataTable(
            id="fire-table",
            page_size=15,
            filter_action="native",
            sort_action="native",
        ),
        html.Div(dcc.Graph(id="fire-trend"), className="graph-card",
                 style={"marginTop": "16px"}),
    ])


def _fill_layer(geometries, colour, opacity):
    """A Plotly mapbox fill layer from a list of GeoJSON geometry strings, or
    None if none parse. Sits below the marker traces."""
    features = []
    for geom in geometries:
        if not geom:
            continue
        try:
            features.append({"type": "Feature", "properties": {},
                             "geometry": json.loads(geom)})
        except (ValueError, TypeError):
            continue
    if not features:
        return None
    return {"sourcetype": "geojson", "type": "fill", "below": "traces",
            "color": colour, "opacity": opacity,
            "source": {"type": "FeatureCollection", "features": features}}


def _map_figure(df, dark, burn_df=None):
    """Incident/warning markers (centroids) with filled polygon overlays for
    events that have an area, plus an optional historical burn-area layer."""
    located = df.dropna(subset=["latitude", "longitude"]) if not df.empty else df
    if located is None or located.empty:
        fig = px.scatter_mapbox(
            pd.DataFrame({"latitude": [], "longitude": []}),
            lat="latitude", lon="longitude", zoom=5.2, center=MELB_CENTER,
            mapbox_style="open-street-map", title="Active Incidents & Warnings")
    else:
        plot = located.copy()
        plot["Kind"] = plot.apply(_kind, axis=1)
        fig = px.scatter_mapbox(
            plot, lat="latitude", lon="longitude", color="Kind",
            color_discrete_map=KIND_COLOURS, hover_name="location",
            hover_data={"category1": True, "status": True, "size": True,
                        "latitude": False, "longitude": False, "Kind": False},
            zoom=5.2, center=MELB_CENTER, mapbox_style="open-street-map",
            title="Active Incidents & Warnings")
        fig.update_traces(marker=dict(size=12))

    layers = []
    # Historical burn areas underneath everything, only when requested.
    if burn_df is not None and not burn_df.empty and "geometry" in burn_df.columns:
        layer = _fill_layer(burn_df["geometry"].dropna().tolist(), BURN_COLOUR, 0.35)
        if layer:
            layers.append(layer)
    # Warning/incident areas, coloured by kind, on top of the burn layer.
    if not df.empty and "geometry" in df.columns:
        kinds = df.copy()
        kinds["Kind"] = kinds.apply(_kind, axis=1)
        for kind, colour in KIND_COLOURS.items():
            geoms = kinds.loc[kinds["Kind"] == kind, "geometry"].dropna().tolist()
            layer = _fill_layer(geoms, colour, 0.25)
            if layer:
                layers.append(layer)
    if layers:
        fig.update_layout(mapbox_layers=layers)  # magic-underscore: keeps style/zoom
    return ui.apply_theme(fig, dark)


def _trend_figure(df, dark):
    if df.empty:
        fig = px.line(title="Active events over time (no history yet)")
        return ui.apply_theme(fig, dark)
    series = {"active_fires": "Active fires", "emergency_warnings": "Emergency",
              "watch_act": "Watch & Act", "advice": "Advice",
              "total_active": "All active"}
    fig = px.line(df, x="timestamp", y=list(series),
                  title="Active events over time",
                  labels={"timestamp": "Time", "value": "Count", "variable": ""})
    fig.for_each_trace(lambda t: t.update(name=series.get(t.name, t.name)))
    fig.update_layout(height=320, legend=dict(orientation="h", y=1.12))
    return ui.apply_theme(fig, dark)


def register_callbacks(app):
    @app.callback(
        Output("fire-collector-status", "children"),
        Output("fire-category-filter", "options"),
        Input("fire-interval", "n_intervals"))
    def collector_status(_):
        s = manager.status()["fire"]
        parts = [html.Strong("Status: "), ui.status_pill(s["running"])]
        if s.get("last_run"):
            parts.append(html.Span(
                f" — last cycle {s['last_run']} ({s.get('runs', 0)} total)"))
        if s.get("last_error"):
            parts.append(html.Div(f"⚠ {s['last_error']}", className="error-text",
                                  style={"marginTop": "4px"}))
        options = [{"label": c, "value": c} for c in fire_data.categories()]
        return html.Div(parts), options

    @app.callback(
        Output("fire-summary", "children"),
        Output("fire-kpis", "children"),
        Output("fire-map", "figure"),
        Output("fire-table", "data"),
        Output("fire-table", "columns"),
        Output("fire-table", "style_table"),
        Output("fire-table", "style_cell"),
        Output("fire-table", "style_header"),
        Output("fire-table", "style_data"),
        Output("fire-trend", "figure"),
        Input("fire-interval", "n_intervals"),
        Input("fire-category-filter", "value"),
        Input("fire-warnings-toggle", "value"),
        Input("fire-layer-toggle", "value"),
        Input("theme-store", "data"))
    def refresh(_, category, warnings_only, layers, dark):
        dark = bool(dark)
        styles = ui.table_styles(dark)
        style_out = (styles["style_table"], styles["style_cell"],
                     styles.get("style_header", {}), styles.get("style_data", {}))

        counts = fire_data.latest_counts()
        kpis = [
            ui.kpi_card("Active Fires", str(counts["active_fires"]),
                        "#ff5722" if counts["active_fires"] else "#2ca02c"),
            ui.kpi_card("Emergency Warnings", str(counts["emergency"]),
                        "#d62728" if counts["emergency"] else "#2ca02c"),
            ui.kpi_card("Watch & Act", str(counts["watch_act"]),
                        "#ff7f0e" if counts["watch_act"] else None),
            ui.kpi_card("Advice", str(counts["advice"]),
                        "#e6c700" if counts["advice"] else None),
            ui.kpi_card("Total Active", str(counts["total"])),
        ]

        df = fire_data.active_incidents(
            category=category, warnings_only="warnings" in (warnings_only or []))
        burn_df = fire_data.burn_areas() if "burn" in (layers or []) else None
        map_fig = _map_figure(df, dark, burn_df)
        trend_fig = _trend_figure(fire_data.load_fire_timeseries(), dark)

        cycles, last_hb = fire_data.heartbeat_summary()
        summary = f"{counts['total']} active event(s) state-wide. "
        if cycles:
            summary += f"Monitor ran {cycles} cycle(s), last {last_hb}."

        if df.empty:
            return (summary, kpis, map_fig, [], [], *style_out, trend_fig)

        table_df = df[[c for c, _ in TABLE_COLUMNS]].copy()
        table_df["updated"] = table_df["updated"].dt.strftime("%d %b %H:%M")
        columns = [{"name": name, "id": col} for col, name in TABLE_COLUMNS]
        return (summary, kpis, map_fig, table_df.to_dict("records"), columns,
                *style_out, trend_fig)

    @app.callback(
        Output("fire-pdf-download", "data"),
        Output("fire-pdf-status", "children"),
        Input("fire-pdf-btn", "n_clicks"),
        prevent_initial_call=True)
    def make_pdf(_):
        from dash import no_update

        from app import reporting
        try:
            filename, pdf_bytes = reporting.build_fire_pdf()
        except reporting.ReportingUnavailable as e:
            return no_update, f"⚠ {e}"
        except Exception as e:
            return no_update, f"⚠ Could not build report: {e}"
        return dcc.send_bytes(pdf_bytes, filename), "✅ Situation report generated."

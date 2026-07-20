"""Storm Tracker page (public, read-only): BoM radar cell detection loop.

Shows the annotated radar loop (last ~2 h of frames), active tracked cells
with real speed/bearing, the change-only alert log, and a per-cell intensity
trend. The animation is CLIENT-side: a 30 s server callback refreshes the
frame list into a store, and a clientside callback cycles the <img> src, so
the loop costs the server nothing between refreshes. Frames themselves are
served by a small Flask route from the storm_frames directory.
"""
import re

import flask
import pandas as pd
import plotly.express as px
from dash import Input, Output, dash_table, dcc, html

from app import ui
from app.collector import manager
from app.config import load_config
from app.modules.storm import data as storm_data
from app.modules.storm.scraper import STORM_FRAMES_DIR, radar_ids

FRAME_ROUTE = "/storm-frames"
_FRAME_NAME = re.compile(r"^annotated_[A-Za-z0-9]+_\d{12}\.png$")

CELL_COLUMNS = [
    ("cell_id", "Cell"), ("radar_id", "Radar"), ("classification", "Class"),
    ("intensity_score", "Score"), ("area_km2", "Area (km²)"),
    ("max_level", "Peak level"), ("speed_kmh", "Speed (km/h)"),
    ("bearing", "Moving"), ("position", "Lat / Lon"), ("frame_ts", "Last seen"),
]
ALERT_COLUMNS = [
    ("timestamp", "Time"), ("classification", "Class"),
    ("alert_type", "Type"), ("message", "Detail"),
]


def layout():
    radars = radar_ids(load_config())
    return html.Div([
        html.Div([
            html.H2("Storm Tracker", style={"display": "inline-block"}),
            html.Button("⤓ Impact areas (GeoJSON)", id="storm-geojson-btn",
                        className="btn btn-primary",
                        style={"float": "right", "marginTop": "6px"}),
            html.Button("⤓ Storm Briefing PDF", id="storm-pdf-btn",
                        className="btn btn-primary",
                        style={"float": "right", "marginTop": "6px",
                               "marginRight": "8px"}),
            html.Div(id="storm-geojson-status", className="muted",
                     style={"clear": "both"}),
            html.Div(id="storm-pdf-status", className="muted"),
            dcc.Download(id="storm-geojson-download"),
            dcc.Download(id="storm-pdf-download"),
        ]),
        html.Div([
            html.Div([
                html.H4("Collector"),
                html.Div(id="storm-collector-status"),
                html.Div(f"BoM radar(s) {', '.join(radars)} — echo frames "
                         "every ~5 min. Collection is managed on the Admin page.",
                         className="muted",
                         style={"marginTop": "8px", "fontSize": "12px"}),
            ], className="panel"),
            html.Div([
                html.H4("Radar loop"),
                dcc.Dropdown(id="storm-radar-select", clearable=False,
                             options=[{"label": r, "value": r} for r in radars],
                             value=radars[0], className="dropdown"),
            ], className="panel"),
        ], className="panel-row"),
        dcc.Interval(id="storm-interval", interval=30_000, n_intervals=0),
        dcc.Interval(id="storm-anim-interval", interval=650, n_intervals=0),
        dcc.Store(id="storm-frames-store", data=[]),
        html.Div(id="storm-summary", className="muted", style={"margin": "10px 0"}),
        html.Div(id="storm-kpis", className="kpi-row"),
        html.Div([
            html.Img(id="storm-loop-img",
                     style={"width": "100%", "maxWidth": "760px",
                            "display": "block", "margin": "0 auto",
                            "borderRadius": "6px"}),
            html.Div(id="storm-loop-label", className="muted",
                     style={"textAlign": "center", "marginTop": "6px"}),
        ], className="graph-card"),
        html.H3("Active Cells"),
        dash_table.DataTable(id="storm-cells-table", page_size=10,
                             sort_action="native"),
        html.H3("Alerts", style={"marginTop": "16px"}),
        dash_table.DataTable(id="storm-alerts-table", page_size=10),
        html.Div(dcc.Graph(id="storm-trend"), className="graph-card",
                 style={"marginTop": "16px"}),
    ])


def _trend_figure(df, dark):
    if df.empty:
        fig = px.line(title="Cell intensity over time (no cells tracked yet)")
    else:
        fig = px.line(df, x="frame_ts", y="intensity_score", color="cell_id",
                      title="Cell intensity over time (strongest cells, last 6 h)",
                      labels={"frame_ts": "Time", "intensity_score": "Intensity",
                              "cell_id": "Cell"})
        fig.update_layout(height=340, legend=dict(orientation="h", y=-0.25))
    return ui.apply_theme(fig, dark)


def register_callbacks(app):
    @app.server.route(f"{FRAME_ROUTE}/<path:filename>")
    def storm_frame(filename):
        # Only the annotated loop frames are servable, nothing else on disk.
        if not _FRAME_NAME.match(filename):
            flask.abort(404)
        return flask.send_from_directory(STORM_FRAMES_DIR, filename,
                                         max_age=3600)

    @app.callback(
        Output("storm-collector-status", "children"),
        Output("storm-frames-store", "data"),
        Input("storm-interval", "n_intervals"),
        Input("storm-radar-select", "value"))
    def collector_status(_, radar_id):
        s = manager.status()["storm"]
        parts = [html.Strong("Status: "), ui.status_pill(s["running"])]
        if s.get("last_run"):
            parts.append(html.Span(
                f" — last cycle {s['last_run']} ({s.get('runs', 0)} total)"))
        if s.get("last_error"):
            parts.append(html.Div(f"⚠ {s['last_error']}", className="error-text",
                                  style={"marginTop": "4px"}))
        radar_id = radar_id or radar_ids()[0]
        frames = [{"src": f"{FRAME_ROUTE}/{f['file']}",
                   "label": f"{radar_id}  {f['label']}"}
                  for f in storm_data.annotated_frames(radar_id)]
        return html.Div(parts), frames

    # Client-side loop animation: cycle the img src through the stored frame
    # list (oldest -> newest, hold briefly on the newest before restarting).
    app.clientside_callback(
        """
        function(tick, frames) {
            if (!frames || !frames.length) {
                return [window.dash_clientside.no_update,
                        "No processed frames yet — the first cycle runs " +
                        "within a few minutes of the collector starting."];
            }
            var hold = 4;  // extra ticks spent on the newest frame
            var cycle = frames.length + hold;
            var i = Math.min(tick % cycle, frames.length - 1);
            return [frames[i].src, frames[i].label +
                    "  (" + (i + 1) + "/" + frames.length + ")"];
        }
        """,
        Output("storm-loop-img", "src"),
        Output("storm-loop-label", "children"),
        Input("storm-anim-interval", "n_intervals"),
        Input("storm-frames-store", "data"))

    @app.callback(
        Output("storm-summary", "children"),
        Output("storm-kpis", "children"),
        Output("storm-cells-table", "data"),
        Output("storm-cells-table", "columns"),
        Output("storm-cells-table", "style_table"),
        Output("storm-cells-table", "style_cell"),
        Output("storm-cells-table", "style_header"),
        Output("storm-cells-table", "style_data"),
        Output("storm-alerts-table", "data"),
        Output("storm-alerts-table", "columns"),
        Output("storm-trend", "figure"),
        Input("storm-interval", "n_intervals"),
        Input("theme-store", "data"))
    def refresh(_, dark):
        dark = bool(dark)
        styles = ui.table_styles(dark)
        style_out = (styles["style_table"], styles["style_cell"],
                     styles.get("style_header", {}), styles.get("style_data", {}))

        counts = storm_data.latest_counts()
        kpis = [
            ui.kpi_card("Active Cells", str(counts["total"]),
                        "#5b8def" if counts["total"] else "#2ca02c"),
            ui.kpi_card("Strong", str(counts["strong"]),
                        "#d62728" if counts["strong"] else "#2ca02c"),
            ui.kpi_card("Moderate", str(counts["moderate"]),
                        "#ff7f0e" if counts["moderate"] else None),
            ui.kpi_card("Max Intensity", str(counts["max_intensity"])),
        ]

        cycles, last_hb = storm_data.heartbeat_summary()
        summary = (f"{counts['total']} radar cell(s) tracked in the last "
                   f"{storm_data.ACTIVE_WINDOW_MINUTES} min. ")
        if cycles:
            summary += f"Monitor ran {cycles} cycle(s), last {last_hb}."

        cells = storm_data.active_cells()
        if cells.empty:
            table_data = []
        else:
            from app.modules.storm.tracker import bearing_to_cardinal
            view = cells.copy()
            view["intensity_score"] = view["intensity_score"].round(0)
            view["area_km2"] = view["area_km2"].round(1)
            view["speed_kmh"] = view["speed_kmh"].round(0)
            view["bearing"] = view["bearing_deg"].map(
                lambda b: bearing_to_cardinal(b) if pd.notna(b) else "—")
            if "latitude" in view.columns:
                view["position"] = view.apply(
                    lambda r: (f"{r['latitude']:.3f}, {r['longitude']:.3f}"
                               if pd.notna(r.get("latitude")) else "—"), axis=1)
            else:
                view["position"] = "—"
            table_data = view[[c for c, _ in CELL_COLUMNS]].to_dict("records")
        cell_columns = [{"name": name, "id": col} for col, name in CELL_COLUMNS]

        alerts = storm_data.recent_alerts()
        alert_data = ([] if alerts.empty
                      else alerts[[c for c, _ in ALERT_COLUMNS]].to_dict("records"))
        alert_columns = [{"name": name, "id": col} for col, name in ALERT_COLUMNS]

        trend = _trend_figure(storm_data.cell_history(), dark)
        return (summary, kpis, table_data, cell_columns, *style_out,
                alert_data, alert_columns, trend)

    @app.callback(
        Output("storm-geojson-download", "data"),
        Output("storm-geojson-status", "children"),
        Input("storm-geojson-btn", "n_clicks"),
        prevent_initial_call=True)
    def download_geojson(_):
        import json as _json
        from datetime import datetime as _dt
        fc = storm_data.impact_featurecollection()
        n = len(fc["features"])
        if not n:
            return None, "No moderate/strong cells with impact areas right now."
        filename = f"storm_impact_areas_{_dt.now():%Y%m%d_%H%M}.geojson"
        return (dcc.send_string(_json.dumps(fc, indent=2), filename),
                f"✅ Exported {n} impact area(s).")

    @app.callback(
        Output("storm-pdf-download", "data"),
        Output("storm-pdf-status", "children"),
        Input("storm-pdf-btn", "n_clicks"),
        prevent_initial_call=True)
    def make_pdf(_):
        from dash import no_update

        from app import reporting
        try:
            filename, pdf_bytes = reporting.build_storm_pdf()
        except reporting.ReportingUnavailable as e:
            return no_update, f"⚠ {e}"
        except Exception as e:
            return no_update, f"⚠ Could not build briefing: {e}"
        return dcc.send_bytes(pdf_bytes, filename), "✅ Storm briefing generated."

"""Weather page (public, read-only): BoM warnings for Victoria.

Rainfall (per monitored location, derived from the flood gauges) is added in a
later slice; this slice covers the warnings feed. Collection is always-on and
managed from the Admin page.
"""
import pandas as pd
from dash import Input, Output, State, dash_table, dcc, html
from dash.exceptions import PreventUpdate

from app import ui
from app.collector import manager
from app.modules.weather import data as weather_data

WARNING_PATH = "/weather/warning/"


def warning_path_for(warning_id):
    return WARNING_PATH + str(warning_id)


def warning_id_from_path(pathname):
    if pathname and pathname.startswith(WARNING_PATH):
        return pathname[len(WARNING_PATH):]
    return None

# BoM's warning_group_type (major/minor/...) is intentionally NOT shown: it
# would be confused with the flood gauge's Minor/Moderate/Major classification.
# It's still stored and used internally (sort order, alert upgrade detection).
TABLE_COLUMNS = [
    ("type_label", "Type"), ("title", "Warning"),
    ("issue_time", "Issued"), ("expiry_time", "Expires"),
]


def layout():
    return html.Div([
        html.H2("Weather Warnings"),
        html.Div([
            html.Div([
                html.H4("Collector"),
                html.Div(id="weather-collector-status"),
                html.Div("BoM warnings for Victoria (api.weather.bom.gov.au). "
                         "Collection is managed on the Admin page.",
                         className="muted",
                         style={"marginTop": "8px", "fontSize": "12px"}),
            ], className="panel"),
            html.Div([
                html.H4("Filters"),
                html.Label("Warning type"),
                dcc.Dropdown(id="weather-type-filter", placeholder="All types",
                             clearable=True, className="dropdown"),
            ], className="panel"),
        ], className="panel-row"),
        dcc.Interval(id="weather-interval", interval=60_000, n_intervals=0),
        html.Div(id="weather-summary", className="muted", style={"margin": "10px 0"}),
        html.Div(id="weather-kpis", className="kpi-row"),
        dash_table.DataTable(
            id="weather-table",
            page_size=15,
            filter_action="native",
            sort_action="native",
            style_cell={"whiteSpace": "normal", "height": "auto"},
        ),
    ])


def register_callbacks(app):
    @app.callback(
        Output("weather-collector-status", "children"),
        Output("weather-type-filter", "options"),
        Input("weather-interval", "n_intervals"))
    def collector_status(_):
        s = manager.status()["weather"]
        parts = [html.Strong("Status: "), ui.status_pill(s["running"])]
        if s.get("last_run"):
            parts.append(html.Span(
                f" — last cycle {s['last_run']} ({s.get('runs', 0)} total)"))
        if s.get("last_error"):
            parts.append(html.Div(f"⚠ {s['last_error']}", className="error-text",
                                  style={"marginTop": "4px"}))
        options = [{"label": label, "value": value}
                   for label, value in weather_data.warning_types()]
        return html.Div(parts), options

    @app.callback(
        Output("weather-summary", "children"),
        Output("weather-kpis", "children"),
        Output("weather-table", "data"),
        Output("weather-table", "columns"),
        Output("weather-table", "style_table"),
        Output("weather-table", "style_cell"),
        Output("weather-table", "style_header"),
        Output("weather-table", "style_data"),
        Output("weather-table", "style_data_conditional"),
        Input("weather-interval", "n_intervals"),
        Input("weather-type-filter", "value"),
        Input("theme-store", "data"))
    def refresh(_, warning_type, dark):
        dark = bool(dark)
        styles = ui.table_styles(dark)
        base_cell = {"whiteSpace": "normal", "height": "auto"}
        base_cell.update(styles["style_cell"])

        counts = weather_data.warning_counts()
        kpis = [
            ui.kpi_card("Active Warnings", str(counts["total"]),
                        "#d62728" if counts["total"] else "#2ca02c"),
            ui.kpi_card("Flood Warnings", str(counts["flood"]),
                        "#1f77b4" if counts["flood"] else None),
            ui.kpi_card("Severe Weather", str(counts["severe"]),
                        "#ff7f0e" if counts["severe"] else None),
        ]

        cycles, last_hb = weather_data.heartbeat_summary()
        summary = f"{counts['total']} active BoM warning(s) for Victoria. "
        if cycles:
            summary += f"Monitor ran {cycles} cycle(s), last {last_hb}."

        # Rows still come back most-serious-first (active_warnings sorts by the
        # stored group_type), but the level itself isn't shown.
        df = weather_data.active_warnings(warning_type=warning_type)
        style_out = (styles["style_table"], base_cell,
                     styles.get("style_header", {}), styles.get("style_data", {}))
        if df.empty:
            return (summary, kpis, [], [], *style_out, [])

        table_df = df.copy()
        table_df["issue_time"] = table_df["issue_time"].dt.strftime("%d %b %H:%M")
        table_df["expiry_time"] = table_df["expiry_time"].dt.strftime("%d %b %H:%M")
        # Link the warning title to its detail/history page.
        table_df["title"] = df.apply(
            lambda r: f"[{str(r['title']).replace('[', '(').replace(']', ')')}]"
                      f"({warning_path_for(r['warning_id'])})", axis=1)
        table_df = table_df[[c for c, _ in TABLE_COLUMNS]]
        columns = [{"name": name, "id": col,
                    **({"presentation": "markdown"} if col == "title" else {})}
                   for col, name in TABLE_COLUMNS]
        return (summary, kpis, table_df.to_dict("records"), columns,
                *style_out, [])

    @app.callback(
        Output("warning-message-frame", "srcDoc"),
        Input("warning-version-select", "value"),
        State("warning-detail-id", "data"),
        prevent_initial_call=True)
    def show_version(issue_time, warning_id):
        if not warning_id or not issue_time:
            raise PreventUpdate
        msg = weather_data.warning_version_message(warning_id, issue_time)
        return msg or "<p>No text recorded for this version.</p>"


def warning_detail_layout(warning_id):
    """Detail page for one warning: full BoM text (images render inline) plus a
    version selector to replay how the warning developed."""
    d = weather_data.warning_detail(warning_id)
    if not d:
        return html.Div([
            html.H2("Warning not found"),
            dcc.Link("← Back to Weather Warnings", href="/weather",
                     className="nav-link"),
        ])
    hist = weather_data.warning_history(warning_id)
    versions = []
    if not hist.empty:
        for ts, phase in zip(hist["issue_time"], hist["phase"]):
            if pd.isna(ts):
                continue
            versions.append({
                "label": ts.strftime("%d %b %Y %H:%M")
                         + (f"  ·  {phase}" if phase else ""),
                "value": ts.strftime("%Y-%m-%d %H:%M:%S")})
    latest_msg = d.get("message") or "<p>No detailed text recorded yet.</p>"
    meta = (f"Issued {d.get('issue_time') or '—'}  ·  "
            f"Expires {d.get('expiry_time') or '—'}  ·  "
            f"Phase: {d.get('phase') or '—'}")
    return html.Div([
        dcc.Link("← Back to Weather Warnings", href="/weather", className="nav-link"),
        html.H2(weather_data._pretty_type(d.get("type"))),
        html.H4(d.get("title") or ""),
        html.Div(meta, className="muted"),
        dcc.Store(id="warning-detail-id", data=str(warning_id)),
        html.Label("Version (issued)", style={"marginTop": "10px",
                                              "display": "block"}),
        dcc.Dropdown(id="warning-version-select", options=versions,
                     value=(versions[0]["value"] if versions else None),
                     clearable=False, className="dropdown",
                     style={"maxWidth": "420px"}),
        html.Div(f"{len(versions)} version(s) recorded — select one to see how "
                 "the warning read at that time.", className="muted",
                 style={"fontSize": "12px", "margin": "4px 0"}),
        # Iframe (sandboxed, scripts blocked) faithfully renders BoM's HTML;
        # any embedded base64 images display inline.
        html.Iframe(id="warning-message-frame", srcDoc=latest_msg, sandbox="",
                    style={"width": "100%", "height": "640px",
                           "border": "1px solid #333d4d", "borderRadius": "6px",
                           "background": "#fff", "marginTop": "8px"}),
    ])

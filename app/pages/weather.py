"""Weather page (public, read-only): BoM warnings for Victoria.

Rainfall (per monitored location, derived from the flood gauges) is added in a
later slice; this slice covers the warnings feed. Collection is always-on and
managed from the Admin page.
"""
from dash import Input, Output, dash_table, dcc, html

from app import ui
from app.collector import manager
from app.modules.weather import data as weather_data

TABLE_COLUMNS = [
    ("group_type", "Level"), ("type_label", "Type"), ("title", "Warning"),
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
            ui.kpi_card("Major / Severe", str(counts["major"]),
                        "#d62728" if counts["major"] else None),
        ]

        cycles, last_hb = weather_data.heartbeat_summary()
        summary = f"{counts['total']} active BoM warning(s) for Victoria. "
        if cycles:
            summary += f"Monitor ran {cycles} cycle(s), last {last_hb}."

        df = weather_data.active_warnings(warning_type=warning_type)
        style_out = (styles["style_table"], base_cell,
                     styles.get("style_header", {}), styles.get("style_data", {}))
        if df.empty:
            return (summary, kpis, [], [], *style_out, [])

        # Colour the Level cell by severity.
        cond = []
        for grp, (_, colour) in weather_data.GROUP_STYLE.items():
            cond.append({"if": {"filter_query": f"{{group_type}} = '{grp}'",
                                "column_id": "group_type"},
                         "color": colour, "fontWeight": "bold"})

        table_df = df.copy()
        table_df["issue_time"] = table_df["issue_time"].dt.strftime("%d %b %H:%M")
        table_df["expiry_time"] = table_df["expiry_time"].dt.strftime("%d %b %H:%M")
        table_df = table_df[[c for c, _ in TABLE_COLUMNS]]
        columns = [{"name": name, "id": col} for col, name in TABLE_COLUMNS]
        return (summary, kpis, table_df.to_dict("records"), columns,
                *style_out, cond)

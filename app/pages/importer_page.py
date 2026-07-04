"""Import page: pull legacy data files into the unified database (admin only)."""
from dash import Input, Output, State, dcc, html

from app import auth, importer

IMPORTS = [
    {
        "id": "flood-db",
        "title": "Flood observations (.db or _flood_data.csv)",
        "hint": "Old flood_monitor.db or any event CSV from the old Flood Monitor.",
        "default": importer.DEFAULT_PATHS["flood_db"],
        "func": importer.import_flood_data,
    },
    {
        "id": "flood-levels",
        "title": "Flood levels (Flood Levels.xlsx)",
        "hint": "Minor/moderate/major levels per station. Replaces any existing levels.",
        "default": importer.DEFAULT_PATHS["flood_levels"],
        "func": importer.import_flood_levels,
    },
    {
        "id": "power-csv",
        "title": "Power time series (power_data.csv)",
        "hint": "Headline outage totals history.",
        "default": importer.DEFAULT_PATHS["power_csv"],
        "func": importer.import_power_timeseries,
    },
    {
        "id": "tracker-csv",
        "title": "Outage tracker (outage_tracker.csv)",
        "hint": "Per-location outage history with durations.",
        "default": importer.DEFAULT_PATHS["tracker_csv"],
        "func": importer.import_outage_tracker,
    },
    {
        "id": "geo-cache",
        "title": "Geocode cache (geo_cache.json)",
        "hint": "Saves re-geocoding locations you already looked up.",
        "default": importer.DEFAULT_PATHS["geo_cache"],
        "func": importer.import_geo_cache,
    },
]


def layout():
    blocks = []
    for item in IMPORTS:
        blocks.append(html.Div([
            html.H4(item["title"]),
            html.P(item["hint"], className="muted"),
            dcc.Input(id=f"import-path-{item['id']}", type="text",
                      value=item["default"], className="text-input wide"),
            html.Button("Import", id=f"import-btn-{item['id']}", className="btn btn-primary"),
            html.Div(id=f"import-status-{item['id']}", className="muted",
                     style={"marginTop": "6px"}),
        ], className="panel"))
    return html.Div([
        html.H2("Import Legacy Data"),
        html.P("The new app starts with an empty database. Use these tools to pull "
               "history in from the old Flood Monitor and PowerDashboard projects. "
               "Imports are additive and duplicate flood readings are skipped, so "
               "re-running an import is safe."),
        html.Div(blocks),
    ])


def register_callbacks(app):
    for item in IMPORTS:
        def _make_handler(func):
            def handler(_, path):
                if not auth.is_admin():
                    return "Not authorised."
                if not path or not path.strip():
                    return "Enter a file path."
                return func(path.strip().strip('"'))
            return handler

        app.callback(
            Output(f"import-status-{item['id']}", "children"),
            Input(f"import-btn-{item['id']}", "n_clicks"),
            State(f"import-path-{item['id']}", "value"),
            prevent_initial_call=True,
        )(_make_handler(item["func"]))

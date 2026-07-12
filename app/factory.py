"""Builds the Dash application: shell layout, routing, theme, auth, health."""
import logging
import os
import secrets

import flask
from dash import Dash, Input, Output, State, dcc, html

# Set by run_desktop.py so the app renders a custom frameless title bar
# (with window controls) instead of relying on the OS chrome.
DESKTOP = os.environ.get("UM_DESKTOP") == "1"

from app import auth, database
from app.config import BASE_DIR, BUNDLE_DIR
from app.pages import (admin, analytics as analytics_page, fire, flood,
                       importer_page, overview, power, settings, station, weather)

log = logging.getLogger(__name__)

# path -> (label, module). Public pages first, then admin-only pages.
PUBLIC_PAGES = [
    ("/", "Overview", overview),
    ("/flood", "Flood Monitor", flood),
    ("/fire", "Fire / Incidents", fire),
    ("/weather", "Weather Warnings", weather),
    ("/power", "Power Outages", power),
]
ADMIN_PAGES = [
    ("/admin", "Admin", admin),
    ("/analytics", "Analytics", analytics_page),
    ("/settings", "Settings", settings),
    ("/import", "Import Data", importer_page),
]
ALL_PAGES = PUBLIC_PAGES + ADMIN_PAGES

# Pages that require an admin session to view. The Admin page is not listed
# because it renders its own login form when unauthenticated.
RESTRICTED = {"/analytics", "/settings", "/import"}


def _titlebar():
    """Custom frameless title bar for the desktop build. The left region is a
    pywebview drag area; the right buttons call the window-control JS API
    (wired up in assets/titlebar.js)."""
    return html.Div([
        html.Div([
            html.Span("⚡", className="tb-logo"),
            html.Span("Passive Monitor", className="tb-title"),
        ], className="tb-drag pywebview-drag-region"),
        html.Div([
            html.Div("–", id="win-min", className="win-btn",
                     title="Minimize", **{"data-win": "min"}),
            html.Div("□", id="win-max", className="win-btn",
                     title="Maximize", **{"data-win": "max"}),
            html.Div("✕", id="win-close", className="win-btn win-close",
                     title="Close", **{"data-win": "close"}),
        ], className="tb-controls"),
    ], className="titlebar")


def _shell_layout():
    children = [
        dcc.Location(id="url"),
        dcc.Store(id="theme-store", data=True, storage_type="local"),
    ]
    if DESKTOP:
        children.append(_titlebar())
    children.append(html.Div([
        html.Div([
            html.Div("⚡ Passive Monitor", className="brand"),
            html.Nav(id="main-nav", className="nav"),
            html.Button("☀ / ☾", id="theme-toggle", className="btn theme-btn",
                        title="Toggle light/dark mode"),
        ], className="sidebar"),
        html.Div(id="page-content", className="content"),
    ], className="body-row"))
    root_class = "app dark has-titlebar" if DESKTOP else "app dark"
    return html.Div(children, id="app-root", className=root_class)


def _login_required_panel():
    return html.Div([
        html.H2("Admin only"),
        html.P("This page requires an admin session."),
        dcc.Link("Go to the Admin login", href="/admin", className="btn btn-primary"),
    ])


def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(os.path.join(BASE_DIR, "unified_monitor.log"),
                                encoding="utf-8"),
        ],
    )

    # Route otherwise-uncaught exceptions to the log file too, so a crash is
    # diagnosable from unified_monitor.log and not only the console/terminal.
    import sys
    import threading

    def _log_uncaught(exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        logging.getLogger("uncaught").critical(
            "Uncaught exception", exc_info=(exc_type, exc, tb))

    sys.excepthook = _log_uncaught
    if hasattr(threading, "excepthook"):
        threading.excepthook = lambda a: _log_uncaught(
            a.exc_type, a.exc_value, a.exc_traceback)


def _register_health(app):
    """Lightweight health endpoint for uptime monitoring / container checks.

    Returns 200 when the DB is reachable, 503 otherwise, with a JSON body that
    also surfaces whether the collectors are alive and how fresh the flood
    heartbeat is — so a monitor can tell 'web up' from 'still collecting'."""
    @app.server.route("/health")
    def health():
        from app.collector import manager
        from app.modules.flood import data as flood_data

        db_ok = True
        try:
            database.read_df("SELECT 1 AS ok")
        except Exception:
            db_ok = False

        status = manager.status()
        _, last_hb = flood_data.heartbeat_summary()
        from app.modules.fire import data as fire_data
        from app.modules.weather import data as weather_data
        _, fire_last_hb = fire_data.heartbeat_summary()
        _, weather_last_hb = weather_data.heartbeat_summary()
        payload = {
            "status": "ok" if db_ok else "error",
            "db_ok": db_ok,
            "flood_running": status["flood"]["running"],
            "power_running": status["power"]["running"],
            "fire_running": status["fire"]["running"],
            "weather_running": status["weather"]["running"],
            "rainfall_running": status["rainfall"]["running"],
            "flood_last_heartbeat": last_hb,
            "fire_last_heartbeat": fire_last_hb,
            "weather_last_heartbeat": weather_last_hb,
            "flood_last_error": status["flood"].get("last_error"),
            "power_last_error": status["power"].get("last_error"),
            "fire_last_error": status["fire"].get("last_error"),
            "weather_last_error": status["weather"].get("last_error"),
        }
        try:
            from app.watchdog import supervisor
            payload["watchdog"] = {"alive": supervisor.is_alive(),
                                   **supervisor.state}
        except Exception:
            payload["watchdog"] = {"alive": False}
        return flask.jsonify(payload), (200 if db_ok else 503)


def create_app(autostart=False):
    setup_logging()
    database.init_db()

    # Load bundled reference data (flood levels) if the table is empty, so a
    # fresh deployment classifies flooding without a manual import.
    from app import importer
    importer.ensure_flood_levels_seed()
    importer.ensure_lfg_impacts_seed()

    app = Dash(
        __name__,
        suppress_callback_exceptions=True,
        title="Passive Monitor",
        assets_folder=os.path.join(BUNDLE_DIR, "assets"),
    )
    app.layout = _shell_layout()

    # Secret key for the admin session cookie. Prefer a stable key from the
    # environment so sessions survive restarts; otherwise generate an ephemeral
    # one (admins just re-log-in after a restart).
    app.server.secret_key = os.environ.get("UM_SECRET_KEY") or secrets.token_hex(32)

    _register_health(app)

    @app.callback(Output("main-nav", "children"), Input("url", "pathname"))
    def render_nav(_pathname):
        items = [(path, label) for path, label, _ in PUBLIC_PAGES]
        if auth.is_admin():
            items += [(path, label) for path, label, _ in ADMIN_PAGES]
        else:
            items.append(("/admin", "Admin"))
        return [dcc.Link(label, href=path, className="nav-link",
                         id=f"nav-{label}") for path, label in items]

    @app.callback(Output("page-content", "children"), Input("url", "pathname"))
    def route(pathname):
        from app import analytics
        analytics.record_view(pathname, is_admin=auth.is_admin())
        # Dynamic station detail pages: /flood/station/<station_key>
        if pathname and pathname.startswith("/flood/station/"):
            return station.layout(station.key_from_path(pathname))
        # Warning detail/history pages: /weather/warning/<id>
        if pathname and pathname.startswith(weather.WARNING_PATH):
            return weather.warning_detail_layout(weather.warning_id_from_path(pathname))
        for path, _, module in ALL_PAGES:
            if pathname == path:
                if path in RESTRICTED and not auth.is_admin():
                    return _login_required_panel()
                return module.layout()
        return html.Div([html.H2("Not found"),
                         html.P(f"No page at {pathname}.")])

    @app.callback(
        Output("theme-store", "data"),
        Input("theme-toggle", "n_clicks"),
        State("theme-store", "data"),
        prevent_initial_call=True)
    def toggle_theme(_, dark):
        return not dark

    @app.callback(Output("app-root", "className"), Input("theme-store", "data"))
    def apply_theme(dark):
        base = "app dark" if dark else "app light"
        return base + " has-titlebar" if DESKTOP else base

    for _, _, module in ALL_PAGES:
        module.register_callbacks(app)
    station.register_callbacks(app)

    if autostart:
        from app.collector import manager
        manager.autostart()
        # Watchdog + threshold alerting for the always-on deployment: restarts
        # stalled collectors and sends webhook notifications on state changes.
        from app.watchdog import supervisor
        supervisor.ensure_started()

    return app

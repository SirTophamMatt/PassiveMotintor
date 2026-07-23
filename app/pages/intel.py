"""Intel Tool: a small internal-tools area behind a simple shared password.

Currently hosts the **Fire burnt-area chart generator** — a self-contained
HTML/SVG/JS tool (``assets/intel_fire_chart.html``) embedded in an isolated
``<iframe srcDoc=...>`` so its own light-themed UI and client-side logic run
untouched inside the dark dashboard shell.

The password is a light *convenience* gate, NOT the admin login: unlocking this
page grants no admin rights and shares nothing with ``auth.is_admin()``. Default
password is ``intel``; override with the ``UM_INTEL_PASSWORD`` environment
variable. Because it is a low-value shared secret (and, like the admin gate,
still enforced server-side via the Flask session), the gate is rendered by the
page itself rather than via the RESTRICTED admin-only mechanism in factory.py.
"""
import hmac
import logging
import os

import flask
from dash import Input, Output, State, dcc, html
from dash.exceptions import PreventUpdate

from app.config import BUNDLE_DIR

log = logging.getLogger(__name__)

SESSION_KEY = "intel_ok"
# Shared page password. Env override lets a deploy change it without a code edit.
INTEL_PASSWORD = os.environ.get("UM_INTEL_PASSWORD", "intel")

# The generator lives in assets/ because the PyInstaller spec already bundles
# that folder (datas=[("assets","assets")]), so BUNDLE_DIR/assets resolves both
# as a script and when frozen. Read once at import; it is static.
_CHART_HTML_PATH = os.path.join(BUNDLE_DIR, "assets", "intel_fire_chart.html")


def _load_chart_html():
    try:
        with open(_CHART_HTML_PATH, "r", encoding="utf-8") as f:
            return f.read()
    except OSError as e:
        log.error("Intel chart asset unreadable (%s): %s", _CHART_HTML_PATH, e)
        return "<!doctype html><p style='font-family:sans-serif;padding:24px'>" \
               "Chart generator asset is missing.</p>"


_CHART_HTML = _load_chart_html()


def _unlocked():
    """Whether the current session has cleared the Intel password gate.

    False outside a request context (e.g. at import time)."""
    try:
        return bool(flask.session.get(SESSION_KEY))
    except RuntimeError:
        return False


# --------------------------------------------------------------------------- #
# Layout
# --------------------------------------------------------------------------- #
def layout():
    return html.Div([
        html.H2("Intel Tool"),
        html.Div(_body(), id="intel-body"),
    ])


def _body():
    return _tool() if _unlocked() else _gate()


def _gate(error=None):
    children = [
        html.H4("Enter password"),
        html.P("This area is protected by a shared password.", className="muted"),
        dcc.Input(id="intel-password", type="password", placeholder="Password",
                  className="text-input wide", n_submit=0),
        html.Button("Enter", id="intel-unlock-btn", className="btn btn-primary",
                    style={"marginTop": "8px"}),
    ]
    if error:
        children.append(html.Div(error, className="error-text",
                                 style={"marginTop": "8px"}))
    return html.Div(children, className="panel", style={"maxWidth": "420px"})


def _tool():
    return html.Div([
        html.Div([
            html.Span("Fire burnt-area chart generator", className="muted"),
            html.Button("Lock", id="intel-lock-btn", className="btn",
                        style={"float": "right"}),
            html.Div(id="intel-lock-dummy"),
        ], style={"marginBottom": "10px"}),
        html.Iframe(
            srcDoc=_CHART_HTML,
            style={"width": "100%", "height": "88vh", "minHeight": "820px",
                   "border": "1px solid var(--line, #d8dee2)",
                   "borderRadius": "6px", "background": "#ffffff"},
        ),
    ])


# --------------------------------------------------------------------------- #
# Callbacks
# --------------------------------------------------------------------------- #
def register_callbacks(app):
    @app.callback(
        Output("intel-body", "children"),
        Input("intel-unlock-btn", "n_clicks"),
        Input("intel-password", "n_submit"),
        State("intel-password", "value"),
        prevent_initial_call=True)
    def unlock(clicks, submits, password):
        # Dash re-fires this when the input is (re)inserted even with
        # prevent_initial_call — the output (intel-body) already exists. Guard
        # on a real click/submit (same lesson as the admin login callback).
        if not clicks and not submits:
            raise PreventUpdate
        if password and hmac.compare_digest(str(password), INTEL_PASSWORD):
            flask.session[SESSION_KEY] = True
            return _tool()
        return _gate(error="Incorrect password." if password
                     else "Enter the password.")

    @app.callback(
        Output("intel-body", "children", allow_duplicate=True),
        Input("intel-lock-btn", "n_clicks"),
        prevent_initial_call=True)
    def lock(n_clicks):
        # Critical guard: the Lock button is inserted after unlocking, which
        # fires this callback with n_clicks=None — without the guard it would
        # lock the page straight back up.
        if not n_clicks:
            raise PreventUpdate
        flask.session.pop(SESSION_KEY, None)
        return _gate()

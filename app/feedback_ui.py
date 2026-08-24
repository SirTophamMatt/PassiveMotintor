"""The feedback widget: a floating button on every page and its modal form.

Shell-level, not a page — mounted once by ``factory._shell_layout`` and driven
by callbacks registered here, the same arrangement as ``app/ticker.py``.

The whole thing is one Dash callback (:func:`register_callbacks`) rather than
one per button, because open / close / submit all write to the same outputs and
splitting them would need ``allow_duplicate`` on nine outputs. ``ctx`` tells the
single handler which control fired.

Context the reporter never has to type — the page they were on, their browser,
and their truncated network — is collected server-side inside the callback,
where ``flask.request`` is live (a Dash callback is a POST to
``/_dash-update-component``, so it has a real request context).
"""
import logging

import flask
from dash import Input, Output, State, ctx, dcc, html

from app import feedback, geoip
from app.analytics import client_ip

log = logging.getLogger(__name__)

_KIND_OPTIONS = [
    {"label": "  Bug report", "value": "bug"},
    {"label": "  Suggestion", "value": "suggestion"},
]
_SEVERITY_OPTIONS = [
    {"label": "Low — cosmetic or minor annoyance", "value": "low"},
    {"label": "Medium — a feature works, but wrongly", "value": "medium"},
    {"label": "High — something is unusable or data looks wrong", "value": "high"},
]

_HIDDEN = {"display": "none"}
_SHOWN = {"display": "block"}


def button():
    """The floating action button, bottom-right on every page."""
    return html.Button(
        [html.Span("✎", className="fab-icon"), html.Span("Feedback")],
        id="fb-open", className="fab", n_clicks=0,
        title="Report a bug or suggest an improvement")


def modal():
    """The form itself. Rendered up-front and hidden with CSS rather than built
    on demand, so the callback can reset its fields by value."""
    return html.Div([
        html.Div([
            html.Div([
                html.H3("Send feedback", className="fb-title"),
                html.Button("✕", id="fb-close", className="fb-x", n_clicks=0,
                            title="Close"),
            ], className="fb-head"),

            html.Div([
                dcc.RadioItems(_KIND_OPTIONS, "bug", id="fb-kind",
                               className="fb-kind", inline=True),

                html.Div([
                    html.Label("How serious is it?"),
                    dcc.Dropdown(_SEVERITY_OPTIONS, "medium", id="fb-severity",
                                 clearable=False, className="fb-dropdown"),
                ], id="fb-severity-wrap"),

                html.Label("Summary"),
                dcc.Input(id="fb-subject", type="text", className="text-input wide",
                          maxLength=feedback.MAX_SUBJECT, debounce=False,
                          placeholder="One line — e.g. 'Flood map markers "
                                      "disappear after 10 minutes'"),

                html.Label("Details"),
                dcc.Textarea(
                    id="fb-message", className="text-input wide fb-textarea",
                    maxLength=feedback.MAX_MESSAGE,
                    placeholder="What happened, what you expected instead, and "
                                "how to make it happen again."),

                html.Div([
                    html.Div([
                        html.Label("Your name (optional)"),
                        dcc.Input(id="fb-name", type="text",
                                  className="text-input wide",
                                  maxLength=feedback.MAX_NAME),
                    ], className="fb-col"),
                    html.Div([
                        html.Label("Your email (optional)"),
                        dcc.Input(id="fb-email", type="email",
                                  className="text-input wide",
                                  maxLength=feedback.MAX_EMAIL,
                                  placeholder="Only used to reply to you"),
                    ], className="fb-col"),
                ], className="fb-row"),

                html.P("The page you are on, your browser version and your "
                       "network (with the address truncated) are attached "
                       "automatically to help us reproduce the problem.",
                       className="muted fb-note"),

                html.Div([
                    html.Button("Send", id="fb-submit",
                                className="btn btn-primary", n_clicks=0),
                    html.Button("Cancel", id="fb-cancel", className="btn",
                                n_clicks=0),
                ], className="fb-actions"),
            ], id="fb-body"),

            html.Div(id="fb-result"),
            # Present from the first render, revealed once a report is filed.
            # A callback Input that does not yet exist never fires, so this
            # cannot be created inside the receipt.
            html.Div(html.Button("Close", id="fb-done",
                                 className="btn btn-primary", n_clicks=0),
                     id="fb-done-wrap", style=_HIDDEN),
        ], className="fb-panel"),
    ], id="fb-modal", className="fb-modal fb-hidden")


def _receipt(row):
    """What the reporter sees after a successful send. The reference is the
    point of this panel, so it gets its own line and a selectable style."""
    delivered = row.get("email_status") == "sent"
    return html.Div([
        html.Div("✓", className="fb-tick"),
        html.H3("Thanks — that's been logged."),
        html.P("Your reference is:"),
        html.Div(row["ref"], className="fb-ref"),
        html.P(
            "It has been emailed to the Watchdesk maintainer." if delivered else
            "It has been saved. Email delivery is not available right now, so "
            "the maintainer will pick it up from the admin queue instead.",
            className="muted"),
        html.P("Quote that reference if you follow this up.", className="muted"),
    ], className="fb-receipt")


def register_callbacks(app):
    @app.callback(
        Output("fb-severity-wrap", "style"),
        Input("fb-kind", "value"))
    def show_severity(kind):
        # Severity is a bug-report concept; asking a suggestion how severe it
        # is just makes the form longer.
        return _SHOWN if kind == "bug" else _HIDDEN

    @app.callback(
        Output("fb-modal", "className"),
        Output("fb-body", "style"),
        Output("fb-result", "children"),
        Output("fb-done-wrap", "style"),
        Output("fb-kind", "value"),
        Output("fb-severity", "value"),
        Output("fb-subject", "value"),
        Output("fb-message", "value"),
        Output("fb-name", "value"),
        Output("fb-email", "value"),
        Input("fb-open", "n_clicks"),
        Input("fb-cancel", "n_clicks"),
        Input("fb-close", "n_clicks"),
        Input("fb-submit", "n_clicks"),
        # Only exists once a receipt has rendered; the app is created with
        # suppress_callback_exceptions=True, which is what makes that legal.
        Input("fb-done", "n_clicks"),
        State("fb-kind", "value"),
        State("fb-severity", "value"),
        State("fb-subject", "value"),
        State("fb-message", "value"),
        State("fb-name", "value"),
        State("fb-email", "value"),
        State("url", "pathname"),
        prevent_initial_call=True)
    def drive(_open, _cancel, _close, _submit, _done, kind, severity, subject,
              message, name, email, pathname):
        trigger = ctx.triggered_id

        if trigger in ("fb-cancel", "fb-close", "fb-done"):
            # Closing clears the form, so the next reporter (or the same person
            # filing a second issue) starts from a blank one.
            return ("fb-modal fb-hidden", _SHOWN, None, _HIDDEN,
                    "bug", "medium", "", "", "", "")

        if trigger == "fb-open":
            return ("fb-modal", _SHOWN, None, _HIDDEN,
                    "bug", "medium", "", "", "", "")

        # --- submit ---------------------------------------------------- #
        keep = (kind, severity, subject, message, name, email)
        try:
            row = feedback.submit(
                kind=kind, message=message, subject=subject,
                reporter_name=name, reporter_email=email,
                severity=severity if kind == "bug" else "",
                page_path=pathname or "",
                user_agent=flask.request.headers.get("User-Agent", ""),
                ip_prefix=geoip.truncate(client_ip()))
        except Exception:
            log.exception("Feedback submission failed")
            row = {"error": "Something went wrong sending that. Please try "
                            "again in a moment."}

        if row.get("error"):
            # Keep the form as the reporter left it — retyping a paragraph
            # because of a missing email address is the worst possible outcome.
            return ("fb-modal", _SHOWN,
                    html.P(row["error"], className="error-text fb-error"),
                    _HIDDEN) + keep

        return ("fb-modal", _HIDDEN, _receipt(row), _SHOWN) + keep

"""Admin page: login-gated controls for the public web deployment.

When not authenticated this renders a login form. Once logged in it exposes the
state-changing controls that used to live on the Flood/Power pages — collection
Start/Stop, auto-start toggles, event-tag management and data export — plus
links to the (also gated) Settings and Import pages.

Every state-changing callback re-checks auth.is_admin() server-side, so the
gating does not rely on the UI merely hiding a button.
"""
from dash import ALL, Input, Output, State, ctx, dcc, html
from dash.exceptions import PreventUpdate

from app import auth, feedback, mailer, notify
from app import tags as tag_store
from app import ui
from app.collector import manager
from app.config import load_config, save_config
from app.modules.storm.scraper import radar_ids as storm_radar_ids
from app.watchdog import supervisor


# --------------------------------------------------------------------------- #
# Layout
# --------------------------------------------------------------------------- #
def layout():
    return html.Div([
        html.H2("Admin"),
        dcc.Download(id="admin-export-download"),
        dcc.Interval(id="admin-status-interval", interval=15_000, n_intervals=0),
        html.Div(_body(), id="admin-body"),
    ])


def _body():
    return _panel() if auth.is_admin() else _login_form()


def _login_form(error=None):
    children = [
        html.H4("Admin login"),
        html.P("Enter the admin password to manage collection, tags and export.",
               className="muted"),
        dcc.Input(id="admin-password", type="password", placeholder="Password",
                  className="text-input wide", n_submit=0),
        html.Button("Log in", id="admin-login-btn", className="btn btn-primary",
                    style={"marginTop": "8px"}),
    ]
    if not auth.admin_password_configured():
        children.append(html.Div(
            "⚠ No admin password is set. Set UM_ADMIN_PASSWORD in the "
            "environment, or an admin_password_hash in config.json, before "
            "deploying publicly.", className="error-text",
            style={"marginTop": "8px"}))
    if error:
        children.append(html.Div(error, className="error-text",
                                 style={"marginTop": "8px"}))
    return html.Div(children, className="panel", style={"maxWidth": "420px"})


def _panel():
    cfg = load_config()
    flood_auto = cfg["flood"].get("autostart", True)
    power_auto = cfg["power"].get("autostart", False)
    fire_auto = cfg["fire"].get("autostart", True)
    weather_auto = cfg["weather"].get("autostart", True)
    rainfall_auto = cfg["rainfall"].get("autostart", True)
    storm_auto = cfg["storm"].get("autostart", True)
    intel_auto = cfg["intel"].get("autostart", True)
    headless = cfg["power"].get("headless", False)
    return html.Div([
        html.Div([
            html.Button("Log out", id="admin-logout-btn", className="btn",
                        style={"float": "right"}),
            html.Div(id="admin-logout-dummy"),
        ]),

        # --- Collection controls ------------------------------------------- #
        html.Div([
            html.Div([
                html.H4("Flood collection"),
                html.Button("Start", id="admin-flood-start", className="btn btn-primary"),
                html.Button("Stop", id="admin-flood-stop", className="btn"),
                dcc.Checklist(
                    id="admin-flood-autostart",
                    options=[{"label": " Auto-start on server boot", "value": "on"}],
                    value=["on"] if flood_auto else [], style={"marginTop": "8px"}),
                html.Div(id="admin-flood-status", className="muted",
                         style={"marginTop": "8px"}),
            ], className="panel"),
            html.Div([
                html.H4("Fire collection"),
                html.Button("Start", id="admin-fire-start", className="btn btn-primary"),
                html.Button("Stop", id="admin-fire-stop", className="btn"),
                dcc.Checklist(
                    id="admin-fire-autostart",
                    options=[{"label": " Auto-start on server boot", "value": "on"}],
                    value=["on"] if fire_auto else [], style={"marginTop": "8px"}),
                html.Div("VicEmergency incident/warning feed — public, no "
                         "credentials.", className="muted",
                         style={"fontSize": "12px", "marginTop": "6px"}),
                html.Div(id="admin-fire-status", className="muted",
                         style={"marginTop": "8px"}),
            ], className="panel"),
            html.Div([
                html.H4("Weather collection"),
                html.Button("Start", id="admin-weather-start", className="btn btn-primary"),
                html.Button("Stop", id="admin-weather-stop", className="btn"),
                dcc.Checklist(
                    id="admin-weather-autostart",
                    options=[{"label": " Auto-start on server boot", "value": "on"}],
                    value=["on"] if weather_auto else [], style={"marginTop": "8px"}),
                html.Div("BoM warnings + rainfall (api.weather.bom.gov.au) — "
                         "public, no credentials.", className="muted",
                         style={"fontSize": "12px", "marginTop": "6px"}),
                html.Div(id="admin-weather-status", className="muted",
                         style={"marginTop": "8px"}),
            ], className="panel"),
            html.Div([
                html.H4("Rainfall collection (AWS network)"),
                html.Button("Start", id="admin-rainfall-start", className="btn btn-primary"),
                html.Button("Stop", id="admin-rainfall-stop", className="btn"),
                html.Button("Fetch now", id="admin-rainfall-fetch", className="btn",
                            style={"marginLeft": "4px"}),
                dcc.Checklist(
                    id="admin-rainfall-autostart",
                    options=[{"label": " Auto-start on server boot", "value": "on"}],
                    value=["on"] if rainfall_auto else [], style={"marginTop": "8px"}),
                html.Div("BoM ~101 AWS stations, one state-page request/cycle. "
                         "Public, no credentials.", className="muted",
                         style={"fontSize": "12px", "marginTop": "6px"}),
                html.Div(id="admin-rainfall-status", className="muted",
                         style={"marginTop": "8px"}),
            ], className="panel"),
            html.Div([
                html.H4("Storm tracking (BoM radar)"),
                html.Button("Start", id="admin-storm-start", className="btn btn-primary"),
                html.Button("Stop", id="admin-storm-stop", className="btn"),
                dcc.Checklist(
                    id="admin-storm-autostart",
                    options=[{"label": " Auto-start on server boot", "value": "on"}],
                    value=["on"] if storm_auto else [], style={"marginTop": "8px"}),
                html.Div("Radar(s) "
                         + ", ".join(storm_radar_ids(cfg))
                         + " echo frames, ~5 min cadence. Public, no credentials.",
                         className="muted",
                         style={"fontSize": "12px", "marginTop": "6px"}),
                html.Div(id="admin-storm-status", className="muted",
                         style={"marginTop": "8px"}),
            ], className="panel"),
            html.Div([
                html.H4("Intelligence feed"),
                html.Button("Start", id="admin-intel-start", className="btn btn-primary"),
                html.Button("Stop", id="admin-intel-stop", className="btn"),
                html.Button("Detect now", id="admin-intel-now", className="btn"),
                dcc.Checklist(
                    id="admin-intel-autostart",
                    options=[{"label": " Auto-start on server boot", "value": "on"}],
                    value=["on"] if intel_auto else [], style={"marginTop": "8px"}),
                html.Div("Journals what CHANGED across every layer. Fetches "
                         "nothing — reads only what the collectors stored.",
                         className="muted",
                         style={"fontSize": "12px", "marginTop": "6px"}),
                html.Div(id="admin-intel-status", className="muted",
                         style={"marginTop": "8px"}),
            ], className="panel"),
            html.Div([
                html.H4("Power collection"),
                html.Button("Start", id="admin-power-start", className="btn btn-primary"),
                html.Button("Stop", id="admin-power-stop", className="btn"),
                dcc.Checklist(
                    id="admin-power-headless",
                    options=[{"label": " Run browser hidden (headless)", "value": "on"}],
                    value=["on"] if headless else [], style={"marginTop": "8px"}),
                dcc.Checklist(
                    id="admin-power-autostart",
                    options=[{"label": " Auto-start on server boot", "value": "on"}],
                    value=["on"] if power_auto else []),
                html.Div("Headless/auto-start apply on the next Start. Power needs a "
                         "visible browser (Xvfb on a headless host) and working "
                         "EM-COP credentials.", className="muted",
                         style={"fontSize": "12px", "marginTop": "6px"}),
                html.Div(id="admin-power-status", className="muted",
                         style={"marginTop": "8px"}),
            ], className="panel"),
        ], className="panel-row"),

        html.Div(id="admin-collector-status", className="panel"),

        # --- Event tags ---------------------------------------------------- #
        html.H3("Event tags"),
        html.P("Tag a date range so its flood + power data can be viewed and "
               "exported together. Leave the end date empty for an ongoing "
               "event, then close it off later under Edit tag.",
               className="muted"),
        html.Div([
            html.Div([
                html.H4("Create tag"),
                dcc.Input(id="admin-tag-name", type="text", placeholder="Event name",
                          className="text-input wide"),
                html.Div(dcc.DatePickerRange(
                    id="admin-tag-dates",
                    display_format="YYYY-MM-DD",
                    start_date_placeholder_text="Start",
                    end_date_placeholder_text="End (optional)"),
                    style={"marginTop": "8px"}),
                dcc.Input(id="admin-tag-notes", type="text", placeholder="Notes (optional)",
                          className="text-input wide", style={"marginTop": "8px"}),
                html.Button("Create tag", id="admin-tag-create",
                            className="btn btn-primary", style={"marginTop": "8px"}),
                html.Div(id="admin-tag-create-status", className="muted",
                         style={"marginTop": "8px"}),
            ], className="panel"),
            html.Div([
                html.H4("Edit tag"),
                dcc.Dropdown(id="admin-tag-edit-select", options=_tag_dropdown(),
                             placeholder="Select a tag to edit",
                             className="dropdown"),
                html.Div(id="admin-tag-edit-current", className="muted",
                         style={"marginTop": "6px"}),
                dcc.Input(id="admin-tag-edit-name", type="text",
                          placeholder="Event name", className="text-input wide",
                          style={"marginTop": "8px"}),
                html.Div(dcc.DatePickerRange(
                    id="admin-tag-edit-dates",
                    display_format="YYYY-MM-DD",
                    start_date_placeholder_text="Start",
                    end_date_placeholder_text="End (empty = ongoing)"),
                    style={"marginTop": "8px"}),
                html.Div([
                    dcc.Input(id="admin-tag-edit-start-time", type="text",
                              placeholder="Start time HH:MM",
                              className="text-input"),
                    dcc.Input(id="admin-tag-edit-end-time", type="text",
                              placeholder="End time HH:MM",
                              className="text-input",
                              style={"marginLeft": "8px"}),
                ], style={"marginTop": "8px"}),
                html.Div("Times are optional — a date on its own runs from "
                         "00:00 to 23:59.", className="muted",
                         style={"fontSize": "12px", "marginTop": "4px"}),
                dcc.Input(id="admin-tag-edit-notes", type="text",
                          placeholder="Notes (optional)",
                          className="text-input wide", style={"marginTop": "8px"}),
                html.Div([
                    html.Button("Save changes", id="admin-tag-edit-save",
                                className="btn btn-primary"),
                    html.Button("End now", id="admin-tag-edit-end-now",
                                className="btn", style={"marginLeft": "8px"}),
                ], style={"marginTop": "8px"}),
                html.Div("“End now” closes an ongoing event at the current "
                         "time.", className="muted",
                         style={"fontSize": "12px", "marginTop": "4px"}),
                html.Div(id="admin-tag-edit-status", className="muted",
                         style={"marginTop": "8px"}),
            ], className="panel"),
            html.Div([
                html.H4("Existing tags"),
                html.Div(id="admin-tag-list", children=_tag_list()),
                html.Label("Delete tag", style={"marginTop": "8px"}),
                dcc.Dropdown(id="admin-tag-delete-select", options=_tag_dropdown(),
                             placeholder="Select a tag", className="dropdown"),
                html.Button("Delete", id="admin-tag-delete", className="btn",
                            style={"marginTop": "8px"}),
                html.Div(id="admin-tag-delete-status", className="muted",
                         style={"marginTop": "8px"}),
            ], className="panel"),
        ], className="panel-row"),

        # --- Feedback ------------------------------------------------------ #
        html.H3(id="admin-fb-heading"),
        html.Div([
            html.Div([
                html.Div([
                    html.Div([
                        html.Label("Show"),
                        dcc.Dropdown(
                            [{"label": "New", "value": "new"},
                             {"label": "Open", "value": "open"},
                             {"label": "Closed", "value": "closed"},
                             {"label": "All", "value": "all"}],
                            "new", id="admin-fb-status-filter", clearable=False,
                            className="dropdown"),
                    ], className="fb-col"),
                    html.Div([
                        html.Label("Type"),
                        dcc.Dropdown(
                            [{"label": "All", "value": "all"},
                             {"label": "Bug reports", "value": "bug"},
                             {"label": "Suggestions", "value": "suggestion"}],
                            "all", id="admin-fb-kind-filter", clearable=False,
                            className="dropdown"),
                    ], className="fb-col"),
                ], className="fb-row"),
                html.Div(id="admin-fb-list", style={"marginTop": "12px"}),
                html.Div(id="admin-fb-status", className="muted",
                         style={"marginTop": "8px"}),
            ], className="panel"),
            html.Div([
                html.H4("Report delivery"),
                html.Div(id="admin-fb-mail-state", children=_fb_mail_state(),
                         className="muted"),
                html.Button("Send test email", id="admin-fb-mail-test",
                            className="btn", style={"marginTop": "10px"}),
                html.Div(id="admin-fb-mail-status", className="muted",
                         style={"marginTop": "8px"}),
                html.P("Reports are stored the moment they are submitted, so "
                       "anything listed here exists whether or not the email got "
                       "out. Use Resend on a report showing a failed delivery "
                       "once the mail settings are fixed.",
                       className="muted",
                       style={"fontSize": "12px", "marginTop": "10px"}),
                dcc.Link("Mail + feedback settings", href="/settings",
                         className="nav-link"),
            ], className="panel"),
        ], className="panel-row"),

        # --- Export -------------------------------------------------------- #
        html.H3("Export data"),
        html.Div([
            html.Div([
                html.H4("Export a range"),
                html.Label("By tag"),
                dcc.Dropdown(id="admin-export-tag", options=_tag_dropdown(),
                             placeholder="Select a tag", className="dropdown"),
                html.Label("…or a custom range (overrides the tag)",
                           style={"marginTop": "8px"}),
                html.Div(dcc.DatePickerRange(
                    id="admin-export-dates", display_format="YYYY-MM-DD",
                    start_date_placeholder_text="Start",
                    end_date_placeholder_text="End")),
                dcc.Checklist(id="admin-export-modules",
                              options=[{"label": " Flood", "value": "flood"},
                                       {"label": " Power", "value": "power"},
                                       {"label": " Rainfall", "value": "rainfall"}],
                              value=["flood", "power", "rainfall"],
                              style={"marginTop": "8px"}),
                html.Button("⤓ Download XLSX", id="admin-export-btn",
                            className="btn btn-primary", style={"marginTop": "8px"}),
                html.Div(id="admin-export-status", className="muted",
                         style={"marginTop": "8px"}),
            ], className="panel"),
            html.Div([
                html.H4("Other admin pages"),
                dcc.Link("Settings (credentials, intervals, thresholds)",
                         href="/settings", className="nav-link"),
                html.Br(),
                dcc.Link("Import legacy data", href="/import", className="nav-link"),
                html.Br(),
                html.Button("Send test notification", id="admin-notify-test",
                            className="btn", style={"marginTop": "10px"}),
                dcc.Checklist(
                    id="admin-notify-pause",
                    options=[{"label": " Pause all notifications", "value": "paused"}],
                    value=["paused"] if cfg["notify"].get("paused") else [],
                    style={"marginTop": "8px"}),
                html.Div(id="admin-notify-status", className="muted",
                         style={"marginTop": "6px"}),
                html.Button("Set / change admin password",
                            id="admin-pw-toggle", className="btn",
                            style={"marginTop": "10px"}),
                html.Div([
                    dcc.Input(id="admin-new-password", type="password",
                              placeholder="New password", className="text-input wide",
                              style={"marginTop": "8px"}),
                    html.Button("Save password", id="admin-pw-save", className="btn",
                                style={"marginTop": "8px"}),
                    html.Div(id="admin-pw-status", className="muted",
                             style={"marginTop": "8px"}),
                ]),
            ], className="panel"),
        ], className="panel-row"),
    ])


def _fb_heading():
    c = feedback.counts()
    return "Feedback — %d new, %d open, %d closed" % (
        c["new"], c["open"], c["closed"])


def _fb_mail_state():
    """Whether a report submitted right now would actually be emailed. Stated
    up front because the failure is silent otherwise: the form keeps accepting
    reports perfectly happily while none of them reach a mailbox."""
    cfg = load_config()
    to = feedback.recipient(cfg)
    if not cfg.get("feedback", {}).get("email_enabled", True):
        return ui.status_pill(False, text_off="Email delivery turned off")
    if not mailer.configured(cfg):
        return ui.status_pill(False, text_off="SMTP not configured")
    if not to:
        return ui.status_pill(False, text_off="No recipient address set")
    return html.Div([ui.status_pill(True, text_on="Emailing reports to"),
                     html.Span(" " + to)])


def _fb_row(row):
    """One report. The message is shown in full rather than truncated: a bug
    report exists to be read, and a queue of 40-character previews just means
    opening every one of them somewhere else."""
    r = feedback.normalise(row.to_dict())
    kind = str(r["kind"])
    bits = [html.Span(str(r["ref"]), className="fb-admin-ref"),
            html.Span(feedback.KINDS.get(kind, kind),
                      className="fb-tag fb-tag-" + kind)]
    if r.get("severity"):
        bits.append(html.Span(str(r["severity"]),
                              className="fb-tag fb-tag-" + str(r["severity"])))
    bits.append(html.Span(str(r["submitted_at"])[:16], className="muted"))
    if r.get("email_status") in ("failed", "skipped"):
        bits.append(html.Span("not emailed", className="fb-tag fb-tag-high",
                              title=str(r.get("email_error") or "")))

    who = r.get("reporter_name") or "Anonymous"
    if r.get("reporter_email"):
        who = "%s <%s>" % (who, r["reporter_email"])

    ref = str(r["ref"])
    return html.Div([
        html.Div(bits, className="fb-admin-head"),
        html.Div(str(r.get("subject") or ""),
                 style={"fontWeight": "600", "marginTop": "4px"}),
        html.Div(str(r["message"]), className="fb-admin-body"),
        html.Div("%s - from %s" % (who, r.get("page_path") or "unknown page"),
                 className="muted", style={"fontSize": "12px"}),
        html.Div([
            html.Button("Open", id={"type": "fb-set", "ref": ref, "to": "open"},
                        className="btn"),
            html.Button("Close", id={"type": "fb-set", "ref": ref, "to": "closed"},
                        className="btn"),
            html.Button("Resend email", id={"type": "fb-resend", "ref": ref},
                        className="btn"),
        ], style={"marginTop": "4px"}),
    ], className="fb-admin-row")


def _fb_list(status="new", kind="all"):
    df = feedback.recent(limit=40, status=status, kind=kind)
    if df.empty:
        return html.Div("No reports match that filter.", className="muted")
    return html.Div([_fb_row(r) for _, r in df.iterrows()])


def _tag_list():
    tags = tag_store.list_tags()
    if not tags:
        return html.Div("No tags yet.", className="muted")
    rows = []
    for t in tags:
        span = t["start_ts"][:16] + "  →  " + (
            t["end_ts"][:16] if t.get("end_ts") else "ongoing")
        rows.append(html.Div([html.Strong(t["name"]), html.Span(f"  {span}")],
                             style={"marginBottom": "4px"}))
    return html.Div(rows)


def _tag_dropdown():
    return [{"label": t["name"], "value": str(t["id"])}
            for t in tag_store.list_tags()]


def _split_ts(ts):
    """'2026-08-01 09:14:00' -> ('2026-08-01', '09:14') for the edit form."""
    if not ts:
        return None, None
    ts = str(ts)
    return ts[:10], (ts[11:16] or None)


def _combine(date, time):
    """Recombine a date-picker value with an optional 'HH:MM' time. Returns the
    date alone when no time is given, so tags._normalise applies its whole-day
    bounds."""
    if not date:
        return None
    date = str(date)[:10]
    time = (time or "").strip()
    if not time:
        return date
    parts = time.split(":")
    if len(parts) not in (2, 3) or not all(p.isdigit() for p in parts):
        raise ValueError(f"Time '{time}' must be HH:MM.")
    hh, mm = int(parts[0]), int(parts[1])
    ss = int(parts[2]) if len(parts) == 3 else 0
    if hh > 23 or mm > 59 or ss > 59:
        raise ValueError(f"Time '{time}' is not a valid time.")
    return f"{date} {hh:02d}:{mm:02d}:{ss:02d}"


# --------------------------------------------------------------------------- #
# Callbacks
# --------------------------------------------------------------------------- #
def register_callbacks(app):
    from dash import no_update

    @app.callback(
        Output("admin-body", "children"),
        Input("admin-login-btn", "n_clicks"),
        Input("admin-password", "n_submit"),
        State("admin-password", "value"),
        prevent_initial_call=True)
    def do_login(_clicks, _submits, password):
        # Dash fires callbacks when their input components are (re)inserted
        # into the page even with prevent_initial_call=True, because the
        # output (admin-body) already exists. Guard on real clicks only.
        if not _clicks and not _submits:
            raise PreventUpdate
        if auth.verify_password(password):
            auth.login()
            return _panel()
        return _login_form(error="Incorrect password." if password
                           else "Enter a password.")

    @app.callback(
        Output("admin-body", "children", allow_duplicate=True),
        Input("admin-logout-btn", "n_clicks"),
        prevent_initial_call=True)
    def do_logout(n_clicks):
        # THE critical guard: when the panel is inserted after login, this
        # callback fires with n_clicks=None (see note in do_login) — without
        # the guard it would log the admin straight back out.
        if not n_clicks:
            raise PreventUpdate
        auth.logout()
        return _login_form()

    # --- collection ------------------------------------------------------- #
    @app.callback(
        Output("admin-flood-status", "children"),
        Input("admin-flood-start", "n_clicks"),
        Input("admin-flood-stop", "n_clicks"),
        State("admin-flood-autostart", "value"),
        prevent_initial_call=True)
    def flood_control(_s, _t, autostart):
        if not _s and not _t:
            raise PreventUpdate
        if not auth.is_admin():
            return "Not authorised."
        cfg = load_config()
        cfg["flood"]["autostart"] = "on" in (autostart or [])
        save_config(cfg)
        if ctx.triggered_id == "admin-flood-start":
            _, msg = manager.start_flood()
        else:
            _, msg = manager.stop_flood()
        return msg

    @app.callback(
        Output("admin-fire-status", "children"),
        Input("admin-fire-start", "n_clicks"),
        Input("admin-fire-stop", "n_clicks"),
        State("admin-fire-autostart", "value"),
        prevent_initial_call=True)
    def fire_control(_s, _t, autostart):
        if not _s and not _t:
            raise PreventUpdate
        if not auth.is_admin():
            return "Not authorised."
        cfg = load_config()
        cfg["fire"]["autostart"] = "on" in (autostart or [])
        save_config(cfg)
        if ctx.triggered_id == "admin-fire-start":
            _, msg = manager.start_fire()
        else:
            _, msg = manager.stop_fire()
        return msg

    @app.callback(
        Output("admin-weather-status", "children"),
        Input("admin-weather-start", "n_clicks"),
        Input("admin-weather-stop", "n_clicks"),
        State("admin-weather-autostart", "value"),
        prevent_initial_call=True)
    def weather_control(_s, _t, autostart):
        if not _s and not _t:
            raise PreventUpdate
        if not auth.is_admin():
            return "Not authorised."
        cfg = load_config()
        cfg["weather"]["autostart"] = "on" in (autostart or [])
        save_config(cfg)
        if ctx.triggered_id == "admin-weather-start":
            _, msg = manager.start_weather()
        else:
            _, msg = manager.stop_weather()
        return msg

    @app.callback(
        Output("admin-rainfall-status", "children"),
        Input("admin-rainfall-start", "n_clicks"),
        Input("admin-rainfall-stop", "n_clicks"),
        Input("admin-rainfall-fetch", "n_clicks"),
        State("admin-rainfall-autostart", "value"),
        prevent_initial_call=True)
    def rainfall_control(_s, _t, _f, autostart):
        if not _s and not _t and not _f:
            raise PreventUpdate
        if not auth.is_admin():
            return "Not authorised."
        cfg = load_config()
        cfg["rainfall"]["autostart"] = "on" in (autostart or [])
        save_config(cfg)
        if ctx.triggered_id == "admin-rainfall-fetch":
            _, msg = manager.fetch_rainfall_now()
        elif ctx.triggered_id == "admin-rainfall-start":
            _, msg = manager.start_rainfall()
        else:
            _, msg = manager.stop_rainfall()
        return msg

    @app.callback(
        Output("admin-storm-status", "children"),
        Input("admin-storm-start", "n_clicks"),
        Input("admin-storm-stop", "n_clicks"),
        State("admin-storm-autostart", "value"),
        prevent_initial_call=True)
    def storm_control(_s, _t, autostart):
        if not _s and not _t:
            raise PreventUpdate
        if not auth.is_admin():
            return "Not authorised."
        cfg = load_config()
        cfg["storm"]["autostart"] = "on" in (autostart or [])
        save_config(cfg)
        if ctx.triggered_id == "admin-storm-start":
            _, msg = manager.start_storm()
        else:
            _, msg = manager.stop_storm()
        return msg

    @app.callback(
        Output("admin-intel-status", "children"),
        Input("admin-intel-start", "n_clicks"),
        Input("admin-intel-stop", "n_clicks"),
        Input("admin-intel-now", "n_clicks"),
        State("admin-intel-autostart", "value"),
        prevent_initial_call=True)
    def intel_control(_s, _t, _n, autostart):
        if not _s and not _t and not _n:
            raise PreventUpdate
        if not auth.is_admin():
            return "Not authorised."
        if ctx.triggered_id == "admin-intel-now":
            # Runs inline: a pass is a few DB reads, not a scrape.
            from app import intel_feed
            written = intel_feed.detect()
            return (f"Detection pass complete — {written} new "
                    f"entr{'y' if written == 1 else 'ies'}.")
        cfg = load_config()
        cfg["intel"]["autostart"] = "on" in (autostart or [])
        save_config(cfg)
        if ctx.triggered_id == "admin-intel-start":
            _, msg = manager.start_intel()
        else:
            _, msg = manager.stop_intel()
        return msg

    @app.callback(
        Output("admin-power-status", "children"),
        Input("admin-power-start", "n_clicks"),
        Input("admin-power-stop", "n_clicks"),
        State("admin-power-headless", "value"),
        State("admin-power-autostart", "value"),
        prevent_initial_call=True)
    def power_control(_s, _t, headless, autostart):
        if not _s and not _t:
            raise PreventUpdate
        if not auth.is_admin():
            return "Not authorised."
        cfg = load_config()
        cfg["power"]["headless"] = "on" in (headless or [])
        cfg["power"]["autostart"] = "on" in (autostart or [])
        save_config(cfg)
        if ctx.triggered_id == "admin-power-start":
            _, msg = manager.start_power()
            mode = "hidden (headless)" if cfg["power"]["headless"] else "visible"
            return f"{msg} Browser mode: {mode}."
        _, msg = manager.stop_power()
        return msg

    @app.callback(
        Output("admin-collector-status", "children"),
        Input("admin-status-interval", "n_intervals"))
    def collector_status(_):
        if not auth.is_admin():
            return None
        s = manager.status()

        def line(label, d):
            parts = [html.Strong(label + ": "), ui.status_pill(d["running"])]
            if d.get("last_run"):
                parts.append(html.Span(
                    f" — last cycle {d['last_run']} ({d.get('runs', 0)} total)"))
            if d.get("last_error"):
                parts.append(html.Div(f"⚠ {d['last_error']}", className="error-text"))
            return html.Div(parts, style={"marginBottom": "6px"})

        wd = supervisor.state
        watchdog_bits = [html.Strong("Watchdog: "),
                         ui.status_pill(supervisor.is_alive())]
        if wd.get("last_check"):
            watchdog_bits.append(html.Span(
                f" — checked {wd['last_check']} ({wd['checks']} passes; restarts: "
                f"{wd['flood_restarts']} flood / {wd.get('fire_restarts', 0)} fire "
                f"/ {wd.get('weather_restarts', 0)} weather / "
                f"{wd['power_restarts']} power)"))
        if wd.get("last_action"):
            watchdog_bits.append(html.Div(f"Last action: {wd['last_action']}",
                                          className="muted"))
        return html.Div([html.H4("Collector status"),
                         line("Flood", s["flood"]), line("Fire", s["fire"]),
                         line("Weather", s["weather"]),
                         line("Rainfall", s["rainfall"]), line("Storm", s["storm"]),
                         line("Power", s["power"]),
                         line("Intel feed", s["intel"]),
                         html.Div(watchdog_bits)])

    # --- tags ------------------------------------------------------------- #
    @app.callback(
        Output("admin-tag-create-status", "children"),
        Output("admin-tag-list", "children"),
        Output("admin-tag-delete-select", "options"),
        Output("admin-tag-edit-select", "options"),
        Output("admin-export-tag", "options"),
        Output("admin-tag-name", "value"),
        Input("admin-tag-create", "n_clicks"),
        State("admin-tag-name", "value"),
        State("admin-tag-dates", "start_date"),
        State("admin-tag-dates", "end_date"),
        State("admin-tag-notes", "value"),
        prevent_initial_call=True)
    def create_tag(n_clicks, name, start_date, end_date, notes):
        if not n_clicks:
            raise PreventUpdate
        if not auth.is_admin():
            return ("Not authorised.", no_update, no_update, no_update,
                    no_update, no_update)
        try:
            tag_store.create_tag(name, start_date, end_date, notes)
            msg = f"✅ Created tag '{name}'."
            cleared = ""
        except ValueError as e:
            return (f"⚠ {e}", no_update, no_update, no_update, no_update,
                    no_update)
        options = _tag_dropdown()
        return (msg, _tag_list(), options, options, options, cleared)

    @app.callback(
        Output("admin-tag-delete-status", "children"),
        Output("admin-tag-list", "children", allow_duplicate=True),
        Output("admin-tag-delete-select", "options", allow_duplicate=True),
        Output("admin-tag-edit-select", "options", allow_duplicate=True),
        Output("admin-tag-edit-select", "value"),
        Output("admin-export-tag", "options", allow_duplicate=True),
        Input("admin-tag-delete", "n_clicks"),
        State("admin-tag-delete-select", "value"),
        State("admin-tag-edit-select", "value"),
        prevent_initial_call=True)
    def delete_tag(n_clicks, tag_id, editing_id):
        if not n_clicks:
            raise PreventUpdate
        if not auth.is_admin():
            return ("Not authorised.", no_update, no_update, no_update,
                    no_update, no_update)
        if not tag_id:
            return ("Select a tag to delete.", no_update, no_update, no_update,
                    no_update, no_update)
        tag_store.delete_tag(int(tag_id))
        options = _tag_dropdown()
        # Clear the edit form if it was holding the tag just deleted.
        editing = None if editing_id == tag_id else no_update
        return ("🗑 Tag deleted.", _tag_list(), options, options, editing,
                options)

    @app.callback(
        Output("admin-tag-edit-current", "children"),
        Output("admin-tag-edit-name", "value"),
        Output("admin-tag-edit-dates", "start_date"),
        Output("admin-tag-edit-dates", "end_date"),
        Output("admin-tag-edit-start-time", "value"),
        Output("admin-tag-edit-end-time", "value"),
        Output("admin-tag-edit-notes", "value"),
        Input("admin-tag-edit-select", "value"),
        Input("admin-tag-list", "children"))
    def load_tag_for_edit(tag_id, _list):
        """Fill the edit form from the selected tag. Also re-runs when the tag
        list changes, so the form reflects a save rather than the stale values
        that were typed into it."""
        if not tag_id:
            return "", "", None, None, "", "", ""
        tag = tag_store.get_tag(int(tag_id))
        if tag is None:
            return "Tag not found.", "", None, None, "", "", ""
        start_date, start_time = _split_ts(tag["start_ts"])
        end_date, end_time = _split_ts(tag.get("end_ts"))
        current = "Currently: {} → {}".format(
            str(tag["start_ts"])[:16],
            str(tag["end_ts"])[:16] if tag.get("end_ts") else "ongoing")
        return (current, tag["name"], start_date, end_date,
                start_time or "", end_time or "", tag.get("notes") or "")

    @app.callback(
        Output("admin-tag-edit-status", "children"),
        Output("admin-tag-list", "children", allow_duplicate=True),
        Output("admin-tag-delete-select", "options", allow_duplicate=True),
        Output("admin-tag-edit-select", "options", allow_duplicate=True),
        Output("admin-export-tag", "options", allow_duplicate=True),
        Input("admin-tag-edit-save", "n_clicks"),
        Input("admin-tag-edit-end-now", "n_clicks"),
        State("admin-tag-edit-select", "value"),
        State("admin-tag-edit-name", "value"),
        State("admin-tag-edit-dates", "start_date"),
        State("admin-tag-edit-dates", "end_date"),
        State("admin-tag-edit-start-time", "value"),
        State("admin-tag-edit-end-time", "value"),
        State("admin-tag-edit-notes", "value"),
        prevent_initial_call=True)
    def edit_tag(save_clicks, end_clicks, tag_id, name, start_date, end_date,
                 start_time, end_time, notes):
        if not (save_clicks or end_clicks):
            raise PreventUpdate
        if not auth.is_admin():
            return "Not authorised.", no_update, no_update, no_update, no_update
        if not tag_id:
            return ("Select a tag to edit.", no_update, no_update, no_update,
                    no_update)
        try:
            if ctx.triggered_id == "admin-tag-edit-end-now":
                # Ends the stored tag as it is — no other form field applies.
                ended = tag_store.end_tag_now(int(tag_id))
                msg = f"✅ Ended at {ended[:16]}."
            else:
                tag_store.update_tag(
                    int(tag_id), name,
                    _combine(start_date, start_time),
                    _combine(end_date, end_time), notes)
                msg = "✅ Saved."
        except ValueError as e:
            return f"⚠ {e}", no_update, no_update, no_update, no_update
        options = _tag_dropdown()
        return msg, _tag_list(), options, options, options

    # --- export ----------------------------------------------------------- #
    @app.callback(
        Output("admin-export-download", "data"),
        Output("admin-export-status", "children"),
        Input("admin-export-btn", "n_clicks"),
        State("admin-export-tag", "value"),
        State("admin-export-dates", "start_date"),
        State("admin-export-dates", "end_date"),
        State("admin-export-modules", "value"),
        prevent_initial_call=True)
    def do_export(n_clicks, tag_id, start_date, end_date, modules):
        if not n_clicks:
            raise PreventUpdate
        if not auth.is_admin():
            return no_update, "Not authorised."
        from app import export

        modules = modules or []
        label = "range"
        if start_date and end_date:
            start = tag_store._normalise(start_date, end_of_day=False)
            end = tag_store._normalise(end_date, end_of_day=True)
            label = f"{start_date}_to_{end_date}"
        elif tag_id:
            tag = tag_store.get_tag(int(tag_id))
            if not tag:
                return no_update, "Selected tag not found."
            start, end = tag_store.resolve_range(tag)
            label = tag["name"]
        else:
            return no_update, "Pick a tag or a custom date range."

        try:
            filename, data = export.build_export(
                start, end, label=label,
                include_flood="flood" in modules,
                include_power="power" in modules,
                include_rainfall="rainfall" in modules)
        except Exception as e:
            return no_update, f"⚠ Export failed: {e}"
        return dcc.send_bytes(data, filename), f"✅ Exported {filename}."

    # --- feedback ---------------------------------------------------------- #
    # The row buttons use pattern-matching ids because the list length is
    # data-driven; one ALL callback serves every rendered row and re-renders the
    # list afterwards, so an action and its result arrive together.
    @app.callback(
        Output("admin-fb-list", "children"),
        Output("admin-fb-heading", "children"),
        Output("admin-fb-status", "children"),
        Input("admin-fb-status-filter", "value"),
        Input("admin-fb-kind-filter", "value"),
        Input({"type": "fb-set", "ref": ALL, "to": ALL}, "n_clicks"),
        Input({"type": "fb-resend", "ref": ALL}, "n_clicks"))
    def refresh_feedback(status, kind, _set_clicks, _resend_clicks):
        message = ""
        trigger = ctx.triggered_id
        # Re-rendering the list fires this callback again with the new buttons
        # at n_clicks None. Only a truthy value is a real press — without this
        # guard, redrawing the list would replay the last action.
        pressed = bool(ctx.triggered and ctx.triggered[0].get("value"))
        if isinstance(trigger, dict) and pressed:
            if not auth.is_admin():
                message = "Not authorised."
            elif trigger["type"] == "fb-set":
                feedback.set_status(trigger["ref"], trigger["to"])
                message = "%s marked %s." % (trigger["ref"], trigger["to"])
            else:
                ok, detail = feedback.resend(trigger["ref"])
                message = ("OK - " if ok else "Failed - ") + detail
        return _fb_list(status or "new", kind or "all"), _fb_heading(), message

    @app.callback(
        Output("admin-fb-mail-state", "children"),
        Output("admin-fb-mail-status", "children"),
        Input("admin-fb-mail-test", "n_clicks"),
        prevent_initial_call=True)
    def test_feedback_email(n_clicks):
        if not n_clicks:
            raise PreventUpdate
        if not auth.is_admin():
            return _fb_mail_state(), "Not authorised."
        cfg = load_config()
        to = feedback.recipient(cfg)
        if not mailer.configured(cfg) or not to:
            return (_fb_mail_state(),
                    "Set the SMTP host, from-address and recipient in Settings "
                    "first.")
        ok, err = mailer.send(
            "[Watchdesk] Test email",
            "This is a test from the Watchdesk admin page.\n\n"
            "If you are reading it, bug reports and suggestions submitted from "
            "the feedback form will reach this mailbox.",
            to_address=to, cfg=cfg)
        return (_fb_mail_state(),
                ("Test email sent to %s." % to) if ok
                else ("Send failed - %s" % err))

    # --- notifications ----------------------------------------------------- #
    @app.callback(
        Output("admin-notify-status", "children"),
        Input("admin-notify-test", "n_clicks"),
        prevent_initial_call=True)
    def test_notification(n_clicks):
        if not n_clicks:
            raise PreventUpdate
        if not auth.is_admin():
            return "Not authorised."
        if not notify.configured():
            return "⚠ No webhook URL set — add one in Settings first."
        ok = notify.send("Test notification — webhook configuration works. 🎉",
                         force=True)
        return ("✅ Test sent — check your channel."
                if ok else "❌ Send failed — see unified_monitor.log for detail.")

    @app.callback(
        Output("admin-notify-status", "children", allow_duplicate=True),
        Input("admin-notify-pause", "value"),
        prevent_initial_call=True)
    def toggle_notify_pause(value):
        if not auth.is_admin():
            raise PreventUpdate
        paused = "paused" in (value or [])
        cfg = load_config()
        cfg["notify"]["paused"] = paused
        save_config(cfg)
        return ("🔕 All notifications paused (the test button still works)."
                if paused else "🔔 Notifications active.")

    # --- admin password --------------------------------------------------- #
    @app.callback(
        Output("admin-pw-status", "children"),
        Input("admin-pw-save", "n_clicks"),
        State("admin-new-password", "value"),
        prevent_initial_call=True)
    def set_password(n_clicks, new_password):
        if not n_clicks:
            raise PreventUpdate
        if not auth.is_admin():
            return "Not authorised."
        if not new_password or len(new_password) < 6:
            return "⚠ Use at least 6 characters."
        auth.set_admin_password(new_password)
        return ("✅ Admin password saved to config.json. Note: UM_ADMIN_PASSWORD "
                "in the environment, if set, overrides it.")

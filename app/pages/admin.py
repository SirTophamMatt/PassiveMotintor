"""Admin page: login-gated controls for the public web deployment.

When not authenticated this renders a login form. Once logged in it exposes the
state-changing controls that used to live on the Flood/Power pages — collection
Start/Stop, auto-start toggles, event-tag management and data export — plus
links to the (also gated) Settings and Import pages.

Every state-changing callback re-checks auth.is_admin() server-side, so the
gating does not rely on the UI merely hiding a button.
"""
from dash import Input, Output, State, ctx, dcc, html

from app import auth
from app import tags as tag_store
from app import ui
from app.collector import manager
from app.config import load_config, save_config


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
               "exported together. Leave the end date empty for an ongoing event.",
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
                                       {"label": " Power", "value": "power"}],
                              value=["flood", "power"], style={"marginTop": "8px"}),
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
        if auth.verify_password(password):
            auth.login()
            return _panel()
        return _login_form(error="Incorrect password." if password
                           else "Enter a password.")

    @app.callback(
        Output("admin-body", "children", allow_duplicate=True),
        Input("admin-logout-btn", "n_clicks"),
        prevent_initial_call=True)
    def do_logout(_):
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
        Output("admin-power-status", "children"),
        Input("admin-power-start", "n_clicks"),
        Input("admin-power-stop", "n_clicks"),
        State("admin-power-headless", "value"),
        State("admin-power-autostart", "value"),
        prevent_initial_call=True)
    def power_control(_s, _t, headless, autostart):
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

        return html.Div([html.H4("Collector status"),
                         line("Flood", s["flood"]), line("Power", s["power"])])

    # --- tags ------------------------------------------------------------- #
    @app.callback(
        Output("admin-tag-create-status", "children"),
        Output("admin-tag-list", "children"),
        Output("admin-tag-delete-select", "options"),
        Output("admin-export-tag", "options"),
        Output("admin-tag-name", "value"),
        Input("admin-tag-create", "n_clicks"),
        State("admin-tag-name", "value"),
        State("admin-tag-dates", "start_date"),
        State("admin-tag-dates", "end_date"),
        State("admin-tag-notes", "value"),
        prevent_initial_call=True)
    def create_tag(_, name, start_date, end_date, notes):
        if not auth.is_admin():
            return "Not authorised.", no_update, no_update, no_update, no_update
        try:
            tag_store.create_tag(name, start_date, end_date, notes)
            msg = f"✅ Created tag '{name}'."
            cleared = ""
        except ValueError as e:
            return f"⚠ {e}", no_update, no_update, no_update, no_update
        return (msg, _tag_list(), _tag_dropdown(), _tag_dropdown(), cleared)

    @app.callback(
        Output("admin-tag-delete-status", "children"),
        Output("admin-tag-list", "children", allow_duplicate=True),
        Output("admin-tag-delete-select", "options", allow_duplicate=True),
        Output("admin-export-tag", "options", allow_duplicate=True),
        Input("admin-tag-delete", "n_clicks"),
        State("admin-tag-delete-select", "value"),
        prevent_initial_call=True)
    def delete_tag(_, tag_id):
        if not auth.is_admin():
            return "Not authorised.", no_update, no_update, no_update
        if not tag_id:
            return "Select a tag to delete.", no_update, no_update, no_update
        tag_store.delete_tag(int(tag_id))
        return ("🗑 Tag deleted.", _tag_list(), _tag_dropdown(), _tag_dropdown())

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
    def do_export(_, tag_id, start_date, end_date, modules):
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
                include_power="power" in modules)
        except Exception as e:
            return no_update, f"⚠ Export failed: {e}"
        return dcc.send_bytes(data, filename), f"✅ Exported {filename}."

    # --- admin password --------------------------------------------------- #
    @app.callback(
        Output("admin-pw-status", "children"),
        Input("admin-pw-save", "n_clicks"),
        State("admin-new-password", "value"),
        prevent_initial_call=True)
    def set_password(_, new_password):
        if not auth.is_admin():
            return "Not authorised."
        if not new_password or len(new_password) < 6:
            return "⚠ Use at least 6 characters."
        auth.set_admin_password(new_password)
        return ("✅ Admin password saved to config.json. Note: UM_ADMIN_PASSWORD "
                "in the environment, if set, overrides it.")

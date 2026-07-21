"""Settings page: EM-COP credentials, URLs, intervals, alert thresholds.

Saved to config.json (gitignored) — credentials are never hardcoded.
"""
from dash import Input, Output, State, dcc, html

from app import auth
from app.config import CONFIG_FILE, load_config, save_config


def _field(label, field_id, value, input_type="text", **kwargs):
    return html.Div([
        html.Label(label),
        dcc.Input(id=field_id, type=input_type, value=value,
                  className="text-input wide", **kwargs),
    ], style={"marginBottom": "10px"})


def layout():
    cfg = load_config()
    return html.Div([
        html.H2("Settings"),
        html.P(["Stored locally in ", html.Code(CONFIG_FILE),
                " — this file is gitignored and never committed."], className="muted"),
        html.Div([
            html.Div([
                html.H4("EM-COP Credentials"),
                _field("Username", "set-username", cfg["emcop"]["username"]),
                _field("Password", "set-password", cfg["emcop"]["password"],
                       input_type="password"),
            ], className="panel"),
            html.Div([
                html.H4("EM-COP URLs"),
                _field("Login URL", "set-login-url", cfg["emcop"]["login_url"]),
                _field("Power outages URL", "set-power-url", cfg["emcop"]["power_url"]),
                _field("Open after login (quick-launch)", "set-after-url",
                       cfg["emcop"]["after_login_url"]),
            ], className="panel"),
        ], className="panel-row"),
        html.Div([
            html.Div([
                html.H4("Road Disruptions (VicRoads)"),
                html.P(["Request a free key at the ",
                        html.A("VicRoads Data Exchange",
                               href="https://data-exchange.vicroads.vic.gov.au/",
                               target="_blank"),
                        " (Disruptions - Road). Collection stays idle until both "
                        "the feed URL and key are set."], className="muted",
                       style={"fontSize": "12px"}),
                _field("Feed URL", "set-roads-url", cfg["roads"]["feed_url"],
                       placeholder="https://api.opendata.transport.vic.gov.au/…/v3"),
                _field("API key (sent as 'KeyId' header)", "set-roads-key",
                       cfg["roads"]["api_key"], input_type="password"),
                _field("Fetch interval (minutes)", "set-roads-interval",
                       cfg["roads"]["interval_minutes"], input_type="number", min=1),
            ], className="panel"),
        ], className="panel-row"),
        html.Div([
            html.Div([
                html.H4("Collection Intervals"),
                _field("Flood fetch interval (minutes)", "set-flood-interval",
                       cfg["flood"]["interval_minutes"], input_type="number", min=1),
                _field("Power fetch interval (seconds)", "set-power-interval",
                       cfg["power"]["interval_seconds"], input_type="number", min=15),
            ], className="panel"),
            html.Div([
                html.H4("Alert Thresholds (customers off)"),
                _field("High alert above", "set-alert-high",
                       cfg["alerts"]["high_customers_off"], input_type="number", min=0),
                _field("Low alert above", "set-alert-low",
                       cfg["alerts"]["low_customers_off"], input_type="number", min=0),
            ], className="panel"),
            html.Div([
                html.H4("Notifications"),
                _field("Webhook URL (Slack / Teams / Discord)",
                       "set-notify-webhook", cfg["notify"]["webhook_url"],
                       placeholder="https://hooks.slack.com/services/…"),
                dcc.Checklist(
                    id="set-notify-toggles",
                    options=[
                        {"label": " Power threshold alerts", "value": "power"},
                        {"label": " Flood level alerts", "value": "flood"},
                        {"label": " Road closure alerts", "value": "roads"},
                        {"label": " Watchdog / collector issues", "value": "watchdog"},
                    ],
                    value=[v for v, key in (("power", "on_power_alert"),
                                            ("flood", "on_flood_alert"),
                                            ("roads", "on_roads_alert"),
                                            ("watchdog", "on_watchdog"))
                           if cfg["notify"].get(key, True)]),
                html.Div("Send a test from the Admin page after saving.",
                         className="muted", style={"fontSize": "12px"}),
            ], className="panel"),
        ], className="panel-row"),
        html.Button("Save Settings", id="settings-save-btn", className="btn btn-primary"),
        html.Div(id="settings-status", className="muted", style={"marginTop": "8px"}),
        html.P("Note: interval changes apply the next time a collector is started.",
               className="muted"),
    ])


def register_callbacks(app):
    @app.callback(
        Output("settings-status", "children"),
        Input("settings-save-btn", "n_clicks"),
        State("set-username", "value"),
        State("set-password", "value"),
        State("set-login-url", "value"),
        State("set-power-url", "value"),
        State("set-after-url", "value"),
        State("set-flood-interval", "value"),
        State("set-power-interval", "value"),
        State("set-roads-url", "value"),
        State("set-roads-key", "value"),
        State("set-roads-interval", "value"),
        State("set-alert-high", "value"),
        State("set-alert-low", "value"),
        State("set-notify-webhook", "value"),
        State("set-notify-toggles", "value"),
        prevent_initial_call=True)
    def save(_, username, password, login_url, power_url, after_url,
             flood_interval, power_interval, roads_url, roads_key,
             roads_interval, alert_high, alert_low,
             notify_webhook, notify_toggles):
        if not auth.is_admin():
            return "Not authorised."
        cfg = load_config()
        cfg["emcop"]["username"] = (username or "").strip()
        cfg["emcop"]["password"] = password or ""
        cfg["emcop"]["login_url"] = (login_url or "").strip()
        cfg["emcop"]["power_url"] = (power_url or "").strip()
        cfg["emcop"]["after_login_url"] = (after_url or "").strip()
        cfg["flood"]["interval_minutes"] = int(flood_interval or 5)
        cfg["power"]["interval_seconds"] = int(power_interval or 60)
        cfg["roads"]["feed_url"] = (roads_url or "").strip()
        cfg["roads"]["api_key"] = (roads_key or "").strip()
        cfg["roads"]["interval_minutes"] = int(roads_interval or 3)
        cfg["alerts"]["high_customers_off"] = int(alert_high or 20000)
        cfg["alerts"]["low_customers_off"] = int(alert_low or 10000)
        toggles = notify_toggles or []
        cfg["notify"]["webhook_url"] = (notify_webhook or "").strip()
        cfg["notify"]["on_power_alert"] = "power" in toggles
        cfg["notify"]["on_flood_alert"] = "flood" in toggles
        cfg["notify"]["on_roads_alert"] = "roads" in toggles
        cfg["notify"]["on_watchdog"] = "watchdog" in toggles
        try:
            save_config(cfg)
            return "✅ Settings saved."
        except OSError as e:
            return f"❌ Could not save settings: {e}"

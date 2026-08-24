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
            html.Div([
                html.H4("Storm Radars"),
                html.P(["Comma-separated BoM radar product IDs to track. Last "
                        "digit = range (1=512 km, 2=256, 3=128, 4=64). Sites with "
                        "built-in coordinates: IDR02 Melbourne, IDR14 Mt Gambier, "
                        "IDR31 Albany — others need coords under "
                        "storm.radar_sites in config.json to place cells on the "
                        "map."], className="muted", style={"fontSize": "12px"}),
                _field("Radar IDs", "set-storm-radars",
                       ", ".join(cfg["storm"]["radar_ids"]),
                       placeholder="IDR023, IDR143"),
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
        html.Div([
            html.Div([
                html.H4("Outgoing Email (SMTP)"),
                html.P("Used to deliver bug reports and suggestions from the "
                       "feedback form. Leave the host blank to disable email — "
                       "reports are still stored and listed on the Admin page. "
                       "The password can instead be set in the UM_SMTP_PASSWORD "
                       "environment variable, which is the better option for the "
                       "container deployment.",
                       className="muted", style={"fontSize": "12px"}),
                _field("SMTP host", "set-smtp-host", cfg["smtp"]["host"],
                       placeholder="smtp.example.com"),
                _field("Port", "set-smtp-port", cfg["smtp"]["port"],
                       input_type="number", min=1),
                html.Label("Transport security"),
                dcc.Dropdown(
                    [{"label": "Auto (SSL on 465, STARTTLS otherwise)",
                      "value": "auto"},
                     {"label": "STARTTLS", "value": "starttls"},
                     {"label": "Implicit TLS / SMTPS", "value": "ssl"},
                     {"label": "None (localhost relay only)", "value": "none"}],
                    cfg["smtp"].get("security", "auto"), id="set-smtp-security",
                    clearable=False, style={"maxWidth": "560px"}),
                _field("Username", "set-smtp-user", cfg["smtp"]["username"],
                       placeholder="Leave blank for an unauthenticated relay"),
                _field("Password", "set-smtp-password", cfg["smtp"]["password"],
                       input_type="password"),
                _field("From address", "set-smtp-from",
                       cfg["smtp"]["from_address"],
                       placeholder="watchdesk@example.com"),
                _field("From name", "set-smtp-from-name",
                       cfg["smtp"]["from_name"]),
            ], className="panel"),
            html.Div([
                html.H4("Feedback Form"),
                html.P("The Feedback button appears on every page. Every "
                       "submission is stored first and emailed second, so an "
                       "unreachable mail server never loses a report.",
                       className="muted", style={"fontSize": "12px"}),
                _field("Send reports to", "set-feedback-recipient",
                       cfg["feedback"]["recipient"],
                       placeholder="WatchdeskMonitor@mattlamont.me"),
                _field("Max submissions per network per hour",
                       "set-feedback-rate", cfg["feedback"]["max_per_hour"],
                       input_type="number", min=0),
                dcc.Checklist(
                    id="set-feedback-toggles",
                    options=[{"label": " Email reports as they arrive",
                              "value": "email"}],
                    value=(["email"] if cfg["feedback"].get("email_enabled", True)
                           else [])),
                html.Div("0 submissions/hour removes the rate limit. Send a test "
                         "email from the Admin page after saving.",
                         className="muted", style={"fontSize": "12px"}),
            ], className="panel"),
            html.Div([
                html.H4("Visitor Geolocation"),
                html.P("Resolves a rough location (country / state / city) for "
                       "each visitor. The client IP is TRUNCATED before it is "
                       "stored or sent to the provider — last octet zeroed for "
                       "IPv4, interface identifier dropped for IPv6 — so traffic "
                       "can be located to a city and grouped by network, but no "
                       "individual address is ever written to disk. Turn this off "
                       "to keep counting views and store no location at all.",
                       className="muted", style={"fontSize": "12px"}),
                dcc.Checklist(
                    id="set-geo-toggles",
                    options=[{"label": " Resolve visitor locations",
                              "value": "enabled"}],
                    value=["enabled"] if cfg["geo"].get("enabled", True) else []),
                _field("Provider URL ({ip} is replaced)", "set-geo-provider",
                       cfg["geo"]["provider_url"]),
                html.Div("Default is ip-api.com: no key, 45 lookups/minute, and "
                         "each network is looked up once and cached. Its free "
                         "tier is HTTP-only and licensed for non-commercial use.",
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
        State("set-storm-radars", "value"),
        State("set-alert-high", "value"),
        State("set-alert-low", "value"),
        State("set-notify-webhook", "value"),
        State("set-notify-toggles", "value"),
        State("set-smtp-host", "value"),
        State("set-smtp-port", "value"),
        State("set-smtp-security", "value"),
        State("set-smtp-user", "value"),
        State("set-smtp-password", "value"),
        State("set-smtp-from", "value"),
        State("set-smtp-from-name", "value"),
        State("set-feedback-recipient", "value"),
        State("set-feedback-rate", "value"),
        State("set-feedback-toggles", "value"),
        State("set-geo-toggles", "value"),
        State("set-geo-provider", "value"),
        prevent_initial_call=True)
    def save(_, username, password, login_url, power_url, after_url,
             flood_interval, power_interval, roads_url, roads_key,
             roads_interval, storm_radars, alert_high, alert_low,
             notify_webhook, notify_toggles, smtp_host, smtp_port,
             smtp_security, smtp_user, smtp_password, smtp_from,
             smtp_from_name, feedback_recipient, feedback_rate,
             feedback_toggles, geo_toggles, geo_provider):
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
        # Storm radars: comma/;-separated product IDs. Only overwrite when at
        # least one is given, so clearing the box can't silently stop tracking.
        radars = [t.strip().upper()
                  for t in (storm_radars or "").replace(";", ",").split(",")
                  if t.strip()]
        ungeoref = []
        if radars:
            cfg["storm"]["radar_ids"] = radars
            from app.modules.storm.scraper import RADAR_SITES
            known = set(RADAR_SITES) | set(cfg["storm"].get("radar_sites", {}))
            ungeoref = [r for r in radars if r[:5] not in known]
        cfg["alerts"]["high_customers_off"] = int(alert_high or 20000)
        cfg["alerts"]["low_customers_off"] = int(alert_low or 10000)
        toggles = notify_toggles or []
        cfg["notify"]["webhook_url"] = (notify_webhook or "").strip()
        cfg["notify"]["on_power_alert"] = "power" in toggles
        cfg["notify"]["on_flood_alert"] = "flood" in toggles
        cfg["notify"]["on_roads_alert"] = "roads" in toggles
        cfg["notify"]["on_watchdog"] = "watchdog" in toggles
        cfg["smtp"]["host"] = (smtp_host or "").strip()
        cfg["smtp"]["port"] = int(smtp_port or 587)
        cfg["smtp"]["security"] = smtp_security or "auto"
        cfg["smtp"]["username"] = (smtp_user or "").strip()
        cfg["smtp"]["password"] = smtp_password or ""
        cfg["smtp"]["from_address"] = (smtp_from or "").strip()
        cfg["smtp"]["from_name"] = (smtp_from_name or "").strip()
        cfg["feedback"]["recipient"] = (feedback_recipient or "").strip()
        # 0 is meaningful here (no limit), so `or` would be wrong — only
        # a genuinely empty box falls back to the default.
        cfg["feedback"]["max_per_hour"] = (
            int(feedback_rate) if feedback_rate not in (None, "") else 5)
        cfg["feedback"]["email_enabled"] = "email" in (feedback_toggles or [])
        cfg["geo"]["enabled"] = "enabled" in (geo_toggles or [])
        cfg["geo"]["provider_url"] = (geo_provider or "").strip()
        try:
            save_config(cfg)
        except OSError as e:
            return f"❌ Could not save settings: {e}"
        msg = "✅ Settings saved."
        if radars:
            msg += f" Tracking radars: {', '.join(radars)} (applies next storm cycle)."
        if ungeoref:
            msg += (f" ⚠ No site coordinates for {', '.join(ungeoref)} — their "
                    "cells won't be placed on the map until coords are added.")
        return msg

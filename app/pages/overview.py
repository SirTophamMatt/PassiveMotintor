"""Overview page: cross-module KPIs, collector status, EM-COP quick-launch,
plus at-a-glance power trends, the outage map, and flooding-station graphs."""
from dash import Input, Output, dcc, html

from app import auth, ui
from app.collector import manager
from app.config import load_config
from app.modules.emcop import launcher
from app.modules.fire import data as fire_data
from app.modules.flood import data as flood_data
from app.modules.power import data as power_data
from app.modules.storm import data as storm_data
from app.modules.weather import data as weather_data
from app.pages import fire as fire_page
from app.pages import flood as flood_page
from app.pages import power as power_page


def _emcop_panel():
    """Server-side quick-launch opens a browser ON the host, so it is admin-only
    (and only meaningful on the desktop build / an interactive host). The
    components are always present but hidden for non-admins, so the status-poll
    callback always has its output to write to; the launch itself is re-checked
    server-side."""
    hidden = not auth.is_admin()
    return html.Div([
        html.H4("EM-COP"),
        html.P("Open EM-COP in a browser window, logged in with your saved credentials."),
        html.Button("Launch EM-COP", id="emcop-launch-btn", className="btn btn-primary"),
        html.Div(id="emcop-launch-status", className="muted", style={"marginTop": "8px"}),
    ], className="panel", style={"display": "none"} if hidden else {})


def layout():
    panels = [
        html.Div([
            html.H4("Collectors"),
            html.Div(id="overview-collectors"),
        ], className="panel"),
        _emcop_panel(),
    ]
    return html.Div([
        html.Div([
            html.H2("Overview", style={"display": "inline-block"}),
            html.Button("⤓ Briefing PDF", id="overview-pdf-btn",
                        className="btn btn-primary",
                        style={"float": "right", "marginTop": "6px"}),
            html.Div(id="overview-pdf-status", className="muted",
                     style={"clear": "both"}),
            dcc.Download(id="overview-pdf-download"),
        ]),
        dcc.Interval(id="overview-interval", interval=30_000, n_intervals=0),
        html.Div(id="overview-kpis", className="kpi-row"),
        html.Div(panels, className="panel-row"),

        # The state above answers "what is happening"; this answers "what
        # changed while I was away". Full log on /feed.
        html.Div([
            html.H3("What changed", style={"display": "inline-block"}),
            dcc.Link("Full Intelligence Feed →", href="/feed",
                     className="gauge-link", style={"marginLeft": "12px"}),
        ]),
        html.Div(id="overview-whats-changed", className="feed feed-compact"),

        html.H3("Power Outages"),
        html.Div(id="overview-power-trends", className="graph-grid"),
        html.Div(dcc.Graph(id="overview-power-map", style={"height": "560px"},
                           config=ui.MAP_CONFIG),
                 className="graph-card"),

        html.H3("Fire / Incidents"),
        html.Div(dcc.Graph(id="overview-fire-map", style={"height": "560px"},
                           config=ui.MAP_CONFIG),
                 className="graph-card"),

        html.H3("Flooding Stations"),
        html.Div(id="overview-flood-graphs", className="graph-grid"),
    ])


# --------------------------------------------------------------------------- #
# Console layout Overview — one screen: warnings | map | what changed.
# Rendered for "/" when the viewer picks the Console layout (app/shell.py).
# --------------------------------------------------------------------------- #
LEVEL_COLOURS = {
    "Emergency Warning": "#e5484d",
    "Watch and Act": "#f5a623",
    "Advice": "#d9bb2b",
}


def warning_rows(limit=10):
    """Active warnings from both authorities, most severe first, as compact
    rows. Shared with the wall display. Built on `briefing._warnings`, so the
    Advice cap and the no-BoM-"level" rule are the briefing's own."""
    from app import briefing
    warnings, omitted_advice = briefing._warnings()
    rows = []
    for w in warnings[:limit]:
        if w.source == "VicEmergency":
            colour = LEVEL_COLOURS.get(w.level, "var(--muted)")
            heading = w.level
        else:
            colour = "var(--accent)"
            heading = "BoM · " + str(w.kind)
        meta = [str(w.kind)] if w.source == "VicEmergency" else []
        if w.issued:
            meta.append("issued " + w.issued.strftime("%H:%M"))
        rows.append(html.Div([
            html.Span(className="warn-diamond", style={"background": colour}),
            html.Div([
                html.Div(heading, className="warn-level", style={"color": colour}),
                dcc.Link(w.title or "Warning", href=w.url or "/fire",
                         className="warn-title"),
                html.Div(" · ".join(meta), className="warn-meta") if meta else None,
            ]),
        ], className="warn-row"))
    hidden = max(0, len(warnings) - limit) + omitted_advice
    if not rows:
        rows.append(html.Div("No active warnings.", className="muted"))
    elif hidden:
        rows.append(html.Div(f"+ {hidden} more — see Fire and Weather",
                             className="muted warn-more"))
    return rows


def source_rows(snap):
    from app import briefing
    rows = []
    for s in snap.sources:
        ok = s.is_healthy
        age = briefing.describe_age(s.age_minutes)
        rows.append(html.Div([
            html.Span("●", className="status-on" if ok else "status-off"),
            html.Span(" " + s.name, className="source-name"),
            html.Span(age, className="source-age"),
        ], className="source-row" + ("" if ok else " source-stale")))
    return rows or [html.Div("Source status unavailable.", className="muted")]


def recent_changes(limit=8, hours=12):
    """Newest feed entries trimmed to their delta lines (context skipped: it is
    the expensive part and a compact card has no room for it)."""
    from app import intel_feed
    from app.pages import feed as feed_page
    entries = intel_feed.entries(hours=hours, limit=limit, with_context=False)
    if not entries:
        return html.Div(f"No changes detected in the last {hours} hours.",
                        className="muted")
    for entry in entries:
        entry["lines"] = entry["lines"][:2]
    return [feed_page._entry_card(entry, is_new=False) for entry in entries]


def console_layout():
    return html.Div([
        dcc.Interval(id="ops-interval", interval=30_000, n_intervals=0),
        html.Div([
            html.Div([
                html.Div([html.H3("Active warnings"),
                          dcc.Link("Fire →", href="/fire", className="ops-more")],
                         className="ops-head"),
                html.Div(id="ops-warnings", className="ops-scroll"),
                html.Div([html.H3("Sources"),
                          dcc.Link("Briefing →", href="/briefing",
                                   className="ops-more")],
                         className="ops-head"),
                html.Div(id="ops-sources", className="ops-sources"),
            ], className="ops-col"),
            html.Div([
                dcc.Graph(id="ops-map", config=ui.MAP_CONFIG,
                          className="ops-map-graph", style={"height": "100%"}),
                dcc.Link("Open full map →", href="/map", className="ops-map-link"),
            ], className="ops-map"),
            html.Div([
                html.Div([html.H3("What changed"),
                          dcc.Link("Feed →", href="/feed", className="ops-more")],
                         className="ops-head"),
                # Actions sit above the feed, not below it: the bottom-right
                # corner belongs to the floating Feedback button.
                html.Div([
                    dcc.Link("Open briefing", href="/briefing",
                             className="btn btn-primary"),
                    html.Button("⤓ Briefing PDF", id="ops-pdf-btn", className="btn"),
                ], className="ops-actions"),
                html.Div(id="ops-pdf-status", className="muted"),
                dcc.Download(id="ops-pdf-download"),
                html.Div(id="ops-changes",
                         className="feed feed-compact ops-scroll"),
            ], className="ops-col"),
        ], className="ops-grid"),
    ], className="ops-page")


def _pdf():
    from dash import no_update

    from app import reporting
    try:
        filename, pdf_bytes = reporting.build_overview_pdf()
    except reporting.ReportingUnavailable as e:
        return no_update, f"⚠ {e}"
    except Exception as e:
        return no_update, f"⚠ Could not build report: {e}"
    return dcc.send_bytes(pdf_bytes, filename), "✅ Report generated."


def register_callbacks(app):
    @app.callback(
        Output("ops-warnings", "children"),
        Output("ops-sources", "children"),
        Output("ops-changes", "children"),
        Input("ops-interval", "n_intervals"))
    def refresh_console(_):
        from app import situation
        return warning_rows(), source_rows(situation.current()), recent_changes()

    @app.callback(
        Output("ops-map", "figure"),
        Input("ops-interval", "n_intervals"),
        Input("theme-store", "data"))
    def refresh_console_map(_, dark):
        from app.pages import unified
        fig = unified.map_figure(unified.DEFAULT_LAYERS, bool(dark),
                                 uirevision="ops-map")
        fig.update_layout(margin=dict(l=0, r=0, t=0, b=0),
                          legend=dict(y=0.01, yanchor="bottom", x=0.01))
        return fig

    @app.callback(
        Output("ops-pdf-download", "data"),
        Output("ops-pdf-status", "children"),
        Input("ops-pdf-btn", "n_clicks"),
        prevent_initial_call=True)
    def make_console_pdf(_):
        return _pdf()

    @app.callback(
        Output("overview-whats-changed", "children"),
        Input("overview-interval", "n_intervals"))
    def refresh_changes(_):
        """The five most recent changes, trimmed to the delta line — the full
        context lines live on /feed. Context lookups are skipped here because
        the compact card has no room for them and they are the expensive part."""
        return recent_changes(limit=5)

    @app.callback(
        Output("overview-kpis", "children"),
        Output("overview-collectors", "children"),
        Input("overview-interval", "n_intervals"))
    def refresh(_):
        cfg = load_config()
        totals = power_data.latest_totals()
        flooding = flood_data.flooding_station_count()

        def fmt(value):
            return f"{int(value):,}" if value is not None else "—"

        if totals:
            off = totals.get("customers_off")
            high = cfg["alerts"]["high_customers_off"]
            low = cfg["alerts"]["low_customers_off"]
            if off is not None and off > high:
                accent, alert = "#d62728", "HIGH ALERT"
            elif off is not None and off > low:
                accent, alert = "#ff7f0e", "Low alert"
            else:
                accent, alert = "#2ca02c", "Normal"
            kpis = [
                ui.kpi_card("Customers Off", fmt(off), accent),
                ui.kpi_card("Power Dependant Off", fmt(totals.get("power_dependant_off"))),
                ui.kpi_card("Planned", fmt(totals.get("planned"))),
                ui.kpi_card("Unplanned", fmt(totals.get("unplanned"))),
                ui.kpi_card("Alert Level", alert, accent),
            ]
        else:
            kpis = [ui.kpi_card("Power Data", "No data yet")]
        kpis.append(ui.kpi_card(
            "Stations Flooding", str(flooding),
            "#d62728" if flooding else "#2ca02c"))

        fire_counts = fire_data.latest_counts()
        kpis.append(ui.kpi_card(
            "Active Fires", str(fire_counts["active_fires"]),
            "#ff5722" if fire_counts["active_fires"] else "#2ca02c"))
        # Kept as two cards, not one total: an Emergency Warning and a Watch &
        # Act are different actions, so summing them lets either level speak for
        # the other (5 Advice-adjacent Watch & Acts reading as an emergency, or
        # a lone Emergency Warning hiding inside a bigger number).
        kpis.append(ui.kpi_card(
            "Emergency Warnings", str(fire_counts["emergency"]),
            "#d62728" if fire_counts["emergency"] else "#2ca02c"))
        kpis.append(ui.kpi_card(
            "Watch & Act", str(fire_counts["watch_act"]),
            "#ff7f0e" if fire_counts["watch_act"] else "#2ca02c"))

        wcounts = weather_data.warning_counts()
        kpis.append(ui.kpi_card(
            "BoM Warnings", str(wcounts["total"]),
            "#ff7f0e" if wcounts["total"] else "#2ca02c"))

        scounts = storm_data.latest_counts()
        kpis.append(ui.kpi_card(
            "Storm Cells (strong)", f"{scounts['total']} ({scounts['strong']})",
            "#d62728" if scounts["strong"]
            else ("#ff7f0e" if scounts["total"] else "#2ca02c")))

        status = manager.status()
        flood_s, power_s = status["flood"], status["power"]
        fire_s, weather_s = status["fire"], status["weather"]
        storm_s = status["storm"]

        def line(label, s, extra=""):
            parts = [html.Strong(label + ": "), ui.status_pill(s["running"])]
            if extra:
                parts.append(html.Span(" " + extra))
            if s.get("last_run"):
                parts.append(html.Span(f" — last run {s['last_run']} ({s.get('runs', 0)} cycles)"))
            if s.get("last_error"):
                parts.append(html.Div(f"Last error: {s['last_error']}", className="error-text"))
            return html.Div(parts, style={"marginBottom": "6px"})

        collectors = [
            line("Flood", flood_s),
            line("Fire", fire_s),
            line("Weather", weather_s),
            line("Storm", storm_s),
            line("Power", power_s),
            line("Intelligence feed", status["intel"]),
        ]
        return kpis, collectors

    @app.callback(
        Output("overview-power-trends", "children"),
        Output("overview-power-map", "figure"),
        Output("overview-fire-map", "figure"),
        Output("overview-flood-graphs", "children"),
        Input("overview-interval", "n_intervals"),
        Input("theme-store", "data"))
    def refresh_graphs(_, dark):
        dark = bool(dark)

        # Power: Past Hour + Past Week trends
        df = power_data.load_timeseries()
        if df.empty:
            trends = [html.Div("No power data yet — start collection on the "
                               "Power page.", className="muted")]
        else:
            now = df["timestamp"].max()
            wanted = {"Past Hour", "Past Week"}
            trends = [
                html.Div(dcc.Graph(figure=power_page._trend_figure(
                    df[df["timestamp"] >= now - window], title, dark)),
                    className="graph-card")
                for title, window in power_page.TREND_WINDOWS if title in wanted
            ]

        # Power: outage map
        outages = power_data.active_outages(include_planned=True)
        map_fig = power_page._map_figure(outages, dark)

        # Fire: active incidents & warnings map
        fire_map = fire_page._map_figure(fire_data.active_incidents(), dark)

        # Flood: a graph per currently-flooding station, linked to its
        # detail page (gauge stick, LFG impacts, briefing PDF).
        from app.pages import station as station_page
        flooding = flood_data.current_flooding_stations()
        if flooding:
            flood_graphs = [
                html.Div([
                    dcc.Link("Gauge details, impacts & briefing →",
                             href=station_page.path_for(station),
                             className="gauge-link"),
                    dcc.Graph(figure=flood_page._station_figure(
                        hist, station, label, levels, dark)),
                ], className="graph-card", style={"border": f"3px solid {colour}"})
                for station, hist, label, colour, levels in flooding
            ]
        else:
            flood_graphs = [html.Div("No stations currently at or above flood "
                                     "level.", className="muted")]
        return trends, map_fig, fire_map, flood_graphs

    @app.callback(
        Output("emcop-launch-status", "children"),
        Input("emcop-launch-btn", "n_clicks"),
        prevent_initial_call=True)
    def launch(_):
        if not auth.is_admin():
            return "Not authorised."
        return launcher.launch_emcop(load_config())

    @app.callback(
        Output("emcop-launch-status", "children", allow_duplicate=True),
        Input("overview-interval", "n_intervals"),
        prevent_initial_call=True)
    def poll_launch_status(_):
        from dash import no_update
        message = launcher.get_status()["message"]
        return message or no_update

    @app.callback(
        Output("overview-pdf-download", "data"),
        Output("overview-pdf-status", "children"),
        Input("overview-pdf-btn", "n_clicks"),
        prevent_initial_call=True)
    def make_pdf(_):
        return _pdf()

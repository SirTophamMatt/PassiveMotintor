"""Wall display (/wall): a navigation-free, big-type view for a screen on an
operations-room wall. Public and read-only, like Overview.

The shell hides the sidebar / console header / feedback button on this path
(`shell.root_class` adds `wall-mode`); the news ticker stays, because a pinned
Emergency Warning is exactly what a wall screen should show.

Nothing here fetches: tiles come from the shared `situation` cache (one
computation per 45 s for every viewer), so a screen left open all day costs
the same as any other open tab.
"""
from dash import Input, Output, dcc, get_asset_url, html

from app import situation
from app.pages import overview

# Tiles, in order. Warning levels stay separate tiles — never one total.
TILES = ["emergency", "watch_act", "advice", "gauges_minor", "customers_off",
         "road_closures"]

# The right-hand panel rotates through these every ROTATE_SECONDS.
PANELS = [
    ("changes", "Latest changes"),
    ("warnings", "Active warnings"),
    ("sources", "Data sources"),
]
ROTATE_SECONDS = 15


def layout():
    return html.Div([
        dcc.Interval(id="wall-interval", interval=30_000, n_intervals=0),
        dcc.Interval(id="wall-rotate", interval=ROTATE_SECONDS * 1000,
                     n_intervals=0),
        dcc.Interval(id="wall-clock-tick", interval=1_000, n_intervals=0),
        html.Div([
            html.Div(_brand(), className="wall-brand"),
            html.Div(id="wall-stale", className="wall-stale"),
            html.Div(className="console-spacer"),
            html.Button("⛶ Full screen", id="wall-fullscreen", className="btn",
                        title="Fill the whole screen (Esc to leave)"),
            dcc.Link("Exit", href="/", className="btn"),
            html.Div(id="wall-clock", className="wall-clock"),
        ], className="wall-head"),
        html.Div(id="wall-tiles", className="wall-tiles"),
        html.Div([
            html.Div(dcc.Graph(id="wall-map", config={"displayModeBar": False},
                               className="wall-map-graph",
                               style={"height": "100%"}),
                     className="wall-map"),
            html.Div([
                html.Div(id="wall-panel-title", className="wall-panel-title"),
                html.Div(id="wall-panel-body", className="wall-panel-body"),
                html.Div(id="wall-panel-dots", className="wall-dots"),
            ], className="wall-panel"),
        ], className="wall-body"),
    ], className="wall-page")


def _brand():
    return [
        html.Img(src=get_asset_url("watchdesk-lockup-dark.svg"), alt="Watchdesk",
                 className="brand-mark brand-dark"),
        html.Img(src=get_asset_url("watchdesk-lockup-light.svg"), alt="Watchdesk",
                 className="brand-mark brand-light"),
    ]


def tile(chip):
    tone = chip.active_tone if chip else None
    return html.Div([
        html.Div(chip.label if chip else "—", className="wall-tile-label"),
        html.Div(chip.display if chip else "—", className="wall-tile-value"),
    ], className="wall-tile" + (f" chip-{tone}" if tone else ""))


def stale_banner(snap):
    stale = snap.stale_sources
    if not stale:
        return None
    from app import briefing
    parts = [f"{s.name} {briefing.describe_age(s.age_minutes)}" for s in stale]
    return "⚠ Stale data — " + " · ".join(parts)


def panel_index(n_intervals):
    return (n_intervals or 0) % len(PANELS)


def register_callbacks(app):
    app.clientside_callback(
        """
        function(_) {
            return new Date().toLocaleTimeString('en-AU', {
                hour: '2-digit', minute: '2-digit', hour12: false,
                timeZone: 'Australia/Melbourne'});   // Melbourne, like the data
        }
        """,
        Output("wall-clock", "children"),
        Input("wall-clock-tick", "n_intervals"))

    app.clientside_callback(
        """
        function(n) {
            if (n && document.documentElement.requestFullscreen
                    && !document.fullscreenElement) {
                document.documentElement.requestFullscreen().catch(function() {});
            }
            return window.dash_clientside.no_update;
        }
        """,
        Output("wall-fullscreen", "title"),
        Input("wall-fullscreen", "n_clicks"),
        prevent_initial_call=True)

    @app.callback(
        Output("wall-tiles", "children"),
        Output("wall-stale", "children"),
        Output("wall-stale", "title"),
        Input("wall-interval", "n_intervals"))
    def refresh_tiles(_):
        snap = situation.current()
        banner = stale_banner(snap)
        # The banner is clipped to one line on the wall; the tooltip has it all.
        return [tile(snap.chip(key)) for key in TILES], banner, banner

    @app.callback(
        Output("wall-map", "figure"),
        Input("wall-interval", "n_intervals"),
        Input("theme-store", "data"))
    def refresh_map(_, dark):
        from app.pages import unified
        fig = unified.map_figure(unified.DEFAULT_LAYERS, bool(dark),
                                 uirevision="wall-map")
        fig.update_layout(margin=dict(l=0, r=0, t=0, b=0),
                          legend=dict(y=0.01, yanchor="bottom", x=0.01,
                                      font=dict(size=14)))
        return fig

    @app.callback(
        Output("wall-panel-title", "children"),
        Output("wall-panel-body", "children"),
        Output("wall-panel-dots", "children"),
        Input("wall-rotate", "n_intervals"))
    def rotate(n):
        index = panel_index(n)
        key, title = PANELS[index]
        if key == "changes":
            body = overview.recent_changes(limit=6)
        elif key == "warnings":
            body = overview.warning_rows(limit=8)
        else:
            body = overview.source_rows(situation.current())
        nxt = PANELS[(index + 1) % len(PANELS)][1]
        dots = [html.Span(className="wall-dot" + (" wall-dot-on" if i == index else ""))
                for i in range(len(PANELS))]
        dots.append(html.Span(f"Next: {nxt}", className="wall-next"))
        return title, body, dots

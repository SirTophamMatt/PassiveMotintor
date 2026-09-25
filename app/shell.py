"""Shell chrome that sits around every page: the two layouts (Classic sidebar /
Console top bar), the colour schemes, and the Display panel that switches them.

Both layouts are ALWAYS in the DOM and CSS shows one. That is deliberate: a Dash
callback whose Input component is missing never fires (the feedback-form lesson),
so the console header, the sidebar and every control they listen to exist from
the first render. The choice itself lives in two `persistence="local"` radio
groups — per browser, like the theme and the sound toggle — so a wall screen or
a duty officer's desk keeps its layout across reloads and nobody else's changes.

The shared controls (sounds, display, theme) are rendered ONCE, outside both
layouts, and CSS docks them into whichever layout is showing: at the foot of the
sidebar, or at the right of the console top bar. Rendering them twice would mean
duplicate component ids.
"""
from dash import Input, Output, State, ctx, dcc, get_asset_url, html, no_update

from app import situation, sound_alerts

LAYOUT_CLASSIC = "classic"
LAYOUT_CONSOLE = "console"
LAYOUTS = [
    (LAYOUT_CONSOLE, "Console — top bar + status strip"),
    (LAYOUT_CLASSIC, "Classic — sidebar"),
]
# What a browser gets until it picks otherwise in the Display panel.
DEFAULT_LAYOUT = LAYOUT_CONSOLE

# Scheme id -> label. Each has a dark AND a light variant in style.css
# (`.app.dark.scheme-<id>` / `.app.light.scheme-<id>`); "watchdesk" is the
# original palette and adds no class. A test pins this list to the stylesheet.
SCHEMES = [
    ("watchdesk", "Watchdesk (default)"),
    ("midnight", "Midnight"),
    ("graphite", "Graphite"),
    ("nightops", "Night Ops — low blue"),
    ("contrast", "High contrast"),
]
DEFAULT_SCHEME = "watchdesk"

WALL_PATH = "/wall"

# Console top-bar menus. Paths not listed fall into "Tools" so a page added to
# the factory later is never unreachable from the console layout.
NAV_GROUPS = [
    ("Situation", ["/", "/feed", "/briefing", WALL_PATH]),
    ("Map", ["/map", "/replay"]),
    ("Hazards", ["/flood", "/fire", "/weather", "/storm", "/roads", "/pager",
                 "/power"]),
    ("Tools", ["/intel", "/admin", "/analytics", "/settings", "/import"]),
]


def root_class(dark, layout, scheme, pathname, desktop=False):
    """The app root's className. Pure, so the combinations are testable."""
    classes = ["app", "dark" if dark else "light"]
    layout = layout if layout in dict(LAYOUTS) else DEFAULT_LAYOUT
    classes.append("layout-" + layout)
    if scheme and scheme != DEFAULT_SCHEME and scheme in dict(SCHEMES):
        classes.append("scheme-" + scheme)
    if pathname == WALL_PATH:
        classes.append("wall-mode")
    if desktop:
        classes.append("has-titlebar")
    return " ".join(classes)


def group_items(items):
    """Arrange (path, label) nav items into the console's menu groups, in
    NAV_GROUPS order; unknown paths are appended to the last group."""
    labels = dict(items)
    grouped = []
    placed = set()
    for name, paths in NAV_GROUPS:
        entries = [(p, labels[p]) for p in paths if p in labels]
        placed.update(p for p, _ in entries)
        grouped.append([name, entries])
    stray = [(p, l) for p, l in items if p not in placed]
    grouped[-1][1].extend(stray)
    return [(name, entries) for name, entries in grouped if entries]


# --------------------------------------------------------------------------- #
# Components
# --------------------------------------------------------------------------- #
def _lockups(extra_class=""):
    return [
        html.Img(src=get_asset_url("watchdesk-lockup-dark.svg"), alt="Watchdesk",
                 className="brand-mark brand-dark " + extra_class),
        html.Img(src=get_asset_url("watchdesk-lockup-light.svg"), alt="Watchdesk",
                 className="brand-mark brand-light " + extra_class),
    ]


def console_header():
    """Console layout: top bar (brand, grouped menus, source health) over the
    statewide status strip. Hidden by CSS in the classic layout."""
    return html.Div([
        html.Div([
            dcc.Link(_lockups("brand-compact"), href="/", className="console-brand"),
            html.Nav(id="console-nav", className="console-nav"),
            html.Div(className="console-spacer"),
            dcc.Link(id="console-sources", href="/briefing",
                     className="console-sources",
                     title="Collector freshness — details on the Briefing page"),
        ], className="console-topbar"),
        html.Div([
            html.Span("Now", className="strip-now"),
            html.Div(id="console-strip", className="strip-chips"),
            html.Span(id="console-clock", className="strip-clock"),
        ], className="console-strip"),
        dcc.Interval(id="clock-tick", interval=5_000, n_intervals=0),
    ], className="console-head")


def controls():
    """Sounds + Display + theme, rendered once and docked by CSS."""
    return html.Div([
        sound_alerts.toggle_button(),
        html.Div([
            html.Button("Display", id="display-btn", n_clicks=0,
                        className="btn display-btn",
                        title="Layout and colour scheme (this browser only)"),
            html.Button("☀ / ☾", id="theme-toggle", className="btn theme-btn",
                        title="Toggle light/dark mode"),
        ], className="display-row"),
        html.Div([
            html.Div("Layout", className="display-panel-label"),
            dcc.RadioItems(id="display-layout",
                           options=[{"label": label, "value": value}
                                    for value, label in LAYOUTS],
                           value=DEFAULT_LAYOUT, className="display-options",
                           persistence=True, persistence_type="local"),
            html.Div("Colour scheme", className="display-panel-label"),
            dcc.RadioItems(id="display-scheme",
                           options=[{"label": html.Span([
                               html.Span(className=f"scheme-swatch swatch-{value}"),
                               label]), "value": value}
                               for value, label in SCHEMES],
                           value=DEFAULT_SCHEME, className="display-options",
                           persistence=True, persistence_type="local"),
            html.Div("Light / dark is the ☀ / ☾ button; every scheme has both.",
                     className="display-panel-note"),
            dcc.Link("Open the wall display →", href=WALL_PATH,
                     className="display-panel-link"),
        ], id="display-panel", className="display-panel display-panel-hidden"),
    ], className="shell-controls")


def render_console_nav(items, pathname):
    groups = []
    for name, entries in group_items(items):
        active = any(p == pathname for p, _ in entries)
        groups.append(html.Div([
            html.Button(name + " ▾", className="cnav-btn"
                        + (" cnav-active" if active else ""), type="button"),
            html.Div([dcc.Link(label, href=path,
                               className="cnav-link"
                               + (" cnav-link-active" if path == pathname else ""))
                      for path, label in entries], className="cnav-menu"),
        ], className="cnav-group"))
    return groups


def render_chip(chip):
    tone = chip.active_tone
    return dcc.Link([
        html.B(chip.display, className="chip-value"),
        html.Span(chip.label, className="chip-label"),
    ], href=chip.href,
        className="strip-chip" + (f" chip-{tone}" if tone else ""))


def render_sources(snap):
    stale = snap.stale_sources
    cls = "console-sources " + ("sources-stale" if stale else "sources-ok")
    text = "● " + snap.sources_label
    if stale:
        text += " — " + ", ".join(s.name for s in stale) + " stale"
    return text, cls


# --------------------------------------------------------------------------- #
# Callbacks
# --------------------------------------------------------------------------- #
CLOCK_JS = """
function(_) {
    // timeZoneName gives AEST or AEDT, so the label is right across DST.
    return new Date().toLocaleTimeString('en-AU', {
        hour: '2-digit', minute: '2-digit', hour12: false,
        timeZone: 'Australia/Melbourne', timeZoneName: 'short'});
}
"""

# Clicking a link inside a CSS :focus-within menu leaves focus on the link, so
# the menu would stay open after the SPA navigates. Blur on every route change.
BLUR_JS = """
function(_) {
    if (document.activeElement && document.activeElement.blur) {
        document.activeElement.blur();
    }
    return window.dash_clientside.no_update;
}
"""


def register_callbacks(app):
    app.clientside_callback(CLOCK_JS, Output("console-clock", "children"),
                            Input("clock-tick", "n_intervals"))
    app.clientside_callback(BLUR_JS, Output("console-nav", "title"),
                            Input("url", "pathname"))

    @app.callback(
        Output("display-panel", "className"),
        Input("display-btn", "n_clicks"),
        Input("url", "pathname"),
        State("display-panel", "className"),
        prevent_initial_call=True)
    def toggle_display_panel(_clicks, _path, current):
        hidden = "display-panel display-panel-hidden"
        if ctx.triggered_id != "display-btn":
            return hidden           # navigating closes the panel
        return "display-panel" if "hidden" in (current or "") else hidden

    @app.callback(
        Output("console-strip", "children"),
        Output("console-sources", "children"),
        Output("console-sources", "className"),
        Input("live-tick", "n_intervals"),
        Input("display-layout", "value"))
    def refresh_strip(_tick, layout):
        # Classic viewers never see the strip, so they cost nothing.
        if layout != LAYOUT_CONSOLE:
            return no_update, no_update, no_update
        snap = situation.current()
        text, cls = render_sources(snap)
        return [render_chip(c) for c in snap.chips], text, cls

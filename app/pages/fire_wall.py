# Passive Monitor — Copyright (c) 2026 SirTophamMatt. All rights reserved.
"""Fire wall (/wall/fire): a map-first scenario wall for a fire event.

The map is the page. Fire incidents, fire warnings and burn areas that
intersect or sit within a few km of each other are linked into one AREA OF
OPERATION (`app.fire_areas`), and the map rotates its focus from one area to
the next — optionally with a statewide view in the rotation — with the area's
facts in a card over the map and every area listed beside it (click one to pin
the map there).

Burn areas show how the fire has MOVED: area burnt now but not at the start of
the window is drawn red, area that was and no longer is (a perimeter re-mapped
smaller) blue. The latest single change breathes, the same way a new warning
area does (`assets/map_pulse.js`, layers named `wd-pulse:<deadline>`), for
`PULSE_SECONDS`.

A burn area that grows, or an area gaining a Watch and Act / Emergency Warning,
takes the map for `TAKEOVER_SECONDS` under a pulsing banner. "New" is decided
per BROWSER (the first snapshot only seeds), like the flood wall.

Same rules as every wall: public, read-only, nothing fetched from a source.
The areas are computed once per `fire_areas.CACHE_SECONDS` for every viewer
with the same options.
"""
import logging
import time
from datetime import datetime, timedelta

import plotly.graph_objects as go
from dash import ALL, Input, Output, State, ctx, dcc, html, no_update

from app import auth, fire_areas, fire_demo, shell, situation
from app.pages import wall

log = logging.getLogger(__name__)

PATH = shell.FIRE_WALL_PATH
TEST_PATH = shell.FIRE_WALL_TEST_PATH

REFRESH_SECONDS = 30
ROTATE_CHOICES = [10, 15, 20, 30, 60]
DEFAULT_ROTATE = 20
STATE_KEY = "__state__"
STATE_VIEW = ({"lat": -36.9, "lon": 145.3}, 6.0)

# The latest burn-area change breathes for this long after it was recorded.
# Longer than a new warning's 180 s: the collector runs every 3 min, and a wall
# is glanced at, not watched.
PULSE_SECONDS = 10 * 60
TAKEOVER_SECONDS = 45
FLASH_SECONDS = 15 * 60
AREAS_LISTED = 12
MEMBERS_LISTED = 7

GROWTH_COLOUR = "#ff1744"
REDUCTION_COLOUR = "#2979ff"
BURNT_COLOUR = "#4e342e"
HISTORIC_COLOUR = "#8d6e63"
AREA_OUTLINE = "#00c8ff"

LEVEL_KEYS = {"Emergency Warning": "emergency", "Watch and Act": "watch",
              "Advice": "advice"}

# Map layer chips: (value, label, swatch colour, swatch shape).
LAYERS = [
    ("emergency", "Emergency Warning", "#d62728", "▲"),
    ("watch_act", "Watch and Act", "#ff7f0e", "▲"),
    ("advice", "Advice", "#e6c700", "▲"),
    ("fires", "Fire incidents", "#ff5722", "●"),
    ("other", "Other incidents", "#5b8def", "●"),
    ("burnt", "Burnt area", BURNT_COLOUR, "■"),
    ("change", "Growth / reduction", GROWTH_COLOUR, "■"),
    ("areas", "Areas of operation", AREA_OUTLINE, "┅"),
    ("history", "Historical burn areas", HISTORIC_COLOUR, "■"),
]
DEFAULT_LAYERS = [key for key, *_ in LAYERS if key != "history"]


def _fire_kinds(layers):
    from app.pages import fire as fire_page
    groups = {"emergency": {"Emergency Warning"}, "watch_act": {"Watch and Act"},
              "advice": {"Advice"}, "fires": {"Fire"},
              "other": {"Other incident", fire_page.MET_KIND}}
    kinds = set()
    for layer in layers or []:
        kinds |= groups.get(layer, set())
    return kinds


# --------------------------------------------------------------------------- #
# Model helpers (UI-free, testable without Dash)
# --------------------------------------------------------------------------- #
def update_flashes(flashes, fresh, now):
    """Merge newly-detected events and drop ones past FLASH_SECONDS."""
    out = [f for f in (flashes or []) if now - f["at"] < FLASH_SECONDS]
    known = {f["key"] for f in out}
    for event in fresh:
        if event["key"] not in known:
            out.append({**event, "at": now})
    return out


def _event_rank(flash):
    if flash.get("level") == "Emergency Warning":
        return 0
    if flash["kind"] == "growth":
        return 1
    return 2


def hot_flash(flashes, areas, now):
    """The takeover in force, if any: (flash, area). Most severe, then newest;
    a flash whose member is no longer in any area is skipped."""
    hot = [f for f in (flashes or []) if now - f["at"] < TAKEOVER_SECONDS]
    hot.sort(key=lambda f: (_event_rank(f), -f["at"]))
    for flash in hot:
        area = fire_areas.area_for_member(areas, flash["member"])
        if area is not None:
            return flash, area
    return None, None


def choose_focus(keys, tick, statewide, pin=None, takeover=None):
    """Which area the map shows. Returns (key or None for the whole state,
    page, pages, mode) — mode is takeover | pinned | rotate."""
    seq = ([None] if statewide or not keys else []) + list(keys)
    pages = len(seq)
    if takeover is not None:
        return takeover, None, pages, "takeover"
    if pin == STATE_KEY:
        return None, None, pages, "pinned"
    if pin is not None and pin in keys:
        return pin, None, pages, "pinned"
    page = (tick or 0) % pages
    return seq[page], page, pages, "rotate"


def warning_counts(snap):
    """Fire-related warnings per level (never summed into one figure)."""
    counts = {"Emergency Warning": 0, "Watch and Act": 0, "Advice": 0}
    df = snap.get("incidents")
    if df is None or df.empty:
        return counts
    from app.pages import fire as fire_page
    for row in df.to_dict("records"):
        if row.get("feed_type") != "warning" or not fire_areas.is_fire_warning(row):
            continue
        kind = fire_page._kind(row)
        if kind in counts:
            counts[kind] += 1
    return counts


def fire_count(snap):
    df = snap.get("incidents")
    if df is None or df.empty:
        return 0
    from app.pages import fire as fire_page
    return int((df.apply(fire_page._kind, axis=1) == "Fire").sum())


# --------------------------------------------------------------------------- #
# Components
# --------------------------------------------------------------------------- #
def _layer_option(key, label, colour, shape):
    return {"label": html.Span([
        html.Span(shape, className="fw-swatch", style={"color": colour}),
        label]), "value": key}


def _radio(id_, choices, value, fmt):
    return dcc.RadioItems(id=id_, options=[{"label": fmt(n), "value": n}
                                           for n in choices],
                          value=value, inline=True, className="display-options",
                          persistence=True, persistence_type="local")


def snapshot(link_km, window, test=False):
    """The live snapshot, or — for an admin on the test page — the simulated
    one (`app.fire_demo`). Re-checked here, not just at routing: every Dash
    callback can be POSTed directly, so a flag from the browser proves
    nothing on its own."""
    if test and auth.is_admin():
        return fire_demo.snapshot(link_km, window)
    return fire_areas.current(link_km, window)


def test_locked():
    return html.Div([
        html.H2("Fire wall test run"),
        html.P("The test run shows simulated fires and warnings, so it is "
               "for signed-in admins only — sign in on the Admin page, then "
               "come back to this address."),
        html.A("Go to Admin →", href="/admin", className="btn btn-primary"),
    ], className="panel", style={"maxWidth": "560px", "margin": "40px auto"})


def layout(test=False):
    options = html.Details([
        html.Summary("⚙ Options", className="btn"),
        html.Div([
            html.Div("Next area every", className="display-panel-label"),
            _radio("fiw-rotate-secs", ROTATE_CHOICES, DEFAULT_ROTATE,
                   lambda n: f" {n} s"),
            dcc.Checklist(id="fiw-statewide",
                          options=[{"label": " Include the whole state in the "
                                             "rotation", "value": "state"}],
                          value=["state"], className="display-options",
                          persistence=True, persistence_type="local"),
            html.Div("Link incidents and warnings within",
                     className="display-panel-label"),
            _radio("fiw-link-km", fire_areas.LINK_KM_CHOICES,
                   fire_areas.DEFAULT_LINK_KM, lambda n: f" {n} km"),
            html.Div("Burn-area change over the last",
                     className="display-panel-label"),
            _radio("fiw-window", fire_areas.WINDOW_HOURS_CHOICES,
                   fire_areas.DEFAULT_WINDOW_HOURS, lambda n: f" {n} h"),
            html.Div("Red = burnt now but not at the start of the window; blue "
                     "= no longer mapped as burnt. The latest change breathes.",
                     className="display-panel-note"),
            html.Div("Remembered by this browser only.",
                     className="display-panel-note"),
        ], className="fw-options-panel"),
    ], className="fw-options")

    return html.Div([
        dcc.Interval(id="fiw-interval", interval=REFRESH_SECONDS * 1000,
                     n_intervals=0),
        dcc.Interval(id="fiw-rotate", interval=DEFAULT_ROTATE * 1000,
                     n_intervals=0),
        dcc.Interval(id="wall-clock-tick", interval=1_000, n_intervals=0),
        dcc.Store(id="fiw-seen", storage_type="memory"),
        dcc.Store(id="fiw-flash", storage_type="memory"),
        dcc.Store(id="fiw-pin", storage_type="memory"),
        dcc.Store(id="fiw-test", data=bool(test)),
        dcc.Store(id="fiw-focus-key", storage_type="memory"),
        dcc.Store(id="fiw-highlight", storage_type="memory"),
        wall.header(PATH, stale_id="fiw-stale",
                    extra=([html.Span("TEST RUN", className="fiw-test-badge"),
                            dcc.Link("Leave test", href=PATH, className="btn")]
                           if test else []) + [options]),
        html.Div(id="fiw-tiles", className="wall-tiles fiw-tiles"),
        html.Div(id="fiw-banner", className="fw-banner"),
        html.Div([
            html.Div([
                dcc.Graph(id="fiw-map", className="wall-map-graph",
                          config={"displayModeBar": False, "scrollZoom": True},
                          style={"height": "100%"}),
                dcc.Checklist(id="fiw-layers",
                              options=[_layer_option(*row) for row in LAYERS],
                              value=DEFAULT_LAYERS, inline=True,
                              className="fw-layer-chips",
                              persistence=True, persistence_type="local"),
                html.Div(id="fiw-focus", className="fiw-focus"),
                html.Div(id="fiw-pager", className="wall-dots fiw-pager"),
            ], className="wall-map fiw-map"),
            html.Div([
                html.Div([
                    html.Span("Areas of operation", className="wall-panel-title"),
                    html.Button("▶ Resume rotation", id="fiw-resume",
                                className="btn fiw-resume",
                                style={"display": "none"}),
                ], className="fiw-side-head"),
                html.Div(id="fiw-list", className="fiw-list"),
            ], className="wall-panel fiw-side"),
        ], className="fiw-body"),
    ], className="wall-page fiw-page")


def _chip(key, label, value, tone):
    return situation.Chip(key, label, value, PATH, tone)


def tiles(snap):
    ok = snap.get("ok")
    areas = snap.get("areas") or []
    counts = warning_counts(snap) if ok else {}
    burnt = sum(a.area_ha for a in areas)
    growing = sum(1 for a in areas if a.grown_ha > 0)
    chips = [
        _chip("fires", "Fire incidents", fire_count(snap) if ok else None, "alert"),
        _chip("areas", "Areas of operation", len(areas) if ok else None, None),
        _chip("emergency", "Emergency Warning", counts.get("Emergency Warning"),
              "emergency"),
        _chip("watch_act", "Watch and Act", counts.get("Watch and Act"), "watch"),
        _chip("advice", "Advice", counts.get("Advice"), "advice"),
        _chip("burnt", "Burnt area (ha)", round(burnt) if ok else None, None),
        _chip("growing", f"Growing · last {snap.get('window_hours')} h",
              growing if ok else None, "emergency"),
    ]
    out = [wall.tile(c) for c in chips]
    out[2].title = out[3].title = out[4].title = (
        "Fire-related warnings only (bushfire, grass fire, planned burn …)")
    return out


FIRE_SOURCES = ("VicEmergency",)


def stale_banner(sit):
    relevant = situation.Situation(
        sit.generated_at,
        sources=[s for s in sit.sources if s.name in FIRE_SOURCES])
    return wall.stale_banner(relevant)


def _ha(value):
    return f"{value:,.0f} ha" if value >= 1 else f"{value:,.1f} ha"


def _change_line(area, window_hours, now):
    bits = []
    if area.grown_ha:
        bits.append(html.Span(f"▲ +{_ha(area.grown_ha)}", className="fiw-grow"))
    if area.reduced_ha:
        bits.append(html.Span(f"▼ −{_ha(area.reduced_ha)}", className="fiw-reduce"))
    if not bits:
        return None
    when = ""
    if area.step_at:
        when = f" · latest {area.step_at:%H:%M}"
        if (now - area.step_at).total_seconds() < PULSE_SECONDS:
            when += " (new)"
    return html.Div([*bits, html.Span(f" in the last {window_hours} h{when}",
                                      className="muted")], className="fiw-change")


def _level_badge(level):
    if not level:
        return None
    return html.Span(level, className=f"fiw-level fiw-{LEVEL_KEYS[level]}")


def _counts_line(area):
    parts = [f"{len(area.fires)} fire{'s' if len(area.fires) != 1 else ''}"]
    if area.warnings:
        parts.append(f"{len(area.warnings)} warning"
                     f"{'s' if len(area.warnings) != 1 else ''}")
    if area.others:
        parts.append(f"{len(area.others)} other incident"
                     f"{'s' if len(area.others) != 1 else ''}")
    if area.area_ha:
        parts.append(f"{_ha(area.area_ha)} burnt")
    return " · ".join(parts)


def _member_row(m):
    if m.role == "warning":
        icon, colour = "▲", {"Emergency Warning": "#d62728",
                             "Watch and Act": "#ff7f0e"}.get(m.level, "#e6c700")
        detail = m.level
    elif m.role == "fire":
        icon, colour = "●", "#ff5722"
        detail = " · ".join(b for b in (m.status, m.size) if b) or "Fire"
    elif m.role == "burn":
        icon, colour = "■", BURNT_COLOUR
        detail = "Burn area"
    else:
        icon, colour = "●", "#5b8def"
        detail = m.kind
    return html.Div([
        html.Span(icon, style={"color": colour}, className="fiw-member-icon"),
        html.Span(m.location, className="fiw-member-name"),
        html.Span(detail, className="muted fiw-member-detail"),
    ], className="fiw-member")


def focus_card(area, snap, mode, now):
    """The card over the map for the area in focus (or the state view)."""
    window = snap.get("window_hours")
    if area is None:
        areas = snap.get("areas") or []
        if not snap.get("ok"):
            text = "Areas of operation unavailable — " + (snap.get("error") or
                                                         "see the server log")
        elif not areas:
            text = "No fire incidents or fire warnings active."
        else:
            text = (f"{len(areas)} area{'s' if len(areas) != 1 else ''} of "
                    "operation across the state")
        return html.Div([
            html.Div([html.Span("Whole state", className="fiw-focus-name"),
                      html.Span("PINNED" if mode == "pinned" else None,
                                className="fiw-tag" if mode == "pinned" else "")],
                     className="fiw-focus-head"),
            html.Div(text, className="muted"),
        ])
    members = (area.warnings + area.fires + area.of_role("burn") + area.others)
    shown = members[:MEMBERS_LISTED]
    more = len(members) - len(shown)
    tag = {"takeover": "LATEST CHANGE", "pinned": "PINNED"}.get(mode)
    return html.Div([
        html.Div([html.Span(area.name, className="fiw-focus-name"),
                  _level_badge(area.level),
                  html.Span(tag, className="fiw-tag") if tag else None],
                 className="fiw-focus-head"),
        html.Div(_counts_line(area), className="fiw-focus-counts"),
        _change_line(area, window, now),
        html.Div([_member_row(m) for m in shown]
                 + ([html.Div(f"+{more} more", className="muted")] if more else []),
                 className="fiw-members"),
    ])


def area_list(snap, flashes):
    """Every area as a clickable card. The card in focus is highlighted in the
    browser (`HIGHLIGHT_JS`), NOT here: making this list depend on the focus
    would close a loop (list -> pin -> focus -> list) that Dash resolves by
    holding updates until the next interval tick — clicks took 5-10 s."""
    areas = snap.get("areas") or []
    flashed = {fire_areas.area_for_member(areas, f["member"]).key
               for f in (flashes or [])
               if fire_areas.area_for_member(areas, f["member"]) is not None}
    cards = [html.Div([
        html.Div([html.Span("Whole state", className="fiw-card-name")],
                 className="fiw-card-head"),
        html.Div(f"{len(areas)} area{'s' if len(areas) != 1 else ''}",
                 className="muted fiw-card-meta"),
    ], id={"type": "fiw-area", "key": STATE_KEY}, n_clicks=0,
        className="fiw-card", **{"data-key": STATE_KEY})]
    for area in areas[:AREAS_LISTED]:
        level = LEVEL_KEYS.get(area.level, "none")
        change = []
        if area.grown_ha:
            change.append(html.Span(f"▲ +{_ha(area.grown_ha)}", className="fiw-grow"))
        if area.reduced_ha:
            change.append(html.Span(f" ▼ −{_ha(area.reduced_ha)}",
                                    className="fiw-reduce"))
        cards.append(html.Div([
            html.Div([html.Span(area.name, className="fiw-card-name"),
                      html.Span("NEW", className="fw-badge")
                      if area.key in flashed else None],
                     className="fiw-card-head"),
            html.Div(_counts_line(area), className="muted fiw-card-meta"),
            html.Div(change, className="fiw-card-change") if change else None,
        ], id={"type": "fiw-area", "key": area.key}, n_clicks=0,
            title="Pin the map on this area",
            className=f"fiw-card fiw-card-{level}", **{"data-key": area.key}))
    if len(areas) > AREAS_LISTED:
        cards.append(html.Div(f"+{len(areas) - AREAS_LISTED} more — they stay in "
                              "the rotation", className="muted fiw-more"))
    return cards


def banner(flash, area):
    if flash is None:
        return None, "fw-banner"
    if flash["kind"] == "growth":
        text, tone = f"⚠ BURN AREA GROWING — {flash['text']}", "growth"
    else:
        text = f"⚠ {flash['level'].upper()} — {area.name}"
        tone = LEVEL_KEYS.get(flash.get("level"), "watch")
    return text, f"fw-banner fw-banner-on fiw-banner-{tone}"


# --------------------------------------------------------------------------- #
# Map
# --------------------------------------------------------------------------- #
def _fc(geoms):
    feats = [{"type": "Feature", "properties": {}, "geometry": g}
             for g in geoms if g]
    return {"type": "FeatureCollection", "features": feats} if feats else None


def _fill(fc, colour, opacity, name=None):
    layer = {"sourcetype": "geojson", "type": "fill", "below": "traces",
             "color": colour, "opacity": opacity, "source": fc}
    if name:
        layer["name"] = name
    return layer


def _line(fc, colour, width, dash=None, name=None, opacity=0.95):
    layer = {"sourcetype": "geojson", "type": "line", "below": "traces",
             "color": colour, "opacity": opacity, "line": {"width": width},
             "source": fc}
    if dash:
        layer["line"]["dash"] = dash
    if name:
        layer["name"] = name
    return layer


def _pair(geoms, colour, opacity, width, name=None):
    fc = _fc(geoms)
    if fc is None:
        return []
    return [_fill(fc, colour, opacity, name), _line(fc, colour, width, name=name)]


def burn_layers(snap, layers, now):
    """Burnt area, growth / reduction over the window, the breathing latest
    step, area outlines and (optionally) historical burn footprints."""
    from app.pages import fire as fire_page
    out = []
    areas = snap.get("areas") or []
    changes = snap.get("changes") or {}
    in_area = {m.id for a in areas for m in a.members if m.role in ("fire", "burn")}

    if "history" in layers:
        burn = snap.get("burn")
        if burn is not None and not burn.empty:
            old = burn[~burn["source_id"].astype(str).isin(in_area)]
            fill = fire_page._fill_layer(old["geometry"].dropna().tolist(),
                                         HISTORIC_COLOUR, 0.25)
            if fill:
                out.append(fill)
    if "burnt" in layers:
        out += _pair([changes[i].geometry for i in sorted(in_area) if i in changes],
                     BURNT_COLOUR, 0.45, 1.5)
    if "change" in layers:
        relevant = [changes[i] for i in sorted(in_area) if i in changes]
        out += _pair([c.grown for c in relevant], GROWTH_COLOUR, 0.4, 1.5)
        out += _pair([c.reduced for c in relevant], REDUCTION_COLOUR, 0.4, 1.5)
        for c in relevant:
            if c.step_at is None:
                continue
            ends = c.step_at + timedelta(seconds=PULSE_SECONDS)
            if now >= ends:
                continue
            name = f"{fire_page.PULSE_PREFIX}{int(ends.timestamp() * 1000)}"
            out += _pair([c.step_grown], GROWTH_COLOUR, 0.5, 2, name)
            out += _pair([c.step_reduced], REDUCTION_COLOUR, 0.5, 2, name)
    return out


def map_figure(snap, focus, layers, dark, now=None):
    """The wall map: VicEmergency incidents/warnings (shared renderer, so the
    warning areas match every other map), burn areas over them, centred on the
    area in focus. `focus` is an OperationArea or None for the whole state."""
    from app.pages import fire as fire_page
    from app.pages import unified
    now = now or datetime.now()
    layers = set(layers if layers is not None else DEFAULT_LAYERS)
    kinds = _fire_kinds(layers)
    df = snap.get("incidents")

    def source(key):
        if key != "fire" or df is None or df.empty:
            return df
        d = df.copy()
        kind = d.apply(fire_page._kind, axis=1)
        d = d[kind.isin(kinds)]
        # A fire's own polygon is drawn below as BURNT AREA, not as a generic
        # incident tint, so the two never stack.
        d.loc[kind.reindex(d.index) == "Fire", "geometry"] = None
        return d

    center, zoom = (focus.center, focus.zoom) if focus is not None else STATE_VIEW
    fig = unified.map_figure(["fire"] if kinds else [], dark, source=source,
                             center=center, zoom=zoom, uirevision="fire-wall-map")
    extra = burn_layers(snap, layers, now)

    areas = snap.get("areas") or []
    if "areas" in layers:
        for area in areas:
            on = focus is not None and area.key == focus.key
            fc = _fc([area.outline])
            if fc:
                extra.append(_line(fc, AREA_OUTLINE, 3 if on else 1.5,
                                   dash=[2, 1.5], opacity=0.95 if on else 0.6))
    current = list(fig.layout.map.layers or [])
    fig.update_layout(map_layers=current + extra)

    if focus is None and areas:
        fig.add_trace(go.Scattermap(
            lat=[a.center["lat"] for a in areas],
            lon=[a.center["lon"] for a in areas],
            mode="markers+text", text=[a.name for a in areas],
            textposition="top center",
            textfont=dict(size=14, color="#ffffff" if dark else "#111111"),
            marker=dict(size=26, color="rgba(0, 200, 255, 0.35)"),
            hoverinfo="skip", showlegend=False))
    # `wdFocus` tells assets/graph_calm.js to move the map when the focus
    # CHANGES (a new area, the takeover, back to the state) and otherwise keep
    # the viewer's own pan/zoom — without it a rotation back to an area seen
    # before would be held at the previous view.
    fig.update_layout(showlegend=False, margin=dict(l=0, r=0, t=0, b=0),
                      meta={"wdFocus": focus.key if focus is not None else STATE_KEY})
    return fig


# --------------------------------------------------------------------------- #
# Callbacks
# --------------------------------------------------------------------------- #
# Marks the card of the area in focus. Deferred a frame so a list that was just
# re-rendered is in the DOM before it is marked.
HIGHLIGHT_JS = """
function(focus, _children) {
    requestAnimationFrame(function() {
        document.querySelectorAll('#fiw-list .fiw-card').forEach(function(el) {
            el.classList.toggle('fiw-card-on',
                                el.getAttribute('data-key') === focus);
        });
    });
    return window.dash_clientside.no_update;
}
"""
def register_callbacks(app):
    @app.callback(
        Output("fiw-tiles", "children"),
        Output("fiw-stale", "children"),
        Output("fiw-stale", "title"),
        Output("fiw-seen", "data"),
        Output("fiw-flash", "data"),
        Input("fiw-interval", "n_intervals"),
        Input("fiw-link-km", "value"),
        Input("fiw-window", "value"),
        State("fiw-seen", "data"),
        State("fiw-flash", "data"),
        State("fiw-test", "data"))
    def refresh(_, link_km, window, seen, flashes, test):
        snap = snapshot(link_km, window, test)
        now = time.time()
        if snap["ok"]:
            seen, fresh = fire_areas.detect_new(seen, fire_areas.events(snap))
        else:
            fresh = []                  # an unreadable snapshot is not "all clear"
        flashes = update_flashes(flashes, fresh, now)
        text = (fire_demo.describe(snap) if snap.get("test")
                else stale_banner(situation.current()))
        return tiles(snap), text, text, seen, flashes

    @app.callback(
        Output("fiw-rotate", "interval"),
        Input("fiw-rotate-secs", "value"))
    def rotate_speed(secs):
        return (secs if secs in ROTATE_CHOICES else DEFAULT_ROTATE) * 1000

    @app.callback(
        Output("fiw-pin", "data"),
        Input({"type": "fiw-area", "key": ALL}, "n_clicks"),
        Input("fiw-resume", "n_clicks"),
        prevent_initial_call=True)
    def pin(_clicks, _resume):
        trigger = ctx.triggered_id
        if trigger == "fiw-resume":
            return None
        # Re-rendering the list re-creates the cards with n_clicks=0, which
        # also lands here: only a real click (a positive count) pins.
        if isinstance(trigger, dict) and any(
                t.get("value") for t in ctx.triggered):
            return trigger.get("key")
        return no_update

    @app.callback(
        Output("fiw-map", "figure"),
        Output("fiw-focus", "children"),
        Output("fiw-focus-key", "data"),
        Output("fiw-pager", "children"),
        Output("fiw-banner", "children"),
        Output("fiw-banner", "className"),
        Output("fiw-resume", "style"),
        Input("fiw-rotate", "n_intervals"),
        Input("fiw-interval", "n_intervals"),
        Input("fiw-flash", "data"),
        Input("fiw-pin", "data"),
        Input("fiw-layers", "value"),
        Input("fiw-statewide", "value"),
        Input("fiw-link-km", "value"),
        Input("fiw-window", "value"),
        Input("theme-store", "data"),
        State("fiw-test", "data"))
    def render(tick, _refresh, flashes, pinned, layers, statewide, link_km,
               window, dark, test):
        snap = snapshot(link_km, window, test)
        areas = snap.get("areas") or []
        now_ts, now = time.time(), datetime.now()
        flash, flash_area = hot_flash(flashes, areas, now_ts)
        keys = [a.key for a in areas]
        focus_key, page, pages, mode = choose_focus(
            keys, tick, bool(statewide and "state" in statewide), pinned,
            flash_area.key if flash_area is not None else None)
        focus = next((a for a in areas if a.key == focus_key), None)
        try:
            fig = map_figure(snap, focus, layers, bool(dark), now)
        except Exception:
            log.exception("Fire wall: map failed")
            fig = no_update
        text, banner_cls = banner(flash, flash_area)

        dots = ([html.Span(className="wall-dot" + (" wall-dot-on" if i == page
                                                   else ""))
                 for i in range(pages)] if pages > 1 and mode == "rotate" else [])
        if mode == "takeover":
            dots.append(html.Span("Showing the latest change", className="wall-next"))
        elif mode == "pinned":
            dots.append(html.Span("Pinned — rotation paused", className="wall-next"))
        elif pages > 1:
            dots.append(html.Span(f"View {page + 1} of {pages}", className="wall-next"))
        resume = {"display": "inline-block" if mode == "pinned" else "none"}
        return (fig, focus_card(focus, snap, mode, now),
                focus_key or STATE_KEY, dots, text, banner_cls, resume)

    @app.callback(
        Output("fiw-list", "children"),
        Input("fiw-interval", "n_intervals"),
        Input("fiw-flash", "data"),
        Input("fiw-link-km", "value"),
        Input("fiw-window", "value"),
        State("fiw-test", "data"))
    def refresh_list(_, flashes, link_km, window, test):
        return area_list(snapshot(link_km, window, test), flashes)

    app.clientside_callback(
        HIGHLIGHT_JS,
        Output("fiw-highlight", "data"),
        Input("fiw-focus-key", "data"),
        Input("fiw-list", "children"))

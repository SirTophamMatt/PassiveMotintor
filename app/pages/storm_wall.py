# Passive Monitor — Copyright (c) 2026 SirTophamMatt. All rights reserved.
"""Storm wall (/wall/storm): a map-first scenario wall for a storm / flood event.

The wall looks only at the weather side of the picture: weather-related SES
jobs (trees down, building damage, flooding …), weather-related VicEmergency
warnings, roads closed by flooding / weather / trees, power outages, flood
gauges at/above Minor and storm cells.

Unlike the fire wall, incidents are LINKED: SES jobs within the link distance
of each other form one AREA OF OPERATION (`app.storm_areas`), rebuilt on every
refresh, so an area grows, merges and splits with the event. Each area gets a
card: where it is (its busiest localities), how many jobs and of what kind,
how many are new, the warnings over it, and the roads, power, gauges and storm
cells around it. The map rotates its focus from area to area (optionally with a
statewide view), the area in focus is a card over the map, and every area is
listed beside it — click one to pin the map there.

An area gaining a Watch and Act / Emergency Warning, or its job count reaching
5 / 10 / 20 / 50, takes the map for `TAKEOVER_SECONDS` under a pulsing banner.
"New" is decided per BROWSER (the first snapshot only seeds), like every wall.

Same rules as every wall: public, read-only, nothing fetched from a source.
The areas are computed once per `storm_areas.CACHE_SECONDS` for every viewer
with the same options.
"""
import logging
import time

import plotly.graph_objects as go
from dash import ALL, Input, Output, State, ctx, dcc, html, no_update

from app import auth, basemap, shell, situation, storm_areas, storm_demo
from app.modules.fire import data as fire_data
from app.pages import fire_wall, flood_wall, wall

log = logging.getLogger(__name__)

PATH = shell.STORM_WALL_PATH
TEST_PATH = shell.STORM_WALL_TEST_PATH

REFRESH_SECONDS = 30
ROTATE_CHOICES = fire_wall.ROTATE_CHOICES
DEFAULT_ROTATE = 20
STATE_KEY = fire_wall.STATE_KEY
STATE_VIEW = fire_wall.STATE_VIEW
TAKEOVER_SECONDS = 45
FLASH_SECONDS = 15 * 60
AREAS_LISTED = 15
MEMBERS_LISTED = 8
PLACES_SHOWN = 4

AREA_OUTLINE = "#00c8ff"
LEVEL_KEYS = fire_wall.LEVEL_KEYS

# Map layer chips: (value, label, swatch colour, swatch shape).
LAYERS = [
    ("emergency", "Emergency Warning", "#d62728", "▲"),
    ("watch_act", "Watch and Act", "#ff7f0e", "▲"),
    ("advice", "Advice", "#e6c700", "▲"),
    ("incidents", "Incidents", "#5b8def", "●"),
    ("closures", "Road closures", "#d62728", "━"),
    ("road_other", "Other road disruptions", "#ff9f1c", "━"),
    ("power", "Power outages", "#7048e8", "●"),
    ("gauges", "Gauges ≥ Minor", "#e6c700", "●"),
    ("cells", "Storm cells", "#d62728", "◉"),
    ("areas", "Areas of operation", AREA_OUTLINE, "┅"),
]
DEFAULT_LAYERS = [key for key, *_ in LAYERS if key != "road_other"]
LEVEL_LAYERS = {"emergency": "Emergency Warning", "watch_act": "Watch and Act",
                "advice": "Advice"}

ROLE_STYLE = {           # icon, colour
    "incident": ("●", "#5b8def"),
    "road": ("━", "#d62728"),
    "power": ("⚡", "#7048e8"),
    "gauge": ("●", "#e6c700"),
    "cell": ("◉", "#d62728"),
}


# --------------------------------------------------------------------------- #
# Model helpers (UI-free, testable without Dash)
# --------------------------------------------------------------------------- #
update_flashes = fire_wall.update_flashes
choose_focus = fire_wall.choose_focus


def _event_rank(flash):
    if flash.get("level") == "Emergency Warning":
        return 0
    if flash.get("level") == "Watch and Act":
        return 1
    return 2


def hot_flash(flashes, areas, now):
    """The takeover in force, if any: (flash, area). Most severe, then newest;
    a flash whose member is no longer in any area is skipped."""
    hot = [f for f in (flashes or []) if now - f["at"] < TAKEOVER_SECONDS]
    hot.sort(key=lambda f: (_event_rank(f), -f["at"]))
    for flash in hot:
        area = storm_areas.area_for_member(areas, flash["member"])
        if area is not None:
            return flash, area
    return None, None


def snapshot(link_km, agencies, weather_only, event_id, test=False):
    """The live snapshot, or — for an admin on the test page — the simulated
    one (`app.storm_demo`). Re-checked here, not just at routing: every Dash
    callback can be POSTed directly, so a flag from the browser proves
    nothing on its own."""
    weather_only = _weather_only(weather_only)
    if test and auth.is_admin():
        return storm_demo.snapshot(link_km, agencies, weather_only)
    return storm_areas.current(link_km, agencies, weather_only,
                               flood_wall.resolve_tag(event_id))


def _weather_only(value):
    return True if value is None else "weather" in (value or [])


def incident_label(agencies):
    agencies = storm_areas.DEFAULT_AGENCIES if agencies is None else agencies
    if len(agencies) == 1 and agencies[0] != fire_data.AGENCY_OTHER:
        return f"{fire_data.AGENCY_LABELS[agencies[0]]} incidents"
    return "Incidents"


def scope_note(snap):
    """One line saying what the wall is counting."""
    agencies = snap.get("agencies") or []
    if set(agencies) >= set(storm_areas.AGENCY_KEYS):
        who = "all agencies"
    elif not agencies:
        who = "no agencies selected"
    else:
        who = ", ".join(fire_data.AGENCY_LABELS[a] for a in storm_areas.AGENCY_KEYS
                        if a in agencies)
    text = (f"Incidents: {who}"
            + (" · weather-related" if snap.get("weather_only") else " · all types")
            + f" · linked within {snap.get('link_km')} km"
            + " · roads closed by flooding, weather or trees")
    tag = snap.get("tag")
    if tag:
        text += f" · started since {tag['name']} ({str(tag['start_ts'])[:16]})"
    return text


def counts(snap):
    """Figures for the tiles, each None when its source could not be read."""
    frames = snap.get("frames") or {}
    ok = snap.get("ok")

    def frame(key):
        df = frames.get(key)
        return None if (df is None or not ok) else df

    inc, roads, power = frame("incidents"), frame("roads"), frame("power")
    gauges, cells = frame("gauges"), frame("cells")
    import pandas as pd
    return {
        "incidents": None if inc is None else len(storm_areas.selected_incidents(
            inc, snap.get("agencies"), snap.get("weather_only", True))),
        "areas": len(snap.get("areas") or []) if ok else None,
        **(storm_areas.warning_counts(snap) if ok else
           {"Emergency Warning": None, "Watch and Act": None, "Advice": None}),
        "closures": None if roads is None else (
            0 if roads.empty else int(pd.to_numeric(roads["is_closure"],
                                                    errors="coerce").fillna(0).sum())),
        "customers": None if power is None else (
            0 if power.empty else int(pd.to_numeric(power["customers_off"],
                                                    errors="coerce").fillna(0).sum())),
        "gauges": None if gauges is None else len(gauges),
        "strong": None if cells is None else (
            0 if cells.empty else int((cells["classification"] == "strong").sum())),
    }


# --------------------------------------------------------------------------- #
# Components
# --------------------------------------------------------------------------- #
_layer_option = fire_wall._layer_option
_radio = fire_wall._radio


def test_locked():
    return html.Div([
        html.H2("Storm wall test run"),
        html.P("The test run shows a simulated storm with fake warnings, so it "
               "is for signed-in admins only — sign in on the Admin page, then "
               "come back to this address."),
        html.A("Go to Admin →", href="/admin", className="btn btn-primary"),
    ], className="panel", style={"maxWidth": "560px", "margin": "40px auto"})


def layout(test=False):
    options = html.Details([
        html.Summary("⚙ Options", className="btn"),
        html.Div([
            html.Div("Next area every", className="display-panel-label"),
            _radio("stw-rotate-secs", ROTATE_CHOICES, DEFAULT_ROTATE,
                   lambda n: f" {n} s"),
            dcc.Checklist(id="stw-statewide",
                          options=[{"label": " Include the whole state in the "
                                             "rotation", "value": "state"}],
                          value=["state"], className="display-options",
                          persistence=True, persistence_type="local"),
            html.Div("Link incidents within", className="display-panel-label"),
            _radio("stw-link-km", storm_areas.LINK_KM_CHOICES,
                   storm_areas.DEFAULT_LINK_KM, lambda n: f" {n} km"),
            html.Div("Incident agency", className="display-panel-label"),
            dcc.Checklist(id="stw-agencies",
                          options=[{"label": f" {fire_data.AGENCY_LABELS[k]}",
                                    "value": k} for k in storm_areas.AGENCY_KEYS],
                          value=storm_areas.DEFAULT_AGENCIES, inline=True,
                          className="display-options",
                          persistence=True, persistence_type="local"),
            dcc.Checklist(id="stw-weather",
                          options=[{"label": " Weather-related incidents only "
                                             "(trees, damage, flooding …)",
                                    "value": "weather"}],
                          value=["weather"], className="display-options",
                          persistence=True, persistence_type="local"),
            html.Div("Event", className="display-panel-label"),
            dcc.Dropdown(id="stw-event", options=flood_wall.event_options(),
                         placeholder="Everything active (no event)",
                         clearable=True, persistence=True,
                         persistence_type="local", className="fw-event-dd"),
            html.Div("With an event chosen, only incidents and road disruptions "
                     "that started during it are counted. Warnings, power, "
                     "gauges and storm cells are always what is current.",
                     className="display-panel-note"),
            *wall.map_style_option("stw-map-style"),
            html.Div("Remembered by this browser only.",
                     className="display-panel-note"),
        ], className="fw-options-panel"),
    ], className="fw-options")

    return html.Div([
        dcc.Interval(id="stw-interval", interval=REFRESH_SECONDS * 1000,
                     n_intervals=0),
        dcc.Interval(id="stw-rotate", interval=DEFAULT_ROTATE * 1000,
                     n_intervals=0),
        dcc.Interval(id="wall-clock-tick", interval=1_000, n_intervals=0),
        dcc.Store(id="stw-seen", storage_type="memory"),
        dcc.Store(id="stw-flash", storage_type="memory"),
        dcc.Store(id="stw-pin", storage_type="memory"),
        dcc.Store(id="stw-test", data=bool(test)),
        dcc.Store(id="stw-focus-key", storage_type="memory"),
        dcc.Store(id="stw-highlight", storage_type="memory"),
        wall.header(PATH, stale_id="stw-stale",
                    extra=([html.Span("TEST RUN", className="fiw-test-badge"),
                            dcc.Link("Leave test", href=PATH, className="btn")]
                           if test else []) + [options]),
        html.Div(id="stw-tiles", className="wall-tiles fiw-tiles stw-tiles"),
        html.Div(id="stw-banner", className="fw-banner"),
        html.Div([
            html.Div([
                dcc.Graph(id="stw-map", className="wall-map-graph",
                          config={"displayModeBar": False, "scrollZoom": True},
                          style={"height": "100%"}),
                dcc.Checklist(id="stw-layers",
                              options=[_layer_option(*row) for row in LAYERS],
                              value=DEFAULT_LAYERS, inline=True,
                              className="fw-layer-chips",
                              persistence=True, persistence_type="local"),
                html.Div(id="stw-focus", className="fiw-focus"),
                html.Div(id="stw-pager", className="wall-dots fiw-pager"),
                html.Div(id="stw-note", className="fw-road-note"),
            ], className="wall-map fiw-map"),
            html.Div([
                html.Div([
                    html.Span("Areas of operation", className="wall-panel-title"),
                    html.Button("▶ Resume rotation", id="stw-resume",
                                className="btn fiw-resume",
                                style={"display": "none"}),
                ], className="fiw-side-head"),
                html.Div(id="stw-list", className="fiw-list"),
            ], className="wall-panel fiw-side"),
        ], className="fiw-body"),
    ], className="wall-page fiw-page stw-page")


def _chip(key, label, value, tone):
    return situation.Chip(key, label, value, PATH, tone)


def tiles(snap, bom=None):
    c = counts(snap)
    chips = [
        _chip("incidents", incident_label(snap.get("agencies")), c["incidents"],
              "alert"),
        _chip("areas", "Areas of operation", c["areas"], None),
        _chip("emergency", "Emergency Warning", c["Emergency Warning"], "emergency"),
        _chip("watch_act", "Watch and Act", c["Watch and Act"], "watch"),
        _chip("advice", "Advice", c["Advice"], "advice"),
        _chip("bom", "BoM warnings", bom, None),
        _chip("closures", "Road closures", c["closures"], None),
        _chip("customers", "Customers off", c["customers"], None),
        _chip("gauges", "Gauges ≥ Minor", c["gauges"], "watch"),
        _chip("strong", "Strong storm cells", c["strong"], "alert"),
    ]
    out = [wall.tile(chip) for chip in chips]
    note = scope_note(snap)
    out[0].title = out[1].title = out[6].title = note
    out[2].title = out[3].title = out[4].title = (
        "Weather-related warnings only (storm, wind, flood …) — fire warnings "
        "are on the fire wall")
    return out


STORM_SOURCES = ("VicEmergency", "BoM Weather", "Roads", "Power", "Flood", "Storm")


def stale_banner(sit):
    relevant = situation.Situation(
        sit.generated_at,
        sources=[s for s in sit.sources if s.name in STORM_SOURCES])
    return wall.stale_banner(relevant)


def _plural(n, word):
    return f"{n} {word}{'' if n == 1 else 's'}"


def _level_badge(level):
    if not level:
        return None
    return html.Span(level, className=f"fiw-level fiw-{LEVEL_KEYS[level]}")


def _places(area):
    shown = area.places[:PLACES_SHOWN]
    more = len(area.places) - len(shown)
    return ", ".join(shown) + (f" +{more} more" if more > 0 else "")


def counts_line(area):
    """'8 incidents · 3 new in the last hour · 2 warnings'."""
    n = len(area.of_role("incident"))
    parts = [_plural(n, "incident")] if n else []
    if area.new_count:
        parts.append(f"{area.new_count} new in the last "
                     f"{storm_areas.NEW_MINUTES} min")
    if area.warnings:
        parts.append(_plural(len(area.warnings), "warning"))
    return " · ".join(parts) or "No incidents"


def kinds_line(area, limit=4):
    kinds = area.kind_counts()
    text = " · ".join(f"{k} ×{n}" if n > 1 else k for k, n in kinds[:limit])
    if len(kinds) > limit:
        text += f" · +{len(kinds) - limit} more"
    return text


def impacts_line(area):
    """Roads, power, gauges and storm cells around the area — only what is there."""
    bits = []
    if area.closures:
        bits.append(_plural(len(area.closures), "road closure"))
    others = len(area.of_role("road")) - len(area.closures)
    if others:
        bits.append(_plural(others, "road disruption"))
    if area.customers_off:
        bits.append(f"⚡ {area.customers_off:,} customers off")
    gauge = area.worst_gauge
    if gauge is not None:
        bits.append(f"gauge at {gauge.kind}")
    if area.strong_cells:
        bits.append(_plural(len(area.strong_cells), "strong storm cell"))
    elif area.of_role("cell"):
        bits.append(_plural(len(area.of_role("cell")), "storm cell"))
    return " · ".join(bits)


def _member_row(m):
    if m.role == "warning":
        icon, colour = "▲", {"Emergency Warning": "#d62728",
                             "Watch and Act": "#ff7f0e"}.get(m.level, "#e6c700")
        detail = " · ".join(b for b in (m.level, m.detail) if b)
    else:
        icon, colour = ROLE_STYLE.get(m.role, ("●", "#5b8def"))
        if m.role == "incident":
            detail = m.kind + (f" · {m.started:%H:%M}" if m.started else "")
        elif m.role == "power":
            detail = f"{int(m.value):,} off"
        elif m.role == "gauge":
            detail = f"{m.kind} · {m.value:.2f} m"
            colour = {"Major": "#d62728", "Moderate": "#ff7f0e"}.get(m.kind, colour)
        elif m.role == "road":
            detail = m.kind
            colour = "#d62728" if m.rank == 1 else "#ff9f1c"
        else:
            detail = m.kind
    return html.Div([
        html.Span(icon, style={"color": colour}, className="fiw-member-icon"),
        html.Span(m.location, className="fiw-member-name"),
        html.Span(detail, className="muted fiw-member-detail"),
    ], className="fiw-member")


def focus_card(area, snap, mode):
    """The card over the map for the area in focus (or the state view)."""
    if area is None:
        areas = snap.get("areas") or []
        if not snap.get("ok"):
            text = "Areas of operation unavailable — " + (snap.get("error") or
                                                         "see the server log")
        elif not areas:
            text = ("No weather-related incidents, or Watch and Act / Emergency "
                    "Warnings, active.")
        else:
            text = (f"{_plural(len(areas), 'area')} of operation across the state"
                    f" · {_plural(sum(len(a.of_role('incident')) for a in areas), 'incident')}")
        return html.Div([
            html.Div([html.Span("Whole state", className="fiw-focus-name"),
                      html.Span("PINNED", className="fiw-tag")
                      if mode == "pinned" else None],
                     className="fiw-focus-head"),
            html.Div(text, className="muted"),
        ])
    members = (area.warnings + area.incidents
               + sorted(area.of_role("road"), key=lambda m: m.rank)
               + sorted(area.of_role("power"), key=lambda m: -m.value)
               + sorted(area.of_role("gauge"), key=lambda m: m.rank)
               + sorted(area.of_role("cell"), key=lambda m: m.rank))
    shown = members[:MEMBERS_LISTED]
    more = len(members) - len(shown)
    tag = {"takeover": "LATEST CHANGE", "pinned": "PINNED"}.get(mode)
    impacts = impacts_line(area)
    return html.Div([
        html.Div([html.Span(area.name, className="fiw-focus-name"),
                  _level_badge(area.level),
                  html.Span(tag, className="fiw-tag") if tag else None],
                 className="fiw-focus-head"),
        html.Div(_places(area), className="muted stw-places") if len(area.places) > 1
        else None,
        html.Div(counts_line(area), className="fiw-focus-counts"),
        html.Div(kinds_line(area), className="stw-kinds") if area.kind_counts()
        else None,
        html.Div(impacts, className="stw-impacts") if impacts else None,
        html.Div([_member_row(m) for m in shown]
                 + ([html.Div(f"+{more} more", className="muted")] if more else []),
                 className="fiw-members"),
    ])


def area_list(snap, flashes):
    """Every area as a clickable card. The card in focus is highlighted in the
    browser (`HIGHLIGHT_JS`) — see fire_wall.area_list for why."""
    areas = snap.get("areas") or []
    flashed = set()
    for f in flashes or []:
        area = storm_areas.area_for_member(areas, f["member"])
        if area is not None:
            flashed.add(area.key)
    total = sum(len(a.of_role("incident")) for a in areas)
    cards = [html.Div([
        html.Div([html.Span("Whole state", className="fiw-card-name")],
                 className="fiw-card-head"),
        html.Div(f"{_plural(len(areas), 'area')} · {_plural(total, 'incident')}",
                 className="muted fiw-card-meta"),
    ], id={"type": "stw-area", "key": STATE_KEY}, n_clicks=0,
        className="fiw-card", **{"data-key": STATE_KEY})]
    for area in areas[:AREAS_LISTED]:
        level = LEVEL_KEYS.get(area.level, "none")
        impacts = impacts_line(area)
        cards.append(html.Div([
            html.Div([html.Span(area.name, className="fiw-card-name"),
                      html.Span("NEW", className="fw-badge")
                      if area.key in flashed else None],
                     className="fiw-card-head"),
            html.Div(_places(area), className="muted fiw-card-meta")
            if len(area.places) > 1 else None,
            html.Div(counts_line(area), className="muted fiw-card-meta"),
            html.Div(kinds_line(area, limit=2), className="fiw-card-meta")
            if area.kind_counts() else None,
            html.Div(impacts, className="muted fiw-card-meta") if impacts else None,
        ], id={"type": "stw-area", "key": area.key}, n_clicks=0,
            title="Pin the map on this area",
            className=f"fiw-card fiw-card-{level}", **{"data-key": area.key}))
    if len(areas) > AREAS_LISTED:
        cards.append(html.Div(f"+{len(areas) - AREAS_LISTED} more — they stay in "
                              "the rotation", className="muted fiw-more"))
    return cards


def banner(flash, area):
    if flash is None:
        return None, "fw-banner"
    if flash["kind"] == "size":
        return (f"⚠ AREA GROWING — {area.name} — {flash['count']} incidents",
                "fw-banner fw-banner-on fiw-banner-watch")
    tone = LEVEL_KEYS.get(flash.get("level"), "watch")
    return (f"⚠ {flash['level'].upper()} — {area.name}",
            f"fw-banner fw-banner-on fiw-banner-{tone}")


# --------------------------------------------------------------------------- #
# Map
# --------------------------------------------------------------------------- #
def map_sources(snap, layers):
    """`source(key)` for unified.map_figure: the snapshot's own (scoped) frames
    narrowed to the ticked layers, so the map shows what the tiles count."""
    from app.pages import fire as fire_page
    import pandas as pd
    frames = snap.get("frames") or {}
    levels = {LEVEL_LAYERS[k] for k in layers if k in LEVEL_LAYERS}

    def source(key):
        if key == "fire":
            df = frames.get("incidents")
            if df is None or df.empty:
                return df
            rows = [r for r in storm_areas.weather_warnings(df)
                    if fire_page._kind(r) in levels]
            if "incidents" in layers:
                rows += storm_areas.selected_incidents(
                    df, snap.get("agencies"), snap.get("weather_only", True))
            return pd.DataFrame(rows, columns=df.columns)
        if key == "roads":
            df = frames.get("roads")
            if df is None or df.empty:
                return df
            closure = pd.to_numeric(df["is_closure"], errors="coerce").fillna(0) > 0
            keep = ((closure & ("closures" in layers))
                    | (~closure & ("road_other" in layers)))
            return df[keep]
        wanted = {"storm": "cells", "power": "power", "flood": "gauges"}
        return frames.get(wanted.get(key)) if wanted.get(key) in layers else None
    return source


def map_figure(snap, focus, layers, dark, style=basemap.LIGHT_STYLE):
    """The wall map, centred on the area in focus (None = the whole state)."""
    from app.pages import unified
    layers = set(layers if layers is not None else DEFAULT_LAYERS)
    center, zoom = (focus.center, focus.zoom) if focus is not None else STATE_VIEW
    fig = unified.map_figure(["fire", "flood", "roads", "storm", "power"], dark,
                             source=map_sources(snap, layers), center=center,
                             zoom=zoom, uirevision="storm-wall-map",
                             basemap=style)
    areas = snap.get("areas") or []
    extra = []
    if "areas" in layers:
        for area in areas:
            on = focus is not None and area.key == focus.key
            fc = fire_wall._fc([area.outline])
            if fc:
                extra.append(fire_wall._line(fc, AREA_OUTLINE, 3 if on else 1.5,
                                             dash=[2, 1.5],
                                             opacity=0.95 if on else 0.6))
    fig.update_layout(map_layers=list(fig.layout.map.layers or []) + extra)
    if focus is None and areas:
        fig.add_trace(go.Scattermap(
            lat=[a.center["lat"] for a in areas],
            lon=[a.center["lon"] for a in areas],
            mode="markers+text",
            text=[f"{a.name} ({len(a.of_role('incident'))})"
                  if a.of_role("incident") else a.name for a in areas],
            textposition="top center",
            textfont=dict(size=14, color=wall.label_colour(style)),
            marker=dict(size=26, color="rgba(0, 200, 255, 0.35)"),
            hoverinfo="skip", showlegend=False))
    # `wdFocus` (assets/graph_calm.js): move the map when the focus changes,
    # otherwise keep the viewer's own pan/zoom.
    fig.update_layout(showlegend=False, margin=dict(l=0, r=0, t=0, b=0),
                      meta={"wdFocus": focus.key if focus is not None else STATE_KEY})
    return fig


# --------------------------------------------------------------------------- #
# Callbacks
# --------------------------------------------------------------------------- #
HIGHLIGHT_JS = fire_wall.HIGHLIGHT_JS.replace("#fiw-list", "#stw-list")

OPTION_INPUTS = ("stw-link-km", "stw-agencies", "stw-weather", "stw-event")


def register_callbacks(app):
    options = [Input(i, "value") for i in OPTION_INPUTS]

    @app.callback(
        Output("stw-tiles", "children"),
        Output("stw-stale", "children"),
        Output("stw-stale", "title"),
        Output("stw-note", "children"),
        Output("stw-seen", "data"),
        Output("stw-flash", "data"),
        Input("stw-interval", "n_intervals"),
        *options,
        State("stw-seen", "data"),
        State("stw-flash", "data"),
        State("stw-test", "data"))
    def refresh(_, link_km, agencies, weather, event_id, seen, flashes, test):
        snap = snapshot(link_km, agencies, weather, event_id, test)
        now = time.time()
        if snap["ok"]:
            seen, fresh = storm_areas.detect_new(seen, storm_areas.events(snap))
        else:
            fresh = []                  # an unreadable snapshot is not "all clear"
        flashes = update_flashes(flashes, fresh, now)
        if snap.get("test"):
            text, bom = storm_demo.describe(snap), None
        else:
            sit = situation.current()
            text = stale_banner(sit)
            chip = sit.chip("bom")
            bom = chip.value if chip else None
        return tiles(snap, bom), text, text, scope_note(snap), seen, flashes

    @app.callback(
        Output("stw-rotate", "interval"),
        Input("stw-rotate-secs", "value"))
    def rotate_speed(secs):
        return (secs if secs in ROTATE_CHOICES else DEFAULT_ROTATE) * 1000

    @app.callback(
        Output("stw-pin", "data"),
        Input({"type": "stw-area", "key": ALL}, "n_clicks"),
        Input("stw-resume", "n_clicks"),
        prevent_initial_call=True)
    def pin(_clicks, _resume):
        trigger = ctx.triggered_id
        if trigger == "stw-resume":
            return None
        # A re-rendered list re-creates the cards with n_clicks=0, which also
        # lands here: only a real click (a positive count) pins.
        if isinstance(trigger, dict) and any(t.get("value") for t in ctx.triggered):
            return trigger.get("key")
        return no_update

    @app.callback(
        Output("stw-map", "figure"),
        Output("stw-focus", "children"),
        Output("stw-focus-key", "data"),
        Output("stw-pager", "children"),
        Output("stw-banner", "children"),
        Output("stw-banner", "className"),
        Output("stw-resume", "style"),
        Input("stw-rotate", "n_intervals"),
        Input("stw-interval", "n_intervals"),
        Input("stw-flash", "data"),
        Input("stw-pin", "data"),
        Input("stw-layers", "value"),
        Input("stw-statewide", "value"),
        *options,
        Input("stw-map-style", "value"),
        Input("theme-store", "data"),
        State("stw-test", "data"))
    def render(tick, _refresh, flashes, pinned, layers, statewide, link_km,
               agencies, weather, event_id, map_mode, dark, test):
        snap = snapshot(link_km, agencies, weather, event_id, test)
        areas = snap.get("areas") or []
        flash, flash_area = hot_flash(flashes, areas, time.time())
        focus_key, page, pages, mode = choose_focus(
            [a.key for a in areas], tick, bool(statewide and "state" in statewide),
            pinned, flash_area.key if flash_area is not None else None)
        focus = next((a for a in areas if a.key == focus_key), None)
        try:
            fig = map_figure(snap, focus, layers, bool(dark),
                             style=basemap.style(map_mode, dark))
        except Exception:
            log.exception("Storm wall: map failed")
            fig = no_update
        text, banner_cls = banner(flash, flash_area)
        dots = ([html.Span(className="wall-dot" + (" wall-dot-on" if i == page
                                                   else ""))
                 for i in range(pages)] if 1 < pages <= 20 and mode == "rotate"
                else [])
        if mode == "takeover":
            dots.append(html.Span("Showing the latest change", className="wall-next"))
        elif mode == "pinned":
            dots.append(html.Span("Pinned — rotation paused", className="wall-next"))
        elif pages > 1:
            dots.append(html.Span(f"View {page + 1} of {pages}", className="wall-next"))
        resume = {"display": "inline-block" if mode == "pinned" else "none"}
        return (fig, focus_card(focus, snap, mode), focus_key or STATE_KEY, dots,
                text, banner_cls, resume)

    @app.callback(
        Output("stw-list", "children"),
        Input("stw-interval", "n_intervals"),
        Input("stw-flash", "data"),
        *options,
        State("stw-test", "data"))
    def refresh_list(_, flashes, link_km, agencies, weather, event_id, test):
        return area_list(snapshot(link_km, agencies, weather, event_id, test),
                         flashes)

    @app.callback(
        Output("stw-event", "options"),
        Input("stw-interval", "n_intervals"))
    def events_list(_):
        return flood_wall.event_options()

    app.clientside_callback(
        HIGHLIGHT_JS,
        Output("stw-highlight", "data"),
        Input("stw-focus-key", "data"),
        Input("stw-list", "children"))

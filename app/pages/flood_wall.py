# Passive Monitor — Copyright (c) 2026 SirTophamMatt. All rights reserved.
"""Flood wall (/wall/flood): a scenario wall for a flood event.

Rotates through every gauge at or above Minor — one gauge per page (with the
flood-gauge stick and the Local Flood Guide impacts it has reached) or four —
beside a statewide map whose layers (gauges, road disruptions, each warning
level, incidents) are toggled from chips on the map itself. A gauge that newly
reaches Minor, or moves UP a class, takes the screen straight away with a
flashing banner, then carries a NEW / UP badge for `FLASH_SECONDS`.

Same rules as the general wall (`wall.py`): public, read-only, nothing fetched
from a source. The gauge list is computed once per `CACHE_SECONDS` for every
viewer, because it groups the whole of `flood_observations` (~1.4M rows in
production — see the 2026-08-09 postmortem).

"New" is decided per BROWSER, like the alert sounds: the server lists what is
flooding, each open wall keeps its own seen-set in a memory store, and the first
snapshot after a load only seeds it — opening the wall mid-event never flashes
the backlog. A gauge that leaves the list is remembered at its peak class for
`QUIET_SECONDS`, so one hovering around a threshold flashes once, not every
time it crosses back.

Options (gauges per page, rotation speed, map on/off, map layers) are
`persistence="local"`, so a wall screen keeps its set-up across reloads.
"""
import logging
import threading
import time
from datetime import datetime, timedelta

import pandas as pd
import plotly.graph_objects as go
from dash import Input, Output, State, dcc, html

from app import database, shell, situation
from app.modules.fire import data as fire_data
from app.modules.flood import data as flood_data
from app.modules.roads import data as roads_data
from app.pages import wall

log = logging.getLogger(__name__)

PATH = shell.FLOOD_WALL_PATH

CACHE_SECONDS = 45
REFRESH_SECONDS = 30
# A gauge whose newest reading is older than this is not shown as flooding:
# a gauge that fell silent at Minor last week is not at Minor now.
STALE_HOURS = 24
HISTORY_DAYS = 2

PER_PAGE_CHOICES = [1, 4]
DEFAULT_PER_PAGE = 4
ROTATE_CHOICES = [10, 15, 20, 30, 60]
DEFAULT_ROTATE = 15

# A new/upgraded gauge takes over the screen for this long, then rotation
# resumes; its badge stays for FLASH_SECONDS.
TAKEOVER_SECONDS = 60
FLASH_SECONDS = 15 * 60
QUIET_SECONDS = 30 * 60
NEAR_MINOR_SHOWN = 6

CLASS_KEYS = {1: "major", 2: "moderate", 3: "minor"}
CLASS_NAMES = {1: "Major", 2: "Moderate", 3: "Minor"}

# Map layer chips: (value, label, swatch colour, swatch shape).
LAYERS = [
    ("gauges", "Flood gauges ≥ Minor", "#ff7f0e", "●"),
    ("gauges_below", "Gauges below Minor", "#5b8def", "●"),
    ("road_closures", "Road closures", "#d62728", "━"),
    ("road_other", "Other road disruptions", "#ff7f0e", "━"),
    ("emergency", "Emergency Warning", "#d62728", "▲"),
    ("watch_act", "Watch and Act", "#ff7f0e", "▲"),
    ("advice", "Advice", "#e6c700", "▲"),
    ("incidents", "Incidents", "#ff5722", "●"),
]
# Below-Minor gauges are ~360 small dots, and lane/partial road disruptions
# are mostly routine: context on demand, not by default.
DEFAULT_LAYERS = [key for key, *_ in LAYERS
                  if key not in ("gauges_below", "road_other")]

# Road causes shown by default: what a flood event closes roads for.
DEFAULT_ROAD_CAUSES = ["flooding", "weather", "trees"]

# Incident agencies (VicEmergency `sourceOrg`, `fire.data.agency_of`); all by
# default. Tick only SES for the flood-and-storm workload.
AGENCY_KEYS = ["ses", "cfa", "frv", "ffm", "other"]
DEFAULT_AGENCIES = list(AGENCY_KEYS)


def _fire_kinds(layers):
    """VicEmergency kinds (`fire._kind`) a set of layer chips switches on."""
    from app.pages import fire as fire_page
    groups = {
        "emergency": {"Emergency Warning"},
        "watch_act": {"Watch and Act"},
        "advice": {"Advice"},
        "incidents": {"Fire", "Other incident", fire_page.MET_KIND},
    }
    kinds = set()
    for layer in layers or []:
        kinds |= groups.get(layer, set())
    return kinds


# --------------------------------------------------------------------------- #
# Model (UI-free, testable without Dash)
# --------------------------------------------------------------------------- #
def _num(value):
    value = pd.to_numeric(value, errors="coerce")
    return None if pd.isna(value) else float(value)


def classify_gauges(latest, levels, now, stale_hours=STALE_HOURS):
    """Latest reading per gauge -> (flooding, near_minor, map_df).

    `latest` has station_name, height_m, ts, latitude, longitude (catchment
    optional). `flooding` is a list of gauge dicts at/above Minor, most severe
    first; `near_minor` the fresh gauges closest BELOW Minor (by metres, not
    ratio — a ratio means nothing on a datum gauge reading 180 m); `map_df`
    every fresh located gauge with the columns `unified.render_flood` needs.
    """
    cutoff = now - timedelta(hours=stale_hours)
    flooding, near, rows = [], [], []
    for _, r in latest.iterrows():
        name = str(r["station_name"]).strip()
        key = name.lower()
        height = _num(r.get("height_m"))
        observed = pd.to_datetime(r.get("ts"), errors="coerce")
        if height is None or pd.isna(observed) or observed < cutoff:
            continue
        lv = levels.get(key)
        priority, label, colour = flood_data.classify_station(height, lv)
        lv_clean = {k: _num(lv.get(k)) if lv else None
                    for k in ("minor", "moderate", "major")}
        lat, lon = _num(r.get("latitude")), _num(r.get("longitude"))
        if lat is not None and lon is not None:
            rows.append({"station_name": name, "key": key, "height_m": height,
                         "latitude": lat, "longitude": lon, "label": label})
        gauge = {"key": key, "station": name, "height": height,
                 "observed": observed.to_pydatetime(), "priority": priority,
                 "label": label, "colour": colour, "levels": lv_clean,
                 "catchment": r.get("catchment") if isinstance(
                     r.get("catchment"), str) else None,
                 "latitude": lat, "longitude": lon}
        if priority < 4:
            flooding.append(gauge)
        elif lv_clean["minor"] is not None:
            gauge["below_minor_m"] = lv_clean["minor"] - height
            near.append(gauge)
    flooding.sort(key=lambda g: (g["priority"], g["station"].lower()))
    near.sort(key=lambda g: g["below_minor_m"])
    return flooding, near[:NEAR_MINOR_SHOWN], pd.DataFrame(rows)


def _attach_trends(flooding):
    """Rate of rise per flooding gauge (`flood.trend`), and re-sort so within a
    class the fastest-rising gauge comes first. One gauge failing costs only
    its own trend."""
    from app.config import load_config
    from app.modules.flood import trend
    cfg = load_config()
    for g in flooding:
        try:
            g["analysis"] = trend.analyse(g["key"], levels=g["levels"], cfg=cfg)
        except Exception:
            log.exception("Flood wall: trend failed for %s", g["station"])
            g["analysis"] = None
        rate = (g["analysis"] or {}).get("rate_m_hr")
        g["rate"] = rate
        g["rising"] = rate is not None and rate >= 0.02
    flooding.sort(key=lambda g: (g["priority"], -(g["rate"] or 0.0),
                                 g["station"].lower()))


def compute(now=None):
    now = now or datetime.now()
    snap = {"at": now, "flooding": [], "near": [], "map": pd.DataFrame(),
            "ok": True}
    try:
        latest = flood_data.latest_readings()
        coords = database.read_df(
            "SELECT station_key, latitude, longitude FROM gauge_coords")
        latest["station_key"] = (latest["station_name"].astype(str)
                                 .str.strip().str.lower())
        latest = latest.merge(coords, on="station_key", how="left") \
            .drop(columns="station_key")
        if not latest.empty:
            flooding, near, map_df = classify_gauges(
                latest, flood_data.load_flood_levels(), now)
            _attach_trends(flooding)
            snap.update(flooding=flooding, near=near, map=map_df)
    except Exception:
        log.exception("Flood wall: gauge list unavailable")
        snap["ok"] = False
    return snap


_lock = threading.Lock()
_cache = {"at": 0.0, "value": None}


def current(max_age=CACHE_SECONDS):
    """Cached snapshot shared by every open flood wall (see module docstring)."""
    with _lock:
        if (_cache["value"] is None
                or time.monotonic() - _cache["at"] >= max_age):
            _cache["value"] = compute()
            _cache["at"] = time.monotonic()
        return _cache["value"]


def reset_cache():
    with _lock:
        _cache["at"] = 0.0
        _cache["value"] = None


def detect_new(prev, current_classes, now, quiet_seconds=QUIET_SECONDS):
    """Which gauges are NEW (just reached Minor) or UP (moved to a more severe
    class) since this browser last looked.

    `prev` is the seen-set from the browser store ({key: {"p": priority,
    "t": epoch}}) or None; `current_classes` is {key: priority} (1 = Major ..
    3 = Minor). Returns (seen, fresh) with fresh = {key: "new" | "up"}.

    The first snapshot (prev None) only seeds. The seen-set keeps each gauge's
    PEAK class: a gauge easing from Moderate to Minor keeps "Moderate" for
    `quiet_seconds`, and a gauge that leaves the list is remembered for as
    long — so one hovering around a threshold flashes once, not every cycle.
    """
    if prev is None:
        return ({k: {"p": p, "t": now} for k, p in current_classes.items()}, {})
    seen, fresh = {}, {}
    for key, p in current_classes.items():
        old = prev.get(key)
        if old is None:
            fresh[key] = "new"
        elif p < old["p"]:
            fresh[key] = "up"
        if old is None or p <= old["p"] or now - old["t"] >= quiet_seconds:
            seen[key] = {"p": p, "t": now}
        else:
            seen[key] = old              # eased off: hold the peak a while
    for key, old in prev.items():
        if key not in seen and now - old["t"] < quiet_seconds:
            seen[key] = old
    return seen, fresh


def update_flashes(flashes, fresh, gauges, now):
    """Merge newly-detected gauges into the flash store and drop expired ones
    or ones no longer flooding."""
    by_key = {g["key"]: g for g in gauges}
    out = {k: v for k, v in (flashes or {}).items()
           if k in by_key and now - v["at"] < FLASH_SECONDS}
    for key, kind in fresh.items():
        if key in by_key:
            out[key] = {"at": now, "kind": kind, "label": by_key[key]["label"]}
    return out


def choose_page(keys, per_page, tick, flashes, now):
    """Which gauges are on screen. Returns (shown, page, pages, takeover).

    While a gauge is inside its takeover window it (and any other fresh ones,
    most severe first) owns the screen; otherwise pages rotate on `tick`."""
    per_page = per_page if per_page in PER_PAGE_CHOICES else DEFAULT_PER_PAGE
    pages = max(1, -(-len(keys) // per_page))
    hot = [k for k in keys
           if k in (flashes or {}) and now - flashes[k]["at"] < TAKEOVER_SECONDS]
    if hot:
        # Up to four at once, whatever the page size: a burst of crossings
        # should all be seen, and a lone one gets the full one-gauge view.
        return hot[:max(per_page, 4)], None, pages, True
    page = (tick or 0) % pages
    return keys[page * per_page:(page + 1) * per_page], page, pages, False


# --------------------------------------------------------------------------- #
# Components
# --------------------------------------------------------------------------- #
def _layer_option(key, label, colour, shape):
    return {"label": html.Span([
        html.Span(shape, className="fw-swatch", style={"color": colour}),
        label]), "value": key}


def layout():
    options = html.Details([
        html.Summary("⚙ Options", className="btn"),
        html.Div([
            html.Div("Gauges per page", className="display-panel-label"),
            dcc.RadioItems(id="fw-per-page",
                           options=[{"label": f" {n}", "value": n}
                                    for n in PER_PAGE_CHOICES],
                           value=DEFAULT_PER_PAGE, inline=True,
                           className="display-options",
                           persistence=True, persistence_type="local"),
            html.Div("Next page every", className="display-panel-label"),
            dcc.RadioItems(id="fw-rotate-secs",
                           options=[{"label": f" {n} s", "value": n}
                                    for n in ROTATE_CHOICES],
                           value=DEFAULT_ROTATE, inline=True,
                           className="display-options",
                           persistence=True, persistence_type="local"),
            html.Div("Event", className="display-panel-label"),
            dcc.Dropdown(id="fw-event", options=event_options(),
                         placeholder="No event — everything current",
                         clearable=True, className="fw-event-dd",
                         persistence=True, persistence_type="local"),
            html.Div("Roads and incidents then only count those that "
                     "started during the event.", className="display-panel-note"),
            html.Div("Road causes", className="display-panel-label"),
            dcc.Checklist(id="fw-road-causes",
                          options=[{"label": " " + roads_data.CAUSE_LABELS[k],
                                    "value": k}
                                   for k in [*DEFAULT_ROAD_CAUSES,
                                             roads_data.CAUSE_OTHER]],
                          value=DEFAULT_ROAD_CAUSES, className="display-options",
                          persistence=True, persistence_type="local"),
            html.Div("Incident agency", className="display-panel-label"),
            dcc.Checklist(id="fw-agencies",
                          options=[{"label": " " + fire_data.AGENCY_LABELS[k],
                                    "value": k} for k in AGENCY_KEYS],
                          value=DEFAULT_AGENCIES, className="display-options",
                          persistence=True, persistence_type="local"),
            dcc.Checklist(id="fw-show-map",
                          options=[{"label": " Show the state map",
                                    "value": "map"}],
                          value=["map"], className="display-options",
                          persistence=True, persistence_type="local"),
            html.Div("Remembered by this browser only.",
                     className="display-panel-note"),
        ], className="fw-options-panel"),
    ], className="fw-options")

    return html.Div([
        dcc.Interval(id="fw-interval", interval=REFRESH_SECONDS * 1000,
                     n_intervals=0),
        dcc.Interval(id="fw-rotate", interval=DEFAULT_ROTATE * 1000,
                     n_intervals=0),
        dcc.Interval(id="wall-clock-tick", interval=1_000, n_intervals=0),
        dcc.Store(id="fw-seen", storage_type="memory"),
        dcc.Store(id="fw-flash", storage_type="memory"),
        dcc.Store(id="fw-shown", storage_type="memory"),
        wall.header(PATH, stale_id="fw-stale", extra=[options]),
        html.Div(id="fw-tiles", className="wall-tiles fw-tiles"),
        html.Div(id="fw-banner", className="fw-banner"),
        html.Div([
            html.Div([
                html.Div(id="fw-gauges", className="fw-gauges fw-per-4"),
                html.Div(id="fw-pager", className="wall-dots"),
            ], className="fw-left"),
            html.Div([
                dcc.Graph(id="fw-map", className="wall-map-graph",
                          config={"displayModeBar": False, "scrollZoom": True},
                          style={"height": "100%"}),
                dcc.Checklist(id="fw-layers",
                              options=[_layer_option(*row) for row in LAYERS],
                              value=DEFAULT_LAYERS, inline=True,
                              className="fw-layer-chips",
                              persistence=True, persistence_type="local"),
                html.Div(id="fw-road-note", className="fw-road-note"),
            ], className="wall-map fw-map"),
        ], id="fw-body", className="fw-body"),
    ], className="wall-page fw-page")


def event_options():
    """Event tags for the Event selector, newest first."""
    from app import tags
    try:
        return [{"label": f"{t['name']} (from {str(t['start_ts'])[:16]}"
                          + ("" if not t.get("end_ts") else
                             f" to {str(t['end_ts'])[:16]}") + ")",
                 "value": t["id"]} for t in tags.list_tags()]
    except Exception:
        log.exception("Flood wall: event tags unavailable")
        return []


def resolve_tag(event_id):
    """The chosen event tag, or None (no event, or a tag since deleted)."""
    from app import tags
    if event_id in (None, ""):
        return None
    try:
        return tags.get_tag(int(event_id))
    except Exception:
        log.exception("Flood wall: event tag %s unavailable", event_id)
        return None


def incident_selection(event_id, agencies):
    """Active VicEmergency INCIDENTS (not warnings, not burn areas) for the
    wall: only the ticked agencies (none ticked = none shown) and, with an
    event chosen, only those that started during it. Returns (df or None when
    unreadable, tag or None)."""
    from app.pages import fire as fire_page
    tag = resolve_tag(event_id)
    try:
        df = fire_data.active_incidents()
    except Exception:
        log.exception("Flood wall: incidents unavailable")
        return None, tag
    if df.empty:
        return df, tag
    df = df[~df.apply(fire_page._kind, axis=1).isin(fire_page.WARNING_KINDS)]
    if tag is not None and not df.empty:
        df = roads_data.filter_since(df, tag["start_ts"], tag.get("end_ts"))
    agencies = DEFAULT_AGENCIES if agencies is None else agencies
    if df.empty:
        return df, tag
    return df[df["source_org"].map(fire_data.agency_of).isin(agencies)], tag


def road_selection(event_id, causes):
    """Active road disruptions for the wall: optionally only those that STARTED
    within the chosen event tag, and only for the ticked causes (none ticked =
    none shown). Returns (df or None when unreadable, tag or None)."""
    tag = resolve_tag(event_id)
    try:
        df = roads_data.active_disruptions()
    except Exception:
        log.exception("Flood wall: road disruptions unavailable")
        return None, tag
    if tag:
        df = roads_data.filter_since(df, tag["start_ts"], tag.get("end_ts"))
    causes = DEFAULT_ROAD_CAUSES if causes is None else causes
    if not causes:
        return df.iloc[0:0], tag
    return roads_data.filter_causes(df, causes), tag


def agency_note(agencies):
    agencies = DEFAULT_AGENCIES if agencies is None else agencies
    if set(agencies) >= set(AGENCY_KEYS):
        return "all agencies"
    if not agencies:
        return "no agencies selected"
    return ", ".join(fire_data.AGENCY_LABELS[a] for a in AGENCY_KEYS
                     if a in agencies)


def incident_label(agencies):
    """'SES incidents' when exactly one agency is ticked, else 'Incidents'."""
    agencies = DEFAULT_AGENCIES if agencies is None else agencies
    if len(agencies) == 1 and agencies[0] != fire_data.AGENCY_OTHER:
        return f"{fire_data.AGENCY_LABELS[agencies[0]]} incidents"
    return "Incidents"


def road_note(tag, causes, agencies=None):
    """One line saying what the road and incident figures are counting."""
    causes = DEFAULT_ROAD_CAUSES if causes is None else causes
    all_keys = set(DEFAULT_ROAD_CAUSES) | {roads_data.CAUSE_OTHER}
    if set(causes) >= all_keys:
        what = "all causes"
    elif not causes:
        what = "no causes selected"
    else:
        what = ", ".join(roads_data.CAUSE_LABELS[c].lower() for c in causes
                         if c in roads_data.CAUSE_LABELS)
    text = f"Roads: {what} · Incidents: {agency_note(agencies)}"
    if tag:
        text += f" · started since {tag['name']} ({str(tag['start_ts'])[:16]})"
    return text


def _chip(key, label, value, tone):
    return situation.Chip(key, label, value, PATH, tone)


def tiles(snap, sit, roads=None, note=None, incidents=None, agencies=None):
    """Flood tiles, then the warning levels (never summed) and road closures.
    `roads` is the filtered disruption frame (`road_selection`); without one
    the statewide closure count is used."""
    flooding = snap["flooding"] if snap["ok"] else None

    def count(pred):
        return None if flooding is None else sum(1 for g in flooding if pred(g))
    chips = [
        _chip("major", "Major gauges", count(lambda g: g["priority"] == 1),
              "emergency"),
        _chip("moderate", "Moderate gauges", count(lambda g: g["priority"] == 2),
              "watch"),
        _chip("minor", "Minor gauges", count(lambda g: g["priority"] == 3),
              "advice"),
        _chip("rising", "Rising gauges", count(lambda g: g.get("rising")), "alert"),
    ]
    chips += [sit.chip(k) for k in ("emergency", "watch_act", "advice")]
    out = [wall.tile(c) for c in chips]
    # Incidents sit beside the warning levels: a count of their own, never
    # added to a warning total.
    if note is not None:
        inc_tile = wall.tile(_chip(
            "incidents", incident_label(agencies),
            None if incidents is None else len(incidents), "alert"))
        inc_tile.title = note
        out.append(inc_tile)
    if roads is None and note is None:
        out.append(wall.tile(sit.chip("road_closures")))
    else:
        closures = (None if roads is None
                    else int(pd.to_numeric(roads["is_closure"], errors="coerce")
                             .fillna(0).sum()) if not roads.empty else 0)
        road_tile = wall.tile(_chip("road_closures", "Road closures", closures, None))
        road_tile.title = note
        out.append(road_tile)
    return out


# Sources a flood wall depends on; the others' staleness is not its business.
FLOOD_SOURCES = ("Flood", "VicEmergency", "Roads")


def stale_banner(sit):
    relevant = situation.Situation(
        sit.generated_at,
        sources=[s for s in sit.sources if s.name in FLOOD_SOURCES])
    return wall.stale_banner(relevant)


def _fmt_level(value):
    return "—" if value is None else f"{value:g} m"


def _thresholds(g):
    here = CLASS_KEYS.get(g["priority"])
    return html.Div([
        html.Span([html.Span(name.title() + " ", className="muted"),
                   _fmt_level(g["levels"].get(name))],
                  className="fw-threshold"
                  + (f" fw-threshold-on fw-{name}" if name == here else ""))
        for name in ("minor", "moderate", "major")], className="fw-thresholds")


def _trend_line(g):
    a = g.get("analysis")
    if not a or a.get("rate_m_hr") is None:
        return html.Div("Trend: not enough recent readings", className="fw-trend muted")
    rate = a["rate_m_hr"]
    arrow = "▲" if rate >= 0.02 else ("▼" if rate <= -0.02 else "▶")
    parts = [html.Span(f"{arrow} {a['headline']}", className="fw-trend-head"
                       + (" fw-rising" if rate >= 0.02 else "")),
             html.Span(f" {rate:+.2f} m/hr", className="fw-trend-rate")]
    lines = [html.Div(parts, className="fw-trend")]
    if a.get("eta_point"):
        lines.append(html.Div(
            f"{str(a['target_name']).title()} potentially reached "
            f"{a['eta_early']:%H:%M}–{a['eta_late']:%H:%M} · trend projection, "
            "not an official flood forecast", className="fw-eta"))
    return html.Div(lines)


def _age(observed, now):
    minutes = max(0, int((now - observed).total_seconds() // 60))
    if minutes < 60:
        return f"{minutes} min ago"
    return f"{minutes // 60} h {minutes % 60:02d} min ago"


def _history_figure(g, dark):
    from app.pages import station as station_page
    from app.pages.flood import _station_figure
    hist = flood_data.station_history(g["key"], days=HISTORY_DAYS)
    if hist.empty:
        return None
    fig = _station_figure(hist, g["station"], g["label"], g["levels"], dark)
    station_page._add_projection(fig, g.get("analysis"))
    fig.update_layout(title=None, height=None, autosize=True, showlegend=False,
                      margin=dict(l=48, r=14, t=8, b=30),
                      xaxis_title=None, yaxis_title="m")
    return fig


def _impacts(g):
    """Local Flood Guide rows the water has reached (highest first) and the
    next one above it, for the one-gauge view."""
    df = flood_data.load_gauge_impacts(g["key"])
    if df.empty:
        return None
    df = df.dropna(subset=["height_m"])
    reached = df[df["height_m"] <= g["height"]].head(3)
    above = df[df["height_m"] > g["height"]].sort_values("height_m").head(1)
    rows = [html.Div([html.B(f"{r['height_m']:.2f} m "), str(r["impact"])],
                     className="fw-impact fw-impact-reached")
            for _, r in reached.iterrows()]
    rows += [html.Div([html.B(f"Next · {r['height_m']:.2f} m "), str(r["impact"])],
                      className="fw-impact") for _, r in above.iterrows()]
    if not rows:
        return None
    return html.Div([html.Div("Local Flood Guide impacts",
                              className="wall-panel-title"), *rows],
                    className="fw-impacts")


def gauge_card(g, dark, flash, single, now, takeover=False):
    from app.pages import station as station_page
    badge = None
    if flash:
        text = ("NEW · " if flash["kind"] == "new" else "UP · ") + g["label"]
        badge = html.Span(text, className="fw-badge")
    head = html.Div([
        dcc.Link(g["station"], href=station_page.path_for(g["station"]),
                 className="fw-name"),
        badge,
        html.Span(CLASS_NAMES.get(g["priority"], ""),
                  className=f"fw-class fw-{CLASS_KEYS.get(g['priority'], 'below')}"),
    ], className="fw-card-head")
    stats = html.Div([
        html.Div([html.Span(f"{g['height']:.2f}", className="fw-height"),
                  html.Span(" m", className="fw-unit")]),
        html.Div([
            _thresholds(g),
            html.Div(f"Observed {g['observed']:%H:%M} · {_age(g['observed'], now)}"
                     + (f" · {g['catchment']}" if g.get("catchment") else ""),
                     className="muted fw-observed"),
            _trend_line(g),
        ], className="fw-stats-right"),
    ], className="fw-stats")

    fig = _history_figure(g, dark)
    graph = (dcc.Graph(figure=fig, config={"displayModeBar": False,
                                           "responsive": True},
                       style={"height": "100%"}, className="fw-graph")
             if fig is not None else
             html.Div(f"No readings in the last {HISTORY_DAYS} days.",
                      className="muted fw-graph fw-graph-empty"))
    if single:
        stick = station_page.build_gauge_stick(
            g["height"], g["levels"], flood_data.load_gauge_impacts(g["key"]),
            dark, title=None)
        stick.update_layout(height=None, autosize=True,
                            margin=dict(l=70, r=10, t=10, b=10))
        body = html.Div([
            dcc.Graph(figure=stick, config={"displayModeBar": False,
                                            "responsive": True},
                      style={"height": "100%"}, className="fw-stick"),
            html.Div([graph, _impacts(g)], className="fw-single-right"),
        ], className="fw-single-body")
    else:
        body = graph
    cls = f"fw-card fw-card-{CLASS_KEYS.get(g['priority'], 'below')}"
    if flash:
        cls += " fw-card-new"
    if takeover:
        cls += " fw-card-flash"
    return html.Div([head, stats, body], className=cls)


def quiet_panel(snap):
    """No gauge at Minor: say so plainly and show what is closest."""
    if not snap["ok"]:
        return html.Div([html.Div("Flood gauges unavailable", className="fw-quiet-title"),
                         html.Div("The gauge list could not be read — see the "
                                  "server log.", className="muted")],
                        className="fw-quiet")
    rows = [html.Div([
        html.Span(g["station"], className="fw-near-name"),
        html.Span(f"{g['height']:.2f} m", className="fw-near-height"),
        html.Span(f"{g['below_minor_m']:.2f} m below Minor", className="muted"),
    ], className="fw-near") for g in snap["near"]]
    return html.Div([
        html.Div("No gauges at or above Minor flood level",
                 className="fw-quiet-title"),
        html.Div("Readings from the last %d hours. A gauge that reaches Minor "
                 "appears here straight away." % STALE_HOURS, className="muted"),
        html.Div([html.Div("Closest to Minor", className="wall-panel-title"),
                  *rows], className="fw-near-list") if rows else None,
    ], className="fw-quiet")


def banner(flashes, gauges, now):
    by_key = {g["key"]: g for g in gauges}
    hot = [(k, v) for k, v in (flashes or {}).items()
           if k in by_key and now - v["at"] < TAKEOVER_SECONDS]
    if not hot:
        return None, "fw-banner"
    hot.sort(key=lambda kv: by_key[kv[0]]["priority"])
    worst = by_key[hot[0][0]]["priority"]
    words = []
    for key, v in hot:
        g = by_key[key]
        verb = "now at" if v["kind"] == "new" else "up to"
        words.append(f"{g['station']} {verb} {CLASS_NAMES.get(g['priority'])}")
    text = ("⚠ NEW FLOOD GAUGE — " if len(hot) == 1
            else f"⚠ {len(hot)} FLOOD GAUGE CHANGES — ") + " · ".join(words)
    return text, f"fw-banner fw-banner-on fw-{CLASS_KEYS.get(worst, 'minor')}"


def map_figure(snap, layers, dark, shown, roads=None, incidents=None):
    from app.pages import fire as fire_page
    from app.pages import unified

    layers = set(layers or [])
    if "roads" in layers:               # chip name before closures/other split
        layers.add("road_closures")
    kinds = _fire_kinds(layers)

    def source(key):
        if key == "flood":
            df = snap["map"]
            if df.empty:
                return df
            keep = pd.Series(True, index=df.index)
            below = df["label"] == "Below flood level"
            if "gauges" not in layers:
                keep &= below
            if "gauges_below" not in layers:
                keep &= ~below
            return df[keep]
        if key == "roads":
            df = roads_data.active_disruptions() if roads is None else roads
            if df is None or df.empty:
                return df
            closure = pd.to_numeric(df["is_closure"], errors="coerce").fillna(0) > 0
            keep = pd.Series(False, index=df.index)
            if "road_closures" in layers:
                keep |= closure
            if "road_other" in layers:
                keep |= ~closure
            return df[keep]
        if key == "fire":
            df = fire_data.active_incidents()
            if df.empty:
                return df
            kind = df.apply(fire_page._kind, axis=1)
            warnings = df[kind.isin(kinds & set(fire_page.WARNING_KINDS))]
            if incidents is None:
                return df[kind.isin(kinds)]
            # Incidents come pre-filtered (agency + event); warnings don't.
            inc = incidents[incidents.apply(fire_page._kind, axis=1).isin(kinds)]
            return pd.concat([warnings, inc])
        return None

    on = []
    if layers & {"gauges", "gauges_below"}:
        on.append("flood")
    if layers & {"road_closures", "road_other"}:
        on.append("roads")
    if kinds:
        on.append("fire")
    fig = unified.map_figure(on, dark, source=source, uirevision="flood-wall-map")

    # Ring the gauges currently on screen, so the map says where they are.
    by_key = {g["key"]: g for g in snap["flooding"]}
    ring = [by_key[k] for k in (shown or []) if k in by_key
            and by_key[k]["latitude"] is not None]
    if ring:
        fig.add_trace(go.Scattermap(
            lat=[g["latitude"] for g in ring], lon=[g["longitude"] for g in ring],
            mode="markers+text", text=[g["station"] for g in ring],
            textposition="top center",
            textfont=dict(size=14, color="#ffffff" if dark else "#111111"),
            marker=dict(size=30, color="rgba(0, 200, 255, 0.35)"),
            hoverinfo="skip", showlegend=False))
    fig.update_layout(showlegend=False, margin=dict(l=0, r=0, t=0, b=0))
    return fig


# --------------------------------------------------------------------------- #
# Callbacks
# --------------------------------------------------------------------------- #
def register_callbacks(app):
    @app.callback(
        Output("fw-tiles", "children"),
        Output("fw-stale", "children"),
        Output("fw-stale", "title"),
        Output("fw-seen", "data"),
        Output("fw-flash", "data"),
        Output("fw-road-note", "children"),
        Input("fw-interval", "n_intervals"),
        Input("fw-event", "value"),
        Input("fw-road-causes", "value"),
        Input("fw-agencies", "value"),
        State("fw-seen", "data"),
        State("fw-flash", "data"))
    def refresh(_, event_id, causes, agencies, seen, flashes):
        snap = current()
        sit = situation.current()
        now = time.time()
        gauges = snap["flooding"]
        if snap["ok"]:
            seen, fresh = detect_new(seen, {g["key"]: g["priority"] for g in gauges},
                                     now)
        else:
            fresh = {}                  # an unreadable list is not "all cleared"
        flashes = update_flashes(flashes, fresh, gauges, now)
        text = stale_banner(sit)
        roads, tag = road_selection(event_id, causes)
        incidents, _ = incident_selection(event_id, agencies)
        note = road_note(tag, causes, agencies)
        return (tiles(snap, sit, roads, note, incidents, agencies), text, text,
                seen, flashes, note)

    @app.callback(
        Output("fw-rotate", "interval"),
        Input("fw-rotate-secs", "value"))
    def rotate_speed(secs):
        return (secs if secs in ROTATE_CHOICES else DEFAULT_ROTATE) * 1000

    @app.callback(
        Output("fw-body", "className"),
        Input("fw-show-map", "value"))
    def show_map(value):
        return "fw-body" if value and "map" in value else "fw-body fw-nomap"

    @app.callback(
        Output("fw-gauges", "children"),
        Output("fw-gauges", "className"),
        Output("fw-pager", "children"),
        Output("fw-banner", "children"),
        Output("fw-banner", "className"),
        Output("fw-shown", "data"),
        Input("fw-rotate", "n_intervals"),
        Input("fw-flash", "data"),
        Input("fw-per-page", "value"),
        Input("theme-store", "data"))
    def render(tick, flashes, per_page, dark):
        snap = current()
        gauges = snap["flooding"]
        now = time.time()
        per_page = per_page if per_page in PER_PAGE_CHOICES else DEFAULT_PER_PAGE
        text, banner_cls = banner(flashes, gauges, now)
        if not gauges:
            return (quiet_panel(snap), "fw-gauges fw-per-1", None, text,
                    banner_cls, [])
        keys = [g["key"] for g in gauges]
        shown, page, pages, takeover = choose_page(keys, per_page, tick,
                                                   flashes, now)
        by_key = {g["key"]: g for g in gauges}
        wall_now = datetime.now()
        single = len(shown) == 1 and (per_page == 1 or takeover)
        cards = []
        for key in shown:
            try:
                cards.append(gauge_card(by_key[key], bool(dark),
                                        (flashes or {}).get(key), single,
                                        wall_now, takeover=takeover))
            except Exception:
                log.exception("Flood wall: card failed for %s", key)
        dots = [html.Span(className="wall-dot" + (" wall-dot-on" if i == page else ""))
                for i in range(pages)] if pages > 1 else []
        n = len(gauges)
        summary = f"{n} gauge{'s' if n != 1 else ''} at or above Minor"
        if takeover:
            summary += " · showing the latest change"
        elif pages > 1:
            summary += f" · page {page + 1} of {pages}"
        dots.append(html.Span(summary, className="wall-next"))
        grid = "fw-gauges " + ("fw-per-1" if single else
                               f"fw-per-4 fw-n-{min(len(cards), 4)}")
        return cards, grid, dots, text, banner_cls, shown

    @app.callback(
        Output("fw-map", "figure"),
        Input("fw-interval", "n_intervals"),
        Input("fw-layers", "value"),
        Input("fw-shown", "data"),
        Input("fw-event", "value"),
        Input("fw-road-causes", "value"),
        Input("fw-agencies", "value"),
        Input("theme-store", "data"))
    def refresh_map(_, layers, shown, event_id, causes, agencies, dark):
        layers = layers if layers is not None else DEFAULT_LAYERS
        roads, _tag = road_selection(event_id, causes)
        if roads is None:               # unreadable: draw no roads, not all
            roads = pd.DataFrame(columns=["is_closure"])
        incidents, _tag = incident_selection(event_id, agencies)
        if incidents is None:           # unreadable: draw no incidents, not all
            incidents = pd.DataFrame(columns=["feed_type"])
        return map_figure(current(), layers, bool(dark), shown, roads=roads,
                          incidents=incidents)

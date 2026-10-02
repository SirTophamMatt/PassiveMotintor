"""Newsroom wall (/wall/news): the Intelligence Feed presented as a live TV news
desk. A bit of fun on top of real data — every word on screen is a stored
feed entry or a live count; nothing is invented for effect.

* **Desk** (always on): the top story with a map centred on it, a "Latest"
  column, a strip of headline numbers and a scrolling crawler of intel.
* **Takeover**: a NEW feed entry at Critical severity bursts onto the screen as
  BREAKING, a Major one as UPDATE!, zooming up to fill it with the headline,
  the numbers behind it and a map zoomed to where it is happening — then
  shrinks back to the desk. Several at once queue up, most severe first.

"New" is decided per BROWSER (memory store; the first snapshot after load only
seeds), like the flood wall and the alert sounds, so opening the newsroom
mid-event never replays the backlog — the **Replay last** button exists for
exactly that. The takeover clock runs CLIENT-side (a 500 ms clientside
callback), so the zoom-in / hold / zoom-out costs the server nothing; the
server is only asked every `REFRESH_SECONDS` for the feed, and that snapshot
is cached for every viewer.
"""
import json
import logging
import threading
import time
from datetime import datetime

import plotly.io as pio
from dash import Input, Output, State, ctx, dcc, html, no_update

from app import shell, situation
from app.pages import wall

log = logging.getLogger(__name__)

PATH = shell.NEWS_WALL_PATH

REFRESH_SECONDS = 20
CACHE_SECONDS = 20
FEED_HOURS = 12
LATEST_SHOWN = 7
CRAWL_HOURS = 6
CONTEXT_LIMIT = 8          # entries whose cross-layer context lines are resolved
STORY_ZOOM = 8.6
QUEUE_LIMIT = 4            # a burst beyond this goes straight to the Latest column
QUEUE_MAX_AGE = 15 * 60    # a queued story not yet shown after this is dropped

# Severity (intel_feed: 3 critical, 2 major) -> takeover label and hold time.
TAKEOVER = {3: ("BREAKING", 15_000), 2: ("UPDATE!", 10_000)}

STRIP = ["emergency", "watch_act", "advice", "gauges_minor", "road_closures",
         "customers_off", "storm_strong"]


# --------------------------------------------------------------------------- #
# Model
# --------------------------------------------------------------------------- #
_lock = threading.Lock()
_cache = {"at": 0.0, "value": None}


def feed(max_age=CACHE_SECONDS):
    """Recent feed entries, newest first — cached for every viewer."""
    from app import intel_feed
    with _lock:
        if _cache["value"] is None or time.monotonic() - _cache["at"] >= max_age:
            try:
                _cache["value"] = intel_feed.entries(
                    hours=FEED_HOURS, limit=60, context_limit=CONTEXT_LIMIT)
            except Exception:
                log.exception("Newsroom: feed unavailable")
                _cache["value"] = []
            _cache["at"] = time.monotonic()
        return _cache["value"]


def reset_cache():
    with _lock:
        _cache["at"] = 0.0
        _cache["value"] = None


def top_story(entries):
    """The desk's lead: the most severe entry of the last few hours, newest
    first among equals; otherwise simply the newest."""
    if not entries:
        return None
    recent = entries[:15]
    return max(recent, key=lambda e: (e["severity"], e["ts"] or datetime.min))


def takeover_label(entry):
    return TAKEOVER.get(entry["severity"], (None, 0))[0]


def detect_new(seen, entries):
    """(seen_ids, fresh) for this browser. The first snapshot (seen None) only
    seeds; afterwards ``fresh`` is the takeover-worthy entries not seen
    before, most severe then newest first."""
    ids = [e["id"] for e in entries]
    if seen is None:
        return ids, []
    known = set(seen)
    fresh = [e for e in entries if e["id"] not in known and takeover_label(e)]
    fresh.sort(key=lambda e: (-e["severity"], -(e["ts"].timestamp() if e["ts"] else 0)))
    return list(known | set(ids))[-500:], fresh


def story(entry, dark, label=None, figure=True):
    """A takeover-ready story: plain JSON for the browser store."""
    label = label or takeover_label(entry) or "UPDATE!"
    hold = TAKEOVER.get(entry["severity"], ("", 10_000))[1] or 10_000
    out = {
        "id": entry["id"], "label": label, "hold": hold,
        "level": "critical" if label == "BREAKING" else "major",
        "headline": entry["headline"],
        "lines": "\n".join(entry.get("lines") or []),
        "meta": f"{entry['hazard_label'].upper()} · {entry['time']} · "
                f"{entry['severity_label'].upper()}",
        "queued": time.time(),
        "figure": None,
    }
    if figure:
        try:
            out["figure"] = json.loads(pio.to_json(story_map(entry, dark)))
        except Exception:
            log.exception("Newsroom: story map failed for %s", entry["id"])
    return out


def story_map(entry, dark, zoom=STORY_ZOOM):
    from app.pages import unified
    lat, lon = entry.get("latitude"), entry.get("longitude")
    located = lat is not None and lon is not None
    fig = unified.map_figure(
        unified.DEFAULT_LAYERS, dark,
        center={"lat": lat, "lon": lon} if located else None,
        zoom=zoom if located else 5.4, uirevision=f"news-{entry['id']}")
    fig.update_layout(showlegend=False, margin=dict(l=0, r=0, t=0, b=0))
    if located:
        import plotly.graph_objects as go
        fig.add_trace(go.Scattermap(
            lat=[lat], lon=[lon], mode="markers",
            marker=dict(size=34, color="rgba(229, 72, 77, 0.35)"),
            hoverinfo="skip", showlegend=False))
    return fig


def crawl_items(entries, now=None):
    """Crawler text: every entry of the last CRAWL_HOURS, newest first."""
    now = now or datetime.now()
    items = []
    for e in entries:
        if e["ts"] and (now - e["ts"]).total_seconds() > CRAWL_HOURS * 3600:
            continue
        first = (e.get("lines") or [""])[0]
        items.append(f"{e['time']}  {e['hazard_label'].upper()}  {e['headline']}"
                     + (f" — {first}" if first else ""))
    return items


# --------------------------------------------------------------------------- #
# Layout
# --------------------------------------------------------------------------- #
def layout():
    live = html.Div([html.Span(className="nw-live-dot"), "LIVE"], className="nw-live")
    replay = html.Button("↻ Replay last", id="nw-replay", className="btn",
                         title="Run the latest Critical/Major story as a takeover")
    return html.Div([
        dcc.Interval(id="nw-refresh", interval=REFRESH_SECONDS * 1000, n_intervals=0),
        dcc.Interval(id="nw-tick", interval=500, n_intervals=0),
        dcc.Interval(id="wall-clock-tick", interval=1_000, n_intervals=0),
        dcc.Store(id="nw-seen", storage_type="memory"),
        dcc.Store(id="nw-queue", storage_type="memory", data=[]),
        dcc.Store(id="nw-stage", storage_type="memory", data={}),
        wall.header(PATH, stale_id="nw-stale", extra=[replay, live]),
        html.Div([
            html.Span("WATCHDESK", className="nw-bug-brand"),
            html.Span("STATE EMERGENCY DESK", className="nw-bug-show"),
            html.Span(id="nw-bug-status", className="nw-bug-status"),
        ], className="nw-bug"),
        html.Div([
            html.Div([
                html.Div("TOP STORY", className="nw-kicker"),
                html.Div(id="nw-top", className="nw-top"),
                html.Div(dcc.Graph(id="nw-map", className="nw-map-graph",
                                   config={"displayModeBar": False, "scrollZoom": True},
                                   style={"height": "100%"}),
                         className="nw-map"),
            ], className="nw-main"),
            html.Div([
                html.Div("LATEST", className="nw-kicker"),
                html.Div(id="nw-latest", className="nw-latest"),
            ], className="nw-side"),
        ], className="nw-desk"),
        html.Div(id="nw-strip", className="nw-strip"),
        html.Div([
            html.Div(id="nw-crawl-label", children="LATEST", className="nw-crawl-label"),
            html.Div(html.Div(id="nw-crawl-text", className="nw-crawl-text"),
                     className="nw-crawl-track"),
        ], className="nw-crawl"),
        # The takeover is always in the DOM (a callback's components must exist
        # from the first render); CSS shows it while nw-on is set.
        html.Div([
            html.Div(className="nw-to-stripes"),
            html.Div([
                html.Div(id="nw-to-label", className="nw-to-label"),
                html.Div(id="nw-to-meta", className="nw-to-meta"),
                html.Div(id="nw-to-headline", className="nw-to-headline"),
                html.Div(id="nw-to-lines", className="nw-to-lines"),
            ], className="nw-to-text"),
            html.Div(dcc.Graph(id="nw-to-map", config={"displayModeBar": False},
                               style={"height": "100%"}), className="nw-to-map"),
        ], id="nw-takeover", className="nw-takeover"),
    ], className="wall-page nw-page")


def _hazard_tag(entry):
    return html.Span(entry["hazard_label"].upper(), className="nw-tag",
                     style={"background": entry["colour"]})


def top_block(entry):
    if entry is None:
        return html.Div([
            html.Div("The desk is quiet", className="nw-top-headline"),
            html.Div(f"No significant changes in the last {FEED_HOURS} hours.",
                     className="nw-top-lines"),
        ])
    return html.Div([
        html.Div([_hazard_tag(entry),
                  html.Span(f"{entry['time']} · {entry['severity_label']}",
                            className="nw-top-meta")]),
        html.Div(entry["headline"], className="nw-top-headline"),
        html.Div([html.Div(line) for line in (entry.get("lines") or [])[:4]],
                 className="nw-top-lines"),
    ], className=f"nw-top-inner nw-sev-{entry['severity']}")


def latest_block(entries, top):
    rows = [html.Div([
        html.Div([html.Span(e["time"], className="nw-latest-time"), _hazard_tag(e)]),
        html.Div(e["headline"], className="nw-latest-headline"),
    ], className=f"nw-latest-row nw-sev-{e['severity']}")
        for e in entries if top is None or e["id"] != top["id"]][:LATEST_SHOWN]
    return rows or html.Div("Nothing else in the last few hours.",
                            className="nw-latest-empty")


def strip_block(sit):
    out = []
    for key in STRIP:
        chip = sit.chip(key)
        if chip is None:
            continue
        tone = chip.active_tone
        out.append(html.Div([
            html.Span(chip.display, className="nw-strip-value"),
            html.Span(chip.label.upper(), className="nw-strip-label"),
        ], className="nw-strip-item" + (f" chip-{tone}" if tone else "")))
    return out


# --------------------------------------------------------------------------- #
# Callbacks
# --------------------------------------------------------------------------- #
STAGE_JS = """
function(_tick, queue, stage) {
    var nu = window.dash_clientside.no_update;
    var now = Date.now();
    stage = stage || {};
    var shown = stage.shown || [];
    var cur = stage.current;
    var base = 'nw-takeover';
    if (cur && now < stage.until) {
        // Hold, then a short zoom-out before the desk returns.
        var cls = base + ' nw-on nw-' + cur.level
                  + (stage.until - now < 700 ? ' nw-leaving' : '');
        return [cls, nu, nu, nu, nu, nu, nu];
    }
    var next = null;
    (queue || []).forEach(function (s) {
        if (!next && shown.indexOf(s.id + ':' + s.queued) < 0) next = s;
    });
    if (!next) {
        if (!cur) return [base, nu, nu, nu, nu, nu, nu];
        return [base, nu, nu, nu, nu, nu, {shown: shown}];
    }
    shown = shown.concat([next.id + ':' + next.queued]).slice(-200);
    var newStage = {current: next, until: now + (next.hold || 10000), shown: shown};
    return [base + ' nw-on nw-' + next.level, next.label, next.meta, next.headline,
            next.lines, next.figure || nu, newStage];
}
"""


def register_callbacks(app):
    @app.callback(
        Output("nw-top", "children"),
        Output("nw-latest", "children"),
        Output("nw-strip", "children"),
        Output("nw-crawl-text", "children"),
        Output("nw-crawl-text", "style"),
        Output("nw-crawl-label", "children"),
        Output("nw-crawl-label", "className"),
        Output("nw-bug-status", "children"),
        Output("nw-map", "figure"),
        Output("nw-stale", "children"),
        Output("nw-stale", "title"),
        Output("nw-seen", "data"),
        Output("nw-queue", "data"),
        Input("nw-refresh", "n_intervals"),
        Input("nw-replay", "n_clicks"),
        Input("theme-store", "data"),
        State("nw-seen", "data"),
        State("nw-queue", "data"))
    def refresh(_n, _replay, dark, seen, queue):
        dark = bool(dark)
        entries = feed()
        sit = situation.current()
        top = top_story(entries)
        now = time.time()

        seen, fresh = detect_new(seen, entries)
        queue = [s for s in (queue or []) if now - s.get("queued", 0) < QUEUE_MAX_AGE]
        for entry in fresh[:QUEUE_LIMIT]:
            queue.append(story(entry, dark))
        if ctx.triggered_id == "nw-replay":
            pick = next((e for e in entries if takeover_label(e)), None) or top
            if pick is not None:
                queue.append(story(pick, dark))
        queue = queue[-QUEUE_LIMIT * 2:]

        items = crawl_items(entries)
        crawl = "      ●      ".join(items) if items else (
            "No significant changes in the last few hours — the desk is quiet.")
        # Scroll speed scales with length so a long crawl doesn't race past.
        style = {"animationDuration": f"{max(40, int(len(crawl) * 0.16))}s"}
        breaking = any(e["severity"] >= 3 for e in entries[:10])
        label = "BREAKING" if breaking else "LATEST"
        status = (f"{len(items)} updates · last {CRAWL_HOURS} h" if items
                  else "Quiet")
        banner = wall.stale_banner(sit)
        try:
            fig = story_map(top, dark) if top else story_map(
                {"id": "state", "latitude": None, "longitude": None}, dark)
        except Exception:
            log.exception("Newsroom: desk map failed")
            fig = no_update
        return (top_block(top), latest_block(entries, top), strip_block(sit),
                crawl, style, label,
                "nw-crawl-label" + (" nw-crawl-breaking" if breaking else ""),
                status, fig, banner, banner, seen, queue)

    app.clientside_callback(
        STAGE_JS,
        Output("nw-takeover", "className"),
        Output("nw-to-label", "children"),
        Output("nw-to-meta", "children"),
        Output("nw-to-headline", "children"),
        Output("nw-to-lines", "children"),
        Output("nw-to-map", "figure"),
        Output("nw-stage", "data"),
        Input("nw-tick", "n_intervals"),
        Input("nw-queue", "data"),
        State("nw-stage", "data"))

# Passive Monitor — Copyright (c) 2026 SirTophamMatt. All rights reserved.
"""Event Replay as ONE self-contained HTML file — for debriefs.

The /replay page asks the server for a new map on every slider step, which is
slow over the web for a long event. This builds the whole event once and hands
it to the browser: a single .html file carrying the event's data, the player
and Plotly itself, so it can be opened from a laptop, a USB stick or an email
attachment and scrubbed or played at any speed with no server at all.

What goes in (all from stored data — nothing here fetches, same rule as
`app.replay`):

* **Flood gauges.** Every gauge with coordinates is on the map, its class
  changing as it did. Every gauge that reached Minor or above during the event
  gets a height graph with its class levels, grouped by river (the BoM
  catchment), and a cursor that follows the replay clock.
* **VicEmergency warnings and incidents** from the state journal (burn-area
  footprints left out — static plan data, not events).
* **Road disruptions, weather-related only** — the flood wall's rule: the
  cause is flooding, weather/storm or trees/debris (`roads.data.causes_of`),
  and the disruption started during the event. A months-old landslip closure
  or a crash is not part of this event's story.
* BoM warnings (as a list — they carry no geometry), statewide customers off,
  and the Intelligence Feed as a click-to-seek timeline.

Time is stored as whole minutes from the event start, and an entity is its
list of state CHANGES; the browser binary-searches each entity for the moment
on the clock. That is what keeps the file small and playback instant at
15 minutes of event time per second (or faster).

Basemap tiles are the one thing the file still needs the internet for; offline
the map draws on a plain background and every layer still works.
"""
import html
import json
import logging
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

from app import database, history, intel_feed, replay
from app.modules.fire import data as fire_data
from app.modules.flood import data as flood_data
from app.modules.roads import data as roads_data
from app.pages import fire as fire_page

log = logging.getLogger(__name__)

TEMPLATE = Path(__file__).resolve().parent / "replay_player.html"

# Graph context before the event starts, so a gauge's rise is visible from
# where it began rather than from the first minute of the tag.
GRAPH_LEAD_HOURS = 12
# A gauge silent this long is drawn as not reporting (mirrors
# replay.STALE_READING_HOURS) instead of frozen at its last class.
STALE_MINUTES = replay.STALE_READING_HOURS * 60
# Power KPI rows older than this read as "—" (mirrors replay._latest_timeseries).
POWER_STALE_MINUTES = 6 * 60
# Same default as the flood wall: what a flood event closes roads for.
ROAD_CAUSES = ("flooding", "weather", "trees")
# ~11 m. Warning polygons are the bulk of the file; a fifth decimal is
# invisible at any zoom a debrief uses.
COORD_DECIMALS = 4
TIMELINE_LIMIT = 400

# Flood classes, most severe = highest number (the player sorts on it).
NO_DATA, BELOW, MINOR, MODERATE, MAJOR = -1, 0, 1, 2, 3
_CLASS_OF_PRIORITY = {1: MAJOR, 2: MODERATE, 3: MINOR, 4: BELOW}

CACHE_SECONDS = 600
_cache = {}
_cache_lock = threading.Lock()


def _minutes(when, start):
    return int(round((when - start).total_seconds() / 60.0))


def _dt(value):
    parsed = pd.to_datetime(value, errors="coerce")
    return None if pd.isna(parsed) else parsed.to_pydatetime().replace(tzinfo=None)


def _text(value):
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    s = str(value).strip()
    return s if s and s.lower() not in ("nan", "none") else None


def _num(value):
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return None if pd.isna(f) else f


def _round_coords(obj):
    if isinstance(obj, float):
        return round(obj, COORD_DECIMALS)
    if isinstance(obj, list):
        return [_round_coords(v) for v in obj]
    if isinstance(obj, dict):
        return {k: _round_coords(v) for k, v in obj.items()}
    return obj


class _Geoms:
    """De-duplicated geometry table: a warning area that is reissued twenty
    times with the same polygon is stored once."""

    def __init__(self):
        self.items, self._index = [], {}

    def add(self, raw):
        if not raw:
            return None
        try:
            geom = json.loads(raw) if isinstance(raw, str) else raw
        except (TypeError, ValueError):
            return None
        if not isinstance(geom, dict) or not geom.get("type"):
            return None
        geom = _round_coords(geom)
        key = json.dumps(geom, sort_keys=True, separators=(",", ":"))
        if key not in self._index:
            self._index[key] = len(self.items)
            self.items.append(geom)
        return self._index[key]


# --------------------------------------------------------------------------- #
# Journal sources
# --------------------------------------------------------------------------- #
def _journal(source, start, end):
    """Each entity's state at the start, plus every change during the event —
    everything the player needs to answer "as at T" for any T in the window."""
    before = history.state_at(source, start)
    during = history.states_between(source, start + timedelta(seconds=1), end)
    frames = [f for f in (before, during) if f is not None and not f.empty]
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True)
    df["_ts"] = pd.to_datetime(df["effective_ts"], errors="coerce")
    df = df.dropna(subset=["_ts"]).sort_values(["entity_key", "_ts"], kind="stable")
    return df


def _row_dict(row):
    return {k: (None if (isinstance(v, float) and pd.isna(v)) else v)
            for k, v in row.items()}


def _fire_hover(r, kind):
    title = _text(r.get("location")) or _text(r.get("headline")) or "Incident"
    bits = [b for b in (kind, incident_type(r) or _text(r.get("event")),
                        _text(r.get("status")), _text(r.get("size")),
                        _text(r.get("source_org"))) if b]
    return "<b>%s</b><br>%s" % (html.escape(title), html.escape(" · ".join(bits)))


def incident_type(r):
    """What an incident is, for the player's type filter: the feed's category,
    with the sub-category for fires so "Fire – Burn Off" and "Fire – Planned
    Burn" can be told apart from a going fire. None for a warning."""
    if fire_data.is_warning(r):
        return None
    cat1 = _text(r.get("category1")) or "Unspecified"
    cat2 = _text(r.get("category2"))
    if cat2 and cat2.lower() != cat1.lower() and cat1.lower() == "fire":
        return "%s – %s" % (cat1, cat2)
    return cat1


def fire_entities(start, end, geoms):
    """VicEmergency warnings + incidents as change lists.

    State row: [t, active, kind, lat, lon, geom index, hover, agency, type]."""
    df = _journal(history.FIRE, start, end)
    if df.empty:
        return []
    out = []
    for key, grp in df.groupby("entity_key", sort=False):
        states = []
        for _, row in grp.iterrows():
            r = _row_dict(row)
            if r.get("feed_type") == "burn-area":
                states = []
                break
            kind = fire_page._kind(r)
            states.append([
                max(_minutes(row["_ts"].to_pydatetime(), start), -1),
                1 if r.get("active") == 1 else 0, kind,
                _num(r.get("latitude")), _num(r.get("longitude")),
                geoms.add(r.get("geometry")), _fire_hover(r, kind),
                fire_data.agency_of(r.get("source_org")), incident_type(r)])
        if states:
            out.append({"k": str(key), "s": _collapse(states)})
    return out


def _collapse(states):
    """Clamp everything before the start to t=-1 and keep only the last such
    state; later states keep their order."""
    pre = [s for s in states if s[0] < 0]
    post = [s for s in states if s[0] >= 0]
    return (pre[-1:] if pre else []) + post


def road_entities(start, end, geoms, causes=ROAD_CAUSES):
    """Weather-related road disruptions that STARTED during the event.

    State row: [t, active, closure, lat, lon, geom index, hover]."""
    df = _journal(history.ROADS, start, end)
    if df.empty:
        return []
    wanted = set(causes)
    out = []
    for key, grp in df.groupby("entity_key", sort=False):
        first = grp.iloc[0]
        began = _dt(first.get("start_time")) or first["_ts"].to_pydatetime()
        if began < start or began > end:
            continue
        rows = [_row_dict(r) for _, r in grp.iterrows()]
        if not any(roads_data.causes_of(r.get("disruption_type"),
                                        r.get("description")) & wanted
                   for r in rows):
            continue
        states = []
        for (_, row), r in zip(grp.iterrows(), rows):
            closure = 1 if r.get("is_closure") in (1, True, "1") else 0
            road = _text(r.get("road_name")) or _text(r.get("location")) or "Road"
            bits = [b for b in (_text(r.get("disruption_type")),
                                "Closed" if closure else _text(r.get("lanes_affected")),
                                _text(r.get("location")), _text(r.get("lga"))) if b]
            hover = "<b>%s</b><br>%s" % (html.escape(road),
                                         html.escape(" · ".join(bits)))
            states.append([
                max(_minutes(row["_ts"].to_pydatetime(), start), -1),
                1 if r.get("active") == 1 else 0, closure,
                _num(r.get("latitude")), _num(r.get("longitude")),
                geoms.add(r.get("geometry")), hover])
        out.append({"k": str(key), "s": _collapse(states)})
    return out


def bom_warnings(start, end):
    """BoM warnings as [title, [[t, active], ...]] — no geometry, so a list."""
    df = _journal(history.WEATHER_WARNING, start, end)
    if df.empty:
        return []
    out = []
    for _, grp in df.groupby("entity_key", sort=False):
        title = None
        states = []
        for _, row in grp.iterrows():
            r = _row_dict(row)
            title = _text(r.get("title")) or _text(r.get("short_title")) or title
            states.append([max(_minutes(row["_ts"].to_pydatetime(), start), -1),
                           1 if r.get("active") == 1 else 0])
        out.append({"title": title or "BoM warning", "s": _collapse(states)})
    return out


# --------------------------------------------------------------------------- #
# Flood gauges
# --------------------------------------------------------------------------- #
def _classify(height, levels):
    return _CLASS_OF_PRIORITY[flood_data.classify_station(height, levels)[0]]


def flood_gauges(start, end):
    """Every gauge with a reading in the window.

    Each carries its class CHANGES for the map (a gauge that never moves is two
    numbers, not two thousand), and — only if it reached Minor during the event
    — its readings for the graph."""
    lead = start - timedelta(hours=GRAPH_LEAD_HOURS)
    df = database.read_df(
        "SELECT station_name, catchment, height_m, timestamp "
        "FROM flood_observations WHERE timestamp >= ? AND timestamp <= ? "
        "ORDER BY timestamp",
        [lead.strftime("%Y-%m-%d %H:%M:%S"), end.strftime("%Y-%m-%d %H:%M:%S")])
    if df.empty:
        return []
    df["height_m"] = pd.to_numeric(df["height_m"], errors="coerce")
    df["ts"] = pd.to_datetime(df["timestamp"], errors="coerce")
    df = df.dropna(subset=["height_m", "ts"])
    df["key"] = df["station_name"].astype(str).str.strip().str.lower()
    levels = flood_data.load_flood_levels()
    coords = database.read_df(
        "SELECT station_key, latitude, longitude FROM gauge_coords "
        "WHERE latitude IS NOT NULL AND longitude IS NOT NULL")
    coords = {r["station_key"]: (float(r["latitude"]), float(r["longitude"]))
              for _, r in coords.iterrows()}

    out = []
    for key, grp in df.groupby("key", sort=False):
        grp = grp.drop_duplicates(subset=["ts"], keep="last")
        lv = levels.get(key)
        minutes = [_minutes(t.to_pydatetime(), start) for t in grp["ts"]]
        heights = [round(float(h), 3) for h in grp["height_m"]]
        classes = [_classify(h, lv) for h in heights]
        in_event = [c for m, c in zip(minutes, classes) if m >= 0]
        peak = max(in_event) if in_event else NO_DATA
        # The class carried into the event is the last reading before the
        # start, if it is recent enough to count.
        changes, last_cls, last_t = [], None, None
        for m, c in zip(minutes, classes):
            if last_t is not None and m - last_t > STALE_MINUTES:
                changes.append([last_t + STALE_MINUTES, NO_DATA])
                last_cls = NO_DATA
            if c != last_cls:
                changes.append([m, c])
                last_cls = c
            last_t = m
        if last_t is not None and _minutes(end, start) - last_t > STALE_MINUTES:
            changes.append([last_t + STALE_MINUTES, NO_DATA])
        pre = [c for c in changes if c[0] < 0]
        changes = ([[-1, pre[-1][1]]] if pre else []) + [c for c in changes if c[0] >= 0]
        if not changes:
            continue
        latlon = coords.get(key)
        name = str(grp["station_name"].iloc[-1]).strip()
        catchment = grp["catchment"].dropna()
        gauge = {
            "name": name,
            "river": (str(catchment.mode().iloc[0]).strip()
                      if not catchment.empty else "Other gauges"),
            "lat": latlon[0] if latlon else None,
            "lon": latlon[1] if latlon else None,
            "lv": [_num(lv.get("minor")), _num(lv.get("moderate")),
                   _num(lv.get("major"))] if lv else [None, None, None],
            "peak": peak,
            "c": changes,
        }
        if peak >= MINOR:
            gauge["r"] = [[m, h] for m, h in zip(minutes, heights)]
            peak_h = max(h for m, h in zip(minutes, heights) if m >= 0)
            gauge["peak_h"] = peak_h
            gauge["peak_t"] = next(m for m, h in zip(minutes, heights)
                                   if m >= 0 and h == peak_h)
        out.append(gauge)
    return out


# --------------------------------------------------------------------------- #
# Series + timeline
# --------------------------------------------------------------------------- #
def fire_recorded(start, end):
    """The VicEmergency counts the dashboard RECORDED each cycle
    (`fire_timeseries`): [t, incidents, fires, emergency, watch&act, advice].

    These are what the dashboard actually showed at the time, so the player's
    warning cards come from here — the same source the /replay page uses — and
    the map's reconstruction is reported beside them, not instead of them."""
    lead = start - timedelta(minutes=POWER_STALE_MINUTES)
    df = database.read_df(
        "SELECT timestamp, total_active, active_fires, emergency_warnings, "
        "watch_act, advice FROM fire_timeseries "
        "WHERE timestamp >= ? AND timestamp <= ? ORDER BY timestamp",
        [lead.strftime("%Y-%m-%d %H:%M:%S"), end.strftime("%Y-%m-%d %H:%M:%S")])
    out = []
    for _, r in df.iterrows():
        when = _dt(r["timestamp"])
        if when is None:
            continue
        vals = [int(_num(r[c]) or 0) for c in ("total_active", "active_fires",
                "emergency_warnings", "watch_act", "advice")]
        total, fires, em, wa, adv = vals
        out.append([_minutes(when, start), max(total - em - wa - adv, 0),
                    fires, em, wa, adv])
    return out


def power_locations(start, end):
    """Per-location outages from the journal: [[t, active, customers, lat, lon]].

    Coordinates the journal recorded without (geocoded only later) are filled
    from the geocode cache — a town's position did not change during the event,
    the same reasoning as `replay.power_at`."""
    df = _journal(history.POWER, start, end)
    if df.empty:
        return []
    cache = database.read_df(
        "SELECT location, latitude, longitude FROM geocode_cache")
    coords = {r["location"]: (_num(r["latitude"]), _num(r["longitude"]))
              for _, r in cache.iterrows()}
    out = []
    for key, grp in df.groupby("entity_key", sort=False):
        states, peak = [], 0
        for _, row in grp.iterrows():
            r = _row_dict(row)
            lat, lon = _num(r.get("latitude")), _num(r.get("longitude"))
            if lat is None and key in coords:
                lat, lon = coords[key]
            customers = int(_num(r.get("customers_off")) or 0)
            active = 1 if r.get("active") == 1 else 0
            t = max(_minutes(row["_ts"].to_pydatetime(), start), -1)
            states.append([t, active, customers, lat, lon])
            if active and t >= -1:
                peak = max(peak, customers)
        states = _collapse(states)
        if states:
            out.append({"name": str(key), "peak": peak, "s": states})
    out.sort(key=lambda e: -e["peak"])
    return out


def power_series(start, end):
    lead = start - timedelta(minutes=POWER_STALE_MINUTES)
    df = database.read_df(
        "SELECT timestamp, customers_off FROM power_timeseries "
        "WHERE timestamp >= ? AND timestamp <= ? ORDER BY timestamp",
        [lead.strftime("%Y-%m-%d %H:%M:%S"), end.strftime("%Y-%m-%d %H:%M:%S")])
    out = []
    for _, r in df.iterrows():
        when, value = _dt(r["timestamp"]), _num(r["customers_off"])
        if when is not None and value is not None:
            out.append([_minutes(when, start), int(value)])
    return out


def timeline(start, end):
    out = []
    for e in replay.timeline(start, end, limit=TIMELINE_LIMIT):
        if e.get("ts") is None:
            continue
        out.append({"t": _minutes(e["ts"], start), "sev": e["severity"],
                    "label": e["severity_label"], "colour": e["colour"],
                    "hazard": e.get("hazard_label"), "h": e["headline"],
                    "lat": e.get("latitude"), "lon": e.get("longitude")})
    return out


# --------------------------------------------------------------------------- #
# Package + render
# --------------------------------------------------------------------------- #
def _section(name, fn, default):
    """One broken source costs only its own layer, never the whole export."""
    try:
        return fn()
    except Exception:
        log.exception("Replay export: %s unavailable", name)
        return default


def build_package(tag_id):
    """The event as one JSON-able dict, or None for an unknown tag."""
    window = replay.event_window(tag_id)
    if not window:
        return None
    start, end = window["start"], window["end"]
    geoms = _Geoms()
    package = {
        "meta": {
            "name": window["name"],
            "start": start.strftime("%Y-%m-%dT%H:%M:%S"),
            "end": end.strftime("%Y-%m-%dT%H:%M:%S"),
            "minutes": max(_minutes(end, start), 1),
            "ongoing": window["ongoing"],
            "generated": datetime.now().strftime("%d %b %Y %H:%M"),
            "coverage": replay.coverage_note(start),
            "road_causes": [roads_data.CAUSE_LABELS[c] for c in ROAD_CAUSES],
            "stale_minutes": STALE_MINUTES,
            "power_stale_minutes": POWER_STALE_MINUTES,
        },
        "colours": dict(fire_page.KIND_COLOURS),
        "agencies": [[k, v] for k, v in fire_data.AGENCY_LABELS.items()],
        "fire": _section("fire", lambda: fire_entities(start, end, geoms), []),
        "roads": _section("roads", lambda: road_entities(start, end, geoms), []),
        "bom": _section("bom", lambda: bom_warnings(start, end), []),
        "gauges": _section("flood", lambda: flood_gauges(start, end), []),
        "power": _section("power", lambda: power_series(start, end), []),
        "outages": _section("outages", lambda: power_locations(start, end), []),
        "recorded": _section("recorded", lambda: fire_recorded(start, end), []),
        "timeline": _section("timeline", lambda: timeline(start, end), []),
    }
    package["geoms"] = geoms.items
    return package


def _plotly_js():
    from plotly.offline import get_plotlyjs
    return get_plotlyjs()


def _safe_json(obj):
    """JSON that cannot close the <script> it sits in."""
    text = json.dumps(obj, separators=(",", ":"), ensure_ascii=False, default=str)
    return (text.replace("<", "\\u003c").replace(">", "\\u003e")
            .replace("&", "\\u0026").replace("\u2028", "\\u2028")
            .replace("\u2029", "\\u2029"))


def render_html(package):
    page = TEMPLATE.read_text(encoding="utf-8")
    title = html.escape("Replay — %s" % package["meta"]["name"])
    # Plain replace, in this order: the data goes in LAST so no token inside
    # it (or inside plotly.js) can ever be substituted.
    page = page.replace("{{TITLE}}", title)
    page = page.replace("{{PLOTLY_JS}}", _plotly_js().replace("</script", "<\\/script"))
    return page.replace("{{REPLAY_DATA}}", _safe_json(package))


def filename(package):
    safe = "".join(c if c.isalnum() or c in "-_" else "_"
                   for c in package["meta"]["name"]).strip("_") or "event"
    return "replay_%s_%s.html" % (safe[:60], package["meta"]["start"][:10])


def export(tag_id):
    """(filename, html) for an event, cached briefly so a debrief room all
    pressing Download does not rebuild it each time. None for an unknown tag."""
    now = time.time()
    with _cache_lock:
        hit = _cache.get(tag_id)
        if hit and now - hit[0] < CACHE_SECONDS:
            return hit[1]
        package = build_package(tag_id)
        if package is None:
            return None
        result = (filename(package), render_html(package))
        for key in [k for k, v in _cache.items() if now - v[0] >= CACHE_SECONDS]:
            del _cache[key]
        _cache[tag_id] = (now, result)
        return result


def clear_cache():
    with _cache_lock:
        _cache.clear()


# --------------------------------------------------------------------------- #
# Diagnosis — what the journal holds for a moment, against what was recorded
# --------------------------------------------------------------------------- #
def diagnose(tag_id, at=None):
    """Plain-text report for one moment of an event (admin only).

    Puts the dashboard's own recorded counts (`fire_timeseries`) beside the
    journal's reconstruction, broken down by feed type, level, category and
    agency, so a replay that disagrees with what people saw can be traced to
    the data rather than guessed at."""
    window = replay.event_window(tag_id)
    if not window:
        return "Event not found."
    when = _dt(at) if at else None
    when = when or window["start"] + (window["end"] - window["start"]) / 2
    stamp = when.strftime("%Y-%m-%d %H:%M:%S")
    out = ["Replay diagnosis — %s" % window["name"],
           "Event: %s -> %s" % (window["start"], window["end"]),
           "Moment: %s   (add ?at=YYYY-MM-DD HH:MM to choose)" % stamp, ""]

    rec = database.read_df(
        "SELECT * FROM fire_timeseries WHERE timestamp <= ? "
        "ORDER BY timestamp DESC LIMIT 1", [stamp])
    out.append("Recorded by the dashboard at the time (fire_timeseries):")
    if rec.empty:
        out.append("  none")
    else:
        r = rec.iloc[0]
        out.append("  at %s: total_active=%s active_fires=%s emergency=%s "
                   "watch_act=%s advice=%s" % (
                       r["timestamp"], r.get("total_active"), r.get("active_fires"),
                       r.get("emergency_warnings"), r.get("watch_act"),
                       r.get("advice")))
    out.append("")

    df = history.state_at(history.FIRE, when)
    out.append("Journal reconstruction at that moment: %d active entities" % len(df))
    if not df.empty:
        rows = [_row_dict(r) for _, r in df.iterrows()]
        tally = {}
        for r in rows:
            key = (_text(r.get("feed_type")) or "?",
                   _text(r.get("warning_level")) if fire_data.is_warning(r)
                   else incident_type(r),
                   fire_data.agency_of(r.get("source_org")))
            tally[key] = tally.get(key, 0) + 1
        out.append("  %-10s %-40s %-8s %s" % ("feed_type", "level / type",
                                              "agency", "count"))
        for (ft, what, agency), n in sorted(tally.items(), key=lambda kv: -kv[1]):
            out.append("  %-10s %-40s %-8s %d" % (ft, (what or "?")[:40], agency, n))
        ages = pd.to_datetime(df["effective_ts"], errors="coerce")
        old = int((ages < pd.Timestamp(when) - pd.Timedelta(days=3)).sum())
        out.append("  last changed more than 3 days before this moment: %d" % old)
    out.append("")

    j = database.read_df(
        "SELECT active, COUNT(*) AS n, COUNT(DISTINCT entity_key) AS entities "
        "FROM entity_state_history WHERE source = ? AND effective_ts BETWEEN ? AND ? "
        "GROUP BY active", [history.FIRE, str(window["start"]), str(window["end"])])
    out.append("Journal rows written during the event (source=fire):")
    for _, r in j.iterrows():
        out.append("  %s: %d rows, %d entities" % (
            "active" if r["active"] else "tombstone", r["n"], r["entities"]))
    live = database.read_df(
        "SELECT feed_type, resolved, COUNT(*) AS n FROM fire_incidents "
        "GROUP BY feed_type, resolved")
    out.append("")
    out.append("fire_incidents table now (feed_type, resolved, count):")
    for _, r in live.iterrows():
        out.append("  %s  resolved=%s  %d" % (r["feed_type"], r["resolved"], r["n"]))
    out.append("")
    out.append("History available from: %s" % history.history_starts())
    return "\n".join(out)

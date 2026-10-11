# Passive Monitor — Copyright (c) 2026 SirTophamMatt. All rights reserved.
"""Storm areas of operation — the storm wall's model.

UI-free (like `fire_areas` / `briefing`): the `/wall/storm` page renders from it.

A storm or flood event shows up as SES jobs scattered across suburbs — trees
down, roofs off, water over roads. Read one at a time they are noise; what a
wall needs is WHERE the work is concentrated. So, unlike the fire wall (one
area per fire), this wall LINKS incidents:

* **Incidents seed and link.** Weather-related incidents (`is_weather_incident`)
  from the selected agencies (SES by default) that sit within `link_km` of each
  other are one area — chained, so a line of jobs along a storm's track is one
  area. The area grows, splits and merges as jobs come and go: it is rebuilt
  from scratch on every snapshot, never stored.
* **Warnings attach.** A weather-related VicEmergency warning
  (`is_weather_warning`) joins every area it is within `link_km` of — a flood
  warning over two clusters is part of both pictures — but never links two
  areas itself (a catchment-wide polygon would merge the state). A Watch and Act
  or Emergency Warning with no incidents near it still gets an area of its own:
  those levels ask people to act, so they are never left without a card.
* **Context attaches to the nearest area**: weather-caused road disruptions,
  power outages, flood gauges at/above Minor and moderate/strong storm cells,
  within `link_km` of an incident. Context never links two areas.

Everything comes from stored data (`load_frames`), handed to `assemble`, which
the in-memory test run (`app.storm_demo`) calls with its own frames — so the
test exercises exactly the linking the wall uses.
"""
import logging
import re
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from app import fire_areas
from app.fire_areas import HAVE_SHAPELY, STRtree, shapely

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Tunables
# --------------------------------------------------------------------------- #
LINK_KM_CHOICES = [2, 5, 10, 20]
DEFAULT_LINK_KM = 5
AGENCY_KEYS = ["ses", "cfa", "frv", "ffm", "other"]
DEFAULT_AGENCIES = ["ses"]
# Weather causes for road disruptions (`roads.data.CAUSES` keys).
ROAD_CAUSES = ["flooding", "weather", "trees"]
# An incident first seen this recently counts as "new" on its area's card.
NEW_MINUTES = 60
# An area crossing one of these incident counts is a takeover ("area growing").
SIZE_STEPS = (5, 10, 20, 50)
# Flood gauges older than this are not counted as flooding.
GAUGE_STALE_HOURS = 24
CACHE_SECONDS = 45

WARNING_RANK = fire_areas.WARNING_RANK
ACT_LEVELS = ("Emergency Warning", "Watch and Act")

# Matched against an INCIDENT's category1/category2/event. Fires are excluded
# before this runs (`fire.data.is_fire`), so "structure" here is storm damage.
_WEATHER_INCIDENT_RE = re.compile(
    r"tree|building|damage|roof|structure|flood|water|storm|wind|hail|"
    r"rain|snow|land ?slip|landslide|\bmet\b|weather|power ?line|debris|"
    r"sandbag|coastal|surge|erosion", re.I)
# Matched against a WARNING's hazard (event/category2; headline only when the
# feed gives no hazard). Fire warnings are excluded first.
_WEATHER_WARNING_RE = re.compile(
    r"weather|storm|wind|flood|thunder|hail|rain|snow|surge|coastal|"
    r"tsunami|land ?slip|landslide|severe|tide|dam\b|blizzard|cyclone",
    re.I)


def _text(value):
    if value is None or value != value:            # None / NaN
        return ""
    return str(value)


def is_weather_incident(row):
    """A storm / flood / wind job: an incident (not a warning, not a fire) whose
    category or event reads as weather damage."""
    from app.modules.fire import data as fire_data
    if row.get("feed_type") in ("warning", "burn-area") or fire_data.is_fire(row):
        return False
    text = " ".join(_text(row.get(c)) for c in ("category1", "category2", "event"))
    return bool(_WEATHER_INCIDENT_RE.search(text))


def is_weather_warning(row):
    """A warning about weather or flooding, by its hazard fields — never a fire
    warning, whatever its headline says."""
    if row.get("feed_type") != "warning" or fire_areas.is_fire_warning(row):
        return False
    hazard = " ".join(_text(row.get(c)) for c in ("event", "category2")).strip()
    text = hazard or " ".join(_text(row.get(c)) for c in ("headline", "location"))
    return bool(_WEATHER_WARNING_RE.search(text))


# --------------------------------------------------------------------------- #
# Members and areas
# --------------------------------------------------------------------------- #
@dataclass
class Member:
    id: str
    role: str                 # incident | warning | road | power | gauge | cell
    kind: str                 # Tree Down / Watch and Act / Closure / Major …
    location: str
    latitude: float = None
    longitude: float = None
    level: str = None         # warnings only
    detail: str = None
    started: datetime = None  # incidents: when it began (feed created / first seen)
    value: float = 0.0        # power: customers off; gauge: height
    rank: int = 9             # gauge class 1-3, cell 1-2, road 1 = closure
    geom: object = None       # metric shapely geometry


@dataclass
class OperationArea:
    key: str
    name: str
    members: list = field(default_factory=list)
    level: str = None         # most severe warning level in the area
    places: list = field(default_factory=list)   # localities, busiest first
    newest_at: datetime = None
    new_count: int = 0        # incidents started in the last NEW_MINUTES
    center: dict = None
    zoom: float = 10.0
    outline: dict = None      # lon/lat GeoJSON of the area's hull

    def of_role(self, role):
        return [m for m in self.members if m.role == role]

    @property
    def incidents(self):
        return sorted(self.of_role("incident"),
                      key=lambda m: m.started or datetime.min, reverse=True)

    @property
    def warnings(self):
        return sorted(self.of_role("warning"),
                      key=lambda m: WARNING_RANK.get(m.level, 9))

    @property
    def level_rank(self):
        return WARNING_RANK.get(self.level, 9)

    @property
    def closures(self):
        return [m for m in self.of_role("road") if m.rank == 1]

    @property
    def customers_off(self):
        return int(sum(m.value or 0 for m in self.of_role("power")))

    @property
    def worst_gauge(self):
        gauges = sorted(self.of_role("gauge"), key=lambda m: m.rank)
        return gauges[0] if gauges else None

    @property
    def strong_cells(self):
        return [m for m in self.of_role("cell") if m.rank == 1]

    def kind_counts(self):
        """Incident kinds, most common first: [('Tree Down', 4), …]."""
        return Counter(m.kind for m in self.of_role("incident")).most_common()

    def member_ids(self):
        return {m.id for m in self.members}


def _dt(value):
    if value is None:
        return None
    try:
        ts = pd.Timestamp(value)
    except (TypeError, ValueError):
        return None
    if ts is pd.NaT or pd.isna(ts):
        return None
    if ts.tzinfo is not None:
        ts = ts.tz_localize(None)
    return ts.to_pydatetime()


def _point(lat, lon):
    return shapely.Point((float(lon) - fire_areas.LON0) * fire_areas.M_PER_DEG_LON,
                         (float(lat) - fire_areas.LAT0) * fire_areas.M_PER_DEG_LAT)


def _located(row):
    lat, lon = row.get("latitude"), row.get("longitude")
    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        return None, None
    if lat != lat or lon != lon:
        return None, None
    return lat, lon


def _geom(row, polygons=True):
    """Metric geometry: the row's polygon(s) when it has them and `polygons`,
    else its point."""
    if polygons:
        mp = fire_areas.polygons_only(row.get("geometry"))
        if mp is not None:
            return fire_areas.to_metric(mp)
    lat, lon = _located(row)
    return _point(lat, lon) if lat is not None else None


def _incident_kind(row):
    for col in ("category2", "category1", "event"):
        text = _text(row.get(col)).strip()
        if text and text.lower() not in ("other", "met"):
            return text
    return "Incident"


def members_from(frames, agencies=None, weather_only=True):
    """Classify the snapshot's rows into members. Rows without a location are
    dropped — they cannot be placed in an area (they still count on the tiles).

    `frames` keys: incidents (VicEmergency incidents + warnings, already scoped
    to the event), roads (weather-caused, scoped), power, gauges, cells."""
    from app.pages import fire as fire_page
    out = []
    for row in weather_warnings(frames.get("incidents")):
        level = fire_page._kind(row)
        out.append(Member(
            id=str(row.get("source_id")), role="warning", kind=level,
            level=level, detail=_text(row.get("event")) or None,
            location=_text(row.get("location")) or _text(row.get("headline")) or "—",
            geom=_geom(row)))
    for row in selected_incidents(frames.get("incidents"), agencies, weather_only):
        lat, lon = _located(row)
        out.append(Member(
            id=str(row.get("source_id")), role="incident", kind=_incident_kind(row),
            location=_text(row.get("location")) or "—", latitude=lat, longitude=lon,
            detail=_text(row.get("status")) or None,
            started=_dt(row.get("created")) or _dt(row.get("first_seen")),
            # An incident's own polygon (rare) is only an outline: link on its point.
            geom=_point(lat, lon) if lat is not None else None))
    for row in _records(frames.get("roads")):
        closure = bool(pd.to_numeric(row.get("is_closure"), errors="coerce") or 0)
        out.append(Member(
            id=f"road:{row.get('source_id') or row.get('id')}", role="road",
            kind="Closure" if closure else "Disruption", rank=1 if closure else 2,
            location=_text(row.get("road_name")) or _text(row.get("location")) or "Road",
            detail=_text(row.get("disruption_type")) or None, geom=_geom(row)))
    for row in _records(frames.get("power")):
        out.append(Member(
            id=f"power:{row.get('location')}", role="power", kind="Power outage",
            location=_text(row.get("location")) or "Outage",
            value=float(pd.to_numeric(row.get("customers_off"), errors="coerce") or 0),
            geom=_geom(row, polygons=False)))
    for row in _records(frames.get("gauges")):
        pri = int(pd.to_numeric(row.get("priority"), errors="coerce") or 4)
        if pri > 3:
            continue
        out.append(Member(
            id=f"gauge:{row.get('station_name')}", role="gauge",
            kind={1: "Major", 2: "Moderate", 3: "Minor"}[pri], rank=pri,
            location=_text(row.get("station_name")) or "Gauge",
            value=float(pd.to_numeric(row.get("height_m"), errors="coerce") or 0),
            geom=_geom(row, polygons=False)))
    for row in _records(frames.get("cells")):
        cls = _text(row.get("classification"))
        if cls not in ("strong", "moderate"):
            continue
        out.append(Member(
            id=f"cell:{row.get('radar_id', '')}:{row.get('cell_id')}", role="cell",
            kind=f"{cls.capitalize()} storm cell", rank=1 if cls == "strong" else 2,
            location=f"Storm cell {row.get('cell_id')}",
            geom=_geom(row, polygons=False)))
    return [m for m in out if m.geom is not None]


def selected_incidents(df, agencies=None, weather_only=True):
    """The incident rows the wall counts: not warnings, not fires or burn areas,
    from the selected agencies, and (by default) weather-related only."""
    from app.modules.fire import data as fire_data
    agencies = DEFAULT_AGENCIES if agencies is None else agencies
    out = []
    for row in _records(df):
        if row.get("feed_type") in ("warning", "burn-area") or fire_data.is_fire(row):
            continue
        if fire_data.agency_of(row.get("source_org")) not in agencies:
            continue
        if weather_only and not is_weather_incident(row):
            continue
        out.append(row)
    return out


def weather_warnings(df):
    return [row for row in _records(df) if is_weather_warning(row)]


def warning_counts(snap):
    """Weather-related warnings per level (never summed into one figure);
    None per level when VicEmergency could not be read."""
    df = (snap.get("frames") or {}).get("incidents")
    counts = {"Emergency Warning": 0, "Watch and Act": 0, "Advice": 0}
    if df is None:
        return dict.fromkeys(counts)
    from app.pages import fire as fire_page
    for row in weather_warnings(df):
        kind = fire_page._kind(row)
        if kind in counts:
            counts[kind] += 1
    return counts


def _records(df):
    if df is None or getattr(df, "empty", True):
        return []
    return df.to_dict("records")


def build_areas(members, link_km=DEFAULT_LINK_KM, now=None):
    """Group members into areas of operation (see the module docstring)."""
    if not HAVE_SHAPELY:
        return []
    now = now or datetime.now()
    link_m = float(link_km) * 1000.0
    incidents = [m for m in members if m.role == "incident"]
    groups = []
    if incidents:
        tree = STRtree([m.geom for m in incidents])
        uf = fire_areas._Union(len(incidents))
        left, right = tree.query([m.geom for m in incidents], predicate="dwithin",
                                 distance=link_m)
        for a, b in zip(left, right):
            uf.union(int(a), int(b))
        index = {}
        for i, m in enumerate(incidents):
            root = uf.find(i)
            if root not in index:
                index[root] = len(groups)
                groups.append([])
            groups[index[root]].append(m)
        group_of = [index[uf.find(i)] for i in range(len(incidents))]
    else:
        tree, group_of = None, []

    # Anchors for context: every incident, plus each standalone warning area.
    anchors = [(m.geom, group_of[i]) for i, m in enumerate(incidents)]
    for m in members:
        if m.role != "warning":
            continue
        hits = (tree.query(m.geom, predicate="dwithin", distance=link_m)
                if tree is not None else [])
        joined = sorted({group_of[int(i)] for i in hits})
        for g in joined:
            groups[g].append(m)
        if not joined and m.level in ACT_LEVELS:
            groups.append([m])
            anchors.append((m.geom, len(groups) - 1))
    if not groups:
        return []

    anchor_tree = STRtree([g for g, _ in anchors])
    for m in members:
        if m.role in ("incident", "warning"):
            continue
        hits = anchor_tree.query(m.geom, predicate="dwithin", distance=link_m)
        if len(hits) == 0:
            continue
        best = min((int(i) for i in hits),
                   key=lambda i: m.geom.distance(anchors[i][0]))
        groups[anchors[best][1]].append(m)

    return sorted((_area(g, now) for g in groups),
                  key=lambda a: (a.level_rank, -len(a.of_role("incident")),
                                 -a.new_count, a.name.lower()))


def _place(location):
    """The locality of a feed location ('12 Smith St, BELGRAVE' -> 'Belgrave')."""
    text = _text(location).strip()
    if "," in text:
        text = text.rsplit(",", 1)[1].strip()
    return text.title() if text.isupper() else text


def _area(members, now):
    incidents = [m for m in members if m.role == "incident"]
    warnings = sorted((m for m in members if m.role == "warning"),
                      key=lambda m: WARNING_RANK.get(m.level, 9))
    seeds = incidents or warnings
    area = OperationArea(key=min(m.id for m in seeds), name="", members=members)
    area.level = warnings[0].level if warnings else None
    if incidents:
        places = Counter(_place(m.location) for m in incidents if m.location != "—")
        # Most incidents first; ties keep the first-seen order.
        area.places = [p for p, _ in places.most_common()]
        started = [m.started for m in incidents if m.started]
        area.newest_at = max(started) if started else None
        cutoff = now - timedelta(minutes=NEW_MINUTES)
        area.new_count = sum(1 for t in started if t >= cutoff)
        area.name = area.places[0] if area.places else incidents[0].location
    else:
        area.name = warnings[0].location
    # Focus on where the work is (incidents and context), not on a warning
    # polygon that can span a whole catchment.
    core = [m.geom for m in members if m.role != "warning"] or [m.geom for m in seeds]
    hull = shapely.union_all(core).convex_hull.buffer(1500)
    area.outline = fire_areas.to_geojson(hull)
    minx, miny, maxx, maxy = hull.bounds
    lonlat = fire_areas._unproject(np.array([[minx, miny], [maxx, maxy]], dtype=float))
    area.center, area.zoom = fire_areas.focus_view(
        (lonlat[0][0], lonlat[0][1], lonlat[1][0], lonlat[1][1]), max_zoom=13.0)
    return area


def area_for_member(areas, member_id):
    return next((a for a in areas if member_id in a.member_ids()), None)


# --------------------------------------------------------------------------- #
# Snapshot (cached for every viewer with the same options)
# --------------------------------------------------------------------------- #
def scope(frames, tag=None):
    """Narrow raw frames to what the wall counts: weather-caused road
    disruptions, and — with an event chosen — only incidents and roads that
    STARTED during it (warnings are never scoped: a warning still current is
    current whenever it was issued)."""
    from app.modules.roads import data as roads_data
    out = dict(frames)
    inc = frames.get("incidents")
    roads = frames.get("roads")
    if roads is not None and not roads.empty:
        roads = roads_data.filter_causes(roads, ROAD_CAUSES)
    if tag is not None:
        start, end = tag["start_ts"], tag.get("end_ts")
        if roads is not None and not roads.empty:
            roads = roads_data.filter_since(roads, start, end)
        if inc is not None and not inc.empty:
            is_warn = inc["feed_type"] == "warning"
            began = roads_data.filter_since(inc[~is_warn], start, end)
            inc = pd.concat([inc[is_warn], began])
    out["roads"], out["incidents"] = roads, inc
    return out


def _fresh_gauges(df, now):
    """map_gauges() carries no timestamp, so staleness is checked here."""
    if df is None or df.empty:
        return df
    from app import database
    recent = database.read_df(
        "SELECT DISTINCT station_name FROM flood_observations WHERE timestamp >= ?",
        [(now - timedelta(hours=GAUGE_STALE_HOURS)).strftime("%Y-%m-%d %H:%M:%S")])
    names = set(recent["station_name"]) if not recent.empty else set()
    return df[df["station_name"].isin(names)]


def load_frames(now=None):
    """Every source the storm wall reads, each isolated: one unreadable source
    is None (shown as "—"), never a broken wall."""
    from app.modules.fire import data as fire_data
    from app.modules.flood import data as flood_data
    from app.modules.power import data as power_data
    from app.modules.roads import data as roads_data
    from app.modules.storm import data as storm_data
    now = now or datetime.now()

    def safe(fn, what):
        try:
            return fn()
        except Exception:
            log.exception("Storm wall: %s unavailable", what)
            return None

    gauges = safe(flood_data.map_gauges, "flood gauges")
    if gauges is not None and not gauges.empty:
        gauges = gauges[pd.to_numeric(gauges["priority"], errors="coerce") <= 3]
        gauges = safe(lambda: _fresh_gauges(gauges, now), "gauge freshness")
    return {
        "incidents": safe(fire_data.active_incidents, "VicEmergency"),
        "roads": safe(roads_data.active_disruptions, "road disruptions"),
        "power": safe(power_data.active_outages, "power outages"),
        "gauges": gauges,
        "cells": safe(storm_data.active_cells, "storm cells"),
    }


def assemble(snap, frames):
    """Fill a snapshot from frames. Shared by the live snapshot and the test
    run (`app.storm_demo`)."""
    frames = scope(frames, snap.get("tag"))
    members = members_from(frames, snap["agencies"], snap["weather_only"])
    snap.update(frames=frames,
                areas=build_areas(members, snap["link_km"], snap["at"]))
    return snap


def new_snapshot(now, link_km, agencies, weather_only, tag):
    return {"at": now, "ok": True, "error": None, "areas": [], "frames": {},
            "link_km": link_km, "agencies": list(agencies),
            "weather_only": bool(weather_only), "tag": tag}


def compute(now=None, link_km=DEFAULT_LINK_KM, agencies=None, weather_only=True,
            tag=None):
    now = now or datetime.now()
    agencies = DEFAULT_AGENCIES if agencies is None else agencies
    snap = new_snapshot(now, link_km, agencies, weather_only, tag)
    if not HAVE_SHAPELY:
        snap.update(ok=False, error="shapely is not installed")
        return snap
    try:
        assemble(snap, load_frames(now))
    except Exception as e:
        log.exception("Storm wall: areas of operation unavailable")
        snap.update(ok=False, error=str(e))
    return snap


_lock = threading.Lock()
_cache = {}


def normalise_options(link_km, agencies, weather_only):
    link_km = link_km if link_km in LINK_KM_CHOICES else DEFAULT_LINK_KM
    agencies = DEFAULT_AGENCIES if agencies is None else [
        a for a in AGENCY_KEYS if a in agencies]
    return link_km, agencies, bool(weather_only)


def current(link_km=DEFAULT_LINK_KM, agencies=None, weather_only=True, tag=None,
            max_age=CACHE_SECONDS):
    """Cached snapshot shared by every open storm wall with the same options."""
    link_km, agencies, weather_only = normalise_options(link_km, agencies,
                                                        weather_only)
    key = (link_km, tuple(agencies), weather_only,
           tag["id"] if tag else None,
           str(tag.get("start_ts")) if tag else None,
           str(tag.get("end_ts")) if tag else None)
    with _lock:
        hit = _cache.get(key)
        if hit is None or time.monotonic() - hit[0] >= max_age:
            hit = (time.monotonic(), compute(link_km=link_km, agencies=agencies,
                                             weather_only=weather_only, tag=tag))
            _cache[key] = hit
            for old in [k for k, v in _cache.items()
                        if time.monotonic() - v[0] > 10 * max_age]:
                _cache.pop(old, None)
        return hit[1]


def reset_cache():
    with _lock:
        _cache.clear()


# --------------------------------------------------------------------------- #
# What is new (per browser, like the fire and flood walls)
# --------------------------------------------------------------------------- #
def size_step(n):
    """The highest SIZE_STEPS threshold `n` incidents has reached (0 = none)."""
    return max([s for s in SIZE_STEPS if n >= s], default=0)


def events(snap):
    """Takeover-worthy facts, each with a stable key: a Watch and Act /
    Emergency Warning in an area (keyed by level, so an escalation is new), and
    an area's incident count reaching the next of SIZE_STEPS. A single new job
    is not one — in a storm that would be a takeover every minute."""
    out, done = [], set()
    for area in snap.get("areas") or []:
        for m in area.warnings:
            if m.level in ACT_LEVELS and m.id not in done:
                done.add(m.id)
                out.append({"key": f"warn:{m.id}:{m.level}", "member": m.id,
                            "kind": "warning", "level": m.level,
                            "text": f"{m.level} — {m.location}"})
        n = len(area.of_role("incident"))
        step = size_step(n)
        if step:
            # The oldest incident is a stable anchor while the area grows.
            lead = min(area.of_role("incident"),
                       key=lambda m: (m.started or datetime.max, m.id)).id
            out.append({"key": f"size:{lead}:{step}", "member": lead,
                        "kind": "size", "count": n,
                        "text": f"{area.name} — {n} incidents"})
    return out


detect_new = fire_areas.detect_new

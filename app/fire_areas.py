# Passive Monitor — Copyright (c) 2026 SirTophamMatt. All rights reserved.
"""Fire areas of operation and burn-area change — the fire wall's model.

UI-free (like `briefing` / `replay`): the `/wall/fire` page renders from it.

Two jobs:

* **Burn-area change.** `record()` runs inside the fire collector and keeps a
  change-only history of every fire polygon (a Fire incident's own area, or a
  VicEmergency `burn-area` feature) in `fire_area_history`. `area_changes()`
  diffs the newest shape against an earlier one: area that is burnt NOW but was
  not before is GROWTH, area that was and no longer is is a REDUCTION (a
  perimeter re-mapped smaller). Both over the selected window, plus the latest
  single step, which is what the wall makes breathe.
* **Areas of operation.** `build_areas()` links fire incidents, fire warnings
  and burn areas that intersect or sit within `link_km` of each other into one
  area, then attaches every other incident / warning that is that close as
  context. The wall focuses its map on one area at a time.

Polygon maths uses shapely in a local metric projection (equirectangular about
Victoria). Within the state that is accurate to a couple of percent for area,
which is the right order for "how much did it grow", and it keeps the
projection affine — so a difference taken in metres is exactly the difference
in degrees, mapped back without distortion.

**It starts when it starts.** A polygon's first recorded shape is its baseline;
growth is only ever reported between two shapes this module actually saw.
"""
import hashlib
import json
import logging
import math
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from app import database

log = logging.getLogger(__name__)

try:                                # required in requirements.txt; guarded so a
    import shapely                  # build without it degrades to "unavailable"
    from shapely import STRtree
    from shapely.geometry import mapping, shape
    HAVE_SHAPELY = True
except ImportError:                 # pragma: no cover - environment dependent
    shapely = STRtree = mapping = shape = None
    HAVE_SHAPELY = False

# --------------------------------------------------------------------------- #
# Tunables
# --------------------------------------------------------------------------- #
LINK_KM_CHOICES = [2, 5, 10, 20]
DEFAULT_LINK_KM = 5
WINDOW_HOURS_CHOICES = [1, 3, 6, 12, 24]
DEFAULT_WINDOW_HOURS = 6
# A change smaller than this (per part) is re-digitising noise, not growth.
MIN_CHANGE_HA = 0.5
# Slivers thinner than 2 x this are removed from a change before it is drawn:
# a perimeter redrawn a few metres to one side is not a new front.
SLIVER_M = 10.0
# A burn-area feature counts as current when the feed dates it within this
# many days (older ones are the historical DELWP footprints, see CLAUDE.md).
BURN_AREA_RECENT_DAYS = 7
CACHE_SECONDS = 45

WARNING_RANK = {"Emergency Warning": 1, "Watch and Act": 2, "Advice": 3}

# Local metric projection about Victoria.
LAT0, LON0 = -37.0, 145.0
M_PER_DEG_LAT = 110_574.0
M_PER_DEG_LON = 111_320.0 * math.cos(math.radians(LAT0))

_TS = "%Y-%m-%d %H:%M:%S"


# --------------------------------------------------------------------------- #
# GeoJSON helpers (no shapely needed)
# --------------------------------------------------------------------------- #
def polygon_parts(geom):
    """Every polygon (list of rings) inside a GeoJSON geometry — Polygon,
    MultiPolygon or a GeometryCollection, which is how VicEmergency wraps an
    incident's area beside its point."""
    if not isinstance(geom, dict):
        return []
    kind = geom.get("type")
    if kind == "Polygon":
        return [geom.get("coordinates") or []]
    if kind == "MultiPolygon":
        return list(geom.get("coordinates") or [])
    if kind == "GeometryCollection":
        out = []
        for g in geom.get("geometries") or []:
            out += polygon_parts(g)
        return out
    return []


def polygons_only(geometry):
    """A stored geometry (string or dict) reduced to a MultiPolygon dict, or
    None when it has no area."""
    if isinstance(geometry, str):
        try:
            geometry = json.loads(geometry)
        except (ValueError, TypeError):
            return None
    parts = [p for p in polygon_parts(geometry) if p and p[0]]
    if not parts:
        return None
    return {"type": "MultiPolygon", "coordinates": parts}


def geom_hash(multipolygon):
    text = json.dumps(multipolygon, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def _ring_area_m2(ring, cos_lat):
    pts = [(float(p[0]) * 111_320.0 * cos_lat, float(p[1]) * M_PER_DEG_LAT)
           for p in ring if len(p) >= 2]
    if len(pts) < 3:
        return 0.0
    total = 0.0
    for (x1, y1), (x2, y2) in zip(pts, pts[1:] + pts[:1]):
        total += x1 * y2 - x2 * y1
    return abs(total) / 2.0


def area_ha(multipolygon):
    """Area of a MultiPolygon dict in hectares (outer rings minus holes),
    projected about its own mean latitude."""
    if not multipolygon:
        return 0.0
    lats = [p[1] for poly in multipolygon["coordinates"] for ring in poly[:1]
            for p in ring if len(p) >= 2]
    if not lats:
        return 0.0
    cos_lat = math.cos(math.radians(sum(lats) / len(lats)))
    m2 = 0.0
    for poly in multipolygon["coordinates"]:
        if not poly:
            continue
        m2 += _ring_area_m2(poly[0], cos_lat)
        m2 -= sum(_ring_area_m2(hole, cos_lat) for hole in poly[1:])
    return max(0.0, m2) / 10_000.0


def _stamp(value):
    if isinstance(value, datetime):
        return value.strftime(_TS)
    return str(value)


def _dt(value):
    parsed = pd.to_datetime(value, errors="coerce")
    return None if pd.isna(parsed) else parsed.to_pydatetime()


# --------------------------------------------------------------------------- #
# Recording (called by the fire collector)
# --------------------------------------------------------------------------- #
_FIRE_POLYGON_SQL = (
    "SELECT source_id, geometry FROM fire_incidents "
    "WHERE last_seen = ? AND resolved = 0 AND geometry IS NOT NULL "
    "AND (feed_type = 'burn-area' "
    "     OR (COALESCE(feed_type, '') != 'warning' "
    "         AND LOWER(TRIM(COALESCE(category1, ''))) = 'fire'))")


def _last_hashes(ids):
    """Newest recorded hash per polygon, in one query (never one per id)."""
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    df = database.read_df(
        "SELECT source_id, geom_hash FROM ("
        "  SELECT source_id, geom_hash, ROW_NUMBER() OVER ("
        "    PARTITION BY source_id ORDER BY recorded_at DESC, id DESC) AS rn"
        f"  FROM fire_area_history WHERE source_id IN ({marks})"
        ") WHERE rn = 1", list(ids))
    return dict(zip(df["source_id"], df["geom_hash"]))


def record(now):
    """Write a history row for every fire polygon seen this cycle whose shape
    differs from its last recorded shape. Returns the rows written."""
    df = database.read_df(_FIRE_POLYGON_SQL, [_stamp(now)])
    if df.empty:
        return 0
    known = _last_hashes([str(s) for s in df["source_id"]])
    rows = []
    for source_id, geometry in zip(df["source_id"], df["geometry"]):
        mp = polygons_only(geometry)
        if mp is None:
            continue
        digest = geom_hash(mp)
        if known.get(str(source_id)) == digest:
            continue
        rows.append({"source_id": str(source_id), "recorded_at": _stamp(now),
                     "geometry": json.dumps(mp, separators=(",", ":")),
                     "geom_hash": digest, "area_ha": round(area_ha(mp), 2)})
    if rows:
        database.insert_rows("fire_area_history", rows, ignore_duplicates=True)
    return len(rows)


# --------------------------------------------------------------------------- #
# Shapely helpers
# --------------------------------------------------------------------------- #
def _project(coords):
    out = coords.copy()
    out[:, 0] = (coords[:, 0] - LON0) * M_PER_DEG_LON
    out[:, 1] = (coords[:, 1] - LAT0) * M_PER_DEG_LAT
    return out


def _unproject(coords):
    out = coords.copy()
    out[:, 0] = coords[:, 0] / M_PER_DEG_LON + LON0
    out[:, 1] = coords[:, 1] / M_PER_DEG_LAT + LAT0
    return out


def to_metric(geojson):
    """GeoJSON dict -> valid shapely geometry in metres, or None."""
    if not geojson:
        return None
    try:
        geom = shapely.transform(shape(geojson), _project)
        if not geom.is_valid:
            geom = shapely.make_valid(geom)
        return None if geom.is_empty else geom
    except Exception:
        return None


def to_geojson(geom):
    """Metric shapely geometry -> lon/lat GeoJSON dict."""
    return mapping(shapely.transform(geom, _unproject))


def metric_ha(geom):
    """Hectares of a metric geometry, corrected from the projection's
    reference latitude to the geometry's own."""
    if geom is None or geom.is_empty:
        return 0.0
    lat = geom.centroid.y / M_PER_DEG_LAT + LAT0
    factor = math.cos(math.radians(lat)) / math.cos(math.radians(LAT0))
    return geom.area * factor / 10_000.0


def _areal(geom):
    """Only the polygonal part of a geometry (make_valid / difference can leave
    stray lines and points)."""
    if geom is None or geom.is_empty:
        return None
    if geom.geom_type in ("Polygon", "MultiPolygon"):
        return geom
    if hasattr(geom, "geoms"):
        polys = [g for g in geom.geoms if g.geom_type in ("Polygon", "MultiPolygon")]
        if polys:
            return shapely.union_all(polys)
    return None


def changed_area(after, before):
    """What is in `after` but not `before`, with slivers and specks removed.
    Returns (metric geometry or None, hectares)."""
    if after is None:
        return None, 0.0
    diff = after if before is None else after.difference(before)
    diff = _areal(diff)
    if diff is None:
        return None, 0.0
    # Morphological opening: drops parts thinner than 2 x SLIVER_M.
    diff = _areal(diff.buffer(-SLIVER_M).buffer(SLIVER_M).intersection(diff))
    if diff is None:
        return None, 0.0
    parts = list(diff.geoms) if hasattr(diff, "geoms") else [diff]
    kept = [p for p in parts if metric_ha(p) >= MIN_CHANGE_HA]
    if not kept:
        return None, 0.0
    out = shapely.union_all(kept)
    return out, metric_ha(out)


# --------------------------------------------------------------------------- #
# Burn-area change
# --------------------------------------------------------------------------- #
@dataclass
class AreaChange:
    """One fire polygon's shape now, and how it moved inside the window."""
    source_id: str
    area_ha: float
    first_recorded: datetime
    geometry: dict                  # current polygons (lon/lat MultiPolygon)
    # Window: newest shape vs the shape at the window's start (or the first
    # shape seen, when the polygon appeared inside the window).
    grown: dict = None
    grown_ha: float = 0.0
    reduced: dict = None
    reduced_ha: float = 0.0
    # Latest single step (newest shape vs the one before it), only when that
    # step happened inside the window. This is what breathes.
    step_at: datetime = None
    step_grown: dict = None
    step_grown_ha: float = 0.0
    step_reduced: dict = None
    step_reduced_ha: float = 0.0
    shapes: int = 1                 # shapes recorded inside the window + baseline

    @property
    def changed(self):
        return bool(self.grown_ha or self.reduced_ha)


def _history(ids, cutoff):
    """The last shape before `cutoff` plus every shape since, per polygon."""
    marks = ",".join("?" * len(ids))
    stamp = _stamp(cutoff)
    return database.read_df(
        "SELECT source_id, recorded_at, geometry, area_ha FROM ("
        "  SELECT source_id, recorded_at, geometry, area_ha, id, ROW_NUMBER() OVER ("
        "    PARTITION BY source_id ORDER BY recorded_at DESC, id DESC) AS rn"
        f"  FROM fire_area_history WHERE source_id IN ({marks}) AND recorded_at < ?"
        ") WHERE rn = 1 "
        "UNION ALL "
        "SELECT source_id, recorded_at, geometry, area_ha FROM fire_area_history "
        f"WHERE source_id IN ({marks}) AND recorded_at >= ? "
        "ORDER BY source_id, recorded_at",
        [*ids, stamp, *ids, stamp])


def area_changes(source_ids, now=None, window_hours=DEFAULT_WINDOW_HOURS):
    """{source_id: AreaChange} for every polygon with recorded history."""
    ids = sorted({str(s) for s in source_ids if s is not None})
    if not ids or not HAVE_SHAPELY:
        return {}
    now = now or datetime.now()
    cutoff = now - timedelta(hours=window_hours)
    df = _history(ids, cutoff)
    out = {}
    for source_id, rows in df.groupby("source_id", sort=False):
        rows = rows.sort_values("recorded_at")
        try:
            out[str(source_id)] = _change_for(str(source_id), rows, cutoff)
        except Exception:
            log.exception("Fire areas: change for %s failed", source_id)
    return out


def _change_for(source_id, rows, cutoff):
    recs = list(rows.itertuples(index=False))
    newest = recs[-1]
    current_json = json.loads(newest.geometry)
    current = to_metric(current_json)
    change = AreaChange(
        source_id=source_id, area_ha=float(newest.area_ha or 0.0),
        first_recorded=_dt(recs[0].recorded_at), geometry=current_json,
        shapes=len(recs))
    if len(recs) < 2 or current is None:
        return change
    baseline = to_metric(json.loads(recs[0].geometry))
    grown, change.grown_ha = changed_area(current, baseline)
    reduced, change.reduced_ha = changed_area(baseline, current)
    change.grown = to_geojson(grown) if grown is not None else None
    change.reduced = to_geojson(reduced) if reduced is not None else None

    step_at = _dt(newest.recorded_at)
    if step_at is not None and step_at >= cutoff:
        prev = to_metric(json.loads(recs[-2].geometry))
        sg, change.step_grown_ha = changed_area(current, prev)
        sr, change.step_reduced_ha = changed_area(prev, current)
        change.step_grown = to_geojson(sg) if sg is not None else None
        change.step_reduced = to_geojson(sr) if sr is not None else None
        if change.step_grown_ha or change.step_reduced_ha:
            change.step_at = step_at
    return change


# --------------------------------------------------------------------------- #
# Areas of operation
# --------------------------------------------------------------------------- #
@dataclass
class Member:
    id: str
    role: str                 # fire | warning | burn | other
    kind: str                 # fire._kind (Fire / a warning level / ...) or "Burn area"
    location: str
    latitude: float = None
    longitude: float = None
    level: str = None         # warnings only
    status: str = None
    size: str = None
    fire_related: bool = False
    geom: object = None       # metric shapely geometry (polygon or point)


@dataclass
class OperationArea:
    key: str
    name: str
    members: list = field(default_factory=list)
    level: str = None         # most severe warning level in the area
    area_ha: float = 0.0      # burnt area now (union of its fire polygons)
    grown_ha: float = 0.0     # inside the window
    reduced_ha: float = 0.0
    step_at: datetime = None  # newest shape change in the area
    center: dict = None
    zoom: float = 9.0
    outline: dict = None      # lon/lat GeoJSON of the area's hull

    def of_role(self, role):
        return [m for m in self.members if m.role == role]

    @property
    def fires(self):
        return self.of_role("fire")

    @property
    def warnings(self):
        return sorted(self.of_role("warning"),
                      key=lambda m: WARNING_RANK.get(m.level, 9))

    @property
    def others(self):
        return self.of_role("other")

    @property
    def level_rank(self):
        return WARNING_RANK.get(self.level, 9)

    def member_ids(self):
        return {m.id for m in self.members}


def _text(value):
    if value is None or value != value:            # None / NaN
        return ""
    return str(value)


def is_fire_warning(row):
    """A warning about a fire (or a burn): by its hazard text, since the
    VicEmergency feed has no single hazard field shared by every warning."""
    text = " ".join(_text(row.get(c)) for c in
                    ("event", "category2", "headline", "category1")).lower()
    return "fire" in text or "burn" in text


def _member(row, kind, role, fire_related):
    geom = None
    mp = polygons_only(row.get("geometry"))
    if mp is not None:
        geom = to_metric(mp)
    lat, lon = row.get("latitude"), row.get("longitude")
    located = lat is not None and lon is not None and lat == lat and lon == lon
    if geom is None and located:
        geom = shapely.Point((float(lon) - LON0) * M_PER_DEG_LON,
                             (float(lat) - LAT0) * M_PER_DEG_LAT)
    return Member(
        id=str(row.get("source_id")), role=role, kind=kind,
        location=_text(row.get("location")) or _text(row.get("headline")) or "—",
        latitude=float(lat) if located else None,
        longitude=float(lon) if located else None,
        level=kind if role == "warning" else None,
        status=_text(row.get("status")) or None, size=_text(row.get("size")) or None,
        fire_related=fire_related, geom=geom)


def members_from(incidents, burn_areas=None, changes=None, now=None,
                 window_hours=DEFAULT_WINDOW_HOURS):
    """Classify active VicEmergency rows (+ current burn-area features) into
    linkable members. Rows without any location are dropped — they cannot be
    placed in an area."""
    from app.pages import fire as fire_page
    changes = changes or {}
    out = []
    if incidents is not None and not incidents.empty:
        for row in incidents.to_dict("records"):
            kind = fire_page._kind(row)
            if kind in fire_page.WARNING_KINDS:
                out.append(_member(row, kind, "warning", is_fire_warning(row)))
            elif kind == "Fire":
                out.append(_member(row, kind, "fire", True))
            else:
                out.append(_member(row, kind, "other", False))
    if burn_areas is not None and not burn_areas.empty:
        now = now or datetime.now()
        recent = now - timedelta(days=BURN_AREA_RECENT_DAYS)
        changed_since = now - timedelta(hours=window_hours)
        for row in burn_areas.to_dict("records"):
            dated = max([d for d in (_dt(row.get("updated")), _dt(row.get("created")))
                         if d is not None], default=None)
            ch = changes.get(str(row.get("source_id")))
            moving = ch is not None and ch.step_at is not None \
                and ch.step_at >= changed_since
            if not moving and (dated is None or dated < recent):
                continue                       # a historical footprint
            # A burn area whose shape is moving seeds an area on its own; a
            # recent but static one only joins an area a fire already made.
            out.append(_member(row, "Burn area", "burn", moving))
    return [m for m in out if m.geom is not None]


class _Union:
    def __init__(self, n):
        self.parent = list(range(n))

    def find(self, i):
        while self.parent[i] != i:
            self.parent[i] = self.parent[self.parent[i]]
            i = self.parent[i]
        return i

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[max(ra, rb)] = min(ra, rb)


def focus_view(bounds, width_px=1100, height_px=700, pad=1.35,
               min_zoom=6.0, max_zoom=12.0):
    """(center, zoom) that fits lon/lat bounds (minx, miny, maxx, maxy) in a
    map of roughly the given size. MapLibre's world is 512 px wide at zoom 0."""
    minx, miny, maxx, maxy = bounds
    lat_c = (miny + maxy) / 2.0
    span_lon = max(maxx - minx, 0.02) * pad
    span_lat = max(maxy - miny, 0.02) * pad
    z_lon = math.log2(360.0 * width_px / (512.0 * span_lon))
    z_lat = math.log2(360.0 * height_px * math.cos(math.radians(lat_c))
                      / (512.0 * span_lat))
    zoom = max(min_zoom, min(max_zoom, min(z_lon, z_lat)))
    return ({"lat": float(lat_c), "lon": float((minx + maxx) / 2.0)},
            round(float(zoom), 2))


def build_areas(members, changes=None, link_km=DEFAULT_LINK_KM):
    """Group members into areas of operation.

    Fire-related members (fire incidents, fire warnings, moving burn areas)
    that intersect or lie within `link_km` of each other are one area — a
    warning polygon covering two fires joins them. Every other member (other
    incidents, non-fire warnings, static burn areas) is attached to the
    nearest area it is that close to, but never links two areas itself: a line
    of fallen trees must not chain two fires 40 km apart into one.
    """
    if not HAVE_SHAPELY:
        return []
    changes = changes or {}
    link_m = float(link_km) * 1000.0
    seeds = [m for m in members if m.fire_related]
    if not seeds:
        return []
    tree = STRtree([m.geom for m in seeds])
    uf = _Union(len(seeds))
    left, right = tree.query([m.geom for m in seeds], predicate="dwithin",
                             distance=link_m)
    for a, b in zip(left, right):
        uf.union(int(a), int(b))
    groups = {}
    for i, m in enumerate(seeds):
        groups.setdefault(uf.find(i), []).append(m)

    group_of = {i: uf.find(i) for i in range(len(seeds))}
    for m in members:
        if m.fire_related:
            continue
        hits = tree.query(m.geom, predicate="dwithin", distance=link_m)
        if len(hits) == 0:
            continue
        nearest = min(hits, key=lambda i: m.geom.distance(seeds[int(i)].geom))
        groups[group_of[int(nearest)]].append(m)

    return sorted((_area(g, changes) for g in groups.values()),
                  key=lambda a: (a.level_rank, -a.grown_ha, -a.area_ha,
                                 a.name.lower()))


def _area(members, changes):
    fires = [m for m in members if m.role == "fire"]
    burns = [m for m in members if m.role in ("fire", "burn")]
    key = min(m.id for m in (fires or members))
    area = OperationArea(key=key, name="", members=members)
    ranked = sorted((m for m in members if m.role == "warning"),
                    key=lambda m: WARNING_RANK.get(m.level, 9))
    area.level = ranked[0].level if ranked else None

    polys = [m.geom for m in burns if m.geom is not None
             and m.geom.geom_type in ("Polygon", "MultiPolygon")]
    if polys:
        area.area_ha = metric_ha(shapely.union_all(polys))
    for m in burns:
        ch = changes.get(m.id)
        if ch is None:
            continue
        area.grown_ha += ch.grown_ha
        area.reduced_ha += ch.reduced_ha
        if ch.step_at and (area.step_at is None or ch.step_at > area.step_at):
            area.step_at = ch.step_at

    # Name: the largest fire, else the first fire, else the worst warning.
    def size(m):
        ch = changes.get(m.id)
        return ch.area_ha if ch else 0.0
    lead = (max(fires, key=size) if fires else (ranked or members)[0])
    others = len(fires) - 1 if fires else 0
    area.name = lead.location + (f" +{others} fire{'s' if others != 1 else ''}"
                                 if others > 0 else "")

    hull = shapely.union_all([m.geom for m in members]).convex_hull.buffer(1000)
    area.outline = to_geojson(hull)
    minx, miny, maxx, maxy = hull.bounds
    lonlat = _unproject(np.array([[minx, miny], [maxx, maxy]], dtype=float))
    area.center, area.zoom = focus_view((lonlat[0][0], lonlat[0][1],
                                         lonlat[1][0], lonlat[1][1]))
    return area


def area_for_member(areas, member_id):
    return next((a for a in areas if member_id in a.member_ids()), None)


# --------------------------------------------------------------------------- #
# Snapshot (cached for every viewer)
# --------------------------------------------------------------------------- #
def _burn_area_rows():
    return database.read_df(
        "SELECT source_id, feed_type, location, headline, latitude, longitude, "
        "geometry, created, updated, status, size FROM fire_incidents "
        "WHERE feed_type = 'burn-area' AND resolved = 0 AND geometry IS NOT NULL")


def compute(now=None, link_km=DEFAULT_LINK_KM, window_hours=DEFAULT_WINDOW_HOURS):
    """Everything the fire wall draws, from stored data only."""
    from app.modules.fire import data as fire_data
    now = now or datetime.now()
    snap = {"at": now, "ok": True, "error": None, "areas": [], "changes": {},
            "incidents": pd.DataFrame(), "burn": pd.DataFrame(),
            "link_km": link_km, "window_hours": window_hours}
    if not HAVE_SHAPELY:
        snap.update(ok=False, error="shapely is not installed")
        return snap
    try:
        incidents = fire_data.active_incidents()
        burn = _burn_area_rows()
        fire_ids = []
        if not incidents.empty:
            from app.pages import fire as fire_page
            kinds = incidents.apply(fire_page._kind, axis=1)
            fire_ids = incidents.loc[kinds == "Fire", "source_id"].tolist()
        ids = fire_ids + (burn["source_id"].tolist() if not burn.empty else [])
        changes = area_changes(ids, now, window_hours)
        members = members_from(incidents, burn, changes, now, window_hours)
        snap.update(incidents=incidents, burn=burn, changes=changes,
                    areas=build_areas(members, changes, link_km))
    except Exception as e:
        log.exception("Fire wall: areas of operation unavailable")
        snap.update(ok=False, error=str(e))
    return snap


_lock = threading.Lock()
_cache = {}


def current(link_km=DEFAULT_LINK_KM, window_hours=DEFAULT_WINDOW_HOURS,
            max_age=CACHE_SECONDS):
    """Cached snapshot shared by every open fire wall with the same options."""
    link_km = link_km if link_km in LINK_KM_CHOICES else DEFAULT_LINK_KM
    window_hours = (window_hours if window_hours in WINDOW_HOURS_CHOICES
                    else DEFAULT_WINDOW_HOURS)
    key = (link_km, window_hours)
    with _lock:
        hit = _cache.get(key)
        if hit is None or time.monotonic() - hit[0] >= max_age:
            hit = (time.monotonic(), compute(link_km=link_km,
                                             window_hours=window_hours))
            _cache[key] = hit
        return hit[1]


def reset_cache():
    with _lock:
        _cache.clear()


# --------------------------------------------------------------------------- #
# What is new (per browser, like the flood wall and the alert sounds)
# --------------------------------------------------------------------------- #
def events(snap):
    """Takeover-worthy facts, each with a stable key: a burn-area step that
    grew, and a Watch and Act / Emergency Warning inside an area (keyed by its
    level, so an escalation is a new key). A new small fire on its own is not
    one — in summer that would be a takeover every few minutes."""
    out = []
    for area in snap.get("areas") or []:
        for m in area.members:
            ch = snap["changes"].get(m.id)
            if m.role in ("fire", "burn") and ch and ch.step_at \
                    and ch.step_grown_ha >= MIN_CHANGE_HA:
                out.append({"key": f"grow:{m.id}:{ch.step_at:%Y%m%d%H%M%S}",
                            "member": m.id, "kind": "growth",
                            "text": f"{m.location} burn area +{ch.step_grown_ha:,.0f} ha"})
            if m.role == "warning" and m.level in ("Emergency Warning",
                                                   "Watch and Act"):
                out.append({"key": f"warn:{m.id}:{m.level}", "member": m.id,
                            "kind": "warning", "level": m.level,
                            "text": f"{m.level} — {m.location}"})
    return out


def detect_new(seen, current_events):
    """(seen_keys, fresh). The first snapshot (seen None) only seeds, so
    opening the wall mid-event replays nothing."""
    keys = [e["key"] for e in current_events]
    if seen is None:
        return keys, []
    known = set(seen)
    fresh = [e for e in current_events if e["key"] not in known]
    # Keep only keys still live: an event that has left and comes back (a
    # warning downgraded and re-escalated) is news again.
    return keys, fresh

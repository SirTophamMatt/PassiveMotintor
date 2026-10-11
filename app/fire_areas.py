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
* **Areas of operation.** `build_areas()` makes one area per fire incident
  (incidents are never grouped), attaching the fire warnings, burn areas and
  other incidents / warnings within `link_km` of it. The wall focuses its map on one area at a time.

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
# A burn-area polygon that first appears inside the window is compared with the
# polygon it replaced (a re-issue under a new feed id) when that one was last
# seen at most this long before the window opened.
PREDECESSOR_LOOKBACK_HOURS = 24
# A change in a fire's REPORTED size (the feed's sizeFmt) takes over the wall
# only when it grows at least this much — the Intelligence Feed's own floor
# (config intel.fire_min_growth_ha / _pct), so the two agree on "significant".
SIZE_TAKEOVER_MIN_HA = 5.0
SIZE_TAKEOVER_MIN_PCT = 10.0

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
    predecessor: str = None         # the re-issued polygon this one replaced

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
    out, fresh = {}, {}
    for source_id, rows in df.groupby("source_id", sort=False):
        rows = rows.sort_values("recorded_at")
        try:
            change = _change_for(str(source_id), rows, cutoff)
        except Exception:
            log.exception("Fire areas: change for %s failed", source_id)
            continue
        out[str(source_id)] = change
        if (change.shapes == 1 and change.first_recorded is not None
                and change.first_recorded >= cutoff):
            fresh[str(source_id)] = rows
    if fresh:
        try:
            _link_predecessors(out, fresh, ids, cutoff)
        except Exception:
            log.exception("Fire areas: predecessor matching failed")
    return out


def _link_predecessors(out, fresh, ids, cutoff):
    """A polygon first seen inside the window may be a RE-ISSUE: the feed
    replacing a fire's mapped area under a new id. Diffed only against itself
    it is just a baseline and its growth is invisible, so compare it with the
    polygon it replaced — one no longer current, last recorded before the new
    one appeared, that overlaps it most."""
    marks = ",".join("?" * len(ids))
    since = cutoff - timedelta(hours=PREDECESSOR_LOOKBACK_HOURS)
    cands = database.read_df(
        "SELECT source_id, recorded_at, geometry, area_ha FROM ("
        "  SELECT source_id, recorded_at, geometry, area_ha, ROW_NUMBER() OVER ("
        "    PARTITION BY source_id ORDER BY recorded_at DESC, id DESC) AS rn"
        f"  FROM fire_area_history WHERE source_id NOT IN ({marks})"
        "   AND recorded_at >= ?) WHERE rn = 1",
        [*ids, _stamp(since)])
    if cands.empty:
        return
    shapes = [(r, to_metric(json.loads(r.geometry)))
              for r in cands.itertuples(index=False)]
    shapes = [(r, g) for r, g in shapes if g is not None]
    claimed = set()
    for source_id, rows in fresh.items():
        change = out[source_id]
        current = to_metric(change.geometry)
        if current is None:
            continue
        best, best_overlap = None, 0.0
        for r, geom in shapes:
            if r.source_id in claimed or _dt(r.recorded_at) is None \
                    or _dt(r.recorded_at) > change.first_recorded:
                continue
            overlap = current.intersection(geom).area if current.intersects(geom) else 0.0
            if overlap > best_overlap:
                best, best_overlap = r, overlap
        if best is None:
            continue
        claimed.add(best.source_id)
        before = pd.DataFrame([best._asdict()])
        linked = _change_for(source_id, pd.concat([before, rows],
                                                  ignore_index=True), cutoff)
        linked.predecessor = str(best.source_id)
        out[source_id] = linked


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
# Reported size (the feed's sizeFmt) — for fires with no mapped polygon
# --------------------------------------------------------------------------- #
@dataclass
class SizeChange:
    """A fire's REPORTED size ('27 ha') now and how it moved inside the window.
    Most fire incidents are a point plus this text, never a polygon, so this is
    the only growth signal they carry. Read from the state journal, which the
    fire collector writes itself whenever an incident's state changes."""
    source_id: str
    size_ha: float
    baseline_ha: float              # at the window's start (or first seen in it)
    grown_ha: float = 0.0
    reduced_ha: float = 0.0
    step_at: datetime = None        # newest size change, when inside the window
    step_from_ha: float = None

    @property
    def changed(self):
        return bool(self.grown_ha or self.reduced_ha)

    @property
    def significant_step(self):
        """Growth at the latest step big enough to take over the wall."""
        if self.step_at is None or self.step_from_ha is None:
            return False
        grew = self.size_ha - self.step_from_ha
        return grew >= max(SIZE_TAKEOVER_MIN_HA,
                           self.step_from_ha * SIZE_TAKEOVER_MIN_PCT / 100.0)


def parse_ha(text):
    from app.intel_feed import _parse_ha
    return _parse_ha(text)


def size_changes(source_ids, now=None, window_hours=DEFAULT_WINDOW_HOURS):
    """{source_id: SizeChange} for fires whose reported size is known."""
    from app import history
    ids = sorted({str(s) for s in source_ids if s is not None})
    if not ids:
        return {}
    now = now or datetime.now()
    cutoff = now - timedelta(hours=window_hours)
    marks = ",".join("?" * len(ids))
    stamp = _stamp(cutoff)
    df = database.read_df(
        "SELECT entity_key, effective_ts, state_json FROM ("
        "  SELECT entity_key, effective_ts, state_json, id, ROW_NUMBER() OVER ("
        "    PARTITION BY entity_key ORDER BY effective_ts DESC, id DESC) AS rn"
        "  FROM entity_state_history WHERE source = ?"
        f"   AND entity_key IN ({marks}) AND effective_ts < ?"
        ") WHERE rn = 1 "
        "UNION ALL "
        "SELECT entity_key, effective_ts, state_json FROM entity_state_history "
        f"WHERE source = ? AND entity_key IN ({marks}) AND effective_ts >= ? "
        "ORDER BY entity_key, effective_ts",
        [history.FIRE, *ids, stamp, history.FIRE, *ids, stamp])
    out = {}
    for key, rows in df.groupby("entity_key", sort=False):
        try:
            change = _size_change_for(str(key), rows, cutoff)
        except Exception:
            log.exception("Fire areas: size change for %s failed", key)
            continue
        if change is not None:
            out[str(key)] = change
    return out


def _size_change_for(source_id, rows, cutoff):
    # Collapse the journal to the moments the SIZE changed (rows are written
    # for any field's change).
    points = []
    for ts, raw in zip(rows["effective_ts"], rows["state_json"]):
        try:
            ha = parse_ha(json.loads(raw).get("size"))
        except (TypeError, ValueError):
            ha = None
        if ha is None:
            continue
        if not points or abs(points[-1][1] - ha) > 1e-9:
            points.append((_dt(ts), ha))
    if not points:
        return None
    base, now_ha = points[0][1], points[-1][1]
    change = SizeChange(source_id=source_id, size_ha=now_ha, baseline_ha=base)
    delta = now_ha - base
    if delta >= MIN_CHANGE_HA:
        change.grown_ha = delta
    elif -delta >= MIN_CHANGE_HA:
        change.reduced_ha = -delta
    if len(points) > 1 and points[-1][0] is not None and points[-1][0] >= cutoff:
        change.step_at = points[-1][0]
        change.step_from_ha = points[-2][1]
    return change


def size_rings(member, size):
    """For a fire with no mapped polygon: circles of its reported size now and
    at the window's start, centred on its point, as lon/lat GeoJSON —
    (now, growth annulus, latest-step annulus). Only an equal-area stand-in, so
    the wall draws them dashed: the real perimeter is unknown."""
    if member.geom is None or member.geom.geom_type != "Point" or size is None:
        return None, None, None

    def disc(ha):
        if not ha or ha <= 0:
            return None
        return member.geom.buffer(math.sqrt(ha * 10_000.0 / math.pi), quad_segs=24)
    now = disc(size.size_ha)
    if now is None:
        return None, None, None
    grown = step = None
    if size.grown_ha:
        base = disc(size.baseline_ha)
        grown = now.difference(base) if base is not None else now
    if size.step_at is not None and size.step_from_ha is not None \
            and size.size_ha > size.step_from_ha:
        prev = disc(size.step_from_ha)
        step = now.difference(prev) if prev is not None else now
    as_json = lambda g: to_geojson(g) if g is not None and not g.is_empty else None
    return as_json(now), as_json(grown), as_json(step)


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
    # Fires whose growth is only known from their REPORTED size:
    # [(member, SizeChange)], shown as "10 → 27 ha reported".
    reported: list = field(default_factory=list)
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
    """A warning about a fire (or a burn), by its HAZARD fields (`event`,
    `category2`). The headline/location is only consulted when the feed gives
    no hazard at all: it is free text, and a flood warning for "Fire Station
    Rd" is still a flood warning."""
    hazard = " ".join(_text(row.get(c)) for c in ("event", "category2")).strip()
    text = (hazard or " ".join(_text(row.get(c)) for c in
                               ("headline", "category1"))).lower()
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


def build_areas(members, changes=None, link_km=DEFAULT_LINK_KM, sizes=None):
    """Build the areas of operation: ONE PER FIRE INCIDENT.

    Incidents are never grouped — two fires a few km apart are two areas, so
    each gets its own turn on the map and its own card. Within `link_km` of a
    fire:

    * a fire warning joins EVERY fire it is that close to (a warning polygon
      over two fires is part of both pictures);
    * a moving burn area joins the NEAREST fire only, so its growth is counted
      once;
    * every other member (other incidents, non-fire warnings, static burn
      areas) attaches to the nearest area as context.

    Fire warnings and moving burn areas with no fire incident nearby still make
    an area, linked among themselves as before (a fire can be warned about
    before an incident appears for it).
    """
    if not HAVE_SHAPELY:
        return []
    changes = changes or {}
    link_m = float(link_km) * 1000.0

    def nearest_first(tree, geoms, m):
        hits = tree.query(m.geom, predicate="dwithin", distance=link_m)
        return sorted((int(i) for i in hits),
                      key=lambda i: m.geom.distance(geoms[i]))

    fires = [m for m in members if m.role == "fire"]
    groups = [[f] for f in fires]
    fire_geoms = [f.geom for f in fires]
    fire_tree = STRtree(fire_geoms) if fires else None

    orphans = []
    for m in members:
        if not m.fire_related or m.role == "fire":
            continue
        hits = nearest_first(fire_tree, fire_geoms, m) if fire_tree else []
        if not hits:
            orphans.append(m)
        elif m.role == "warning":
            for i in hits:
                groups[i].append(m)
        else:
            groups[hits[0]].append(m)

    # Anchors for context: every fire-related member, by its group (a shared
    # warning anchors each of its groups).
    anchors = [(m.geom, i) for i, g in enumerate(groups) for m in g]
    if orphans:
        tree = STRtree([m.geom for m in orphans])
        uf = _Union(len(orphans))
        left, right = tree.query([m.geom for m in orphans], predicate="dwithin",
                                 distance=link_m)
        for a, b in zip(left, right):
            uf.union(int(a), int(b))
        index = {}
        for i, m in enumerate(orphans):
            root = uf.find(i)
            if root not in index:
                index[root] = len(groups)
                groups.append([])
            groups[index[root]].append(m)
            anchors.append((m.geom, index[root]))
    if not groups:
        return []

    anchor_geoms = [g for g, _ in anchors]
    anchor_tree = STRtree(anchor_geoms)
    for m in members:
        if m.fire_related:
            continue
        hits = anchor_tree.query(m.geom, predicate="dwithin", distance=link_m)
        if len(hits) == 0:
            continue
        # Nearest anchor; between areas sharing it, the nearest fire.
        best = min((int(i) for i in hits), key=lambda i: (
            m.geom.distance(anchor_geoms[i]),
            m.geom.distance(groups[anchors[i][1]][0].geom)))
        groups[anchors[best][1]].append(m)

    sizes = sizes or {}
    return sorted((_area(g, changes, sizes) for g in groups),
                  key=lambda a: (a.level_rank, -a.grown_ha, -a.area_ha,
                                 a.name.lower()))


def _area(members, changes, sizes=None):
    sizes = sizes or {}
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

    # Fires with no mapped polygon (most of them): their reported size is the
    # only measure of burnt area and of growth. A fire whose polygon is mapped
    # is measured by the polygon, never twice.
    for m in fires:
        if m.geom is not None and m.geom.geom_type in ("Polygon", "MultiPolygon"):
            continue
        sc = sizes.get(m.id)
        ha = sc.size_ha if sc is not None else parse_ha(m.size)
        area.area_ha += ha or 0.0
        if sc is None:
            continue
        area.grown_ha += sc.grown_ha
        area.reduced_ha += sc.reduced_ha
        if sc.changed or sc.step_at:
            area.reported.append((m, sc))
        if sc.step_at and (sc.grown_ha or sc.reduced_ha) and (
                area.step_at is None or sc.step_at > area.step_at):
            area.step_at = sc.step_at

    # Name: the largest fire, else the first fire, else the worst warning.
    def size(m):
        ch = changes.get(m.id)
        if ch:
            return ch.area_ha
        sc = sizes.get(m.id)
        return sc.size_ha if sc else (parse_ha(m.size) or 0.0)
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
            "sizes": {}, "incidents": pd.DataFrame(), "burn": pd.DataFrame(),
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
        sizes = size_changes(fire_ids, now, window_hours)
        assemble(snap, incidents, burn, changes, sizes)
    except Exception as e:
        log.exception("Fire wall: areas of operation unavailable")
        snap.update(ok=False, error=str(e))
    return snap


def assemble(snap, incidents, burn, changes, sizes=None):
    """Fill a snapshot from rows + their area changes. Shared by the live
    snapshot and the simulated test run (`app.fire_demo`), so the test
    exercises exactly the linking the wall uses."""
    members = members_from(incidents, burn, changes, snap["at"],
                           snap["window_hours"])
    sizes = sizes or {}
    snap.update(incidents=incidents, burn=burn, changes=changes, sizes=sizes,
                areas=build_areas(members, changes, snap["link_km"], sizes))
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
    grew (a mapped polygon, or — for a fire with none — its reported size
    growing by at least SIZE_TAKEOVER_MIN_HA / _PCT), and a Watch and Act / Emergency Warning inside an area (keyed by its
    level, so an escalation is a new key). A new small fire on its own is not
    one — in summer that would be a takeover every few minutes."""
    out, done = [], set()
    for area in snap.get("areas") or []:
        for m in area.members:
            if m.id in done:               # a warning shared by two areas
                continue
            done.add(m.id)
            ch = snap["changes"].get(m.id)
            if m.role in ("fire", "burn") and ch and ch.step_at \
                    and ch.step_grown_ha >= MIN_CHANGE_HA:
                out.append({"key": f"grow:{m.id}:{ch.step_at:%Y%m%d%H%M%S}",
                            "member": m.id, "kind": "growth",
                            "text": f"{m.location} burn area +{ch.step_grown_ha:,.0f} ha"})
            sc = (snap.get("sizes") or {}).get(m.id)
            if m.role == "fire" and sc is not None and sc.significant_step \
                    and not (ch and ch.step_grown_ha >= MIN_CHANGE_HA):
                out.append({"key": f"size:{m.id}:{sc.step_at:%Y%m%d%H%M%S}",
                            "member": m.id, "kind": "growth",
                            "text": f"{m.location} {_ha_text(sc.step_from_ha)} → "
                                    f"{_ha_text(sc.size_ha)}"})
            if m.role == "warning" and m.level in ("Emergency Warning",
                                                   "Watch and Act"):
                out.append({"key": f"warn:{m.id}:{m.level}", "member": m.id,
                            "kind": "warning", "level": m.level,
                            "text": f"{m.level} — {m.location}"})
    return out


def _ha_text(value):
    return f"{value:,.0f} ha" if value >= 10 else f"{value:,.1f} ha"


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

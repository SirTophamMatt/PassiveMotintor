"""VicEmergency incident & warning scraper.

Each cycle fetches the public VicEmergency GeoJSON feed (all live incidents and
community warnings state-wide), upserts every feature into ``fire_incidents``
keyed by its stable feed id, marks anything no longer present (or Safe/Complete)
as resolved, and writes a per-cycle ``fire_timeseries`` row (KPI counts + a
continuity heartbeat).

The feed also carries planned-burn boundary polygons (``feedType`` ``burn-area``)
which are static plan data, not live events — those are skipped. All other
categories are kept (fire, storm/tree-down, rescue, met warnings, ...) so the
page can present fire first but still surface everything. No auth: the feed is
public. It is served gzip-encoded, so the raw bytes are decompressed on read.
"""
import gzip
import json
import logging
from datetime import datetime

import requests

from app import database

log = logging.getLogger(__name__)

FEED_URL = "https://emergency.vic.gov.au/public/osom-geojson.json"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

# Incident statuses that mean the event is over (case-insensitive).
RESOLVED_STATUSES = {"safe", "complete"}

# Warning levels normalised to lowercase, for counting.
_LEVELS = ("emergency warning", "watch and act", "advice")

# Columns updated/inserted on fire_incidents (source_id handled separately).
_FIELDS = [
    "feed_type", "category1", "category2", "event", "warning_level", "severity",
    "status", "size", "resources", "location", "source_org", "action",
    "headline", "url", "latitude", "longitude", "geometry", "created", "updated",
]


def _fetch_feed():
    """Return the feed's feature list, or None on any fetch/parse failure (so a
    transient outage leaves existing rows untouched rather than wiping them)."""
    try:
        resp = requests.get(FEED_URL, headers=HEADERS, timeout=20)
        resp.raise_for_status()
    except requests.RequestException as e:
        log.warning("VicEmergency feed fetch failed: %s", e)
        return None
    raw = resp.content
    if raw[:2] == b"\x1f\x8b":  # gzip magic — the feed is served compressed
        try:
            raw = gzip.decompress(raw)
        except OSError as e:
            log.warning("VicEmergency feed gunzip failed: %s", e)
            return None
    try:
        return json.loads(raw).get("features", [])
    except (ValueError, AttributeError) as e:
        log.warning("VicEmergency feed parse failed: %s", e)
        return None


def _iter_coords(geom):
    """Yield every [lon, lat] pair in a GeoJSON geometry (Point / Polygon /
    GeometryCollection / nested arrays)."""
    if not geom:
        return
    if geom.get("type") == "GeometryCollection":
        for g in geom.get("geometries", []):
            yield from _iter_coords(g)
        return

    def walk(node):
        if isinstance(node, (list, tuple)):
            if len(node) >= 2 and all(isinstance(v, (int, float)) for v in node[:2]):
                yield node
            else:
                for child in node:
                    yield from walk(child)

    yield from walk(geom.get("coordinates"))


def _centroid(geom):
    """A representative (lat, lon) for a map marker — the mean of all vertices.
    Returns (None, None) if the geometry has no coordinates."""
    lons, lats = [], []
    for pair in _iter_coords(geom):
        lons.append(pair[0])
        lats.append(pair[1])
    if not lats:
        return None, None
    return sum(lats) / len(lats), sum(lons) / len(lons)


def _parse_dt(value):
    """Normalise a feed datetime (ISO-8601, either 'Z' or a '+10:00' offset) to
    a local-time 'YYYY-MM-DD HH:MM:SS' string. One uniform precision avoids the
    pandas mixed-format NaT trap the flood module hit. Returns None on failure."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone().replace(tzinfo=None)  # to server-local wall time
    return dt.isoformat(sep=" ", timespec="seconds")


def _flat(value):
    """Coerce a feed value that may arrive as a list/dict into a scalar string,
    so SQLite binding never chokes on feed variability (e.g. sizeFmt sometimes
    comes through as ['63 ha'])."""
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v) for v in value if v not in (None, "")) or None
    if isinstance(value, dict):
        return None
    return value


def _extract(feature):
    """Flatten one GeoJSON feature into a fire_incidents row dict."""
    p = feature.get("properties", {}) or {}
    cap = p.get("cap") or {}
    feed_type = p.get("feedType")
    category1 = _flat(p.get("category1"))
    geom = feature.get("geometry")
    lat, lon = _centroid(geom)
    return {
        "source_id": str(p.get("id")) if p.get("id") is not None else None,
        "feed_type": feed_type,
        "category1": category1,
        "category2": _flat(p.get("category2")),
        "event": _flat(cap.get("event")),
        # For warnings the level is carried in category1 (Advice / Watch and
        # Act / Emergency Warning); incidents have no level.
        "warning_level": category1 if feed_type == "warning" else None,
        "severity": _flat(cap.get("severity")),
        "status": _flat(p.get("status")),
        "size": _flat(p.get("sizeFmt") or p.get("size")),
        "resources": p.get("resources") if isinstance(p.get("resources"), int) else None,
        "location": _flat(p.get("location")),
        "source_org": _flat(p.get("sourceOrg")),
        "action": _flat(p.get("action")),
        "headline": _flat(p.get("webHeadline") or p.get("sourceTitle") or p.get("name")),
        "url": _flat(p.get("url")),
        "latitude": lat,
        "longitude": lon,
        # Store the raw geometry only when it's an area worth drawing (points
        # are already covered by the centroid marker), to keep rows lean.
        "geometry": (json.dumps(geom) if geom
                     and geom.get("type") != "Point" else None),
        "created": _parse_dt(p.get("created")),
        "updated": _parse_dt(p.get("updated")),
    }


def _is_resolved(status):
    return str(status or "").strip().lower() in RESOLVED_STATUSES


def _upsert(row, now):
    """Update the existing row for this source_id, or insert a new one.
    Returns 'updated' or 'inserted'."""
    resolved = 1 if _is_resolved(row["status"]) else 0
    set_clause = ", ".join(f"{f}=?" for f in _FIELDS)
    changed = database.execute(
        f"UPDATE fire_incidents SET {set_clause}, last_seen=?, resolved=? "
        "WHERE source_id=?",
        [row[f] for f in _FIELDS] + [now, resolved, row["source_id"]])
    if changed:
        return "updated"
    cols = ["source_id"] + _FIELDS + ["first_seen", "last_seen", "resolved"]
    database.execute(
        f"INSERT INTO fire_incidents ({', '.join(cols)}) "
        f"VALUES ({', '.join('?' for _ in cols)})",
        [row["source_id"]] + [row[f] for f in _FIELDS] + [now, now, resolved])
    return "inserted"


def _count_levels(rows):
    counts = {lvl: 0 for lvl in _LEVELS}
    for r in rows:
        lvl = str(r.get("warning_level") or "").strip().lower()
        if lvl in counts:
            counts[lvl] += 1
    return counts


def fetch_fire_data():
    """Fetch the feed, upsert incidents/warnings, resolve stale ones, and write
    a heartbeat/KPI row. Returns the number of newly-inserted rows."""
    now = datetime.now().isoformat(sep=" ", timespec="seconds")
    features = _fetch_feed()
    if features is None:
        return 0

    rows = []
    for feature in features:
        row = _extract(feature)
        if row["source_id"]:
            # Burn areas (historical DELWP burn footprints) are kept for the
            # map's toggleable "burn scars" layer but excluded from live counts.
            rows.append(row)

    inserted = updated = 0
    for row in rows:
        if _upsert(row, now) == "inserted":
            inserted += 1
        else:
            updated += 1

    # Anything still marked active in the DB but absent from this feed has
    # cleared — resolve it (guard against an empty feed wiping everything).
    seen = [r["source_id"] for r in rows]
    if seen:
        placeholders = ", ".join("?" for _ in seen)
        database.execute(
            f"UPDATE fire_incidents SET resolved=1, last_seen=? "
            f"WHERE resolved=0 AND source_id NOT IN ({placeholders})",
            [now] + seen)

    active = [r for r in rows if r["feed_type"] != "burn-area"
              and not _is_resolved(r["status"])]
    levels = _count_levels(active)
    database.insert_rows("fire_timeseries", [{
        "timestamp": now,
        "total_active": len(active),
        "active_fires": sum(1 for r in active
                            if str(r["category1"]).strip().lower() == "fire"),
        "emergency_warnings": levels["emergency warning"],
        "watch_act": levels["watch and act"],
        "advice": levels["advice"],
    }])
    log.info("Fire fetch: %d features (%d new, %d updated), %d active events",
             len(rows), inserted, updated, len(active))
    return inserted

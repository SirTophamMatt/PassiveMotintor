"""VicRoads / Transport Victoria "Unplanned Disruptions - Road" scraper.

Each cycle fetches the Disruptions - Road GeoJSON feed (near-real-time road
closures and disruptions across DoT-managed *and* local-council roads), upserts
every feature into ``road_disruptions`` keyed by its stable feed id, marks any
disruption no longer present (road reopened) as resolved, and writes a per-cycle
``road_timeseries`` row (KPI counts + a continuity heartbeat).

Unlike the VicEmergency / BoM feeds this one needs an API key: the key is sent
in the ``KeyID`` request header, and the exact endpoint URL comes with your Data
Exchange subscription (https://data-exchange.vicroads.vic.gov.au/). Both live in
config (``roads.feed_url`` / ``roads.api_key``); until BOTH are set the collector
logs and skips, so a fresh deploy never crashes — the same shape as power
collection without EM-COP credentials.

The parser is bound to the **v3 OpenAPI** (`Road Disruptions - Unplanned - v3`):
the response is an envelope ``{meta, data: <FeatureCollection>, links}`` (features
live at ``data.features``), geometry is Point or LineString, and the disruption
properties are partly nested under ``reference`` (road/intersection/LGA/SES region)
and ``impact`` (direction / impact type). Paging uses ``meta.total_pages``.
"""
import json
import logging
from datetime import datetime

import requests

from app import database
from app.config import load_config

log = logging.getLogger(__name__)

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
           "Accept": "application/json"}

# eventLocationStatus values (v3 enum) that mean the road is no longer a live
# closure, so is_closure is forced off even if a closedRoadName lingers.
_REOPENED_STATUSES = {"reopened", "inactive"}
# roadAccessType text that means traffic still gets through (not a full closure).
_NOT_CLOSURE_HINTS = ("partial", "lane", "reduced", "shoulder")

# Columns updated/inserted on road_disruptions (source_id handled separately).
_FIELDS = [
    "status", "disruption_type", "is_closure", "road_name", "location",
    "direction", "lanes_affected", "lga", "ses_region", "transport_region",
    "description", "latitude", "longitude", "geometry", "start_time",
    "end_time", "created", "updated",
]


def _flat(value):
    """Coerce a feed value that may arrive as a list/dict into a scalar string,
    so SQLite binding never chokes on feed variability."""
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v) for v in value if v not in (None, "")) or None
    if isinstance(value, dict):
        # Prefer a human-readable member if the field is an object.
        for k in ("value", "name", "text", "description", "displayName"):
            if k in value and value[k] not in (None, ""):
                return str(value[k])
        return None
    return value


def _iter_coords(geom):
    """Yield every [lon, lat] pair in a GeoJSON geometry (Point / LineString /
    Polygon / GeometryCollection / nested arrays)."""
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
    """Normalise a feed datetime (ISO-8601, 'Z' or an offset) to a local-time
    'YYYY-MM-DD HH:MM:SS' string. One uniform precision avoids the pandas
    mixed-format NaT trap. Returns None on failure."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone().replace(tzinfo=None)  # to server-local wall time
    return dt.isoformat(sep=" ", timespec="seconds")


def _is_closure(props):
    """True when this is a live FULL road closure (v3 fields). A reopened/
    inactive event, or one where traffic still gets through (partial/lane/
    reduced/shoulder), is not counted."""
    if str(props.get("eventLocationStatus") or "").strip().lower() in _REOPENED_STATUSES:
        return False
    access = str(props.get("roadAccessType") or "").lower()
    if any(h in access for h in _NOT_CLOSURE_HINTS):
        return False
    if props.get("closedRoadName"):
        return True
    if "clos" in access:  # "closed" / "closure"
        return True
    etype = f"{props.get('eventType') or ''} {props.get('eventSubType') or ''}".lower()
    return "clos" in etype


def _extract(feature):
    """Flatten one v3 GeoJSON feature into a road_disruptions row dict."""
    p = feature.get("properties") or {}
    ref = p.get("reference") or {}
    imp = p.get("impact") or {}
    geom = feature.get("geometry")
    lat, lon = _centroid(geom)

    dtype = ", ".join(x for x in (_flat(p.get("eventType")),
                                  _flat(p.get("eventSubType"))) if x) or None
    road_name = (_flat(p.get("closedRoadName")) or _flat(p.get("declaredRoadName"))
                 or _flat(ref.get("localRoadName"))
                 or _flat(ref.get("declaredRoadNumber")))
    location = (_flat(ref.get("startIntersectionLocality"))
                or _flat(ref.get("localGovernmentArea"))
                or _flat(p.get("description")))
    # A single event can have several impact locations; the per-feature id is the
    # stable upsert key (fall back to impactId/eventId if id is null).
    src = p.get("id") or p.get("impactId") or p.get("eventId")
    return {
        "source_id": str(src) if src not in (None, "") else None,
        "status": _flat(p.get("status")) or _flat(p.get("eventLocationStatus")),
        "disruption_type": dtype,
        "is_closure": 1 if _is_closure(p) else 0,
        "road_name": road_name,
        "location": location,
        "direction": _flat(imp.get("direction")),
        "lanes_affected": (_flat(p.get("numberLanesImpacted"))
                           or _flat(p.get("roadAccessType"))),
        "lga": _flat(ref.get("localGovernmentArea")),
        "ses_region": _flat(ref.get("closedRoadSESRegion")),
        "transport_region": _flat(ref.get("closedRoadTransportRegion")),
        "description": _flat(p.get("description")),
        "latitude": lat,
        "longitude": lon,
        # Store the raw geometry only for lines worth drawing (a Point is already
        # covered by the centroid marker), to keep rows lean.
        "geometry": (json.dumps(geom) if geom
                     and geom.get("type") != "Point" else None),
        "start_time": _parse_dt(p.get("created")),
        "end_time": _parse_dt(p.get("endTime")),
        "created": _parse_dt(p.get("created")),
        "updated": _parse_dt(p.get("lastUpdated")),
    }


def _fetch_feed(cfg):
    """Return the feed's feature list, or None on any fetch/parse failure (so a
    transient outage leaves existing rows untouched rather than wiping them).
    Returns None (and logs at debug) when the feed URL or API key is unset."""
    roads = cfg["roads"]
    url, key = roads.get("feed_url"), roads.get("api_key")
    if not url or not key:
        log.debug("Roads feed URL/API key not set — skipping.")
        return None
    limit = int(roads.get("page_limit") or 0)
    max_pages = max(1, int(roads.get("max_pages") or 1))
    headers = {**HEADERS, "KeyId": key}  # v3 security scheme header name

    features = []
    page = 1
    while page <= max_pages:
        params = {"page": page}
        if limit > 0:
            params["limit"] = limit
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=25)
            resp.raise_for_status()
            payload = resp.json()
        except requests.RequestException as e:
            log.warning("Road disruptions feed fetch (page %d) failed: %s", page, e)
            return features or None
        except ValueError as e:
            log.warning("Road disruptions feed parse (page %d) failed: %s", page, e)
            return features or None

        batch, meta = _features_of(payload)
        features.extend(batch)
        # Prefer the API's own page count; otherwise stop on a short/empty page.
        total_pages = meta.get("total_pages")
        if total_pages:
            if page >= int(total_pages):
                break
        elif len(batch) < (limit or 100):  # v3 default page size is 100
            break
        page += 1
    return features


def _features_of(payload):
    """Return (features, meta) from a v3 response. Tolerates the v3 envelope
    ({meta, data: FeatureCollection}), a bare FeatureCollection, or a plain list."""
    if isinstance(payload, list):
        return payload, {}
    if not isinstance(payload, dict):
        return [], {}
    meta = payload.get("meta") or {}
    data = payload.get("data")
    if isinstance(data, dict) and "features" in data:      # v3 envelope
        return data.get("features") or [], meta
    if "features" in payload:                               # bare FeatureCollection
        return payload.get("features") or [], meta
    return [], meta


def _upsert(row, now):
    """Update the existing row for this source_id, or insert a new one.
    Returns 'updated' or 'inserted'."""
    set_clause = ", ".join(f"{f}=?" for f in _FIELDS)
    changed = database.execute(
        f"UPDATE road_disruptions SET {set_clause}, last_seen=?, resolved=0 "
        "WHERE source_id=?",
        [row[f] for f in _FIELDS] + [now, row["source_id"]])
    if changed:
        return "updated"
    cols = ["source_id"] + _FIELDS + ["first_seen", "last_seen", "resolved"]
    database.execute(
        f"INSERT INTO road_disruptions ({', '.join(cols)}) "
        f"VALUES ({', '.join('?' for _ in cols)})",
        [row["source_id"]] + [row[f] for f in _FIELDS] + [now, now, 0])
    return "inserted"


def fetch_road_data():
    """Fetch the feed, upsert disruptions, resolve any that have cleared, and
    write a heartbeat/KPI row. Returns the number of newly-inserted rows.
    A missing key/URL or a fetch failure returns 0 without touching stored rows."""
    now = datetime.now().isoformat(sep=" ", timespec="seconds")
    cfg = load_config()
    features = _fetch_feed(cfg)
    if features is None:
        return 0

    rows = [r for r in (_extract(f) for f in features) if r["source_id"]]

    inserted = updated = 0
    for row in rows:
        if _upsert(row, now) == "inserted":
            inserted += 1
        else:
            updated += 1

    # Anything still active in the DB but absent from this feed has reopened —
    # resolve it (guard against an empty feed wiping everything).
    seen = [r["source_id"] for r in rows]
    if seen:
        placeholders = ", ".join("?" for _ in seen)
        database.execute(
            f"UPDATE road_disruptions SET resolved=1, last_seen=? "
            f"WHERE resolved=0 AND source_id NOT IN ({placeholders})",
            [now] + seen)

    closures = sum(1 for r in rows if r["is_closure"])
    database.insert_rows("road_timeseries", [{
        "timestamp": now,
        "total_active": len(rows),
        "closures": closures,
        "other_disruptions": len(rows) - closures,
    }])
    log.info("Roads fetch: %d disruptions (%d new, %d updated), %d full closures",
             len(rows), inserted, updated, closures)
    return inserted

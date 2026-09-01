"""Read-only spatial API: the monitored state as GeoJSON.

Every page in this app renders its own data server-side. This module does the
one thing none of them do — hands the *georeferenced* state to another system as
plain GeoJSON, so a mapping client can draw it.

It exists for God's Eye View, which consumes these layers on a 3D globe, but it
is deliberately generic: no client-specific shaping, no auth coupling, nothing
that writes. A GET here can never change state.

**The property contract is shared.** God's Eye View also ships an offline
snapshot exporter (`scripts/export-passive-monitor.mjs`) that reads this same
database directly and emits the same shape. The two must agree, because the
client swaps between them by changing a URL and nothing else. Every feature
carries:

    name      display label
    hazard    warning | flood-warning | incident | burn-area
              | flood | storm | power | roads
    severity  3 critical / 2 major / 1 notable / 0 background
    status    short operational state ("Watch and Act", "Below flood level")
    detail    one-line measurement context ("296 ha - 12 resources")
    ts        source timestamp
    source    provenance string

If you change a field here, change it there too.
"""
import json
import logging

import flask

from app import database

log = logging.getLogger(__name__)

# category1 verbatim from the VicEmergency feed -> (bucket, severity).
# Matched as a folded substring: the feed owns this vocabulary, not us, so an
# unfamiliar level must not hard-fail.
_WARNING_BUCKETS = (
    ("emergency", "warn-emergency", 3),
    ("watch", "warn-watch", 2),
    ("advice", "warn-advice", 1),
    ("community", "warn-community", 0),
)

# Layer id -> the hazard bucket it serves. Keep in step with God's Eye View's
# localLayers.js registry.
LAYERS = (
    "pm-warn-emergency",
    "pm-warn-watch",
    "pm-warn-advice",
    "pm-warn-community",
    "pm-flood-warning",
    "pm-incident",
    "pm-burn",
    "pm-flood",
    "pm-storm",
    "pm-power",
    "pm-roads",
)


def _clean(value):
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text.lower() == "nan" else text


def _num(value):
    """A real finite number, or None.

    pandas fills missing numerics with float NaN, and NaN is NOT valid JSON —
    `json.dumps` happily writes a bare `NaN` token that every strict parser,
    including the browser's `JSON.parse`, rejects. So every numeric that reaches
    a response body goes through here first. It also rescues the integer-ish
    floats pandas produces (`2.0`), which a naive `str(v).isdigit()` test
    silently drops.
    """
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def _detail(parts):
    """Join non-empty parts with a middot, dropping case-insensitive repeats."""
    seen = set()
    out = []
    for part in parts:
        text = _clean(part)
        if not text or text.lower() in seen:
            continue
        seen.add(text.lower())
        out.append(text)
    return " · ".join(out)


def _valid(lat, lon):
    try:
        lat = float(lat)
        lon = float(lon)
    except (TypeError, ValueError):
        return False
    if lat != lat or lon != lon:      # NaN
        return False
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return False
    return not (lat == 0 and lon == 0)


def _polygons(raw):
    """Every Polygon/MultiPolygon inside a raw GeoJSON column.

    VicEmergency does not return a bare polygon: these columns are usually a
    GeometryCollection pairing marker Points with the real warning-area
    Polygons, and a multi-area warning can carry eight of each. A
    GeometryCollection has no ``coordinates`` key at all — it has ``geometries``
    — so a naive ``if geom.get("coordinates")`` test silently discards the
    warning areas, which is the entire payload. Recurse, keep the areas, drop
    the redundant Points (each record already carries its own lat/lon anchor).
    """
    text = _clean(raw)
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        return []

    found = []

    def walk(node, depth=0):
        if not isinstance(node, dict) or depth > 8:
            return
        geom = node.get("geometry") if node.get("type") == "Feature" else node
        if not isinstance(geom, dict):
            return
        kind = geom.get("type")
        if kind == "GeometryCollection":
            for child in geom.get("geometries") or []:
                walk(child, depth + 1)
            return
        coords = geom.get("coordinates")
        if kind in ("Polygon", "MultiPolygon") and isinstance(coords, list) and coords:
            found.append({"type": kind, "coordinates": coords})

    walk(parsed)
    return found


def _lines(raw):
    """Line or area geometry for a road disruption (a closure is a stretch)."""
    text = _clean(raw)
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        return []
    geom = parsed.get("geometry") if parsed.get("type") == "Feature" else parsed
    if not isinstance(geom, dict):
        return []
    kind = geom.get("type")
    coords = geom.get("coordinates")
    if kind in ("LineString", "MultiLineString", "Polygon", "MultiPolygon") \
            and isinstance(coords, list) and coords:
        return [{"type": kind, "coordinates": coords}]
    return []


def _feature(geometry, properties):
    return {"type": "Feature", "geometry": geometry, "properties": properties}


def _point(lon, lat):
    return {"type": "Point", "coordinates": [float(lon), float(lat)]}


def _vic_emergency(include_resolved=False):
    """fire_incidents split by feed_type, then warnings split by level.

    The table is misnamed: it is the whole VicEmergency feed, and it is not
    fire-only — riverine flood warnings live here too. `feed_type` separates a
    public warning over an AREA from an operational incident at a POINT from a
    planned burn footprint.
    """
    where = "" if include_resolved else "WHERE resolved = 0"
    df = database.read_df(
        "SELECT source_id, feed_type, event, category1, category2, warning_level,"
        " severity, status, size, resources, location, action, headline, url,"
        " latitude, longitude, updated, last_seen, resolved, geometry"
        f" FROM fire_incidents {where}")

    groups = {key: [] for key in (
        "warn-emergency", "warn-watch", "warn-advice", "warn-community",
        "warn-flood", "incident", "burn")}

    for row in df.to_dict("records"):
        if not _valid(row.get("latitude"), row.get("longitude")):
            continue

        feed_type = _clean(row.get("feed_type")).lower()
        warning = _clean(row.get("warning_level"))
        level_key = warning.lower()
        resolved = bool(row.get("resolved"))

        if feed_type == "warning":
            bucket = None
            severity = 1
            for needle, key, sev in _WARNING_BUCKETS:
                if needle in level_key:
                    bucket, severity = key, sev
                    break
            # category2 == 'Met' marks the Bureau-issued products (riverine
            # flood, severe weather, thunderstorm). They get their own layer so
            # weather toggles independently of fire; severity still comes from
            # the escalation level.
            if _clean(row.get("category2")).lower() == "met":
                bucket, hazard = "warn-flood", "flood-warning"
            else:
                # An unrecognised level parks with Advice — the lowest
                # ACTIONABLE rung — rather than vanishing.
                bucket, hazard = (bucket or "warn-advice"), "warning"
        elif feed_type == "burn-area":
            bucket, severity, hazard = "burn", 0, "burn-area"
        else:
            bucket, hazard = "incident", "incident"
            severity = 2 if _clean(row.get("category1")).lower() == "fire" else 1

        if resolved:
            severity = 0

        name = (_clean(row.get("location")) or _clean(row.get("event"))
                or "VicEmergency record")
        resources = _num(row.get("resources"))
        props = {
            "name": name,
            "hazard": hazard,
            "warningLevel": warning or None,
            "severity": severity,
            "status": warning or _clean(row.get("status")) or _clean(row.get("event")),
            "detail": _detail([
                _clean(row.get("event")),
                _clean(row.get("size")),
                f"{int(resources)} resources" if resources else "",
                _clean(row.get("action")),
            ]),
            "headline": _clean(row.get("headline")),
            "category": _clean(row.get("category1")),
            "resolved": resolved,
            "ts": _clean(row.get("updated")) or _clean(row.get("last_seen")),
            "url": _clean(row.get("url")),
            "source": f"Passive Monitor · {hazard}",
        }

        groups[bucket].append(
            _feature(_point(row["longitude"], row["latitude"]), props))

        polys = _polygons(row.get("geometry"))
        for index, geom in enumerate(polys):
            area_props = dict(props)
            area_props["name"] = (
                f"{name} (area {index + 1} of {len(polys)})" if len(polys) > 1
                else f"{name} (area)")
            area_props["isExtent"] = True
            groups[bucket].append(_feature(geom, area_props))

    return groups


def _floods():
    """Latest observation per gauge, positioned via gauge_coords.

    `station_key` is a lowercased `station_name`, so the join folds case —
    matching on the raw name alone loses gauges to casing drift.
    """
    df = database.read_df("""
        SELECT fo.station_name, fo.catchment, fo.height_m, fo.tendency,
               fo.classification, fo.time_day, fo.timestamp,
               gc.latitude, gc.longitude,
               fl.minor, fl.moderate, fl.major
        FROM flood_observations fo
        JOIN gauge_coords gc ON gc.station_key = lower(fo.station_name)
        LEFT JOIN flood_levels fl ON fl.station_key = gc.station_key
        JOIN (SELECT station_name, MAX(timestamp) AS newest
              FROM flood_observations GROUP BY station_name) latest
          ON latest.station_name = fo.station_name
         AND latest.newest = fo.timestamp
        GROUP BY fo.station_name
    """)

    out = []
    for row in df.to_dict("records"):
        if not _valid(row.get("latitude"), row.get("longitude")):
            continue
        classification = _clean(row.get("classification"))
        key = classification.lower()
        severity = 3 if "major" in key else 2 if "moderate" in key else 1 if "minor" in key else 0

        # NaN formats happily as "nan m" rather than raising, so the guard has
        # to be an explicit finite check, not a try/except around the format.
        height = _num(row.get("height_m"))
        height_text = f"{height:.2f} m" if height is not None else ""

        thresholds = []
        for label, field in (("minor", "minor"), ("mod", "moderate"), ("maj", "major")):
            value = _num(row.get(field))
            if value is not None:
                thresholds.append(f"{label} {value:g}")

        out.append(_feature(_point(row["longitude"], row["latitude"]), {
            "name": _clean(row.get("station_name")) or "Flood gauge",
            "hazard": "flood",
            "severity": severity,
            "status": classification or "Below flood level",
            "detail": _detail([height_text, _clean(row.get("tendency")),
                               _clean(row.get("catchment")), " / ".join(thresholds)]),
            "heightM": height,
            "catchment": _clean(row.get("catchment")),
            "ts": _clean(row.get("timestamp")) or _clean(row.get("time_day")),
            "source": "Passive Monitor · flood",
        }))
    return out


def _storms():
    """Most recent radar frame only — earlier frames are the same cells at
    earlier positions, and drawing them all smears each storm across its own
    history."""
    newest = database.read_df("SELECT MAX(frame_ts) AS ts FROM storm_cells")
    if newest.empty or not _clean(newest.iloc[0]["ts"]):
        return []
    frame_ts = _clean(newest.iloc[0]["ts"])

    df = database.read_df(
        "SELECT cell_id, radar_id, frame_ts, latitude, longitude, area_km2,"
        " max_level, mean_level, intensity_score, classification, speed_kmh,"
        " bearing_deg, status, impact_geojson FROM storm_cells WHERE frame_ts = ?",
        (frame_ts,))

    out = []
    for row in df.to_dict("records"):
        if not _valid(row.get("latitude"), row.get("longitude")):
            continue
        classification = _clean(row.get("classification"))
        key = classification.lower()
        severity = 0
        if "severe" in key or "intense" in key:
            severity = 3
        elif "strong" in key:
            severity = 2
        elif "moderate" in key:
            severity = 1
        if not severity:
            try:                       # reflectivity backs up a missing label
                level = float(row.get("max_level"))
                severity = 3 if level >= 12 else 2 if level >= 9 else 1 if level >= 6 else 0
            except (TypeError, ValueError):
                pass

        motion = ""
        try:
            speed = float(row.get("speed_kmh"))
            if speed > 0:
                motion = f"{round(speed)} km/h"
                bearing = row.get("bearing_deg")
                if bearing is not None:
                    motion += f" @ {round(float(bearing))}°"
        except (TypeError, ValueError):
            pass

        area_km2 = _num(row.get("area_km2"))
        area = f"{round(area_km2)} km²" if area_km2 is not None else ""

        props = {
            "name": f"Storm cell {_clean(row.get('cell_id')) or '—'}",
            "hazard": "storm",
            "severity": severity,
            "status": classification or f"Level {row.get('max_level')}",
            "detail": _detail([area, motion,
                               f"radar {_clean(row.get('radar_id'))}" if _clean(row.get("radar_id")) else ""]),
            "areaKm2": area_km2,
            "ts": _clean(row.get("frame_ts")),
            "source": "Passive Monitor · storm",
        }
        out.append(_feature(_point(row["longitude"], row["latitude"]), props))
        for geom in _polygons(row.get("impact_geojson")):
            area_props = dict(props)
            area_props["name"] = f"{props['name']} (impact area)"
            area_props["isExtent"] = True
            out.append(_feature(geom, area_props))
    return out


def _power(include_resolved=False):
    """Outages positioned through the geocode cache. The outage table stores a
    place name only, so an outage with no cached geocode has nowhere to draw."""
    where = "" if include_resolved else "WHERE po.restored = 0"
    df = database.read_df(
        "SELECT po.location, po.customers_off, po.type, po.first_seen,"
        " po.last_seen, po.restored, po.duration_mins, gc.latitude, gc.longitude"
        " FROM power_outages po JOIN geocode_cache gc ON gc.location = po.location"
        f" {where}")

    out = []
    for row in df.to_dict("records"):
        if not _valid(row.get("latitude"), row.get("longitude")):
            continue
        try:
            customers = int(row.get("customers_off") or 0)
        except (TypeError, ValueError):
            customers = 0
        restored = bool(row.get("restored"))
        severity = 3 if customers >= 2000 else 2 if customers >= 500 else 1 if customers >= 100 else 0
        if restored:
            severity = 0

        duration = ""
        try:
            minutes = float(row.get("duration_mins"))
            if minutes > 0:
                duration = f"{round(minutes / 60)} h out"
        except (TypeError, ValueError):
            pass

        out.append(_feature(_point(row["longitude"], row["latitude"]), {
            "name": _clean(row.get("location")) or "Outage",
            "hazard": "power",
            "severity": severity,
            "status": "Restored" if restored else f"{customers:,} customers off",
            "detail": _detail([_clean(row.get("type")), duration]),
            "customersOff": customers,
            "resolved": restored,
            "ts": _clean(row.get("last_seen")) or _clean(row.get("first_seen")),
            "source": "Passive Monitor · power",
        }))
    return out


def _roads(include_resolved=False):
    """VicTraffic disruptions. Empty until `roads.api_key` is configured — that
    collector log-and-skips without one."""
    where = "" if include_resolved else "WHERE resolved = 0"
    try:
        df = database.read_df(
            "SELECT source_id, status, disruption_type, is_closure, road_name,"
            " location, direction, lanes_affected, lga, latitude, longitude,"
            " geometry, start_time, updated, last_seen, resolved"
            f" FROM road_disruptions {where}")
    except Exception:
        return []

    out = []
    for row in df.to_dict("records"):
        if not _valid(row.get("latitude"), row.get("longitude")):
            continue
        closure = bool(row.get("is_closure"))
        resolved = bool(row.get("resolved"))
        severity = 0 if resolved else (2 if closure else 1)
        name = (_clean(row.get("road_name")) or _clean(row.get("location"))
                or "Road disruption")
        props = {
            "name": name,
            "hazard": "roads",
            "severity": severity,
            "status": "Road closed" if closure else (
                _clean(row.get("status")) or _clean(row.get("disruption_type")) or "Disruption"),
            "detail": _detail([_clean(row.get("disruption_type")),
                               _clean(row.get("location")),
                               _clean(row.get("direction")),
                               _clean(row.get("lanes_affected")),
                               _clean(row.get("lga"))]),
            "isClosure": closure,
            "resolved": resolved,
            "ts": (_clean(row.get("updated")) or _clean(row.get("last_seen"))
                   or _clean(row.get("start_time"))),
            "source": "Passive Monitor · roads",
        }
        out.append(_feature(_point(row["longitude"], row["latitude"]), props))
        for geom in _lines(row.get("geometry")):
            extent = dict(props)
            extent["name"] = f"{name} (extent)"
            extent["isExtent"] = True
            out.append(_feature(geom, extent))
    return out


def build_layers(include_resolved=False):
    """All layers as {layer_id: [Feature, ...]}.

    One pass over VicEmergency serves seven of them, so callers asking for
    everything do not re-query per layer.
    """
    vic = _vic_emergency(include_resolved)
    return {
        "pm-warn-emergency": vic["warn-emergency"],
        "pm-warn-watch": vic["warn-watch"],
        "pm-warn-advice": vic["warn-advice"],
        "pm-warn-community": vic["warn-community"],
        "pm-flood-warning": vic["warn-flood"],
        "pm-incident": vic["incident"],
        "pm-burn": vic["burn"],
        "pm-flood": _floods(),
        "pm-storm": _storms(),
        "pm-power": _power(include_resolved),
        "pm-roads": _roads(include_resolved),
    }


def register(app):
    """Attach the read-only spatial API to the Dash app's Flask server."""

    def _include_resolved():
        return flask.request.args.get("resolved", "").lower() in ("1", "true", "yes")

    @app.server.route("/api/intel/layers")
    def intel_layers():
        """Layer inventory with counts — lets a client discover what exists
        (and see that an empty layer is empty on purpose) without pulling
        every feature."""
        try:
            layers = build_layers(_include_resolved())
        except Exception:
            log.exception("intel layer inventory failed")
            return flask.jsonify({"error": "layer inventory unavailable"}), 503
        return flask.jsonify({
            "layers": [{"id": key, "count": len(value)}
                       for key, value in layers.items()],
        })

    @app.server.route("/api/intel/geojson/<layer_id>")
    def intel_layer_geojson(layer_id):
        """One layer as a GeoJSON FeatureCollection."""
        if layer_id not in LAYERS:
            return flask.jsonify({"error": "unknown layer"}), 404
        try:
            features = build_layers(_include_resolved()).get(layer_id, [])
        except Exception:
            log.exception("intel geojson failed for %s", layer_id)
            return flask.jsonify({"error": "layer unavailable"}), 503

        response = flask.jsonify({
            "type": "FeatureCollection",
            "layer": layer_id,
            "features": features,
        })
        # Collectors run on their own cadence; a short cache keeps a polling
        # client from re-running these queries harder than the data changes.
        response.headers["Cache-Control"] = "public, max-age=30"
        return response

    @app.server.route("/api/intel/geojson")
    def intel_all_geojson():
        """Every layer in one response, keyed by layer id."""
        try:
            layers = build_layers(_include_resolved())
        except Exception:
            log.exception("intel geojson (all) failed")
            return flask.jsonify({"error": "layers unavailable"}), 503
        response = flask.jsonify({
            "layers": {
                key: {"type": "FeatureCollection", "layer": key, "features": value}
                for key, value in layers.items()
            },
        })
        response.headers["Cache-Control"] = "public, max-age=30"
        return response

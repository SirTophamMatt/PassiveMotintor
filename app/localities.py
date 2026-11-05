# Passive Monitor — Copyright (c) 2026 SirTophamMatt. All rights reserved.
"""Suburb / locality boundaries — the shapes behind "which suburbs is this
outage in".

EM-COP lists each power outage with the localities it affects, by NAME only.
This module holds the boundary of every Victorian locality so those names can
be drawn as areas on a map, and answers "which boundary is this name".

**Reference data, loaded on the server.** The development sandbox cannot reach
any government data host, so nothing is committed: the server downloads the
boundaries itself (Admin → Power → *Download locality boundaries*, or once on
boot while the table is empty) or imports a file someone downloaded by hand
(Import page / ``python -m app.localities import <file>``). One load replaces
the whole table in one transaction, and a load far smaller than what is already
there is refused, so a half-finished download can never wipe a good set.

Sources (``localities`` in config):

* ``arcgis`` (default) — an ArcGIS REST layer, by default the ABS ASGS 2021
  **Suburbs and Localities** (SAL) layer filtered to Victoria. Fetched as the
  list of object ids first, then in batches by id, so it works whatever the
  server's page size is. GeoJSON output is asked for; Esri JSON is converted
  when that is all the server speaks.
* ``geojson`` — any URL returning a GeoJSON FeatureCollection (a Vicmap Admin
  locality-polygon WFS GetFeature with ``outputFormat=application/json`` is the
  obvious one). WFS paging (``numberMatched`` / ``startIndex``) is followed.
* a file — GeoJSON, Esri JSON, or a shapefile (``.shp`` or zipped).

**Built without sight of the live sources.** The name, state and id properties
are found from a list of the usual field names (``NAME_FIELDS`` …), the
coordinates are checked to really be longitude/latitude (a projected VicGrid /
MGA file is refused with a clear message, never stored as nonsense), and a
response that cannot be understood is saved to ``localities_debug_<name>`` next
to the database with an error naming that file.

**Shapes are simplified** to ``simplify_m`` (50 m) and rounded to 4 dp
(~11 m) before storing — invisible at suburb zoom, and what keeps a map of a
few hundred affected suburbs light enough to refresh every minute.

**Matching a name** (``resolve``) normalises both sides the same way (case,
punctuation, postcodes, ``Mt``/``Mount``, ``St``/``Saint`` …). A name several
localities share (Golden Point, Spring Hill …) is decided by PROXIMITY: the
candidate nearest the outage's other, unambiguous localities — an outage is one
piece of network — or failing that the place the geocoder put that name. With
nothing to go on it stays ``ambiguous`` and is not drawn: a guessed suburb on
the wrong side of the state is worse than a missing one.
"""
import argparse
import gzip
import io
import json
import logging
import math
import os
import re
import threading
import urllib.parse
import urllib.request
import zipfile
from collections import Counter
from dataclasses import dataclass
from datetime import datetime

from app import database
from app.config import BASE_DIR, load_config

log = logging.getLogger(__name__)

HEADERS = {"User-Agent": "PassiveMonitor/1.0 (locality boundaries)"}

# lon_min, lat_min, lon_max, lat_max — Victoria plus a margin for the Murray
# and the SA/NSW edges. A locality whose inside point falls outside is dropped.
VIC_BBOX = (140.6, -39.6, 150.3, -33.8)

# Candidate property names, most specific first, compared case-insensitively.
# (`locality_n`, `sal_name21` …: a shapefile's .dbf caps field names at 10
# characters, so LOCALITY_NAME arrives truncated.)
NAME_FIELDS = ("locality_name", "gazetted_locality_name", "sal_name_2021",
               "sal_name21", "sal_name_2016", "ssc_name_2016", "ssc_name16",
               "locality_n", "loc_name", "vic_loca_2", "locality", "suburb",
               "name")
ID_FIELDS = ("sal_code_2021", "sal_code21", "pfi", "ufi", "loc_pid",
             "ssc_code_2016", "objectid", "fid", "id")
LGA_FIELDS = ("lga_name", "lga_name_2021", "lga_name21", "lga", "vic_lga__3")
STATE_FIELDS = ("state_code_2021", "ste_code21", "state_code", "state_name_2021",
                "ste_name21", "state_name", "state", "state_pid")
VIC_STATE_VALUES = {"2", "vic", "vic.", "victoria"}
# Every value a state field can hold under the encodings we know (ABS codes,
# abbreviations, names). A state field whose values are all in here is
# trusted; one holding something else (a code we do not know) is ignored and
# the Victoria bounding box alone decides.
KNOWN_STATE_VALUES = VIC_STATE_VALUES | {str(n) for n in range(1, 10)} | {
    "nsw", "qld", "sa", "wa", "tas", "nt", "act", "ot", "new south wales",
    "queensland", "south australia", "western australia", "tasmania",
    "northern territory", "australian capital territory", "other territories"}

# A load smaller than this share of what is already stored is refused (a
# truncated download must never replace a complete set) unless forced.
MIN_REPLACE_FRACTION = 0.5

# Matching statuses.
MATCHED, AMBIGUOUS, UNMATCHED, NO_DATA = "matched", "ambiguous", "unmatched", "no boundaries"


class LocalityError(RuntimeError):
    """A load could not be done; the message is user-facing."""


# --------------------------------------------------------------------------- #
# Names
# --------------------------------------------------------------------------- #
_QUALIFIER_RE = re.compile(r"\s*\(([^()]*)\)\s*$")
_STATE_SUFFIX_RE = re.compile(r"\s*-?\s*\bvic\b\.?\s*$", re.I)
_ABBREVIATIONS = {"MT": "MOUNT", "ST": "SAINT", "STH": "SOUTH", "NTH": "NORTH",
                  "PT": "PORT", "UPR": "UPPER", "LWR": "LOWER"}


def _clean_space(text):
    return " ".join(str(text or "").replace("\xa0", " ").split())


def split_qualifier(name):
    """("Golden Point", "Ballarat") from "Golden Point (Ballarat - Vic.)".

    ABS adds a bracketed qualifier to a name used more than once; a bare
    "(Vic.)" only says which state, so it is no qualifier at all."""
    text = _clean_space(name)
    m = _QUALIFIER_RE.search(text)
    if not m:
        return text, ""
    qualifier = _STATE_SUFFIX_RE.sub("", m.group(1)).strip(" -")
    return text[:m.start()].strip(), qualifier


def normalise(name):
    """The key names are matched on — applied to BOTH sides, so it only has to
    be consistent, not pretty. Qualifier, postcode and punctuation dropped,
    upper case, and the common abbreviations spelt out."""
    base, _ = split_qualifier(name)
    s = base.upper().replace("’", "").replace("'", "").replace("&", " AND ")
    s = re.sub(r"\b\d{4}\b", " ", s)
    s = re.sub(r"[^A-Z ]+", " ", s)
    return " ".join(_ABBREVIATIONS.get(w, w) for w in s.split())


def _title_word(word):
    if not word:
        return word
    out = word[:1].upper() + word[1:].lower()
    if out.startswith("Mc") and len(out) > 2:
        out = "Mc" + out[2].upper() + out[3:]
    return out


def display_name(name):
    """A name for screens: ALL-CAPS names (EM-COP, Vicmap) title-cased, mixed
    case left as published, the qualifier kept without the state."""
    base, qualifier = split_qualifier(name)
    base = re.sub(r"\s+\d{4}$", "", base)          # a trailing postcode
    if base and base == base.upper() and any(c.isalpha() for c in base):
        base = " ".join("-".join(_title_word(p) for p in w.split("-"))
                        for w in base.split(" "))
    return f"{base} ({qualifier})" if qualifier else base


# --------------------------------------------------------------------------- #
# Geometry
# --------------------------------------------------------------------------- #
def _polygonal(geom):
    """Only the polygon parts of a geometry (a locality is an area), or None."""
    from shapely.geometry import MultiPolygon
    if geom is None or geom.is_empty:
        return None
    if geom.geom_type in ("Polygon", "MultiPolygon"):
        return geom
    parts = []
    for g in getattr(geom, "geoms", []):
        p = _polygonal(g)
        if p is not None:
            parts.extend(p.geoms if p.geom_type == "MultiPolygon" else [p])
    if not parts:
        return None
    return parts[0] if len(parts) == 1 else MultiPolygon(parts)


def _round_coords(value):
    if isinstance(value, (list, tuple)):
        if value and isinstance(value[0], (int, float)):
            return [round(float(v), 4) for v in value]
        return [_round_coords(v) for v in value]
    return value


def _check_lonlat(geom):
    minx, miny, maxx, maxy = geom.bounds
    if not (-180 <= minx <= 180 and -180 <= maxx <= 180
            and -90 <= miny <= 90 and -90 <= maxy <= 90):
        raise LocalityError(
            "The boundary coordinates are not longitude/latitude (they look "
            "projected — VicGrid or MGA). Download the data in a geographic "
            "coordinate system (GDA2020, GDA94 or WGS84 — EPSG:7844, 4283 or "
            "4326) and load it again.")


def prepare_geometry(raw, simplify_m=50):
    """(full, stored) shapely geometries for one GeoJSON geometry, or None.
    `stored` is simplified and rounded; `full` is used for the area."""
    import shapely
    from shapely.geometry import shape
    geom = shape(raw)
    if geom.is_empty:
        return None
    _check_lonlat(geom)
    if not geom.is_valid:
        geom = shapely.make_valid(geom)
    geom = _polygonal(geom)
    if geom is None:
        return None
    tolerance = max(float(simplify_m or 0), 0.0) / 111_320.0
    stored = geom.simplify(tolerance, preserve_topology=True) if tolerance else geom
    stored = _polygonal(shapely.set_precision(stored, 0.0001))
    if stored is None:          # a sliver smaller than the rounding grid
        stored = geom
    return geom, stored


def _area_km2(geom):
    lat = geom.representative_point().y
    return abs(geom.area) * 111.32 * 111.32 * math.cos(math.radians(lat))


def _esri_polygon(geometry):
    """Esri JSON rings → a GeoJSON (Multi)Polygon. Esri winds outer rings
    clockwise and holes anticlockwise; each hole goes to the ring holding it."""
    from shapely.geometry import LinearRing, MultiPolygon, Point, Polygon, mapping
    rings = [r for r in (geometry or {}).get("rings") or [] if len(r) >= 4]
    if not rings:
        return None
    outers, holes = [], []
    for ring in rings:
        (holes if LinearRing(ring).is_ccw else outers).append(ring)
    if not outers:              # a server that ignores the winding rule
        outers, holes = holes, []
    shells = [Polygon(o) for o in outers]
    owned = [[] for _ in outers]
    for hole in holes:
        pt = Point(hole[0])
        for i, shell in enumerate(shells):
            if shell.contains(pt):
                owned[i].append(hole)
                break
    polys = [Polygon(o, h) for o, h in zip(outers, owned)]
    geom = polys[0] if len(polys) == 1 else MultiPolygon(polys)
    return mapping(geom)


def esri_features(doc):
    """GeoJSON features from an Esri JSON FeatureSet."""
    out = []
    for f in (doc or {}).get("features") or []:
        out.append({"type": "Feature", "properties": f.get("attributes") or {},
                    "geometry": _esri_polygon(f.get("geometry"))})
    return out


def features_of(doc):
    """Features from a parsed document: GeoJSON FeatureCollection, a bare
    list, or an Esri FeatureSet."""
    if isinstance(doc, list):
        return doc
    if not isinstance(doc, dict):
        return []
    feats = doc.get("features")
    if not isinstance(feats, list):
        return []
    if feats and "attributes" in feats[0] and "properties" not in feats[0]:
        return esri_features(doc)
    return feats


# --------------------------------------------------------------------------- #
# Features → rows
# --------------------------------------------------------------------------- #
def _lower_props(feature):
    return {str(k).lower(): v for k, v in (feature.get("properties") or {}).items()}


def _first_field(keys, candidates):
    return next((c for c in candidates if c in keys), None)


def _text(value):
    if value is None:
        return ""
    if isinstance(value, float) and value != value:
        return ""
    return _clean_space(value)


def features_to_rows(features, source="", name_field="", simplify_m=50,
                     use_state=True):
    """Boundary rows for the Victorian locality polygons among `features`, and
    a {reason: count} of what was skipped."""
    features = list(features or [])
    keys = set()
    for f in features[:200]:
        keys.update(_lower_props(f))
    name_key = (name_field or "").strip().lower() or _first_field(keys, NAME_FIELDS)
    if not name_key or name_key not in keys:
        raise LocalityError(
            "No locality-name property found in the data (properties seen: %s). "
            "Set localities.name_field in config.json to the right one."
            % (", ".join(sorted(keys)[:20]) or "none"))
    state_key = _first_field(keys, STATE_FIELDS) if use_state else None
    id_key = _first_field(keys, ID_FIELDS)
    lga_key = _first_field(keys, LGA_FIELDS)
    loaded_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    rows, skipped = [], Counter()
    for i, f in enumerate(features):
        p = _lower_props(f)
        if state_key and _text(p.get(state_key)).lower() not in VIC_STATE_VALUES:
            skipped["other state"] += 1
            continue
        name = _text(p.get(name_key))
        if not name:
            skipped["no name"] += 1
            continue
        if not f.get("geometry"):
            skipped["no geometry"] += 1
            continue
        try:
            prepared = prepare_geometry(f["geometry"], simplify_m)
        except LocalityError:
            raise
        except Exception:
            skipped["unreadable geometry"] += 1
            continue
        if prepared is None:
            skipped["not an area"] += 1
            continue
        full, stored = prepared
        pt = stored.representative_point()
        if not (VIC_BBOX[0] <= pt.x <= VIC_BBOX[2] and VIC_BBOX[1] <= pt.y <= VIC_BBOX[3]):
            skipped["outside Victoria"] += 1
            continue
        from shapely.geometry import mapping
        base, qualifier = split_qualifier(name)
        min_lon, min_lat, max_lon, max_lat = stored.bounds
        rows.append({
            "name": name,
            "display_name": display_name(name),
            "name_key": normalise(name),
            "qualifier": qualifier or None,
            "lga": _text(p.get(lga_key)) or None if lga_key else None,
            "source": source,
            "source_id": _text(p.get(id_key)) or str(i) if id_key else str(i),
            "geometry": json.dumps(_round_coords(mapping(stored)),
                                   separators=(",", ":")),
            "latitude": round(pt.y, 5), "longitude": round(pt.x, 5),
            "min_lat": min_lat, "min_lon": min_lon,
            "max_lat": max_lat, "max_lon": max_lon,
            "area_km2": round(_area_km2(full), 3),
            "loaded_at": loaded_at,
        })
    if state_key and not rows and features:
        values = {_text(_lower_props(f).get(state_key)).lower() for f in features}
        if not values <= KNOWN_STATE_VALUES:
            # The "state" field meant something else here (a code we do not
            # know): fall back to the Victoria bounding box alone.
            return features_to_rows(features, source, name_field, simplify_m,
                                    use_state=False)
    _label_duplicates(rows)
    return rows, dict(skipped)


def _label_duplicates(rows):
    """A name several localities share gets its LGA on screen when the source
    gave it no qualifier of its own (Vicmap does not; ABS does)."""
    counts = Counter(r["name_key"] for r in rows)
    for r in rows:
        if counts[r["name_key"]] > 1 and not r["qualifier"] and r.get("lga"):
            r["qualifier"] = r["lga"]
            r["display_name"] = "%s (%s)" % (display_name(r["name"]),
                                             display_name(r["lga"]))


# --------------------------------------------------------------------------- #
# Fetching
# --------------------------------------------------------------------------- #
def _debug_path(name):
    return os.path.join(BASE_DIR, f"localities_debug_{name}.txt")


def _save_debug(name, body):
    path = _debug_path(name)
    try:
        with open(path, "wb") as f:
            f.write(body[:2_000_000] if isinstance(body, bytes) else
                    str(body)[:2_000_000].encode("utf-8", "replace"))
    except OSError:
        return "(could not save the response)"
    return path


def _get_json(url, params, timeout, debug_name):
    full = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(params) if params else url
    host = urllib.parse.urlsplit(url).netloc or url
    try:
        req = urllib.request.Request(full, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
    except Exception as e:      # network, HTTP error, timeout
        raise LocalityError(f"Could not reach {host} ({e.__class__.__name__}: {e})")
    if body[:2] == b"\x1f\x8b":
        body = gzip.decompress(body)
    try:
        return json.loads(body.decode("utf-8-sig"))
    except (ValueError, UnicodeDecodeError):
        path = _save_debug(debug_name, body)
        raise LocalityError(f"{host} did not return JSON — the response was "
                            f"saved to {path}")


def _arcgis_error(doc):
    err = doc.get("error") if isinstance(doc, dict) else None
    if not err:
        return None
    details = "; ".join(str(d) for d in err.get("details") or [] if d)
    return "%s%s" % (err.get("message") or "error", f" ({details})" if details else "")


def fetch_arcgis(cfg):
    """Every feature of an ArcGIS REST layer matching `arcgis_where`."""
    base = (cfg.get("arcgis_url") or "").strip().rstrip("/")
    if not base:
        raise LocalityError("localities.arcgis_url is not set.")
    if base.endswith("/query"):
        base = base[:-len("/query")]
    query = base + "/query"
    timeout = float(cfg.get("timeout_seconds") or 60)
    where = (cfg.get("arcgis_where") or "").strip() or "1=1"
    ids_doc = _get_json(query, {"where": where, "returnIdsOnly": "true",
                                "f": "json"}, timeout, "arcgis")
    error = _arcgis_error(ids_doc)
    if error:
        raise LocalityError(f"The ArcGIS layer refused the query ({error}). "
                            f"Check localities.arcgis_where ({where!r}).")
    ids = sorted((ids_doc or {}).get("objectIds") or [])
    if not ids:
        path = _save_debug("arcgis", json.dumps(ids_doc)[:100_000])
        raise LocalityError(f"The ArcGIS query matched no features (where "
                            f"{where!r}); the response was saved to {path}")
    batch = max(1, int(cfg.get("batch_size") or 100))
    offset = max(float(cfg.get("simplify_m") or 0), 0.0) / 2 / 111_320.0
    features = []
    for i in range(0, len(ids), batch):
        params = {"objectIds": ",".join(str(x) for x in ids[i:i + batch]),
                  "outFields": "*", "returnGeometry": "true", "outSR": "4326",
                  "geometryPrecision": "5", "f": "geojson"}
        if offset:
            params["maxAllowableOffset"] = "%.7f" % offset
        doc = _get_json(query, params, timeout, "arcgis")
        if _arcgis_error(doc) or not isinstance(doc.get("features"), list):
            # Older servers do not speak GeoJSON: ask for Esri JSON instead.
            params["f"] = "json"
            doc = _get_json(query, params, timeout, "arcgis")
            error = _arcgis_error(doc)
            if error:
                raise LocalityError(f"The ArcGIS layer refused a batch ({error}).")
        features.extend(features_of(doc))
        log.info("Localities: fetched %d / %d features", len(features), len(ids))
    return features


def fetch_geojson(cfg):
    """Every feature from a GeoJSON URL, following WFS paging when the server
    says there are more (numberMatched > what came back)."""
    url = (cfg.get("geojson_url") or "").strip()
    if not url:
        raise LocalityError("localities.geojson_url is not set.")
    timeout = float(cfg.get("timeout_seconds") or 60)
    features, start = [], 0
    for _ in range(200):        # hard stop; a state is a few thousand features
        page_url = url if not start else _with_param(url, "startIndex", start)
        doc = _get_json(page_url, None, timeout, "geojson")
        page = features_of(doc)
        if not page and not features:
            path = _save_debug("geojson", json.dumps(doc)[:100_000])
            raise LocalityError(f"No features in the response; it was saved to {path}")
        features.extend(page)
        matched = doc.get("numberMatched") if isinstance(doc, dict) else None
        try:
            matched = int(matched)
        except (TypeError, ValueError):
            break
        if not page or len(features) >= matched:
            break
        start = len(features)
    return features


def _with_param(url, key, value):
    parts = urllib.parse.urlsplit(url)
    query = [(k, v) for k, v in urllib.parse.parse_qsl(parts.query)
             if k.lower() != key.lower()]
    query.append((key, str(value)))
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query)))


def read_file(path):
    """Features from a GeoJSON / Esri JSON file or a shapefile (.shp or .zip)."""
    if not os.path.exists(path):
        raise LocalityError(f"File not found: {path}")
    ext = os.path.splitext(path)[1].lower()
    if ext in (".json", ".geojson"):
        try:
            with open(path, "r", encoding="utf-8-sig") as f:
                return features_of(json.load(f))
        except ValueError as e:
            raise LocalityError(f"{os.path.basename(path)} is not valid JSON ({e})")
    if ext in (".zip", ".shp"):
        return _read_shapefile(path)
    raise LocalityError("Unsupported file type — use .geojson / .json, a .shp, "
                        "or a zipped shapefile (.zip).")


def _read_shapefile(path):
    import shapefile
    prj = None
    if path.lower().endswith(".zip"):
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
            shps = [n for n in names if n.lower().endswith(".shp")]
            if not shps:
                raise LocalityError("The zip holds no .shp file.")
            preferred = [n for n in shps if re.search(r"locality|sal|suburb", n, re.I)]
            stem = (preferred or shps)[0][:-4]

            def part(ext):
                hit = next((n for n in names if n.lower() == (stem + ext).lower()), None)
                return io.BytesIO(z.read(hit)) if hit else None
            reader = shapefile.Reader(shp=part(".shp"), shx=part(".shx"),
                                      dbf=part(".dbf"), encodingErrors="replace")
            prj_part = part(".prj")
            prj = prj_part.read().decode("utf-8", "replace") if prj_part else None
    else:
        reader = shapefile.Reader(path, encodingErrors="replace")
        prj_path = path[:-4] + ".prj"
        if os.path.exists(prj_path):
            with open(prj_path, "r", encoding="utf-8", errors="replace") as f:
                prj = f.read()
    if prj and prj.strip().upper().startswith("PROJCS"):
        crs = prj.split('"')[1] if '"' in prj else "projected"
        raise LocalityError(
            f"The shapefile is in a projected coordinate system ({crs}). "
            "Download it in GDA2020 / GDA94 / WGS84 latitude-longitude and load "
            "it again.")
    out = []
    try:
        for sr in reader.iterShapeRecords():
            try:
                geom = sr.shape.__geo_interface__
            except Exception:
                geom = None
            out.append({"type": "Feature", "properties": sr.record.as_dict(),
                        "geometry": geom})
    finally:
        reader.close()
    return out


# --------------------------------------------------------------------------- #
# Storage
# --------------------------------------------------------------------------- #
_COLUMNS = ("name", "display_name", "name_key", "qualifier", "lga", "source",
            "source_id", "geometry", "latitude", "longitude", "min_lat",
            "min_lon", "max_lat", "max_lon", "area_km2", "loaded_at")


def count():
    df = database.read_df("SELECT COUNT(*) AS n FROM locality_boundaries")
    return int(df.iloc[0]["n"]) if not df.empty else 0


def replace_all(rows):
    """Swap the whole table for `rows` in ONE transaction: a failure part-way
    leaves the previous set exactly as it was."""
    sql = "INSERT INTO locality_boundaries (%s) VALUES (%s)" % (
        ", ".join(_COLUMNS), ", ".join("?" for _ in _COLUMNS))
    conn = database.get_connection()
    try:
        conn.execute("DELETE FROM locality_boundaries")
        conn.executemany(sql, [[r.get(c) for c in _COLUMNS] for r in rows])
        conn.commit()
    finally:
        conn.close()
    _invalidate()
    return len(rows)


def load_features(features, source, cfg=None, force=False):
    """Validate, simplify and store. Returns (count, message)."""
    cfg = cfg or load_config()["localities"]
    rows, skipped = features_to_rows(features, source, cfg.get("name_field") or "",
                                     cfg.get("simplify_m", 50))
    if not rows:
        raise LocalityError("No Victorian locality polygons in the data (%d "
                            "features; skipped: %s)." % (len(list(features or [])),
                                                         _skipped_text(skipped)))
    existing = count()
    if not force and existing and len(rows) < existing * MIN_REPLACE_FRACTION:
        raise LocalityError(
            f"Only {len(rows):,} localities in the new data against {existing:,} "
            "already loaded — not replacing them (a download cut short looks "
            "exactly like this). Use force to replace anyway.")
    replace_all(rows)
    msg = f"Loaded {len(rows):,} locality boundaries from {source}."
    if skipped:
        msg += f" Skipped: {_skipped_text(skipped)}."
    log.info("Localities: %s", msg)
    return len(rows), msg


def _skipped_text(skipped):
    return ", ".join(f"{n:,} {why}" for why, n in sorted(skipped.items())) or "none"


def _source_label(cfg):
    if (cfg.get("source") or "arcgis") == "geojson":
        return urllib.parse.urlsplit(cfg.get("geojson_url") or "").netloc or "GeoJSON URL"
    url = cfg.get("arcgis_url") or ""
    if "abs.gov.au" in url and "SAL" in url.upper():
        return "ABS Suburbs and Localities (ASGS 2021)"
    return urllib.parse.urlsplit(url).netloc or "ArcGIS layer"


def fetch_and_load(cfg=None, force=False):
    cfg = cfg or load_config()["localities"]
    if (cfg.get("source") or "arcgis") == "geojson":
        features = fetch_geojson(cfg)
    else:
        features = fetch_arcgis(cfg)
    return load_features(features, _source_label(cfg), cfg, force)


def import_path(path, force=False):
    features = read_file(path)
    return load_features(features, os.path.basename(path), force=force)


# --------------------------------------------------------------------------- #
# Background loading (Admin button, boot) — one at a time
# --------------------------------------------------------------------------- #
_run_lock = threading.Lock()
_state = {"running": False, "last_error": None, "last_message": None,
          "finished": None}


def run(job, *args, **kwargs):
    """Run a load under the single-flight lock. Returns (ok, message)."""
    if not _run_lock.acquire(blocking=False):
        return False, "A locality boundary load is already running."
    _state.update(running=True)
    try:
        _, msg = job(*args, **kwargs)
        _state.update(last_message=msg, last_error=None)
        return True, msg
    except LocalityError as e:
        _state.update(last_error=str(e))
        log.warning("Localities: %s", e)
        return False, str(e)
    except Exception as e:
        _state.update(last_error=f"{e.__class__.__name__}: {e}")
        log.exception("Localities: load failed")
        return False, f"Load failed: {e.__class__.__name__}: {e}"
    finally:
        _state.update(running=False,
                      finished=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        _run_lock.release()


def start_fetch(force=False):
    """Download in a background thread. Returns (started, message)."""
    if _state["running"]:
        return False, "A locality boundary load is already running."
    threading.Thread(target=run, args=(fetch_and_load,), kwargs={"force": force},
                     name="localities-fetch", daemon=True).start()
    return True, ("Downloading locality boundaries in the background — this "
                  "page shows the result when it finishes.")


def autofetch():
    """On boot: download once if nothing is loaded and autofetch is on."""
    try:
        cfg = load_config()["localities"]
        if not cfg.get("autofetch", True) or count() > 0:
            return False
    except Exception:
        log.exception("Localities: autofetch check failed")
        return False
    started, _ = start_fetch()
    return started


def status():
    df = database.read_df(
        "SELECT COUNT(*) AS n, MAX(loaded_at) AS loaded_at, MAX(source) AS source "
        "FROM locality_boundaries")
    row = df.iloc[0] if not df.empty else {}
    return {"count": int(row.get("n") or 0) if len(df) else 0,
            "loaded_at": row.get("loaded_at") if len(df) else None,
            "source": row.get("source") if len(df) else None,
            "running": _state["running"], "last_error": _state["last_error"],
            "last_message": _state["last_message"], "finished": _state["finished"]}


# --------------------------------------------------------------------------- #
# Lookup
# --------------------------------------------------------------------------- #
@dataclass
class Candidate:
    id: int
    name: str
    display: str
    qualifier_key: str
    lga_key: str
    latitude: float
    longitude: float


@dataclass
class Match:
    name: str                   # as listed by the source (EM-COP)
    status: str                 # matched | ambiguous | unmatched | no boundaries
    id: int = None
    display: str = None
    latitude: float = None
    longitude: float = None
    candidates: int = 0


_cache_lock = threading.Lock()
_cache = {"stamp": None, "by_key": {}, "geoms": {}}
GEOMETRY_CACHE_LIMIT = 6000


def _invalidate():
    with _cache_lock:
        _cache.update(stamp=None, by_key={}, geoms={})


def _table_stamp():
    df = database.read_df("SELECT COUNT(*) AS n, MAX(id) AS m, MAX(loaded_at) AS t "
                          "FROM locality_boundaries")
    if df.empty:
        return (0, None, None)
    r = df.iloc[0]
    return (int(r["n"] or 0), r["m"], r["t"])


def index():
    """{name_key: [Candidate, ...]} — cached until the table changes."""
    stamp = _table_stamp()
    with _cache_lock:
        if stamp == _cache["stamp"]:
            return _cache["by_key"]
    df = database.read_df(
        "SELECT id, name, display_name, name_key, qualifier, lga, latitude, "
        "longitude FROM locality_boundaries")
    by_key = {}
    for r in df.itertuples(index=False):
        by_key.setdefault(r.name_key, []).append(Candidate(
            id=int(r.id), name=r.name, display=r.display_name or display_name(r.name),
            qualifier_key=normalise(r.qualifier or ""), lga_key=normalise(r.lga or ""),
            latitude=float(r.latitude), longitude=float(r.longitude)))
    with _cache_lock:
        _cache.update(stamp=stamp, by_key=by_key, geoms={})
    return by_key


def geometries(ids):
    """{id: GeoJSON geometry dict} for boundary ids (cached)."""
    ids = [int(i) for i in dict.fromkeys(ids) if i is not None]
    with _cache_lock:
        out = {i: _cache["geoms"][i] for i in ids if i in _cache["geoms"]}
    missing = [i for i in ids if i not in out]
    for start in range(0, len(missing), 500):
        chunk = missing[start:start + 500]
        df = database.read_df(
            "SELECT id, geometry FROM locality_boundaries WHERE id IN (%s)"
            % ",".join("?" * len(chunk)), chunk)
        for r in df.itertuples(index=False):
            try:
                out[int(r.id)] = json.loads(r.geometry)
            except (TypeError, ValueError):
                continue
    with _cache_lock:
        if len(_cache["geoms"]) > GEOMETRY_CACHE_LIMIT:
            _cache["geoms"] = {}
        _cache["geoms"].update({i: out[i] for i in missing if i in out})
    return out


def _valid_point(pt):
    try:
        lat, lon = float(pt[0]), float(pt[1])
    except (TypeError, ValueError, IndexError):
        return None
    if lat != lat or lon != lon:
        return None
    return lat, lon


def resolve(names, hints=None, by_key=None):
    """A Match per name, in order. `names` are the localities of ONE outage —
    they decide between localities sharing a name; `hints` ({name: (lat,
    lon)}, the geocoder's guess) are the fallback."""
    by_key = index() if by_key is None else by_key
    names = [_text(n) for n in names]
    if not by_key:
        return [Match(n, NO_DATA) for n in names]
    hints = hints or {}
    found, pending = {}, []
    for name in dict.fromkeys(names):
        cands = by_key.get(normalise(name), [])
        _, qualifier = split_qualifier(name)
        if qualifier and len(cands) > 1:
            q = normalise(qualifier)
            narrowed = [c for c in cands if q and q in (c.qualifier_key, c.lga_key)]
            cands = narrowed or cands
        if len(cands) == 1:
            c = cands[0]
            found[name] = Match(name, MATCHED, c.id, c.display, c.latitude,
                                c.longitude, 1)
        elif not cands:
            found[name] = Match(name, UNMATCHED)
        else:
            pending.append((name, cands))
    anchors = [(m.latitude, m.longitude) for m in found.values() if m.status == MATCHED]
    for name, cands in pending:
        pts = anchors or ([p] if (p := _valid_point(hints.get(name))) else [])
        if not pts:
            found[name] = Match(name, AMBIGUOUS, candidates=len(cands))
            continue
        alat = sum(p[0] for p in pts) / len(pts)
        alon = sum(p[1] for p in pts) / len(pts)
        scale = math.cos(math.radians(alat))
        best = min(cands, key=lambda c: (c.latitude - alat) ** 2
                   + ((c.longitude - alon) * scale) ** 2)
        found[name] = Match(name, MATCHED, best.id, best.display, best.latitude,
                            best.longitude, len(cands))
    return [found[n] for n in names]


def attach(df, groups):
    """Add locality columns to per-location outage rows:
    ``locality_id`` · ``locality_name`` · ``locality_status`` ·
    ``locality_lat`` / ``locality_lon`` (inside the shape) · ``locality_geometry``
    (GeoJSON dict). `groups` gives each row's outage, so a shared name is
    decided by the other suburbs of the SAME outage."""
    if df is None or df.empty:
        return df
    df = df.copy()
    for col in ("locality_id", "locality_name", "locality_status",
                "locality_lat", "locality_lon", "locality_geometry"):
        df[col] = None
    by_key = index()
    if not by_key:
        df["locality_status"] = NO_DATA
        return df
    groups = list(groups)
    members = {}
    for pos, g in enumerate(groups):
        members.setdefault(g, []).append(pos)
    lat_col = df.columns.get_loc("latitude") if "latitude" in df.columns else None
    lon_col = df.columns.get_loc("longitude") if "longitude" in df.columns else None
    loc_col = df.columns.get_loc("location")
    results = [None] * len(df)
    for positions in members.values():
        names = [df.iat[p, loc_col] for p in positions]
        hints = {}
        if lat_col is not None and lon_col is not None:
            for p, n in zip(positions, names):
                pt = _valid_point((df.iat[p, lat_col], df.iat[p, lon_col]))
                if pt:
                    hints[_text(n)] = pt
        for p, m in zip(positions, resolve(names, hints, by_key)):
            results[p] = m
    shapes = geometries(m.id for m in results if m and m.id is not None)
    df["locality_id"] = [m.id for m in results]
    df["locality_name"] = [m.display for m in results]
    df["locality_status"] = [m.status for m in results]
    df["locality_lat"] = [m.latitude for m in results]
    df["locality_lon"] = [m.longitude for m in results]
    df["locality_geometry"] = [shapes.get(m.id) if m.id is not None else None
                               for m in results]
    return df


def bounds_view(geometries_, pad_km=1.0):
    """(center, zoom) framing GeoJSON geometries on a 512-px-tile map."""
    from shapely.geometry import shape
    boxes = []
    for g in geometries_:
        try:
            boxes.append(shape(g).bounds)
        except Exception:
            continue
    if not boxes:
        return None, None
    min_lon = min(b[0] for b in boxes); min_lat = min(b[1] for b in boxes)
    max_lon = max(b[2] for b in boxes); max_lat = max(b[3] for b in boxes)
    pad = pad_km / 111.0
    min_lat -= pad; max_lat += pad
    min_lon -= pad / max(math.cos(math.radians(min_lat)), 0.2)
    max_lon += pad / max(math.cos(math.radians(max_lat)), 0.2)
    center = {"lat": (min_lat + max_lat) / 2, "lon": (min_lon + max_lon) / 2}
    span = max(max_lon - min_lon,
               (max_lat - min_lat) / max(math.cos(math.radians(center["lat"])), 0.2), 1e-4)
    zoom = math.log2(360.0 / span) - 0.6
    return center, max(5.0, min(13.0, zoom))


# --------------------------------------------------------------------------- #
# CLI: python -m app.localities fetch | import <file> | status | match <names>
# --------------------------------------------------------------------------- #
def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m app.localities",
                                     description="Suburb / locality boundaries")
    sub = parser.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch", help="download from the configured source")
    f.add_argument("--force", action="store_true",
                   help="replace even when the new set is much smaller")
    i = sub.add_parser("import", help="load a GeoJSON / shapefile")
    i.add_argument("path")
    i.add_argument("--force", action="store_true")
    sub.add_parser("status", help="what is loaded")
    m = sub.add_parser("match", help="show how names would match")
    m.add_argument("names", nargs="+")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    database.init_db(backup=False)
    if args.cmd == "fetch":
        ok, msg = run(fetch_and_load, force=args.force)
    elif args.cmd == "import":
        ok, msg = run(import_path, args.path, force=args.force)
    elif args.cmd == "status":
        s = status()
        print(f"{s['count']:,} localities loaded"
              + (f" from {s['source']} at {s['loaded_at']}" if s["count"] else ""))
        return 0
    else:
        for match in resolve(args.names):
            print(f"{match.name!r}: {match.status}"
                  + (f" -> {match.display} ({match.latitude:.4f}, "
                     f"{match.longitude:.4f})" if match.id is not None else "")
                  + (f" [{match.candidates} candidates]" if match.candidates > 1 else ""))
        return 0
    print(msg)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

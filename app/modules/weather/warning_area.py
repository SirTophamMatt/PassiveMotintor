# Passive Monitor — Copyright (c) 2026 SirTophamMatt. All rights reserved.
"""BoM warning areas, traced from the warning's own map image.

BoM draws a thunderstorm / severe weather warning's area by hand, but publishes
it ONLY as a picture: the API detail, the warning XML, the AMOC XML and the CAP
file all name whole forecast districts (probed live on the server 2026-10-25 —
the CAP `<area>` is two `AMOC-AreaCode` geocodes, no `<polygon>`). VicEmergency
republishes the warning with those districts' polygons, which overstate the
area ~3x. The drawn area exists only in the map PNG embedded (base64) in the
warning's `message`, which the weather collector already stores per issue.

So: calibrate the picture, then trace the fill.

* **Calibration is per image and self-checking.** BoM's map is true Mercator
  (x and y scales agree to 0.1%), so a picture needs three numbers: pixels per
  degree and an x/y offset. The town dots (one exact blue) are matched to known
  town coordinates, starting from the reference frame scaled to the image's
  width, and refitted by least squares. An image is used only when at least
  `MIN_TOWNS` dots match with an RMS under `MAX_RMS_PX` — a map BoM has
  re-framed, zoomed or restyled is REFUSED, never traced against the wrong
  frame. On the reference image 16 towns fit within 1.5 km (1 px ~ 1.3 km).
* **The fill** is one exact yellow. Labels, dots and district lines punch holes
  in it, so it is morphologically closed before the outline is taken; the
  legend's "Warning Area" swatch is dropped as the one component that fills its
  own bounding box.

UI-free, no fetching. `trace_message` is pure; `record` stores one result.
"""
import base64
import binascii
import json
import logging
import math
import re
from dataclasses import dataclass, field

import cv2
import numpy as np
from shapely.geometry import Polygon, mapping
from shapely.ops import unary_union

from app import database

log = logging.getLogger(__name__)

# Bump when the tracing changes: results are stored per method, so a better
# tracer re-traces old issues instead of mixing two methods' shapes.
METHOD = "bom-map-v2"  # v2: BoM's real fill colour (v1 matched a screenshot's)

# Statuses stored with every result, so "no area" always says why.
TRACED = "traced"              # geometry present
NO_MAP = "no_map"              # the message carries no image
UNCALIBRATED = "uncalibrated"  # image(s), but not a map we can georeference
NO_AREA = "no_area"            # calibrated map with no warning fill on it
ERROR = "error"

FILL_RGB = (255, 220, 0)       # the "Warning Area" yellow, as BoM stores it
# Summed |R|+|G|+|B| difference. Wide enough for a screenshot's shifted
# colour (the fixture reads 250,221,41 = 47 off); land (255,242,211) is 233 off.
FILL_TOL = 60
DOT_RGB = (48, 0, 252)         # town dots
DOT_TOL = 60

# Reference frame: BoM's Victorian warning map, 707 px wide, fitted to the
# town dots (x = s*lon + x0 ; y = -s*K*mercator(lat) + y0, K = 180/pi).
REF_WIDTH = 707
REF_SCALE = 74.32
REF_X0 = -10453.4
REF_Y0 = -2602.2

MIN_TOWNS = 8
MAX_RMS_PX = 2.0
MIN_PART_PX = 30               # smaller fill specks are anti-aliasing noise
_K = 180.0 / math.pi

# Towns BoM marks with a dot on the Victorian map: (name, lat, lon).
TOWNS = (
    ("Mildura", -34.185, 142.162), ("Ouyen", -35.070, 142.317),
    ("Murrayville", -35.262, 141.183), ("Swan Hill", -35.338, 143.554),
    ("Kerang", -35.734, 143.920), ("Rainbow", -35.900, 141.996),
    ("Warracknabeal", -36.253, 142.393), ("Horsham", -36.717, 142.199),
    ("Edenhope", -37.036, 141.297), ("Stawell", -37.057, 142.780),
    ("Hamilton", -37.744, 142.022), ("Portland", -38.343, 141.604),
    ("Warrnambool", -38.383, 142.483), ("Colac", -38.340, 143.585),
    ("Geelong", -38.147, 144.361), ("Ballarat", -37.562, 143.850),
    ("Bacchus Marsh", -37.675, 144.438), ("Daylesford", -37.342, 144.143),
    ("Maryborough", -37.045, 143.739), ("Bendigo", -36.757, 144.279),
    ("Echuca", -36.141, 144.751), ("Shepparton", -36.380, 145.399),
    ("Euroa", -36.755, 145.571), ("Seymour", -37.026, 145.139),
    ("Yarra Glen", -37.656, 145.374), ("Melbourne", -37.814, 144.963),
    ("Frankston", -38.144, 145.126), ("Wonthaggi", -38.606, 145.591),
    ("Warragul", -38.161, 145.931), ("Traralgon", -38.195, 146.540),
    ("Sale", -38.104, 147.065), ("Mt Baw Baw", -37.837, 146.268),
    ("Mansfield", -37.052, 146.088), ("Wangaratta", -36.358, 146.313),
    ("Wodonga", -36.121, 146.888), ("Corryong", -36.196, 147.902),
    ("Bright", -36.729, 146.961), ("Falls Creek", -36.866, 147.278),
    ("Dargo", -37.457, 147.253), ("Buchan", -37.501, 148.176),
    ("Orbost", -37.708, 148.456), ("Bairnsdale", -37.823, 147.611),
)

_DATA_URI = re.compile(r"data:image/(png|jpe?g);base64,([A-Za-z0-9+/=\s]+)", re.I)


@dataclass
class Calibration:
    scale: float              # pixels per degree of longitude
    x0: float
    y0: float
    towns: list = field(default_factory=list)   # matched town names
    rms_px: float = 0.0

    def to_lonlat(self, x, y):
        lon = (x - self.x0) / self.scale
        m = (self.y0 - y) / (self.scale * _K)
        return lon, math.degrees(2 * math.atan(math.exp(m)) - math.pi / 2)

    def to_px(self, lat, lon):
        return (self.scale * lon + self.x0,
                self.y0 - self.scale * _K * _merc(lat))


@dataclass
class TraceResult:
    status: str
    geometry: dict = None     # GeoJSON geometry (lon/lat), when traced
    area_km2: float = None
    towns: int = 0            # towns matched on the best calibrated image
    rms_px: float = None
    images: int = 0
    detail: str = ""


def _merc(lat):
    return math.log(math.tan(math.pi / 4 + math.radians(lat) / 2))


# --------------------------------------------------------------------------- #
# Images
# --------------------------------------------------------------------------- #
def extract_images(message):
    """Decoded BGR images embedded as data URIs in a warning's HTML."""
    out = []
    for _, b64 in _DATA_URI.findall(message or ""):
        try:
            raw = base64.b64decode(re.sub(r"\s+", "", b64), validate=False)
        except (binascii.Error, ValueError):
            continue
        img = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
        if img is not None:
            out.append(img)
    return out


def _colour_mask(img, rgb, tol):
    bgr = np.array(rgb[::-1], dtype=np.int16)
    return (np.abs(img.astype(np.int16) - bgr).sum(axis=2) <= tol).astype(np.uint8)


# --------------------------------------------------------------------------- #
# Calibration
# --------------------------------------------------------------------------- #
def find_dots(img):
    """Centres (x, y) of the town dots: small, round, solid blobs of DOT_RGB."""
    r = img.shape[1] / REF_WIDTH
    mask = _colour_mask(img, DOT_RGB, DOT_TOL)
    n, _, stats, cents = cv2.connectedComponentsWithStats(mask, connectivity=8)
    dots = []
    for i in range(1, n):
        x, y, w, h, area = stats[i]
        if not (6 * r * r <= area <= 80 * r * r):
            continue
        if abs(int(w) - int(h)) > max(1, round(r)) or area < 0.6 * w * h:
            continue
        dots.append(tuple(cents[i]))
    return dots


def _fit(pairs):
    """Least-squares (scale, x0, y0) from [((x, y), (lat, lon)), ...]."""
    rows, rhs = [], []
    for (x, y), (lat, lon) in pairs:
        rows.append([lon, 1.0, 0.0]); rhs.append(x)
        rows.append([-_K * _merc(lat), 0.0, 1.0]); rhs.append(y)
    sol, *_ = np.linalg.lstsq(np.array(rows), np.array(rhs), rcond=None)
    return float(sol[0]), float(sol[1]), float(sol[2])


def calibrate(img):
    """A Calibration for a BoM Victorian warning map, or None when the picture
    does not match the map frame closely enough to trust."""
    dots = find_dots(img)
    if len(dots) < MIN_TOWNS:
        return None
    r = img.shape[1] / REF_WIDTH
    cal = Calibration(REF_SCALE * r, REF_X0 * r, REF_Y0 * r)
    pts = np.array(dots)
    for tol in (8 * r, 4 * r, 3 * r, 3 * r):
        best = {}  # dot index -> (distance, town)
        for name, lat, lon in TOWNS:
            px, py = cal.to_px(lat, lon)
            d = np.hypot(pts[:, 0] - px, pts[:, 1] - py)
            i = int(np.argmin(d))
            if d[i] <= tol and (i not in best or d[i] < best[i][0]):
                best[i] = (float(d[i]), (name, lat, lon))
        if len(best) < 4:
            return None
        pairs = [(dots[i], (t[1], t[2])) for i, (_, t) in best.items()]
        cal = Calibration(*_fit(pairs), towns=[t[0] for _, t in best.values()])
    resid = [math.hypot(*np.subtract(cal.to_px(lat, lon), xy))
             for xy, (lat, lon) in pairs]
    cal.rms_px = math.sqrt(sum(e * e for e in resid) / len(resid))
    if len(cal.towns) < MIN_TOWNS or cal.rms_px > MAX_RMS_PX * max(r, 1.0):
        return None
    return cal


# --------------------------------------------------------------------------- #
# Tracing
# --------------------------------------------------------------------------- #
def fill_contours(img):
    """Outer pixel contours of the warning fill, legend swatch excluded."""
    r = img.shape[1] / REF_WIDTH
    mask = _colour_mask(img, FILL_RGB, FILL_TOL)
    k = max(3, int(round(7 * r)) | 1)
    closed = cv2.morphologyEx(
        mask, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k)))
    contours, _ = cv2.findContours(closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    out = []
    for c in contours:
        area = cv2.contourArea(c)
        if area < MIN_PART_PX * r * r:
            continue
        _, _, w, h = cv2.boundingRect(c)
        # An axis-aligned box is the legend swatch. contourArea runs through
        # pixel centres, so a filled w x h box measures (w-1) x (h-1).
        if area >= 0.95 * max(1, w - 1) * max(1, h - 1):
            continue
        out.append(c)
    return out


def trace_image(img, cal):
    """The warning area on one calibrated image as a shapely geometry, or None."""
    parts = []
    for c in fill_contours(img):
        c = cv2.approxPolyDP(c, 1.0, True)[:, 0, :]
        if len(c) < 3:
            continue
        poly = Polygon([cal.to_lonlat(float(x), float(y)) for x, y in c])
        if not poly.is_valid:
            poly = poly.buffer(0)
        if not poly.is_empty:
            parts.append(poly)
    return unary_union(parts) if parts else None


def area_km2(geom):
    lat = geom.centroid.y
    return geom.area * 111.32 * 111.32 * math.cos(math.radians(lat))


def _rounded(geom, dp=4):
    """GeoJSON mapping with coordinates rounded (4 dp ~ 11 m, under a pixel)."""
    def walk(v):
        if isinstance(v, (list, tuple)):
            if v and isinstance(v[0], (int, float)):
                return [round(float(x), dp) for x in v]
            return [walk(x) for x in v]
        return v
    g = mapping(geom)
    return {"type": g["type"], "coordinates": walk(g["coordinates"])}


def trace_message(message):
    """Trace every map embedded in a warning's HTML into one TraceResult."""
    images = extract_images(message)
    if not images:
        return TraceResult(NO_MAP, detail="the warning carries no map image")
    geoms, best = [], None
    for img in images:
        cal = calibrate(img)
        if cal is None:
            continue
        if best is None or len(cal.towns) > len(best.towns):
            best = cal
        g = trace_image(img, cal)
        if g is not None:
            geoms.append(g)
    n = len(images)
    if best is None:
        return TraceResult(UNCALIBRATED, images=n,
                           detail="no image matched BoM's Victorian map frame")
    if not geoms:
        return TraceResult(NO_AREA, images=n, towns=len(best.towns),
                           rms_px=round(best.rms_px, 2),
                           detail="map has no warning area marked")
    geom = unary_union(geoms)
    return TraceResult(TRACED, geometry=_rounded(geom),
                       area_km2=round(area_km2(geom), 1), images=n,
                       towns=len(best.towns), rms_px=round(best.rms_px, 2),
                       detail="traced from BoM's warning map (%d towns, "
                              "RMS %.1f px)" % (len(best.towns), best.rms_px))


# --------------------------------------------------------------------------- #
# Storage
# --------------------------------------------------------------------------- #
def record(warning_id, issue_time, message, now):
    """Trace one issued version and store the result (any status). Idempotent:
    a version already traced by this METHOD is left alone. Never raises."""
    try:
        res = trace_message(message)
    except Exception as e:  # a bad image never costs the collection cycle
        log.exception("Weather: tracing %s @ %s failed", warning_id, issue_time)
        res = TraceResult(ERROR, detail=str(e)[:200])
    try:
        database.insert_rows("weather_warning_areas", [{
            "warning_id": warning_id, "issue_time": issue_time, "method": METHOD,
            "status": res.status,
            "geojson": json.dumps(res.geometry) if res.geometry else None,
            "area_km2": res.area_km2, "towns_matched": res.towns,
            "rms_px": res.rms_px, "images": res.images, "detail": res.detail,
            "traced_at": now,
        }], ignore_duplicates=True)
    except Exception:
        log.exception("Weather: storing the area of %s failed", warning_id)
    return res


def backfill(now, limit=50):
    """Trace stored versions this METHOD has not seen yet, newest first —
    covers issues collected before tracing existed and a METHOD bump.
    Bounded per call so a long history is worked off over a few cycles."""
    df = database.read_df(
        "SELECT u.warning_id, u.issue_time, u.message "
        "FROM weather_warning_updates u LEFT JOIN weather_warning_areas a "
        "  ON a.warning_id = u.warning_id AND a.issue_time = u.issue_time "
        "  AND a.method = ? "
        "WHERE a.id IS NULL AND u.issue_time IS NOT NULL "
        "ORDER BY u.issue_time DESC LIMIT ?", [METHOD, int(limit)])
    for r in df.itertuples(index=False):
        record(r.warning_id, r.issue_time, r.message, now)
    return len(df)

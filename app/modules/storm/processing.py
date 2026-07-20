"""Radar echo detection on BoM transparent frame layers.

Ported from the standalone "storm Tracker" project and simplified: frames are
the *echo-only transparency* layer (``IDRxxx.T.<ts>.png``), so every opaque
pixel that matches the BoM rain-rate palette IS radar echo. Detection matches
pixels to the standard 15-level palette, AMALGAMATES nearby fragments into
one cell (a ragged rain band reads as a handful of cells, not dozens — see
``CLUSTER_GAP_PX``), and scores each cell from the levels it contains.

Each moderate/strong cell also gets an **impact area**: an ellipse fitted to
the cell (BoM storm-tracker style) swept along its motion vector for the
prediction window and hulled into one polygon (US warning-polygon style).
The same polygon is georeferenced and exported as GeoJSON by the scraper.
"""
import logging
import math

import cv2
import numpy as np

log = logging.getLogger(__name__)

# The standard BoM radar rain-rate palette, drizzle (1) -> extreme (15), as
# (level, (R, G, B)). Levels 1-2 are barely-drizzle and mostly speckle, so
# cell detection starts at DETECT_MIN_LEVEL; scoring still uses every level.
PALETTE = [
    (1, (245, 245, 255)),
    (2, (180, 180, 255)),
    (3, (120, 120, 255)),
    (4, (20, 20, 255)),
    (5, (0, 216, 195)),
    (6, (0, 150, 144)),
    (7, (0, 102, 102)),
    (8, (255, 255, 0)),
    (9, (255, 200, 0)),
    (10, (255, 150, 0)),
    (11, (255, 100, 0)),
    (12, (255, 0, 0)),
    (13, (200, 0, 0)),
    (14, (120, 0, 0)),
    (15, (40, 0, 0)),
]
PALETTE_TOLERANCE = 30   # per-channel ± when matching a pixel to a level
DETECT_MIN_LEVEL = 3
MODERATE_MIN_LEVEL = 8   # yellow and up
STRONG_MIN_LEVEL = 12    # red and up

MIN_BLOB_AREA_PX = 90    # min real echo pixels for a cell (post-merge)
# Echo fragments separated by up to ~2x this many pixels are amalgamated into
# one cell (morphological close). 6 px = 6 km bridging at a 128 km radar.
CLUSTER_GAP_PX = 6
# Merge/split HYSTERESIS: a region tracked as ONE cell in the previous frame
# stays one cell until its fragments separate beyond ~2x this gap. Without
# this, echoes hovering around CLUSTER_GAP_PX flip between one-storm and
# many-storms every frame, and each flip jumps the centroid and wrecks the
# speed/bearing estimate.
CLUSTER_SPLIT_GAP_PX = 14

PREDICT_MINUTES = 30     # impact-area / prediction projection window
MIN_MOTION_KMH = 5       # below this a cell is treated as stationary

CLASS_STYLE = {  # classification -> (sort priority, BGR draw colour)
    "strong": (1, (40, 40, 220)),
    "moderate": (2, (0, 165, 255)),
    "weak": (3, (90, 200, 90)),
}


def decode_frame(raw: bytes):
    """Decode PNG bytes to (bgr, alpha) uint8 arrays. BoM saves rain-free
    frames as grayscale+alpha and rainy ones as palette/RGB, so every channel
    layout is normalised here. Returns (None, None) if the image is unreadable."""
    buf = np.frombuffer(raw, dtype=np.uint8)
    img = cv2.imdecode(buf, cv2.IMREAD_UNCHANGED)
    if img is None:
        return None, None
    if img.ndim == 2:                                   # grayscale
        return cv2.cvtColor(img, cv2.COLOR_GRAY2BGR), np.full(img.shape, 255, np.uint8)
    if img.shape[2] == 2:                               # grayscale + alpha
        bgr = cv2.cvtColor(img[:, :, 0], cv2.COLOR_GRAY2BGR)
        return bgr, img[:, :, 1]
    if img.shape[2] == 4:                               # BGRA
        return img[:, :, :3].copy(), img[:, :, 3]
    return img, np.full(img.shape[:2], 255, np.uint8)   # BGR


def level_masks(bgr: np.ndarray, alpha: np.ndarray):
    """Per-level binary masks of pixels matching the BoM palette (opaque only)."""
    opaque = alpha > 0
    masks = {}
    for level, (r, g, b) in PALETTE:
        target = np.array([b, g, r], dtype=np.int16)
        lo = np.clip(target - PALETTE_TOLERANCE, 0, 255).astype(np.uint8)
        hi = np.clip(target + PALETTE_TOLERANCE, 0, 255).astype(np.uint8)
        mask = cv2.inRange(bgr, lo, hi)
        mask[~opaque] = 0
        masks[level] = mask
    return masks


def classify_cell(max_level: int, mean_level: float) -> str:
    if max_level >= STRONG_MIN_LEVEL or (max_level >= 10 and mean_level >= 8):
        return "strong"
    if max_level >= MODERATE_MIN_LEVEL:
        return "moderate"
    return "weak"


def _fit_ellipse(contour):
    """(center(x,y), axes(major,minor), angle°) around a contour. fitEllipse
    needs >= 5 points; fall back to the minimum-area rectangle."""
    if len(contour) >= 5:
        (cx, cy), (w, h), ang = cv2.fitEllipse(contour)
    else:
        (cx, cy), (w, h), ang = cv2.minAreaRect(contour)
    # Never let a degenerate axis collapse the ellipse to a line.
    return (float(cx), float(cy)), (max(float(w), 6.0), max(float(h), 6.0)), float(ang)


def _close(mask, gap_px):
    kernel = cv2.getStructuringElement(
        cv2.MORPH_ELLIPSE, (2 * gap_px + 1, 2 * gap_px + 1))
    return cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)


def _measure(contour, detect_mask, masks, km_per_px):
    """Detection dict for one chosen contour, or None if it holds too little
    real echo. The centroid is REFLECTIVITY-WEIGHTED (heavier palette levels
    pull harder), so it follows the storm core rather than the outline shape —
    a merge/split at the fringe barely moves it."""
    contour_mask = np.zeros(detect_mask.shape, dtype=np.uint8)
    cv2.drawContours(contour_mask, [contour], -1, 255, thickness=-1)
    echo = cv2.bitwise_and(detect_mask, contour_mask)
    echo_px = int(np.count_nonzero(echo))
    if echo_px < MIN_BLOB_AREA_PX:  # area = REAL echo, not the merged hull
        return None

    level_counts = {}
    weight = np.zeros(detect_mask.shape, dtype=np.float32)
    for level, mask in masks.items():
        band = cv2.bitwise_and(mask, contour_mask)
        n = int(np.count_nonzero(band))
        if n:
            level_counts[level] = n
            weight[band > 0] = level
    total = sum(level_counts.values())
    if not total:
        return None

    ys, xs = np.nonzero(weight)
    w = weight[ys, xs]
    cx = float((xs * w).sum() / w.sum())
    cy = float((ys * w).sum() / w.sum())

    max_level = max(level_counts)
    mean_level = sum(lvl * n for lvl, n in level_counts.items()) / total
    area_km2 = echo_px * km_per_px * km_per_px
    x, y, bw, bh = cv2.boundingRect(contour)
    intensity = (mean_level * 4.0) + (max_level * 2.5) + min(area_km2 / 15.0, 20.0)

    return {
        "centroid_x": cx,
        "centroid_y": cy,
        "area_px": echo_px,
        "area_km2": float(area_km2),
        "bbox": (int(x), int(y), int(bw), int(bh)),
        "max_level": int(max_level),
        "mean_level": float(mean_level),
        "intensity_score": float(intensity),
        "classification": classify_cell(max_level, mean_level),
        "contour": contour,
        "ellipse": _fit_ellipse(contour),
    }


def detect_cells(bgr: np.ndarray, alpha: np.ndarray, km_per_px: float,
                 prev_labels: np.ndarray | None = None) -> list:
    """Find echo cells in one frame, merging fragments within CLUSTER_GAP_PX,
    with merge/split HYSTERESIS against the previous frame:

    - clusters form at the tight CLUSTER_GAP_PX,
    - but a coarse region (CLUSTER_SPLIT_GAP_PX) whose footprint was exactly
      ONE tracked cell last frame is KEPT as one cell — it only splits once
      its fragments separate beyond the coarse gap. Two previously-separate
      cells are never coarse-merged (their region has two labels).

    ``prev_labels`` is the previous frame's cell-footprint label image from
    the scraper (0 = background, N = tracked cell #N); None disables the
    hysteresis (first frame / tests)."""
    masks = level_masks(bgr, alpha)

    detect_mask = np.zeros(bgr.shape[:2], dtype=np.uint8)
    for level, mask in masks.items():
        if level >= DETECT_MIN_LEVEL:
            detect_mask = cv2.bitwise_or(detect_mask, mask)

    small = np.ones((3, 3), np.uint8)
    detect_mask = cv2.morphologyEx(detect_mask, cv2.MORPH_OPEN, small)

    fine = _close(detect_mask, CLUSTER_GAP_PX)
    coarse = _close(detect_mask, CLUSTER_SPLIT_GAP_PX)

    coarse_contours, _ = cv2.findContours(coarse, cv2.RETR_EXTERNAL,
                                          cv2.CHAIN_APPROX_SIMPLE)
    chosen = []
    for cc in coarse_contours:
        region = np.zeros(coarse.shape, dtype=np.uint8)
        cv2.drawContours(region, [cc], -1, 255, thickness=-1)
        keep_whole = False
        if prev_labels is not None:
            under = np.unique(prev_labels[region > 0])
            keep_whole = len(under[under > 0]) == 1
        if keep_whole:
            chosen.append(cc)
        else:
            sub = cv2.bitwise_and(fine, region)
            sub_contours, _ = cv2.findContours(sub, cv2.RETR_EXTERNAL,
                                               cv2.CHAIN_APPROX_SIMPLE)
            chosen.extend(sub_contours)

    detections = []
    for contour in chosen:
        d = _measure(contour, detect_mask, masks, km_per_px)
        if d is not None:
            detections.append(d)
    return detections


def footprint_labels(tracked_cells: list, shape=(512, 512)) -> np.ndarray:
    """Label image of this frame's tracked-cell footprints (cell #N filled
    with N), fed back into the next detect_cells call as ``prev_labels``."""
    labels = np.zeros(shape, dtype=np.int32)
    for i, cell in enumerate(tracked_cells, start=1):
        if cell.get("contour") is not None:
            cv2.drawContours(labels, [cell["contour"]], -1, i, thickness=-1)
    return labels


# --------------------------------------------------------------------------
# Impact area: fitted ellipse swept along the motion vector, hulled into one
# polygon. This is what gets drawn AND exported (georeferenced) as GeoJSON.
# --------------------------------------------------------------------------

def _ellipse_points(ellipse, delta=12):
    (cx, cy), (w, h), ang = ellipse
    return cv2.ellipse2Poly((int(cx), int(cy)), (int(w / 2), int(h / 2)),
                            int(ang), 0, 360, delta)


def _motion_px(cell, km_per_px, minutes):
    """Displacement (dx, dy) in px over `minutes`, or None if quasi-stationary."""
    speed, bearing = cell.get("speed_kmh"), cell.get("bearing_deg")
    if speed is None or bearing is None or speed < MIN_MOTION_KMH:
        return None
    dist_px = (speed / 60.0) * minutes / km_per_px
    rad = math.radians(bearing)
    return math.sin(rad) * dist_px, -math.cos(rad) * dist_px


def impact_polygon(cell, km_per_px, minutes=PREDICT_MINUTES):
    """The cell's impact area over the next `minutes`: convex hull of its
    fitted ellipse now + the same ellipse displaced along the motion vector.
    Returns an (N, 2) int array of image points (closed implicitly)."""
    ellipse = cell.get("ellipse")
    if ellipse is None:
        return None
    pts = _ellipse_points(ellipse).astype(np.float32)
    motion = _motion_px(cell, km_per_px, minutes)
    if motion is not None:
        pts = np.vstack([pts, pts + np.array(motion, dtype=np.float32)])
    hull = cv2.convexHull(pts.astype(np.int32))
    return hull.reshape(-1, 2)


def _draw_dashed(image, points, colour, thickness=2, dash=10, gap=6):
    """Dashed closed polyline."""
    if len(points) < 2:
        return
    pts = list(points) + [points[0]]
    for i in range(len(pts) - 1):
        x1, y1 = pts[i]
        x2, y2 = pts[i + 1]
        length = math.hypot(x2 - x1, y2 - y1)
        if length == 0:
            continue
        ux, uy = (x2 - x1) / length, (y2 - y1) / length
        dist, drawing = 0.0, True
        while dist < length:
            seg = dash if drawing else gap
            end = min(dist + seg, length)
            if drawing:
                cv2.line(image,
                         (int(x1 + ux * dist), int(y1 + uy * dist)),
                         (int(x1 + ux * end), int(y1 + uy * end)),
                         colour, thickness, cv2.LINE_AA)
            dist += seg
            drawing = not drawing


def _label(image, text, x, y, scale=0.42):
    (tw, th), base = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, scale, 1)
    x = int(min(max(2, x), image.shape[1] - tw - 4))
    y = int(max(th + 4, min(y, image.shape[0] - base - 2)))
    cv2.rectangle(image, (x - 2, y - th - 3), (x + tw + 3, y + base + 2),
                  (0, 0, 0), -1)
    cv2.putText(image, text, (x, y), cv2.FONT_HERSHEY_SIMPLEX, scale,
                (255, 255, 255), 1, cv2.LINE_AA)


def draw_annotated(base: np.ndarray, tracked_cells: list, km_per_px: float,
                   predict_minutes: int = PREDICT_MINUTES) -> np.ndarray:
    """Draw tracked cells onto a composited radar image.

    Deliberately uncluttered: WEAK cells are a thin outline only (no label,
    box or arrow). Moderate/strong cells get the full product — translucent
    impact-area polygon (ellipse swept along motion), dashed projected
    ellipse, motion arrow, track tail and ONE compact label."""
    out = base.copy()

    # Weak cells first: quiet thin outlines under everything else.
    for cell in tracked_cells:
        if cell.get("classification") == "weak" and cell.get("contour") is not None:
            cv2.drawContours(out, [cell["contour"]], -1,
                             CLASS_STYLE["weak"][1], 1)

    severe = [c for c in tracked_cells
              if c.get("classification") in ("strong", "moderate")]

    # Impact-area fills in one translucent pass (so overlaps don't stack dark).
    overlay = out.copy()
    filled = False
    for cell in severe:
        hull = impact_polygon(cell, km_per_px, predict_minutes)
        if hull is not None:
            cv2.fillPoly(overlay, [hull.astype(np.int32)],
                         CLASS_STYLE[cell["classification"]][1])
            filled = True
    if filled:
        out = cv2.addWeighted(overlay, 0.18, out, 0.82, 0)

    for cell in severe:
        colour = CLASS_STYLE[cell["classification"]][1]
        cx, cy = int(cell["centroid_x"]), int(cell["centroid_y"])

        hull = impact_polygon(cell, km_per_px, predict_minutes)
        if hull is not None:
            cv2.polylines(out, [hull.astype(np.int32)], True, colour, 2,
                          cv2.LINE_AA)

        if cell.get("contour") is not None:
            cv2.drawContours(out, [cell["contour"]], -1, colour, 1)

        # Projected ellipse at +predict_minutes, dashed (BoM-tracker motif).
        motion = _motion_px(cell, km_per_px, predict_minutes)
        if motion is not None and cell.get("ellipse") is not None:
            (ex, ey), axes, ang = cell["ellipse"]
            moved = ((ex + motion[0], ey + motion[1]), axes, ang)
            _draw_dashed(out, [tuple(p) for p in _ellipse_points(moved)],
                         colour, thickness=1)
            arrow_end = (int(cx + motion[0]), int(cy + motion[1]))
            cv2.arrowedLine(out, (cx, cy), arrow_end, (0, 0, 0), 3,
                            tipLength=0.25)
            cv2.arrowedLine(out, (cx, cy), arrow_end, colour, 1,
                            tipLength=0.25)

        # Track tail.
        points = cell.get("track_points") or []
        for i in range(1, len(points)):
            cv2.line(out, points[i - 1], points[i], colour,
                     max(1, i - 1) or 1, cv2.LINE_AA)
        cv2.circle(out, (cx, cy), 3, (255, 255, 255), -1)

        speed, bearing = cell.get("speed_kmh"), cell.get("bearing_deg")
        if speed is not None and bearing is not None and speed >= MIN_MOTION_KMH:
            from app.modules.storm.tracker import bearing_to_cardinal
            move = f" {speed:.0f}km/h {bearing_to_cardinal(bearing)}"
        else:
            move = ""
        _label(out,
               f"{cell['classification'].upper()[:3]} "
               f"{cell['intensity_score']:.0f}{move}",
               cx + 10, cy - 8)
    return out

"""Radar echo detection on BoM transparent frame layers.

Ported from the standalone "storm Tracker" project and simplified: frames are
now the *echo-only transparency* layer (``IDRxxx.T.<ts>.png``) rather than a
scrape of the composited page image, so there is no terrain / ocean / legend
to heuristically exclude — every opaque pixel that matches the BoM rain-rate
palette IS radar echo. Detection matches pixels to the standard 15-level
palette, groups them into contours ("cells"), and scores each cell from the
levels it contains.
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

MIN_BLOB_AREA_PX = 90    # ignore tiny speckle contours
MIN_FILL_RATIO = 0.12    # reject sparse boxes formed by thin echo bridges

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


def detect_cells(bgr: np.ndarray, alpha: np.ndarray, km_per_px: float) -> list:
    """Find echo cells in one frame. Each detection dict carries centroid,
    area (px and km²), the palette levels present, an intensity score, a
    classification, and its contour (for drawing)."""
    masks = level_masks(bgr, alpha)

    detect_mask = np.zeros(bgr.shape[:2], dtype=np.uint8)
    for level, mask in masks.items():
        if level >= DETECT_MIN_LEVEL:
            detect_mask = cv2.bitwise_or(detect_mask, mask)

    # Close small gaps so one storm reads as one contour, then drop speckle.
    kernel = np.ones((3, 3), np.uint8)
    detect_mask = cv2.morphologyEx(detect_mask, cv2.MORPH_CLOSE, kernel)
    detect_mask = cv2.morphologyEx(detect_mask, cv2.MORPH_OPEN, kernel)

    contours, _ = cv2.findContours(detect_mask, cv2.RETR_EXTERNAL,
                                   cv2.CHAIN_APPROX_SIMPLE)
    detections = []
    for contour in contours:
        area_px = cv2.contourArea(contour)
        if area_px < MIN_BLOB_AREA_PX:
            continue
        x, y, w, h = cv2.boundingRect(contour)
        if w * h <= 0 or (area_px / (w * h)) < MIN_FILL_RATIO:
            continue
        moments = cv2.moments(contour)
        if moments["m00"] == 0:
            continue
        cx = moments["m10"] / moments["m00"]
        cy = moments["m01"] / moments["m00"]

        contour_mask = np.zeros(detect_mask.shape, dtype=np.uint8)
        cv2.drawContours(contour_mask, [contour], -1, 255, thickness=-1)

        level_counts = {}
        for level, mask in masks.items():
            n = int(np.count_nonzero(cv2.bitwise_and(mask, contour_mask)))
            if n:
                level_counts[level] = n
        total = sum(level_counts.values())
        if not total:
            continue
        max_level = max(level_counts)
        mean_level = sum(lvl * n for lvl, n in level_counts.items()) / total
        area_km2 = area_px * km_per_px * km_per_px

        # Heuristic 0-100ish score: how heavy the echo is, biased by peak
        # intensity, with a capped size contribution so a huge light band
        # doesn't outrank a compact violent cell.
        intensity = (mean_level * 4.0) + (max_level * 2.5) + min(area_km2 / 15.0, 20.0)

        detections.append({
            "centroid_x": float(cx),
            "centroid_y": float(cy),
            "area_px": int(area_px),
            "area_km2": float(area_km2),
            "bbox": (int(x), int(y), int(w), int(h)),
            "max_level": int(max_level),
            "mean_level": float(mean_level),
            "intensity_score": float(intensity),
            "classification": classify_cell(max_level, mean_level),
            "contour": contour,
        })
    return detections


def _shift_contour(contour, dx: int, dy: int):
    shifted = contour.copy()
    shifted[:, 0, 0] += dx
    shifted[:, 0, 1] += dy
    return shifted


def _draw_dashed(image, points, colour, thickness=2, dash=10, gap=6):
    """Dashed closed polyline (the +30 min predicted cell outline)."""
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
    y = max(th + 4, y)
    cv2.rectangle(image, (x - 2, y - th - 3), (x + tw + 3, y + base + 2),
                  (0, 0, 0), -1)
    cv2.putText(image, text, (x, y), cv2.FONT_HERSHEY_SIMPLEX, scale,
                (255, 255, 255), 1, cv2.LINE_AA)


def draw_annotated(base: np.ndarray, tracked_cells: list, km_per_px: float,
                   predict_minutes: int = 30) -> np.ndarray:
    """Draw tracked cells onto a composited radar image: contour + box in the
    classification colour, track tail, motion arrow with speed/bearing, and a
    dashed predicted outline ``predict_minutes`` ahead."""
    out = base.copy()
    for cell in tracked_cells:
        colour = CLASS_STYLE.get(cell.get("classification", "weak"),
                                 CLASS_STYLE["weak"])[1]
        contour = cell.get("contour")
        if contour is not None:
            cv2.drawContours(out, [contour], -1, colour, 2)
        x, y, w, h = cell.get("bbox", (0, 0, 0, 0))
        if w and h:
            cv2.rectangle(out, (x, y), (x + w, y + h), colour, 1)
        cx, cy = int(cell["centroid_x"]), int(cell["centroid_y"])
        cv2.circle(out, (cx, cy), 3, (255, 255, 255), -1)
        _label(out,
               f"{cell['classification'].upper()} {int(cell['intensity_score'])} "
               f"{cell['area_km2']:.0f}km2",
               x, y - 5)

        # Track tail (thickens toward the newest point).
        points = cell.get("track_points") or []
        for i in range(1, len(points)):
            cv2.line(out, points[i - 1], points[i], (0, 0, 0),
                     max(2, i + 1), cv2.LINE_AA)
            cv2.line(out, points[i - 1], points[i], colour,
                     max(1, i), cv2.LINE_AA)

        speed = cell.get("speed_kmh")
        bearing = cell.get("bearing_deg")
        if speed is None or bearing is None or speed < 3:
            continue
        # Compass bearing (0=N, clockwise) -> image vector (y grows downward).
        rad = math.radians(bearing)
        px_per_min = (speed / 60.0) / km_per_px
        arrow_px = max(15, min(int(px_per_min * predict_minutes), 60))
        dx = int(math.sin(rad) * arrow_px)
        dy = int(-math.cos(rad) * arrow_px)
        cv2.arrowedLine(out, (cx, cy), (cx + dx, cy + dy), (0, 0, 0), 4,
                        tipLength=0.3)
        cv2.arrowedLine(out, (cx, cy), (cx + dx, cy + dy), colour, 2,
                        tipLength=0.3)
        _label(out, f"{cell['cell_id']} {speed:.0f}km/h {bearing:.0f}deg",
               cx + 8, cy + 18)

        if contour is not None:
            pred_px = int(px_per_min * predict_minutes)
            pdx = int(math.sin(rad) * pred_px)
            pdy = int(-math.cos(rad) * pred_px)
            predicted = _shift_contour(contour, pdx, pdy)
            pred_points = [tuple(pt[0]) for pt in predicted]
            _draw_dashed(out, pred_points, (0, 0, 0), thickness=3)
            _draw_dashed(out, pred_points, colour, thickness=1)
            _label(out, f"+{predict_minutes}min", cx + pdx, cy + pdy)
    return out

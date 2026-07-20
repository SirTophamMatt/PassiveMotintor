"""Frame-to-frame storm cell tracking.

Ported from the standalone "storm Tracker" with the gaps fixed:

- matching is globally-nearest (all candidate pairs sorted by distance) rather
  than first-come greedy, so two nearby cells can't steal each other's match;
- a cell missed in one frame is kept ("coasting") for MISS_LIMIT frames before
  being dropped, so a momentary detection dropout no longer re-identifies a
  storm under a new id;
- speed uses the real time between the frames' own BoM timestamps and the
  radar's km/px scale, giving km/h instead of "pixels per poll";
- heading is a compass bearing (0 = N, clockwise), smoothed with a circular
  mean over the recent history.
"""
import math
import uuid
from collections import deque


def _distance(x1, y1, x2, y2):
    return math.hypot(x2 - x1, y2 - y1)


def _bearing(x1, y1, x2, y2):
    """Compass bearing of movement in image space (y grows downward)."""
    return (math.degrees(math.atan2(x2 - x1, -(y2 - y1)))) % 360


def _circular_mean(angles):
    if not angles:
        return None
    s = sum(math.sin(math.radians(a)) for a in angles)
    c = sum(math.cos(math.radians(a)) for a in angles)
    if s == 0 and c == 0:
        return None
    return math.degrees(math.atan2(s, c)) % 360


def bearing_to_cardinal(bearing):
    if bearing is None:
        return ""
    points = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
    return points[int((bearing + 22.5) // 45) % 8]


class CellTracker:
    MISS_LIMIT = 3  # frames a cell may go undetected before it is dropped

    def __init__(self, max_match_distance_px=45, history_length=5):
        self.max_match_distance_px = max_match_distance_px
        self.history_length = history_length
        self.cells = {}  # cell_id -> {x, y, last_ts, misses, history: deque}

    def update(self, detections, frame_ts, km_per_px):
        """Match ``detections`` (from processing.detect_cells) against known
        cells. ``frame_ts`` is the frame's own observation datetime. Returns
        the tracked-cell dicts for this frame (detections enriched with id,
        smoothed speed/bearing and track points)."""
        # Globally-nearest matching: consider every (cell, detection) pair
        # within range, closest first, each side used at most once.
        pairs = []
        for cell_id, cell in self.cells.items():
            for i, d in enumerate(detections):
                dist = _distance(cell["x"], cell["y"],
                                 d["centroid_x"], d["centroid_y"])
                if dist <= self.max_match_distance_px:
                    pairs.append((dist, cell_id, i))
        pairs.sort(key=lambda p: p[0])

        matched_cells, matched_dets = set(), set()
        assignment = {}
        for dist, cell_id, i in pairs:
            if cell_id in matched_cells or i in matched_dets:
                continue
            assignment[i] = cell_id
            matched_cells.add(cell_id)
            matched_dets.add(i)

        tracked = []
        for i, d in enumerate(detections):
            cell_id = assignment.get(i)
            if cell_id is None:
                cell_id = f"CELL-{uuid.uuid4().hex[:6].upper()}"
                history = deque(maxlen=self.history_length)
                speed = bearing = None
                status = "new"
            else:
                cell = self.cells[cell_id]
                history = cell["history"]
                dt_hours = max((frame_ts - cell["last_ts"]).total_seconds(),
                               1.0) / 3600.0
                dist_km = _distance(cell["x"], cell["y"], d["centroid_x"],
                                    d["centroid_y"]) * km_per_px
                history.append({
                    "x": d["centroid_x"], "y": d["centroid_y"],
                    "speed": dist_km / dt_hours,
                    "bearing": _bearing(cell["x"], cell["y"],
                                        d["centroid_x"], d["centroid_y"]),
                })
                speeds = [h["speed"] for h in history if h["speed"] is not None]
                bearings = [h["bearing"] for h in history
                            if h["bearing"] is not None]
                speed = sum(speeds) / len(speeds) if speeds else None
                bearing = _circular_mean(bearings)
                status = "active"
            if not history or status == "new":
                history.append({"x": d["centroid_x"], "y": d["centroid_y"],
                                "speed": None, "bearing": None})

            self.cells[cell_id] = {
                "x": d["centroid_x"], "y": d["centroid_y"],
                "last_ts": frame_ts, "misses": 0, "history": history,
            }
            tracked.append({
                **d,
                "cell_id": cell_id,
                "speed_kmh": speed,
                "bearing_deg": bearing,
                "status": status,
                "track_points": [(int(h["x"]), int(h["y"])) for h in history],
            })

        # Unmatched known cells coast for a few frames, then drop.
        for cell_id in list(self.cells):
            if cell_id in matched_cells or self.cells[cell_id]["last_ts"] == frame_ts:
                continue
            self.cells[cell_id]["misses"] += 1
            if self.cells[cell_id]["misses"] > self.MISS_LIMIT:
                del self.cells[cell_id]
        return tracked

    def active_cell_ids(self):
        return set(self.cells)

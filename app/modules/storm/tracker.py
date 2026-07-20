"""Frame-to-frame storm cell tracking.

Ported from the standalone "storm Tracker" with the gaps fixed:

- matching is globally-nearest (all candidate pairs sorted by distance) rather
  than first-come greedy, so two nearby cells can't steal each other's match;
- a cell missed in one frame is kept ("coasting") for MISS_LIMIT frames before
  being dropped, so a momentary detection dropout no longer re-identifies a
  storm under a new id;
- speed/heading come from a LEAST-SQUARES velocity fit over the whole recent
  track (timestamped positions), not from per-frame hops. At radar cadence a
  storm only moves a few px per frame, so a hop bearing is dominated by
  centroid noise; a fit over ~30 min of positions divides that noise by the
  baseline. Heading is a compass bearing (0 = N, clockwise) and is reported
  only when the fitted speed clears MIN_REPORT_KMH — a quasi-stationary
  cell's "direction" is pure noise.
"""
import math
import uuid
from collections import deque


def _distance(x1, y1, x2, y2):
    return math.hypot(x2 - x1, y2 - y1)


def bearing_to_cardinal(bearing):
    if bearing is None:
        return ""
    points = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
    return points[int((bearing + 22.5) // 45) % 8]


def _fit_velocity(history):
    """Least-squares (vx, vy) in px/hour over the track history — one straight
    line through every timestamped position. Needs >= 2 points spread in time."""
    if len(history) < 2:
        return None
    t0 = history[0]["t"]
    ts = [(h["t"] - t0) / 3600.0 for h in history]
    xs = [h["x"] for h in history]
    ys = [h["y"] for h in history]
    n = len(ts)
    tm, xm, ym = sum(ts) / n, sum(xs) / n, sum(ys) / n
    denom = sum((t - tm) ** 2 for t in ts)
    if denom == 0:
        return None
    vx = sum((t - tm) * (x - xm) for t, x in zip(ts, xs)) / denom
    vy = sum((t - tm) * (y - ym) for t, y in zip(ts, ys)) / denom
    return vx, vy


class CellTracker:
    MISS_LIMIT = 3       # frames a cell may go undetected before it is dropped
    MAX_SPEED_KMH = 160  # a jump implying more than this is a mismatch
                         # artefact — the position sample is discarded so it
                         # cannot bend the fitted track
    MIN_REPORT_KMH = 5   # below this the fitted direction is noise: report
                         # the speed but no bearing

    # 8 frames ≈ 40 min of positions: at typical cell speeds this cuts the
    # fitted-bearing error to ~3-5° under realistic centroid noise (vs ~15°
    # for per-hop bearings), at the cost of ~40 min lag on a genuine turn.
    def __init__(self, max_match_distance_px=45, history_length=8):
        self.max_match_distance_px = max_match_distance_px
        self.history_length = history_length
        self.cells = {}  # cell_id -> {x, y, area_px, last_ts, misses, history}

    def _match_range(self, cell, det):
        """Allowed match distance for a pair: large cells (merged complexes)
        get more slack, since a partial merge/split legitimately moves the
        centroid further than a compact cell could travel in one frame."""
        area = max(cell.get("area_px") or 0, det.get("area_px") or 0)
        return max(self.max_match_distance_px, 0.6 * math.sqrt(area))

    def _motion(self, history, km_per_px):
        """(speed_kmh, bearing_deg) from the fitted velocity; bearing is None
        when the cell is effectively stationary."""
        v = _fit_velocity(history)
        if v is None:
            return None, None
        vx, vy = v
        speed = math.hypot(vx, vy) * km_per_px
        if speed < self.MIN_REPORT_KMH:
            return speed, None
        bearing = (math.degrees(math.atan2(vx, -vy))) % 360
        return speed, bearing

    def update(self, detections, frame_ts, km_per_px):
        """Match ``detections`` (from processing.detect_cells) against known
        cells. ``frame_ts`` is the frame's own observation datetime. Returns
        the tracked-cell dicts for this frame (detections enriched with id,
        fitted speed/bearing and track points)."""
        # Globally-nearest matching: consider every (cell, detection) pair
        # within range, closest first, each side used at most once.
        pairs = []
        for cell_id, cell in self.cells.items():
            for i, d in enumerate(detections):
                dist = _distance(cell["x"], cell["y"],
                                 d["centroid_x"], d["centroid_y"])
                if dist <= self._match_range(cell, d):
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

        t_sec = frame_ts.timestamp()
        tracked = []
        for i, d in enumerate(detections):
            cell_id = assignment.get(i)
            point = {"t": t_sec, "x": d["centroid_x"], "y": d["centroid_y"]}
            if cell_id is None:
                cell_id = f"CELL-{uuid.uuid4().hex[:6].upper()}"
                history = deque([point], maxlen=self.history_length)
                speed = bearing = None
                status = "new"
            else:
                cell = self.cells[cell_id]
                history = cell["history"]
                last = history[-1] if history else None
                if last is not None:
                    dt_hours = max(t_sec - last["t"], 1.0) / 3600.0
                    jump_kmh = (_distance(last["x"], last["y"],
                                          point["x"], point["y"])
                                * km_per_px / dt_hours)
                    if jump_kmh <= self.MAX_SPEED_KMH:
                        history.append(point)
                    # else: implausible jump — drop the sample entirely so it
                    # cannot bend the fitted track (the cell's match position
                    # below still updates, so tracking continues).
                else:
                    history.append(point)
                speed, bearing = self._motion(history, km_per_px)
                status = "active"

            self.cells[cell_id] = {
                "x": d["centroid_x"], "y": d["centroid_y"],
                "area_px": d.get("area_px"),
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

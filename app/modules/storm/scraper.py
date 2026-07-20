"""BoM radar storm-cell scraper.

Each cycle probes the BoM radar frame URLs for the last few minutes
(``IDRxxx.T.<YYYYMMDDHHMM>.png`` — the transparent echo-only layer, published
about every 5 minutes), processes any frame not already in ``storm_frames``
(de-dup on the frame's OWN timestamp, so re-polling never double-processes),
runs cell detection + tracking, records cells/alerts, saves an annotated
composite frame for the dashboard loop, and writes a ``storm_timeseries``
heartbeat row.

No Selenium: the frame naming is deterministic, so a handful of cheap GETs
(most returning 404 for minutes with no frame) replaces the old headless-
Chrome page scrape entirely. The static map underlay (background / topography
/ locations layers) is fetched once per radar and cached in memory.
"""
import logging
import os
from datetime import datetime, timedelta, timezone

import urllib.error
import urllib.request

import cv2
import numpy as np

from app import database
from app.config import BASE_DIR, load_config
from app.modules.storm import processing
from app.modules.storm.tracker import CellTracker, bearing_to_cardinal

log = logging.getLogger(__name__)

FRAME_URL = "https://reg.bom.gov.au/radar/{radar_id}.T.{stamp}.png"
LAYER_URL = ("https://reg.bom.gov.au/products/radar_transparencies/"
             "{radar_id}.{layer}.png")
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

PROBE_WINDOW_MINUTES = 16   # how far back each cycle looks for new frames
KEEP_ANNOTATED_FRAMES = 24  # loop length kept on disk (~2 hours of frames)

STORM_FRAMES_DIR = os.path.join(BASE_DIR, "storm_frames")

# In-process state (like the flood module's backfill cache): one tracker per
# radar, the cached map underlay, and each cell's best-seen severity for
# change-only alert rows.
_trackers = {}
_base_cache = {}
_cell_severity = {}

_SEVERITY = {"strong": 1, "moderate": 2}


def km_per_px(radar_id):
    """BoM product ids encode the zoom in the last digit (1=512 km range,
    2=256, 3=128, 4=64); frames are 512 px across the full diameter."""
    ranges = {"1": 512, "2": 256, "3": 128, "4": 64}
    range_km = ranges.get(str(radar_id)[-1], 128)
    return (2 * range_km) / 512.0


def _fetch(url, timeout=20):
    """GET a URL, returning None on 404 (a minute with no frame). urllib, not
    requests — like the weather module, it uses the OS certificate store, which
    matters behind TLS-inspecting proxies."""
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def _base_image(radar_id):
    """Composite the static map layers once per radar (cached). Falls back to
    a plain dark canvas if BoM's transparency layers can't be fetched."""
    if radar_id in _base_cache:
        return _base_cache[radar_id]
    base = np.full((512, 512, 3), 30, dtype=np.uint8)
    for layer in ("background", "topography", "locations"):
        try:
            raw = _fetch(LAYER_URL.format(radar_id=radar_id, layer=layer))
        except (urllib.error.URLError, OSError) as e:
            log.warning("Radar layer %s fetch failed: %s", layer, e)
            raw = None
        if not raw:
            continue
        bgr, alpha = processing.decode_frame(raw)
        if bgr is None or bgr.shape[:2] != base.shape[:2]:
            continue
        mask = (alpha > 0)
        base[mask] = bgr[mask]
    _base_cache[radar_id] = base
    return base


def _overlay(base, bgr, alpha):
    out = base.copy()
    mask = alpha > 0
    out[mask] = bgr[mask]
    return out


def _local_ts(frame_dt_utc):
    """Frame UTC datetime -> the app-wide local 'YYYY-MM-DD HH:MM:SS' string."""
    return (frame_dt_utc.replace(tzinfo=timezone.utc).astimezone()
            .replace(tzinfo=None).isoformat(sep=" ", timespec="seconds"))


def _new_frames(radar_id):
    """Probe the last PROBE_WINDOW_MINUTES of minute-stamped frame URLs and
    return [(frame_dt_utc, png_bytes)] for frames not yet processed, oldest
    first. Minutes already in storm_frames are skipped without a request."""
    now = datetime.utcnow().replace(second=0, microsecond=0)
    candidates = [now - timedelta(minutes=m)
                  for m in range(1, PROBE_WINDOW_MINUTES + 1)]
    seen = set(database.read_df(
        "SELECT frame_ts FROM storm_frames WHERE radar_id = ? AND frame_ts >= ?",
        [radar_id, _local_ts(min(candidates))])["frame_ts"])

    frames = []
    for dt in sorted(candidates):
        if _local_ts(dt) in seen:
            continue
        stamp = dt.strftime("%Y%m%d%H%M")
        try:
            raw = _fetch(FRAME_URL.format(radar_id=radar_id, stamp=stamp))
        except (urllib.error.URLError, OSError) as e:
            log.warning("Radar frame %s fetch failed: %s", stamp, e)
            continue
        if raw:
            frames.append((dt, raw))
    return frames


def _prune_annotated(radar_id):
    try:
        files = sorted(f for f in os.listdir(STORM_FRAMES_DIR)
                       if f.startswith(f"annotated_{radar_id}_"))
        for name in files[:-KEEP_ANNOTATED_FRAMES]:
            os.remove(os.path.join(STORM_FRAMES_DIR, name))
    except OSError as e:
        log.warning("Annotated frame prune failed: %s", e)


def _record_alerts(tracked, frame_ts_local, radar_id=None):
    """One storm_alerts row per state CHANGE only: a cell reaching moderate/
    strong for the first time, or escalating. A persisting strong cell adds
    nothing (the old project logged it every frame)."""
    rows = []
    for cell in tracked:
        sev = _SEVERITY.get(cell["classification"])
        if sev is None:
            continue
        prev = _cell_severity.get(cell["cell_id"])
        if prev is not None and prev <= sev:
            continue
        _cell_severity[cell["cell_id"]] = sev
        movement = ""
        if cell.get("speed_kmh") is not None and cell.get("bearing_deg") is not None:
            movement = (f", moving {bearing_to_cardinal(cell['bearing_deg'])} "
                        f"at {cell['speed_kmh']:.0f} km/h")
        rows.append({
            "timestamp": frame_ts_local,
            "cell_id": cell["cell_id"],
            "alert_type": "new_cell" if prev is None else "escalation",
            "classification": cell["classification"],
            "message": (f"{cell['cell_id']} {cell['classification'].upper()}"
                        + (f" on {radar_id}" if radar_id else "")
                        + f" — score {cell['intensity_score']:.0f}, "
                        f"~{cell['area_km2']:.0f} km²{movement}"),
        })
    database.insert_rows("storm_alerts", rows)
    return len(rows)


def radar_ids(cfg=None):
    """The configured radar list. Accepts the legacy single ``radar_id``
    string so an old config.json keeps working."""
    cfg = cfg or load_config()
    ids = cfg["storm"].get("radar_ids") or cfg["storm"].get("radar_id") or "IDR023"
    return [ids] if isinstance(ids, str) else list(ids)


def _process_radar(radar_id, now_local):
    """Fetch + process all new frames for one radar. Returns
    (frames_processed, last frame's tracked cells)."""
    scale = km_per_px(radar_id)
    tracker = _trackers.setdefault(radar_id, CellTracker())
    frames = _new_frames(radar_id)

    last_tracked = []
    for frame_dt, raw in frames:
        frame_ts_local = _local_ts(frame_dt)
        bgr, alpha = processing.decode_frame(raw)
        if bgr is None:
            log.warning("Undecodable radar frame at %s", frame_ts_local)
            continue

        detections = processing.detect_cells(bgr, alpha, scale)
        tracked = tracker.update(detections, frame_dt, scale)
        last_tracked = tracked

        database.insert_rows("storm_frames", [{
            "radar_id": radar_id, "frame_ts": frame_ts_local,
            "fetched_at": now_local, "cells_detected": len(tracked),
        }], ignore_duplicates=True)
        database.insert_rows("storm_cells", [{
            "cell_id": c["cell_id"], "radar_id": radar_id,
            "frame_ts": frame_ts_local,
            "centroid_x": c["centroid_x"], "centroid_y": c["centroid_y"],
            "area_km2": c["area_km2"], "max_level": c["max_level"],
            "mean_level": c["mean_level"],
            "intensity_score": c["intensity_score"],
            "classification": c["classification"],
            "speed_kmh": c["speed_kmh"], "bearing_deg": c["bearing_deg"],
            "status": c["status"],
        } for c in tracked])
        _record_alerts(tracked, frame_ts_local, radar_id)

        annotated = processing.draw_annotated(
            _overlay(_base_image(radar_id), bgr, alpha), tracked, scale)
        stamp = frame_dt.strftime("%Y%m%d%H%M")
        cv2.imwrite(os.path.join(
            STORM_FRAMES_DIR, f"annotated_{radar_id}_{stamp}.png"), annotated)

    if frames:
        _prune_annotated(radar_id)
    log.info("Storm fetch (%s): %d new frame(s), %d active cell(s)",
             radar_id, len(frames), len(last_tracked))
    return len(frames), last_tracked


def fetch_storm_data():
    """One collection cycle over every configured radar. Returns the total
    number of new frames processed."""
    cfg = load_config()
    os.makedirs(STORM_FRAMES_DIR, exist_ok=True)
    now_local = datetime.now().isoformat(sep=" ", timespec="seconds")

    total_frames = 0
    cells = []
    for radar_id in radar_ids(cfg):
        n, last_tracked = _process_radar(radar_id, now_local)
        total_frames += n
        cells.extend(last_tracked)

    # Forget best-severity state for cells every tracker has dropped, so a
    # storm that dies and later reforms nearby alerts again.
    live = set()
    for tracker in _trackers.values():
        live |= tracker.active_cell_ids()
    for cell_id in [c for c in _cell_severity if c not in live]:
        del _cell_severity[cell_id]

    strong = sum(1 for c in cells if c["classification"] == "strong")
    moderate = sum(1 for c in cells if c["classification"] == "moderate")
    database.insert_rows("storm_timeseries", [{
        "timestamp": now_local,
        "frames_processed": total_frames,
        "active_cells": len(cells) if total_frames else None,
        "strong_cells": strong if total_frames else None,
        "moderate_cells": moderate if total_frames else None,
        "max_intensity": (max((c["intensity_score"] for c in cells),
                              default=0.0) if total_frames else None),
    }])
    return total_frames

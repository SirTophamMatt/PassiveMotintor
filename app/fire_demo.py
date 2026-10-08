# Passive Monitor — Copyright (c) 2026 SirTophamMatt. All rights reserved.
"""Fire wall test run (/wall/fire/test): a simulated fire, entirely in memory.

Lets the fire wall be watched working on a deployed server outside fire season.
Nothing here touches the database, the collectors, the Intelligence Feed,
webhooks, the ticker or alert sounds — the scenario is generated from the
clock on every refresh and handed to the SAME model code the live wall uses
(`fire_areas.assemble` -> `members_from` / `build_areas`, `_change_for` for the
burn-area diff), so what it proves is the real linking, diffing, breathing and
takeover path, minus only the VicEmergency feed itself.

The scenario repeats every `CYCLE_SECONDS`, one new burn-area shape per
`STEP_SECONDS` (the live collector's 3-minute cadence):

* **TEST Fire Alpha** grows every step, driven east — red growth, the newest
  step breathing, and a growth takeover each step.
* **TEST Fire Bravo**, 15 km east, is re-mapped SMALLER at step 3 — blue.
* **TEST warning** covers both, so they read as ONE area of operation:
  Advice -> Watch and Act at step 2 -> Emergency Warning at step 4, each a
  takeover.
* **TEST tree down** near Alpha attaches to that area as context.
* **TEST Grass Fire Charlie** near Melton with its own Advice — a second area.
* **TEST flood Advice** in Gippsland — a non-fire warning, so NOT an area.

Admin-only (fake Emergency Warnings on a public page could be screenshotted
and passed on as real) and labelled TEST throughout.
"""
import json
import math
import time
from datetime import datetime, timedelta

import pandas as pd

from app import fire_areas

STEP_SECONDS = 180
STEPS = 6
CYCLE_SECONDS = STEP_SECONDS * STEPS

ALPHA = (147.74, -35.99)
BRAVO = (147.93, -36.02)
CHARLIE = (144.60, -37.71)


def _stamp(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def blob(lon, lat, rx, ry, points=16, wobble=0.12, seed=0):
    """An irregular, roughly elliptical fire-scar polygon (degrees)."""
    ring = []
    for i in range(points):
        a = 2 * math.pi * i / points
        r = 1 + wobble * math.sin(3 * a + seed) * math.cos(2 * a - seed)
        ring.append([lon + rx * r * math.cos(a), lat + ry * r * math.sin(a)])
    ring.append(ring[0])
    return {"type": "Polygon", "coordinates": [ring]}


def alpha_shape(step):
    # Grows and runs east with the wind.
    return blob(ALPHA[0] + 0.006 * step, ALPHA[1], 0.022 + 0.009 * step,
                0.016 + 0.004 * step, seed=1)


def bravo_shape(step):
    # Re-mapped smaller from step 3: a reduction, drawn blue.
    return blob(BRAVO[0], BRAVO[1], 0.028 if step < 3 else 0.019,
                0.02 if step < 3 else 0.015, seed=2)


def warning_level(step):
    if step >= 4:
        return "Emergency Warning"
    if step >= 2:
        return "Watch and Act"
    return "Advice"


def clock(now_ts=None):
    """(cycle start, current step 0..STEPS-1, epoch of the next step)."""
    now_ts = time.time() if now_ts is None else now_ts
    start = now_ts - (now_ts % CYCLE_SECONDS)
    step = int((now_ts - start) // STEP_SECONDS)
    return start, step, start + (step + 1) * STEP_SECONDS


def _with_point(poly, lon, lat):
    return {"type": "GeometryCollection",
            "geometries": [{"type": "Point", "coordinates": [lon, lat]}, poly]}


def _row(sid, location, lat, lon, geometry=None, feed_type="incident",
         category1="Fire", category2=None, event=None, level=None,
         status="Going", size=None, org="VIC/CFA", first_seen=None):
    return {"source_id": sid, "feed_type": feed_type, "category1": category1,
            "category2": category2, "event": event, "warning_level": level,
            "severity": None, "status": status, "size": size,
            "resources": None, "location": location, "source_org": org,
            "action": None, "headline": location, "url": None,
            "latitude": lat, "longitude": lon,
            "geometry": json.dumps(geometry) if geometry else None,
            "created": first_seen, "updated": first_seen,
            "first_seen": first_seen, "last_seen": first_seen, "resolved": 0}


def incidents(now_ts=None):
    """The scenario's active VicEmergency-shaped rows at this moment."""
    start, step, _ = clock(now_ts)
    at = lambda k: _stamp(datetime.fromtimestamp(start + k * STEP_SECONDS))
    level = warning_level(step)
    # A warning is "first seen" at its latest level change, so its area
    # breathes like a newly issued warning does on the live maps.
    level_since = at(4 if step >= 4 else 2 if step >= 2 else 0)
    box = {"type": "Polygon", "coordinates": [[
        [147.62, -36.10], [148.03, -36.10], [148.03, -35.90],
        [147.62, -35.90], [147.62, -36.10]]]}
    rows = [
        _row("TEST-alpha", "TEST Fire Alpha", ALPHA[1], ALPHA[0],
             _with_point(alpha_shape(step), *ALPHA), size="Large",
             first_seen=at(0)),
        _row("TEST-bravo", "TEST Fire Bravo", BRAVO[1], BRAVO[0],
             _with_point(bravo_shape(step), *BRAVO), status="Under Control",
             size="Medium", first_seen=at(0)),
        _row("TEST-warning", "TEST warning — Alpha and Bravo area", -36.0, 147.82,
             _with_point(box, 147.82, -36.0), feed_type="warning",
             category1=level, event="Bushfire", level=level,
             first_seen=level_since),
        _row("TEST-tree", "TEST tree down — Murray River Rd", -36.03, 147.68,
             category1="Tree Down", status="Responding", org="VIC/SES",
             first_seen=at(0)),
        _row("TEST-charlie", "TEST Grass Fire Charlie", CHARLIE[1], CHARLIE[0],
             size="Small", first_seen=at(0)),
        _row("TEST-charlie-advice", "TEST Advice — Charlie", CHARLIE[1] - 0.01,
             CHARLIE[0] + 0.01, feed_type="warning", category1="Advice",
             event="Grass Fire", level="Advice", first_seen=at(0)),
        _row("TEST-flood", "TEST flood Advice — Gippsland", -38.19, 146.54,
             feed_type="warning", category1="Advice", event="Riverine Flood",
             level="Advice", org="VIC/SES", first_seen=at(0)),
    ]
    return pd.DataFrame(rows)


def _history(shape_at, step, start):
    rows = []
    for k in range(step + 1):
        mp = fire_areas.polygons_only(shape_at(k))
        rows.append({"recorded_at": _stamp(datetime.fromtimestamp(
                         start + k * STEP_SECONDS)),
                     "geometry": json.dumps(mp), "area_ha": fire_areas.area_ha(mp)})
    # Collapse unchanged consecutive shapes, as the change-only table would.
    out = [rows[0]]
    for r in rows[1:]:
        if r["geometry"] != out[-1]["geometry"]:
            out.append(r)
    return pd.DataFrame(out)


def snapshot(link_km=fire_areas.DEFAULT_LINK_KM,
             window_hours=fire_areas.DEFAULT_WINDOW_HOURS, now_ts=None):
    """A fire-wall snapshot of the simulated scenario (same shape as
    `fire_areas.compute`), plus a `test` block describing the cycle."""
    now_ts = time.time() if now_ts is None else now_ts
    now = datetime.fromtimestamp(now_ts)
    start, step, next_ts = clock(now_ts)
    snap = {"at": now, "ok": True, "error": None, "areas": [], "changes": {},
            "incidents": pd.DataFrame(), "burn": pd.DataFrame(),
            "link_km": link_km, "window_hours": window_hours,
            "test": {"step": step + 1, "steps": STEPS,
                     "next": datetime.fromtimestamp(next_ts)}}
    if not fire_areas.HAVE_SHAPELY:
        snap.update(ok=False, error="shapely is not installed")
        return snap
    cutoff = now - timedelta(hours=window_hours)
    changes = {
        sid: fire_areas._change_for(sid, _history(shape, step, start), cutoff)
        for sid, shape in (("TEST-alpha", alpha_shape), ("TEST-bravo", bravo_shape))}
    burn = pd.DataFrame(columns=["source_id", "geometry", "created", "updated"])
    return fire_areas.assemble(snap, incidents(now_ts), burn, changes)


def describe(snap):
    """One line for the wall header while the test runs."""
    t = snap.get("test") or {}
    return (f"TEST RUN — simulated fires, nothing here is real · step "
            f"{t.get('step')} of {t.get('steps')} · next change "
            f"{t['next']:%H:%M:%S}" if t else "TEST RUN")

# Passive Monitor — Copyright (c) 2026 SirTophamMatt. All rights reserved.
"""Storm wall test run (/wall/storm/test): a simulated storm, entirely in memory.

Lets the storm wall be watched working on a deployed server between storms.
Nothing here touches the database, the collectors, the Intelligence Feed,
webhooks, the ticker or alert sounds — the scenario is generated from the clock
on every refresh and handed to the SAME model code the live wall uses
(`storm_areas.assemble` -> `scope` / `members_from` / `build_areas`), so what it
proves is the real linking, attaching and takeover path, minus only the feeds.

The scenario repeats every `CYCLE_SECONDS`, one step per `STEP_SECONDS`:

* **Dandenong Ranges** — SES tree-down / building-damage jobs, two more every
  step, chained along the storm's path into ONE area; crossing 5 and 10
  incidents are takeovers. A Severe Thunderstorm warning over it goes Advice ->
  Watch and Act (step 3) -> Emergency Warning (step 5); a road closed by fallen
  trees, a power outage and (from step 4) a strong storm cell attach to it.
* **Geelong** — a second, separate cluster of three jobs, with a gauge at Minor
  from step 3.
* **Shepparton** — a flood Watch and Act with no jobs near it: an area of its
  own, because that level asks people to act.
* **Gippsland** — a flash-flood Advice with no jobs: on the map, not an area.
* An SES rescue near Bendigo (not weather — left out unless "weather-related
  only" is off) and a CFA grass fire beside the storm (never a storm job).

Admin-only and labelled TEST throughout, like the fire wall's test run.
"""
import json
import time
from datetime import datetime

import pandas as pd

from app import storm_areas

STEP_SECONDS = 180
STEPS = 6
CYCLE_SECONDS = STEP_SECONDS * STEPS

# Jobs in the order they come in: (locality, kind, lon, lat).
RANGES_JOBS = [
    ("Belgrave", "Tree Down", 145.355, -37.909),
    ("Belgrave", "Building Damage", 145.360, -37.912),
    ("Tecoma", "Tree Down", 145.343, -37.906),
    ("Upwey", "Tree Down", 145.331, -37.903),
    ("Selby", "Building Damage", 145.371, -37.917),
    ("Upwey", "Flooding", 145.322, -37.899),
    ("Kallista", "Tree Down", 145.375, -37.885),
    ("Belgrave", "Tree Down", 145.351, -37.914),
    ("Monbulk", "Building Damage", 145.403, -37.876),
    ("Ferny Creek", "Tree Down", 145.343, -37.880),
    ("Sassafras", "Tree Down", 145.355, -37.866),
    ("Olinda", "Tree Down", 145.369, -37.858),
]
GEELONG_JOBS = [
    ("Belmont", "Building Damage", 144.341, -38.175),
    ("Highton", "Tree Down", 144.312, -38.170),
    ("Grovedale", "Flooding", 144.334, -38.198),
]


def _stamp(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def clock(now_ts=None):
    """(cycle start, current step 0..STEPS-1, epoch of the next step)."""
    now_ts = time.time() if now_ts is None else now_ts
    start = now_ts - (now_ts % CYCLE_SECONDS)
    step = int((now_ts - start) // STEP_SECONDS)
    return start, step, start + (step + 1) * STEP_SECONDS


def warning_level(step):
    if step >= 4:
        return "Emergency Warning"
    if step >= 2:
        return "Watch and Act"
    return "Advice"


def _box(lon0, lat0, lon1, lat1):
    return {"type": "Polygon", "coordinates": [[
        [lon0, lat0], [lon1, lat0], [lon1, lat1], [lon0, lat1], [lon0, lat0]]]}


def _row(sid, location, lat, lon, feed_type="incident", category1="Tree Down",
         category2=None, event=None, level=None, org="VIC/SES",
         status="Responding", geometry=None, first_seen=None):
    return {"source_id": sid, "feed_type": feed_type, "category1": category1,
            "category2": category2, "event": event, "warning_level": level,
            "severity": None, "status": status, "size": None, "resources": None,
            "location": location, "source_org": org, "action": None,
            "headline": location, "url": None, "latitude": lat, "longitude": lon,
            "geometry": json.dumps(geometry) if geometry else None,
            "created": first_seen, "updated": first_seen,
            "first_seen": first_seen, "last_seen": first_seen, "resolved": 0}


def frames(now_ts=None):
    """The scenario's rows at this moment, shaped like `storm_areas.load_frames`."""
    start, step, _ = clock(now_ts)
    at = lambda k: _stamp(datetime.fromtimestamp(start + k * STEP_SECONDS))
    level = warning_level(step)
    level_since = at(4 if step >= 4 else 2 if step >= 2 else 0)
    rows = []
    for i, (place, kind, lon, lat) in enumerate(RANGES_JOBS[:2 * (step + 1)]):
        rows.append(_row(f"TEST-ranges-{i + 1:02d}", f"TEST {place}",
                         lat, lon, category1=kind, first_seen=at(i // 2)))
    for i, (place, kind, lon, lat) in enumerate(GEELONG_JOBS):
        rows.append(_row(f"TEST-geelong-{i + 1}", f"TEST {place}", lat, lon,
                         category1=kind, first_seen=at(0)))
    rows += [
        _row("TEST-storm-warning", "TEST Severe Thunderstorm — Dandenong Ranges",
             -37.89, 145.36, feed_type="warning", category1=level, level=level,
             event="Severe Thunderstorm", geometry=_box(145.28, -37.95, 145.45, -37.83),
             first_seen=level_since),
        _row("TEST-shepparton-flood", "TEST Goulburn River — Shepparton",
             -36.38, 145.40, feed_type="warning", category1="Watch and Act",
             level="Watch and Act", event="Riverine Flood",
             geometry=_box(145.34, -36.42, 145.46, -36.34), first_seen=at(0)),
        _row("TEST-gippsland-advice", "TEST Flash flooding — Traralgon",
             -38.19, 146.54, feed_type="warning", category1="Advice",
             level="Advice", event="Flash Flood", first_seen=at(0)),
        _row("TEST-bendigo-rescue", "TEST Rescue — Bendigo", -36.76, 144.28,
             category1="Rescue", first_seen=at(0)),
        _row("TEST-grass-fire", "TEST Grass fire — Lysterfield", -37.93, 145.30,
             category1="Fire", category2="Grass Fire", org="VIC/CFA",
             status="Under Control", first_seen=at(0)),
    ]
    roads = pd.DataFrame([{
        "source_id": "TEST-road-1", "disruption_type": "Hazard, Fallen trees",
        "description": "Road closed due to fallen trees", "is_closure": 1,
        "road_name": "TEST Mount Dandenong Tourist Rd", "location": "Ferny Creek",
        "latitude": -37.886, "longitude": 145.347, "geometry": None,
        "created": at(1), "first_seen": at(1), "updated": at(1), "status": "Closed",
        "direction": None, "lanes_affected": None, "lga": "Yarra Ranges",
        "ses_region": None}] if step >= 1 else [],
        columns=["source_id", "disruption_type", "description", "is_closure",
                 "road_name", "latitude", "longitude", "geometry", "created",
                 "first_seen"])
    power = pd.DataFrame([{
        "location": "TEST Belgrave", "customers_off": 1240 + 380 * step,
        "type": "Unplanned", "latitude": -37.910, "longitude": 145.357}])
    gauges = pd.DataFrame([{
        "station_name": "TEST Barwon River at Geelong", "height_m": 2.31,
        "latitude": -38.165, "longitude": 144.33, "priority": 3,
        "label": "Minor flooding", "colour": "#e6c700"}] if step >= 2 else [],
        columns=["station_name", "height_m", "latitude", "longitude", "priority",
                 "label", "colour"])
    cells = pd.DataFrame([{
        "cell_id": "T1", "radar_id": "IDR023", "classification": "strong",
        "latitude": -37.87, "longitude": 145.40, "intensity_score": 61,
        "area_km2": 95.0, "impact_geojson": None}] if step >= 3 else [],
        columns=["cell_id", "radar_id", "classification", "latitude", "longitude",
                 "intensity_score", "area_km2", "impact_geojson"])
    return {"incidents": pd.DataFrame(rows), "roads": roads, "power": power,
            "gauges": gauges, "cells": cells}


def snapshot(link_km=storm_areas.DEFAULT_LINK_KM, agencies=None, weather_only=True,
             now_ts=None):
    """A storm-wall snapshot of the simulated scenario (same shape as
    `storm_areas.compute`), plus a `test` block describing the cycle. Event tags
    are ignored: the scenario is its own event."""
    now_ts = time.time() if now_ts is None else now_ts
    _, step, next_ts = clock(now_ts)
    link_km, agencies, weather_only = storm_areas.normalise_options(
        link_km, agencies, weather_only)
    snap = storm_areas.new_snapshot(datetime.fromtimestamp(now_ts), link_km,
                                    agencies, weather_only, None)
    snap["test"] = {"step": step + 1, "steps": STEPS,
                    "next": datetime.fromtimestamp(next_ts)}
    if not storm_areas.HAVE_SHAPELY:
        snap.update(ok=False, error="shapely is not installed")
        return snap
    return storm_areas.assemble(snap, frames(now_ts))


def describe(snap):
    """One line for the wall header while the test runs."""
    t = snap.get("test") or {}
    return (f"TEST RUN — simulated storm, nothing here is real · step "
            f"{t.get('step')} of {t.get('steps')} · next change "
            f"{t['next']:%H:%M:%S}" if t else "TEST RUN")

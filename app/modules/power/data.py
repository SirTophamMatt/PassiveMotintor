# Passive Monitor — Copyright (c) 2026 SirTophamMatt. All rights reserved.
"""Power outage data queries.

Two shapes: per-LOCALITY rows (`power_outages`, one per suburb listed under an
outage, each carrying its outage's `outage_ref`) and per-OUTAGE rows
(`power_outage_events`). See outages.py for how an outage keeps its ID.
"""
import json

import pandas as pd

from app import database, localities
from app.modules.power import outages


def load_timeseries(since=None):
    query = "SELECT timestamp, customers_off, power_dependant_off, planned, unplanned FROM power_timeseries"
    params = []
    if since is not None:
        query += " WHERE timestamp >= ?"
        params.append(since.isoformat(sep=" ", timespec="seconds"))
    df = database.read_df(query + " ORDER BY timestamp", params)
    if not df.empty:
        df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
        for col in ("customers_off", "power_dependant_off", "planned", "unplanned"):
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def load_timeseries_range(start, end):
    """Timeseries rows with timestamp in [start, end] (strings), for export."""
    df = database.read_df(
        "SELECT timestamp, customers_off, power_dependant_off, planned, unplanned "
        "FROM power_timeseries WHERE timestamp BETWEEN ? AND ? ORDER BY timestamp",
        [start, end])
    return df


def outages_in_range(start, end):
    """Outages that overlap [start, end]: seen during the window (first_seen on
    or before the end, and still active or last seen at/after the start)."""
    df = database.read_df(
        "SELECT o.outage_ref, o.location, o.customers_off, o.type, o.first_seen, "
        "       o.last_seen, o.restored, o.duration_mins, g.latitude, g.longitude "
        "FROM power_outages o "
        "LEFT JOIN geocode_cache g ON g.location = o.location "
        "WHERE o.first_seen <= ? AND (o.restored = 0 OR o.last_seen >= ?) "
        "ORDER BY o.first_seen",
        [end, start])
    return df


def latest_totals():
    df = database.read_df(
        "SELECT * FROM power_timeseries ORDER BY timestamp DESC LIMIT 1")
    if df.empty:
        return None
    return df.iloc[0].to_dict()


def active_outages(include_planned=True, min_customers=0):
    """Active (unrestored) per-LOCALITY outage rows, with coordinates from the
    geocode cache and the `outage_ref` each locality is listed under. A
    suburb on two outages is two rows — use `outages.customers_total` /
    `outage_count` to count outages, never `len()` or a plain sum."""
    df = database.read_df(
        "SELECT o.outage_ref, o.location, o.customers_off, o.type, o.first_seen, "
        "       o.last_seen, g.latitude, g.longitude "
        "FROM power_outages o "
        "LEFT JOIN geocode_cache g ON g.location = o.location "
        "WHERE o.restored = 0")
    if df.empty:
        return df
    df["first_seen"] = pd.to_datetime(df["first_seen"], errors="coerce")
    df["duration_mins"] = (
        (pd.Timestamp.now() - df["first_seen"]).dt.total_seconds() // 60)
    if not include_planned:
        df = df[~df["type"].fillna("").str.strip().str.lower().eq("planned")]
    if min_customers:
        df = df[df["customers_off"].fillna(0) >= min_customers]
    return df


def with_localities(df):
    """Per-locality rows + their locality boundary (localities.attach), each
    shared name decided by the other suburbs of the same outage."""
    if df is None or df.empty:
        return df
    return localities.attach(df, [outages.group_key(r) for r in df.to_dict("records")])


def map_outages(include_planned=True, min_customers=0):
    """Active per-locality rows ready for a map: boundaries attached."""
    return with_localities(active_outages(include_planned, min_customers))


_EVENT_COLS = ("outage_ref, type, first_seen, last_seen, restored, restored_at, "
               "duration_mins, customers_off, peak_customers, locations_json, "
               "all_locations_json, cells_json")


def _event_frame(df):
    if df.empty:
        return df
    df = df.copy()
    df["locations"] = [_json_list(v) for v in df["locations_json"]]
    df["all_locations"] = [_json_list(v) for v in df["all_locations_json"]]
    df["locality_count"] = [len(v) for v in df["locations"]]
    df["label"] = [outages.label(v) for v in df["locations"]]
    first = pd.to_datetime(df["first_seen"], errors="coerce")
    live = (pd.Timestamp.now() - first).dt.total_seconds() // 60
    df["duration_mins"] = df["duration_mins"].where(
        pd.to_numeric(df["restored"], errors="coerce").fillna(0) == 1, live)
    return df


def _json_list(raw):
    try:
        value = json.loads(raw) if raw else []
    except (TypeError, ValueError):
        return []
    return value if isinstance(value, list) else []


def outage_events(include_planned=True, min_customers=0):
    """Active OUTAGES, one row each: ref, type, customers, localities listed
    (`locations`, EM-COP order), `label` ("Belgrave, Tecoma +3")."""
    df = _event_frame(database.read_df(
        f"SELECT {_EVENT_COLS} FROM power_outage_events WHERE restored = 0 "
        "ORDER BY customers_off DESC, first_seen"))
    if df.empty:
        return df
    if not include_planned:
        df = df[~df["type"].fillna("").str.strip().str.lower().eq("planned")]
    if min_customers:
        df = df[pd.to_numeric(df["customers_off"], errors="coerce").fillna(0)
                >= min_customers]
    return df.reset_index(drop=True)


def outage_events_in_range(start, end):
    """Outages that overlap [start, end] (strings), for export."""
    return _event_frame(database.read_df(
        f"SELECT {_EVENT_COLS} FROM power_outage_events "
        "WHERE first_seen <= ? AND (restored = 0 OR last_seen >= ?) "
        "ORDER BY first_seen", [end, start]))


def outage(ref):
    """One outage by ID — active or restored — with every locality row it has
    ever listed (`rows`, boundaries attached), or None."""
    ev = _event_frame(database.read_df(
        f"SELECT {_EVENT_COLS} FROM power_outage_events WHERE outage_ref = ?", [ref]))
    if ev.empty:
        return None
    rows = database.read_df(
        "SELECT o.outage_ref, o.location, o.customers_off, o.type, o.first_seen, "
        "       o.last_seen, o.restored, o.duration_mins, g.latitude, g.longitude "
        "FROM power_outages o LEFT JOIN geocode_cache g ON g.location = o.location "
        "WHERE o.outage_ref = ? ORDER BY o.restored, o.id", [ref])
    out = ev.iloc[0].to_dict()
    out["rows"] = with_localities(rows) if not rows.empty else rows
    try:
        cells = json.loads(out.get("cells_json") or "{}")
    except (TypeError, ValueError):
        cells = {}
    out["cells"] = cells if isinstance(cells, dict) else {}
    return out


def outages_for_location(location):
    """Every ACTIVE outage listing a locality — the link from a suburb back to
    its outage(s)."""
    df = database.read_df(
        "SELECT DISTINCT outage_ref FROM power_outages "
        "WHERE restored = 0 AND outage_ref IS NOT NULL AND location = ?",
        [location])
    return [] if df.empty else list(df["outage_ref"])


DURATION_BUCKETS = [
    (360, "Less than 6 hours", "#1f77b4"),
    (1440, "6 to 24 hours", "#e6c700"),
    (2880, "24 to 48 hours", "#ff7f0e"),
    (float("inf"), "More than 48 hours", "#d62728"),
]

DURATION_COLOUR_MAP = {label: colour for _, label, colour in DURATION_BUCKETS}


def duration_bucket(minutes):
    for threshold, label, _ in DURATION_BUCKETS:
        if minutes < threshold:
            return label
    return DURATION_BUCKETS[-1][1]

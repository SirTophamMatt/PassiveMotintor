"""Flood data queries and flood-level classification.

Collection is always-on and writes every reading under the fixed LIVE_EVENT
bucket; the old per-event namespacing is gone. Views and exports now select
data by timestamp range (usually resolved from an event tag), not by the event
column.
"""
import pandas as pd

from app import database

# The single always-on collection bucket. Kept in the event column so the
# existing dedup index (event, station, timestamp, height) still works.
LIVE_EVENT = "live"


def get_events():
    """Distinct event values still present (legacy/back-compat only)."""
    df = database.read_df(
        "SELECT DISTINCT event FROM flood_observations ORDER BY event")
    return df["event"].dropna().tolist()


def get_catchments(start=None, end=None):
    query = "SELECT DISTINCT catchment FROM flood_observations"
    params = []
    if start and end:
        query += " WHERE timestamp BETWEEN ? AND ?"
        params = [start, end]
    df = database.read_df(query + " ORDER BY catchment", params)
    return df["catchment"].dropna().tolist()


def heartbeat_summary(start=None, end=None):
    """Returns (cycle_count, last_timestamp) for collection heartbeats, optionally
    within a timestamp range."""
    query = "SELECT COUNT(*) AS n, MAX(timestamp) AS last FROM flood_heartbeat"
    params = []
    if start and end:
        query += " WHERE timestamp BETWEEN ? AND ?"
        params = [start, end]
    df = database.read_df(query, params)
    if df.empty or not df.iloc[0]["n"]:
        return 0, None
    return int(df.iloc[0]["n"]), df.iloc[0]["last"]


def load_observations(start=None, end=None, catchment=None):
    """Observations within a timestamp range (both ends inclusive). With no
    range, returns everything."""
    query = "SELECT * FROM flood_observations"
    clauses, params = [], []
    if start and end:
        clauses.append("timestamp BETWEEN ? AND ?")
        params += [start, end]
    if catchment:
        clauses.append("catchment = ?")
        params.append(catchment)
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    df = database.read_df(query + " ORDER BY timestamp", params)
    if not df.empty:
        df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
        df["height_m"] = pd.to_numeric(df["height_m"], errors="coerce")
    return df


def load_flood_levels():
    """Returns a dict: lowercase station name -> {minor, moderate, major}."""
    df = database.read_df("SELECT * FROM flood_levels")
    return {
        row["station_key"]: {
            "minor": row["minor"], "moderate": row["moderate"], "major": row["major"],
        }
        for _, row in df.iterrows()
    }


def classify_station(latest_height, levels):
    """Returns (priority, label, colour) for a station's latest height.

    Priority sorts flooded stations first: 1=major, 2=moderate, 3=minor, 4=none.
    """
    if levels and pd.notna(latest_height):
        if pd.notna(levels["major"]) and latest_height >= levels["major"]:
            return 1, "Major flooding", "#d62728"
        if pd.notna(levels["moderate"]) and latest_height >= levels["moderate"]:
            return 2, "Moderate flooding", "#ff7f0e"
        if pd.notna(levels["minor"]) and latest_height >= levels["minor"]:
            return 3, "Minor flooding", "#e6c700"
    return 4, "Below flood level", "#9aa0a6"


def flooding_station_count(event=None):
    """Number of stations whose most recent reading exceeds their minor level."""
    query = "SELECT station_name, height_m, timestamp FROM flood_observations"
    params = []
    if event:
        query += " WHERE event = ?"
        params.append(event)
    df = database.read_df(query, params)
    if df.empty:
        return 0
    levels = load_flood_levels()
    if not levels:
        return 0
    df = df.sort_values("timestamp")
    latest = df.groupby("station_name").tail(1)
    count = 0
    for _, row in latest.iterrows():
        lv = levels.get(str(row["station_name"]).strip().lower())
        priority, _, _ = classify_station(row["height_m"], lv)
        if priority < 4:
            count += 1
    return count


def current_flooding_stations(max_stations=12):
    """Stations whose most recent reading (any event) is at/above minor flood
    level, with their full height history for plotting. Returns a list of
    (station, history_df, label, colour, levels), flooding-severity first."""
    levels = load_flood_levels()
    if not levels:
        return []
    latest = database.read_df(
        "SELECT station_name, height_m, MAX(timestamp) AS ts "
        "FROM flood_observations GROUP BY station_name")
    if latest.empty:
        return []
    flooding = []
    for _, row in latest.iterrows():
        lv = levels.get(str(row["station_name"]).strip().lower())
        height = pd.to_numeric(row["height_m"], errors="coerce")
        priority, label, colour = classify_station(height, lv)
        if priority < 4:
            flooding.append((priority, row["station_name"], label, colour, lv))
    flooding.sort(key=lambda x: (x[0], x[1]))

    out = []
    for _, station, label, colour, lv in flooding[:max_stations]:
        hist = database.read_df(
            "SELECT timestamp, height_m FROM flood_observations "
            "WHERE station_name = ? ORDER BY timestamp", [station])
        hist["timestamp"] = pd.to_datetime(hist["timestamp"], errors="coerce")
        hist["height_m"] = pd.to_numeric(hist["height_m"], errors="coerce")
        hist = hist.dropna(subset=["height_m"])
        if not hist.empty:
            out.append((station, hist, label, colour, lv))
    return out

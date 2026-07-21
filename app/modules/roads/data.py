"""Road-disruption data queries and classification."""
import pandas as pd

from app import database

# (sort priority, colour) — full closures sort first / red, other disruptions
# amber, mirroring the fire module's classify().
CLOSURE_STYLE = (1, "#d62728")
OTHER_STYLE = (3, "#ff7f0e")

_TS_COLS = ("start_time", "end_time", "created", "updated", "first_seen", "last_seen")


def classify(is_closure):
    """(priority, colour) for a disruption row."""
    return CLOSURE_STYLE if is_closure else OTHER_STYLE


def active_disruptions(closures_only=False):
    """Currently-active disruptions, most recently updated first."""
    query = "SELECT * FROM road_disruptions WHERE resolved = 0"
    if closures_only:
        query += " AND is_closure = 1"
    df = database.read_df(query + " ORDER BY is_closure DESC, updated DESC")
    if not df.empty:
        for col in _TS_COLS:
            if col in df.columns:
                df[col] = pd.to_datetime(df[col], format="ISO8601", errors="coerce")
    return df


def latest_counts():
    """Headline counts of active disruptions for KPI cards."""
    df = database.read_df(
        "SELECT is_closure FROM road_disruptions WHERE resolved = 0")
    if df.empty:
        return {"total": 0, "closures": 0, "other": 0}
    closures = int(df["is_closure"].fillna(0).sum())
    return {"total": len(df), "closures": closures, "other": len(df) - closures}


def load_road_timeseries(since=None):
    """Per-cycle KPI history for trend graphs."""
    query = "SELECT * FROM road_timeseries"
    params = []
    if since is not None:
        query += " WHERE timestamp >= ?"
        params.append(since.isoformat(sep=" ", timespec="seconds"))
    df = database.read_df(query + " ORDER BY timestamp", params)
    if not df.empty:
        df["timestamp"] = pd.to_datetime(df["timestamp"], format="ISO8601",
                                         errors="coerce")
    return df


def heartbeat_summary():
    """(cycle_count, last_timestamp) for the roads collector heartbeat."""
    df = database.read_df(
        "SELECT COUNT(*) AS n, MAX(timestamp) AS last FROM road_timeseries")
    if df.empty or not df.iloc[0]["n"]:
        return 0, None
    return int(df.iloc[0]["n"]), df.iloc[0]["last"]

"""Fire / incident data queries and warning classification."""
import pandas as pd

from app import database

# Warning level -> (sort priority, colour). Lower priority sorts first / is
# more severe, mirroring flood classify_station.
WARNING_STYLE = {
    "Emergency Warning": (1, "#d62728"),
    "Watch and Act": (2, "#ff7f0e"),
    "Advice": (3, "#e6c700"),
}

_TS_COLS = ("created", "updated", "first_seen", "last_seen")


def classify(warning_level=None, category1=None):
    """(priority, colour) for a row: warnings by level, live fires amber, else grey."""
    if warning_level in WARNING_STYLE:
        return WARNING_STYLE[warning_level]
    if str(category1 or "").strip().lower() == "fire":
        return 2, "#ff7f0e"
    return 4, "#9aa0a6"


def active_incidents(category=None, warnings_only=False):
    """Currently-active incidents/warnings, most recently updated first."""
    query = "SELECT * FROM fire_incidents WHERE resolved = 0"
    params = []
    if warnings_only:
        query += " AND feed_type = 'warning'"
    if category:
        query += " AND category1 = ?"
        params.append(category)
    df = database.read_df(query + " ORDER BY updated DESC", params)
    if not df.empty:
        for col in _TS_COLS:
            df[col] = pd.to_datetime(df[col], format="ISO8601", errors="coerce")
    return df


def categories():
    """Distinct category1 values among active rows (for the page filter)."""
    df = database.read_df(
        "SELECT DISTINCT category1 FROM fire_incidents "
        "WHERE resolved = 0 AND category1 IS NOT NULL ORDER BY category1")
    return df["category1"].tolist()


def latest_counts():
    """Headline counts of active events for KPI cards."""
    df = database.read_df(
        "SELECT category1, warning_level FROM fire_incidents WHERE resolved = 0")
    if df.empty:
        return {"total": 0, "active_fires": 0, "emergency": 0,
                "watch_act": 0, "advice": 0}
    level = df["warning_level"].fillna("")
    return {
        "total": len(df),
        "active_fires": int((df["category1"].fillna("").str.lower() == "fire").sum()),
        "emergency": int((level == "Emergency Warning").sum()),
        "watch_act": int((level == "Watch and Act").sum()),
        "advice": int((level == "Advice").sum()),
    }


def load_fire_timeseries(since=None):
    """Per-cycle KPI history for trend graphs."""
    query = "SELECT * FROM fire_timeseries"
    params = []
    if since is not None:
        query += " WHERE timestamp >= ?"
        params.append(since.isoformat(sep=" ", timespec="seconds"))
    df = database.read_df(query + " ORDER BY timestamp", params)
    if not df.empty:
        df["timestamp"] = pd.to_datetime(df["timestamp"], format="ISO8601", errors="coerce")
    return df


def heartbeat_summary():
    """(cycle_count, last_timestamp) for the fire collector heartbeat."""
    df = database.read_df(
        "SELECT COUNT(*) AS n, MAX(timestamp) AS last FROM fire_timeseries")
    if df.empty or not df.iloc[0]["n"]:
        return 0, None
    return int(df.iloc[0]["n"]), df.iloc[0]["last"]

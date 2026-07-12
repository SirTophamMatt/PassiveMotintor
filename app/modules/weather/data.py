"""Weather data queries and warning classification."""
import pandas as pd

from app import database

# BoM warning_group_type -> (sort priority, colour). Lower = more severe.
GROUP_STYLE = {
    "major": (1, "#d62728"),
    "severe": (1, "#d62728"),
    "moderate": (2, "#ff7f0e"),
    "minor": (3, "#e6c700"),
}

_TS_COLS = ("issue_time", "expiry_time", "first_seen", "last_seen")


def classify(group_type):
    """(priority, colour) for a warning by its BoM group type."""
    return GROUP_STYLE.get(str(group_type or "").strip().lower(), (4, "#5b8def"))


def _pretty_type(t):
    """flood_warning -> 'Flood Warning'."""
    return str(t or "").replace("_", " ").title()


def active_warnings(warning_type=None):
    """Active BoM VIC warnings, most severe first then most recently issued."""
    query = "SELECT * FROM weather_warnings WHERE active = 1"
    params = []
    if warning_type:
        query += " AND type = ?"
        params.append(warning_type)
    df = database.read_df(query + " ORDER BY issue_time DESC", params)
    if not df.empty:
        for col in _TS_COLS:
            df[col] = pd.to_datetime(df[col], format="ISO8601", errors="coerce")
        df["type_label"] = df["type"].map(_pretty_type)
        df["_prio"] = df["group_type"].map(lambda g: classify(g)[0])
        df = df.sort_values(["_prio", "issue_time"], ascending=[True, False])
    return df


def warning_types():
    """Distinct active warning types (for the page filter)."""
    df = database.read_df(
        "SELECT DISTINCT type FROM weather_warnings "
        "WHERE active = 1 AND type IS NOT NULL ORDER BY type")
    return [(_pretty_type(t), t) for t in df["type"].tolist()]


def warning_counts():
    """Headline counts of active warnings for KPI cards."""
    df = database.read_df(
        "SELECT type, group_type FROM weather_warnings WHERE active = 1")
    if df.empty:
        return {"total": 0, "flood": 0, "severe": 0, "major": 0}
    typ = df["type"].fillna("")
    grp = df["group_type"].fillna("").str.lower()
    return {
        "total": len(df),
        "flood": int(typ.str.contains("flood").sum()),
        "severe": int(typ.str.contains("severe").sum()),
        "major": int(grp.isin(["major", "severe"]).sum()),
    }


def heartbeat_summary():
    """(cycle_count, last_timestamp) for the weather collector heartbeat."""
    df = database.read_df(
        "SELECT COUNT(*) AS n, MAX(timestamp) AS last FROM weather_heartbeat")
    if df.empty or not df.iloc[0]["n"]:
        return 0, None
    return int(df.iloc[0]["n"]), df.iloc[0]["last"]

"""Power outage data queries."""
import pandas as pd

from app import database


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
        "SELECT o.location, o.customers_off, o.type, o.first_seen, o.last_seen, "
        "       o.restored, o.duration_mins, g.latitude, g.longitude "
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
    """Active (unrestored) outages with coordinates from the geocode cache."""
    df = database.read_df(
        "SELECT o.location, o.customers_off, o.type, o.first_seen, o.last_seen, "
        "       g.latitude, g.longitude "
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

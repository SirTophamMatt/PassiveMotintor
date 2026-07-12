"""Weather data queries and warning classification."""
import re

import pandas as pd

from app import database

# Trailing BoM gauge-type markers to strip when extracting a town from a gauge
# name, e.g. "Buffalo River at Lake Buffalo HG" -> "Lake Buffalo".
_GAUGE_SUFFIX = re.compile(r"\s*\((?:HG|TW)\)\s*$|\s+(?:HG|TW)\s*$", re.I)
_SPLITTERS = (" downstream of ", " upstream of ", " at ")


def gauge_town(station_name):
    """Extract a searchable town/place from a BoM gauge name (shared by rainfall
    location seeding and gauge->rainfall matching). Returns None if nothing
    usable remains."""
    if not station_name:
        return None
    s = str(station_name).strip()
    low = s.lower()
    for sep in _SPLITTERS:
        idx = low.rfind(sep)
        if idx != -1:
            s = s[idx + len(sep):]
            break
    s = _GAUGE_SUFFIX.sub("", s)
    s = re.sub(r"\s*\([^)]*\)\s*$", "", s).strip()  # drop trailing parentheticals
    return s or None

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


def warning_detail(warning_id):
    """Latest stored row for one warning (dict), or None."""
    df = database.read_df(
        "SELECT * FROM weather_warnings WHERE warning_id = ?", [warning_id])
    if df.empty:
        return None
    return df.iloc[0].to_dict()


def warning_history(warning_id):
    """All recorded versions of a warning, newest issue first."""
    df = database.read_df(
        "SELECT issue_time, phase, title, message FROM weather_warning_updates "
        "WHERE warning_id = ? ORDER BY issue_time DESC", [warning_id])
    if not df.empty:
        df["issue_time"] = pd.to_datetime(df["issue_time"], format="ISO8601",
                                          errors="coerce")
    return df


def warning_version_message(warning_id, issue_time):
    """The full message for one recorded version of a warning, or None."""
    df = database.read_df(
        "SELECT message FROM weather_warning_updates "
        "WHERE warning_id = ? AND issue_time = ?", [warning_id, issue_time])
    if df.empty:
        return None
    return df.iloc[0]["message"]


def latest_rainfall():
    """Most recent rainfall reading per monitored location, with its catchment."""
    df = database.read_df(
        "SELECT r.location_key, r.name, r.latitude, r.longitude, "
        "       r.rain_since_9am_mm, r.forecast_max_mm, r.forecast_chance, "
        "       r.timestamp, l.catchment "
        "FROM rainfall_observations r "
        "JOIN (SELECT location_key, MAX(timestamp) AS mt FROM rainfall_observations "
        "      GROUP BY location_key) m "
        "  ON r.location_key = m.location_key AND r.timestamp = m.mt "
        "LEFT JOIN weather_locations l ON l.location_key = r.location_key")
    if not df.empty:
        for c in ("rain_since_9am_mm", "forecast_max_mm", "forecast_chance"):
            df[c] = pd.to_numeric(df[c], errors="coerce")
        df = df.sort_values("rain_since_9am_mm", ascending=False, na_position="last")
    return df


def rainfall_history(location_key, days=7):
    """Rain-since-9am time series for one location (for the gauge overlay)."""
    query = ("SELECT timestamp, rain_since_9am_mm FROM rainfall_observations "
             "WHERE location_key = ?")
    params = [location_key]
    if days:
        query += " AND timestamp >= datetime('now', 'localtime', ?)"
        params.append(f"-{int(days)} days")
    df = database.read_df(query + " ORDER BY timestamp", params)
    if not df.empty:
        df["timestamp"] = pd.to_datetime(df["timestamp"], format="ISO8601",
                                         errors="coerce")
        df["rain_since_9am_mm"] = pd.to_numeric(df["rain_since_9am_mm"],
                                                errors="coerce")
    return df


def location_for_gauge(station_name):
    """The monitored rainfall location matching a flood gauge's town, or None."""
    town = gauge_town(station_name)
    if not town:
        return None
    df = database.read_df(
        "SELECT * FROM weather_locations WHERE location_key = ?", [town.lower()])
    return None if df.empty else df.iloc[0].to_dict()


def rainfall_summary():
    """(num_locations, wettest_name, wettest_mm) from the latest readings."""
    df = latest_rainfall()
    if df.empty:
        return 0, None, None
    top = df.iloc[0]
    return len(df), top["name"], top["rain_since_9am_mm"]


def latest_aws_rainfall():
    """Latest rain-since-9am per AWS station, with coords from the registry."""
    df = database.read_df(
        "SELECT r.wmo, r.name, r.rain_since_9am_mm, r.obs_time, "
        "       s.latitude, s.longitude "
        "FROM rainfall_aws r "
        "JOIN (SELECT wmo, MAX(obs_time) AS mt FROM rainfall_aws GROUP BY wmo) m "
        "  ON r.wmo = m.wmo AND r.obs_time = m.mt "
        "LEFT JOIN aws_stations s ON s.wmo = r.wmo")
    if not df.empty:
        df["rain_since_9am_mm"] = pd.to_numeric(df["rain_since_9am_mm"], errors="coerce")
        df = df.sort_values("rain_since_9am_mm", ascending=False, na_position="last")
    return df


def aws_rainfall_history(wmo, start=None, end=None):
    """Rain-since-9am time series for one AWS station (optionally within a
    timestamp range), oldest first — for charts."""
    query = ("SELECT obs_time, rain_since_9am_mm FROM rainfall_aws WHERE wmo = ?")
    params = [str(wmo)]
    if start and end:
        query += " AND timestamp BETWEEN ? AND ?"
        params += [start, end]
    df = database.read_df(query + " ORDER BY obs_time", params)
    if not df.empty:
        df["obs_time"] = pd.to_datetime(df["obs_time"], format="ISO8601", errors="coerce")
        df["rain_since_9am_mm"] = pd.to_numeric(df["rain_since_9am_mm"], errors="coerce")
    return df


def _window_total(values):
    """Total rain accumulated across a series of rain-since-9am readings, using
    positive increments so daily 9am resets are handled (a drop = reset, and the
    post-reset value is fresh rain). Baseline (pre-window same-day rain) excluded."""
    total = 0.0
    prev = None
    for v in values:
        if pd.isna(v):
            continue
        v = float(v)
        if prev is None:
            prev = v
            continue
        total += (v - prev) if v >= prev else v  # v < prev => 9am reset
        prev = v
    return round(total, 1)


def aws_event_total(start=None, end=None):
    """Per-station cumulative rainfall over a window (for a tagged event),
    reset-proof. Returns wmo, name, total_mm, wettest first."""
    query = "SELECT wmo, name, obs_time, rain_since_9am_mm FROM rainfall_aws"
    params = []
    if start and end:
        query += " WHERE timestamp BETWEEN ? AND ?"
        params += [start, end]
    df = database.read_df(query + " ORDER BY wmo, obs_time", params)
    if df.empty:
        return df
    df["rain_since_9am_mm"] = pd.to_numeric(df["rain_since_9am_mm"], errors="coerce")
    out = (df.groupby(["wmo", "name"])["rain_since_9am_mm"]
             .apply(lambda s: _window_total(s.tolist()))
             .reset_index(name="total_mm"))
    return out.sort_values("total_mm", ascending=False)


def load_aws_range(start, end):
    """Raw AWS rainfall readings within a timestamp range, for tagged export."""
    return database.read_df(
        "SELECT wmo, name, rain_since_9am_mm, obs_time, timestamp "
        "FROM rainfall_aws WHERE timestamp BETWEEN ? AND ? "
        "ORDER BY name, obs_time", [start, end])


def aws_summary():
    """(station_count, wettest_name, wettest_since9am_mm) from latest readings."""
    df = latest_aws_rainfall()
    if df.empty:
        return 0, None, None
    top = df.iloc[0]
    return len(df), top["name"], top["rain_since_9am_mm"]


def heartbeat_summary():
    """(cycle_count, last_timestamp) for the weather collector heartbeat."""
    df = database.read_df(
        "SELECT COUNT(*) AS n, MAX(timestamp) AS last FROM weather_heartbeat")
    if df.empty or not df.iloc[0]["n"]:
        return 0, None
    return int(df.iloc[0]["n"]), df.iloc[0]["last"]

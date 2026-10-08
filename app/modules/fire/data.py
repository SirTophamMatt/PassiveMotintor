# Passive Monitor — Copyright (c) 2026 SirTophamMatt. All rights reserved.
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

# Feed types that are NOT incidents. A community warning (including an Advice)
# is a statement about a hazard, not an incident; a burn area is a historical
# footprint. Neither is ever counted as a fire.
NOT_INCIDENT_FEED_TYPES = ("warning", "burn-area")


def is_warning(row):
    return str(row.get("feed_type") or "").strip().lower() == "warning"


def is_incident(row):
    return (str(row.get("feed_type") or "").strip().lower()
            not in NOT_INCIDENT_FEED_TYPES)


def is_fire(row):
    """The ONE definition of "a fire" used site-wide: an INCIDENT whose
    category is Fire. Warnings and Advices about fires are warnings, not fires,
    so they never add to a fire count."""
    return is_incident(row) and \
        str(row.get("category1") or "").strip().lower() == "fire"


def classify(warning_level=None, category1=None, feed_type=None):
    """(priority, colour) for a row: warnings by level, live fires amber, else grey."""
    if warning_level in WARNING_STYLE:
        return WARNING_STYLE[warning_level]
    if is_fire({"feed_type": feed_type, "category1": category1}):
        return 2, "#ff7f0e"
    return 4, "#9aa0a6"


def active_incidents(category=None, warnings_only=False):
    """Currently-active incidents/warnings, most recently updated first.
    Excludes historical burn areas (see burn_areas())."""
    query = ("SELECT * FROM fire_incidents "
             "WHERE resolved = 0 AND feed_type != 'burn-area'")
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
        "WHERE resolved = 0 AND feed_type != 'burn-area' "
        "AND category1 IS NOT NULL ORDER BY category1")
    return df["category1"].tolist()


def burn_areas():
    """Historical DELWP burn-area footprints (with polygon geometry) for the
    map's toggleable 'burn scars' layer."""
    return database.read_df(
        "SELECT source_id, location, geometry, created, updated "
        "FROM fire_incidents WHERE feed_type = 'burn-area' AND resolved = 0 "
        "AND geometry IS NOT NULL")


def latest_counts():
    """Headline counts of active events for KPI cards."""
    df = database.read_df(
        "SELECT feed_type, category1, warning_level FROM fire_incidents "
        "WHERE resolved = 0 AND feed_type != 'burn-area'")
    if df.empty:
        return {"total": 0, "incidents": 0, "active_fires": 0, "emergency": 0,
                "watch_act": 0, "advice": 0}
    rows = df.to_dict("records")
    warnings = df["feed_type"].fillna("").str.lower() == "warning"
    level = df["warning_level"].where(warnings, "").fillna("")
    return {
        # Incidents and warnings together. Kept for callers that need the
        # feed's size, but never shown as one figure: a warning is not an
        # incident, and adding them lets either one over-represent the other.
        "total": len(df),
        "incidents": int((~warnings).sum()),
        "active_fires": sum(1 for r in rows if is_fire(r)),
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


# --------------------------------------------------------------------------- #
# Agency — who an incident belongs to (VicEmergency `sourceOrg`, e.g. "VIC/SES")
# --------------------------------------------------------------------------- #
# Matched by keyword because the feed writes the agency as free text with a
# state prefix. Order matters only for readability: the patterns don't overlap.
AGENCIES = [
    ("ses", "SES", ("SES",)),
    ("cfa", "CFA", ("CFA",)),
    ("frv", "FRV", ("FRV", "MFB", "FIRE RESCUE")),
    ("ffm", "Forest Fire Management", ("DELWP", "DEECA", "FFM", "FOREST FIRE",
                                       "PARKS")),
]
AGENCY_OTHER = "other"
AGENCY_LABELS = dict([(k, label) for k, label, _ in AGENCIES]
                     + [(AGENCY_OTHER, "Other agencies")])


def agency_of(source_org):
    """'VIC/SES' -> 'ses'; anything unrecognised (or missing) -> 'other'."""
    if source_org is None or source_org != source_org:      # None / NaN
        return AGENCY_OTHER
    text = str(source_org).upper()
    for key, _, needles in AGENCIES:
        if any(n in text for n in needles):
            return key
    return AGENCY_OTHER

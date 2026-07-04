"""Legacy data import.

Pulls data from the old Flood Monitor / PowerDashboard projects into the
unified database. Each function takes a file path and returns a status
message; nothing is imported automatically.
"""
import json
import logging
import os
import sqlite3

import pandas as pd

from app import database
from app.config import LEGACY_ROOT

log = logging.getLogger(__name__)

# Suggested default paths into the old projects (one level up).
DEFAULT_PATHS = {
    "flood_db": os.path.join(LEGACY_ROOT, "Flood Monitor", "flood_monitor.db"),
    "flood_levels": os.path.join(LEGACY_ROOT, "Flood Monitor", "Flood Levels.xlsx"),
    "power_csv": os.path.join(LEGACY_ROOT, "power_data.csv"),
    "tracker_csv": os.path.join(LEGACY_ROOT, "outage_tracker.csv"),
    "geo_cache": os.path.join(LEGACY_ROOT, "geo_cache.json"),
}

FLOOD_COLUMN_MAP = {
    "Catchment": "catchment",
    "Station Name": "station_name",
    "Station Type": "station_type",
    "Time/Day": "time_day",
    "Height (m)": "height_m",
    "Gauge Datum": "gauge_datum",
    "Tendency": "tendency",
    "Crossing (m)": "crossing_m",
    "Flood Classification": "classification",
    "Recent Data": "recent_data",
    "Timestamp": "timestamp",
    "Event": "event",
}


def _flood_rows_from_df(df, fallback_event):
    df = df.rename(columns=FLOOD_COLUMN_MAP)
    keep = [c for c in FLOOD_COLUMN_MAP.values() if c in df.columns]
    df = df[keep].copy()
    if "event" not in df.columns:
        df["event"] = fallback_event
    df["event"] = df["event"].fillna(fallback_event)
    if "height_m" in df.columns:
        df["height_m"] = pd.to_numeric(df["height_m"], errors="coerce")
    if "timestamp" in df.columns:
        df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce").astype(str)
    return df.to_dict("records")


def import_flood_data(path):
    """Imports a legacy flood SQLite DB or a *_flood_data.csv event file."""
    if not os.path.exists(path):
        return f"File not found: {path}"
    try:
        if path.lower().endswith(".db"):
            conn = sqlite3.connect(path)
            try:
                df = pd.read_sql_query("SELECT * FROM observations", conn)
            finally:
                conn.close()
            fallback = "LegacyImport"
        else:
            df = pd.read_csv(path)
            name = os.path.basename(path)
            fallback = name.replace("_flood_data.csv", "") or "LegacyImport"
        rows = _flood_rows_from_df(df, fallback)
        inserted = database.insert_rows("flood_observations", rows, ignore_duplicates=True)
        return f"Imported {inserted:,} new flood observations ({len(rows):,} read)."
    except Exception as e:
        log.exception("Flood import failed")
        return f"Import failed: {e}"


def import_flood_levels(path):
    if not os.path.exists(path):
        return f"File not found: {path}"
    try:
        df = pd.read_excel(path)
        rows = []
        for _, row in df.iterrows():
            name = str(row.get("Station Name", "")).strip()
            if not name:
                continue
            rows.append({
                "station_key": name.lower(),
                "station_name": name,
                "minor": pd.to_numeric(row.get("Minor Flood Level"), errors="coerce"),
                "moderate": pd.to_numeric(row.get("Moderate Flood Level"), errors="coerce"),
                "major": pd.to_numeric(row.get("Major Flood Level"), errors="coerce"),
            })
        # NaN -> None so SQLite stores NULL
        for r in rows:
            for key in ("minor", "moderate", "major"):
                if pd.isna(r[key]):
                    r[key] = None
        database.execute("DELETE FROM flood_levels")
        database.insert_rows("flood_levels", rows, ignore_duplicates=True)
        return f"Loaded flood levels for {len(rows):,} stations."
    except Exception as e:
        log.exception("Flood levels import failed")
        return f"Import failed: {e}"


def import_power_timeseries(path):
    if not os.path.exists(path):
        return f"File not found: {path}"
    try:
        df = pd.read_csv(path)
        df = df.rename(columns={
            "Customers Off": "customers_off",
            "Power Dependant Customers Off": "power_dependant_off",
            "Planned": "planned",
            "Unplanned": "unplanned",
        })
        ts = pd.to_datetime(df["timestamp"], errors="coerce", utc=True)
        df["timestamp"] = (ts.dt.tz_convert("Australia/Melbourne")
                             .dt.tz_localize(None)
                             .dt.strftime("%Y-%m-%d %H:%M:%S"))
        rows = []
        for _, row in df.iterrows():
            if pd.isna(row["timestamp"]) or row["timestamp"] == "NaT":
                continue
            rows.append({
                "timestamp": row["timestamp"],
                "customers_off": pd.to_numeric(row.get("customers_off"), errors="coerce"),
                "power_dependant_off": pd.to_numeric(row.get("power_dependant_off"), errors="coerce"),
                "planned": pd.to_numeric(row.get("planned"), errors="coerce"),
                "unplanned": pd.to_numeric(row.get("unplanned"), errors="coerce"),
            })
        for r in rows:
            for key in ("customers_off", "power_dependant_off", "planned", "unplanned"):
                r[key] = None if pd.isna(r[key]) else int(r[key])
        inserted = database.insert_rows("power_timeseries", rows)
        return f"Imported {inserted:,} power readings."
    except Exception as e:
        log.exception("Power timeseries import failed")
        return f"Import failed: {e}"


def import_outage_tracker(path):
    if not os.path.exists(path):
        return f"File not found: {path}"
    try:
        df = pd.read_csv(path)
        rows = []
        for _, row in df.iterrows():
            restored = row.get("Restored")
            rows.append({
                "location": row.get("Location"),
                "customers_off": pd.to_numeric(row.get("Customers Off"), errors="coerce"),
                "type": row.get("Type"),
                "first_seen": str(pd.to_datetime(row.get("First Seen"), errors="coerce")),
                "last_seen": str(pd.to_datetime(row.get("Last Seen"), errors="coerce")),
                "restored": 1 if str(restored).strip().lower() in ("true", "1") else 0,
                "duration_mins": pd.to_numeric(row.get("Duration (mins)"), errors="coerce"),
            })
        for r in rows:
            if pd.isna(r["customers_off"]):
                r["customers_off"] = None
            if pd.isna(r["duration_mins"]):
                r["duration_mins"] = None
        inserted = database.insert_rows("power_outages", rows)
        return f"Imported {inserted:,} outage tracker records."
    except Exception as e:
        log.exception("Outage tracker import failed")
        return f"Import failed: {e}"


def import_geo_cache(path):
    if not os.path.exists(path):
        return f"File not found: {path}"
    try:
        with open(path, "r", encoding="utf-8") as f:
            cache = json.load(f)
        rows = [{"location": loc, "latitude": coords[0], "longitude": coords[1]}
                for loc, coords in cache.items()
                if isinstance(coords, (list, tuple)) and len(coords) == 2]
        inserted = database.insert_rows("geocode_cache", rows, ignore_duplicates=True)
        return f"Imported {inserted:,} cached locations ({len(rows):,} read)."
    except Exception as e:
        log.exception("Geo cache import failed")
        return f"Import failed: {e}"

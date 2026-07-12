"""Single SQLite database for all modules."""
import functools
import glob
import logging
import os
import sqlite3
from datetime import datetime

import pandas as pd

from app.config import BASE_DIR

log = logging.getLogger(__name__)

BACKUP_DIR = os.path.join(BASE_DIR, "backups")


def _self_heal(fn):
    """If the DB file was deleted/recreated empty at runtime (so tables are
    missing), rebuild the schema once and retry. Prevents a vanished DB from
    crashing the UI; init_db is idempotent so this is safe."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (sqlite3.OperationalError, pd.errors.DatabaseError) as e:
            if "no such table" not in str(e):
                raise
            log.warning("Schema missing (%s) — rebuilding and retrying", e)
            init_db()
            return fn(*args, **kwargs)
    return wrapper

DB_FILE = os.path.join(BASE_DIR, "unified_monitor.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS flood_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event TEXT NOT NULL,
    catchment TEXT,
    station_name TEXT,
    station_type TEXT,
    time_day TEXT,
    height_m REAL,
    gauge_datum TEXT,
    tendency TEXT,
    crossing_m TEXT,
    classification TEXT,
    recent_data TEXT,
    timestamp TEXT
);
-- Dedup on the real observation timestamp so backfilled history and live
-- readings share one key and exact repeats are skipped (see migration below).
CREATE UNIQUE INDEX IF NOT EXISTS idx_flood_obs_unique2
    ON flood_observations (event, station_name, timestamp, height_m);
CREATE INDEX IF NOT EXISTS idx_flood_obs_event ON flood_observations (event);

-- One row per collection cycle, proving the monitor was running even when no
-- new observations arrived (the "heartbeat").
CREATE TABLE IF NOT EXISTS flood_heartbeat (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    stations_seen INTEGER,
    new_rows INTEGER
);
CREATE INDEX IF NOT EXISTS idx_flood_hb_event ON flood_heartbeat (event);

CREATE TABLE IF NOT EXISTS flood_levels (
    station_key TEXT PRIMARY KEY,   -- lowercased station name used for matching
    station_name TEXT,
    minor REAL,
    moderate REAL,
    major REAL
);

-- Height->impact rows extracted from VICSES Local Flood Guides, one row per
-- expected impact / historical flood level at a gauge height. Reloaded from
-- seed/lfg_impacts.json on every boot (seed file is the source of truth).
CREATE TABLE IF NOT EXISTS gauge_impacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    station_key TEXT NOT NULL,      -- lowercased station name (= flood_levels key)
    gauge_name TEXT,
    town TEXT,                      -- community the guide is written for
    source_pdf TEXT,                -- Local Flood Guide filename
    height_m REAL NOT NULL,
    impact TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_gauge_impacts_key ON gauge_impacts (station_key);

CREATE TABLE IF NOT EXISTS power_timeseries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    customers_off INTEGER,
    power_dependant_off INTEGER,
    planned INTEGER,
    unplanned INTEGER
);
CREATE INDEX IF NOT EXISTS idx_power_ts_time ON power_timeseries (timestamp);

CREATE TABLE IF NOT EXISTS power_outages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    location TEXT NOT NULL,
    customers_off INTEGER,
    type TEXT,
    first_seen TEXT,
    last_seen TEXT,
    restored INTEGER NOT NULL DEFAULT 0,
    duration_mins REAL
);
CREATE INDEX IF NOT EXISTS idx_outages_loc ON power_outages (location, restored);

CREATE TABLE IF NOT EXISTS geocode_cache (
    location TEXT PRIMARY KEY,
    latitude REAL,
    longitude REAL
);

-- VicEmergency incidents + community warnings, upserted each cycle on the
-- feed's stable feature id. An incident that drops out of the feed (or goes
-- Safe/Complete) is marked resolved rather than deleted, so history is kept.
CREATE TABLE IF NOT EXISTS fire_incidents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id TEXT UNIQUE,       -- VicEmergency feature id (stable across cycles)
    feed_type TEXT,              -- incident | warning
    category1 TEXT,              -- Fire, Tree Down, Advice, Watch and Act, ...
    category2 TEXT,
    event TEXT,                  -- cap.event for warnings (e.g. Riverine Flood)
    warning_level TEXT,          -- Advice | Watch and Act | Emergency Warning (warnings)
    severity TEXT,               -- cap.severity (Minor/Moderate/Severe/Extreme)
    status TEXT,                 -- Going / Under Control / Safe / ...
    size TEXT,                   -- descriptive (Small/Medium/Large); feed has no ha
    resources INTEGER,
    location TEXT,
    source_org TEXT,
    action TEXT,
    headline TEXT,
    url TEXT,
    latitude REAL,
    longitude REAL,
    created TEXT,
    updated TEXT,
    first_seen TEXT,
    last_seen TEXT,
    resolved INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_fire_incidents_active ON fire_incidents (resolved, category1);

-- One aggregate row per collection cycle: KPI history for trend graphs and the
-- continuity heartbeat (proves the collector ran even with no active events).
CREATE TABLE IF NOT EXISTS fire_timeseries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    total_active INTEGER,
    active_fires INTEGER,
    emergency_warnings INTEGER,
    watch_act INTEGER,
    advice INTEGER
);
CREATE INDEX IF NOT EXISTS idx_fire_ts_time ON fire_timeseries (timestamp);

-- Event tags: named date ranges applied over the always-on data stream. An
-- event is no longer a collection-time label but a (name, start, end) window
-- used to slice flood + power data for viewing and export. NULL end = ongoing.
CREATE TABLE IF NOT EXISTS event_tags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    start_ts TEXT NOT NULL,
    end_ts TEXT,
    notes TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_event_tags_start ON event_tags (start_ts);
"""


def get_connection():
    return sqlite3.connect(DB_FILE, timeout=30)


def backup_db(keep=15):
    """Snapshot the database to backups/ on startup, keeping the most recent
    `keep` copies. Cheap insurance against accidental deletion/corruption so
    previous events are never permanently lost."""
    if not os.path.exists(DB_FILE):
        return
    try:
        # Only bother if there's actually data to protect.
        src = sqlite3.connect(DB_FILE)
        try:
            has_data = src.execute(
                "SELECT EXISTS(SELECT 1 FROM flood_observations LIMIT 1)").fetchone()[0]
        except sqlite3.OperationalError:
            has_data = True  # table missing? still snapshot what's there
        if not has_data:
            src.close()
            return
        os.makedirs(BACKUP_DIR, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        dest_path = os.path.join(BACKUP_DIR, f"unified_monitor_{stamp}.db")
        dest = sqlite3.connect(dest_path)
        with dest:
            src.backup(dest)  # consistent even with WAL active
        dest.close()
        src.close()
        backups = sorted(glob.glob(os.path.join(BACKUP_DIR, "unified_monitor_*.db")))
        for old in backups[:-keep]:
            try:
                os.remove(old)
            except OSError:
                pass
        log.info("Database backed up to %s", dest_path)
    except Exception as e:
        log.warning("Database backup failed (non-fatal): %s", e)


def init_db():
    backup_db()
    conn = get_connection()
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
        # Migration: drop the old (time_day-based) flood dedup index so the new
        # timestamp-based one in SCHEMA takes over. Safe to run repeatedly.
        conn.execute("DROP INDEX IF EXISTS idx_flood_obs_unique")
        _migrate_events_to_tags(conn)
        conn.commit()
    finally:
        conn.close()
    log.info("Database ready at %s", DB_FILE)


def _migrate_events_to_tags(conn):
    """Turn pre-existing named flood events into tags so past incidents stay
    selectable under the new date-range model. Idempotent: skips the always-on
    'live' bucket and any event that already has a tag of the same name."""
    try:
        existing = {r[0] for r in conn.execute("SELECT name FROM event_tags")}
        rows = conn.execute(
            "SELECT event, MIN(timestamp), MAX(timestamp) "
            "FROM flood_observations "
            "WHERE event IS NOT NULL AND event != 'live' "
            "GROUP BY event").fetchall()
    except sqlite3.OperationalError:
        return  # tables not ready yet
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for event, start_ts, end_ts in rows:
        if not event or event in existing or not start_ts:
            continue
        conn.execute(
            "INSERT INTO event_tags (name, start_ts, end_ts, notes, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            [event, start_ts, end_ts,
             "Migrated from a named collection event.", now])
        log.info("Migrated event '%s' to a tag (%s -> %s)", event, start_ts, end_ts)


@_self_heal
def read_df(query, params=None):
    conn = get_connection()
    try:
        return pd.read_sql_query(query, conn, params=params or [])
    finally:
        conn.close()


@_self_heal
def insert_rows(table, rows, ignore_duplicates=False):
    """Insert a list of dicts. Returns number of rows actually inserted."""
    if not rows:
        return 0
    cols = list(rows[0].keys())
    verb = "INSERT OR IGNORE" if ignore_duplicates else "INSERT"
    sql = f"{verb} INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})"
    conn = get_connection()
    try:
        cur = conn.executemany(sql, [[r.get(c) for c in cols] for r in rows])
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


@_self_heal
def execute(sql, params=None):
    conn = get_connection()
    try:
        cur = conn.execute(sql, params or [])
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()

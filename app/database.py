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

-- BoM warnings (api.weather.bom.gov.au /warnings), upserted on the BoM id.
-- A warning no longer in the feed (or past expiry) is marked inactive, kept.
CREATE TABLE IF NOT EXISTS weather_warnings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    warning_id TEXT UNIQUE,      -- BoM id, e.g. IDV36620
    type TEXT,                   -- flood_warning, severe_weather_warning, ...
    title TEXT,
    short_title TEXT,
    group_type TEXT,             -- minor / moderate / major / severe
    phase TEXT,                  -- new / update / final / cancel
    state TEXT,
    issue_time TEXT,
    expiry_time TEXT,
    message TEXT,                 -- latest full warning body (HTML; may embed images)
    first_seen TEXT,
    last_seen TEXT,
    active INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_weather_warn_active ON weather_warnings (active, type);

-- One row per issued version of a warning (BoM reissues keep the same id but
-- bump issue_time), so a warning's development can be replayed. De-duped on
-- (warning_id, issue_time).
CREATE TABLE IF NOT EXISTS weather_warning_updates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    warning_id TEXT NOT NULL,
    issue_time TEXT,
    phase TEXT,
    title TEXT,
    message TEXT,
    captured_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_weather_warn_updates
    ON weather_warning_updates (warning_id, issue_time);

-- Monitored rainfall locations, resolved to a BoM geohash (derived from flood
-- gauge towns/catchments, cached here so we only geocode once).
CREATE TABLE IF NOT EXISTS weather_locations (
    location_key TEXT PRIMARY KEY,   -- lowercased town/catchment name
    name TEXT,
    geohash TEXT,
    latitude REAL,
    longitude REAL,
    catchment TEXT
);

-- Per-location rainfall readings (rain since 9am), de-duped on (location, ts).
CREATE TABLE IF NOT EXISTS rainfall_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    location_key TEXT NOT NULL,
    name TEXT,
    latitude REAL,
    longitude REAL,
    rain_since_9am_mm REAL,
    forecast_max_mm REAL,        -- today's forecast rain upper bound (leading indicator)
    forecast_chance INTEGER,
    timestamp TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_rainfall_unique
    ON rainfall_observations (location_key, timestamp);

-- BoM AWS station registry (wmo -> name + coords), seeded from per-station JSON.
CREATE TABLE IF NOT EXISTS aws_stations (
    wmo TEXT PRIMARY KEY,
    name TEXT,
    latitude REAL,
    longitude REAL
);

-- Every AWS rain-since-9am reading, kept for after-the-fact interrogation and
-- tagging (like flood/power). De-duped on the BoM observation time so polling
-- more often than BoM updates adds nothing. Event totals are derived from the
-- positive increments (a drop = the 9am reset), so totals survive resets.
CREATE TABLE IF NOT EXISTS rainfall_aws (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wmo TEXT NOT NULL,
    name TEXT,
    rain_since_9am_mm REAL,
    obs_time TEXT NOT NULL,
    timestamp TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_rainfall_aws_unique
    ON rainfall_aws (wmo, obs_time);
CREATE INDEX IF NOT EXISTS idx_rainfall_aws_time ON rainfall_aws (timestamp);

-- One row per weather collection cycle: KPI counts + continuity heartbeat.
CREATE TABLE IF NOT EXISTS weather_heartbeat (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    vic_warnings INTEGER,
    locations_polled INTEGER,
    new_warnings INTEGER
);
CREATE INDEX IF NOT EXISTS idx_weather_hb_time ON weather_heartbeat (timestamp);

-- Lightweight, privacy-preserving page analytics. visitor_hash is a daily
-- salted hash of IP+User-Agent (no raw IP/PII stored); it lets us count unique
-- visitors per day without identifying anyone.
CREATE TABLE IF NOT EXISTS page_views (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    path TEXT,
    visitor_hash TEXT,
    is_admin INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_page_views_time ON page_views (timestamp);

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
    geometry TEXT,               -- raw GeoJSON geometry (for polygon rendering)
    created TEXT,
    updated TEXT,
    first_seen TEXT,
    last_seen TEXT,
    resolved INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_fire_incidents_active ON fire_incidents (resolved, feed_type);

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

-- Radar frames processed by the storm tracker, de-duped on the frame's OWN
-- BoM timestamp so re-polling never double-processes an unchanged image
-- (the standalone project's fetch-time naming produced duplicate frames).
CREATE TABLE IF NOT EXISTS storm_frames (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    radar_id TEXT NOT NULL,
    frame_ts TEXT NOT NULL,      -- radar observation time (local)
    fetched_at TEXT,
    cells_detected INTEGER
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_storm_frames_unique
    ON storm_frames (radar_id, frame_ts);

-- One row per tracked cell per frame: position (image px), size (km²),
-- palette levels seen, score/classification, and smoothed motion (real km/h
-- + compass bearing from the frames' own timestamps and the radar km/px scale).
CREATE TABLE IF NOT EXISTS storm_cells (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cell_id TEXT NOT NULL,
    radar_id TEXT,
    frame_ts TEXT NOT NULL,
    centroid_x REAL,
    centroid_y REAL,
    latitude REAL,               -- georeferenced via the radar site + km/px
    longitude REAL,
    area_km2 REAL,
    max_level INTEGER,
    mean_level REAL,
    intensity_score REAL,
    classification TEXT,         -- strong / moderate / weak
    speed_kmh REAL,
    bearing_deg REAL,
    status TEXT,
    impact_geojson TEXT          -- GeoJSON Feature: impact-area polygon (lon/lat)
);
CREATE INDEX IF NOT EXISTS idx_storm_cells_ts ON storm_cells (frame_ts);
CREATE INDEX IF NOT EXISTS idx_storm_cells_cell ON storm_cells (cell_id);

-- Change-only alert log: a cell reaching moderate/strong for the first time
-- or escalating writes ONE row (never one per frame it persists).
CREATE TABLE IF NOT EXISTS storm_alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    cell_id TEXT,
    alert_type TEXT,             -- new_cell / escalation
    classification TEXT,
    message TEXT
);
CREATE INDEX IF NOT EXISTS idx_storm_alerts_time ON storm_alerts (timestamp);

-- One row per storm collection cycle: KPI counts + continuity heartbeat.
CREATE TABLE IF NOT EXISTS storm_timeseries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    frames_processed INTEGER,
    active_cells INTEGER,
    strong_cells INTEGER,
    moderate_cells INTEGER,
    max_intensity REAL
);
CREATE INDEX IF NOT EXISTS idx_storm_ts_time ON storm_timeseries (timestamp);

-- VicRoads / Transport Victoria "Unplanned Disruptions - Road" GeoJSON feed,
-- upserted each cycle on the disruption's stable feed id. A disruption that
-- drops out of the feed (road reopened) is marked resolved rather than deleted,
-- so history is kept — same policy as fire_incidents.
CREATE TABLE IF NOT EXISTS road_disruptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id TEXT UNIQUE,       -- feed disruption id (stable across cycles)
    status TEXT,                 -- e.g. Closed / Partially closed / Open
    disruption_type TEXT,        -- reason/category (crash, flooding, works, ...)
    is_closure INTEGER NOT NULL DEFAULT 0,  -- 1 when the road is fully closed
    road_name TEXT,
    location TEXT,               -- descriptive locality / between-streets text
    direction TEXT,
    lanes_affected TEXT,
    lga TEXT,                    -- local government area
    ses_region TEXT,             -- reference.closedRoadSESRegion (SES grouping)
    transport_region TEXT,       -- reference.closedRoadTransportRegion
    description TEXT,            -- public advice text
    latitude REAL,
    longitude REAL,
    geometry TEXT,               -- raw GeoJSON geometry (LineString/Polygon render)
    start_time TEXT,
    end_time TEXT,
    created TEXT,
    updated TEXT,
    first_seen TEXT,
    last_seen TEXT,
    resolved INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_road_disruptions_active
    ON road_disruptions (resolved, is_closure);

-- One aggregate row per collection cycle: KPI history + continuity heartbeat
-- (proves the collector ran even with no active disruptions).
CREATE TABLE IF NOT EXISTS road_timeseries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    total_active INTEGER,
    closures INTEGER,
    other_disruptions INTEGER
);
CREATE INDEX IF NOT EXISTS idx_road_ts_time ON road_timeseries (timestamp);

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
        _ensure_column(conn, "fire_incidents", "geometry", "TEXT")
        _ensure_column(conn, "weather_warnings", "message", "TEXT")
        _ensure_column(conn, "storm_cells", "latitude", "REAL")
        _ensure_column(conn, "storm_cells", "longitude", "REAL")
        _ensure_column(conn, "storm_cells", "impact_geojson", "TEXT")
        _ensure_column(conn, "road_disruptions", "ses_region", "TEXT")
        _ensure_column(conn, "road_disruptions", "transport_region", "TEXT")
        _migrate_events_to_tags(conn)
        conn.commit()
    finally:
        conn.close()
    log.info("Database ready at %s", DB_FILE)


def _ensure_column(conn, table, column, decl):
    """Add a column to an existing table if it's missing (idempotent). Lets a
    new column reach databases created before it was added to the schema."""
    try:
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
    except sqlite3.OperationalError:
        return  # table not present yet; CREATE in SCHEMA already covers it
    if cols and column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        log.info("Added column %s.%s", table, column)


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

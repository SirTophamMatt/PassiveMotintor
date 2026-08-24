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
-- EXPRESSION index, and it has to be one: every per-gauge lookup in the app
-- matches on LOWER(TRIM(station_name)) (station_latest, station_history,
-- flood trend analysis, projection verification), which no plain index on
-- station_name can serve — SQLite falls back to scanning the whole table.
-- At VPS scale (~1.4M rows) that is ~390 ms per lookup, and the 60-second
-- intel pass issues hundreds of them. Measured end-to-end on a 1.4M-row DB
-- with a 300-projection backlog: one cycle took 103.6 s against a 60 s
-- interval, which is what took the server down on 2026-08-09; with this index
-- the same cycle is 3.1 s. Created here (not via _ensure_column) because
-- init_db replays the whole schema on every boot, so existing DBs pick it up
-- automatically — it takes ~2 s to build on 1.4M rows.
CREATE INDEX IF NOT EXISTS idx_flood_obs_station_ts
    ON flood_observations (LOWER(TRIM(station_name)), timestamp);

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

-- Flood gauge coordinates, matched from BoM Water Data Online (KiWIS) station
-- names to our flood-warning gauge names. Lets flood gauges render on the map
-- (the BoM flood feed itself carries no lat/lons). Reloaded from
-- seed/gauge_coords.json on every boot (seed is the source of truth), same
-- policy as flood_levels / gauge_impacts. Only matched gauges are stored.
CREATE TABLE IF NOT EXISTS gauge_coords (
    station_key TEXT PRIMARY KEY,   -- lowercased station name (= flood_levels key)
    station_name TEXT,
    latitude REAL,
    longitude REAL,
    kiwis_no TEXT,                  -- BoM Water Data Online station number (AWRC)
    kiwis_name TEXT,                -- the matched KiWIS station name (for audit)
    confidence TEXT,               -- high / medium / low / manual
    method TEXT                    -- exact / subset / fuzzy / manual
);

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

-- Every AWS observation, kept for after-the-fact interrogation and tagging
-- (like flood/power). De-duped on the BoM observation time so polling more
-- often than BoM updates adds nothing. Event totals are derived from the
-- positive increments (a drop = the 9am reset), so totals survive resets.
-- Named `rainfall_aws` for history: it began as rain-only and grew the rest of
-- the AWS observation set in 2026-08 (Phase A). The table was deliberately NOT
-- renamed — every existing rainfall query, export and event total keeps working
-- and no historical row had to be rewritten. Weather columns are NULL for rows
-- collected before that, which is the honest answer: they were never observed.
CREATE TABLE IF NOT EXISTS rainfall_aws (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wmo TEXT NOT NULL,
    name TEXT,
    rain_since_9am_mm REAL,
    obs_time TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    -- Current-observation weather fields (all nullable: BoM prints "-" when a
    -- station doesn't report a field, and that must stay NULL, never 0).
    temperature_c REAL,
    apparent_temperature_c REAL,
    dew_point_c REAL,
    relative_humidity_pct REAL,
    delta_t_c REAL,
    wind_direction TEXT,             -- compass point, or CALM
    wind_speed_kmh REAL,
    wind_gust_kmh REAL,
    pressure_msl_hpa REAL,
    -- Daily highest-gust summary (BoM prints value and time in one cell).
    max_gust_direction TEXT,
    max_gust_kmh REAL,
    max_gust_time TEXT               -- local clock time as published, e.g. 02:40pm
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

-- Lightweight page analytics. visitor_hash is a daily salted hash of
-- IP+User-Agent, letting us count unique visitors per day. ip_prefix and the
-- country/region/city columns (added 2026-08-24) carry the visitor's coarse
-- origin: the IP is TRUNCATED before storage (last octet zeroed for IPv4, the
-- interface identifier dropped for IPv6), so location resolves to city level
-- but no individual address is ever written to disk. See app/geoip.py.
CREATE TABLE IF NOT EXISTS page_views (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    path TEXT,
    visitor_hash TEXT,
    is_admin INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_page_views_time ON page_views (timestamp);

-- Coarse visitor geolocation, resolved from the TRUNCATED client IP. Cached so
-- each /24 (or IPv6 /48) network is looked up once, not once per page view --
-- the free geolocation providers are rate-limited and a lookup must never sit
-- in the render path. `status` records the outcome, so a private-range or
-- unresolvable address is remembered instead of being retried on every hit.
CREATE TABLE IF NOT EXISTS ip_geo_cache (
    ip_prefix TEXT PRIMARY KEY,     -- truncated IP, e.g. 203.0.113.0
    country TEXT,
    country_code TEXT,
    region TEXT,                    -- state / province
    city TEXT,
    latitude REAL,
    longitude REAL,
    org TEXT,                       -- ISP / carrier, useful for spotting bots
    status TEXT,                    -- ok / private / failed
    looked_up_at TEXT
);

-- Bug reports and suggestions submitted from the in-app feedback form. Every
-- submission is stored here FIRST and emailed second, so an unconfigured or
-- broken SMTP server loses nothing: `email_status` records whether the mail
-- got out, and the Admin page can retry the ones that did not.
CREATE TABLE IF NOT EXISTS feedback_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ref TEXT UNIQUE NOT NULL,       -- human-facing ID, e.g. WD-BUG-260824-4XKQ
    kind TEXT NOT NULL,             -- bug / suggestion
    submitted_at TEXT NOT NULL,
    subject TEXT,
    message TEXT NOT NULL,
    reporter_name TEXT,
    reporter_email TEXT,
    severity TEXT,                  -- bug reports only: low / medium / high
    page_path TEXT,                 -- page the reporter was on when reporting
    user_agent TEXT,
    ip_prefix TEXT,                 -- truncated, same treatment as page_views
    email_status TEXT,              -- sent / failed / skipped
    email_error TEXT,
    status TEXT NOT NULL DEFAULT 'new',   -- new / open / closed
    admin_notes TEXT
);
CREATE INDEX IF NOT EXISTS idx_feedback_submitted
    ON feedback_reports (submitted_at);
CREATE INDEX IF NOT EXISTS idx_feedback_status ON feedback_reports (status);

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

-- Intelligence Feed: append-only journal of DETECTED CHANGES across every
-- module ("Walwa fire increased 38 ha", "Major flood threshold crossed").
-- Written only by app/intel_feed.py, only when something actually moved, so
-- the feed reads as a change log rather than a state dump. Never deleted.
CREATE TABLE IF NOT EXISTS intel_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,             -- when the change became true (SOURCE time)
    detected_at TEXT NOT NULL,    -- when the detector noticed it
    hazard TEXT NOT NULL,         -- fire|flood|power|storm|weather|roads|rainfall
    entity_key TEXT,              -- stable id within the hazard
    entity_name TEXT,             -- display name ("Snowy River at Orbost")
    kind TEXT NOT NULL,           -- growth|threshold|escalation|new|cleared|...
    severity INTEGER NOT NULL,    -- 3 critical / 2 major / 1 notable / 0 info
    headline TEXT NOT NULL,       -- "Walwa fire increased 38 ha"
    metric TEXT,                  -- "area_ha", "height_m", "customers_off", ...
    prev_value REAL,
    new_value REAL,
    unit TEXT,
    prev_label TEXT,              -- non-numeric transitions (Moderate -> Strong)
    new_label TEXT,
    since TEXT,                   -- timestamp of the value we moved FROM
    rate TEXT,                    -- "Rising 0.18 m/hr", "+82% over 30 minutes"
    detail TEXT,                  -- JSON list of extra context lines
    latitude REAL,
    longitude REAL,
    url TEXT                      -- deep link into the owning page
);
CREATE INDEX IF NOT EXISTS idx_intel_events_ts ON intel_events (ts DESC);
-- One row per (entity, metric, moment): re-running the detector over the same
-- data can never duplicate an entry, so restarts and overlapping cycles are safe.
CREATE UNIQUE INDEX IF NOT EXISTS idx_intel_events_dedup
    ON intel_events (hazard, entity_key, metric, kind, ts);

-- Metric history for entities whose own table is an UPSERT (fire area, per-
-- location outage counts, road status) and therefore keeps no past values.
-- A row is written ONLY when the value changes, so this stays small; it is
-- what makes "214 -> 296 ha since 12:05" possible for those sources.
CREATE TABLE IF NOT EXISTS intel_metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    hazard TEXT NOT NULL,
    entity_key TEXT NOT NULL,
    metric TEXT NOT NULL,
    value REAL,
    label TEXT,
    ts TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_intel_metrics_lookup
    ON intel_metrics (hazard, entity_key, metric, ts DESC);

-- Generic state-change journal for entities whose own table is an UPSERT and
-- therefore keeps no past state (fire incidents, road disruptions, per-location
-- power outages, weather warnings). This is what makes Event Replay able to
-- answer "what did Passive Monitor know at 14:30".
--
-- It is a CHANGE JOURNAL, not a poll-by-poll snapshot table: a row is written
-- only when an entity's relevant state actually differs from its last recorded
-- state (compared by `state_hash` over canonical JSON). At a 60-second poll a
-- snapshot table would write ~1,440 rows/entity/day; this writes one per actual
-- change, which for a typical incident is a handful over its whole life.
--
-- Sources that ALREADY keep true history (flood_observations, storm_cells,
-- rainfall_aws) are deliberately NOT duplicated here — they stay the primary
-- historical source for their own layer.
CREATE TABLE IF NOT EXISTS entity_state_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,         -- fire | roads | power | weather_warning
    entity_key TEXT NOT NULL,     -- stable id within the source
    effective_ts TEXT NOT NULL,   -- when the state became true (SOURCE time)
    recorded_at TEXT NOT NULL,    -- when we noticed it
    active INTEGER NOT NULL DEFAULT 1,  -- 0 = resolved/withdrawn (tombstone)
    state_json TEXT NOT NULL,     -- the entity's state at that moment
    state_hash TEXT NOT NULL,     -- hash of canonical state_json, for de-dup
    latitude REAL,
    longitude REAL
);
-- Reconstruction is always "latest row per entity at or before T, for one
-- source", so both indexes are lookups that query plans actually use.
CREATE INDEX IF NOT EXISTS idx_entity_hist_source_ts
    ON entity_state_history (source, effective_ts);
CREATE INDEX IF NOT EXISTS idx_entity_hist_entity
    ON entity_state_history (source, entity_key, effective_ts);
-- Re-running a collector over unchanged data can never double-write.
CREATE UNIQUE INDEX IF NOT EXISTS idx_entity_hist_unique
    ON entity_state_history (source, entity_key, effective_ts, state_hash);

-- When each source's change journal started. Replay uses this to say plainly
-- how far back full-fidelity reconstruction actually goes, instead of implying
-- it can rebuild moments it was never running for. Written once per source, on
-- the first cycle that records anything.
CREATE TABLE IF NOT EXISTS history_availability (
    source TEXT PRIMARY KEY,
    available_from TEXT NOT NULL,
    note TEXT
);

-- Flood trend projections and their VERIFICATION (app/modules/flood/trend.py).
-- Every ETA the app shows is written here when it is made, then scored against
-- the observations that followed. That is what lets the UI publish a real hit
-- rate next to the projection instead of asking anyone to take it on trust.
-- A projection is never edited except to record its outcome, and never deleted.
CREATE TABLE IF NOT EXISTS flood_projections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    station_key TEXT NOT NULL,
    station_name TEXT,
    made_at TEXT NOT NULL,        -- when the projection was computed
    observed_at TEXT NOT NULL,    -- obs time of the latest reading it used
    current_height REAL,
    target_kind TEXT,             -- class | impact
    target_name TEXT,             -- minor/moderate/major, or the impact text
    target_height REAL,
    distance_m REAL,
    rate_m_hr REAL,
    rate_stderr REAL,
    accel_m_hr2 REAL,
    accel_label TEXT,
    readings_used INTEGER,
    span_minutes INTEGER,
    eta_point TEXT,
    eta_early TEXT,
    eta_late TEXT,
    lead_minutes REAL,            -- eta_point - observed_at, for lead bucketing
    rainfall_mm REAL,
    method TEXT NOT NULL,         -- maths version, so scores aren't mixed
    -- filled in by verify_projections()
    outcome TEXT NOT NULL DEFAULT 'pending',  -- pending|reached|not_reached|receded
    actual_ts TEXT,
    error_minutes REAL,           -- actual - eta_point (+ = arrived late)
    within_range INTEGER,         -- landed inside the quoted early..late window
    verified_at TEXT
);
-- One projection per gauge per threshold per observation: the detector can run
-- as often as it likes without inflating the sample it is later scored on.
CREATE UNIQUE INDEX IF NOT EXISTS idx_flood_proj_unique
    ON flood_projections (station_key, target_name, observed_at, method);
CREATE INDEX IF NOT EXISTS idx_flood_proj_outcome
    ON flood_projections (outcome, station_key);
"""


# Weather columns added to `rainfall_aws` in Phase A. Kept as data (not inline
# _ensure_column calls) so the collector can import the canonical list instead
# of repeating the field names — one place to add a field in future.
AWS_WEATHER_COLUMNS = [
    ("temperature_c", "REAL"),
    ("apparent_temperature_c", "REAL"),
    ("dew_point_c", "REAL"),
    ("relative_humidity_pct", "REAL"),
    ("delta_t_c", "REAL"),
    ("wind_direction", "TEXT"),
    ("wind_speed_kmh", "REAL"),
    ("wind_gust_kmh", "REAL"),
    ("pressure_msl_hpa", "REAL"),
    ("max_gust_direction", "TEXT"),
    ("max_gust_kmh", "REAL"),
    ("max_gust_time", "TEXT"),
]


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
        # Visitor geolocation (2026-08-24). page_views predates it, so the
        # columns are added here rather than in SCHEMA -- existing rows keep
        # NULLs and the analytics queries report those as "Unknown".
        for _col in ("ip_prefix", "country", "country_code", "region", "city"):
            _ensure_column(conn, "page_views", _col, "TEXT")
        # AWS observations grew from rain-only to the full BoM field set
        # (2026-08, Phase A). Purely additive: existing rows keep their rainfall
        # and get NULL weather, so rainfall history/exports are untouched.
        for col, decl in AWS_WEATHER_COLUMNS:
            _ensure_column(conn, "rainfall_aws", col, decl)
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
    """Insert a list of dicts. Returns number of rows actually inserted.

    The column list is the UNION of every row's keys, in first-seen order, so a
    ragged batch (rows with different key sets — e.g. a fire incident with no
    warning_level followed by a warning that has one) binds NULL for the keys a
    given row is missing instead of silently dropping the extra values. For a
    single row or a uniform batch this is exactly rows[0].keys()."""
    if not rows:
        return 0
    cols = list(rows[0].keys())
    seen = set(cols)
    for r in rows[1:]:
        for c in r:
            if c not in seen:
                seen.add(c)
                cols.append(c)
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

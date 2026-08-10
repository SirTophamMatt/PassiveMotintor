"""AWS storage: additive migration, de-duplication, and the guarantee that the
rainfall history the app already depends on keeps working unchanged.
"""
import sqlite3

import pandas as pd
import pytest

from app import database
from app.modules.weather import aws
from app.modules.weather import data as weather_data
from tests.conftest import load_fixture

# The rainfall_aws table exactly as it existed before the observation fields
# were added — what a deployed database actually looks like on upgrade.
LEGACY_SCHEMA = """
CREATE TABLE rainfall_aws (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wmo TEXT NOT NULL,
    name TEXT,
    rain_since_9am_mm REAL,
    obs_time TEXT NOT NULL,
    timestamp TEXT NOT NULL
);
CREATE UNIQUE INDEX idx_rainfall_aws_unique ON rainfall_aws (wmo, obs_time);
"""

LEGACY_ROWS = [
    ("94839", "Charlton", 1.4, "2026-08-01 09:30", "2026-08-01 09:31:00"),
    ("94839", "Charlton", 3.8, "2026-08-01 10:00", "2026-08-01 10:01:00"),
    ("95839", "Nhill", 0.0, "2026-08-01 09:30", "2026-08-01 09:31:00"),
]


@pytest.fixture
def legacy_db(monkeypatch, tmp_path):
    """A database carrying pre-migration AWS rainfall history."""
    path = str(tmp_path / "unified_monitor.db")
    conn = sqlite3.connect(path)
    conn.executescript(LEGACY_SCHEMA)
    conn.executemany(
        "INSERT INTO rainfall_aws (wmo, name, rain_since_9am_mm, obs_time, "
        "timestamp) VALUES (?, ?, ?, ?, ?)", LEGACY_ROWS)
    conn.commit()
    conn.close()
    monkeypatch.setattr(database, "DB_FILE", path)
    monkeypatch.setattr(database, "BACKUP_DIR", str(tmp_path / "backups"))
    return path


def _columns(path, table="rainfall_aws"):
    conn = sqlite3.connect(path)
    try:
        return [r[1] for r in conn.execute("PRAGMA table_info(%s)" % table)]
    finally:
        conn.close()


def test_migration_adds_weather_columns_to_an_existing_database(legacy_db):
    assert "temperature_c" not in _columns(legacy_db)
    database.init_db()
    cols = _columns(legacy_db)
    for name, _decl in database.AWS_WEATHER_COLUMNS:
        assert name in cols, name


def test_migration_preserves_existing_rainfall_history(legacy_db):
    database.init_db()
    conn = sqlite3.connect(legacy_db)
    try:
        rows = conn.execute(
            "SELECT wmo, name, rain_since_9am_mm, obs_time, timestamp "
            "FROM rainfall_aws ORDER BY id").fetchall()
    finally:
        conn.close()
    assert rows == LEGACY_ROWS
    # Never observed, so they must read as unknown rather than zero.
    assert weather_data.aws_observation_history("94839")["temperature_c"].isna().all()


def test_migration_is_idempotent(legacy_db):
    database.init_db()
    first = _columns(legacy_db)
    database.init_db()
    database.init_db()
    assert _columns(legacy_db) == first


def test_unique_wmo_obs_time_still_holds_after_migration(legacy_db):
    database.init_db()
    inserted = database.insert_rows("rainfall_aws", [{
        "wmo": "94839", "name": "Charlton", "rain_since_9am_mm": 1.4,
        "obs_time": "2026-08-01 09:30", "timestamp": "2026-08-01 12:00:00",
        "temperature_c": 11.0}], ignore_duplicates=True)
    assert inserted == 0


def test_event_totals_survive_the_migration_and_the_9am_reset(legacy_db):
    """The reset-proof rainfall total is the calculation most at risk from a
    schema change, so it is re-checked against a migrated database."""
    database.init_db()
    database.insert_rows("rainfall_aws", [
        # ... 3.8 mm by 10:00 (already in the legacy rows), then the 9am reset
        # drops the counter and 2.5 mm of fresh rain falls after it.
        {"wmo": "94839", "name": "Charlton", "rain_since_9am_mm": 6.0,
         "obs_time": "2026-08-01 11:00", "timestamp": "2026-08-01 11:01:00"},
        {"wmo": "94839", "name": "Charlton", "rain_since_9am_mm": 0.4,
         "obs_time": "2026-08-02 09:30", "timestamp": "2026-08-02 09:31:00"},
        {"wmo": "94839", "name": "Charlton", "rain_since_9am_mm": 2.5,
         "obs_time": "2026-08-02 10:30", "timestamp": "2026-08-02 10:31:00"},
    ], ignore_duplicates=True)
    totals = weather_data.aws_event_total()
    charlton = totals[totals["wmo"] == "94839"].iloc[0]
    # (3.8-1.4) + (6.0-3.8) + 0.4 (post-reset) + (2.5-0.4) = 7.1
    assert charlton["total_mm"] == pytest.approx(7.1)


def _stub_page(monkeypatch, fixture="vicall_sample.html"):
    """Serve the fixture instead of BoM, and keep coordinate seeding offline."""
    monkeypatch.setattr(
        aws, "_fetch",
        lambda url, timeout=30: load_fixture(fixture) if url == aws.VICALL_URL else None)


def test_fetch_stores_the_full_observation(db, monkeypatch):
    _stub_page(monkeypatch)
    assert aws.fetch_aws_observations() == 6

    df = weather_data.latest_aws_observations()
    charlton = df[df["wmo"] == "94839"].iloc[0]
    assert charlton["temperature_c"] == 10.1
    assert charlton["wind_direction"] == "W"
    assert charlton["wind_gust_kmh"] == 15.0
    assert charlton["max_gust_kmh"] == 46.0
    assert charlton["max_gust_time"] == "02:40pm"

    sheoaks = df[df["wmo"] == "94842"].iloc[0]
    assert sheoaks["wind_gust_kmh"] != 0
    assert pd.isna(sheoaks["wind_gust_kmh"])


def test_refetching_the_same_observations_adds_nothing(db, monkeypatch):
    _stub_page(monkeypatch)
    assert aws.fetch_aws_observations() == 6
    assert aws.fetch_aws_observations() == 0
    assert len(weather_data.latest_aws_observations()) == 6


def test_legacy_collector_entry_point_still_works(db, monkeypatch):
    """collector.py, the watchdog and the Admin button all call this name."""
    _stub_page(monkeypatch)
    assert aws.fetch_aws_rainfall() == 6


def test_latest_observations_returns_only_the_newest_row_per_station(db):
    database.insert_rows("rainfall_aws", [
        {"wmo": "94839", "name": "Charlton", "obs_time": "2026-08-01 09:00",
         "timestamp": "2026-08-01 09:01:00", "temperature_c": 5.0},
        {"wmo": "94839", "name": "Charlton", "obs_time": "2026-08-01 10:00",
         "timestamp": "2026-08-01 10:01:00", "temperature_c": 9.0},
    ], ignore_duplicates=True)
    df = weather_data.latest_aws_observations()
    assert len(df) == 1
    assert df.iloc[0]["temperature_c"] == 9.0


def test_rainfall_view_keeps_its_columns_and_wettest_first_order(db, monkeypatch):
    """Existing rainfall callers must see exactly what they always saw."""
    _stub_page(monkeypatch)
    aws.fetch_aws_observations()
    df = weather_data.latest_aws_rainfall()
    assert list(df.columns) == ["wmo", "name", "rain_since_9am_mm", "obs_time",
                                "latitude", "longitude"]
    rain = df["rain_since_9am_mm"].dropna().tolist()
    assert rain == sorted(rain, reverse=True)
    assert weather_data.aws_summary()[1] == "Sheoaks"  # wettest, 3.2 mm

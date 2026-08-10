"""`aws_weather_summary` — the operational headline values the Weather page and
(from Phase B) the briefing are built on.

The behaviour worth guarding: an extreme is only published if a station
actually reported it recently. A dead station must not become "the state's
warmest right now", and a field nobody reported must come back as None rather
than a misleading zero.
"""
import pandas as pd
import pytest

from app import database
from app.modules.weather import data as weather_data


def _obs(wmo, name, obs_time, **fields):
    row = {"wmo": wmo, "name": name, "obs_time": obs_time,
           "timestamp": obs_time + ":00"}
    row.update(fields)
    return row


def _store(rows):
    database.insert_rows("rainfall_aws", rows, ignore_duplicates=True)


def test_empty_network_returns_empty_summary(db):
    s = weather_data.aws_weather_summary()
    assert s["stations"] == 0
    for key in ("strongest_gust", "lowest_humidity", "warmest", "coldest",
                "max_daily_gust", "wettest"):
        assert s[key] is None, key
    assert s["top_gusts"] == [] and s["lowest_humidities"] == []


def test_extremes_are_reported_with_their_station(db):
    _store([
        _obs("1", "Mount William", "2026-08-10 23:00", wind_gust_kmh=82,
             wind_direction="NW", temperature_c=4.1, relative_humidity_pct=88,
             rain_since_9am_mm=2.0),
        _obs("2", "Mildura", "2026-08-10 23:00", wind_gust_kmh=20,
             temperature_c=38.2, relative_humidity_pct=18,
             rain_since_9am_mm=0.0),
        _obs("3", "Falls Creek", "2026-08-10 23:00", wind_gust_kmh=44,
             temperature_c=1.2, relative_humidity_pct=99,
             rain_since_9am_mm=42.6),
    ])
    s = weather_data.aws_weather_summary()
    assert (s["strongest_gust"]["value"], s["strongest_gust"]["station"]) == (82.0, "Mount William")
    assert s["strongest_gust"]["wind_direction"] == "NW"
    assert (s["lowest_humidity"]["value"], s["lowest_humidity"]["station"]) == (18.0, "Mildura")
    assert (s["warmest"]["value"], s["warmest"]["station"]) == (38.2, "Mildura")
    assert (s["coldest"]["value"], s["coldest"]["station"]) == (1.2, "Falls Creek")
    assert (s["wettest"]["value"], s["wettest"]["station"]) == (42.6, "Falls Creek")
    assert s["stations"] == 3 and s["reporting"] == 3 and s["stale"] == 0


def test_top_gusts_are_ranked_and_capped_at_three(db):
    _store([_obs(str(i), "Station %d" % i, "2026-08-10 23:00",
                 wind_gust_kmh=10 * i) for i in range(1, 6)])
    gusts = weather_data.aws_weather_summary()["top_gusts"]
    assert [g["value"] for g in gusts] == [50.0, 40.0, 30.0]


def test_stale_stations_are_excluded_from_current_conditions(db):
    """A station last heard from 18 hours ago must not be published as the
    state's current warmest — it isn't current."""
    _store([
        _obs("1", "Live Station", "2026-08-10 23:00", temperature_c=12.0,
             wind_gust_kmh=30),
        _obs("2", "Dead Station", "2026-08-10 05:00", temperature_c=41.0,
             wind_gust_kmh=99),
    ])
    s = weather_data.aws_weather_summary()
    assert s["stations"] == 2 and s["reporting"] == 1 and s["stale"] == 1
    assert s["warmest"]["station"] == "Live Station"
    assert s["strongest_gust"]["station"] == "Live Station"


def test_daily_max_gust_still_counts_a_stale_station(db):
    """The daily maximum is a since-midnight summary, not a current reading, so
    it is taken from every station's latest row."""
    _store([
        _obs("1", "Live Station", "2026-08-10 23:00", max_gust_kmh=40,
             max_gust_direction="S", max_gust_time="03:00pm"),
        _obs("2", "Dead Station", "2026-08-10 05:00", max_gust_kmh=106,
             max_gust_direction="N", max_gust_time="01:17am"),
    ])
    g = weather_data.aws_weather_summary()["max_daily_gust"]
    assert (g["value"], g["station"], g["max_gust_time"]) == (106.0, "Dead Station", "01:17am")


def test_a_field_nobody_reports_is_none(db):
    """Legacy rows carry rainfall only. Pressure was never observed, so it must
    read as unknown rather than as an extreme of zero."""
    _store([_obs("1", "Charlton", "2026-08-10 23:00", rain_since_9am_mm=4.0)])
    s = weather_data.aws_weather_summary()
    assert s["wettest"]["value"] == 4.0
    assert s["strongest_gust"] is None
    assert s["warmest"] is None
    assert s["lowest_humidity"] is None


def test_summary_ignores_superseded_readings(db):
    _store([
        _obs("1", "Mount William", "2026-08-10 20:00", wind_gust_kmh=95),
        _obs("1", "Mount William", "2026-08-10 23:00", wind_gust_kmh=31),
    ])
    s = weather_data.aws_weather_summary()
    assert s["stations"] == 1
    assert s["strongest_gust"]["value"] == 31.0


@pytest.mark.parametrize("max_age,expected_reporting", [(6, 1), (24, 2), (None, 2)])
def test_freshness_window_is_configurable(db, max_age, expected_reporting):
    _store([
        _obs("1", "Live Station", "2026-08-10 23:00", temperature_c=12.0),
        _obs("2", "Old Station", "2026-08-10 05:00", temperature_c=41.0),
    ])
    s = weather_data.aws_weather_summary(max_age_hours=max_age)
    assert s["reporting"] == expected_reporting


def test_observation_history_is_ordered_and_typed(db):
    _store([
        _obs("1", "Charlton", "2026-08-10 21:00", temperature_c=14.0),
        _obs("1", "Charlton", "2026-08-10 23:00", temperature_c=10.0),
        _obs("1", "Charlton", "2026-08-10 22:00", temperature_c=12.0),
    ])
    h = weather_data.aws_observation_history("1")
    assert h["temperature_c"].tolist() == [14.0, 12.0, 10.0]
    assert pd.api.types.is_datetime64_any_dtype(h["obs_time"])

"""AWS observation parser: field extraction and defensive behaviour.

The rule these tests exist to protect: a field BoM didn't publish must come out
as None, never as 0. A missing gust is not a calm night and a missing rain
total is not a dry day — storing either as zero would quietly corrupt every
summary, event total and briefing built on top of them.
"""
import pytest

from app.modules.weather import aws
from tests.conftest import load_fixture


@pytest.fixture(scope="module")
def stations():
    """Parsed fixture rows keyed by station name."""
    rows = aws._parse_vicall(load_fixture("vicall_sample.html"))
    return {r["name"]: r for r in rows}


def test_normal_station_extracts_every_field(stations):
    s = stations["Charlton"]
    assert s["wmo"] == "94839"
    assert s["temperature_c"] == 10.1
    assert s["apparent_temperature_c"] == 8.4
    assert s["dew_point_c"] == 9.8
    assert s["relative_humidity_pct"] == 98.0
    assert s["delta_t_c"] == 0.2
    assert s["wind_direction"] == "W"
    assert s["wind_speed_kmh"] == 9.0
    assert s["wind_gust_kmh"] == 15.0
    assert s["pressure_msl_hpa"] == 1010.1
    assert s["rain_since_9am_mm"] == 1.4
    assert s["max_gust_direction"] == "SW"
    assert s["max_gust_kmh"] == 46.0
    assert s["max_gust_time"] == "02:40pm"
    assert s["obs_time"].endswith(" 23:00")


def test_knots_columns_are_not_mistaken_for_kmh(stations):
    """Charlton's gust is 15 km/h / 8 kts. The kts cells share the `-wind-`
    group header, so a looser match would overwrite the km/h value with 8."""
    assert stations["Charlton"]["wind_gust_kmh"] == 15.0
    assert stations["Charlton"]["wind_speed_kmh"] == 9.0


def test_daily_extremes_do_not_leak_into_current_temperature(stations):
    """`-lowtmp`/`-hightmp` end in "tmp" but are daily extremes, not the
    current air temperature; the leading "-" in the suffix keeps them apart."""
    assert stations["Charlton"]["temperature_c"] == 10.1


def test_missing_pressure_is_none_not_zero(stations):
    s = stations["Nhill"]
    assert s["pressure_msl_hpa"] is None
    assert s["temperature_c"] == 7.6          # rest of the station survives
    assert s["max_gust_kmh"] == 52.0


def test_missing_gust_is_none_not_zero(stations):
    s = stations["Sheoaks"]
    assert s["wind_gust_kmh"] is None
    assert s["max_gust_kmh"] is None
    assert s["max_gust_direction"] is None
    assert s["max_gust_time"] is None
    assert s["wind_speed_kmh"] == 11.0        # wind itself still reported


def test_missing_temperature_is_none_not_zero(stations):
    s = stations["Coldstream"]
    for field in ("temperature_c", "apparent_temperature_c", "dew_point_c",
                  "relative_humidity_pct", "delta_t_c"):
        assert s[field] is None, field
    assert s["pressure_msl_hpa"] == 1011.0
    assert s["max_gust_time"] == "11:48am"


def test_malformed_values_drop_only_their_own_field(stations):
    s = stations["Garbled Creek"]
    assert s["temperature_c"] is None         # "abc"
    assert s["relative_humidity_pct"] is None  # "N/A"
    assert s["rain_since_9am_mm"] is None     # "1.2.3"
    assert s["max_gust_kmh"] is None          # "??:??"
    assert s["wind_gust_kmh"] == 29.0         # good cells still parsed
    assert s["pressure_msl_hpa"] == 1002.5


def test_calm_direction_is_preserved(stations):
    s = stations["Still Plains"]
    assert s["wind_direction"] == "CALM"
    assert s["wind_speed_kmh"] == 0.0         # a REPORTED zero stays zero


def test_station_without_observation_time_is_skipped(stations):
    """No obs time means no de-dup key, so the reading can't be stored safely."""
    assert "Silent Hill" not in stations


def test_non_station_rows_are_ignored(stations):
    assert set(stations) == {"Charlton", "Nhill", "Sheoaks", "Coldstream",
                             "Garbled Creek", "Still Plains"}


def test_one_bad_station_never_costs_the_rest(monkeypatch):
    """A station that blows up mid-parse is dropped on its own — the statewide
    fetch must still store every other observation."""
    real = aws._parse_station_row

    def explode(tr, now):
        row = real(tr, now)
        if row and row["name"] == "Nhill":
            raise ValueError("simulated BoM weirdness")
        return row

    monkeypatch.setattr(aws, "_parse_station_row", explode)
    names = {r["name"] for r in aws._parse_vicall(load_fixture("vicall_sample.html"))}
    assert "Nhill" not in names
    assert "Charlton" in names and "Coldstream" in names


def test_column_order_and_unknown_columns_do_not_change_the_result():
    """Reordered columns plus two columns BoM doesn't publish today must yield
    exactly the same values as the canonical layout."""
    normal = {r["name"]: r
              for r in aws._parse_vicall(load_fixture("vicall_sample.html"))}["Charlton"]
    reordered = aws._parse_vicall(load_fixture("vicall_reordered.html"))
    assert len(reordered) == 1
    assert reordered[0] == normal


def test_empty_page_parses_to_nothing():
    assert aws._parse_vicall(b"<html><body><p>Service unavailable</p></body></html>") == []


@pytest.mark.parametrize("cell,expected", [
    ("4602:40pm", (46.0, "02:40pm")),      # the ambiguous case: 46 not 460
    ("10601:17am", (106.0, "01:17am")),    # three-digit gust
    ("10.011:00pm", (10.0, "11:00pm")),    # decimal value
    ("-1.606:35pm", (-1.6, "06:35pm")),    # negative value
    ("46", (46.0, None)),                  # value published without a time
    ("-", (None, None)),                   # not reported
    ("", (None, None)),
    ("garbage", (None, None)),
])
def test_value_time_cells_split_correctly(cell, expected):
    assert aws._parse_value_time(cell) == expected


@pytest.mark.parametrize("text", ["-", "", "  ", "--", "N/A", "na"])
def test_missing_markers_become_none(text):
    assert aws._clean(text) is None

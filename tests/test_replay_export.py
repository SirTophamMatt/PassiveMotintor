"""The downloadable replay: one self-contained HTML file per event.

What matters: the file plays without a server (data + player + Plotly inline),
it carries what the event actually looked like — gauge classes as they changed,
graphs only for gauges that flooded, grouped by river — roads are limited to
weather-related disruptions that started during the event, and nothing in the
stored data can break out of the page's script.
"""
import json
import re
from datetime import datetime, timedelta

import pytest

from app import database, history, replay_export, tags

T0 = datetime(2026, 8, 11, 12, 0, 0)


def at(minutes):
    return T0 + timedelta(minutes=minutes)


def stamp(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S")


@pytest.fixture
def event(db):
    replay_export.clear_cache()
    tags.create_tag("Test Flood", stamp(T0), stamp(at(600)))
    return tags.list_tags()[0]["id"]


def _gauge(name, catchment, readings, levels=(1.0, 2.0, 3.0), coords=True):
    key = name.lower()
    database.insert_rows("flood_levels", [{
        "station_key": key, "station_name": name, "minor": levels[0],
        "moderate": levels[1], "major": levels[2]}])
    if coords:
        database.insert_rows("gauge_coords", [{
            "station_key": key, "station_name": name,
            "latitude": -37.0, "longitude": 145.0}])
    database.insert_rows("flood_observations", [
        {"event": "live", "station_name": name, "catchment": catchment,
         "height_m": h, "timestamp": stamp(at(m))} for m, h in readings])


def _road(key, minutes, dtype, closure=1, active=True, start=None, desc=None):
    history.record_state(
        history.ROADS, key,
        {"status": "Active", "disruption_type": dtype, "is_closure": closure,
         "road_name": "Main Rd", "location": "Town", "description": desc,
         "geometry": json.dumps({"type": "LineString",
                                 "coordinates": [[145.0, -37.0], [145.1, -37.1]]}),
         "start_time": stamp(start or at(minutes))},
        effective_ts=at(minutes), active=active, latitude=-37.0, longitude=145.0)


def _package(event):
    return replay_export.build_package(event)


# --------------------------------------------------------------------------- #
def test_unknown_event_exports_nothing(db):
    assert replay_export.build_package(999) is None
    assert replay_export.export(999) is None


def test_meta_describes_the_event(event):
    meta = _package(event)["meta"]
    assert meta["name"] == "Test Flood"
    assert meta["minutes"] == 600
    assert meta["start"] == "2026-08-11T12:00:00"
    assert "Flooding" in meta["road_causes"]


def test_gauge_classes_change_as_they_did(event):
    _gauge("Big River at Town", "Big River",
           [(-60, 0.5), (60, 0.8), (120, 1.5), (180, 2.5), (300, 1.2)])
    g = _package(event)["gauges"][0]
    # Carried in at t=-1 (Below), then Minor, Moderate, back to Minor.
    assert g["c"] == [[-1, 0], [120, 1], [180, 2], [300, 1]]
    assert g["peak"] == 2
    assert g["river"] == "Big River"
    assert g["peak_h"] == 2.5 and g["peak_t"] == 180


def test_only_flooded_gauges_carry_graph_readings(event):
    _gauge("Flooded", "A", [(10, 1.5)])
    _gauge("Quiet", "A", [(10, 0.2)])
    gauges = {g["name"]: g for g in _package(event)["gauges"]}
    assert "r" in gauges["Flooded"]
    assert "r" not in gauges["Quiet"]       # map class only, no graph payload


def test_graph_includes_the_lead_in_before_the_event(event):
    _gauge("Rising", "A", [(-300, 0.4), (60, 1.6)])
    g = _package(event)["gauges"][0]
    assert g["r"][0] == [-300, 0.4]


def test_flooding_only_before_the_event_does_not_count(event):
    _gauge("Earlier", "A", [(-120, 2.5), (30, 0.3)])
    g = _package(event)["gauges"][0]
    assert g["peak"] == 0 and "r" not in g


def test_a_silent_gauge_goes_to_no_data(db):
    replay_export.clear_cache()
    tags.create_tag("Long", stamp(T0), stamp(at(3 * 1440)))
    _gauge("Silent", "A", [(10, 1.5)])
    g = replay_export.build_package(tags.list_tags()[0]["id"])["gauges"][0]
    stale = replay_export.STALE_MINUTES
    assert [10 + stale, replay_export.NO_DATA] in g["c"]


def test_a_gauge_without_coords_still_gets_its_graph(event):
    _gauge("Unmapped", "A", [(10, 1.5)], coords=False)
    g = _package(event)["gauges"][0]
    assert g["lat"] is None and "r" in g


def test_weather_roads_kept_others_dropped(event):
    _road("flood1", 30, "Flooding, Water over road")
    _road("storm1", 40, "Hazard, Tree down")
    _road("crash1", 50, "Collision, Vehicle")
    keys = {r["k"] for r in _package(event)["roads"]}
    assert keys == {"flood1", "storm1"}


def test_road_cause_from_due_to_phrase_but_not_road_names(event):
    _road("due", 30, "Closure", desc="Closed due to flooding")
    _road("name", 30, "Roadworks", desc="Works on Cabbage Tree Road")
    keys = {r["k"] for r in _package(event)["roads"]}
    assert keys == {"due"}


def test_roads_that_started_before_the_event_are_dropped(event):
    _road("old", 30, "Flooding", start=T0 - timedelta(days=20))
    assert _package(event)["roads"] == []


def test_road_reopening_is_a_tombstone(event):
    _road("r1", 30, "Flooding")
    _road("r1", 200, "Flooding", active=False)
    states = _package(event)["roads"][0]["s"]
    assert [s[0] for s in states] == [30, 200]
    assert [s[1] for s in states] == [1, 0]
    assert states[0][5] is not None            # the line geometry is kept


def test_fire_entities_carry_kind_and_dedupe_geometry(event):
    area = {"type": "Polygon", "coordinates": [[[145, -37], [145.1, -37],
                                                [145.1, -37.1], [145, -37]]]}
    for minutes, level in ((-30, "Advice"), (100, "Watch and Act")):
        history.record_state(
            history.FIRE, "w1",
            {"feed_type": "warning", "warning_level": level, "location": "Town",
             "category1": "Flood", "geometry": json.dumps(area)},
            effective_ts=at(minutes), latitude=-37.0, longitude=145.0)
    pkg = _package(event)
    states = pkg["fire"][0]["s"]
    assert [s[0] for s in states] == [-1, 100]
    assert [s[2] for s in states] == ["Advice", "Watch and Act"]
    assert states[0][5] == states[1][5]        # same polygon stored once
    assert len(pkg["geoms"]) == 1


def test_burn_areas_are_left_out(event):
    history.record_state(history.FIRE, "b1", {"feed_type": "burn-area"},
                         effective_ts=at(10))
    assert _package(event)["fire"] == []


def test_entities_resolved_before_the_event_are_absent(event):
    history.record_state(history.FIRE, "gone", {"feed_type": "incident",
                                                "category1": "Fire"},
                         effective_ts=at(-200))
    history.record_state(history.FIRE, "gone", {"feed_type": "incident",
                                                "category1": "Fire"},
                         effective_ts=at(-100), active=False)
    assert _package(event)["fire"] == []


def test_power_series_and_bom_warnings(event):
    database.insert_rows("power_timeseries", [
        {"timestamp": stamp(at(5)), "customers_off": 1200},
        {"timestamp": stamp(at(65)), "customers_off": 5400}])
    history.record_state(history.WEATHER_WARNING, "IDV1",
                         {"title": "Flood Warning for Big River"},
                         effective_ts=at(20))
    pkg = _package(event)
    assert pkg["power"] == [[5, 1200], [65, 5400]]
    assert pkg["bom"][0]["title"] == "Flood Warning for Big River"
    assert pkg["bom"][0]["s"] == [[20, 1]]


def test_one_broken_source_does_not_break_the_export(event, monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("source down")
    monkeypatch.setattr(replay_export, "road_entities", boom)
    _gauge("Still here", "A", [(10, 1.5)])
    pkg = _package(event)
    assert pkg["roads"] == [] and len(pkg["gauges"]) == 1


# --------------------------------------------------------------------------- #
# The file itself
# --------------------------------------------------------------------------- #
def test_html_is_self_contained(event):
    _gauge("Big River at Town", "Big River", [(10, 1.5)])
    name, page = replay_export.export(event)
    assert name == "replay_Test_Flood_2026-08-11.html"
    assert "{{" not in page.split('id="replay-data"')[0][-2000:]
    # Plotly is inline, not fetched — no external script tags at all.
    assert not re.search(r"<script[^>]+src=", page)
    assert "scattermap" in page
    data = page.split('id="replay-data">', 1)[1].split("</script>", 1)[0]
    assert json.loads(data)["gauges"][0]["name"] == "Big River at Town"


def test_stored_text_cannot_close_the_script(event):
    history.record_state(
        history.WEATHER_WARNING, "x",
        {"title": "</script><script>alert(1)</script>"}, effective_ts=at(5))
    _name, page = replay_export.export(event)
    data = page.split('id="replay-data">', 1)[1].split("</script>", 1)[0]
    assert "alert(1)" in json.loads(data)["bom"][0]["title"]
    assert page.count("<script>alert(1)") == 0


def test_export_is_cached(event, monkeypatch):
    first = replay_export.export(event)
    monkeypatch.setattr(replay_export, "build_package",
                        lambda _t: pytest.fail("rebuilt instead of cached"))
    assert replay_export.export(event) is first


def test_route_serves_and_downloads(event):
    from app.factory import create_app
    client = create_app(autostart=False).server.test_client()
    r = client.get("/replay/export/%d" % event)
    assert r.status_code == 200 and r.mimetype == "text/html"
    assert "Content-Disposition" not in r.headers
    r = client.get("/replay/export/%d?download=1" % event)
    assert "attachment" in r.headers["Content-Disposition"]
    assert client.get("/replay/export/999999").status_code == 404


def test_player_runs_maplibre_from_disk():
    """Opened from file://, Chrome refuses MapLibre's module worker and the map
    never loads — the template carries the shim that restarts it as a classic
    worker (verified in Chromium; this guards against it being dropped)."""
    page = replay_export.TEMPLATE.read_text(encoding="utf-8")
    shim = page.index('location.protocol !== "file:"')
    assert shim < page.index("{{PLOTLY_JS}}")
    assert "window.Worker = Shim" in page

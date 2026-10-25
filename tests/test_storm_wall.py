"""Storm wall (/wall/storm): weather-related incidents linked into areas of
operation, what attaches to an area (and what never links two), event scoping,
takeovers, the simulated test run and the wiring that makes it a wall."""
import json
from datetime import datetime, timedelta

import pandas as pd
import pytest

from app import auth, database, shell, storm_areas as sa, storm_demo
from app.pages import storm_wall as stw

NOW = datetime(2026, 10, 11, 14, 0, 0)


def _stamp(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def box(lon, lat, d):
    return {"type": "Polygon", "coordinates": [[[lon, lat], [lon + d, lat],
                                                [lon + d, lat + d], [lon, lat + d],
                                                [lon, lat]]]}


def put(sid, lat=-37.9, lon=145.35, category="Tree Down", org="VIC/SES",
        feed_type="incident", level=None, event=None, geom=None, seen=NOW,
        location=None, category2=None):
    database.execute("DELETE FROM fire_incidents WHERE source_id = ?", [sid])
    database.insert_rows("fire_incidents", [{
        "source_id": sid, "feed_type": feed_type, "category1": category,
        "category2": category2, "warning_level": level, "event": event,
        "location": location or sid.title(), "source_org": org,
        "latitude": lat, "longitude": lon, "status": "Responding",
        "geometry": json.dumps(geom) if geom else None,
        "created": _stamp(seen), "updated": _stamp(seen),
        "first_seen": _stamp(seen), "last_seen": _stamp(seen), "resolved": 0}])


def road(sid, lat, lon, kind="Hazard, Fallen trees", closure=1, seen=NOW):
    database.insert_rows("road_disruptions", [{
        "source_id": sid, "disruption_type": kind, "is_closure": closure,
        "road_name": f"{sid} Rd", "latitude": lat, "longitude": lon,
        "created": _stamp(seen), "first_seen": _stamp(seen),
        "last_seen": _stamp(seen), "resolved": 0}])


def warning(sid, level, event="Severe Thunderstorm", geom=None, lat=-37.9,
            lon=145.35):
    put(sid, lat=lat, lon=lon, category=level, feed_type="warning", level=level,
        event=event, geom=geom)


def areas(**kw):
    snap = sa.compute(NOW, **kw)
    assert snap["ok"], snap["error"]
    return snap


def ids(area, role="incident"):
    return sorted(m.id for m in area.of_role(role))


@pytest.fixture(autouse=True)
def fresh(db):
    sa.reset_cache()
    yield
    sa.reset_cache()


# --------------------------------------------------------------------------- #
# Wiring
# --------------------------------------------------------------------------- #
def test_storm_wall_is_a_public_wall_in_the_switcher_and_menu():
    from app.factory import PUBLIC_PAGES, RESTRICTED
    from app.pages import wall
    assert shell.STORM_WALL_PATH == "/wall/storm"
    assert shell.STORM_WALL_PATH in [p for p, _, _ in PUBLIC_PAGES]
    assert shell.STORM_WALL_PATH not in RESTRICTED
    assert shell.STORM_WALL_PATH in [p for p, _ in wall.SCENARIOS]
    assert shell.STORM_WALL_PATH in dict(shell.NAV_GROUPS)["Situation"]
    assert "wall-mode" in shell.root_class(True, "console", None,
                                           shell.STORM_WALL_PATH)


def test_layout_builds_and_test_page_is_locked_for_anonymous():
    from dash import Dash
    Dash(__name__)
    assert stw.layout() is not None
    assert shell.STORM_WALL_TEST_PATH == "/wall/storm/test"
    assert "Admin" in str(stw.test_locked())
    assert "TEST RUN" in str(stw.layout(test=True))


def test_callbacks_register_in_the_real_app():
    from app.factory import create_app
    app = create_app(autostart=False)
    outputs = " ".join(app.callback_map)
    for out in ("stw-tiles.children", "stw-list.children", "stw-map.figure",
                "stw-pin.data", "stw-event.options"):
        assert out in outputs


# --------------------------------------------------------------------------- #
# What counts as weather
# --------------------------------------------------------------------------- #
def test_weather_incidents_and_warnings_are_recognised():
    assert sa.is_weather_incident({"category1": "Tree Down"})
    assert sa.is_weather_incident({"category1": "Building Damage"})
    assert sa.is_weather_incident({"category1": "Other", "category2": "Flooding"})
    assert not sa.is_weather_incident({"category1": "Rescue"})
    assert not sa.is_weather_incident({"category1": "Fire", "category2": "Grass Fire"})
    assert not sa.is_weather_incident({"category1": "Tree Down", "feed_type": "warning"})
    assert sa.is_weather_warning({"feed_type": "warning", "event": "Riverine Flood"})
    assert sa.is_weather_warning({"feed_type": "warning",
                                  "event": "Severe Thunderstorm"})
    assert not sa.is_weather_warning({"feed_type": "warning", "event": "Bushfire"})
    # A fire warning stays a fire warning whatever its headline says.
    assert not sa.is_weather_warning({"feed_type": "warning", "event": "Grass Fire",
                                      "headline": "Fire near Storm Creek Rd"})


# --------------------------------------------------------------------------- #
# Linking incidents into areas
# --------------------------------------------------------------------------- #
def test_nearby_ses_jobs_link_and_chain_and_distant_ones_do_not():
    put("a1", lat=-37.90, lon=145.30)
    put("a2", lat=-37.90, lon=145.34)          # ~3.5 km from a1
    put("a3", lat=-37.90, lon=145.38)          # ~3.5 km from a2, ~7 km from a1
    put("far", lat=-38.15, lon=144.35)
    result = areas(link_km=5)["areas"]
    assert sorted(ids(a) for a in result) == [["a1", "a2", "a3"], ["far"]]


def test_link_distance_is_the_option():
    put("a1", lat=-37.90, lon=145.30)
    put("a2", lat=-37.90, lon=145.39)          # ~8 km
    assert len(areas(link_km=5)["areas"]) == 2
    assert len(areas(link_km=10)["areas"]) == 1


def test_only_ses_weather_jobs_by_default_and_never_fires():
    put("ses", category="Tree Down")
    put("cfa-tree", category="Tree Down", org="VIC/CFA", lat=-37.91)
    put("rescue", category="Rescue", lat=-37.92)
    put("fire", category="Fire", org="VIC/SES", lat=-37.905)
    assert [ids(a) for a in areas()["areas"]] == [["ses"]]
    both = areas(agencies=["ses", "cfa"], weather_only=False)["areas"]
    assert [ids(a) for a in both] == [["cfa-tree", "rescue", "ses"]]


def test_area_is_named_after_its_busiest_locality_and_counts_kinds():
    put("j1", location="12 Main St, BELGRAVE", lat=-37.90)
    put("j2", location="BELGRAVE", category="Building Damage", lat=-37.905)
    put("j3", location="Upwey", lat=-37.91)
    area = areas()["areas"][0]
    assert area.name == "Belgrave"
    assert area.places == ["Belgrave", "Upwey"]
    assert area.kind_counts() == [("Tree Down", 2), ("Building Damage", 1)]


def test_new_count_uses_the_last_hour():
    put("old", seen=NOW - timedelta(hours=3))
    put("new", seen=NOW - timedelta(minutes=10), lat=-37.905)
    area = areas()["areas"][0]
    assert area.new_count == 1 and area.newest_at == NOW - timedelta(minutes=10)


# --------------------------------------------------------------------------- #
# Warnings and context attach, never link
# --------------------------------------------------------------------------- #
def test_a_warning_over_two_clusters_joins_both_without_merging_them():
    put("west", lat=-37.90, lon=145.20)
    put("east", lat=-37.90, lon=145.50)        # ~26 km apart
    warning("w", "Watch and Act", geom=box(145.15, -37.95, 0.4))
    result = areas()["areas"]
    assert sorted(ids(a) for a in result) == [["east"], ["west"]]
    assert all(ids(a, "warning") == ["w"] and a.level == "Watch and Act"
               for a in result)
    # One warning, one takeover — not one per area it covers.
    assert [e["member"] for e in sa.events(areas()) if e["kind"] == "warning"] == ["w"]


def test_warnings_alone_an_act_level_gets_an_area_an_advice_does_not():
    warning("advice", "Advice", event="Flash Flood", lat=-38.2, lon=146.5)
    warning("watch", "Watch and Act", event="Riverine Flood", lat=-36.4, lon=145.4)
    warning("fire", "Emergency Warning", event="Bushfire", lat=-37.0, lon=144.0)
    result = areas()["areas"]
    assert [a.key for a in result] == ["watch"]


def test_context_attaches_to_the_nearest_area_and_never_links_two():
    put("a1", lat=-37.90, lon=145.30)
    put("a2", lat=-37.90, lon=145.42)          # ~10.5 km: two areas at 5 km
    road("between", -37.90, 145.36)            # ~5.3 km from each: attaches to none
    road("trees", -37.90, 145.31)              # beside a1
    road("crash", -37.90, 145.305, kind="Accident, Crash")   # not weather: dropped
    database.insert_rows("geocode_cache", [{"location": "BELGRAVE",
                                            "latitude": -37.90, "longitude": 145.425}])
    database.insert_rows("power_outages", [{
        "location": "BELGRAVE", "customers_off": 900, "type": "Unplanned",
        "first_seen": _stamp(NOW), "last_seen": _stamp(NOW), "restored": 0}])
    result = areas(link_km=5)["areas"]
    assert len(result) == 2
    a1 = sa.area_for_member(result, "a1")
    a2 = sa.area_for_member(result, "a2")
    assert [m.id for m in a1.of_role("road")] == ["road:trees"]
    assert a2.customers_off == 900 and not a1.customers_off
    assert "road:between" not in {m.id for a in result for m in a.members}


def _outage(location, customers, first_seen, lat=-37.90, lon=145.425):
    database.insert_rows("geocode_cache", [{"location": location,
                                            "latitude": lat, "longitude": lon}])
    database.insert_rows("power_outages", [{
        "location": location, "customers_off": customers, "type": "Unplanned",
        "first_seen": first_seen, "last_seen": _stamp(NOW), "restored": 0}])


def test_one_outage_over_several_localities_counts_once():
    # EM-COP lists one 1,000-customer outage with three suburbs; the scraper
    # stores a row per suburb, each carrying the whole 1,000.
    put("a1", lat=-37.90, lon=145.42)
    start = _stamp(NOW - timedelta(hours=1))
    for i, town in enumerate(("BELGRAVE", "TECOMA", "UPWEY")):
        _outage(town, 1000, start, lon=145.42 + i * 0.003)
    _outage("MONBULK", 250, _stamp(NOW - timedelta(minutes=30)), lon=145.43)
    snap = areas(link_km=5)
    assert sa.area_for_member(snap["areas"], "a1").customers_off == 1250
    assert sa.outage_customers(snap["frames"]["power"]) == 1250
    snap["frames"]["power_total"] = None            # no statewide figure yet
    assert stw.counts(snap)["customers"] == 1250


def test_customers_tile_uses_the_em_cop_statewide_total():
    start = _stamp(NOW - timedelta(hours=1))
    for town in ("BELGRAVE", "TECOMA", "UPWEY"):
        _outage(town, 1000, start)
    database.insert_rows("power_timeseries", [{
        "timestamp": _stamp(NOW), "customers_off": 3120,
        "power_dependant_off": 0, "planned": 0, "unplanned": 1}])
    assert stw.counts(areas(link_km=5))["customers"] == 3120


def test_gauges_and_storm_cells_attach_from_frames():
    frames = {
        "incidents": pd.DataFrame([{"source_id": "a1", "feed_type": "incident",
                                    "category1": "Flooding", "source_org": "VIC/SES",
                                    "latitude": -38.17, "longitude": 144.34,
                                    "location": "Belmont", "created": _stamp(NOW)}]),
        "gauges": pd.DataFrame([
            {"station_name": "Barwon R", "height_m": 2.3, "priority": 3,
             "latitude": -38.165, "longitude": 144.33},
            {"station_name": "Dry Ck", "height_m": 0.4, "priority": 4,
             "latitude": -38.168, "longitude": 144.335}]),
        "cells": pd.DataFrame([
            {"cell_id": "S", "radar_id": "IDR023", "classification": "strong",
             "latitude": -38.18, "longitude": 144.35},
            {"cell_id": "W", "radar_id": "IDR023", "classification": "weak",
             "latitude": -38.18, "longitude": 144.35}]),
    }
    snap = sa.new_snapshot(NOW, 5, ["ses"], True, None)
    area = sa.assemble(snap, frames)["areas"][0]
    assert area.worst_gauge.kind == "Minor"
    assert [m.id for m in area.of_role("gauge")] == ["gauge:Barwon R"]
    assert [m.id for m in area.strong_cells] == ["cell:IDR023:S"]
    assert len(area.of_role("cell")) == 1          # weak cells are not context


def test_an_event_tag_scopes_incidents_and_roads_but_not_warnings():
    put("before", seen=NOW - timedelta(days=2))
    put("during", seen=NOW - timedelta(hours=1), lat=-37.95, lon=145.6)
    road("old-road", -37.90, 145.351, seen=NOW - timedelta(days=2))
    warning("w", "Watch and Act", geom=box(145.3, -37.95, 0.1))
    tag = {"id": 1, "name": "Storm", "start_ts": _stamp(NOW - timedelta(hours=6)),
           "end_ts": None}
    result = areas(tag=tag)["areas"]
    assert sorted(ids(a) for a in result if a.of_role("incident")) == [["during"]]
    # The warning over the (out-of-event) first job is still current: it stays,
    # as an area of its own because no counted job is near it.
    assert "w" in {m.id for a in result for m in a.members}
    assert not any(a.of_role("road") for a in result)


# --------------------------------------------------------------------------- #
# Takeovers
# --------------------------------------------------------------------------- #
def test_events_key_warnings_by_level_and_areas_by_size_step():
    for i in range(5):
        put(f"j{i}", lat=-37.90 - i * 0.005, seen=NOW - timedelta(minutes=i))
    warning("w", "Watch and Act", geom=box(145.3, -37.95, 0.1))
    keys = {e["key"] for e in sa.events(areas())}
    assert keys == {"warn:w:Watch and Act", "size:j4:5"}
    warning("w", "Emergency Warning", geom=box(145.3, -37.95, 0.1))
    sa.reset_cache()
    seen, fresh = sa.detect_new(list(keys), sa.events(areas()))
    assert [e["key"] for e in fresh] == ["warn:w:Emergency Warning"]
    assert sa.size_step(4) == 0 and sa.size_step(12) == 10


def test_hot_flash_prefers_emergency_and_expires():
    put("a1")
    warning("w", "Emergency Warning", geom=box(145.3, -37.95, 0.1))
    result = areas()["areas"]
    flashes = [{"key": "size", "member": "a1", "kind": "size", "count": 5, "at": 100},
               {"key": "w", "member": "w", "kind": "warning",
                "level": "Emergency Warning", "at": 90}]
    flash, area = stw.hot_flash(flashes, result, 110)
    assert flash["key"] == "w" and area.key == "a1"
    assert stw.hot_flash(flashes, result, 100 + stw.TAKEOVER_SECONDS)[0] is None
    text, cls = stw.banner(flashes[0], area)
    assert "AREA GROWING" in text and "5 incidents" in text


# --------------------------------------------------------------------------- #
# Tiles, cards, map
# --------------------------------------------------------------------------- #
def test_tiles_keep_warning_levels_separate_and_say_dash_when_unreadable():
    put("a1")
    warning("w1", "Watch and Act", geom=box(145.3, -37.95, 0.1))
    warning("w2", "Advice", event="Flood")
    warning("fw", "Emergency Warning", event="Bushfire")
    c = stw.counts(areas())
    assert (c["Emergency Warning"], c["Watch and Act"], c["Advice"]) == (0, 1, 1)
    assert c["incidents"] == 1 and c["areas"] == 1
    broken = sa.new_snapshot(NOW, 5, ["ses"], True, None)
    broken.update(ok=False, error="boom")
    values = [t.children[1].children for t in stw.tiles(broken)]
    assert set(values) == {"—"}


def test_focus_card_list_and_map_render():
    put("a1", location="BELGRAVE")
    put("a2", location="UPWEY", lat=-37.905, category="Building Damage")
    warning("w", "Watch and Act", geom=box(145.3, -37.95, 0.1))
    road("trees", -37.90, 145.351)
    snap = areas()
    area = snap["areas"][0]
    card = str(stw.focus_card(area, snap, "rotate"))
    assert "Belgrave" in card and "2 incidents" in card and "1 road closure" in card
    assert "Upwey" in str(stw.area_list(snap, []))
    assert "No weather-related incidents" in str(
        stw.focus_card(None, sa.new_snapshot(NOW, 5, ["ses"], True, None), "rotate"))
    fig = stw.map_figure(snap, area, stw.DEFAULT_LAYERS, True)
    assert fig.layout.meta["wdFocus"] == area.key
    names = {t.name for t in fig.data}
    assert any("Road" in (n or "") for n in names)
    quiet = stw.map_figure(snap, None, ["incidents"], False)
    assert not any("Road" in (t.name or "") for t in quiet.data)
    assert not list(quiet.layout.map.layers or [])      # no warning areas, no outlines


# --------------------------------------------------------------------------- #
# Test run
# --------------------------------------------------------------------------- #
def _test_snap(step, **kw):
    start, _, _ = storm_demo.clock(1_800_000_000.0)
    return storm_demo.snapshot(now_ts=start + step * storm_demo.STEP_SECONDS + 5, **kw)


def test_test_run_links_grows_and_escalates():
    levels, sizes = [], []
    for step in range(storm_demo.STEPS):
        snap = _test_snap(step)
        assert snap["ok"] and snap["test"]["step"] == step + 1
        ranges = sa.area_for_member(snap["areas"], "TEST-ranges-01")
        levels.append(ranges.level)
        sizes.append(len(ranges.of_role("incident")))
        assert not any("TEST-geelong-1" in a.member_ids() and a is ranges
                       for a in snap["areas"])
    assert levels == ["Advice", "Advice", "Watch and Act", "Watch and Act",
                      "Emergency Warning", "Emergency Warning"]
    assert sizes == [2, 4, 6, 8, 10, 12]
    last = _test_snap(5)
    keys = {a.key for a in last["areas"]}
    assert keys == {"TEST-ranges-01", "TEST-geelong-1", "TEST-shepparton-flood"}
    member_ids = {m.id for a in last["areas"] for m in a.members}
    assert not member_ids & {"TEST-gippsland-advice", "TEST-bendigo-rescue",
                             "TEST-grass-fire"}
    ranges = sa.area_for_member(last["areas"], "TEST-ranges-01")
    assert ranges.closures and ranges.customers_off and ranges.strong_cells
    assert "TEST-bendigo-rescue" in {
        m.id for a in _test_snap(5, weather_only=False)["areas"] for m in a.members}


def test_test_run_produces_takeovers():
    seen, _ = sa.detect_new(None, sa.events(_test_snap(1)))
    _, fresh = sa.detect_new(seen, sa.events(_test_snap(2)))
    assert {e["key"] for e in fresh} == {"warn:TEST-storm-warning:Watch and Act",
                                         "size:TEST-ranges-01:5"}


def test_test_run_writes_nothing():
    _test_snap(3)
    for table in ("fire_incidents", "road_disruptions", "power_outages",
                  "entity_state_history", "intel_events"):
        assert database.read_df(f"SELECT COUNT(*) AS n FROM {table}").iloc[0]["n"] == 0


def test_test_run_is_admin_only(monkeypatch):
    monkeypatch.setattr(auth, "is_admin", lambda: False)
    assert "test" not in stw.snapshot(5, ["ses"], ["weather"], None, test=True)
    monkeypatch.setattr(auth, "is_admin", lambda: True)
    assert stw.snapshot(5, ["ses"], ["weather"], None,
                        test=True)["test"]["steps"] == storm_demo.STEPS


def _post(client, test):
    outs = [{"id": i, "property": p} for i, p in (
        ("stw-tiles", "children"), ("stw-stale", "children"),
        ("stw-stale", "title"), ("stw-note", "children"),
        ("stw-seen", "data"), ("stw-flash", "data"))]
    return client.post("/_dash-update-component", json={
        "output": "..%s.." % "...".join(f"{o['id']}.{o['property']}" for o in outs),
        "outputs": outs,
        "inputs": [{"id": "stw-interval", "property": "n_intervals", "value": 1},
                   {"id": "stw-link-km", "property": "value", "value": 5},
                   {"id": "stw-agencies", "property": "value", "value": ["ses"]},
                   {"id": "stw-weather", "property": "value", "value": ["weather"]},
                   {"id": "stw-event", "property": "value", "value": None}],
        "state": [{"id": "stw-seen", "property": "data", "value": []},
                  {"id": "stw-flash", "property": "data", "value": []},
                  {"id": "stw-test", "property": "data", "value": test}],
        "changedPropIds": ["stw-interval.n_intervals"]})


def test_refresh_callback_runs_the_test_only_for_an_admin(monkeypatch):
    from app.factory import create_app
    client = create_app(autostart=False).server.test_client()
    r = _post(client, True)
    assert r.status_code == 200 and "TEST RUN" not in r.get_data(as_text=True)
    monkeypatch.setattr(auth, "is_admin", lambda: True)
    r = _post(client, True)
    assert r.status_code == 200, r.get_data(as_text=True)[:500]
    text = r.get_data(as_text=True)
    assert "TEST RUN" in text and "SES incidents" in text

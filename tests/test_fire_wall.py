"""Fire wall (/wall/fire): burn-area history and change, linking incidents and
warnings into areas of operation, focus/takeover rules, the map layers and the
wiring that makes it a wall."""
import json
import os
from datetime import datetime, timedelta

import pytest

from app import database, fire_areas as fa, shell
from app.pages import fire_wall as fiw

NOW = datetime(2026, 10, 8, 14, 0, 0)


def _stamp(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def square(lon, lat, d, point=True):
    """A d-degree square with its SW corner at lon/lat, wrapped like the feed
    wraps an incident's area (GeometryCollection of Point + Polygon)."""
    poly = {"type": "Polygon", "coordinates": [[[lon, lat], [lon + d, lat],
                                                [lon + d, lat + d], [lon, lat + d],
                                                [lon, lat]]]}
    if not point:
        return poly
    return {"type": "GeometryCollection",
            "geometries": [{"type": "Point", "coordinates": [lon, lat]}, poly]}


def put(sid, geom=None, seen=NOW, category="Fire", feed_type="incident",
        level=None, lat=-37.0, lon=145.0, event=None, created=None,
        location=None):
    database.execute("DELETE FROM fire_incidents WHERE source_id = ?", [sid])
    database.insert_rows("fire_incidents", [{
        "source_id": sid, "feed_type": feed_type, "category1": category,
        "warning_level": level, "event": event, "location": location or sid.title(),
        "latitude": lat, "longitude": lon, "status": "Going",
        "geometry": json.dumps(geom) if geom else None,
        "created": _stamp(created) if created else None,
        "first_seen": _stamp(seen), "last_seen": _stamp(seen), "resolved": 0}])


def history(sid, geom, at):
    mp = fa.polygons_only(geom)
    database.insert_rows("fire_area_history", [{
        "source_id": sid, "recorded_at": _stamp(at), "geometry": json.dumps(mp),
        "geom_hash": fa.geom_hash(mp), "area_ha": fa.area_ha(mp)}])


@pytest.fixture(autouse=True)
def fresh(db):
    fa.reset_cache()
    yield
    fa.reset_cache()


# --------------------------------------------------------------------------- #
# Wiring
# --------------------------------------------------------------------------- #
def test_fire_wall_is_a_public_wall_in_the_switcher_and_menu():
    from app.factory import PUBLIC_PAGES, RESTRICTED
    from app.pages import wall
    assert shell.FIRE_WALL_PATH == "/wall/fire"
    assert shell.FIRE_WALL_PATH in [p for p, _, _ in PUBLIC_PAGES]
    assert shell.FIRE_WALL_PATH not in RESTRICTED
    assert shell.FIRE_WALL_PATH in [p for p, _ in wall.SCENARIOS]
    assert shell.FIRE_WALL_PATH in dict(shell.NAV_GROUPS)["Situation"]
    assert "wall-mode" in shell.root_class(True, "console", None, shell.FIRE_WALL_PATH)


def test_layout_builds():
    from dash import Dash
    Dash(__name__)
    assert fiw.layout() is not None


def test_callbacks_register_in_the_real_app():
    from app.factory import create_app
    app = create_app()
    outputs = " ".join(app.callback_map)
    assert "fiw-map.figure" in outputs and "fiw-pin.data" in outputs


def test_the_area_list_does_not_depend_on_the_pin():
    """list -> pin -> render -> list was a loop Dash resolved by holding the
    update until the next interval tick (clicks took 5-10 s to land)."""
    from app.factory import create_app
    app = create_app()
    for out, cb in app.callback_map.items():
        if "fiw-list.children" in out:
            assert all(i["id"] not in ("fiw-pin", "fiw-focus-key")
                       for i in cb["inputs"])


def test_graph_calm_moves_the_map_when_the_focus_changes():
    js = open(os.path.join(os.path.dirname(__file__), "..", "assets",
                           "graph_calm.js"), encoding="utf-8").read()
    assert "wdFocus" in js and "__wdFocus" in js


# --------------------------------------------------------------------------- #
# Geometry helpers
# --------------------------------------------------------------------------- #
def test_polygons_only_drops_the_point_and_handles_bad_input():
    mp = fa.polygons_only(json.dumps(square(145, -37, 0.1)))
    assert mp["type"] == "MultiPolygon" and len(mp["coordinates"]) == 1
    assert fa.polygons_only({"type": "Point", "coordinates": [145, -37]}) is None
    assert fa.polygons_only("not json") is None
    assert fa.polygons_only(None) is None


def test_area_is_in_hectares():
    # 0.01 deg square at 37 S: ~1,112 m x ~889 m = ~99 ha.
    ha = fa.area_ha(fa.polygons_only(square(145, -37, 0.01)))
    assert 90 < ha < 105


# --------------------------------------------------------------------------- #
# Recording (collector side)
# --------------------------------------------------------------------------- #
def test_record_is_change_only_and_ignores_warnings_and_points():
    put("f1", square(145, -37, 0.05))
    put("w1", square(145, -37, 0.2), category="Advice", feed_type="warning",
        level="Advice")
    put("pt", None)
    put("tree", square(146, -37, 0.05), category="Tree Down")
    assert fa.record(NOW) == 1
    assert fa.record(NOW) == 0                      # same cycle again
    later = NOW + timedelta(minutes=3)
    put("f1", square(145, -37, 0.05), seen=later)   # unchanged shape
    assert fa.record(later) == 0
    later2 = later + timedelta(minutes=3)
    put("f1", square(145, -37, 0.08), seen=later2)  # grew
    assert fa.record(later2) == 1
    rows = database.read_df("SELECT source_id FROM fire_area_history")
    assert rows["source_id"].tolist() == ["f1", "f1"]


def test_a_moved_centre_point_is_not_a_shape_change():
    geom = square(145, -37, 0.05)
    put("f1", geom)
    fa.record(NOW)
    moved = json.loads(json.dumps(geom))
    moved["geometries"][0]["coordinates"] = [145.02, -36.98]
    later = NOW + timedelta(minutes=3)
    put("f1", moved, seen=later)
    assert fa.record(later) == 0


def test_burn_area_features_are_recorded():
    put("b1", square(145, -37, 0.05, point=False), category=None,
        feed_type="burn-area")
    assert fa.record(NOW) == 1


def test_scraper_records_areas_without_breaking(monkeypatch):
    from app.modules.fire import scraper
    feature = {"type": "Feature", "geometry": square(145, -37, 0.05),
               "properties": {"id": "f9", "feedType": "incident",
                              "category1": "Fire", "status": "Going",
                              "location": "Walwa"}}
    monkeypatch.setattr(scraper, "_fetch_feed", lambda: [feature])
    scraper.fetch_fire_data()
    assert database.read_df(
        "SELECT COUNT(*) AS n FROM fire_area_history").iloc[0]["n"] == 1

    def boom(now):
        raise RuntimeError("history broken")
    monkeypatch.setattr(fa, "record", boom)
    scraper.fetch_fire_data()                       # still collects


# --------------------------------------------------------------------------- #
# Change
# --------------------------------------------------------------------------- #
def test_growth_over_the_window_and_the_latest_step():
    history("f1", square(145, -37, 0.05), NOW - timedelta(hours=8))
    history("f1", square(145, -37, 0.06), NOW - timedelta(hours=2))
    history("f1", square(145, -37, 0.08), NOW - timedelta(minutes=2))
    ch = fa.area_changes(["f1"], NOW, window_hours=6)["f1"]
    full = fa.area_ha(fa.polygons_only(square(145, -37, 0.08)))
    base = fa.area_ha(fa.polygons_only(square(145, -37, 0.05)))
    prev = fa.area_ha(fa.polygons_only(square(145, -37, 0.06)))
    # Window: against the shape at the window's start (the 8 h old one).
    assert ch.grown_ha == pytest.approx(full - base, rel=0.03)
    assert ch.reduced_ha == 0
    # Step: against the shape just before the newest one.
    assert ch.step_grown_ha == pytest.approx(full - prev, rel=0.03)
    assert ch.step_at == NOW - timedelta(minutes=2)
    assert ch.grown["type"] in ("Polygon", "MultiPolygon")


def test_reduction_is_reported_separately():
    history("f1", square(145, -37, 0.08), NOW - timedelta(hours=1))
    history("f1", square(145, -37, 0.05), NOW - timedelta(minutes=5))
    ch = fa.area_changes(["f1"], NOW)["f1"]
    assert ch.grown_ha == 0
    assert ch.reduced_ha > 1000 and ch.step_reduced_ha > 1000
    assert ch.reduced is not None and ch.step_reduced is not None


def test_redigitising_slivers_are_not_growth():
    # The perimeter nudged ~5 m east: a thin sliver, not a new front.
    history("f1", square(145, -37, 0.05), NOW - timedelta(hours=1))
    history("f1", square(145.00005, -37, 0.05), NOW - timedelta(minutes=5))
    ch = fa.area_changes(["f1"], NOW)["f1"]
    assert ch.grown_ha == 0 and ch.reduced_ha == 0 and ch.step_at is None


def test_a_change_older_than_the_window_does_not_breathe_or_count():
    history("f1", square(145, -37, 0.05), NOW - timedelta(hours=20))
    history("f1", square(145, -37, 0.08), NOW - timedelta(hours=10))
    ch = fa.area_changes(["f1"], NOW, window_hours=6)["f1"]
    assert not ch.changed and ch.step_at is None
    assert ch.area_ha > 0


def test_first_shape_is_a_baseline_not_growth():
    history("f1", square(145, -37, 0.05), NOW - timedelta(minutes=2))
    ch = fa.area_changes(["f1"], NOW)["f1"]
    assert not ch.changed and ch.step_at is None


# --------------------------------------------------------------------------- #
# Areas of operation
# --------------------------------------------------------------------------- #
def _areas(link_km=5, now=NOW, window=6):
    snap = fa.compute(now, link_km=link_km, window_hours=window)
    assert snap["ok"], snap["error"]
    return snap


def _ids(area):
    return sorted(m.id for m in area.members)


def test_nearby_fires_link_and_distant_ones_do_not():
    put("a1", lat=-37.0, lon=145.0)
    put("a2", lat=-37.02, lon=145.02)          # ~3 km away
    put("far", lat=-38.0, lon=147.0)
    areas = _areas()["areas"]
    assert sorted(_ids(a) for a in areas) == [["a1", "a2"], ["far"]]


def test_link_distance_is_the_option():
    put("a1", lat=-37.0, lon=145.0)
    put("a2", lat=-37.0, lon=145.09)           # ~8 km away
    assert len(_areas(link_km=5)["areas"]) == 2
    assert len(_areas(link_km=10)["areas"]) == 1


def test_a_warning_covering_two_fires_joins_them():
    put("a1", lat=-37.0, lon=145.0)
    put("a2", lat=-37.0, lon=145.3)            # ~27 km apart
    put("w1", square(144.95, -37.05, 0.4), category="Watch and Act",
        feed_type="warning", level="Watch and Act", event="Bushfire",
        lat=-36.85, lon=145.15)
    areas = _areas()["areas"]
    assert len(areas) == 1
    assert _ids(areas[0]) == ["a1", "a2", "w1"]
    assert areas[0].level == "Watch and Act"


def test_other_incidents_attach_but_never_chain_two_fires():
    put("a1", lat=-37.0, lon=145.0)
    put("a2", lat=-37.0, lon=145.12)           # ~11 km: separate at 5 km
    put("tree", category="Tree Down", lat=-37.0, lon=145.06)  # ~5.3/5.3 km
    put("tree2", category="Tree Down", lat=-37.0, lon=145.02)  # near a1 only
    put("lonely", category="Tree Down", lat=-38.5, lon=146.5)
    areas = _areas(link_km=5)["areas"]
    assert len(areas) == 2
    members = {m.id for a in areas for m in a.members}
    assert "tree2" in members and "lonely" not in members
    a1 = fa.area_for_member(areas, "a1")
    assert "tree2" in a1.member_ids() and "a2" not in a1.member_ids()


def test_a_non_fire_warning_alone_is_not_an_area():
    put("flood", square(145, -37, 0.2), category="Advice", feed_type="warning",
        level="Advice", event="Riverine Flood")
    assert _areas()["areas"] == []


def test_a_fire_warning_alone_is_an_area():
    put("w1", square(145, -37, 0.2), category="Emergency Warning",
        feed_type="warning", level="Emergency Warning", event="Bushfire")
    areas = _areas()["areas"]
    assert len(areas) == 1 and areas[0].level == "Emergency Warning"


def test_historical_burn_areas_stay_out_and_recent_ones_join():
    put("a1", lat=-37.0, lon=145.0)
    put("old", square(145.0, -37.01, 0.02, point=False), feed_type="burn-area",
        category=None, created=NOW - timedelta(days=400))
    put("new", square(145.0, -37.01, 0.02, point=False), feed_type="burn-area",
        category=None, created=NOW - timedelta(days=1))
    areas = _areas()["areas"]
    assert len(areas) == 1
    assert "new" in areas[0].member_ids() and "old" not in areas[0].member_ids()


def test_a_moving_burn_area_is_an_area_on_its_own():
    put("b1", square(146, -37, 0.05, point=False), feed_type="burn-area",
        category=None, created=NOW - timedelta(days=400))
    history("b1", square(146, -37, 0.05, point=False), NOW - timedelta(hours=2))
    history("b1", square(146, -37, 0.08, point=False), NOW - timedelta(minutes=2))
    areas = _areas()["areas"]
    assert len(areas) == 1 and areas[0].grown_ha > 0
    assert areas[0].step_at == NOW - timedelta(minutes=2)


def test_area_rolls_up_burnt_area_and_growth_and_is_named_after_the_largest_fire():
    put("small", square(145.0, -37.0, 0.01), location="Small Gully")
    put("big", square(145.02, -37.0, 0.05), location="Big Ridge",
        lat=-37.0, lon=145.02)
    history("small", square(145.0, -37.0, 0.01), NOW - timedelta(hours=1))
    history("big", square(145.02, -37.0, 0.04), NOW - timedelta(hours=1))
    history("big", square(145.02, -37.0, 0.05), NOW - timedelta(minutes=1))
    area = _areas()["areas"][0]
    assert area.name == "Big Ridge +1 fire"
    assert area.grown_ha > 0 and area.area_ha > area.grown_ha
    assert -37.1 < area.center["lat"] < -36.9 and 6 <= area.zoom <= 12


def test_areas_sort_most_severe_warning_first():
    put("quiet", lat=-38.0, lon=147.0, location="Quiet")
    put("hot", lat=-37.0, lon=145.0, location="Hot")
    put("ew", None, category="Emergency Warning", feed_type="warning",
        level="Emergency Warning", event="Bushfire", lat=-37.0, lon=145.01)
    areas = _areas()["areas"]
    assert [a.name for a in areas] == ["Hot", "Quiet"]


def test_focus_view_zooms_in_on_small_areas_and_out_on_big_ones():
    _, small = fa.focus_view((145.0, -37.0, 145.05, -36.95))
    _, big = fa.focus_view((144.0, -38.0, 147.0, -36.0))
    assert small > big
    _, tiny = fa.focus_view((145.0, -37.0, 145.0, -37.0))
    assert tiny <= 12


# --------------------------------------------------------------------------- #
# New / takeover / focus
# --------------------------------------------------------------------------- #
def test_events_and_first_snapshot_only_seeds():
    put("a1", square(145, -37, 0.08))
    put("w1", None, category="Watch and Act", feed_type="warning",
        level="Watch and Act", event="Bushfire", lat=-37.0, lon=145.01)
    history("a1", square(145, -37, 0.05), NOW - timedelta(hours=1))
    history("a1", square(145, -37, 0.08), NOW - timedelta(minutes=2))
    snap = _areas()
    events = fa.events(snap)
    assert {e["kind"] for e in events} == {"growth", "warning"}
    seen, fresh = fa.detect_new(None, events)
    assert fresh == []
    seen, fresh = fa.detect_new(seen, events)
    assert fresh == []
    escalated = events + [{"key": "warn:w1:Emergency Warning", "member": "w1",
                           "kind": "warning", "level": "Emergency Warning",
                           "text": "x"}]
    _, fresh = fa.detect_new(seen, escalated)
    assert [e["key"] for e in fresh] == ["warn:w1:Emergency Warning"]


def test_advice_alone_never_takes_over():
    put("a1")
    put("w1", None, category="Advice", feed_type="warning", level="Advice",
        event="Bushfire", lat=-37.0, lon=145.01)
    assert fa.events(_areas()) == []


def test_choose_focus_rotates_pins_and_takes_over():
    keys = ["a", "b"]
    assert fiw.choose_focus(keys, 0, True)[0] is None          # the state first
    assert fiw.choose_focus(keys, 1, True)[0] == "a"
    assert fiw.choose_focus(keys, 3, True)[0] is None          # wraps
    assert fiw.choose_focus(keys, 0, False)[0] == "a"
    assert fiw.choose_focus([], 5, False)[0] is None           # nothing: the state
    assert fiw.choose_focus(keys, 1, True, pin="b")[:1] == ("b",)
    assert fiw.choose_focus(keys, 1, True, pin=fiw.STATE_KEY)[0] is None
    assert fiw.choose_focus(keys, 1, True, pin="gone")[3] == "rotate"
    assert fiw.choose_focus(keys, 1, True, pin="b", takeover="a")[::3] == ("a", "takeover")


def test_hot_flash_prefers_emergency_then_growth_and_expires():
    put("a1")
    put("a2", lat=-38.0, lon=147.0)
    areas = _areas()["areas"]
    flashes = fiw.update_flashes(None, [
        {"key": "g", "member": "a1", "kind": "growth", "text": "A1 +5 ha"},
        {"key": "w", "member": "a2", "kind": "warning", "level": "Watch and Act",
         "text": "x"}], now=1000.0)
    flash, area = fiw.hot_flash(flashes, areas, 1001.0)
    assert flash["key"] == "g" and area.key == "a1"
    assert fiw.hot_flash(flashes, areas, 1000.0 + fiw.TAKEOVER_SECONDS)[0] is None
    assert fiw.update_flashes(flashes, [], 1000.0 + fiw.FLASH_SECONDS) == []


# --------------------------------------------------------------------------- #
# Tiles and map
# --------------------------------------------------------------------------- #
def test_tiles_keep_warning_levels_separate_and_count_fire_warnings_only():
    put("a1")
    put("w1", None, category="Watch and Act", feed_type="warning",
        level="Watch and Act", event="Grass Fire", lat=-37, lon=145.01)
    put("w2", None, category="Watch and Act", feed_type="warning",
        level="Watch and Act", event="Riverine Flood", lat=-36, lon=144)
    counts = fiw.warning_counts(_areas())
    assert counts == {"Emergency Warning": 0, "Watch and Act": 1, "Advice": 0}
    assert len(fiw.tiles(_areas())) == 7


def test_tiles_say_dash_not_zero_when_unreadable():
    snap = {"ok": False, "areas": [], "window_hours": 6}
    values = [t.children[1].children for t in fiw.tiles(snap)]
    assert set(values) == {"—"}


def _layers(fig):
    return [l.to_plotly_json() for l in fig.layout.map.layers]


def test_map_draws_growth_red_reduction_blue_and_breathes_the_latest_step():
    put("a1", square(145, -37, 0.08))
    put("a2", square(146, -37, 0.05), lat=-37.0, lon=146.0)
    history("a1", square(145, -37, 0.05), NOW - timedelta(hours=1))
    history("a1", square(145, -37, 0.08), NOW - timedelta(minutes=2))
    history("a2", square(146, -37, 0.08), NOW - timedelta(hours=1))
    history("a2", square(146, -37, 0.05), NOW - timedelta(hours=3, minutes=-150))
    snap = _areas()
    fig = fiw.map_figure(snap, snap["areas"][0], fiw.DEFAULT_LAYERS, True, NOW)
    layers = _layers(fig)
    colours = [l.get("color") for l in layers]
    assert fiw.GROWTH_COLOUR in colours and fiw.REDUCTION_COLOUR in colours
    assert fiw.BURNT_COLOUR in colours and fiw.AREA_OUTLINE in colours
    pulsing = [l for l in layers if str(l.get("name", "")).startswith("wd-pulse:")]
    assert pulsing and all(l["color"] in (fiw.GROWTH_COLOUR, fiw.REDUCTION_COLOUR)
                           for l in pulsing)
    deadline = int(pulsing[0]["name"].split(":")[1])
    expected = (NOW - timedelta(minutes=2)
                + timedelta(seconds=fiw.PULSE_SECONDS)).timestamp() * 1000
    assert deadline == int(expected)
    assert fig.layout.meta["wdFocus"] == snap["areas"][0].key
    assert fig.layout.map.zoom == snap["areas"][0].zoom

    # After the pulse window: still red, no longer breathing.
    later = NOW + timedelta(seconds=fiw.PULSE_SECONDS + 60)
    calm = _layers(fiw.map_figure(snap, snap["areas"][0], fiw.DEFAULT_LAYERS,
                                  True, later))
    assert not [l for l in calm if str(l.get("name", "")).startswith("wd-pulse:")]
    assert fiw.GROWTH_COLOUR in [l.get("color") for l in calm]


def test_layer_chips_switch_burn_layers_off():
    put("a1", square(145, -37, 0.08))
    history("a1", square(145, -37, 0.05), NOW - timedelta(hours=1))
    history("a1", square(145, -37, 0.08), NOW - timedelta(minutes=2))
    snap = _areas()
    fig = fiw.map_figure(snap, None, ["fires"], False, NOW)
    assert _layers(fig) == []
    assert fig.layout.meta["wdFocus"] == fiw.STATE_KEY


def test_fire_polygons_are_drawn_once_as_burnt_area():
    put("a1", square(145, -37, 0.08))
    history("a1", square(145, -37, 0.08), NOW - timedelta(hours=1))
    snap = _areas()
    layers = _layers(fiw.map_figure(snap, snap["areas"][0], ["fires", "burnt"],
                                    False, NOW))
    from app.pages import fire as fire_page
    assert fire_page.KIND_COLOURS["Fire"] not in [l.get("color") for l in layers]
    assert fiw.BURNT_COLOUR in [l.get("color") for l in layers]


def test_focus_card_and_list_render():
    put("a1", square(145, -37, 0.08))
    history("a1", square(145, -37, 0.05), NOW - timedelta(hours=1))
    history("a1", square(145, -37, 0.08), NOW - timedelta(minutes=2))
    snap = _areas()
    area = snap["areas"][0]
    assert fiw.focus_card(area, snap, "rotate", NOW) is not None
    assert fiw.focus_card(None, snap, "pinned", NOW) is not None
    cards = fiw.area_list(snap, [])
    assert len(cards) == 2 and cards[0].id["key"] == fiw.STATE_KEY
    assert getattr(cards[1], "data-key") == area.key


def test_quiet_state_is_said_plainly():
    snap = _areas()
    assert snap["areas"] == []
    fig = fiw.map_figure(snap, None, fiw.DEFAULT_LAYERS, True, NOW)
    assert fig.layout.map.zoom == fiw.STATE_VIEW[1]


# --------------------------------------------------------------------------- #
# Test run (/wall/fire/test) — simulated, in memory, admin only
# --------------------------------------------------------------------------- #
from app import auth, fire_demo  # noqa: E402


def _test_snap(step, link_km=5):
    start, _, _ = fire_demo.clock(1_800_000_000.0)
    return fire_demo.snapshot(link_km, 6, now_ts=start + step * fire_demo.STEP_SECONDS + 5)


def test_fire_warning_is_judged_by_hazard_not_headline():
    flood = {"event": "Riverine Flood", "headline": "Flooding near Fire Station Rd"}
    assert not fa.is_fire_warning(flood)
    assert fa.is_fire_warning({"event": "Bushfire"})
    assert fa.is_fire_warning({"event": None, "headline": "Grass fire near X"})


def test_test_run_links_and_separates_as_intended():
    snap = _test_snap(0)
    assert snap["ok"] and snap["test"]["step"] == 1
    ids = sorted(sorted(a.member_ids()) for a in snap["areas"])
    assert ["TEST-alpha", "TEST-bravo", "TEST-tree", "TEST-warning"] in ids
    assert ["TEST-charlie", "TEST-charlie-advice"] in ids
    assert not any("TEST-flood" in a.member_ids() for a in snap["areas"])


def test_test_run_grows_shrinks_and_escalates():
    levels, grown, reduced = [], [], []
    for step in range(fire_demo.STEPS):
        area = fa.area_for_member(_test_snap(step)["areas"], "TEST-alpha")
        levels.append(area.level)
        grown.append(area.grown_ha)
        reduced.append(area.reduced_ha)
    assert levels == ["Advice", "Advice", "Watch and Act", "Watch and Act",
                      "Emergency Warning", "Emergency Warning"]
    assert grown[0] == 0 and all(b > a for a, b in zip(grown, grown[1:]))
    assert reduced[:3] == [0, 0, 0] and reduced[3] > 0


def test_test_run_produces_takeovers_and_breathing_layers():
    snap = _test_snap(2)
    kinds = {e["kind"] for e in fa.events(snap)}
    assert kinds == {"growth", "warning"}
    area = fa.area_for_member(snap["areas"], "TEST-alpha")
    layers = _layers(fiw.map_figure(snap, area, fiw.DEFAULT_LAYERS, True,
                                    snap["at"]))
    assert any(str(l.get("name", "")).startswith("wd-pulse:") for l in layers)


def test_test_run_writes_nothing():
    _test_snap(3)
    for table in ("fire_incidents", "fire_area_history", "entity_state_history",
                  "intel_events"):
        assert database.read_df(f"SELECT COUNT(*) AS n FROM {table}").iloc[0]["n"] == 0


def test_test_run_is_admin_only(monkeypatch):
    monkeypatch.setattr(auth, "is_admin", lambda: False)
    assert "test" not in fiw.snapshot(5, 6, test=True)
    monkeypatch.setattr(auth, "is_admin", lambda: True)
    assert fiw.snapshot(5, 6, test=True)["test"]["steps"] == fire_demo.STEPS
    assert "test" not in fiw.snapshot(5, 6, test=False)


def test_a_direct_callback_post_cannot_get_simulated_data():
    from app.factory import create_app
    client = create_app(autostart=False).server.test_client()
    outputs = [{"id": "fiw-list", "property": "children"}]
    inputs = [{"id": "fiw-interval", "property": "n_intervals", "value": 1},
              {"id": "fiw-flash", "property": "data", "value": None},
              {"id": "fiw-link-km", "property": "value", "value": 5},
              {"id": "fiw-window", "property": "value", "value": 6}]
    r = client.post("/_dash-update-component", json={
        "output": "fiw-list.children", "outputs": outputs[0], "inputs": inputs,
        "state": [{"id": "fiw-test", "property": "data", "value": True}],
        "changedPropIds": ["fiw-interval.n_intervals"]})
    assert r.status_code == 200
    assert "TEST Fire" not in r.get_data(as_text=True)


def test_test_page_is_routed_and_locked_for_anonymous():
    from dash import Dash
    Dash(__name__)
    assert shell.FIRE_WALL_TEST_PATH == "/wall/fire/test"
    assert "wall-mode" in shell.root_class(True, "console", None,
                                           shell.FIRE_WALL_TEST_PATH)
    assert "Admin" in str(fiw.test_locked())
    page = str(fiw.layout(test=True))
    assert "TEST RUN" in page and "fiw-test" in page


def test_refresh_callback_runs_the_test_for_an_admin(monkeypatch):
    """Drives the real tiles/stale/seen/flash callback through Dash, in the
    order the page declares its inputs and states."""
    from app.factory import create_app
    monkeypatch.setattr(auth, "is_admin", lambda: True)
    client = create_app(autostart=False).server.test_client()
    outs = [{"id": i, "property": p} for i, p in (
        ("fiw-tiles", "children"), ("fiw-stale", "children"),
        ("fiw-stale", "title"), ("fiw-seen", "data"), ("fiw-flash", "data"))]
    body = {"output": "..%s.." % "...".join(f"{o['id']}.{o['property']}" for o in outs),
            "outputs": outs,
            "inputs": [{"id": "fiw-interval", "property": "n_intervals", "value": 1},
                       {"id": "fiw-link-km", "property": "value", "value": 5},
                       {"id": "fiw-window", "property": "value", "value": 6}],
            "state": [{"id": "fiw-seen", "property": "data", "value": []},
                      {"id": "fiw-flash", "property": "data", "value": []},
                      {"id": "fiw-test", "property": "data", "value": True}],
            "changedPropIds": ["fiw-interval.n_intervals"]}
    r = client.post("/_dash-update-component", json=body)
    assert r.status_code == 200, r.get_data(as_text=True)[:500]
    text = r.get_data(as_text=True)
    assert "TEST RUN" in text and "Areas of operation" in text

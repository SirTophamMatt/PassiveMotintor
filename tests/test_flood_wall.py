"""Flood wall (/wall/flood): gauge selection, new/upgrade detection, paging,
the map layer toggles and the wiring that makes it a wall."""
from datetime import datetime, timedelta

import pandas as pd
import pytest

from app import database, shell
from app.pages import flood_wall as fw

NOW = 1_000_000.0


def _stamp(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _gauge(name, heights, minor=1.0, moderate=2.0, major=3.0, located=True,
           age_minutes=0):
    """Readings every 15 minutes, the last `age_minutes` ago."""
    key = name.lower()
    database.insert_rows("flood_levels", [{
        "station_key": key, "station_name": name,
        "minor": minor, "moderate": moderate, "major": major}])
    if located:
        database.insert_rows("gauge_coords", [{
            "station_key": key, "station_name": name,
            "latitude": -37.7, "longitude": 145.5}])
    last = datetime.now() - timedelta(minutes=age_minutes)
    rows = [{"event": "live", "station_name": name, "height_m": h,
             "timestamp": _stamp(last - timedelta(minutes=15 * (len(heights) - 1 - i)))}
            for i, h in enumerate(heights)]
    database.insert_rows("flood_observations", rows, ignore_duplicates=True)


@pytest.fixture
def fresh_cache():
    fw.reset_cache()
    yield
    fw.reset_cache()


# --------------------------------------------------------------------------- #
# Wiring
# --------------------------------------------------------------------------- #
def test_flood_wall_gets_wall_mode_and_general_wall_still_does():
    assert "wall-mode" in shell.root_class(True, "console", None, shell.FLOOD_WALL_PATH)
    assert "wall-mode" in shell.root_class(True, "console", None, shell.WALL_PATH)
    assert "wall-mode" not in shell.root_class(True, "console", None, "/wallaby")


def test_flood_wall_is_public_and_in_the_switcher():
    from app.factory import PUBLIC_PAGES, RESTRICTED
    from app.pages import wall
    assert shell.FLOOD_WALL_PATH in [p for p, _, _ in PUBLIC_PAGES]
    assert shell.FLOOD_WALL_PATH not in RESTRICTED
    assert shell.FLOOD_WALL_PATH in [p for p, _ in wall.SCENARIOS]


def test_layouts_build():
    from dash import Dash

    from app.pages import wall
    Dash(__name__)
    assert fw.layout() is not None and wall.layout() is not None


def test_every_layer_chip_maps_to_something():
    for key, *_ in fw.LAYERS:
        assert (key in ("gauges", "gauges_below", "road_closures", "road_other")
                or fw._fire_kinds([key]))
    assert "gauges_below" not in fw.DEFAULT_LAYERS
    assert fw._fire_kinds(["advice"]) == {"Advice"}
    assert "Fire" in fw._fire_kinds(["incidents"])
    assert "Advice" not in fw._fire_kinds(["incidents", "emergency"])


# --------------------------------------------------------------------------- #
# New / upgraded detection
# --------------------------------------------------------------------------- #
def test_first_snapshot_only_seeds():
    seen, fresh = fw.detect_new(None, {"a": 3, "b": 1}, NOW)
    assert fresh == {} and set(seen) == {"a", "b"}


def test_new_gauge_and_upgrade_are_flagged_but_not_downgrade():
    seen, _ = fw.detect_new(None, {"a": 3, "b": 2}, NOW)
    seen, fresh = fw.detect_new(seen, {"a": 2, "b": 3, "c": 3}, NOW + 30)
    assert fresh == {"a": "up", "c": "new"}      # b eased off: no flash


def test_gauge_hovering_around_minor_flashes_once():
    seen, _ = fw.detect_new(None, {}, NOW)
    seen, fresh = fw.detect_new(seen, {"a": 3}, NOW + 30)
    assert fresh == {"a": "new"}
    seen, fresh = fw.detect_new(seen, {}, NOW + 60)          # dips below
    seen, fresh = fw.detect_new(seen, {"a": 3}, NOW + 90)    # back again
    assert fresh == {}
    # ...but long after it left, returning is news again.
    seen, _ = fw.detect_new(seen, {}, NOW + 120)
    seen, _ = fw.detect_new(seen, {}, NOW + 120 + fw.QUIET_SECONDS)
    seen, fresh = fw.detect_new(seen, {"a": 3}, NOW + 120 + fw.QUIET_SECONDS + 1)
    assert fresh == {"a": "new"}


def test_moderate_boundary_flapping_does_not_reflash():
    seen, _ = fw.detect_new(None, {"a": 3}, NOW)
    seen, fresh = fw.detect_new(seen, {"a": 2}, NOW + 30)
    assert fresh == {"a": "up"}
    seen, fresh = fw.detect_new(seen, {"a": 3}, NOW + 60)
    seen, fresh = fw.detect_new(seen, {"a": 2}, NOW + 90)
    assert fresh == {}


def test_flashes_expire_and_drop_gauges_no_longer_flooding():
    gauges = [{"key": "a", "label": "Minor flooding"},
              {"key": "b", "label": "Major flooding"}]
    flashes = fw.update_flashes({}, {"a": "new", "b": "up"}, gauges, NOW)
    assert flashes["b"] == {"at": NOW, "kind": "up", "label": "Major flooding"}
    later = fw.update_flashes(flashes, {}, gauges[1:], NOW + 10)
    assert set(later) == {"b"}
    assert fw.update_flashes(later, {}, gauges, NOW + fw.FLASH_SECONDS + 1) == {}


# --------------------------------------------------------------------------- #
# Paging and takeover
# --------------------------------------------------------------------------- #
def test_pages_rotate_and_wrap():
    keys = list("abcdefghi")
    assert fw.choose_page(keys, 4, 0, {}, NOW) == (list("abcd"), 0, 3, False)
    assert fw.choose_page(keys, 4, 2, {}, NOW)[0] == ["i"]
    assert fw.choose_page(keys, 4, 3, {}, NOW)[1] == 0
    assert fw.choose_page(keys, 1, 4, {}, NOW)[0] == ["e"]
    assert fw.choose_page(keys, 99, None, {}, NOW)[0] == list("abcd")  # bad value


def test_new_gauge_takes_over_then_rotation_resumes():
    keys = list("abcdef")
    flashes = {"e": {"at": NOW, "kind": "new", "label": "Minor flooding"}}
    shown, page, _, takeover = fw.choose_page(keys, 4, 0, flashes, NOW + 5)
    assert shown == ["e"] and takeover and page is None
    # A burst is shown together even on the one-gauge page setting.
    burst = {k: {"at": NOW, "kind": "new"} for k in "abcdef"}
    assert fw.choose_page(keys, 1, 0, burst, NOW + 5)[0] == list("abcd")
    shown, _, _, takeover = fw.choose_page(
        keys, 4, 0, flashes, NOW + fw.TAKEOVER_SECONDS + 1)
    assert shown == list("abcd") and not takeover


def test_banner_names_the_change_and_takes_the_worst_colour():
    gauges = [{"key": "a", "station": "Alpha", "priority": 1},
              {"key": "b", "station": "Bravo", "priority": 3}]
    flashes = {"a": {"at": NOW, "kind": "up"}, "b": {"at": NOW, "kind": "new"}}
    text, cls = fw.banner(flashes, gauges, NOW + 1)
    assert "Alpha up to Major" in text and "Bravo now at Minor" in text
    assert "fw-major" in cls and "fw-banner-on" in cls
    assert fw.banner(flashes, gauges, NOW + fw.TAKEOVER_SECONDS + 1) == (None, "fw-banner")


# --------------------------------------------------------------------------- #
# Gauge selection against the database
# --------------------------------------------------------------------------- #
def test_compute_lists_flooding_gauges_severity_first(db, fresh_cache):
    _gauge("Minor Creek", [0.8, 1.1, 1.2, 1.3])
    _gauge("Major River", [3.0, 3.1, 3.2, 3.3])
    _gauge("Dry Gully", [0.2, 0.2, 0.3, 0.3])
    snap = fw.compute()
    assert [g["station"] for g in snap["flooding"]] == ["Major River", "Minor Creek"]
    assert snap["flooding"][0]["label"] == "Major flooding"
    assert [g["station"] for g in snap["near"]] == ["Dry Gully"]
    assert snap["near"][0]["below_minor_m"] == pytest.approx(0.7)
    assert set(snap["map"]["station_name"]) == {"Minor Creek", "Major River", "Dry Gully"}


def test_stale_gauge_is_not_shown_as_flooding(db, fresh_cache):
    _gauge("Silent River", [2.5, 2.5], age_minutes=(fw.STALE_HOURS + 1) * 60)
    snap = fw.compute()
    assert snap["flooding"] == [] and snap["map"].empty


def test_unlocated_gauge_is_listed_but_not_mapped(db, fresh_cache):
    _gauge("Nowhere Creek", [1.5, 1.6, 1.7, 1.8], located=False)
    snap = fw.compute()
    assert [g["station"] for g in snap["flooding"]] == ["Nowhere Creek"]
    assert snap["map"].empty


def test_rising_gauge_sorts_first_within_its_class(db, fresh_cache):
    _gauge("Steady Creek", [1.5] * 8)
    _gauge("Rising Creek", [1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7, 1.8])
    snap = fw.compute()
    assert [g["station"] for g in snap["flooding"]] == ["Rising Creek", "Steady Creek"]
    assert snap["flooding"][0]["rising"] and not snap["flooding"][1]["rising"]


def test_tiles_count_each_class_separately(db, fresh_cache):
    from app import situation
    _gauge("A", [1.5] * 4)
    _gauge("B", [1.5] * 4)
    _gauge("C", [3.5] * 4)
    tiles = fw.tiles(fw.compute(), situation.compute())
    values = [t.children[1].children for t in tiles]
    assert values[:3] == ["1", "0", "2"]          # Major, Moderate, Minor


def test_unreadable_gauge_list_shows_dashes_not_zero():
    from app import situation
    snap = {"ok": False, "flooding": [], "near": [], "map": pd.DataFrame()}
    tiles = fw.tiles(snap, situation.Situation(datetime.now()))
    assert tiles[0].children[1].children == "—"


def test_cards_and_map_render(db, fresh_cache):
    from dash import Dash
    Dash(__name__)
    _gauge("Card River", [1.1, 1.2, 1.3, 1.4, 1.5, 1.6])
    snap = fw.compute()
    g = snap["flooding"][0]
    for single in (False, True):
        assert fw.gauge_card(g, True, {"at": NOW, "kind": "new"}, single,
                             datetime.now(), takeover=True) is not None
    fig = fw.map_figure(snap, fw.DEFAULT_LAYERS, True, [g["key"]])
    names = [t.text for t in fig.data if getattr(t, "text", None) is not None]
    assert any("Card River" in list(n) for n in names), "on-screen gauge is ringed"
    # Gauges switched off: only the ring remains for the flood layer.
    off = fw.map_figure(snap, ["roads"], False, [])
    assert all(t.name is None or not str(t.name).startswith("Gauge") for t in off.data)


def test_quiet_panel_when_nothing_is_flooding(db, fresh_cache):
    _gauge("Low River", [0.4] * 4)
    assert fw.quiet_panel(fw.compute()) is not None


# --------------------------------------------------------------------------- #
# Roads: event tag + causes
# --------------------------------------------------------------------------- #
from app.modules.roads import data as roads_data  # noqa: E402


@pytest.mark.parametrize("dtype, desc, expected", [
    ("Flooding, Water over road", None, {"flooding"}),
    ("Hazard, Fallen tree", None, {"trees"}),
    ("Weather, Storm damage", None, {"weather"}),
    ("Hazard, Landslip", None, {"weather"}),
    ("Crash, Vehicle", "Cabbage Tree Road closed near Tree St", {"other"}),
    ("Hazard", "Road closed due to fallen trees", {"trees"}),
    ("Incident", "Closed because of flooding over the causeway", {"flooding"}),
    (None, None, {"other"}),
    (float("nan"), float("nan"), {"other"}),
])
def test_road_causes(dtype, desc, expected):
    assert roads_data.causes_of(dtype, desc) == expected


def _road(sid, dtype, closure, created, resolved=0, desc=None):
    return {"source_id": sid, "disruption_type": dtype, "is_closure": closure,
            "created": created, "first_seen": created, "last_seen": created,
            "resolved": resolved, "description": desc, "road_name": sid,
            "latitude": -37.5, "longitude": 145.0}


@pytest.fixture
def roads_and_event(db):
    from app import tags
    tags.create_tag("Oct floods", "2026-10-01 06:00:00")
    database.insert_rows("road_disruptions", [
        _road("before", "Flooding, Water over road", 1, "2026-09-30 22:00:00"),
        _road("flood", "Flooding, Water over road", 1, "2026-10-01 09:00:00"),
        _road("tree", "Hazard, Fallen tree", 1, "2026-10-01 10:00:00"),
        _road("lane", "Flooding, Water over road", 0, "2026-10-01 11:00:00"),
        _road("crash", "Crash", 1, "2026-10-01 12:00:00"),
        _road("reopened", "Flooding", 1, "2026-10-01 13:00:00", resolved=1),
    ])
    return tags.list_tags()[0]["id"]


def test_event_and_causes_limit_roads_to_current_event_closures(roads_and_event):
    df, tag = fw.road_selection(roads_and_event, fw.DEFAULT_ROAD_CAUSES)
    assert tag["name"] == "Oct floods"
    assert set(df["source_id"]) == {"flood", "tree", "lane"}
    # No event: the pre-event flooding closure counts again.
    df, tag = fw.road_selection(None, fw.DEFAULT_ROAD_CAUSES)
    assert tag is None and "before" in set(df["source_id"])
    # Other causes on: the crash appears; nothing ticked: nothing at all.
    assert "crash" in set(fw.road_selection(None, ["other"])[0]["source_id"])
    assert fw.road_selection(None, [])[0].empty


def test_road_tile_counts_closures_only_and_explains_itself(roads_and_event):
    from app import situation
    roads, tag = fw.road_selection(roads_and_event, fw.DEFAULT_ROAD_CAUSES)
    note = fw.road_note(tag, fw.DEFAULT_ROAD_CAUSES)
    snap = {"ok": True, "flooding": [], "near": [], "map": pd.DataFrame()}
    road_tile = fw.tiles(snap, situation.Situation(datetime.now()), roads, note)[-1]
    assert road_tile.children[1].children == "2"      # flood + tree, not the lane
    assert "Oct floods" in road_tile.title and "flooding" in road_tile.title


def test_map_splits_closures_from_other_disruptions(roads_and_event):
    roads, _ = fw.road_selection(roads_and_event, fw.DEFAULT_ROAD_CAUSES)
    snap = {"ok": True, "flooding": [], "near": [], "map": pd.DataFrame()}
    closed = fw.map_figure(snap, ["road_closures"], True, [], roads=roads)
    names = {t.name for t in closed.data if t.name}
    assert names == {"Road: Closure"}
    both = fw.map_figure(snap, ["road_closures", "road_other"], True, [], roads=roads)
    assert {t.name for t in both.data if t.name} == {"Road: Closure",
                                                    "Road: Other disruption"}


def test_unknown_or_deleted_event_falls_back_to_everything_current(roads_and_event):
    df, tag = fw.road_selection(99999, fw.DEFAULT_ROAD_CAUSES)
    assert tag is None and "before" in set(df["source_id"])


# --------------------------------------------------------------------------- #
# Incidents: agency filter (SES etc.) + event tag
# --------------------------------------------------------------------------- #
from app.modules.fire import data as fire_data  # noqa: E402


@pytest.mark.parametrize("org, expected", [
    ("VIC/SES", "ses"), ("vic/ses", "ses"), ("VIC/CFA", "cfa"), ("VIC/FRV", "frv"),
    ("VIC/MFB", "frv"), ("VIC/DELWP", "ffm"), ("VIC/DEECA", "ffm"),
    ("VIC/EMV", "other"), (None, "other"), (float("nan"), "other"),
])
def test_agency_of(org, expected):
    assert fire_data.agency_of(org) == expected


def _incident(sid, org, created, feed_type="incident", category="Flooding",
              level=None):
    return {"source_id": sid, "feed_type": feed_type, "category1": level or category,
            "warning_level": level, "source_org": org, "location": sid,
            "latitude": -37.0, "longitude": 145.0, "created": created,
            "first_seen": created, "last_seen": created, "updated": created,
            "resolved": 0}


@pytest.fixture
def incidents_and_event(db):
    from app import tags
    tags.create_tag("Oct floods", "2026-10-01 06:00:00")
    database.insert_rows("fire_incidents", [
        _incident("ses-old", "VIC/SES", "2026-09-30 20:00:00"),
        _incident("ses-new", "VIC/SES", "2026-10-01 09:00:00", category="Tree Down"),
        _incident("cfa-new", "VIC/CFA", "2026-10-01 10:00:00", category="Fire"),
        _incident("warn", "VIC/SES", "2026-10-01 11:00:00", feed_type="warning",
                  level="Watch and Act"),
    ])
    return tags.list_tags()[0]["id"]


def test_incidents_filter_by_agency_and_event_and_never_include_warnings(
        incidents_and_event):
    df, _ = fw.incident_selection(None, None)
    assert set(df["source_id"]) == {"ses-old", "ses-new", "cfa-new"}
    df, _ = fw.incident_selection(None, ["ses"])
    assert set(df["source_id"]) == {"ses-old", "ses-new"}
    df, tag = fw.incident_selection(incidents_and_event, ["ses"])
    assert tag["name"] == "Oct floods" and set(df["source_id"]) == {"ses-new"}
    assert fw.incident_selection(None, [])[0].empty


def test_incident_tile_counts_and_names_the_agency(incidents_and_event):
    from app import situation
    snap = {"ok": True, "flooding": [], "near": [], "map": pd.DataFrame()}
    incidents, tag = fw.incident_selection(None, ["ses"])
    note = fw.road_note(tag, fw.DEFAULT_ROAD_CAUSES, ["ses"])
    tiles = fw.tiles(snap, situation.Situation(datetime.now()), None, note,
                     incidents, ["ses"])
    tile = next(t for t in tiles if "incidents" in t.children[0].children.lower())
    assert tile.children[0].children == "SES incidents"
    assert tile.children[1].children == "2"
    assert "Incidents: SES" in note
    assert fw.incident_label(None) == "Incidents"


def test_map_filters_incidents_but_keeps_warnings(incidents_and_event):
    incidents, _ = fw.incident_selection(None, ["ses"])
    snap = {"ok": True, "flooding": [], "near": [], "map": pd.DataFrame()}
    fig = fw.map_figure(snap, fw.DEFAULT_LAYERS, True, [], incidents=incidents)
    texts = " ".join(str(t.text) for t in fig.data if getattr(t, "text", None) is not None)
    assert "ses-new" in texts.lower() or "Ses-New" in texts
    assert "cfa-new" not in texts.lower()
    assert "warn" in texts.lower()          # the Watch and Act stays on the map

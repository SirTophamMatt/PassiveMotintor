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
        assert key in ("gauges", "gauges_below", "roads") or fw._fire_kinds([key])
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

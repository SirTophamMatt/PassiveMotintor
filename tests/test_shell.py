"""Shell layouts, colour schemes, the situation model and the wall display."""
import os
import re
from datetime import datetime

import pytest

from app import shell, situation
from app.briefing import SourceStatus

STYLE = os.path.join(os.path.dirname(__file__), "..", "assets", "style.css")


# --------------------------------------------------------------------------- #
# root_class
# --------------------------------------------------------------------------- #
def test_root_class_defaults_to_console_dark_with_no_scheme_class():
    assert shell.DEFAULT_LAYOUT == shell.LAYOUT_CONSOLE
    assert shell.root_class(True, None, None, "/") == "app dark layout-console"
    assert shell.root_class(True, "bogus", None, "/") == "app dark layout-console"
    assert shell.root_class(True, "classic", None, "/") == "app dark layout-classic"


def test_root_class_console_scheme_and_light():
    cls = shell.root_class(False, shell.LAYOUT_CONSOLE, "midnight", "/fire").split()
    assert cls == ["app", "light", "layout-console", "scheme-midnight"]


def test_default_scheme_adds_no_class():
    assert "scheme-" not in shell.root_class(True, "console", shell.DEFAULT_SCHEME, "/")


def test_unknown_scheme_is_ignored_not_injected():
    # Values come from the browser's localStorage, so never trust them into a class.
    assert "scheme" not in shell.root_class(True, "classic", "evil injected", "/")


def test_wall_path_adds_wall_mode_in_either_layout():
    for layout in (shell.LAYOUT_CLASSIC, shell.LAYOUT_CONSOLE):
        assert "wall-mode" in shell.root_class(True, layout, None, shell.WALL_PATH)
    assert "wall-mode" not in shell.root_class(True, "classic", None, "/wallaby")


def test_desktop_keeps_titlebar_class():
    assert shell.root_class(True, "classic", None, "/", desktop=True).endswith("has-titlebar")


# --------------------------------------------------------------------------- #
# Colour schemes are pinned to the stylesheet
# --------------------------------------------------------------------------- #
def test_every_scheme_has_dark_and_light_css():
    css = open(STYLE, encoding="utf-8").read()
    for scheme, _ in shell.SCHEMES:
        assert f".swatch-{scheme}" in css
        if scheme == shell.DEFAULT_SCHEME:
            continue
        for mode in ("dark", "light"):
            block = re.search(r"\.app\.%s\.scheme-%s\s*\{([^}]*)\}" % (mode, scheme), css)
            assert block, f"missing .app.{mode}.scheme-{scheme}"
            for token in ("--bg:", "--bg-panel:", "--bg-sidebar:", "--text:",
                          "--muted:", "--border:", "--accent:", "--on-accent:"):
                assert token in block.group(1), f"{scheme}/{mode} lacks {token}"


# --------------------------------------------------------------------------- #
# Console nav grouping
# --------------------------------------------------------------------------- #
def test_every_page_lands_in_exactly_one_console_group():
    from app.factory import ALL_PAGES
    items = [(path, label) for path, label, _ in ALL_PAGES]
    grouped = shell.group_items(items)
    placed = [p for _, entries in grouped for p, _ in entries]
    assert sorted(placed) == sorted(p for p, _ in items)


def test_unknown_page_falls_into_tools_rather_than_vanishing():
    grouped = dict(shell.group_items([("/", "Overview"), ("/new", "New Thing")]))
    assert ("/new", "New Thing") in grouped["Tools"]


def test_empty_groups_are_dropped():
    names = [name for name, _ in shell.group_items([("/", "Overview")])]
    assert names == ["Situation"]


def test_wall_is_a_public_page():
    from app.factory import PUBLIC_PAGES, RESTRICTED
    assert shell.WALL_PATH in [p for p, _, _ in PUBLIC_PAGES]
    assert shell.WALL_PATH not in RESTRICTED


# --------------------------------------------------------------------------- #
# Situation model
# --------------------------------------------------------------------------- #
def test_chips_keep_warning_levels_separate_and_ordered():
    chips = situation.build_chips({"emergency": 1, "watch_act": 3, "advice": 12})
    assert [c.key for c in chips[:3]] == ["emergency", "watch_act", "advice"]
    assert [c.value for c in chips[:3]] == [1, 3, 12]


def test_missing_source_shows_dash_not_zero():
    chip = situation.build_chips({"customers_off": None})[6]
    assert chip.key == "customers_off" and chip.display == "—"
    nan = situation.build_chips({"customers_off": float("nan")})[6]
    assert nan.display == "—"


def test_tone_only_applies_when_count_is_positive():
    zero = situation.build_chips({"emergency": 0})[0]
    one = situation.build_chips({"emergency": 1})[0]
    assert zero.active_tone is None and one.active_tone == "emergency"


def test_large_counts_are_grouped():
    chip = situation.build_chips({"customers_off": 21400.0})[6]
    assert chip.display == "21,400"


def test_sources_label_and_stale_list():
    snap = situation.Situation(datetime.now(), sources=[
        SourceStatus("Flood"), SourceStatus("Power", state="stale"),
        SourceStatus("Roads", state="never")])
    assert snap.sources_label == "1/3 sources live"
    assert [s.name for s in snap.stale_sources] == ["Power", "Roads"]


def test_cache_computes_once_within_ttl(monkeypatch):
    calls = []
    monkeypatch.setattr(situation, "compute",
                        lambda: calls.append(1) or situation.Situation(datetime.now()))
    situation.reset_cache()
    situation.current()
    situation.current()
    assert len(calls) == 1
    situation.current(max_age=0)
    assert len(calls) == 2
    situation.reset_cache()


def test_one_broken_module_only_blanks_its_own_chip(monkeypatch):
    from app.modules.fire import data as fire_data
    from app.modules.power import data as power_data

    def boom():
        raise RuntimeError("feed down")
    monkeypatch.setattr(power_data, "latest_totals", boom)
    monkeypatch.setattr(fire_data, "latest_counts",
                        lambda: {"emergency": 2, "watch_act": 0, "advice": 0,
                                 "active_fires": 1})
    snap = situation.compute()
    assert snap.chip("customers_off").display == "—"
    assert snap.chip("emergency").value == 2


def test_compute_runs_on_an_empty_database():
    snap = situation.compute()
    assert len(snap.chips) == 9
    assert snap.sources, "every source reports, even when it has never run"


# --------------------------------------------------------------------------- #
# Wall display
# --------------------------------------------------------------------------- #
def test_wall_tiles_exist_as_chips():
    from app.pages import wall
    keys = {c.key for c in situation.build_chips({})}
    assert set(wall.TILES) <= keys


def test_wall_panel_rotation_wraps():
    from app.pages import wall
    n = len(wall.PANELS)
    assert [wall.panel_index(i) for i in range(n + 1)] == list(range(n)) + [0]
    assert wall.panel_index(None) == 0


def test_wall_stale_banner_names_stale_sources_only():
    from app.pages import wall
    healthy = situation.Situation(datetime.now(), sources=[SourceStatus("Flood")])
    assert wall.stale_banner(healthy) is None
    stale = situation.Situation(datetime.now(), sources=[
        SourceStatus("Flood"), SourceStatus("Power", age_minutes=34, state="stale")])
    banner = wall.stale_banner(stale)
    assert "Power 34 min ago" in banner and "Flood" not in banner


@pytest.mark.parametrize("builder", ["layout", "console_layout"])
def test_page_layouts_build(builder):
    from dash import Dash

    from app.pages import overview, wall
    Dash(__name__)   # get_asset_url (the brand lockups) needs a current app
    module = wall if builder == "layout" else overview
    assert getattr(module, builder)() is not None


# --------------------------------------------------------------------------- #
# Storm alerts table readability
# --------------------------------------------------------------------------- #
def test_storm_alert_rows_are_humanised():
    import pandas as pd

    from app.pages import storm
    rows = storm.alert_rows(pd.DataFrame([
        {"timestamp": "2026-09-25 05:29:00", "classification": "moderate",
         "alert_type": "new_cell", "message": "CELL-A92C7C MODERATE"},
        {"timestamp": None, "classification": "Strong",
         "alert_type": "escalation", "message": None},
    ]))
    assert rows[0]["timestamp"] == "Fri 25 Sep 05:29"
    assert rows[0]["classification"] == "MODERATE"
    assert rows[0]["alert_type"] == "New cell"
    assert rows[1]["class_key"] == "strong" and rows[1]["alert_type"] == "Intensified"
    assert rows[1]["timestamp"] == "—" and rows[1]["message"] == ""


@pytest.mark.parametrize("dark", [True, False])
def test_storm_alert_table_is_themed_and_left_aligned(dark):
    from app.pages import storm
    table, cell, header, data, conditional, widths = storm.alert_table_styles(dark)
    # The bug: no theme styles at all, so light text sat on a white table.
    assert data.get("backgroundColor") and data.get("color")
    assert cell["textAlign"] == "left" and cell["whiteSpace"] == "normal"
    assert any(r["if"].get("column_id") == "classification" for r in conditional)

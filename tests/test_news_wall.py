"""Newsroom wall (/wall/news): takeover detection, labels, the desk model."""
import json
from datetime import datetime, timedelta

import pytest

from app import shell
from app.pages import news_wall as nw
from app.pages import wall

NOW = datetime(2026, 10, 2, 14, 0, 0)


def entry(id_, severity, minutes_ago=0, lat=-37.8, lon=145.0, lines=None):
    ts = NOW - timedelta(minutes=minutes_ago)
    labels = {3: "Critical", 2: "Major", 1: "Notable", 0: "Info"}
    return {
        "id": id_, "ts": ts, "time": ts.strftime("%H:%M"), "date": ts.date(),
        "hazard": "fire", "hazard_label": "Fire", "kind": "growth",
        "severity": severity, "severity_label": labels[severity],
        "colour": "#e5484d", "headline": f"Story {id_}",
        "entity_name": f"Place {id_}", "latitude": lat, "longitude": lon,
        "url": "/fire", "lines": lines if lines is not None else [f"line {id_}"],
    }


def test_route_is_a_wall_scenario():
    from app.factory import PUBLIC_PAGES, RESTRICTED
    assert nw.PATH == "/wall/news"
    assert nw.PATH in [p for p, _, _ in PUBLIC_PAGES] and nw.PATH not in RESTRICTED
    assert shell.is_wall(nw.PATH)
    assert (nw.PATH, "Newsroom") in wall.SCENARIOS


def test_labels_follow_severity():
    assert nw.takeover_label(entry(1, 3)) == "BREAKING"
    assert nw.takeover_label(entry(1, 2)) == "UPDATE!"
    assert nw.takeover_label(entry(1, 1)) is None
    assert nw.takeover_label(entry(1, 0)) is None


def test_first_snapshot_only_seeds():
    entries = [entry(1, 3), entry(2, 2)]
    seen, fresh = nw.detect_new(None, entries)
    assert sorted(seen) == [1, 2]
    assert fresh == []


def test_new_entries_take_over_most_severe_first():
    seen, _ = nw.detect_new(None, [entry(1, 3)])
    entries = [entry(4, 2, 0), entry(3, 1, 1), entry(2, 3, 5), entry(5, 3, 2), entry(1, 3, 30)]
    seen, fresh = nw.detect_new(seen, entries)
    # Notable (3) never takes over; Critical before Major; newest Critical first.
    assert [e["id"] for e in fresh] == [5, 2, 4]
    assert set(seen) == {1, 2, 3, 4, 5}
    # Seen now: the next pass is quiet.
    assert nw.detect_new(seen, entries)[1] == []


def test_top_story_prefers_severity_then_recency():
    assert nw.top_story([]) is None
    entries = [entry(1, 1, 0), entry(2, 3, 20), entry(3, 3, 10), entry(4, 2, 1)]
    assert nw.top_story(entries)["id"] == 3


def test_crawl_drops_old_entries_and_carries_the_numbers():
    items = nw.crawl_items([entry(1, 2, 10, lines=["214 → 296 ha"]),
                            entry(2, 1, nw.CRAWL_HOURS * 60 + 5)], now=NOW)
    assert len(items) == 1
    assert "FIRE" in items[0] and "Story 1" in items[0] and "214 → 296 ha" in items[0]


def test_story_is_json_for_the_browser(monkeypatch):
    monkeypatch.setattr(nw, "story_map", lambda e, dark: __import__(
        "plotly.graph_objects", fromlist=["Figure"]).Figure())
    s = nw.story(entry(7, 3, lines=["a", "b"]), dark=True)
    json.dumps(s)
    assert s["label"] == "BREAKING" and s["level"] == "critical"
    assert s["hold"] == nw.TAKEOVER[3][1]
    assert s["lines"] == "a\nb"
    assert s["meta"].startswith("FIRE ·")
    u = nw.story(entry(8, 2), dark=False, figure=False)
    assert u["label"] == "UPDATE!" and u["level"] == "major" and u["figure"] is None


def test_story_map_failure_still_gives_a_story(monkeypatch):
    def boom(e, dark):
        raise RuntimeError("no map")
    monkeypatch.setattr(nw, "story_map", boom)
    assert nw.story(entry(9, 3), dark=True)["figure"] is None


@pytest.mark.parametrize("lat", [-37.8, None])
def test_story_map_builds_located_or_not(lat):
    fig = nw.story_map(entry(1, 3, lat=lat, lon=145.0 if lat else None), dark=True)
    json.loads(fig.to_json())


def test_latest_column_excludes_the_top_story():
    entries = [entry(i, 1, i) for i in range(12)]
    rows = nw.latest_block(entries, entries[0])
    assert len(rows) == nw.LATEST_SHOWN
    assert "Story 0" not in str(rows)


def test_layout_has_every_callback_component():
    from dash import Dash
    Dash(__name__)                 # the wall header resolves asset URLs
    text = str(nw.layout())
    for cid in ("nw-refresh", "nw-tick", "nw-seen", "nw-queue", "nw-stage",
                "nw-replay", "nw-takeover", "nw-to-map", "nw-map", "nw-crawl-text",
                "nw-stale", "nw-strip", "nw-bug-status"):
        assert cid in text, cid


def test_stylesheet_hides_shell_ticker_on_the_newsroom():
    css = open("assets/style.css", encoding="utf-8").read()
    assert ".app:has(.nw-page) .ticker" in css
    assert "@keyframes nw-zoom-in" in css

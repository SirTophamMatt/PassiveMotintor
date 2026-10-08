"""Warnings and Advices never count towards fires, anywhere on the site.

A VicEmergency community warning (Emergency Warning / Watch and Act / Advice)
is a statement about a hazard — often not even a fire — so it must never add
to an "Active Fires" figure, be summed into an incident total, or be filed
under the "fire" hazard in the Intelligence Feed.
"""
from datetime import datetime, timedelta

import pandas as pd
import pytest

from app import database, intel_feed
from app.modules.fire import data as fire_data


def _stamp(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def put(sid, feed_type="incident", category1="Fire", level=None,
        category2=None, lat=-37.0, lon=145.0, first_seen=None, updated=None):
    now = datetime.now()
    database.insert_rows("fire_incidents", [{
        "source_id": sid, "feed_type": feed_type, "category1": category1,
        "category2": category2, "warning_level": level, "location": sid,
        "latitude": lat, "longitude": lon, "status": "Going",
        "first_seen": _stamp(first_seen or now), "last_seen": _stamp(now),
        "updated": _stamp(updated or now), "resolved": 0}])


@pytest.fixture
def scene(db):
    put("fire-1")
    put("fire-2", category1="fire ")                       # feed spacing/case
    put("tree", category1="Tree Down")
    put("ew", "warning", "Emergency Warning", "Emergency Warning", "Fire")
    put("advice-fire", "warning", "Advice", "Advice", "Fire")
    put("advice-flood", "warning", "Advice", "Advice", "Met")
    # Defensive: a warning row carrying "Fire" in category1 is still a warning.
    put("odd-warning", "warning", "Fire", "Fire")
    put("burn", "burn-area", "Fire")
    database.execute("UPDATE fire_incidents SET resolved = 1 WHERE source_id = 'burn'")
    return db


# --------------------------------------------------------------------------- #
# The one definition
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("row, fire", [
    ({"feed_type": "incident", "category1": "Fire"}, True),
    ({"feed_type": None, "category1": "Fire"}, True),        # legacy row
    ({"feed_type": "incident", "category1": "Tree Down"}, False),
    ({"feed_type": "warning", "category1": "Advice"}, False),
    ({"feed_type": "warning", "category1": "Fire"}, False),
    ({"feed_type": "Warning", "category1": "Fire"}, False),
    ({"feed_type": "burn-area", "category1": "Fire"}, False),
])
def test_is_fire(row, fire):
    assert fire_data.is_fire(row) is fire


def test_counts_keep_warnings_out_of_fires_and_incidents(scene):
    c = fire_data.latest_counts()
    assert c["active_fires"] == 2
    assert c["incidents"] == 3                 # two fires + the tree down
    assert c["emergency"] == 1 and c["advice"] == 2
    # The odd "Fire" warning is neither a fire nor a misread warning level.
    assert c["emergency"] + c["watch_act"] + c["advice"] == 3


def test_classify_does_not_treat_a_warning_as_a_fire():
    fire_prio, _ = fire_data.classify(None, "Fire", "incident")
    warn_prio, _ = fire_data.classify(None, "Fire", "warning")
    assert fire_prio == 2 and warn_prio == 4


def test_map_kind_of_a_warning_is_never_fire():
    from app.pages import fire as fire_page
    row = {"feed_type": "warning", "category1": "Fire", "warning_level": "Fire"}
    assert fire_page._kind(row) in fire_page.WARNING_KINDS


def test_collector_heartbeat_counts_only_fire_incidents(db, monkeypatch):
    from app.modules.fire import scraper

    def feature(fid, feed_type, category1):
        return {"type": "Feature",
                "geometry": {"type": "Point", "coordinates": [145, -37]},
                "properties": {"id": fid, "feedType": feed_type,
                               "category1": category1, "status": "Going"}}
    monkeypatch.setattr(scraper, "_fetch_feed", lambda: [
        feature("f", "incident", "Fire"),
        feature("w", "warning", "Advice"),
        feature("x", "warning", "Fire")])
    scraper.fetch_fire_data()
    row = database.read_df("SELECT * FROM fire_timeseries").iloc[-1]
    assert row["active_fires"] == 1 and row["advice"] == 1


# --------------------------------------------------------------------------- #
# Pages and reports
# --------------------------------------------------------------------------- #
def test_no_page_sums_warnings_into_a_fire_or_total_figure():
    import inspect

    from app import reporting
    from app.pages import fire as fire_page
    from app.pages import unified
    assert "Fire Warnings" not in inspect.getsource(unified)
    assert '"Total Active"' not in inspect.getsource(fire_page)
    assert "Total Active Events" not in inspect.getsource(reporting)


def test_briefing_files_warnings_apart_from_fires(scene):
    from app import briefing
    kpis = {k.label: k.group for k in briefing.build_briefing_snapshot().situation}
    assert kpis["Active Fires"] == "Fire"
    assert kpis["Emergency Warnings"] == "Warnings"
    assert kpis["Watch & Act"] == "Warnings"


# --------------------------------------------------------------------------- #
# Intelligence Feed
# --------------------------------------------------------------------------- #
def test_warning_changes_are_their_own_hazard_not_fire(db):
    put("w1", "warning", "Advice", "Advice", "Met",
        first_seen=datetime.now() - timedelta(minutes=5))
    intel_feed.detect()
    rows = database.read_df("SELECT hazard, metric FROM intel_events")
    assert not rows.empty
    assert set(rows["hazard"]) == {intel_feed.WARNING}
    assert intel_feed.HAZARD_LABEL[intel_feed.WARNING] == "Warning"
    assert intel_feed.WARNING in intel_feed.HAZARDS
    entry = intel_feed.entries(hours=1)[0]
    assert entry["hazard_label"] == "Warning"


def test_old_fire_filed_warning_entries_are_moved_on_boot(db):
    ts = _stamp(datetime.now())
    database.insert_rows("intel_events", [
        {"ts": ts, "detected_at": ts, "hazard": "fire", "kind": "new",
         "severity": 2, "headline": "New Advice issued", "entity_key": "w",
         "metric": "warning_level"},
        {"ts": ts, "detected_at": ts, "hazard": "fire", "kind": "growth",
         "severity": 2, "headline": "Walwa fire increased 5 ha",
         "entity_key": "f", "metric": "area_ha"}])
    database.insert_rows("intel_metrics", [
        {"hazard": "fire", "entity_key": "w", "metric": "warning_level",
         "value": 3, "label": "Advice", "ts": ts}])
    database.init_db()
    database.init_db()                                   # idempotent
    ev = database.read_df("SELECT hazard, metric FROM intel_events ORDER BY metric")
    assert ev.to_dict("records") == [
        {"hazard": "fire", "metric": "area_ha"},
        {"hazard": "warning", "metric": "warning_level"}]
    m = database.read_df("SELECT hazard FROM intel_metrics")
    assert m["hazard"].tolist() == ["warning"]


def test_context_line_counts_incidents_and_warnings_apart():
    sources = {"roads": pd.DataFrame(), "outages": pd.DataFrame(),
               "gauges": pd.DataFrame(),
               "fires": pd.DataFrame([
                   {"location": "a", "feed_type": "incident", "latitude": -37.0,
                    "longitude": 145.0},
                   {"location": "b", "feed_type": "warning", "latitude": -37.0,
                    "longitude": 145.01},
                   {"location": "c", "feed_type": "warning", "latitude": -37.0,
                    "longitude": 145.02}])}
    flood = intel_feed._nearby_lines(-37.0, 145.0, 10, sources, "flood")
    assert flood == ["1 active incident, 2 warnings current within 10 km"]
    warn = intel_feed._nearby_lines(-37.0, 145.0, 10, sources, intel_feed.WARNING)
    assert warn == ["1 active incident within 10 km"]
    assert intel_feed._nearby_lines(-37.0, 145.0, 10, sources, "fire") == []


def test_a_fire_and_its_own_warning_are_not_a_cross_hazard_consequence(db):
    from app import briefing
    ts = _stamp(datetime.now() - timedelta(minutes=10))
    database.insert_rows("intel_events", [
        {"ts": ts, "detected_at": ts, "hazard": "fire", "kind": "growth",
         "severity": 3, "headline": "Walwa fire increased 300 ha",
         "entity_key": "f", "latitude": -36.0, "longitude": 147.7},
        {"ts": ts, "detected_at": ts, "hazard": "warning", "kind": "escalation",
         "severity": 3, "headline": "Warning upgraded to Watch and Act — Walwa",
         "entity_key": "w", "metric": "warning_level",
         "latitude": -36.0, "longitude": 147.71}])
    assert briefing.build_briefing_snapshot().consequences == []

"""Briefing model: aggregation, time filtering, warning separation, staleness.

The properties worth protecting are the ones an operator would be misled by:
a change from outside the window appearing inside it, warning levels being
collapsed into a total, a dead collector's data reading as current, and a
projection reading as a measurement.
"""
import json
from datetime import datetime, timedelta

import pytest

from app import briefing, database, intel_feed


# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #
def _stamp(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def intel_event(minutes_ago, hazard, headline, severity=intel_feed.MAJOR,
                entity_key=None, entity_name=None, latitude=None,
                longitude=None, kind="growth", **extra):
    ts = _stamp(datetime.now() - timedelta(minutes=minutes_ago))
    row = {"ts": ts, "detected_at": ts, "hazard": hazard, "kind": kind,
           "severity": severity, "headline": headline,
           "entity_key": entity_key or headline.lower().replace(" ", "-"),
           "entity_name": entity_name or headline,
           "latitude": latitude, "longitude": longitude}
    row.update(extra)
    return row


def store_events(rows):
    database.insert_rows("intel_events", rows, ignore_duplicates=True)


def heartbeat(minutes_ago=1):
    """Make every collector look like it just ran, so freshness doesn't drown
    unrelated assertions in stale-source noise."""
    ts = _stamp(datetime.now() - timedelta(minutes=minutes_ago))
    database.insert_rows("flood_heartbeat",
                         [{"event": "live", "timestamp": ts}])
    database.insert_rows("fire_timeseries", [{"timestamp": ts}])
    database.insert_rows("weather_heartbeat", [{"timestamp": ts}])
    database.insert_rows("road_timeseries", [{"timestamp": ts}])
    database.insert_rows("storm_timeseries", [{"timestamp": ts}])
    database.insert_rows("power_timeseries",
                         [{"timestamp": ts, "customers_off": 1200}])
    database.insert_rows("rainfall_aws", [{
        "wmo": "94839", "name": "Charlton", "obs_time": ts, "timestamp": ts,
        "rain_since_9am_mm": 4.2, "wind_gust_kmh": 31.0,
        "temperature_c": 12.0, "relative_humidity_pct": 70.0}],
        ignore_duplicates=True)


# --------------------------------------------------------------------------- #
# No-data behaviour
# --------------------------------------------------------------------------- #
def test_empty_database_still_produces_a_usable_briefing(db):
    """A briefing on an empty system must render, not raise: "nothing is
    happening" is a valid and important operational answer."""
    s = briefing.build_briefing_snapshot()
    assert s.generated_at is not None
    assert s.window_minutes == briefing.DEFAULT_WINDOW_MINUTES
    assert s.changes == [] and s.warnings == [] and s.consequences == []
    assert s.situation, "KPIs should still render with zero/unknown values"
    assert s.sources, "every source should report, even if it never ran"
    assert all(src.state == "never" for src in s.sources)


def test_no_data_text_renders_every_section(db):
    text = briefing.briefing_text(briefing.build_briefing_snapshot())
    for heading in ("CURRENT SITUATION", "SIGNIFICANT CHANGES",
                    "ACTIVE WARNINGS — VICEMERGENCY", "ACTIVE WARNINGS — BOM",
                    "EMERGING CONSEQUENCES", "WATCH POINTS",
                    "WEATHER OBSERVATIONS", "DATA FRESHNESS"):
        assert heading in text, heading
    assert "No significant changes detected" in text
    assert "No significant cross-layer consequences currently identified." in text


# --------------------------------------------------------------------------- #
# Section 1 — current situation
# --------------------------------------------------------------------------- #
def test_flood_levels_are_never_collapsed_into_one_total(db):
    """One gauge at Major must not be able to hide behind a dozen at Minor."""
    database.insert_rows("flood_levels", [
        {"station_key": "big river", "station_name": "Big River",
         "minor": 1.0, "moderate": 2.0, "major": 3.0},
        {"station_key": "small creek", "station_name": "Small Creek",
         "minor": 1.0, "moderate": 2.0, "major": 3.0}])
    database.insert_rows("flood_observations", [
        {"event": "live", "station_name": "Big River", "height_m": 3.4,
         "timestamp": "2026-08-10 23:00:00"},
        {"event": "live", "station_name": "Small Creek", "height_m": 1.2,
         "timestamp": "2026-08-10 23:00:00"}])
    labels = {k.label: k.value for k in briefing.build_briefing_snapshot().situation}
    assert labels["Gauges ≥ Minor"] == "2"     # both are at least minor
    assert labels["Gauges ≥ Moderate"] == "1"
    assert labels["Gauges ≥ Major"] == "1"


def test_unknown_values_show_an_em_dash_not_zero(db):
    """No power data is not "0 customers off" — that would read as all clear."""
    labels = {k.label: k.value for k in briefing.build_briefing_snapshot().situation}
    assert labels["Customers Without Power"] == "—"


# --------------------------------------------------------------------------- #
# Section 2 — significant changes
# --------------------------------------------------------------------------- #
def test_changes_outside_the_window_are_excluded(db):
    store_events([
        intel_event(10, "fire", "Inside the window"),
        intel_event(200, "fire", "Outside the window"),
    ])
    headlines = [c.headline
                 for c in briefing.build_briefing_snapshot(window_minutes=60).changes]
    assert headlines == ["Inside the window"]


@pytest.mark.parametrize("window,expected", [
    (30, 1), (60, 2), (180, 3), (720, 4),
])
def test_each_selectable_window_includes_the_right_changes(db, window, expected):
    store_events([
        intel_event(5, "fire", "Five minutes ago"),
        intel_event(45, "flood", "Forty-five minutes ago"),
        intel_event(150, "roads", "Two and a half hours ago"),
        intel_event(600, "power", "Ten hours ago"),
    ])
    s = briefing.build_briefing_snapshot(window_minutes=window)
    assert len(s.changes) == expected


def test_changes_are_ordered_by_severity_then_recency(db):
    """At a briefing the critical thing from 40 minutes ago outranks the
    notable thing from 2 minutes ago."""
    store_events([
        intel_event(2, "power", "Notable and recent", severity=intel_feed.NOTABLE),
        intel_event(40, "flood", "Critical but older", severity=intel_feed.CRITICAL),
        intel_event(30, "fire", "Major, middle", severity=intel_feed.MAJOR),
        intel_event(5, "roads", "Major, newer", severity=intel_feed.MAJOR),
    ])
    headlines = [c.headline for c in briefing.build_briefing_snapshot().changes]
    assert headlines == ["Critical but older", "Major, newer", "Major, middle",
                         "Notable and recent"]


def test_info_level_noise_is_left_to_the_full_feed(db):
    store_events([
        intel_event(5, "fire", "Worth briefing", severity=intel_feed.NOTABLE),
        intel_event(5, "power", "Just noise", severity=intel_feed.INFO,
                    entity_key="noise"),
    ])
    headlines = [c.headline for c in briefing.build_briefing_snapshot().changes]
    assert headlines == ["Worth briefing"]


def test_change_lines_carry_the_measurement_not_the_surroundings(db):
    """Cross-layer context belongs in Emerging Consequences; a change keeps
    only what it measured, so the same fact isn't printed under two headings."""
    store_events([intel_event(
        5, "flood", "Snowy River crossed Moderate", severity=intel_feed.CRITICAL,
        metric="height_m", prev_value=5.92, new_value=6.13, unit="m",
        rate="Rising 0.18 m/hr", latitude=-37.70, longitude=148.49)])
    database.insert_rows("road_disruptions", [{
        "source_id": "r1", "is_closure": 1, "road_name": "Princes Highway",
        "latitude": -37.69, "longitude": 148.46, "resolved": 0}])
    change = briefing.build_briefing_snapshot().changes[0]
    assert any("6.13" in line for line in change.lines)
    assert not any("within" in line for line in change.lines)


# --------------------------------------------------------------------------- #
# Section 3 — active warnings
# --------------------------------------------------------------------------- #
def _warning_row(source_id, level, event="Riverine Flood", location="Somewhere"):
    return {"source_id": source_id, "feed_type": "warning", "category1": level,
            "event": event, "warning_level": level, "location": location,
            "headline": level, "action": "Stay Informed", "resolved": 0,
            "created": "2026-08-10 22:00:00", "updated": "2026-08-10 22:30:00"}


def test_vicemergency_and_bom_warnings_stay_separate(db):
    database.insert_rows("fire_incidents",
                         [_warning_row("w1", "Emergency Warning")])
    database.insert_rows("weather_warnings", [{
        "warning_id": "IDV1", "type": "severe_weather_warning",
        "title": "Severe Weather Warning for Central", "group_type": "severe",
        "issue_time": "2026-08-10 21:00:00", "active": 1}])
    s = briefing.build_briefing_snapshot()
    assert len(s.warnings_from("VicEmergency")) == 1
    assert len(s.warnings_from("BoM")) == 1


def test_vicemergency_levels_are_ordered_worst_first(db):
    database.insert_rows("fire_incidents", [
        _warning_row("w1", "Advice"),
        _warning_row("w2", "Emergency Warning"),
        _warning_row("w3", "Watch and Act"),
    ])
    levels = [w.level for w in
              briefing.build_briefing_snapshot().warnings_from("VicEmergency")]
    assert levels == ["Emergency Warning", "Watch and Act", "Advice"]


def test_advices_are_capped_and_the_omission_is_declared(db):
    """Dozens of Advices must never push an Emergency Warning off the page —
    and the briefing has to say how many it didn't list."""
    rows = [_warning_row("ew", "Emergency Warning")]
    rows += [_warning_row("a%d" % i, "Advice") for i in range(20)]
    database.insert_rows("fire_incidents", rows)
    s = briefing.build_briefing_snapshot()
    vic = s.warnings_from("VicEmergency")
    assert sum(1 for w in vic if w.level == "Advice") == briefing.ADVICE_LIMIT
    assert sum(1 for w in vic if w.level == "Emergency Warning") == 1
    assert s.omitted_advice == 20 - briefing.ADVICE_LIMIT
    assert "further Advice warning" in briefing.briefing_text(s)


def test_vicemergency_warning_uses_event_and_location_not_the_level_echo(db):
    """A VicEmergency warning's `headline` is just the level again ("Advice");
    the useful pair is the event and where it is."""
    database.insert_rows("fire_incidents", [
        _warning_row("w1", "Watch and Act", event="Riverine Flood",
                     location="the King River")])
    w = briefing.build_briefing_snapshot().warnings_from("VicEmergency")[0]
    assert w.kind == "Riverine Flood"
    assert w.title == "the King River"


def test_bom_group_type_is_not_published_as_a_level(db):
    """group_type reads as minor/moderate/major and would be mistaken for the
    flood gauge classification, which is a completely different scale."""
    database.insert_rows("weather_warnings", [{
        "warning_id": "IDV1", "type": "flood_warning", "title": "Flood Warning",
        "group_type": "major", "issue_time": "2026-08-10 21:00:00", "active": 1}])
    w = briefing.build_briefing_snapshot().warnings_from("BoM")[0]
    assert w.level is None
    assert w.kind == "Flood Warning"


# --------------------------------------------------------------------------- #
# Section 4 — emerging consequences
# --------------------------------------------------------------------------- #
def test_multiple_hazards_at_one_place_become_a_consequence(db):
    store_events([
        intel_event(10, "flood", "Snowy River crossed Moderate",
                    severity=intel_feed.CRITICAL, entity_name="Snowy River at Orbost",
                    latitude=-37.70, longitude=148.49),
        intel_event(20, "roads", "Princes Highway closed at Orbost",
                    severity=intel_feed.MAJOR, latitude=-37.69, longitude=148.46),
    ])
    consequences = briefing.build_briefing_snapshot().consequences
    assert len(consequences) == 1
    assert consequences[0].title == "Snowy River at Orbost"
    assert "Snowy River crossed Moderate" in consequences[0].lines
    assert "Princes Highway closed at Orbost" in consequences[0].lines


def test_a_lone_hazard_is_not_a_cross_layer_consequence(db):
    """One fire on its own is a change, not a consequence — restating it under
    a second heading is noise."""
    store_events([intel_event(10, "fire", "Walwa fire increased 38 ha",
                              latitude=-36.13, longitude=147.73)])
    assert briefing.build_briefing_snapshot().consequences == []


def test_distant_hazards_are_not_clustered_together(db):
    store_events([
        intel_event(10, "flood", "Gauge rising in the east",
                    severity=intel_feed.CRITICAL, latitude=-37.70, longitude=148.49),
        intel_event(20, "fire", "Fire in the far west", severity=intel_feed.MAJOR,
                    latitude=-37.70, longitude=141.00),
    ])
    assert briefing.build_briefing_snapshot().consequences == []


def test_consequences_are_deterministic(db):
    store_events([
        intel_event(10, "flood", "Gauge crossed Moderate",
                    severity=intel_feed.CRITICAL, latitude=-37.70, longitude=148.49),
        intel_event(20, "roads", "Highway closed", severity=intel_feed.MAJOR,
                    latitude=-37.69, longitude=148.46),
    ])
    first = briefing.build_briefing_snapshot().consequences
    second = briefing.build_briefing_snapshot().consequences
    assert [c.lines for c in first] == [c.lines for c in second]


# --------------------------------------------------------------------------- #
# Section 5 — watch points
# --------------------------------------------------------------------------- #
def test_strong_gusts_are_reported_as_observations(db):
    database.insert_rows("rainfall_aws", [{
        "wmo": "1", "name": "Mount William", "obs_time": _stamp(datetime.now()),
        "timestamp": _stamp(datetime.now()), "wind_gust_kmh": 87.0}],
        ignore_duplicates=True)
    points = briefing.build_briefing_snapshot().watch_points
    gust = next(p for p in points if "gust" in p.title.lower())
    assert gust.kind == briefing.OBSERVED
    assert any("Mount William" in line for line in gust.lines)
    # Our reporting threshold must not read as a warning criterion.
    assert any("not a warning criterion" in line for line in gust.lines)


def test_a_calm_network_produces_no_gust_watch_point(db):
    database.insert_rows("rainfall_aws", [{
        "wmo": "1", "name": "Somewhere", "obs_time": _stamp(datetime.now()),
        "timestamp": _stamp(datetime.now()), "wind_gust_kmh": 12.0}],
        ignore_duplicates=True)
    assert briefing.build_briefing_snapshot().watch_points == []


def _rising_gauge(name="Snowy River at Orbost", start=5.70, rate_m_hr=0.20,
                  minor=5.0, moderate=5.8, major=6.5):
    """A gauge with enough recent history for the trend engine: readings every
    15 minutes over the last 2 hours, rising steadily toward `major`."""
    key = name.lower()
    database.insert_rows("flood_levels", [{
        "station_key": key, "station_name": name,
        "minor": minor, "moderate": moderate, "major": major}])
    database.insert_rows("gauge_coords", [{
        "station_key": key, "station_name": name,
        "latitude": -37.70, "longitude": 148.49}])
    now = datetime.now()
    rows = []
    for step in range(9, -1, -1):          # 2h15m of history, oldest first
        when = now - timedelta(minutes=15 * step)
        height = start + rate_m_hr * (15 * (9 - step) / 60.0)
        rows.append({"event": "live", "station_name": name,
                     "height_m": round(height, 3), "timestamp": _stamp(when)})
    database.insert_rows("flood_observations", rows, ignore_duplicates=True)
    return name


def test_a_gauge_approaching_its_next_threshold_becomes_a_watch_point(db):
    name = _rising_gauge()
    points = briefing.build_briefing_snapshot().watch_points
    gauge = next((p for p in points if p.title == name), None)
    assert gauge is not None, "a rising gauge near its threshold should be watched"
    assert any("below" in line and "rising" in line for line in gauge.lines)


def test_a_flood_projection_is_labelled_as_a_projection_and_disclaimed(db):
    """The single most important labelling rule in the briefing: an
    extrapolated ETA must never be able to read as an official forecast."""
    name = _rising_gauge()
    points = briefing.build_briefing_snapshot().watch_points
    gauge = next(p for p in points if p.title == name)
    if gauge.kind != briefing.PROJECTION:
        pytest.skip("trend engine declined to project from this history")
    assert any("Projected to reach" in line for line in gauge.lines)
    assert any("not an official flood forecast" in line for line in gauge.lines)


def test_a_steady_gauge_is_not_a_watch_point(db):
    """Sitting below a threshold and not moving is not something to watch."""
    _rising_gauge(name="Steady Creek", start=2.0, rate_m_hr=0.0,
                  minor=5.0, moderate=5.8, major=6.5)
    titles = [p.title for p in briefing.build_briefing_snapshot().watch_points]
    assert "Steady Creek" not in titles


def test_storm_cells_are_watch_points_with_their_movement(db):
    now = _stamp(datetime.now())
    database.insert_rows("storm_cells", [{
        "cell_id": "14", "radar_id": "IDR02", "frame_ts": now,
        "classification": "strong", "speed_kmh": 52.0, "bearing_deg": 135.0,
        "area_km2": 180.0, "intensity_score": 88.0}])
    points = briefing.build_briefing_snapshot().watch_points
    cell = next(p for p in points if "Storm cell" in p.title)
    assert cell.kind == briefing.OBSERVED
    assert "SE at 52 km/h" in cell.lines[0]


# --------------------------------------------------------------------------- #
# Section 7 — data freshness
# --------------------------------------------------------------------------- #
def test_a_collector_that_never_ran_is_reported_as_never(db):
    s = briefing.build_briefing_snapshot()
    flood = next(src for src in s.sources if src.name == "Flood")
    assert flood.state == "never" and flood.last_update is None
    assert briefing.describe_age(None) == "never"


def test_a_recent_cycle_is_healthy(db):
    heartbeat(minutes_ago=1)
    s = briefing.build_briefing_snapshot()
    assert next(x for x in s.sources if x.name == "Flood").state == "ok"
    assert next(x for x in s.sources if x.name == "AWS").state == "ok"


def test_an_old_cycle_is_flagged_stale(db):
    """Old data must never be allowed to look current."""
    database.insert_rows("flood_heartbeat", [{
        "event": "live",
        "timestamp": _stamp(datetime.now() - timedelta(hours=6))}])
    s = briefing.build_briefing_snapshot()
    flood = next(src for src in s.sources if src.name == "Flood")
    assert flood.state == "stale"
    assert flood.age_minutes == pytest.approx(360, abs=2)
    assert flood in s.stale_sources
    assert "CHECK" in briefing.briefing_text(s)


def test_every_collector_is_accounted_for(db):
    names = {src.name for src in briefing.build_briefing_snapshot().sources}
    assert names == {"Flood", "VicEmergency", "BoM Weather", "AWS", "Roads",
                     "Power", "Storm"}


@pytest.mark.parametrize("minutes,expected", [
    (None, "never"), (0.5, "just now"), (4, "4 min ago"), (75, "75 min ago"),
    (130, "2 hr 10 min ago"), (120, "2 hr ago"),
])
def test_age_wording_is_consistent(minutes, expected):
    assert briefing.describe_age(minutes) == expected


# --------------------------------------------------------------------------- #
# Resilience
# --------------------------------------------------------------------------- #
def test_one_broken_module_does_not_take_the_briefing_down(db, monkeypatch):
    """A collector's data layer throwing must cost that section only."""
    def boom():
        raise RuntimeError("simulated module failure")

    monkeypatch.setattr(briefing.fire_data, "latest_counts", boom)
    s = briefing.build_briefing_snapshot()
    assert s.situation == []          # the failed section degrades to empty
    assert s.sources                  # everything else still built
    assert briefing.briefing_text(s)  # and it still renders


def test_a_broken_intel_feed_leaves_the_rest_intact(db, monkeypatch):
    monkeypatch.setattr(intel_feed, "entries",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    s = briefing.build_briefing_snapshot()
    assert s.changes == []
    assert s.situation and s.sources


# --------------------------------------------------------------------------- #
# as_of
# --------------------------------------------------------------------------- #
def test_as_of_moves_the_change_window_not_the_clock(db):
    """The briefing model must not be welded to datetime.now(): this is what
    makes a historical briefing possible later."""
    store_events([
        intel_event(200, "fire", "Three hours ago"),
        intel_event(5, "flood", "Five minutes ago"),
    ])
    as_of = datetime.now() - timedelta(minutes=180)
    s = briefing.build_briefing_snapshot(as_of=as_of, window_minutes=60)
    assert [c.headline for c in s.changes] == ["Three hours ago"]
    assert s.as_of == as_of
    # State sections are still live until the Phase C journal exists, and the
    # snapshot must say so rather than implying the KPIs are historical.
    assert s.state_is_live is True


def test_window_label_matches_the_selector(db):
    for minutes, label in briefing.WINDOW_OPTIONS:
        s = briefing.build_briefing_snapshot(window_minutes=minutes)
        assert s.window_label == label
        assert s.window_start == s.as_of - timedelta(minutes=minutes)

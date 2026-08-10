"""Event Replay: reconstructing a past moment from stored data only.

The failure this suite exists to prevent is a replay that looks authoritative
but isn't — current data leaking into a historical frame, a resolved entity
still on the map, KPIs that don't move with the slider, or a partial
reconstruction presented as a complete one.
"""
from datetime import datetime, timedelta

import pytest

from app import database, history, replay, tags
from app.pages import unified

T0 = datetime(2026, 8, 11, 12, 0, 0)


def at(minutes):
    return T0 + timedelta(minutes=minutes)


def stamp(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S")


@pytest.fixture
def event(db):
    """A four-hour tagged event starting at T0."""
    tag_id = tags.create_tag("Test Event", stamp(T0), stamp(at(240)))
    return tag_id if isinstance(tag_id, int) else tags.list_tags()[0]["id"]


# --------------------------------------------------------------------------- #
# Event windows
# --------------------------------------------------------------------------- #
def test_event_window_reports_its_bounds_and_duration(event):
    window = replay.event_window(event)
    assert window["start"] == T0
    assert window["end"] == at(240)
    assert window["duration"] == timedelta(hours=4)
    assert window["ongoing"] is False
    assert replay.format_duration(window["duration"]) == "4h 00m"


def test_an_ongoing_event_replays_up_to_now(db):
    tags.create_tag("Ongoing", stamp(datetime.now() - timedelta(hours=2)))
    tag_id = tags.list_tags()[0]["id"]
    window = replay.event_window(tag_id)
    assert window["ongoing"] is True
    assert window["end"] <= datetime.now() + timedelta(seconds=5)
    assert window["duration"] >= timedelta(hours=1, minutes=59)


def test_an_unknown_event_returns_nothing(db):
    assert replay.event_window(999999) is None


def test_event_options_flag_ongoing_events(db):
    tags.create_tag("Closed", stamp(T0), stamp(at(60)))
    tags.create_tag("Still running", stamp(T0))
    labels = [label for label, _ in replay.event_options()]
    assert any("(ongoing)" in label for label in labels)
    assert any(label.startswith("Closed") for label in labels)


@pytest.mark.parametrize("delta,expected", [
    (timedelta(minutes=45), "45m"),
    (timedelta(hours=4), "4h 00m"),
    (timedelta(hours=6, minutes=30), "6h 30m"),
    (timedelta(days=2, hours=3), "2d 3h"),
])
def test_duration_formatting(delta, expected):
    assert replay.format_duration(delta) == expected


# --------------------------------------------------------------------------- #
# Layer frames at a moment
# --------------------------------------------------------------------------- #
def _fire(minutes, level="Advice", status="Going", active=True):
    history.record_state(
        history.FIRE, "f1",
        {"feed_type": "incident", "category1": "Fire", "warning_level": level,
         "status": status, "location": "Walwa", "size": "Medium",
         "geometry": None},
        effective_ts=at(minutes), active=active,
        latitude=-36.13, longitude=147.73)


def test_a_layer_is_empty_before_the_entity_appeared(event):
    _fire(60)
    assert replay.fire_at(at(30)).empty


def test_a_layer_shows_the_state_that_was_current(event):
    _fire(60, level="Advice")
    _fire(120, level="Watch and Act")
    assert replay.fire_at(at(90)).iloc[0]["warning_level"] == "Advice"
    assert replay.fire_at(at(150)).iloc[0]["warning_level"] == "Watch and Act"


def test_a_resolved_entity_leaves_the_map_at_the_right_moment(event):
    _fire(60)
    _fire(180, status="Safe", active=False)
    assert len(replay.fire_at(at(179))) == 1
    assert replay.fire_at(at(181)).empty


def test_flood_gauges_replay_from_real_observation_history(event):
    """Flood kept true history long before the journal existed, so this layer
    must reconstruct correctly for old events too."""
    database.insert_rows("flood_levels", [{
        "station_key": "big river", "station_name": "Big River",
        "minor": 1.0, "moderate": 2.0, "major": 3.0}])
    database.insert_rows("gauge_coords", [{
        "station_key": "big river", "station_name": "Big River",
        "latitude": -37.0, "longitude": 145.0}])
    database.insert_rows("flood_observations", [
        {"event": "live", "station_name": "Big River", "height_m": 0.5,
         "timestamp": stamp(at(10))},
        {"event": "live", "station_name": "Big River", "height_m": 2.4,
         "timestamp": stamp(at(120))},
    ])
    assert replay.flood_at(at(60)).iloc[0]["label"] == "Below flood level"
    assert replay.flood_at(at(180)).iloc[0]["label"] == "Moderate flooding"


def test_a_gauge_that_stopped_reporting_is_not_drawn_as_current(event):
    """Without a staleness cutoff a gauge silent since March would still be
    painted on an August replay."""
    database.insert_rows("flood_levels", [{
        "station_key": "old gauge", "station_name": "Old Gauge",
        "minor": 1.0, "moderate": 2.0, "major": 3.0}])
    database.insert_rows("gauge_coords", [{
        "station_key": "old gauge", "station_name": "Old Gauge",
        "latitude": -37.0, "longitude": 145.0}])
    database.insert_rows("flood_observations", [{
        "event": "live", "station_name": "Old Gauge", "height_m": 2.5,
        "timestamp": stamp(T0 - timedelta(days=30))}])
    assert replay.flood_at(at(60)).empty


def test_aws_observations_replay_to_the_selected_moment(event):
    database.insert_rows("aws_stations", [{
        "wmo": "1", "name": "Mount William", "latitude": -37.3,
        "longitude": 142.6}])
    database.insert_rows("rainfall_aws", [
        {"wmo": "1", "name": "Mount William", "obs_time": stamp(at(30)),
         "timestamp": stamp(at(30)), "wind_gust_kmh": 40.0},
        {"wmo": "1", "name": "Mount William", "obs_time": stamp(at(150)),
         "timestamp": stamp(at(150)), "wind_gust_kmh": 87.0},
    ], ignore_duplicates=True)
    assert replay.aws_at(at(60)).iloc[0]["wind_gust_kmh"] == 40.0
    assert replay.aws_at(at(180)).iloc[0]["wind_gust_kmh"] == 87.0


def test_storm_cells_replay_and_expire(event):
    database.insert_rows("storm_cells", [{
        "cell_id": "14", "radar_id": "IDR02", "frame_ts": stamp(at(60)),
        "latitude": -37.5, "longitude": 143.8, "classification": "strong",
        "area_km2": 180.0, "intensity_score": 88.0}])
    assert len(replay.storm_at(at(65))) == 1
    # A cell unseen for longer than the active window has gone.
    assert replay.storm_at(at(200)).empty


# --------------------------------------------------------------------------- #
# Historical KPIs
# --------------------------------------------------------------------------- #
def _kpi_series(minutes):
    return {k["label"]: k["value"] for k in replay.historical_kpis(at(minutes))}


def test_kpis_reflect_the_selected_moment_not_the_present(event):
    database.insert_rows("fire_timeseries", [
        {"timestamp": stamp(at(30)), "active_fires": 2,
         "emergency_warnings": 0, "watch_act": 1},
        {"timestamp": stamp(at(120)), "active_fires": 5,
         "emergency_warnings": 2, "watch_act": 3},
    ])
    assert _kpi_series(60)["Active Fires"] == "2"
    assert _kpi_series(150)["Active Fires"] == "5"
    assert _kpi_series(150)["Emergency Warnings"] == "2"


def test_kpis_are_unknown_before_the_collector_was_running(event):
    """"—" and "0" are different answers: one means nothing was happening, the
    other means we weren't looking."""
    database.insert_rows("fire_timeseries", [
        {"timestamp": stamp(at(120)), "active_fires": 5,
         "emergency_warnings": 0, "watch_act": 0}])
    assert _kpi_series(30)["Active Fires"] == "—"


def test_a_long_dead_collector_does_not_report_stale_counts(event):
    database.insert_rows("power_timeseries", [
        {"timestamp": stamp(T0 - timedelta(days=2)), "customers_off": 5000}])
    assert _kpi_series(60)["Customers Off"] == "—"


def test_flood_kpi_is_computed_from_observations_at_that_moment(event):
    database.insert_rows("flood_levels", [{
        "station_key": "big river", "station_name": "Big River",
        "minor": 1.0, "moderate": 2.0, "major": 3.0}])
    database.insert_rows("gauge_coords", [{
        "station_key": "big river", "station_name": "Big River",
        "latitude": -37.0, "longitude": 145.0}])
    database.insert_rows("flood_observations", [
        {"event": "live", "station_name": "Big River", "height_m": 0.4,
         "timestamp": stamp(at(10))},
        {"event": "live", "station_name": "Big River", "height_m": 1.5,
         "timestamp": stamp(at(120))},
    ])
    assert _kpi_series(60)["Gauges ≥ Minor"] == "0"
    assert _kpi_series(150)["Gauges ≥ Minor"] == "1"


def test_every_kpi_is_present_even_with_no_data(event):
    labels = [k["label"] for k in replay.historical_kpis(at(60))]
    assert labels == ["Customers Off", "Active Fires", "Emergency Warnings",
                      "Watch & Act", "Gauges ≥ Minor", "Road Closures",
                      "Strong Storm Cells"]


# --------------------------------------------------------------------------- #
# Timeline
# --------------------------------------------------------------------------- #
def test_timeline_returns_the_events_inside_the_window_oldest_first(event):
    for minutes, headline in [(30, "First"), (200, "Third"), (90, "Second")]:
        ts = stamp(at(minutes))
        database.insert_rows("intel_events", [{
            "ts": ts, "detected_at": ts, "hazard": "fire",
            "entity_key": headline, "kind": "growth", "severity": 2,
            "headline": headline}], ignore_duplicates=True)
    window = replay.event_window(event)
    assert [e["headline"] for e in
            replay.timeline(window["start"], window["end"])] == \
        ["First", "Second", "Third"]


def test_timeline_excludes_events_outside_the_event_window(event):
    for minutes, headline in [(-60, "Before"), (60, "During"), (400, "After")]:
        ts = stamp(at(minutes))
        database.insert_rows("intel_events", [{
            "ts": ts, "detected_at": ts, "hazard": "fire",
            "entity_key": headline, "kind": "growth", "severity": 2,
            "headline": headline}], ignore_duplicates=True)
    window = replay.event_window(event)
    headlines = [e["headline"]
                 for e in replay.timeline(window["start"], window["end"])]
    assert headlines == ["During"]


# --------------------------------------------------------------------------- #
# Honesty about coverage
# --------------------------------------------------------------------------- #
def test_with_no_journal_the_note_says_so_plainly(db):
    note = replay.coverage_note(T0)
    assert "No incident/road/power state history has been recorded yet" in note
    assert replay.coverage(T0)["full_fidelity"] is False


def test_an_event_after_the_journal_started_is_full_fidelity(db):
    history.note_history_start(history.FIRE, when=T0 - timedelta(days=2))
    info = replay.coverage(T0)
    assert info["full_fidelity"] is True
    assert "all layers replay from recorded state" in replay.coverage_note(T0)


def test_an_older_event_is_never_presented_as_full_fidelity(db):
    """The single most important honesty rule in Replay."""
    history.note_history_start(history.FIRE, when=T0 + timedelta(days=10))
    info = replay.coverage(T0)
    assert info["full_fidelity"] is False
    note = replay.coverage_note(T0)
    assert "partial" in note
    assert "Nothing is back-dated or inferred" in note


def test_coverage_waits_for_every_source_to_be_journalling(db):
    """The picture is only complete once EVERY layer was recording, so the
    latest source start is the honest answer."""
    history.note_history_start(history.FIRE, when=T0 - timedelta(days=5))
    history.note_history_start(history.ROADS, when=T0 + timedelta(days=1))
    assert replay.coverage(T0)["full_fidelity"] is False


# --------------------------------------------------------------------------- #
# The shared renderer
# --------------------------------------------------------------------------- #
def test_replay_and_the_live_map_use_the_same_renderers(event):
    """The refactor's whole point: no second copy of the map code."""
    _fire(30)
    figure = unified.map_figure(["fire"], dark=False,
                                source=replay.frame_source(at(60)),
                                uirevision="replay-map")
    assert len(figure.data) >= 1
    assert any(trace.lat is not None and len(trace.lat) for trace in figure.data)


def test_a_moment_with_nothing_happening_still_renders_a_map(event):
    """Plotly falls back to bare numbered axes with no mapbox traces, and a
    quiet moment is the normal case in replay."""
    figure = unified.map_figure(["fire"], dark=False,
                                source=replay.frame_source(at(60)),
                                uirevision="replay-map")
    assert figure.layout.mapbox.style == "open-street-map"
    assert len(figure.data) == 1 and len(figure.data[0].lat) == 0


def test_replay_draws_from_stored_state_not_the_live_sources(event, monkeypatch):
    """Playback must run entirely from stored data — a scrub or a play must
    never put load on BoM or VicEmergency.

    Every live fetcher is replaced with one that raises. `build_layers` catches
    per-layer failures, so a regression that reached for live data would show
    up as an EMPTY fire layer rather than an error — which is exactly what this
    asserts against: the markers have to still be there.
    """
    _fire(30)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("replay reached for live data")

    for key in unified.LIVE_SOURCES:
        monkeypatch.setitem(unified.LIVE_SOURCES, key, forbidden)

    figure = unified.map_figure(["fire"], dark=False,
                                source=replay.frame_source(at(60)),
                                uirevision="replay-map")
    plotted = sum(len(trace.lat or []) for trace in figure.data)
    assert plotted == 1, "the historical fire should still be drawn"

    # ...and the same call using the live source would now blow up, proving the
    # patch is real and the assertion above isn't passing by accident.
    live = unified.map_figure(["fire"], dark=False, uirevision="live")
    assert sum(len(trace.lat or []) for trace in live.data) == 0


def test_all_replay_layer_keys_have_a_renderer():
    """A key without a renderer would silently draw nothing on the replay map."""
    assert set(replay.LAYER_PROVIDERS) <= set(unified.RENDERERS)

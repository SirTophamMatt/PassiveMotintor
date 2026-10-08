"""The 2026-10-08 /admin/cpu hotspots: shared latest-reading cache and the
batched intel metric lookup."""
from app import database, intel_feed
from app.modules.flood import data as flood_data


def _obs(station, height, ts):
    database.insert_rows("flood_observations", [{
        "event": "live", "station_name": station, "height_m": height,
        "timestamp": ts, "catchment": "C"}])


def test_latest_readings_refreshes_on_new_reading(db):
    _obs("A River at X", 1.0, "2026-10-08 10:00:00")
    first = flood_data.latest_readings()
    assert first.set_index("station_name")["height_m"]["A River at X"] == 1.0
    _obs("A River at X", 2.5, "2026-10-08 10:15:00")
    again = flood_data.latest_readings()
    assert again.set_index("station_name")["height_m"]["A River at X"] == 2.5
    assert again.set_index("station_name")["ts"]["A River at X"] == \
        "2026-10-08 10:15:00"


def test_latest_readings_cached_until_new_reading(db, monkeypatch):
    _obs("B Creek at Y", 1.0, "2026-10-08 10:00:00")
    flood_data.latest_readings()
    calls = []
    real = database.read_df
    monkeypatch.setattr(database, "read_df",
                        lambda q, *a, **k: calls.append(q) or real(q, *a, **k))
    flood_data.latest_readings()
    assert not any("GROUP BY station_name" in q for q in calls)


def test_latest_readings_returns_a_copy(db):
    _obs("C River at Z", 1.0, "2026-10-08 10:00:00")
    df = flood_data.latest_readings()
    df["height_m"] = 99
    assert flood_data.latest_readings()["height_m"].iloc[0] == 1.0


def test_metric_prev_map_matches_single_lookup(db):
    for ts, val, lab in [("2026-10-08 10:00:00", 1, "Closed"),
                         ("2026-10-08 11:00:00", 0, "Open")]:
        database.insert_rows("intel_metrics", [{
            "hazard": "roads", "entity_key": "r1", "metric": "closed",
            "value": val, "label": lab, "ts": ts}])
    database.insert_rows("intel_metrics", [{
        "hazard": "roads", "entity_key": "r2", "metric": "closed",
        "value": 1, "label": None, "ts": "2026-10-08 09:00:00"}])
    m = intel_feed._metric_prev_map("roads", "closed")
    assert m["r1"] == intel_feed._metric_prev("roads", "r1", "closed")
    assert m["r1"][:2] == (0, "Open")
    assert m["r2"][1] is None          # NULL label stays None, never NaN


def test_record_with_prev_skips_unchanged(db):
    intel_feed._metric_record("roads", "r1", "closed", 1, "Closed")
    prev = intel_feed._metric_prev_map("roads", "closed")["r1"]
    assert intel_feed._metric_record("roads", "r1", "closed", 1, "Closed",
                                     prev=prev) is False
    assert intel_feed._metric_record("roads", "r1", "closed", 0, "Open",
                                     prev=prev) is True


def test_roads_detector_close_then_reopen(db):
    """End to end through the batched lookup: a seen-open road closing and
    reopening writes one metric row per change and one feed entry each."""
    from datetime import timedelta
    from app.config import load_config
    now = intel_feed._now()
    stamp = intel_feed._stamp
    database.insert_rows("road_disruptions", [{
        "source_id": "d1", "road_name": "Test Rd", "is_closure": 0,
        "first_seen": stamp(now - timedelta(minutes=30)),
        "last_seen": stamp(now - timedelta(minutes=20)), "resolved": 0}])
    cfg = load_config()
    cutoff = now - timedelta(minutes=180)
    intel_feed._detect_roads(cfg, cutoff)          # seeds "Open"
    intel_feed._detect_roads(cfg, cutoff)          # unchanged: no new row
    with database.get_connection() as c:
        c.execute("UPDATE road_disruptions SET is_closure = 1, last_seen = ?",
                  [stamp(now - timedelta(minutes=10))])
    intel_feed._detect_roads(cfg, cutoff)
    with database.get_connection() as c:
        c.execute("UPDATE road_disruptions SET resolved = 1, last_seen = ?",
                  [stamp(now)])
    intel_feed._detect_roads(cfg, cutoff)
    metrics = database.read_df(
        "SELECT label FROM intel_metrics WHERE hazard = 'roads' ORDER BY id")
    assert metrics["label"].tolist() == ["Open", "Closed", "Open"]
    events = database.read_df(
        "SELECT kind FROM intel_events WHERE hazard = 'roads' ORDER BY ts")
    assert events["kind"].tolist() == ["new", "cleared"]

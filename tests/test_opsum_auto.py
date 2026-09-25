"""Operational Summary auto-fill: the deck's clocks (as at 0830 / 24 h to
0600), journal reconstruction vs live, refusals instead of substitutes, each
provider's output shape, merge/apply rules, and the external parsers."""
import json
from datetime import datetime, timedelta

import pytest

from app import database, history, opsum, opsum_auto, opsum_sources
from app.config import load_config

D = "2026-09-26"
NOW = datetime(2026, 9, 26, 9, 15)          # summary built at 09:15
TS = "%Y-%m-%d %H:%M:%S"


def _fire_row(sid, feed_type, cat1, status="Going", cat2=None, created=None,
              resolved=0, first_seen=None):
    return {"source_id": sid, "feed_type": feed_type, "category1": cat1,
            "category2": cat2, "status": status, "resolved": resolved,
            "warning_level": cat1 if feed_type == "warning" else None,
            "created": created, "first_seen": first_seen or created,
            "last_seen": NOW.strftime(TS)}


def _journal(sid, when, state, active=True):
    history.record_state(history.FIRE, sid, state, effective_ts=when, active=active)


@pytest.fixture
def fire(db):
    database.insert_rows("fire_timeseries", [{"timestamp": NOW.strftime(TS), "total_active": 1}])
    history.note_history_start(history.FIRE, datetime(2026, 9, 20))
    # 07:00 a Watch and Act appears; at 09:00 it is upgraded to Emergency Warning.
    _journal("w1", datetime(2026, 9, 26, 7), {"feed_type": "warning", "category1": "Watch and Act"})
    _journal("w1", datetime(2026, 9, 26, 9), {"feed_type": "warning", "category1": "Emergency Warning"})
    _journal("w2", datetime(2026, 9, 26, 8), {"feed_type": "warning", "category1": "Advice"})
    _journal("w3", datetime(2026, 9, 26, 8), {"feed_type": "warning",
                                              "category1": "Community Information"})
    # A going fire at 08:00, resolved at 08:20 (tombstone) -> not going at 08:30.
    _journal("f1", datetime(2026, 9, 26, 8), {"feed_type": "incident", "category1": "Fire",
                                              "status": "Going"})
    _journal("f1", datetime(2026, 9, 26, 8, 20), {"feed_type": "incident", "category1": "Fire",
                                                  "status": "Safe"}, active=False)
    _journal("f2", datetime(2026, 9, 26, 6), {"feed_type": "incident", "category1": "Fire",
                                              "status": "Going"})
    database.insert_rows("fire_incidents", [
        _fire_row("a", "incident", "Fire", cat2="Grass Fire", created="2026-09-25 10:00:00"),
        _fire_row("b", "incident", "Fire", cat2="Bushfire", created="2026-09-26 05:59:59"),
        _fire_row("c", "incident", "Fire", cat2="Structure Fire", created="2026-09-26 01:00:00"),
        _fire_row("d", "incident", "Fire", cat2="Non-Structure Fire", created="2026-09-26 02:00:00"),
        # outside the window (before 06:00 yesterday / after 06:00 today)
        _fire_row("e", "incident", "Fire", cat2="Grass Fire", created="2026-09-25 05:00:00"),
        _fire_row("f", "incident", "Fire", cat2="Grass Fire", created="2026-09-26 07:00:00"),
        # not fires / not incidents
        _fire_row("g", "incident", "Tree Down", cat2="Grass", created="2026-09-26 01:00:00"),
        _fire_row("h", "warning", "Advice", created="2026-09-26 01:00:00"),
    ], ignore_duplicates=True)
    return db


def test_context_clamps_to_now():
    ctx = opsum_auto.context(D, now=datetime(2026, 9, 26, 7, 0))
    assert ctx.snap_live and ctx.snap == datetime(2026, 9, 26, 7, 0)
    assert not ctx.cutoff_partial and ctx.cutoff == datetime(2026, 9, 26, 6, 0)
    assert ctx.window_start == datetime(2026, 9, 25, 6, 0)
    ctx = opsum_auto.context(D, now=NOW)
    assert not ctx.snap_live and ctx.snap == datetime(2026, 9, 26, 8, 30)


def test_warnings_reconstructed_as_at_0830_not_now(fire):
    sugg, misses = opsum_auto.suggest(D, now=NOW, external=False)
    # w1 was Watch and Act at 08:30 even though it is an Emergency Warning now.
    assert sugg["warnings"].value == [[0, 1, 1, 1]]
    assert "state journal" in sugg["warnings"].source
    assert sugg["warnings"].as_at.startswith("08:30")


def test_going_fires_and_24h_totals(fire):
    sugg, _ = opsum_auto.suggest(D, now=NOW, external=False)
    # f2 going; f1 resolved before 08:30. Grass/bush a+b, structure c; d is
    # non-structure, e/f outside 06:00-06:00, g not a fire.
    assert sugg["ops_incidents"].value == [["1", "2", "1", None, None]]
    assert "not published" in sugg["ops_incidents"].note


def test_journal_gap_is_refused_not_substituted(db):
    database.insert_rows("fire_timeseries", [{"timestamp": NOW.strftime(TS)}])
    history.note_history_start(history.FIRE, datetime(2026, 9, 26, 9, 0))   # after 08:30
    sugg, misses = opsum_auto.suggest(D, now=NOW, external=False)
    assert "warnings" not in sugg
    assert "does not reach back" in misses["VicEmergency warnings / going fires"]


def test_before_0830_uses_live_state(db):
    database.insert_rows("fire_timeseries", [{"timestamp": "2026-09-26 07:55:00"}])
    database.insert_rows("fire_incidents", [
        _fire_row("x", "warning", "Emergency Warning"),
        _fire_row("y", "incident", "Fire", status="Going"),
        _fire_row("z", "incident", "Fire", status="Going", resolved=1)])
    sugg, _ = opsum_auto.suggest(D, now=datetime(2026, 9, 26, 8, 0), external=False)
    assert sugg["warnings"].value == [[1, 0, 0, 0]]
    assert sugg["ops_incidents"].value[0][0] == "1"
    assert "not reached yet" in sugg["warnings"].note


def test_no_collector_data_is_a_miss(db):
    _, misses = opsum_auto.suggest(D, now=NOW, external=False)
    assert "no data" in misses["VicEmergency warnings / going fires"]


def test_fire_kind():
    assert opsum_auto.fire_kind("Grass Fire") == "grass"
    assert opsum_auto.fire_kind("Bushfire") == "grass"
    assert opsum_auto.fire_kind("Structure Fire") == "structure"
    assert opsum_auto.fire_kind("Non-Structure Fire") is None
    assert opsum_auto.fire_kind(float("nan")) is None


def test_bom_warnings_grouped_by_type(db):
    database.insert_rows("weather_warnings", [
        {"warning_id": "A", "type": "marine_wind_warning", "title": "Strong Wind Warning for West Coast",
         "short_title": "Strong Wind Warning", "group_type": "minor", "issue_time": "2026-09-26 05:00:00",
         "active": 1},
        {"warning_id": "B", "type": "flood_warning", "title": "Minor Flood Warning for the Ovens River",
         "short_title": None, "group_type": "minor", "issue_time": "2026-09-26 06:00:00", "active": 1},
        {"warning_id": "C", "type": "flood_warning", "title": "Old", "group_type": "minor",
         "issue_time": "2026-09-20 06:00:00", "active": 0},
    ])
    sugg, _ = opsum_auto.suggest(D, now=NOW, external=False)
    text = sugg["bom_warnings"].value
    lines = text.split("\n")
    assert sum(1 for l in lines if l.startswith("# ")) == 2
    assert "Minor Flood Warning for the Ovens River" in lines
    assert "Strong Wind Warning" in lines and "Old" not in text


def test_bom_warnings_nil(db):
    sugg, _ = opsum_auto.suggest(D, now=NOW, external=False)
    assert sugg["bom_warnings"].value == "Nil"


def test_roads_summary(db):
    database.insert_rows("road_timeseries", [{"timestamp": NOW.strftime(TS), "total_active": 3}])
    database.insert_rows("road_disruptions", [
        {"source_id": "1", "is_closure": 1, "road_name": "Great Alpine Rd", "lga": "Alpine",
         "disruption_type": "Flooding, Water over road", "resolved": 0},
        {"source_id": "2", "is_closure": 0, "road_name": "Hume Fwy", "resolved": 0},
        {"source_id": "3", "is_closure": 1, "road_name": "Gone", "resolved": 1},
    ])
    sugg, _ = opsum_auto.suggest(D, now=NOW, external=False)
    v = sugg["road_network"].value
    assert v.startswith("**1 full road closure** and 1 other unplanned disruption statewide.")
    assert "  Great Alpine Rd, Alpine — Flooding" in v and "Gone" not in v


def test_roads_without_collector_is_a_miss(db):
    _, misses = opsum_auto.suggest(D, now=NOW, external=False)
    assert "VicRoads" in misses["Road network"]


def test_power_significant_and_stale(db):
    database.insert_rows("power_timeseries", [{"timestamp": "2026-09-26 09:00:00",
                                               "customers_off": 5400}])
    database.insert_rows("power_outages", [
        {"location": "Bright", "customers_off": 4200, "restored": 0},
        {"location": "Tiny", "customers_off": 12, "restored": 0}])
    sugg, _ = opsum_auto.suggest(D, now=NOW, external=False)
    v = sugg["power_disruptions"].value
    assert v.split("\n") == ["5,400 customers off supply statewide.", "  Bright — 4,200 customers"]
    # stale data is refused rather than reported as current
    _, misses = opsum_auto.suggest(D, now=NOW + timedelta(hours=5), external=False)
    assert "stale" in misses["Power disruptions"]


def test_power_nil_significant(db):
    database.insert_rows("power_timeseries", [{"timestamp": "2026-09-26 09:00:00",
                                               "customers_off": 40}])
    sugg, _ = opsum_auto.suggest(D, now=NOW, external=False)
    assert sugg["power_disruptions"].value.startswith("Nil significant (40 customers")


def _obs(name, ts, h):
    return {"event": "live", "station_name": name, "timestamp": ts, "height_m": h}


def test_flood_snapshot(db):
    database.insert_rows("flood_levels", [
        {"station_key": "murray river at barham", "minor": 5.5, "moderate": 6.0, "major": 6.5},
        {"station_key": "ovens river at wangaratta", "minor": 11.9, "moderate": 12.6, "major": 13.1},
        {"station_key": "dry creek at nowhere", "minor": 3.0, "moderate": 4.0, "major": 5.0},
        {"station_key": "old river at silent", "minor": 1.0, "moderate": 2.0, "major": 3.0},
    ])
    rows = []
    for i in range(6):                      # Wangaratta rising steadily to Major
        ts = (NOW - timedelta(minutes=150 - 30 * i)).strftime(TS)
        rows.append(_obs("Ovens River at Wangaratta", ts, 12.9 + 0.1 * i))
    rows += [_obs("Murray River at Barham", "2026-09-26 09:00:00", 5.63),
             _obs("Dry Creek at Nowhere", "2026-09-26 09:00:00", 1.0),
             _obs("Old River at Silent", "2026-09-20 09:00:00", 9.0)]   # stale
    database.insert_rows("flood_observations", rows)
    sugg, _ = opsum_auto.suggest(D, now=NOW, external=False)
    g = sugg["flood_gauges"].value
    assert g[0][:2] == ["Wangaratta", "Barham"]
    assert g[1][:2] == ["13.40 m", "5.63 m"]
    assert g[2][:2] == ["MAJOR", "MINOR"]
    assert g[3][0] == "Rising"
    assert all(g[i][2] == "" for i in range(5))           # unused columns cleared
    assert "2 gauge(s)" in sugg["flood_gauges"].note
    assert sugg["flood_overview_date"].value == "26 SEPTEMBER 2026"


def test_merge_and_apply_rules():
    data = opsum.blank(D)
    data["fields"]["ops_incidents"] = [["", "9", "", "", "7"]]
    data["fields"]["kp_today"] = "typed"
    sugg = {
        "ops_incidents": opsum_auto._merge(
            opsum_auto.Suggestion("ops_incidents", [["1", None, None, None, None]], "A", "08:30"),
            opsum_auto.Suggestion("ops_incidents", [[None, "2", "3", None, None]], "B", "06:00")),
        "kp_today": opsum_auto.Suggestion("kp_today", "auto", "X"),
        "earthquakes": opsum_auto.Suggestion("earthquakes", "Nil", "GA"),
    }
    assert sugg["ops_incidents"].source == "A; B"
    changed = opsum_auto.apply(data, sugg)
    assert data["fields"]["ops_incidents"] == [["1", "9", "3", "", "7"]]   # typed cells kept
    assert data["fields"]["kp_today"] == "typed"
    assert data["fields"]["earthquakes"] == "Nil"
    assert set(changed) == {"ops_incidents", "earthquakes"}
    opsum_auto.apply(data, sugg, overwrite=True)
    assert data["fields"]["ops_incidents"] == [["1", "2", "3", "", "7"]]
    assert data["fields"]["kp_today"] == "auto"


def test_every_auto_key_is_a_field():
    assert set(opsum_auto.AUTO_KEYS) <= set(opsum.FIELDS)


def test_one_broken_provider_costs_only_its_fields(db, monkeypatch):
    def boom(ctx):
        raise RuntimeError("kaput")
    monkeypatch.setattr(opsum_auto, "INTERNAL",
                        [("Broken", boom, ())] + opsum_auto.INTERNAL)
    sugg, misses = opsum_auto.suggest(D, now=NOW, external=False)
    assert misses["Broken"] == "RuntimeError: kaput"
    assert "bom_warnings" in sugg


# --------------------------------------------------------------------------- #
# External parsers
# --------------------------------------------------------------------------- #
STATE_HTML = """<html><body><div id="content">
<h1>Victorian Forecast</h1>
<p class="date">Issued at 4:40 am EST on Saturday 26 September 2026.</p>
<h2>Weather Situation</h2>
<p>A strong high pressure system over western Victoria will move slowly east.</p>
<div class="day main"><h2>Forecast for the rest of Saturday 26 September</h2>
<p>Areas of morning fog and frost inland.</p><p>Mostly sunny.</p></div>
<div class="day"><h2>Forecast for Sunday 27 September</h2><p>Windy.</p></div>
</div></body></html>"""


def test_parse_state_forecast():
    f = opsum_sources.parse_state_forecast(STATE_HTML)
    assert f["situation"].startswith("A strong high pressure")
    assert f["forecast"] == "Areas of morning fog and frost inland.\nMostly sunny."
    assert f["forecast_heading"].startswith("Forecast for the rest of Saturday")


def test_parse_state_forecast_nothing_recognised():
    assert opsum_sources.parse_state_forecast("<html><p>maintenance</p></html>") == {}


def _quake(lon, lat, t, mag, desc="10 km SW of Moe, VIC", depth=8):
    return {"type": "Feature", "geometry": {"type": "Point", "coordinates": [lon, lat]},
            "properties": {"epicentral_time": t, "preferred_magnitude": mag,
                           "description": desc, "depth": depth}}


def test_parse_quakes_filters_region_time_and_magnitude():
    now = datetime(2026, 9, 26, 9, 0)
    payload = json.dumps({"type": "FeatureCollection", "features": [
        _quake(146.2, -38.2, "2026-09-26T08:00:00", 3.1),                  # keep
        _quake(146.2, -38.2, "2026-09-25T10:00:00", 2.6, desc="older"),    # keep
        _quake(146.2, -38.2, "2026-09-26T08:00:00", 1.9, desc="small"),    # too small
        _quake(146.2, -38.2, "2026-09-23T08:00:00", 4.0, desc="old"),      # > 48 h
        _quake(131.0, -25.0, "2026-09-26T08:00:00", 5.0, desc="NT"),       # not VIC
        {"type": "Feature", "geometry": None, "properties": {}},           # junk
    ]})
    out = opsum_sources.parse_quakes(payload, now, hours=48, min_mag=2.5)
    assert [q["desc"] for q in out] == ["10 km SW of Moe, VIC", "older"]
    assert out[0]["mag"] == 3.1 and out[0]["depth"] == 8.0


def test_parse_quakes_handles_utc_and_other_keys():
    now = datetime.now()
    t = (datetime.now().astimezone() - timedelta(hours=1)).astimezone(
        __import__("datetime").timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    payload = {"features": [{"geometry": {"type": "Point", "coordinates": [144.9, -37.8]},
                             "properties": {"origin_time": t, "magnitude": "2.7",
                                            "place": "Melbourne"}}]}
    out = opsum_sources.parse_quakes(payload, now)
    assert out and out[0]["desc"] == "Melbourne" and out[0]["depth"] is None


def test_parse_quakes_rejects_non_feature_payload():
    with pytest.raises(ValueError):
        opsum_sources.parse_quakes("{}", datetime.now())


def test_external_failure_is_reported_and_sampled(db, monkeypatch, tmp_path):
    monkeypatch.setattr(opsum_sources, "BASE_DIR", str(tmp_path))
    monkeypatch.setattr(opsum_sources, "_fetch", lambda url, timeout: b"<html>nope</html>")
    _, misses = opsum_auto.suggest(D, now=NOW, external=True)
    assert "sample saved" in misses["BoM state forecast"]
    assert (tmp_path / "opsum_debug_bom_forecast.html").exists()
    assert "not understood" in misses["GA earthquakes"]


def test_external_success_fills_fields(db, monkeypatch):
    def fake(url, timeout):
        if "bom" in url:
            return STATE_HTML.encode()
        return json.dumps({"features": []}).encode()
    monkeypatch.setattr(opsum_sources, "_fetch", fake)
    sugg, _ = opsum_auto.suggest(D, now=NOW, external=True)
    assert sugg["wx_situation"].value.startswith("A strong high")
    assert sugg["earthquakes"].value == "Nil"
    assert "2.5+" in sugg["earthquakes"].note


def test_unreachable_source_message(monkeypatch):
    opsum_sources._cache.clear()
    with pytest.raises(opsum_sources.SourceError, match="could not reach"):
        opsum_sources._fetch("http://127.0.0.1:9/x", 1)


def test_config_defaults_present():
    o = load_config()["opsum"]
    assert o["snapshot_time"] == "08:30" and o["stats_cutoff"] == "06:00"


def test_unapplied_lists_only_real_differences():
    data = opsum.blank(D)
    data["fields"]["bom_warnings"] = "Nil"
    data["fields"]["power_disruptions"] = "typed over"
    data["fields"]["warnings"] = [["0", "1", "", ""]]
    sugg = {"bom_warnings": opsum_auto.Suggestion("bom_warnings", "Nil", "x"),
            "power_disruptions": opsum_auto.Suggestion("power_disruptions", "Nil significant", "x"),
            "warnings": opsum_auto.Suggestion("warnings", [["0", "1", None, None]], "x")}
    assert opsum_auto.unapplied(data, sugg) == ["power_disruptions"]

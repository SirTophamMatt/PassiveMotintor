"""Power outage IDs: one ID per EM-COP outage, stable across cycles, with its
localities linked (app/modules/power/outages.py) — and every consumer counting
an outage once instead of once per suburb."""
import json
import re
from datetime import datetime, timedelta

import pandas as pd
import pytest

from app import database, history, localities
from app.modules.power import data as power_data
from app.modules.power import outages

T0 = datetime(2026, 11, 4, 10, 0)


def _row(locations, customers=900, typ="Unplanned", first="2026-11-04 09:00:00"):
    return {"locations": list(locations), "customers_off": customers, "type": typ,
            "first_seen": first, "cells": [", ".join(locations), "", str(customers),
                                           "", typ, first[11:16]]}


def _events():
    return database.read_df("SELECT * FROM power_outage_events ORDER BY id")


def _locs(ref=None, active_only=False):
    q = "SELECT * FROM power_outages WHERE 1=1"
    args = []
    if ref:
        q += " AND outage_ref = ?"
        args.append(ref)
    if active_only:
        q += " AND restored = 0"
    return database.read_df(q + " ORDER BY id", args)


def _ref_of(location, active=True):
    df = _locs(active_only=active)
    return list(df[df["location"] == location]["outage_ref"])


def _square(lon, lat, d=0.02):
    return {"type": "Polygon", "coordinates": [[[lon, lat], [lon + d, lat],
                                                [lon + d, lat + d], [lon, lat + d],
                                                [lon, lat]]]}


def _load_boundaries(*items):
    feats = [{"type": "Feature", "properties": {"SAL_NAME_2021": name,
                                                "STATE_CODE_2021": "2"},
              "geometry": _square(lon, lat)} for name, lon, lat in items]
    localities.load_features(feats, "test", force=True)


# --------------------------------------------------------------------------- #
# References + parsing
# --------------------------------------------------------------------------- #
def test_reference_shape_and_alphabet():
    ref = outages.make_ref("2026-11-04 09:00:00")
    assert re.fullmatch(r"PWR-261104-[A-HJ-NP-Z2-9]{4}", ref)
    assert not set(ref[-4:]) & set("IO01")


def test_split_locations_dedupes_and_keeps_order():
    assert outages.split_locations(" BELGRAVE,TECOMA , belgrave,\xa0UPWEY,, ") == [
        "BELGRAVE", "TECOMA", "UPWEY"]


def test_parse_row_uses_full_title_list():
    from app.modules.power import scraper
    r = scraper.parse_row(["BELGRAVE, TECOMA +1", "AusNet", "1,234", "x",
                           "Unplanned", "09:00"], "BELGRAVE, TECOMA, UPWEY",
                          {"id": "r1"})
    assert r["locations"] == ["BELGRAVE", "TECOMA", "UPWEY"]
    assert r["customers_off"] == 1234 and r["type"] == "Unplanned"
    assert r["cells"][1] == "AusNet" and r["attrs"] == {"id": "r1"}
    assert scraper.parse_row(["too", "short"]) is None


def test_label():
    assert outages.label(["BELGRAVE"]) == "Belgrave"
    assert outages.label(["BELGRAVE", "TECOMA"]) == "Belgrave & Tecoma"
    assert outages.label(["A", "B", "C"]) == "A, B, C"
    assert outages.label(["A", "B", "C", "D", "E"]) == "A, B +3"


# --------------------------------------------------------------------------- #
# Identity across cycles
# --------------------------------------------------------------------------- #
def test_one_outage_one_id_with_linked_localities(db):
    counts = outages.sync([_row(["BELGRAVE", "TECOMA", "UPWEY"])], now=T0)
    assert counts["new"] == 1
    ev = _events()
    assert len(ev) == 1
    ref = ev.iloc[0]["outage_ref"]
    rows = _locs(ref)
    assert list(rows["location"]) == ["BELGRAVE", "TECOMA", "UPWEY"]
    assert set(rows["customers_off"]) == {900}
    assert json.loads(ev.iloc[0]["locations_json"]) == ["BELGRAVE", "TECOMA", "UPWEY"]


def test_id_holds_while_customers_fall_and_list_shrinks(db):
    outages.sync([_row(["BELGRAVE", "TECOMA", "UPWEY"], 900)], now=T0)
    ref = _events().iloc[0]["outage_ref"]
    outages.sync([_row(["BELGRAVE", "UPWEY"], 300)], now=T0 + timedelta(minutes=1))
    ev = _events()
    assert len(ev) == 1 and ev.iloc[0]["outage_ref"] == ref
    assert ev.iloc[0]["customers_off"] == 300 and ev.iloc[0]["peak_customers"] == 900
    assert json.loads(ev.iloc[0]["all_locations_json"]) == ["BELGRAVE", "TECOMA", "UPWEY"]
    tecoma = _locs(ref)[lambda d: d["location"] == "TECOMA"].iloc[0]
    assert tecoma["restored"] == 1 and tecoma["duration_mins"] > 0
    assert set(_locs(ref, active_only=True)["location"]) == {"BELGRAVE", "UPWEY"}


def test_first_seen_read_differently_still_same_outage(db):
    outages.sync([_row(["A", "B", "C"], first="2026-11-04 09:00:00")], now=T0)
    ref = _events().iloc[0]["outage_ref"]
    outages.sync([_row(["A", "B", "C"], first="2026-11-04 10:01:00")],
                 now=T0 + timedelta(minutes=1))
    assert list(_events()["outage_ref"]) == [ref]


def test_type_change_is_a_different_outage(db):
    outages.sync([_row(["A"], typ="Planned")], now=T0)
    outages.sync([_row(["A"], typ="Unplanned")], now=T0 + timedelta(minutes=1))
    ev = _events()
    assert len(ev) == 2 and list(ev["restored"]) == [1, 0]


def test_two_outages_same_minute_same_suburb_keep_separate_ids(db):
    a = _row(["BELGRAVE"], 40, "Planned", "2026-11-04 09:00:00")
    b = _row(["BELGRAVE"], 12, "Planned", "2026-11-04 09:00:00")
    outages.sync([a, b], now=T0)
    first = dict(zip(_events()["customers_off"], _events()["outage_ref"]))
    assert len(first) == 2
    # Listed in the other order next cycle: customers break the tie.
    outages.sync([b, a], now=T0 + timedelta(minutes=1))
    again = dict(zip(_events()["customers_off"], _events()["outage_ref"]))
    assert again == first
    assert len(_locs(active_only=True)) == 2


def test_suburb_on_two_outages_links_both(db):
    outages.sync([_row(["BELGRAVE", "TECOMA"], 900),
                  _row(["BELGRAVE"], 40, "Planned", "2026-11-04 09:30:00")], now=T0)
    refs = _ref_of("BELGRAVE")
    assert len(refs) == 2
    assert sorted(power_data.outages_for_location("BELGRAVE")) == sorted(refs)
    df = power_data.active_outages()
    assert outages.outage_count(df) == 2
    assert outages.customers_total(df) == 940      # not 900 + 900 + 40


def test_restored_then_back_within_window_keeps_id(db):
    outages.sync([_row(["A", "B"])], now=T0)
    ref = _events().iloc[0]["outage_ref"]
    counts = outages.sync([], now=T0 + timedelta(minutes=1))
    assert counts["restored"] == 1 and _events().iloc[0]["restored"] == 1
    assert _locs(active_only=True).empty
    counts = outages.sync([_row(["A", "B"])], now=T0 + timedelta(minutes=10))
    assert counts["reopened"] == 1
    ev = _events()
    assert len(ev) == 1 and ev.iloc[0]["outage_ref"] == ref and ev.iloc[0]["restored"] == 0
    # Its locality rows came back too — no second row per suburb.
    assert len(_locs(ref)) == 2 and set(_locs(ref)["restored"]) == {0}


def test_back_after_window_is_a_new_outage(db):
    outages.sync([_row(["A"])], now=T0)
    outages.sync([], now=T0 + timedelta(minutes=1))
    outages.sync([_row(["A"])], now=T0 + timedelta(minutes=45), reopen_minutes=30)
    assert len(_events()) == 2


def test_pre_id_rows_are_adopted_not_duplicated(db):
    database.insert_rows("power_outages", [
        {"location": "BELGRAVE", "customers_off": 900, "type": "Unplanned",
         "first_seen": "2026-11-04 09:00:00", "last_seen": "2026-11-04 09:55:00",
         "restored": 0},
        {"location": "TECOMA", "customers_off": 900, "type": "Unplanned",
         "first_seen": "2026-11-04 09:00:00", "last_seen": "2026-11-04 09:55:00",
         "restored": 0},
        {"location": "GONE", "customers_off": 5, "type": "Planned",
         "first_seen": "2026-11-04 08:00:00", "last_seen": "2026-11-04 09:55:00",
         "restored": 0}])
    counts = outages.sync([_row(["BELGRAVE", "TECOMA"], 850)], now=T0)
    assert counts["adopted"] == 1 and counts["new"] == 0
    ref = _events().iloc[0]["outage_ref"]
    rows = _locs()
    assert list(rows[rows["location"] != "GONE"]["outage_ref"]) == [ref, ref]
    assert len(rows) == 3                                    # nothing inserted twice
    assert rows[rows["location"] == "GONE"].iloc[0]["restored"] == 1


# --------------------------------------------------------------------------- #
# Journal + replay
# --------------------------------------------------------------------------- #
def _journal():
    return database.read_df("SELECT entity_key, active, state_json FROM "
                            "entity_state_history WHERE source = 'power' ORDER BY id")


def test_journal_one_entity_per_locality_with_its_outages(db):
    outages.sync([_row(["BELGRAVE", "TECOMA"], 900),
                  _row(["BELGRAVE"], 40, "Planned", "2026-11-04 09:30:00")], now=T0)
    j = _journal()
    assert sorted(j["entity_key"]) == ["BELGRAVE", "TECOMA"]
    state = json.loads(j[j["entity_key"] == "BELGRAVE"].iloc[0]["state_json"])
    assert state["customers_off"] == 940
    assert sorted(o[1] for o in state["outages"]) == [40, 900]
    # An unchanged cycle writes nothing (change-only journal).
    outages.sync([_row(["BELGRAVE", "TECOMA"], 900),
                  _row(["BELGRAVE"], 40, "Planned", "2026-11-04 09:30:00")],
                 now=T0 + timedelta(minutes=1))
    assert len(_journal()) == 2


def test_journal_tombstones_a_restored_locality(db):
    outages.sync([_row(["A", "B"])], now=T0)
    outages.sync([_row(["A"])], now=T0 + timedelta(minutes=1))
    j = _journal()
    assert list(j[j["entity_key"] == "B"]["active"]) == [1, 0]


def test_replay_puts_an_outages_suburbs_back_together(db):
    from app import replay
    outages.sync([_row(["BELGRAVE", "TECOMA"], 900),
                  _row(["BELGRAVE"], 40, "Planned", "2026-11-04 09:30:00")], now=T0)
    frame = replay.power_at(T0 + timedelta(minutes=5))
    assert len(frame) == 3                      # (outage, locality) pairs
    items = outages.summaries(frame)
    assert sorted(o["customers_off"] for o in items) == [40, 900]
    big = next(o for o in items if o["customers_off"] == 900)
    assert sorted(m["location"] for m in big["members"]) == ["BELGRAVE", "TECOMA"]
    assert big["ref"] == _ref_of("TECOMA")[0]


def test_replay_of_pre_id_journal_rows_still_groups(db):
    history.record_batch(history.POWER, [
        {"entity_key": loc, "state": {"location": loc, "customers_off": 900,
                                      "type": "Unplanned",
                                      "first_seen": "2026-11-04 09:00:00"},
         "active": True} for loc in ("A", "B")], effective_ts="2026-11-04 09:10:00")
    from app import replay
    frame = replay.power_at(datetime(2026, 11, 4, 9, 30))
    items = outages.summaries(frame)
    assert len(items) == 1 and items[0]["ref"] is None and len(items[0]["members"]) == 2


# --------------------------------------------------------------------------- #
# Map
# --------------------------------------------------------------------------- #
def _marker_traces(traces):
    return [t for t in traces if t.name == "Power outage"]


def test_unified_map_one_marker_per_outage_and_suburbs_filled(db):
    from app.pages import unified
    _load_boundaries(("Belgrave", 145.35, -37.92), ("Tecoma", 145.34, -37.90),
                     ("Upwey", 145.33, -37.90))
    database.insert_rows("geocode_cache", [
        {"location": n, "latitude": -37.9, "longitude": 145.3}
        for n in ("BELGRAVE", "TECOMA", "UPWEY", "NOWHERE")])
    outages.sync([_row(["BELGRAVE", "TECOMA", "UPWEY", "NOWHERE"], 900),
                  _row(["BELGRAVE"], 40, "Planned", "2026-11-04 09:30:00")], now=T0)
    traces, fills = unified.render_power(power_data.map_outages())
    markers = _marker_traces(traces)
    assert sum(len(t.lat) for t in markers) == 2
    refs = {c for t in markers for c in t.customdata}
    assert refs == set(_ref_of("BELGRAVE"))
    fill_layers = [f for f in fills if f["type"] == "fill"]
    assert sum(len(f["source"]["features"]) for f in fill_layers) == 4  # 3 + Belgrave again
    planned = [f for f in fills if f["type"] == "line" and f["line"].get("dash")]
    assert len(planned) == 1
    hover = " ".join(" ".join(t.text) for t in traces if t.name == "Suburb on an outage")
    assert "Belgrave" in hover and _ref_of("TECOMA")[0] in hover
    # The unmatched locality is still on the map (a small dot), never lost.
    assert "Nowhere" in hover
    assert "1 not outlined" in " ".join(markers[0].text + markers[-1].text)


def test_unified_map_without_boundaries_still_draws_outages(db):
    from app.pages import unified
    database.insert_rows("geocode_cache", [
        {"location": n, "latitude": -37.9, "longitude": 145.3} for n in ("A", "B")])
    outages.sync([_row(["A", "B"])], now=T0)
    traces, fills = unified.render_power(power_data.map_outages())
    assert fills == []
    assert sum(len(t.lat) for t in _marker_traces(traces)) == 1


def test_power_page_selection_frames_the_outage(db):
    from app.pages import power as power_page
    _load_boundaries(("Belgrave", 145.35, -37.92), ("Tecoma", 145.34, -37.90))
    database.insert_rows("geocode_cache", [
        {"location": n, "latitude": -37.9, "longitude": 145.3}
        for n in ("BELGRAVE", "TECOMA")])
    outages.sync([_row(["BELGRAVE", "TECOMA"])], now=T0)
    ref = _ref_of("BELGRAVE")[0]
    rows = power_data.map_outages()
    plain = power_page._map_figure(rows, dark=False)
    chosen = power_page._map_figure(rows, dark=False, selected=ref)
    assert plain.layout.meta["wdFocus"] == "state"
    assert chosen.layout.meta["wdFocus"] == ref
    assert chosen.layout.map.zoom > plain.layout.map.zoom
    assert len(chosen.layout.map.layers) == len(plain.layout.map.layers) + 1
    table = power_page.table_rows(power_data.outage_events(), outages.summaries(rows))
    assert table[0]["outage_ref"] == ref and table[0]["mapped"] == "2/2"
    assert "Belgrave" in table[0]["localities"]
    detail = str(power_page.outage_detail(ref))
    assert ref in detail and "Tecoma" in detail and "outlined" in detail


def test_power_detail_links_an_outage_sharing_a_suburb(db):
    from app.pages import power as power_page
    outages.sync([_row(["BELGRAVE", "TECOMA"], 900),
                  _row(["BELGRAVE"], 40, "Planned", "2026-11-04 09:30:00")], now=T0)
    big = [r for r in _ref_of("TECOMA")][0]
    other = [r for r in _ref_of("BELGRAVE") if r != big][0]
    detail = str(power_page.outage_detail(big))
    assert "also on" in detail and other in detail


# --------------------------------------------------------------------------- #
# Consumers count an outage once
# --------------------------------------------------------------------------- #
def test_intel_feed_one_entry_per_outage(db, monkeypatch):
    from app import intel_feed
    from app.config import load_config
    outages.sync([_row(["A", "B", "C"], 900, first="2026-11-04 09:58:00")], now=T0)
    cfg = load_config()
    monkeypatch.setattr(intel_feed, "_now", lambda: T0 + timedelta(minutes=1))
    intel_feed._detect_power(cfg, T0 - timedelta(hours=3))
    new = database.read_df("SELECT * FROM intel_events WHERE kind = 'new'")
    assert len(new) == 1
    assert new.iloc[0]["entity_key"] == _ref_of("A")[0]
    assert new.iloc[0]["headline"] == "New outage: A, B, C"
    # Growth: one entry, not three.
    outages.sync([_row(["A", "B", "C"], 2900, first="2026-11-04 09:58:00")],
                 now=T0 + timedelta(minutes=2))
    monkeypatch.setattr(intel_feed, "_now", lambda: T0 + timedelta(minutes=3))
    intel_feed._detect_power(cfg, T0 - timedelta(hours=3))
    grew = database.read_df("SELECT * FROM intel_events WHERE kind = 'growth'")
    assert len(grew) == 1 and "grew by 2,000" in grew.iloc[0]["headline"]


def test_intel_feed_skips_an_outage_already_announced_by_locality(db, monkeypatch):
    from app import intel_feed
    from app.config import load_config
    intel_feed.record("power", "new", intel_feed.NOTABLE, "New outage at A",
                      "2026-11-04 09:58:00", entity_key="A")
    outages.sync([_row(["A", "B"], 900, first="2026-11-04 09:58:00")], now=T0)
    monkeypatch.setattr(intel_feed, "_now", lambda: T0 + timedelta(minutes=1))
    intel_feed._detect_power(load_config(), T0 - timedelta(hours=3))
    assert len(database.read_df("SELECT * FROM intel_events WHERE kind = 'new'")) == 1


def test_context_line_counts_outages_not_suburbs():
    from app import intel_feed
    sources = {"roads": None, "gauges": None, "fires": None,
               "outages": pd.DataFrame([
                   {"outage_ref": "PWR-1", "location": loc, "customers_off": 900,
                    "latitude": -37.9, "longitude": 145.3} for loc in "ABC"])}
    lines = intel_feed._nearby_lines(-37.9, 145.3, 10, sources, "fire")
    assert "1 power outage nearby — 900 customers off" in lines
    assert not any("power outage" in l
                   for l in intel_feed._nearby_lines(-37.9, 145.3, 10, sources, "power"))


def test_storm_wall_counts_each_outage_once():
    from app import storm_areas
    df = pd.DataFrame([{"outage_ref": "PWR-1", "location": l, "customers_off": 900}
                       for l in "ABC"]
                      + [{"outage_ref": "PWR-2", "location": "A", "customers_off": 40}])
    assert storm_areas.outage_customers(df) == 940
    members = storm_areas.members_from({"power": df.assign(latitude=-37.9,
                                                           longitude=145.3)})
    assert len({m.id for m in members}) == 4       # A on two outages: two members


def test_opsum_lists_outages_not_localities(db):
    from app import opsum_auto
    from app.config import load_config
    database.insert_rows("power_timeseries", [{"timestamp": "2026-11-04 09:59:00",
                                               "customers_off": 5400}])
    outages.sync([_row(["BRIGHT", "POREPUNKAH", "WANDILIGONG"], 4200)], now=T0)
    lines = opsum_auto.p_power(type("Ctx", (), {"cfg": load_config(),
                                               "now": T0})())[0].value.split("\n")
    assert lines == ["5,400 customers off supply statewide.",
                     "  Bright, Porepunkah, Wandiligong — 4,200 customers"]


def test_export_has_outage_and_locality_sheets(db):
    from app import export
    outages.sync([_row(["A", "B"])], now=T0)
    _, data = export.build_export("2026-11-04 00:00:00", "2026-11-04 23:59:59",
                                  include_flood=False, include_rainfall=False)
    import io
    sheets = pd.read_excel(io.BytesIO(data), sheet_name=None)
    assert len(sheets["Power Outages"]) == 1
    assert sheets["Power Outages"].iloc[0]["localities"] == "A, B"
    locs = sheets["Power Outage Localities"]
    assert list(locs["location"]) == ["A", "B"]
    assert set(locs["outage_ref"]) == {sheets["Power Outages"].iloc[0]["outage_ref"]}

"""Incident statistics (/stats): periods (06:00 day, FY, event), the RFA /
structure / grass classification by agency, window boundaries, trend buckets,
active-now counts, comparisons and the page rendering from a snapshot."""
from datetime import date, datetime

import pytest

from app import database, stats
from app.pages import stats as stats_page

NOW = datetime(2026, 10, 13, 9, 30)
TS = "%Y-%m-%d %H:%M:%S"


def _row(sid, cat1, org, cat2=None, created=None, resolved=0, feed_type="incident",
         location="1 Main St, Ferntree Gully"):
    return {"source_id": sid, "feed_type": feed_type, "category1": cat1,
            "category2": cat2, "source_org": org, "resolved": resolved,
            "created": created, "first_seen": created or "2026-10-01 00:00:00",
            "last_seen": NOW.strftime(TS), "location": location}


@pytest.fixture
def data(db):
    database.insert_rows("fire_incidents", [
        # Inside 24 h to 06:00 13 Oct  (12 Oct 06:00 < t <= 13 Oct 06:00)
        _row("r1", "Tree Down", "VIC/SES", created="2026-10-12 07:00:00"),
        _row("r2", "Building Damage", "VIC/SES", created="2026-10-13 06:00:00", resolved=1),
        _row("s1", "Fire", "VIC/CFA", "Structure Fire", "2026-10-12 12:00:00"),
        _row("s2", "Fire", "VIC/FRV", "Structure Fire", "2026-10-12 13:00:00"),
        _row("s3", "Fire", "VIC/FRV", "Non-Structure Fire", "2026-10-12 13:30:00"),
        _row("g1", "Fire", "VIC/CFA", "Grass Fire", "2026-10-12 14:00:00", resolved=1),
        _row("g2", "Fire", "VIC/DEECA", "Bushfire", "2026-10-12 15:00:00"),
        _row("g3", "Fire", "VIC/CFA", "Scrub Fire", "2026-10-12 16:00:00"),
        # Outside (on/before the start, after the end)
        _row("o1", "Tree Down", "VIC/SES", created="2026-10-12 06:00:00"),
        _row("o2", "Fire", "VIC/CFA", "Grass Fire", "2026-10-13 07:00:00"),
        # Previous day
        _row("p1", "Flooding", "VIC/SES", created="2026-10-11 10:00:00", resolved=1),
        # Collection began long before (so comparisons are allowed)
        _row("old", "Tree Down", "VIC/SES", created="2026-09-01 10:00:00", resolved=1),
        # Never incidents
        _row("w1", "Watch and Act", "VIC/CFA", created="2026-10-12 10:00:00",
             feed_type="warning"),
        _row("b1", "Fire", "VIC/DELWP", "Bushfire", "2026-10-12 10:00:00",
             feed_type="burn-area"),
    ], ignore_duplicates=True)
    database.insert_rows("fire_timeseries", [
        {"timestamp": "2026-10-12 10:00:00", "total_active": 9, "active_fires": 3,
         "emergency_warnings": 0, "watch_act": 1, "advice": 2},
        {"timestamp": "2026-10-12 10:03:00", "total_active": 12, "active_fires": 4,
         "emergency_warnings": 0, "watch_act": 1, "advice": 2},
    ])
    return db


def test_day_period_is_0600_to_0600():
    p = stats.day_period(date(2026, 10, 13))
    assert p.start == datetime(2026, 10, 12, 6) and p.end == datetime(2026, 10, 13, 6)
    assert p.previous == (datetime(2026, 10, 11, 6), datetime(2026, 10, 12, 6))


def test_last_completed_day_rolls_at_0600():
    assert stats.last_completed_day(datetime(2026, 10, 13, 5, 59)) == date(2026, 10, 12)
    assert stats.last_completed_day(datetime(2026, 10, 13, 6, 0)) == date(2026, 10, 13)


def test_today_period_in_progress():
    p = stats.today_period(datetime(2026, 10, 13, 5))
    assert p.start == datetime(2026, 10, 12, 6) and p.partial(datetime(2026, 10, 13, 5))


def test_financial_year():
    assert stats.fy_start_year(date(2026, 6, 30)) == 2025
    assert stats.fy_start_year(date(2026, 7, 1)) == 2026
    p = stats.fy_period(2026, NOW)
    assert p.start == datetime(2026, 7, 1) and p.end == datetime(2027, 7, 1)
    assert p.label == "FY 2026-27"
    # Same span of the previous FY, not the whole of it.
    assert p.previous == (datetime(2025, 7, 1), datetime(2025, 7, 1) + (NOW - p.start))
    assert [y for y, _ in stats.fy_options(NOW, first_year=2024)] == [2026, 2025, 2024]


def test_event_period():
    tag = {"name": "Oct storms", "start_ts": "2026-10-10 12:00:00", "end_ts": None}
    p = stats.event_period(tag, NOW)
    assert p.end == NOW and "ongoing" in p.label and p.previous is None


def test_classify_rules():
    c = stats.classify({"category1": "Fire", "category2": "Structure Fire",
                        "source_org": "VIC/FRV"})
    assert c["structure_frv"] and not c["rfa"]
    c = stats.classify({"category1": "Fire", "category2": "Grass and Scrub Fire",
                        "source_org": "VIC/CFA"})
    assert c["grass_cfa"]
    c = stats.classify({"category1": "Fire", "category2": "Bushfire",
                        "source_org": "VIC/DEECA"})
    assert c["grass_ffm"]
    c = stats.classify({"category1": "Tree Down", "category2": None, "source_org": "VIC/SES"})
    assert c["rfa"] and not c["fire"]
    # Non-structure is not a structure fire.
    c = stats.classify({"category1": "Fire", "category2": "Non-Structure Fire",
                        "source_org": "VIC/CFA"})
    assert c["kind"] is None


def test_period_totals(data):
    snap = stats.build(stats.day_period(date(2026, 10, 13)), NOW)
    t = snap.totals
    assert t["rfa"] == 2                       # r1 + r2 (06:00 exactly is inside)
    assert (t["structure_cfa"], t["structure_frv"], t["structure"]) == (1, 1, 2)
    assert (t["grass_cfa"], t["grass_ffm"], t["grass"]) == (2, 1, 3)
    assert t["fires"] == 6 and t["incidents"] == 8
    assert snap.previous["rfa"] == 2          # p1 + o1 (06:00 12 Oct closes 11 Oct's day)


def test_active_now(data):
    snap = stats.build(stats.day_period(date(2026, 10, 13)), NOW)
    # Unresolved incidents: r1, s1, s2, s3, g2, g3, o1, o2 (warnings/burn areas never)
    assert snap.active == {"incidents": 8, "rfa": 2, "fires": 6}


def test_fire_matrix(data):
    snap = stats.build(stats.day_period(date(2026, 10, 13)), NOW)
    rows = {r["kind"]: r for r in snap.fire_matrix}
    assert rows["Structure"]["cfa"] == 1 and rows["Structure"]["frv"] == 1
    assert rows["Grass & scrub/bush"]["ffm"] == 1 and rows["Grass & scrub/bush"]["total"] == 3
    assert rows["Other fires"]["frv"] == 1


def test_trend_buckets_hourly_and_edges(data):
    p = stats.day_period(date(2026, 10, 13))
    snap = stats.build(p, NOW)
    assert snap.unit == "hour" and len(snap.trend) == 24
    assert snap.trend["rfa"].sum() == 2          # 06:00 exactly lands in the last bucket
    assert snap.trend.iloc[-1]["rfa"] == 1
    assert snap.trend["structure"].sum() == 2 and snap.trend["grass"].sum() == 3


def test_bucket_units():
    assert stats.bucket_unit(datetime(2026, 7, 1), datetime(2027, 7, 1)) == "month"
    assert len(stats.bucket_edges(datetime(2026, 7, 1), datetime(2027, 7, 1), "month")) == 12
    edges = stats.bucket_edges(datetime(2026, 10, 1, 12), datetime(2026, 10, 3), "day")
    assert edges[0] == datetime(2026, 10, 1, 6)


def test_active_trend_excludes_warnings(data):
    df, unit = stats.active_trend(datetime(2026, 10, 12, 6), datetime(2026, 10, 13, 6))
    assert unit == "hour" and df.iloc[0]["incidents"] == 9 and df.iloc[0]["fires"] == 4


def test_partial_coverage_noted(data):
    snap = stats.build(stats.fy_period(2025, NOW), NOW)
    assert any("PARTIAL" in n for n in snap.notes)
    assert snap.previous is None                 # no data last FY to compare with


def test_empty_db(db):
    snap = stats.build(stats.day_period(date(2026, 10, 13)), NOW)
    assert snap.totals["rfa"] == 0 and snap.active["incidents"] == 0
    stats_page.period_cards(snap, False)
    stats_page.trend_figure(snap, True)
    stats_page.active_trend_figure(snap, True)
    stats_page.breakdown(snap)


def test_page_renders(data):
    snap = stats.build(stats.day_period(date(2026, 10, 13)), NOW)
    cards = stats_page.period_cards(snap, False)
    assert len(cards) == 4 and len(stats_page.active_cards(snap, True)) == 3
    assert stats_page.layout() is not None


def test_stats_in_nav():
    from app import factory, shell
    assert "/stats" in [p for p, _, _ in factory.PUBLIC_PAGES]
    assert any("/stats" in paths for _, paths in shell.NAV_GROUPS)


@pytest.mark.parametrize("cat1,cat2,org,expected", [
    ("Fire", "Structure Fire", "VIC/FRV", "structure_frv"),
    ("Structure Fire", None, "VIC/FRV", "structure_frv"),     # type as the category
    ("STRUCTURE FIRE", "Residential", "VIC/FRV", "structure_frv"),
    ("Fire", "Building", "VIC/MFB", "structure_frv"),
    ("Grass Fire", None, "VIC/CFA", "grass_cfa"),
    ("Fire", "Grass and Scrub", "VIC/CFA", "grass_cfa"),
])
def test_fire_type_read_from_either_category(cat1, cat2, org, expected):
    c = stats.classify({"category1": cat1, "category2": cat2, "source_org": org})
    assert c["fire"] and c[expected]


def test_alarm_is_not_a_fire():
    c = stats.classify({"category1": "Fire Alarm", "category2": None, "source_org": "VIC/FRV"})
    assert not c["fire"] and c["kind"] is None


def test_classification_check(data):
    database.insert_rows("fire_incidents", [
        _row("x1", "Structure Fire", "VIC/FRV", None, "2026-10-12 20:00:00"),
        _row("x2", "Fire", "VIC/EMV", "Structure Fire", "2026-10-12 21:00:00"),
    ], ignore_duplicates=True)
    snap = stats.build(stats.day_period(date(2026, 10, 13)), NOW)
    assert snap.totals["structure_frv"] == 2
    rows = {(o, c1, c2): a for o, c1, c2, _, a in snap.check}
    assert rows[("VIC/FRV", "Structure Fire", "—")] == "Structure fire (FRV)"
    assert rows[("VIC/EMV", "Fire", "Structure Fire")].startswith("Fire — structure fire but agency")
    assert rows[("VIC/SES", "Tree Down", "—")] == "SES RFA"
    stats_page.breakdown(snap)


# --- Real feed shape: FRV publishes no feed; metro jobs arrive as ESTA events ---
def _feed_rows():
    import json
    from tests.conftest import FIXTURE_DIR
    import os
    from app.modules.fire import scraper
    with open(os.path.join(FIXTURE_DIR, "osom_incidents_sample.json")) as fh:
        feats = json.load(fh)["features"]
    rows = []
    for f in feats:
        r = scraper._extract(f)
        r.update(first_seen=r["created"], last_seen=NOW.strftime(TS), resolved=0)
        rows.append(r)
    return rows


def test_esta_events_are_not_identified_without_the_pager():
    rows = _feed_rows()
    building = [r for r in rows if r["category2"] == "Building Fire"]
    orgs = {r["source_org"] for r in building}
    assert "VIC/ESTA" in orgs and "VIC/CFA" in orgs     # the shape seen live 2026-10-14
    for r in building:
        c = stats.classify(r)
        expected = "structure_esta" if r["source_org"] == "VIC/ESTA" else "structure_cfa"
        assert c[expected], r


def test_esta_f_number():
    assert stats.esta_f_number("ESTA:261014231") == "F261014231"
    assert stats.esta_f_number("297496") is None
    assert stats.esta_f_number(None) is None


@pytest.mark.parametrize("messages,expected", [
    ([("FS91", "[]")], "frv"),
    ([("MTEL", "[]")], "cfa"),
    ([(None, '[{"service": "FRV"}, {"service": "FRV"}, {"service": "CFA"}]')], "frv"),
    ([(None, '[{"service": "FRV"}, {"service": "CFA"}]')], None),     # a tie is not a guess
    ([(None, None)], None),
])
def test_pager_agency(messages, expected):
    assert stats.pager_agency(messages) == expected


def test_esta_structure_fire_attributed_to_frv_by_pager(db):
    database.insert_rows("fire_incidents", [
        _row("ESTA:261014300", "Fire", "VIC/ESTA", "Building Fire", "2026-10-12 20:00:00"),
        _row("ESTA:261014301", "Fire", "VIC/ESTA", "Building Fire", "2026-10-12 20:10:00"),
        _row("ESTA:261014302", "Fire", "VIC/ESTA", "Building Fire", "2026-10-12 20:20:00"),
        _row("old", "Tree Down", "VIC/SES", created="2026-09-01 10:00:00", resolved=1),
    ], ignore_duplicates=True)
    database.insert_rows("pager_messages", [
        {"msg_hash": "a", "received_at": "2026-10-12 20:01:00", "sent_at": "2026-10-12 20:01:00",
         "message": "HbALERT STRUC1 ... F261014300 [FS35_]", "f_number": "F261014300",
         "brigade": "FS35"},
        {"msg_hash": "b", "received_at": "2026-10-12 20:11:00", "sent_at": "2026-10-12 20:11:00",
         "message": "@@ALERT STRUC1 ... F261014301 [BOOR]", "f_number": "F261014301",
         "brigade": "BOOR"},
    ])
    snap = stats.build(stats.day_period(date(2026, 10, 13)), NOW)
    t = snap.totals
    assert (t["structure_frv"], t["structure_cfa"], t["structure_esta"]) == (1, 1, 1)
    assert t["structure"] == 3
    labels = {a for *_, a in snap.check}
    assert "Structure fire (FRV) — via CFA pager" in labels
    assert "Structure fire (ESTA — agency not identified)" in labels
    stats_page.period_cards(snap, False)


def _pager_hb(*times):
    database.insert_rows("pager_heartbeat", [
        {"timestamp": t, "rows_seen": 10, "new_rows": 0, "new_escalations": 0} for t in times])


def test_esta_job_not_on_pager_is_frv_when_pager_was_running(db):
    """Mazzanet carries CFA and the ex-CFA FRV stations, never the ex-MFB
    metro stations: an ESTA job the pager never saw is a metro FRV job."""
    database.insert_rows("fire_incidents", [
        _row("ESTA:261014400", "Fire", "VIC/ESTA", "Structure Fire", "2026-10-12 20:00:00"),
        _row("ESTA:261014401", "Fire", "VIC/ESTA", "Structure Fire", "2026-10-12 23:00:00"),
        _row("ESTA:261014402", "Fire", "VIC/ESTA", "Structure Fire", "2026-10-12 21:00:00"),
        _row("ESTA:261014403", "Fire", "VIC/ESTA", "Non Structure Fire", "2026-10-12 20:05:00"),
        _row("old", "Tree Down", "VIC/SES", created="2026-09-01 10:00:00", resolved=1),
    ], ignore_duplicates=True)
    # Pager running 20:00-21:30, then down (23:00 job has no cycle after it).
    _pager_hb("2026-10-12 20:04:00", "2026-10-12 20:08:00", "2026-10-12 21:04:00")
    database.insert_rows("pager_messages", [
        {"msg_hash": "c", "received_at": "2026-10-12 21:01:00", "sent_at": "2026-10-12 21:01:00",
         "message": "@@ALERT STRUC1 ... F261014402 [DAND]", "f_number": "F261014402",
         "brigade": "DAND"},
    ])
    snap = stats.build(stats.day_period(date(2026, 10, 13)), NOW)
    t = snap.totals
    assert t["structure_frv"] == 1            # 20:00, pager running, never paged
    assert t["structure_cfa"] == 1            # 21:00, paged to a CFA brigade
    assert t["structure_esta"] == 1           # 23:00, pager not running -> not guessed
    assert t["structure"] == 3                # Non Structure Fire is never a structure fire
    labels = {a for *_, a in snap.check}
    assert "Structure fire (FRV) — ESTA job not on the CFA pager (ex-MFB area)" in labels
    assert "Structure fire (CFA) — via CFA pager" in labels


def test_pager_tie_is_not_inferred_as_frv(db):
    database.insert_rows("fire_incidents", [
        _row("ESTA:261014500", "Fire", "VIC/ESTA", "Structure Fire", "2026-10-12 20:00:00"),
        _row("old", "Tree Down", "VIC/SES", created="2026-09-01 10:00:00", resolved=1),
    ], ignore_duplicates=True)
    _pager_hb("2026-10-12 20:04:00")
    database.insert_rows("pager_messages", [
        {"msg_hash": "d", "received_at": "2026-10-12 20:01:00", "sent_at": "2026-10-12 20:01:00",
         "message": "x F261014500", "f_number": "F261014500", "brigade": None,
         "units_json": '[{"service": "FRV"}, {"service": "CFA"}]'},
    ])
    snap = stats.build(stats.day_period(date(2026, 10, 13)), NOW)
    assert snap.totals["structure_esta"] == 1 and snap.totals["structure_frv"] == 0


# --- Admin copy button: what makes up each card ---
def test_card_text_lists_exactly_the_counted_incidents(data):
    snap = stats.build(stats.day_period(date(2026, 10, 13)), NOW)
    for key in stats.CARD_ROWS:
        text = stats.card_text(snap, key)
        lines = text.split("\n")
        assert lines[1].split("\t") == stats.COPY_COLUMNS
        assert all(len(l.split("\t")) == len(stats.COPY_COLUMNS) for l in lines[2:])
        expected = {"rfa": snap.totals["rfa"], "structure": snap.totals["structure"],
                    "grass": snap.totals["grass"], "incidents": snap.totals["incidents"],
                    "active_incidents": snap.active["incidents"],
                    "active_rfa": snap.active["rfa"],
                    "active_fires": snap.active["fires"]}[key]
        assert len(lines) - 2 == expected, key
        assert f"— {expected} incident" in lines[0]
    structure = stats.card_text(snap, "structure")
    assert "\ts1\t" in structure and "\ts2\t" in structure and "\ts3\t" not in structure


def test_card_text_survives_tabs_and_newlines_in_feed_text(db):
    database.insert_rows("fire_incidents", [
        _row("t1", "Tree Down", "VIC/SES", created="2026-10-12 08:00:00",
             location="12 Main\tSt,\nBelgrave"),
    ], ignore_duplicates=True)
    snap = stats.build(stats.day_period(date(2026, 10, 13)), NOW)
    lines = stats.card_text(snap, "rfa").split("\n")
    assert len(lines) == 3 and "12 Main St, Belgrave" in lines[2]


def test_copy_buttons_only_for_admins(data):
    snap = stats.build(stats.day_period(date(2026, 10, 13)), NOW)

    def has_copy(cards):
        return "stats-copy" in str(cards)
    assert not has_copy(stats_page.period_cards(snap, False))
    assert not has_copy(stats_page.active_cards(snap, False))
    assert has_copy(stats_page.period_cards(snap, False, admin=True))
    assert has_copy(stats_page.active_cards(snap, False, admin=True))

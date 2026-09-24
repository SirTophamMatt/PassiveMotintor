"""CFA pager: page parsing, message meaning, storage/de-dup and job collation."""
import json
from datetime import datetime

import pytest

from app.modules.pager import data as pager_data
from app.modules.pager import parse, scraper
from tests.conftest import load_fixture

NOW = datetime(2026, 9, 24, 14, 30)


# --- page parsing --------------------------------------------------------- #
def test_parse_page_reads_every_row_and_skips_header():
    rows = parse.parse_page(load_fixture("pager_sample.html"), now=NOW)
    assert len(rows) == 7
    first = rows[0]
    assert first["capcode"] == "0240608"
    assert first["sent_at"] == datetime(2026, 9, 24, 14, 2, 11)
    assert first["alias"] == "CFA: CORIO"
    assert first["message"].startswith("@@ALERT 06233 NOSTC1 TRUCK TRAILER")


def test_parse_page_does_not_depend_on_column_order():
    html = ("<table><tr><td>@@ALERT F2609241234 MAKE TANKERS 5</td>"
            "<td>CFA: D8</td><td>24/09/2026 14:15:40</td><td>0240700</td></tr>"
            "</table>")
    (row,) = parse.parse_page(html, now=NOW)
    assert row["capcode"] == "0240700"
    assert row["sent_at"] == datetime(2026, 9, 24, 14, 15, 40)
    assert row["message"] == "@@ALERT F2609241234 MAKE TANKERS 5"
    assert row["alias"] == "CFA: D8"


def test_parse_page_text_fallback_without_table():
    html = ("<pre>0240700 14:15:40 24-09-26 @@ALERT F2609241234 MAKE TANKERS 5\n"
            "not a message line\n</pre>")
    (row,) = parse.parse_page(html, now=NOW)
    assert row["capcode"] == "0240700"
    assert row["message"] == "@@ALERT F2609241234 MAKE TANKERS 5"


def test_parse_page_nothing_recognisable_is_empty():
    assert parse.parse_page("<html><body>Maintenance</body></html>", now=NOW) == []


@pytest.mark.parametrize("text,expected", [
    ("14:23:05 24-09-26", datetime(2026, 9, 24, 14, 23, 5)),
    ("24/09/2026 14:23", datetime(2026, 9, 24, 14, 23)),
    ("2026-09-24 14:23:05", datetime(2026, 9, 24, 14, 23, 5)),
    ("Wed 24 Sep 14:23", datetime(2026, 9, 24, 14, 23)),
    ("14:10", datetime(2026, 9, 24, 14, 10)),
    # Time-only, but later than now: it was yesterday evening.
    ("23:55:00", datetime(2026, 9, 23, 23, 55)),
    ("no time here", None),
    ("25:61", None),
])
def test_parse_sent_time(text, expected):
    assert parse.parse_sent_time(text, NOW) == expected


# --- message meaning -------------------------------------------------------- #
# Real messages from the Mazzanet page (2026-09-26).
REAL = {
    "MTEL": ("@@ALERT 09124 ALARC1 ASE - , INPUT - MAIN ENTRY PENINSULA ON THE BAY "
             "435 NEPEAN HWY FRANKSTON /R O W Y - //WELLS ST M 100A C6 (354767) "
             "F CMTEL P94 PT31 F260926603 [MTEL]"),
    "CORO": ("@@ALERT 06233 NOSTC1 TRUCK TRAILER FIRE NORTH BOUND GEELONG RING RD "
             "LOVELY BANKS M 431 F8 (661823) * INFO: SUGGESTED ON RAMP ANAKIE RD "
             "OR COX RD * F CCORO P62A F260926602 [CORO]"),
    "GLBU": ("@@ALERT GLBU1 RESCC1 * VEHICLE ACCIDENT - POSS PERSON TRAPPED CNR "
             "MELBA HWY/TWO HILLS RD GLENBURN SVNE 6367 D6 (637595) AFPR CGLBU "
             "CTOLA CYEAAR F260926326 [GLBU]"),
    "FS91": ("@@ALERT 09107 STRUC1 UNIT FIRE 22 STANLEY ST FRANKSTON /STANLEY "
             "LANE //BURROW ST M 100A H5 (363769) F AP91 CFTON P90 F260926562 "
             "[FS91_]"),
}


@pytest.mark.parametrize("key,f_number,incident", [
    ("MTEL", "F260926603", "ALARC1"),
    ("CORO", "F260926602", "NOSTC1"),
    ("GLBU", "F260926326", "RESCC1"),
    ("FS91", "F260926562", "STRUC1"),
])
def test_real_messages_brigade_type_and_f_number(key, f_number, incident):
    m = parse.parse_message(REAL[key])
    # Brigade/station comes from the trailing [..], trailing "_" dropped.
    assert m["brigade"] == key
    assert m["incident_type"] == incident
    assert m["f_number"] == f_number
    assert m["priority"] == "Emergency"
    assert not m["is_escalation"]


def _units(key):
    return [(u["code"], u["kind"], u.get("type"))
            for u in parse.parse_message(REAL[key])["units"]]


def test_units_frv_appliances_and_brigade_page():
    assert _units("MTEL") == [("CMTEL", "brigade", None),
                              ("P94", "appliance", "Pumper"),
                              ("PT31", "appliance", "Pumper Tanker")]


def test_units_stop_at_the_star_separator_not_the_job_text():
    # "... COX RD * F CCORO P62A": nothing from the INFO text is a unit.
    assert _units("CORO") == [("CCORO", "brigade", None),
                              ("P62A", "appliance", "Pumper")]


def test_units_stop_at_the_grid_reference():
    assert [c for c, _, _ in _units("GLBU")] == ["AFPR", "CGLBU", "CTOLA", "CYEAAR"]
    # The map reference before the grid ref ("D6") is not a unit.
    assert all(c != "D6" for c, _, _ in _units("GLBU"))


@pytest.mark.parametrize("code,service,type_,brigade", [
    ("COROT1", "CFA", "Tanker", "CORO"),
    ("COROP1", "CFA", "Pumper", "CORO"),
    ("COROPT1", "CFA", "Pumper Tanker", "CORO"),
    ("COROULT1", "CFA", "Ultralight", "CORO"),
    ("COROFCV1", "CFA", "FCV", "CORO"),
    ("P1A", "FRV", "Pumper", "FS01"),
    ("P1B", "FRV", "Pumper", "FS01"),
    ("PT31", "FRV", "Pumper Tanker", "FS31"),
    ("AP91", "FRV", "AP", "FS91"),
])
def test_classify_unit(code, service, type_, brigade):
    u = parse.classify_unit(code)
    assert (u["kind"], u["service"], u["type"], u["brigade"]) == \
        ("appliance", service, type_, brigade)


def test_alarm_group_token_is_not_an_appliance():
    # "GLBU1" after ALERT is the brigade + alarm level, not a vehicle.
    assert parse.classify_unit("GLBU1")["kind"] == "other"


def test_brigade_falls_back_to_alert_group_without_brackets():
    m = parse.parse_message("@@ALERT GLBU1 RESCC1 VEHICLE ACCIDENT F260926326")
    assert (m["brigade"], m["incident_type"]) == ("GLBU", "RESCC1")


def test_required_appliance_by_call_sign():
    m = parse.parse_message("@@ALERT F260926602 NOSTC1 TANKER COROT1 REQUIRED [CORO]")
    assert m["required"] == [("Tanker", "COROT1")]
    assert m["is_escalation"]


@pytest.mark.parametrize("text,make", [
    ("F2609241234 MAKE TANKERS 5", {"Tanker": 5}),
    ("MAKE 5 TANKERS", {"Tanker": 5}),
    ("MAKE PUMPERS 2 TANKERS 4", {"Pumper": 2, "Tanker": 4}),
    ("MAKE UP TANKERS TO 6", {"Tanker": 6}),
    ("MAKE ULTRA LIGHTS 3", {"Ultralight": 3}),
    ("MAKE PUMPER TANKERS 2", {"Pumper Tanker": 2}),
    ("make tankers 5", {"Tanker": 5}),
    ("MAKE TANKERS", {"Tanker": None}),
])
def test_make_requests(text, make):
    assert parse.parse_escalation(text.upper())["make"] == make


def test_make_request_is_not_also_counted_as_required():
    esc = parse.parse_escalation("MAKE TANKERS 5 ALL REQUIRED")
    assert esc == {"make": {"Tanker": 5}, "required": []}


def test_pumper_tanker_is_one_request():
    esc = parse.parse_escalation("PUMPER TANKER HAST1 REQUIRED")
    assert esc["required"] == [("Pumper Tanker", "HAST1")]


def test_appliance_named_without_request_is_not_escalation():
    # A turnout that merely mentions a tanker is not asking for one.
    assert not parse.parse_message("@@ALERT 06233 NOSTC1 TANKER ROLLOVER F2609241234 [CORO]")["is_escalation"]


# --- storage + collation ---------------------------------------------------- #
def _store_fixture():
    rows = scraper.build_rows(
        parse.parse_page(load_fixture("pager_sample.html"), now=NOW),
        received="2026-09-24 14:30:00")
    return scraper.store(rows)


def test_store_dedups_rereads(db):
    assert _store_fixture() == (7, 2)
    # The next cycle sees the same page: nothing new.
    assert _store_fixture() == (0, 0)


def test_undated_message_keyed_to_day_first_seen(db):
    row = {"capcode": "1", "alias": None, "sent_at": None,
           "message": "QD NO TIME ON THIS ONE"}
    a = scraper.build_rows([row], "2026-09-24 10:00:00")
    b = scraper.build_rows([row], "2026-09-24 10:04:00")
    assert a[0]["msg_hash"] == b[0]["msg_hash"]
    assert a[0]["sent_at"] == "2026-09-24 10:00:00"


def test_jobs_collate_by_f_number(db):
    _store_fixture()
    jobs = pager_data.jobs(24, now=NOW).set_index("f_number")
    assert set(jobs.index) == {"F2609241234", "F2609241240"}
    truck = jobs.loc["F2609241234"]
    # The same text to two capcodes is one message for the job.
    assert truck["messages"] == 4
    assert truck["capcodes"] == 5
    assert truck["brigade"] == "CORO"
    assert truck["incident_type"] == "NOSTC1"
    assert truck["escalated"]
    # Appliances from the unit lists AND the ULTRALIGHT LANGULT1 request.
    assert truck["appliances"] == ("Pumper ×1 (P62A) · Tanker ×2 (COROT1, LARAT1)"
                                   " · Ultralight ×1 (LANGULT1)")
    assert truck["brigades_paged"] == "CORO"
    assert truck["escalation"] == ("MAKE Tankers 5 (2 paged) · MAKE Pumpers 2 "
                                   "(1 paged) · Ultralight req ×1")
    assert truck["make_matched"] == "Pumper, Tanker"
    frv = jobs.loc["F2609241240"]
    assert not frv["escalated"]
    assert (frv["brigade"], frv["incident_type"]) == ("FS91", "STRUC1")
    assert frv["appliances"] == "AP ×1 (AP91) · Pumper ×1 (P90)"


def test_latest_make_figure_wins(db):
    rows = [{"capcode": "1", "alias": None, "message": msg,
             "sent_at": datetime(2026, 9, 24, 14, minute)}
            for minute, msg in ((5, "F2609241234 MAKE TANKERS 5"),
                                (20, "F2609241234 MAKE TANKERS 8"))]
    scraper.store(scraper.build_rows(rows, "2026-09-24 14:30:00"))
    (job,) = pager_data.jobs(24, now=NOW).to_dict("records")
    assert job["escalation"] == "MAKE Tankers 8"


def test_counts_and_window(db):
    _store_fixture()
    c = pager_data.counts(24, now=NOW)
    assert c == {"messages": 7, "jobs": 2, "esc_jobs": 1, "esc_messages": 2}
    # 12 minutes back from 14:30 -> only the 14:20 and 14:21 pages.
    assert pager_data.counts(0.2, now=NOW)["messages"] == 2


def test_escalations_after_and_job_messages(db):
    _store_fixture()
    esc = pager_data.escalations_after(0)
    assert len(esc) == 2
    assert pager_data.escalations_after(pager_data.max_id()).empty
    msgs = pager_data.job_messages("F2609241234")
    assert list(msgs["sent_at"]) == sorted(msgs["sent_at"])
    assert json.loads(esc.iloc[0]["make_json"]) == {"Tanker": 5, "Pumper": 2}


def test_unparseable_page_saves_debug_copy(db, monkeypatch, tmp_path):
    class Resp:
        content = b"<html><body>New layout</body></html>"

        def raise_for_status(self):
            pass

    monkeypatch.setattr(scraper.requests, "get", lambda *a, **k: Resp())
    debug = tmp_path / "pager_debug.html"
    monkeypatch.setattr(scraper, "DEBUG_FILE", str(debug))
    with pytest.raises(RuntimeError, match="layout"):
        scraper.fetch_pager_data()
    assert debug.read_bytes() == Resp.content
    # The heartbeat still records that the cycle ran.
    assert pager_data.heartbeat_summary()[0] == 1


def test_reparse_upgrades_rows_from_an_older_parser(db):
    from app import database
    _store_fixture()
    # Simulate rows written by parser v1 (no units, old brigade guess).
    database.execute("UPDATE pager_messages SET parser_version = 1, "
                     "brigade = 'KNKE1', units_json = NULL")
    assert scraper.reparse_stale() == 7
    assert scraper.reparse_stale() == 0            # nothing left to do
    df = database.read_df("SELECT brigade, units_json FROM pager_messages "
                          "WHERE f_number = 'F2609241240'")
    assert df.iloc[0]["brigade"] == "FS91"
    assert "AP91" in df.iloc[0]["units_json"]

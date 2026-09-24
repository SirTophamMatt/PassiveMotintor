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
    assert first["alias"] == "CFA: CRANBOURNE"
    assert first["message"].startswith("@@ALERT F2609241234 CRAN1 G&SC1")


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
def test_parse_message_alert_fields():
    m = parse.parse_message("@@ALERT F2609241234 CRAN1 STRUC1 HOUSE FIRE")
    assert m["f_number"] == "F2609241234"
    assert m["brigade"] == "CRAN1"
    assert m["incident_type"] == "STRUC1"
    assert m["priority"] == "Emergency"
    assert not m["is_escalation"]


def test_parse_message_brigade_before_f_number():
    m = parse.parse_message("ALERT CHUR4 F090202691 G&SC1 TANKER TRAWT1 REQUIRED")
    assert (m["f_number"], m["brigade"], m["incident_type"]) == \
        ("F090202691", "CHUR4", "G&SC1")
    assert m["required"] == [("Tanker", "TRAWT1")]
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
    assert not parse.parse_message("@@ALERT F2609241234 CRAN1 NOSTC1 TANKER ROLLOVER")["is_escalation"]


# --- storage + collation ---------------------------------------------------- #
def _store_fixture():
    rows = scraper.build_rows(
        parse.parse_page(load_fixture("pager_sample.html"), now=NOW),
        received="2026-09-24 14:30:00")
    return scraper.store(rows)


def test_store_dedups_rereads(db):
    assert _store_fixture() == (7, 3)
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
    grass = jobs.loc["F2609241234"]
    # The same text to two capcodes is one message for the job.
    assert grass["messages"] == 4
    assert grass["capcodes"] == 5
    assert grass["brigade"] == "CRAN1"
    assert grass["incident_type"] == "G&SC1"
    assert grass["escalated"]
    assert grass["escalation"] == ("MAKE Tankers 5 (1 paged) · MAKE Pumpers 2 · "
                                   "Ultralight req ×1")
    assert grass["make_matched"] == "Tanker"
    assert not jobs.loc["F2609241240"]["escalated"]


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
    assert c == {"messages": 7, "jobs": 2, "esc_jobs": 1, "esc_messages": 3}
    # 12 minutes back from 14:30 -> only the 14:20 and 14:21 pages.
    assert pager_data.counts(0.2, now=NOW)["messages"] == 2


def test_escalations_after_and_job_messages(db):
    _store_fixture()
    esc = pager_data.escalations_after(0)
    assert len(esc) == 3
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

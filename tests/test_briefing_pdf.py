"""Briefing PDF: builds from the same snapshot the screen shows, and survives
every combination of missing sections.

A briefing pack is generated under time pressure, so "the report failed" is a
much worse outcome than "the report says there is nothing to report". These
tests exist to keep an empty section from ever becoming an exception.
"""
from datetime import datetime, timedelta

import pytest

from app import briefing, database, reporting

reportlab = pytest.importorskip("reportlab",
                                reason="reportlab is required for PDF reports")


def _text_of(pdf_bytes):
    pypdf = pytest.importorskip("pypdf")
    import io
    reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def _empty_snapshot():
    now = datetime.now()
    return briefing.BriefingSnapshot(
        generated_at=now, as_of=now, window_minutes=60, window_label="1 hour",
        window_start=now - timedelta(minutes=60))


def test_pdf_builds_from_a_completely_empty_snapshot():
    name, blob = reporting.build_briefing_pdf(_empty_snapshot())
    assert name.startswith("watchdesk_briefing_") and name.endswith(".pdf")
    assert blob[:4] == b"%PDF"
    assert len(blob) > 1000


def test_empty_sections_print_their_nothing_to_report_line():
    """An absent heading and an empty one mean very different things on a
    briefing sheet, so a section is never silently dropped."""
    text = _text_of(reporting.build_briefing_pdf(_empty_snapshot())[1])
    assert "No significant changes detected in this period." in text
    assert "No active VicEmergency community warnings." in text
    assert "No active BoM warnings." in text
    assert "No significant cross-layer consequences currently identified." in text
    assert "Nothing currently approaching a threshold." in text


def test_pdf_carries_every_section_heading():
    text = _text_of(reporting.build_briefing_pdf(_empty_snapshot())[1])
    for heading in ("Operational Briefing", "Current Situation",
                    "Significant Changes", "Active Warnings — VicEmergency",
                    "Active Warnings — Bureau of Meteorology",
                    "Emerging Consequences", "Watch Points",
                    "Weather Observations", "Data Freshness"):
        assert heading in text, heading


def test_pdf_renders_the_snapshot_it_is_given_not_a_fresh_one(db):
    """The pack must show what was on screen — rebuilding would let the two
    disagree by however long the operator took to click Export."""
    snapshot = _empty_snapshot()
    snapshot.changes = [briefing.Change(
        ts=datetime.now(), time="13:43", headline="Walwa fire increased 38 ha",
        lines=["Walwa fire: 214 → 296 ha"], severity=2, severity_label="Major",
        hazard="fire", hazard_label="Fire", entity_name="Walwa fire",
        url="/fire")]
    text = _text_of(reporting.build_briefing_pdf(snapshot)[1])
    assert "Walwa fire increased 38 ha" in text
    assert "13:43" in text


def test_stale_sources_are_warned_about_on_page_one(db):
    """If a source is dead that has to be known before anything below it is
    read, not discovered on the last page."""
    snapshot = _empty_snapshot()
    snapshot.sources = [
        briefing.SourceStatus("Flood", state="stale", age_minutes=400),
        briefing.SourceStatus("AWS", state="ok", age_minutes=3,
                              last_update=datetime.now()),
    ]
    text = _text_of(reporting.build_briefing_pdf(snapshot)[1])
    assert "Data warning" in text
    assert "Flood" in text


def test_no_stale_banner_when_everything_is_healthy():
    snapshot = _empty_snapshot()
    snapshot.sources = [briefing.SourceStatus("AWS", state="ok", age_minutes=2,
                                              last_update=datetime.now())]
    assert "Data warning" not in _text_of(
        reporting.build_briefing_pdf(snapshot)[1])


def test_projections_are_labelled_distinctly_from_observations():
    """An extrapolated ETA must never be printable as a measurement."""
    snapshot = _empty_snapshot()
    snapshot.watch_points = [
        briefing.WatchPoint(title="Snowy River at Orbost",
                            lines=["0.34 m below Major"],
                            kind=briefing.PROJECTION),
        briefing.WatchPoint(title="Storm cell 14", lines=["Strong · SE at 52 km/h"],
                            kind=briefing.OBSERVED),
    ]
    text = _text_of(reporting.build_briefing_pdf(snapshot)[1])
    assert "Trend projection" in text
    assert "Observed" in text


def test_markup_characters_in_source_text_do_not_break_the_report():
    """BoM titles carry & and < ; reportlab Paragraphs parse a mini-HTML, so
    unescaped source text would raise mid-build."""
    snapshot = _empty_snapshot()
    snapshot.warnings = [briefing.Warning(
        source="BoM", kind="Flood Warning",
        title="Ovens & King Rivers <urgent> \"quoted\" 'text'",
        issued=datetime.now())]
    name, blob = reporting.build_briefing_pdf(snapshot)
    assert blob[:4] == b"%PDF"
    assert "Ovens & King Rivers" in _text_of(blob)


def test_pdf_builds_without_kaleido():
    """The briefing is tables and text by design, so it must not depend on the
    chart renderer — the one component most likely to be broken on a server."""
    def explode(*_args, **_kwargs):
        raise reporting.ReportingUnavailable("kaleido missing")

    original = reporting._fig_png
    reporting._fig_png = explode
    try:
        _name, blob = reporting.build_briefing_pdf(_empty_snapshot())
        assert blob[:4] == b"%PDF"
    finally:
        reporting._fig_png = original


def test_pdf_can_build_its_own_snapshot_when_not_given_one(db):
    name, blob = reporting.build_briefing_pdf(window_minutes=30)
    assert blob[:4] == b"%PDF"
    assert "1 hour" not in _text_of(blob)
    assert "30 minutes" in _text_of(blob)


def test_existing_overview_pdf_still_builds(db):
    """The shared-table refactor must not have broken the report that was
    already in service."""
    database.insert_rows("power_timeseries", [
        {"timestamp": "2026-08-10 23:00:00", "customers_off": 1200,
         "power_dependant_off": 4, "planned": 1, "unplanned": 9}])
    name, blob = reporting.build_overview_pdf()
    assert name.startswith("watchdesk_overview_")
    assert blob[:4] == b"%PDF"


def test_screen_and_pdf_agree_on_the_same_snapshot(db):
    """The whole point of the shared model: one set of operational statements.
    Anything stated in the text rendering must appear in the PDF."""
    snapshot = briefing.build_briefing_snapshot()
    snapshot.situation = [briefing.Kpi("Customers Without Power", "1,240",
                                       "Power")]
    text = briefing.briefing_text(snapshot)
    pdf_text = _text_of(reporting.build_briefing_pdf(snapshot)[1])
    assert "1,240" in text and "1,240" in pdf_text
    assert snapshot.generated_label in pdf_text

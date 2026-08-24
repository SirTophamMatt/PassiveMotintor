"""Feedback form: reference IDs, storage-before-email, and the email body."""
import datetime

import pytest

from app import database, feedback, mailer


@pytest.fixture
def cfg():
    """Config with email switched off, so submit() never touches a network."""
    return {"feedback": {"recipient": "reports@example.com",
                         "email_enabled": False, "max_per_hour": 5},
            "smtp": {}}


@pytest.fixture
def sending_cfg():
    return {"feedback": {"recipient": "reports@example.com",
                         "email_enabled": True, "max_per_hour": 5},
            "smtp": {"host": "smtp.example.com", "port": 587,
                     "from_address": "watchdesk@example.com",
                     "from_name": "Watchdesk", "security": "starttls"}}


# --------------------------------------------------------------- reference --
def test_ref_shape():
    ref = feedback.make_ref("bug", datetime.datetime(2026, 8, 24))
    assert ref.startswith("WD-BUG-260824-")
    assert len(ref.split("-")[-1]) == 4


def test_ref_kinds_are_distinguishable():
    when = datetime.datetime(2026, 8, 24)
    assert feedback.make_ref("suggestion", when).startswith("WD-SUG-")


def test_ref_avoids_ambiguous_characters():
    """A reference is read off a screen and typed into an email, so the four
    characters people reliably confuse must never appear in one."""
    suffixes = "".join(feedback.make_ref("bug").rsplit("-", 1)[-1]
                       for _ in range(300))
    assert not (set("IO01") & set(suffixes))


def test_refs_are_unique_across_many():
    refs = {feedback.make_ref("bug") for _ in range(500)}
    assert len(refs) > 490  # 32^4 space; a handful of collisions is expected


# -------------------------------------------------------------- validation --
@pytest.mark.parametrize("kind,message,email,ok", [
    ("bug", "The flood map never loads for me.", "", True),
    ("suggestion", "Please add a dark mode to the PDF.", "a@b.co", True),
    ("bug", "", "", False),               # nothing said
    ("bug", "broken", "", False),         # too short to act on
    ("nonsense", "A real description here.", "", False),
    ("bug", "A real description here.", "not-an-email", False),
])
def test_validate(kind, message, email, ok):
    assert (feedback.validate(kind, message, email) == "") is ok


# ----------------------------------------------------------------- storage --
def test_submit_stores_and_returns_reference(db, cfg):
    row = feedback.submit("bug", "The flood map never loads on mobile.",
                          subject="Map blank", severity="high",
                          page_path="/flood", ip_prefix="203.0.113.0", cfg=cfg)
    assert "error" not in row
    assert row["ref"].startswith("WD-BUG-")

    stored = database.read_df("SELECT * FROM feedback_reports")
    assert len(stored) == 1
    assert stored.iloc[0]["ref"] == row["ref"]
    assert stored.iloc[0]["severity"] == "high"
    assert stored.iloc[0]["status"] == "new"


def test_report_survives_email_failure(db, sending_cfg, monkeypatch):
    """The whole point of storing first: a dead mail server must cost a
    notification, never the report."""
    monkeypatch.setattr(mailer, "send",
                        lambda *a, **k: (False, "SMTPConnectError: refused"))
    row = feedback.submit("bug", "Something is definitely wrong here.",
                          cfg=sending_cfg)
    assert row["email_status"] == "failed"

    stored = database.read_df("SELECT * FROM feedback_reports")
    assert len(stored) == 1
    assert stored.iloc[0]["email_status"] == "failed"
    assert "refused" in stored.iloc[0]["email_error"]


def test_unconfigured_smtp_is_skipped_not_failed(db, cfg):
    row = feedback.submit("suggestion", "Add a CSV export to the feed page.",
                          cfg=cfg)
    assert row["email_status"] == "skipped"
    assert database.read_df("SELECT * FROM feedback_reports").shape[0] == 1


def test_invalid_submission_stores_nothing(db, cfg):
    row = feedback.submit("bug", "short", cfg=cfg)
    assert row["error"]
    assert database.read_df("SELECT * FROM feedback_reports").empty


def test_severity_is_dropped_for_suggestions(db, cfg):
    """Severity is a bug-report concept; a 'high severity suggestion' would
    sort into the wrong queue."""
    feedback.submit("suggestion", "It would be good to have a print view.",
                    severity="high", cfg=cfg)
    assert database.read_df(
        "SELECT severity FROM feedback_reports").iloc[0]["severity"] is None


def test_long_message_is_truncated_not_rejected(db, cfg):
    row = feedback.submit("bug", "x" * (feedback.MAX_MESSAGE + 500), cfg=cfg)
    assert "error" not in row
    stored = database.read_df("SELECT message FROM feedback_reports")
    assert len(stored.iloc[0]["message"]) == feedback.MAX_MESSAGE


# ------------------------------------------------------------- rate limit --
def test_rate_limit_applies_per_network(db, cfg):
    for _ in range(5):
        assert "error" not in feedback.submit(
            "bug", "Another genuine problem report.",
            ip_prefix="203.0.113.0", cfg=cfg)
    blocked = feedback.submit("bug", "One report too many for this network.",
                              ip_prefix="203.0.113.0", cfg=cfg)
    assert "error" in blocked
    # A different network is unaffected — one abusive host must not lock
    # everyone else out.
    assert "error" not in feedback.submit(
        "bug", "A report from somewhere else entirely.",
        ip_prefix="198.51.100.0", cfg=cfg)


def test_rate_limit_can_be_disabled(db):
    cfg = {"feedback": {"recipient": "r@example.com", "email_enabled": False,
                        "max_per_hour": 0}, "smtp": {}}
    for _ in range(8):
        assert "error" not in feedback.submit(
            "bug", "Repeated but legitimate report.",
            ip_prefix="203.0.113.0", cfg=cfg)


# ----------------------------------------------------------------- e-mail --
def test_email_body_leads_with_the_reference(db, cfg):
    row = feedback.submit("bug", "Gauge chart shows the wrong units.",
                          subject="Wrong units", severity="medium",
                          reporter_name="Sam", reporter_email="sam@example.com",
                          page_path="/flood/station/foo",
                          ip_prefix="203.0.113.0", cfg=cfg)
    body = feedback.format_email(row)
    assert body.splitlines()[0] == "Reference: " + row["ref"]
    assert "Gauge chart shows the wrong units." in body
    assert "sam@example.com" in body
    assert "/flood/station/foo" in body
    assert "203.0.113.0" in body


def test_email_names_the_reporter_as_anonymous_when_unknown(db, cfg):
    row = feedback.submit("suggestion", "A genuinely useful suggestion here.",
                          cfg=cfg)
    assert "Anonymous" in feedback.format_email(row)


def test_reply_to_is_the_reporter(db, sending_cfg, monkeypatch):
    """Replying to a report must answer the reporter, not the app's own
    from-address."""
    captured = {}

    def fake_send(subject, body, to_address=None, reply_to="", cfg=None):
        captured.update(subject=subject, to=to_address, reply_to=reply_to)
        return True, ""

    monkeypatch.setattr(mailer, "send", fake_send)
    row = feedback.submit("bug", "A reproducible problem, described.",
                          reporter_email="sam@example.com", cfg=sending_cfg)
    assert captured["reply_to"] == "sam@example.com"
    assert captured["to"] == "reports@example.com"
    assert row["ref"] in captured["subject"]


def test_message_headers(db):
    msg = mailer.build_message("Subject here", "Body here", "to@example.com",
                               "from@example.com", from_name="Watchdesk",
                               reply_to="sam@example.com")
    assert msg["To"] == "to@example.com"
    assert msg["From"] == "Watchdesk <from@example.com>"
    assert msg["Reply-To"] == "sam@example.com"
    assert msg["Message-ID"]
    assert msg.get_content().strip() == "Body here"


def test_mailer_reports_failure_instead_of_raising(db):
    ok, err = mailer.send("Subject", "Body", to_address="to@example.com",
                          cfg={"smtp": {}})
    assert ok is False
    assert "not configured" in err


def test_security_mode_follows_the_port():
    assert mailer._security({"port": 465, "security": "auto"}) == "ssl"
    assert mailer._security({"port": 587, "security": "auto"}) == "starttls"
    assert mailer._security({"port": 465, "security": "starttls"}) == "starttls"


# ----------------------------------------------------------------- queue ---
def test_status_transitions_and_counts(db, cfg):
    row = feedback.submit("bug", "Something worth triaging properly.", cfg=cfg)
    assert feedback.counts()["new"] == 1

    feedback.set_status(row["ref"], "closed")
    counts = feedback.counts()
    assert counts["new"] == 0 and counts["closed"] == 1

    # An unknown status must not be written — the queue filters depend on it.
    assert feedback.set_status(row["ref"], "banana") == 0
    assert feedback.counts()["closed"] == 1


def test_recent_filters_by_kind_and_status(db, cfg):
    feedback.submit("bug", "A bug that needs looking at.", cfg=cfg)
    s = feedback.submit("suggestion", "A suggestion worth considering.",
                        cfg=cfg)
    feedback.set_status(s["ref"], "closed")

    assert len(feedback.recent(kind="bug")) == 1
    assert len(feedback.recent(status="new")) == 1
    assert len(feedback.recent(status="all", kind="all")) == 2


def test_resend_delivers_a_stored_report(db, cfg, sending_cfg, monkeypatch):
    row = feedback.submit("bug", "Stored while the mail server was down.",
                          cfg=cfg)
    assert row["email_status"] == "skipped"

    monkeypatch.setattr(mailer, "send", lambda *a, **k: (True, ""))
    ok, _ = feedback.resend(row["ref"], cfg=sending_cfg)
    assert ok
    assert database.read_df(
        "SELECT email_status FROM feedback_reports"
    ).iloc[0]["email_status"] == "sent"


def test_resend_of_an_unknown_reference_is_reported_not_raised(db, cfg):
    ok, message = feedback.resend("WD-BUG-260824-ZZZZ", cfg=cfg)
    assert ok is False
    assert "WD-BUG-260824-ZZZZ" in message


# ------------------------------------------------------------ round-trip --
def test_null_columns_do_not_render_as_nan(db, cfg):
    """A NULL TEXT column comes back from pandas as NaN, which is TRUTHY —
    so an un-normalised `value or default` renders the string "nan" where the
    fallback belongs. This is the same trap as the collectors' NaT handling."""
    feedback.submit("bug", "Filed without leaving a name or a subject.",
                    cfg=cfg)
    row = feedback.normalise(feedback.recent().iloc[0].to_dict())

    assert row["reporter_name"] is None
    assert row["severity"] is None
    assert (row["reporter_name"] or "Anonymous") == "Anonymous"


def test_resend_of_an_anonymous_report_has_no_reply_to(db, cfg, sending_cfg,
                                                       monkeypatch):
    """Resend reads the row back through pandas, so a NULL reporter_email must
    not become the string "nan" in a Reply-To header."""
    row = feedback.submit("bug", "Anonymous but perfectly valid report.",
                          cfg=cfg)
    captured = {}

    def fake_send(subject, body, to_address=None, reply_to="", cfg=None):
        captured["reply_to"] = reply_to
        return True, ""

    monkeypatch.setattr(mailer, "send", fake_send)
    feedback.resend(row["ref"], cfg=sending_cfg)
    assert captured["reply_to"] == ""

"""Bug reports and suggestions: reference IDs, storage, and email delivery.

UI-free, like ``briefing`` and ``replay`` — the shell widget in
``app/feedback_ui.py`` and the Admin page both render from here.

Order of operations matters. :func:`submit` writes the row FIRST and mails
SECOND, updating ``email_status`` afterwards. A report therefore survives an
unconfigured, throttled or broken SMTP server: the reporter still gets their
reference, the row is still in the database, and the Admin page can retry the
delivery. Doing it the other way round — mail, then store — loses submissions
exactly when the system is already unhealthy, which is when bug reports matter
most.

Reference IDs look like ``WD-BUG-260824-4XKQ``: kind, the submission date, and
four random characters from an alphabet with no I/O/0/1, so a reference read
off a screen and typed into an email survives the trip. They are short enough
to quote in conversation and carry no sequence number, so they leak nothing
about volume.
"""
import datetime
import logging
import random
import re

from app import database, mailer
from app.config import load_config

log = logging.getLogger(__name__)

KINDS = {"bug": "Bug report", "suggestion": "Suggestion"}
SEVERITIES = ["low", "medium", "high"]
STATUSES = ["new", "open", "closed"]

# No I, O, 0 or 1 — the four characters people reliably mistype.
_REF_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_REF_PREFIX = {"bug": "BUG", "suggestion": "SUG"}

# Field caps. The form enforces its own limits, but this is the boundary that
# actually matters: the values arrive from a public page.
MAX_SUBJECT = 150
MAX_MESSAGE = 5000
MAX_NAME = 100
MAX_EMAIL = 200

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _now():
    return datetime.datetime.now()


def make_ref(kind, when=None):
    """A fresh reference ID. Uniqueness is enforced by the table's UNIQUE
    index; :func:`submit` retries on the (vanishingly rare) collision."""
    when = when or _now()
    suffix = "".join(random.choice(_REF_ALPHABET) for _ in range(4))
    return "WD-%s-%s-%s" % (_REF_PREFIX.get(kind, "MSG"),
                            when.strftime("%y%m%d"), suffix)


def valid_email(value):
    """Loose sanity check. The address is optional and only used for Reply-To,
    so the job is to reject obvious typos, not to prove deliverability."""
    return bool(_EMAIL_RE.match((value or "").strip()))


def validate(kind, message, reporter_email=""):
    """Returns an error string for the reporter, or "" when the form is good."""
    if kind not in KINDS:
        return "Choose whether this is a bug report or a suggestion."
    if not (message or "").strip():
        return "Please describe the %s." % (
            "problem" if kind == "bug" else "suggestion")
    if len((message or "").strip()) < 10:
        return "Please add a little more detail (at least 10 characters)."
    if reporter_email and not valid_email(reporter_email):
        return "That email address does not look right."
    return ""


def _clean(value, limit):
    return (str(value).strip()[:limit]) if value else None


def rate_limited(ip_prefix, cfg=None):
    """True when this network has already submitted its hourly allowance.

    The form is on a public page with no login, so something has to bound it.
    Counting per truncated IP means one abusive host is throttled while a whole
    office behind one NAT still gets a workable allowance (default 5/hour). A
    counting failure is never allowed to block a legitimate report."""
    cfg = cfg or load_config()
    limit = int(cfg.get("feedback", {}).get("max_per_hour", 5) or 0)
    if limit <= 0 or not ip_prefix:
        return False
    since = (_now() - datetime.timedelta(hours=1)).isoformat(
        sep=" ", timespec="seconds")
    try:
        df = database.read_df(
            "SELECT COUNT(*) AS n FROM feedback_reports "
            "WHERE ip_prefix = ? AND submitted_at >= ?", [ip_prefix, since])
    except Exception:
        log.debug("rate-limit check failed", exc_info=True)
        return False
    return int(df.iloc[0]["n"] or 0) >= limit


def normalise(row):
    """Turn a row read back through pandas into plain Python values.

    A NULL TEXT column arrives as **NaN, which is truthy**, so
    ``row.get("reporter_name") or "Anonymous"`` renders the literal string
    "nan" — the same trap as the pandas-NaT normalisation the collectors do.
    NaN is the only value that is not equal to itself, which is what makes the
    check work without dragging pandas in here."""
    return {k: (None if v is None or v != v else v) for k, v in row.items()}


def recipient(cfg=None):
    """Where reports are emailed. ``feedback.recipient`` overrides the general
    ``smtp.to_address``, so the app can mail alerts and reports to different
    mailboxes."""
    cfg = cfg or load_config()
    return ((cfg.get("feedback", {}).get("recipient") or "").strip()
            or (cfg.get("smtp", {}).get("to_address") or "").strip())


def format_email(row):
    """The plain-text body of a report email. The reference is on the first
    line so it survives a mail client's preview snippet."""
    lines = [
        "Reference: %s" % row["ref"],
        "Type:      %s" % KINDS.get(row["kind"], row["kind"]),
    ]
    if row.get("severity"):
        lines.append("Severity:  %s" % row["severity"].title())
    lines += [
        "Submitted: %s" % row["submitted_at"],
        "Page:      %s" % (row.get("page_path") or "unknown"),
    ]
    who = row.get("reporter_name") or "Anonymous"
    if row.get("reporter_email"):
        who = "%s <%s>" % (who, row["reporter_email"])
    lines.append("From:      %s" % who)
    if row.get("ip_prefix"):
        lines.append("Network:   %s (truncated)" % row["ip_prefix"])
    if row.get("user_agent"):
        lines.append("Browser:   %s" % row["user_agent"])
    lines += ["", "-" * 60, "", row.get("subject") or "(no subject)", "",
              row["message"], "", "-" * 60, "",
              "Sent by Watchdesk. Reply to this email to answer the reporter "
              "directly (if they left an address)."]
    return "\n".join(lines)


def submit(kind, message, subject="", reporter_name="", reporter_email="",
           severity="", page_path="", user_agent="", ip_prefix="", cfg=None):
    """Store a report and try to email it. Returns the stored row as a dict.

    ``row['error']`` is set for a rejected submission (nothing is stored);
    otherwise ``row['ref']`` is the reference to show the reporter and
    ``row['email_status']`` says whether the mail got out."""
    kind = (kind or "").strip().lower()
    error = validate(kind, message, reporter_email)
    if error:
        return {"error": error}

    cfg = cfg or load_config()
    if rate_limited(ip_prefix, cfg):
        return {"error": "You have sent several reports in the last hour. "
                         "Please wait a little before sending another."}

    now = _now()
    row = {
        "kind": kind,
        "submitted_at": now.isoformat(sep=" ", timespec="seconds"),
        "subject": _clean(subject, MAX_SUBJECT),
        "message": str(message).strip()[:MAX_MESSAGE],
        "reporter_name": _clean(reporter_name, MAX_NAME),
        "reporter_email": _clean(reporter_email, MAX_EMAIL),
        "severity": severity if severity in SEVERITIES else None,
        "page_path": _clean(page_path, 200),
        "user_agent": _clean(user_agent, 300),
        "ip_prefix": _clean(ip_prefix, 60),
        "email_status": "pending",
        "status": "new",
    }
    if kind != "bug":
        row["severity"] = None

    # Retry only the reference: a genuine insert failure must surface.
    for attempt in range(5):
        row["ref"] = make_ref(kind, now)
        try:
            database.insert_rows("feedback_reports", [row])
            break
        except Exception as e:
            if "UNIQUE" in str(e).upper() and attempt < 4:
                continue
            log.exception("Could not store feedback report")
            return {"error": "Sorry — the report could not be saved. "
                             "Please try again in a moment."}

    status, err = _deliver(row, cfg)
    row["email_status"], row["email_error"] = status, err
    return row


def _deliver(row, cfg):
    """Email one stored report and write the outcome back to its row."""
    to_address = recipient(cfg)
    if not cfg.get("feedback", {}).get("email_enabled", True):
        status, err = "skipped", "Email delivery is turned off in Settings."
    elif not to_address:
        status, err = "skipped", "No feedback recipient address configured."
    elif not mailer.configured(cfg):
        status, err = "skipped", "SMTP is not configured."
    else:
        subject_line = "[Watchdesk %s] %s — %s" % (
            KINDS.get(row["kind"], row["kind"]), row["ref"],
            row.get("subject") or "no subject")
        ok, err = mailer.send(subject_line, format_email(row),
                              to_address=to_address,
                              reply_to=row.get("reporter_email") or "", cfg=cfg)
        status = "sent" if ok else "failed"

    try:
        database.execute(
            "UPDATE feedback_reports SET email_status = ?, email_error = ? "
            "WHERE ref = ?", [status, err or None, row["ref"]])
    except Exception:
        log.debug("Could not record email status for %s", row["ref"],
                  exc_info=True)
    if status != "sent":
        log.warning("Report %s stored but not emailed (%s): %s",
                    row["ref"], status, err)
    return status, err


def resend(ref, cfg=None):
    """Retry delivery of an already-stored report (Admin page). Returns
    ``(ok, message)``."""
    cfg = cfg or load_config()
    df = database.read_df("SELECT * FROM feedback_reports WHERE ref = ?", [ref])
    if df.empty:
        return False, "No report with reference %s." % ref
    status, err = _deliver(normalise(df.iloc[0].to_dict()), cfg)
    if status == "sent":
        return True, "Emailed %s to %s." % (ref, recipient(cfg))
    return False, "%s not sent (%s): %s" % (ref, status, err)


def recent(limit=50, status=None, kind=None):
    """Most recent reports, newest first, for the Admin list."""
    where, params = [], []
    if status and status != "all":
        where.append("status = ?")
        params.append(status)
    if kind and kind != "all":
        where.append("kind = ?")
        params.append(kind)
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    params.append(int(limit))
    return database.read_df(
        "SELECT ref, kind, severity, submitted_at, subject, message, "
        "reporter_name, reporter_email, page_path, email_status, email_error, "
        "status FROM feedback_reports %s "
        "ORDER BY submitted_at DESC, id DESC LIMIT ?" % clause, params)


def set_status(ref, status):
    if status not in STATUSES:
        return 0
    return database.execute(
        "UPDATE feedback_reports SET status = ? WHERE ref = ?", [status, ref])


def counts():
    """Report counts by status, for the Admin heading."""
    df = database.read_df(
        "SELECT status, COUNT(*) AS n FROM feedback_reports GROUP BY status")
    out = {s: 0 for s in STATUSES}
    for _, r in df.iterrows():
        out[str(r["status"])] = int(r["n"])
    out["total"] = int(df["n"].sum()) if not df.empty else 0
    return out

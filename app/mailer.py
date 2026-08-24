"""Outbound email over SMTP (stdlib only — no new dependency).

Used by the feedback form to deliver bug reports and suggestions. Settings live
under ``smtp`` in config.json (written from the Settings page), except the
password, which may also come from the ``UM_SMTP_PASSWORD`` environment
variable — preferred for a container deployment, where config.json sits on a
mounted volume.

Transport security follows the port unless overridden: 465 implies implicit TLS
(SMTPS), anything else STARTTLS. Both are on by default because every mail host
worth using requires one of them; ``smtp.security = "none"`` exists for a
local relay on localhost and is not a sensible choice over the open internet.

:func:`send` never raises. It returns ``(ok, error)`` so the caller can record
the outcome — the feedback form stores the report first and mails second, so a
misconfigured server costs a notification, never a submission.
"""
import logging
import os
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

from app.config import load_config

log = logging.getLogger(__name__)

# Timeout for connect + handshake + send. Short enough that a dead mail host
# cannot wedge the request thread behind a Dash callback.
TIMEOUT_SECONDS = 20


def password(cfg=None):
    """SMTP password, environment first so a deployment need not put it in
    config.json."""
    env = os.environ.get("UM_SMTP_PASSWORD")
    if env:
        return env
    cfg = cfg or load_config()
    return cfg.get("smtp", {}).get("password") or ""


def configured(cfg=None):
    """True when there is enough configuration to attempt a send. The password
    is not required — an unauthenticated local relay is legitimate."""
    cfg = cfg or load_config()
    scfg = cfg.get("smtp", {})
    return bool((scfg.get("host") or "").strip()
                and (scfg.get("from_address") or "").strip())


def _security(scfg):
    mode = (scfg.get("security") or "auto").lower()
    if mode != "auto":
        return mode
    return "ssl" if int(scfg.get("port") or 587) == 465 else "starttls"


def build_message(subject, body, to_address, from_address, from_name="",
                  reply_to=""):
    """An RFC-5322 message. Split out from :func:`send` so tests can assert on
    the headers without a live SMTP server."""
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = ("%s <%s>" % (from_name, from_address)) if from_name else from_address
    msg["To"] = to_address
    msg["Date"] = formatdate(localtime=True)
    # An explicit Message-ID keeps replies threading correctly; without one the
    # sending host invents its own and some hosts refuse the message entirely.
    msg["Message-ID"] = make_msgid(domain=from_address.split("@")[-1] or None)
    if reply_to:
        # So hitting Reply on a bug report answers the reporter, not the app.
        msg["Reply-To"] = reply_to
    msg.set_content(body)
    return msg


def send(subject, body, to_address=None, reply_to="", cfg=None):
    """Send one plain-text email. Returns ``(ok, error_message)``.

    Never raises: an SMTP problem is the caller's data, not their exception."""
    cfg = cfg or load_config()
    scfg = cfg.get("smtp", {})
    to_address = (to_address or scfg.get("to_address") or "").strip()
    from_address = (scfg.get("from_address") or "").strip()
    host = (scfg.get("host") or "").strip()

    if not host or not from_address:
        return False, "SMTP is not configured (host and from-address required)."
    if not to_address:
        return False, "No recipient address configured."

    port = int(scfg.get("port") or 587)
    mode = _security(scfg)
    user = (scfg.get("username") or "").strip()
    pw = password(cfg)

    msg = build_message(subject, body, to_address, from_address,
                        from_name=scfg.get("from_name") or "", reply_to=reply_to)
    try:
        context = ssl.create_default_context()
        if mode == "ssl":
            server = smtplib.SMTP_SSL(host, port, timeout=TIMEOUT_SECONDS,
                                      context=context)
        else:
            server = smtplib.SMTP(host, port, timeout=TIMEOUT_SECONDS)
        with server:
            server.ehlo()
            if mode == "starttls":
                server.starttls(context=context)
                server.ehlo()
            if user:
                server.login(user, pw)
            server.send_message(msg)
        log.info("Email sent to %s (%s)", to_address, subject)
        return True, ""
    except (smtplib.SMTPException, OSError, ssl.SSLError) as e:
        log.warning("Email send failed (%s): %s", subject, e)
        return False, "%s: %s" % (type(e).__name__, e)

"""Lightweight, privacy-preserving page analytics.

Page views are logged from the URL-change callback (the app is a Dash SPA, so
there's no full-page GET per navigation). We never store a raw IP or any PII:
a visitor is identified only by a **daily salted hash** of IP + User-Agent, so
unique-visitor counts work while the identifier can't be reversed and rotates
every day. Logging is best-effort and must never break navigation.
"""
import datetime
import hashlib
import logging
import os

import flask

from app import database

log = logging.getLogger(__name__)

# Paths not worth counting as page views.
_IGNORE_PREFIXES = ("/_dash", "/assets", "/health", "/favicon")


def _client_ip():
    req = flask.request
    # Behind Caddy the real client is in X-Forwarded-For; fall back to peer.
    fwd = req.headers.get("X-Forwarded-For", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return req.remote_addr or ""


def _visitor_hash():
    ua = flask.request.headers.get("User-Agent", "")
    day = datetime.date.today().isoformat()
    salt = os.environ.get("UM_SECRET_KEY", "pm-analytics-salt")
    raw = f"{_client_ip()}|{ua}|{day}|{salt}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def record_view(pathname, is_admin=False):
    """Record one page view. Silently no-ops on any error or ignorable path."""
    if not pathname or pathname.startswith(_IGNORE_PREFIXES):
        return
    try:
        database.insert_rows("page_views", [{
            "timestamp": datetime.datetime.now().isoformat(sep=" ", timespec="seconds"),
            "path": str(pathname)[:200],
            "visitor_hash": _visitor_hash(),
            "is_admin": 1 if is_admin else 0,
        }])
    except Exception:  # analytics is never allowed to break the page
        log.debug("page view not recorded", exc_info=True)


def _since(hours=None, days=None):
    delta = datetime.timedelta(hours=hours or 0, days=days or 0)
    return (datetime.datetime.now() - delta).isoformat(sep=" ", timespec="seconds")


def summary():
    """Views + unique visitors over 24h / 7d / 30d, for KPI cards."""
    out = {}
    for label, kw in (("24h", {"hours": 24}), ("7d", {"days": 7}),
                      ("30d", {"days": 30})):
        df = database.read_df(
            "SELECT COUNT(*) AS views, COUNT(DISTINCT visitor_hash) AS visitors "
            "FROM page_views WHERE timestamp >= ?", [_since(**kw)])
        out[label] = {"views": int(df.iloc[0]["views"] or 0),
                      "visitors": int(df.iloc[0]["visitors"] or 0)}
    return out


def views_by_day(days=30):
    """Daily views + unique visitors for the trend chart."""
    df = database.read_df(
        "SELECT substr(timestamp, 1, 10) AS day, COUNT(*) AS views, "
        "COUNT(DISTINCT visitor_hash) AS visitors FROM page_views "
        "WHERE timestamp >= ? GROUP BY day ORDER BY day", [_since(days=days)])
    return df


def top_pages(days=7, limit=12):
    """Most-viewed paths over the window."""
    return database.read_df(
        "SELECT path, COUNT(*) AS views, COUNT(DISTINCT visitor_hash) AS visitors "
        "FROM page_views WHERE timestamp >= ? AND is_admin = 0 "
        "GROUP BY path ORDER BY views DESC LIMIT ?", [_since(days=days), limit])

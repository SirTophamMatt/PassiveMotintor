"""Lightweight page analytics: views, unique visitors, and visitor origin.

Page views are logged from the URL-change callback (the app is a Dash SPA, so
there's no full-page GET per navigation). Logging is best-effort and must never
break navigation.

Each view stores three things about the visitor:

- ``visitor_hash`` — a **daily salted hash** of IP + User-Agent, so
  unique-visitor counts work while the identifier can't be reversed and rotates
  every day.
- ``ip_prefix`` — the client address with its host part removed (IPv4 /24,
  IPv6 /48). Full addresses are never written to disk; see ``app/geoip.py``
  for why that line is drawn there.
- ``country`` / ``region`` / ``city`` — resolved from the prefix through a
  cached background lookup, so the first view from a new network stores no
  location and every later one does.
"""
import datetime
import hashlib
import logging
import os

import flask

from app import database, geoip

log = logging.getLogger(__name__)

# Paths not worth counting as page views.
_IGNORE_PREFIXES = ("/_dash", "/assets", "/health", "/favicon")


def client_ip():
    """The requesting client's address, as a string.

    Behind Caddy the real client is the FIRST entry in X-Forwarded-For (the
    rest of the list is proxies); fall back to the socket peer when the header
    is absent. The value is untrusted — a client can send whatever it likes in
    that header — so everything downstream either hashes it or runs it through
    ``geoip.truncate``, which rejects anything that isn't a real address."""
    req = flask.request
    fwd = req.headers.get("X-Forwarded-For", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return req.remote_addr or ""


def _visitor_hash():
    ua = flask.request.headers.get("User-Agent", "")
    day = datetime.date.today().isoformat()
    salt = os.environ.get("UM_SECRET_KEY", "pm-analytics-salt")
    raw = f"{client_ip()}|{ua}|{day}|{salt}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def record_view(pathname, is_admin=False):
    """Record one page view. Silently no-ops on any error or ignorable path."""
    if not pathname or pathname.startswith(_IGNORE_PREFIXES):
        return
    try:
        prefix = geoip.truncate(client_ip())
        # Cached hit or nothing: resolve_async queues a miss for the background
        # worker rather than putting an HTTP call in the render path.
        row = {
            "timestamp": datetime.datetime.now().isoformat(sep=" ", timespec="seconds"),
            "path": str(pathname)[:200],
            "visitor_hash": _visitor_hash(),
            "is_admin": 1 if is_admin else 0,
            "ip_prefix": prefix or None,
        }
        row.update(geoip.resolve_async(prefix))
        database.insert_rows("page_views", [row])
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


def by_country(days=30, limit=15):
    """Visitors and views by country. Rows with no resolved location are
    reported as "Unknown" rather than dropped — a chart that silently omits
    them overstates how much of the audience is actually located."""
    return database.read_df(
        "SELECT COALESCE(NULLIF(country, ''), 'Unknown') AS country, "
        "COUNT(*) AS views, COUNT(DISTINCT visitor_hash) AS visitors "
        "FROM page_views WHERE timestamp >= ? AND is_admin = 0 "
        "GROUP BY 1 ORDER BY visitors DESC, views DESC LIMIT ?",
        [_since(days=days), limit])


def by_city(days=30, limit=20):
    """Visitors by city, with their country and state for disambiguation
    (there are Richmonds in three Australian states alone)."""
    return database.read_df(
        "SELECT city, region, country, "
        "COUNT(*) AS views, COUNT(DISTINCT visitor_hash) AS visitors "
        "FROM page_views "
        "WHERE timestamp >= ? AND is_admin = 0 AND city IS NOT NULL AND city != '' "
        "GROUP BY city, region, country ORDER BY visitors DESC, views DESC LIMIT ?",
        [_since(days=days), limit])


def located_points(days=30, limit=500):
    """Coordinates for the visitor map, joined from the geo cache.

    The point is the CENTRE OF THE NETWORK's city, not a visitor's position —
    a truncated IP cannot locate anyone more precisely than that, and the map
    label says so."""
    return database.read_df(
        "SELECT g.latitude, g.longitude, g.city, g.region, g.country, "
        "COUNT(*) AS views, COUNT(DISTINCT v.visitor_hash) AS visitors "
        "FROM page_views v JOIN ip_geo_cache g ON g.ip_prefix = v.ip_prefix "
        "WHERE v.timestamp >= ? AND v.is_admin = 0 AND g.latitude IS NOT NULL "
        "GROUP BY g.ip_prefix ORDER BY visitors DESC LIMIT ?",
        [_since(days=days), limit])


def top_networks(days=30, limit=15):
    """Busiest truncated networks, with the ISP/organisation where known.

    Useful for the question the country chart cannot answer: is this traffic a
    crawler farm, or people?"""
    return database.read_df(
        "SELECT v.ip_prefix, g.org, g.city, g.country, "
        "COUNT(*) AS views, COUNT(DISTINCT v.visitor_hash) AS visitors "
        "FROM page_views v LEFT JOIN ip_geo_cache g ON g.ip_prefix = v.ip_prefix "
        "WHERE v.timestamp >= ? AND v.is_admin = 0 AND v.ip_prefix IS NOT NULL "
        "GROUP BY v.ip_prefix ORDER BY views DESC LIMIT ?",
        [_since(days=days), limit])


def location_coverage(days=30):
    """How many views carry a resolved location, so the geography charts can
    say what share of traffic they actually represent."""
    df = database.read_df(
        "SELECT COUNT(*) AS total, "
        "SUM(CASE WHEN country IS NOT NULL AND country != '' THEN 1 ELSE 0 END) "
        "AS located FROM page_views WHERE timestamp >= ? AND is_admin = 0",
        [_since(days=days)])
    total = int(df.iloc[0]["total"] or 0)
    located = int(df.iloc[0]["located"] or 0)
    return {"total": total, "located": located,
            "pct": (100.0 * located / total) if total else 0.0}

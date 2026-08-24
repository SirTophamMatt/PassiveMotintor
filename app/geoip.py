"""Coarse visitor geolocation from a truncated client IP.

What this deliberately does NOT do
----------------------------------
Store a full IP address. Every address is passed through :func:`truncate`
first — IPv4 keeps three octets (203.0.113.47 -> 203.0.113.0), IPv6 keeps the
routing prefix (/48). That is the level GA and Matomo anonymise at: enough to
resolve a city and to group traffic by network, not enough to identify a
subscriber. The truncated value is what gets written to
``page_views.ip_prefix``, what gets sent to the geolocation provider, and what
keys the cache.

How the lookup runs
-------------------
Never in the render path. ``analytics.record_view`` calls
:func:`resolve_async`, which returns whatever is already cached (usually a hit
— one lookup covers a whole /24 indefinitely) and, on a miss, hands the work to
a single background worker thread. So the first view from a new network stores
a NULL location and every later one carries it. A failure is cached as
``status='failed'`` and not retried for RETRY_AFTER_HOURS, so a provider outage
costs one request per network per day rather than one per page view.

The provider is ``geo.provider_url`` in config (default ip-api.com — no key,
45 requests/minute, HTTP-only and non-commercial on the free tier). Its
response is mapped through :data:`FIELD_MAP`; point the URL at another JSON
endpoint and adjust that map to switch providers. Set ``geo.enabled`` false to
store the prefix and nothing else.
"""
import datetime
import ipaddress
import logging
import queue
import threading

import requests

from app import database
from app.config import load_config

log = logging.getLogger(__name__)

# Provider JSON key -> our column.
FIELD_MAP = {
    "country": "country",
    "countryCode": "country_code",
    "regionName": "region",
    "city": "city",
    "lat": "latitude",
    "lon": "longitude",
    "org": "org",
}

# How long a failed lookup is remembered. Long enough that a provider outage
# cannot become a request storm, short enough that a network which was
# unresolvable yesterday gets another chance.
RETRY_AFTER_HOURS = 24

# Bounded, so a burst of traffic from many new networks cannot grow the queue
# without limit. Overflow is dropped; the next view from that network requeues.
_QUEUE_MAX = 500
_queue = queue.Queue(maxsize=_QUEUE_MAX)
_worker = None
_worker_lock = threading.Lock()

LOCATION_COLUMNS = ("country", "country_code", "region", "city")


def truncate(ip):
    """Drop the host part of an address, keeping only the network.

    IPv4 -> /24 (203.0.113.47 becomes 203.0.113.0); IPv6 -> /48. Returns "" for
    anything unparseable, so a spoofed X-Forwarded-For header cannot inject
    arbitrary text into the database or into a provider URL."""
    if not ip:
        return ""
    try:
        addr = ipaddress.ip_address(str(ip).strip())
    except ValueError:
        return ""
    prefix = 24 if addr.version == 4 else 48
    net = ipaddress.ip_network("%s/%d" % (addr, prefix), strict=False)
    return str(net.network_address)


def is_public(ip_prefix):
    """False for loopback / private / link-local ranges — there is nothing to
    look up for a LAN address, and the desktop build is entirely localhost."""
    try:
        addr = ipaddress.ip_address(ip_prefix)
    except ValueError:
        return False
    return not (addr.is_private or addr.is_loopback or addr.is_link_local
                or addr.is_reserved or addr.is_multicast)


def enabled(cfg=None):
    cfg = cfg or load_config()
    return bool(cfg.get("geo", {}).get("enabled", True))


def _now():
    return datetime.datetime.now().isoformat(sep=" ", timespec="seconds")


def cached(ip_prefix):
    """The cache row for a prefix as a dict, or None. Never raises."""
    if not ip_prefix:
        return None
    try:
        df = database.read_df(
            "SELECT * FROM ip_geo_cache WHERE ip_prefix = ?", [ip_prefix])
    except Exception:
        log.debug("geo cache read failed", exc_info=True)
        return None
    if df.empty:
        return None
    return df.iloc[0].to_dict()


def _is_fresh(row):
    """Whether a cache row can be used as-is. Successful and private-range
    results are permanent; failures expire so they get another chance."""
    if row is None:
        return False
    if row.get("status") != "failed":
        return True
    try:
        age = datetime.datetime.now() - datetime.datetime.fromisoformat(
            str(row.get("looked_up_at")))
    except (TypeError, ValueError):
        return False
    return age < datetime.timedelta(hours=RETRY_AFTER_HOURS)


def _store(ip_prefix, status, fields=None):
    row = {"ip_prefix": ip_prefix, "status": status, "looked_up_at": _now()}
    row.update(fields or {})
    cols = ", ".join(row)
    placeholders = ", ".join("?" for _ in row)
    sql = "INSERT OR REPLACE INTO ip_geo_cache (%s) VALUES (%s)" % (
        cols, placeholders)
    try:
        database.execute(sql, list(row.values()))
    except Exception:
        log.debug("geo cache write failed", exc_info=True)


def lookup(ip_prefix, cfg=None):
    """Resolve a prefix through the provider and cache the result.

    BLOCKING — call it from the worker thread or a test, never from a Dash
    callback. Returns the resolved fields, or None if the lookup failed."""
    cfg = cfg or load_config()
    gcfg = cfg.get("geo", {})
    url = (gcfg.get("provider_url") or "").strip()
    if not url:
        _store(ip_prefix, "failed")
        return None
    try:
        resp = requests.get(url.replace("{ip}", ip_prefix),
                            timeout=float(gcfg.get("timeout_seconds", 5)))
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as e:
        log.info("Geolocation lookup failed for %s: %s", ip_prefix, e)
        _store(ip_prefix, "failed")
        return None

    # ip-api.com signals a miss in the body with 200 OK, not an HTTP error.
    if str(data.get("status", "success")).lower() == "fail":
        log.debug("Geolocation miss for %s: %s", ip_prefix, data.get("message"))
        _store(ip_prefix, "failed")
        return None

    fields = {col: data.get(key) for key, col in FIELD_MAP.items()
              if data.get(key) not in (None, "")}
    _store(ip_prefix, "ok", fields)
    return fields


def _run_worker():
    while True:
        ip_prefix = _queue.get()
        try:
            if not _is_fresh(cached(ip_prefix)):
                lookup(ip_prefix)
        except Exception:  # a worker crash would silently end all lookups
            log.exception("Geolocation worker failed for %s", ip_prefix)
        finally:
            _queue.task_done()


def _ensure_worker():
    global _worker
    with _worker_lock:
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_run_worker, daemon=True,
                                       name="geoip-worker")
            _worker.start()


def resolve_async(ip_prefix, cfg=None):
    """Location for a prefix if it is already known, queueing a lookup if not.

    Returns a dict of location columns (possibly empty) and never blocks, so it
    is safe to call from the page-view path."""
    if not ip_prefix or not enabled(cfg):
        return {}
    if not is_public(ip_prefix):
        # Cached so a localhost/LAN deployment stops asking on every view.
        if cached(ip_prefix) is None:
            _store(ip_prefix, "private")
        return {}

    row = cached(ip_prefix)
    if _is_fresh(row):
        if row.get("status") != "ok":
            return {}
        return {c: row.get(c) for c in LOCATION_COLUMNS if row.get(c)}

    _ensure_worker()
    try:
        _queue.put_nowait(ip_prefix)
    except queue.Full:
        log.debug("Geolocation queue full; skipping %s", ip_prefix)
    return {}

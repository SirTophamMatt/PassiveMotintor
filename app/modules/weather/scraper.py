"""BoM weather scraper (api.weather.bom.gov.au).

Each cycle fetches current warnings for Victoria and upserts them on their BoM
id (a warning that drops out of the feed or passes its expiry is marked
inactive, not deleted), then writes a continuity heartbeat. Rainfall collection
(per monitored location) is added in a later slice; the location plumbing lives
here so the schema and cycle are ready for it.

The API is the public JSON backing the BoM website — no auth, but undocumented,
so every field access is defensive.
"""
import json
import logging
import urllib.request
from datetime import datetime

from app import database

log = logging.getLogger(__name__)

BASE = "https://api.weather.bom.gov.au/v1"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
           "Accept": "application/json"}
STATE = "VIC"

# Fields updated/inserted on weather_warnings (warning_id handled separately).
_WARN_FIELDS = ["type", "title", "short_title", "group_type", "phase", "state",
                "issue_time", "expiry_time"]


def _get(path):
    """GET a JSON path under BASE, or None on any failure."""
    try:
        req = urllib.request.Request(BASE + path, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=25) as resp:
            return json.loads(resp.read())
    except Exception as e:  # network / JSON / HTTP — never fatal to a cycle
        log.warning("BoM API GET %s failed: %s", path, e)
        return None


def _parse_dt(value):
    """ISO-8601 (with 'Z' or offset) -> local 'YYYY-MM-DD HH:MM:SS' string, one
    uniform precision (avoids the pandas mixed-format NaT trap). None on failure."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone().replace(tzinfo=None)
    return dt.isoformat(sep=" ", timespec="seconds")


def _warning_row(w):
    return {
        "warning_id": str(w.get("id")) if w.get("id") else None,
        "type": w.get("type"),
        "title": w.get("title"),
        "short_title": w.get("short_title"),
        "group_type": w.get("warning_group_type"),
        "phase": w.get("phase"),
        "state": w.get("state"),
        "issue_time": _parse_dt(w.get("issue_time")),
        "expiry_time": _parse_dt(w.get("expiry_time")),
    }


def _upsert_warning(row, now):
    set_clause = ", ".join(f"{f}=?" for f in _WARN_FIELDS)
    changed = database.execute(
        f"UPDATE weather_warnings SET {set_clause}, last_seen=?, active=1 "
        "WHERE warning_id=?",
        [row[f] for f in _WARN_FIELDS] + [now, row["warning_id"]])
    if changed:
        return False
    cols = ["warning_id"] + _WARN_FIELDS + ["first_seen", "last_seen", "active"]
    database.execute(
        f"INSERT INTO weather_warnings ({', '.join(cols)}) "
        f"VALUES ({', '.join('?' for _ in cols)})",
        [row["warning_id"]] + [row[f] for f in _WARN_FIELDS] + [now, now, 1])
    return True


def fetch_warnings(now):
    """Fetch current VIC warnings, upsert them, retire the ones no longer live.
    Returns (active_count, new_count)."""
    payload = _get("/warnings")
    if not payload or "data" not in payload:
        return None, 0
    rows = [_warning_row(w) for w in payload["data"]
            if w.get("state") == STATE and w.get("id")]
    new = 0
    for row in rows:
        if _upsert_warning(row, now):
            new += 1
    seen = [r["warning_id"] for r in rows]
    if seen:
        placeholders = ", ".join("?" for _ in seen)
        database.execute(
            f"UPDATE weather_warnings SET active=0, last_seen=? "
            f"WHERE active=1 AND warning_id NOT IN ({placeholders})",
            [now] + seen)
    else:
        database.execute(
            "UPDATE weather_warnings SET active=0, last_seen=? WHERE active=1", [now])
    return len(rows), new


def fetch_weather_data():
    """Run a weather collection cycle (warnings now; rainfall in a later slice),
    write a heartbeat, and return the number of new warnings."""
    now = datetime.now().isoformat(sep=" ", timespec="seconds")
    active, new = fetch_warnings(now)
    database.insert_rows("weather_heartbeat", [{
        "timestamp": now,
        "vic_warnings": active if active is not None else 0,
        "locations_polled": 0,
        "new_warnings": new,
    }])
    log.info("Weather fetch: %s active VIC warning(s), %d new",
             active if active is not None else "?", new)
    return new

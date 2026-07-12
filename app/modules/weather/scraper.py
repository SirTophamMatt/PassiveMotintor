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
import urllib.parse
import urllib.request
from datetime import datetime

from app import database
from app.config import load_config
from app.modules.weather import data as weather_data

log = logging.getLogger(__name__)

_GEOHASH_B32 = "0123456789bcdefghjkmnpqrstuvwxyz"

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


def _store_detail(warning_id, now):
    """Fetch a warning's full body and record it: update the latest message on
    weather_warnings and append a version row (unique per issue_time) so the
    warning's development can be replayed. The message HTML may embed base64
    images (e.g. severe-weather graphics)."""
    payload = _get(f"/warnings/{warning_id}")
    if not payload or "data" not in payload:
        return
    d = payload["data"]
    message = d.get("message")
    issue = _parse_dt(d.get("issue_time"))
    if message is not None:
        database.execute("UPDATE weather_warnings SET message=? WHERE warning_id=?",
                         [message, warning_id])
    if issue:  # only version rows with a real issue time (dedup key)
        database.insert_rows("weather_warning_updates", [{
            "warning_id": warning_id, "issue_time": issue, "phase": d.get("phase"),
            "title": d.get("title"), "message": message, "captured_at": now,
        }], ignore_duplicates=True)


def fetch_warnings(now):
    """Fetch current VIC warnings, upsert them, pull each one's full detail
    (text + version history), and retire the ones no longer live.
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
        _store_detail(row["warning_id"], now)
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


def _geohash_decode(gh):
    """Approximate (lat, lon) at the centre of a geohash cell — used for map
    markers so we don't need an extra API call per location."""
    lat = [-90.0, 90.0]
    lon = [-180.0, 180.0]
    even = True
    for ch in str(gh):
        try:
            cd = _GEOHASH_B32.index(ch)
        except ValueError:
            return None, None
        for mask in (16, 8, 4, 2, 1):
            if even:
                mid = (lon[0] + lon[1]) / 2
                lon[0 if cd & mask else 1] = mid
            else:
                mid = (lat[0] + lat[1]) / 2
                lat[0 if cd & mask else 1] = mid
            even = not even
    return (lat[0] + lat[1]) / 2, (lon[0] + lon[1]) / 2


def _resolve_location(town):
    """Resolve a town name to a BoM location {name, geohash, lat, lon}, or None."""
    payload = _get("/locations?search=" + urllib.parse.quote(town))
    if not payload or not payload.get("data"):
        return None
    results = payload["data"]
    pick = next((r for r in results if r.get("state") == STATE), results[0])
    gh = pick.get("geohash")
    if not gh:
        return None
    lat, lon = _geohash_decode(gh)
    return {"name": pick.get("name") or town, "geohash": gh,
            "latitude": lat, "longitude": lon}


def ensure_locations():
    """One-time seed of rainfall locations from the flood gauges' towns, resolved
    to BoM geohashes and cached in weather_locations. No-op once populated."""
    existing = database.read_df("SELECT COUNT(*) AS n FROM weather_locations")
    if not existing.empty and existing.iloc[0]["n"]:
        return 0
    gauges = database.read_df(
        "SELECT DISTINCT station_name, catchment FROM flood_observations "
        "WHERE station_name IS NOT NULL")
    if gauges.empty:
        return 0
    # Distinct towns (first catchment wins), capped for politeness.
    towns = {}
    for _, g in gauges.iterrows():
        town = weather_data.gauge_town(g["station_name"])
        if town and town.lower() not in towns:
            towns[town.lower()] = (town, g["catchment"])
    limit = load_config()["weather"].get("max_rainfall_locations", 40)
    added = 0
    for key, (town, catchment) in list(towns.items())[:limit]:
        loc = _resolve_location(town)
        if not loc:
            continue
        database.insert_rows("weather_locations", [{
            "location_key": key, "name": loc["name"], "geohash": loc["geohash"],
            "latitude": loc["latitude"], "longitude": loc["longitude"],
            "catchment": catchment,
        }], ignore_duplicates=True)
        added += 1
    log.info("Seeded %d rainfall location(s) from flood gauges", added)
    return added


def fetch_rainfall(now):
    """Poll rain-since-9am + today's rain forecast for each cached location.
    Returns the number of locations polled."""
    locs = database.read_df(
        "SELECT location_key, name, geohash, latitude, longitude "
        "FROM weather_locations WHERE geohash IS NOT NULL")
    if locs.empty:
        return 0
    rows = []
    for _, loc in locs.iterrows():
        gh6 = str(loc["geohash"])[:6]
        rain = fmax = fchance = None
        obs = _get(f"/locations/{gh6}/observations")
        if obs and obs.get("data"):
            rain = obs["data"].get("rain_since_9am")
        fc = _get(f"/locations/{gh6}/forecasts/daily")
        if fc and fc.get("data"):
            day0 = fc["data"][0] if fc["data"] else {}
            amount = (day0.get("rain") or {}).get("amount") or {}
            fmax = amount.get("max")
            fchance = (day0.get("rain") or {}).get("chance")
        rows.append({
            "location_key": loc["location_key"], "name": loc["name"],
            "latitude": loc["latitude"], "longitude": loc["longitude"],
            "rain_since_9am_mm": rain, "forecast_max_mm": fmax,
            "forecast_chance": fchance, "timestamp": now,
        })
    database.insert_rows("rainfall_observations", rows, ignore_duplicates=True)
    return len(rows)


def fetch_weather_data():
    """Run a weather collection cycle (warnings + rainfall), write a heartbeat,
    and return the number of new warnings."""
    now = datetime.now().isoformat(sep=" ", timespec="seconds")
    active, new = fetch_warnings(now)
    try:
        ensure_locations()
        polled = fetch_rainfall(now)
    except Exception:
        log.exception("Rainfall collection failed (non-fatal)")
        polled = 0
    database.insert_rows("weather_heartbeat", [{
        "timestamp": now,
        "vic_warnings": active if active is not None else 0,
        "locations_polled": polled,
        "new_warnings": new,
    }])
    log.info("Weather fetch: %s active VIC warning(s), %d new; %d rainfall loc(s)",
             active if active is not None else "?", new, polled)
    return new

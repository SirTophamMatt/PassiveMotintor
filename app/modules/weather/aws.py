"""BoM AWS rainfall-network scraper.

Captures rain-since-9am for every Victorian Automatic Weather Station (~101) in
a single request to BoM's state observations page, so it is light on BoM (one
page/cycle, not 101 per-station calls). Each reading is stored against its BoM
observation time and de-duped, so polling more often than BoM updates (~30 min)
never adds duplicate rows — the data volume is set by BoM's cadence, not ours.

Station coordinates aren't on the state page, so they're back-filled a few at a
time from the per-station JSON into a small `aws_stations` registry.

Event totals are NOT stored as a running sum: the raw rain-since-9am counter is
kept, and totals for any window are computed from the positive increments (a
drop means the 9am reset fired), which is correct across any number of resets.
"""
import json
import logging
import re
import urllib.request
from datetime import datetime, timedelta

from bs4 import BeautifulSoup

from app import database

log = logging.getLogger(__name__)

VICALL_URL = "http://www.bom.gov.au/vic/observations/vicall.shtml"
STATION_JSON = "http://www.bom.gov.au/fwo/IDV60801/IDV60801.{wmo}.json"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

_DT_RE = re.compile(r"(\d{1,2})/(\d{1,2}):(\d{2})(am|pm)", re.I)
_WMO_RE = re.compile(r"IDV60801\.(\d+)\.shtml")
# Seed at most this many new stations' coordinates per cycle (registry fills in
# over the first few cycles rather than a 101-request burst on first run).
_COORD_SEED_PER_CYCLE = 25


def _fetch(url, timeout=30):
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except Exception as e:
        log.warning("AWS fetch %s failed: %s", url, e)
        return None


def _to_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_obs_time(text, now):
    """BoM shows the obs time as 'DD/HH:MMam' (no month/year). Resolve to a full
    local 'YYYY-MM-DD HH:MM' string, rolling back a month if the day is ahead of
    today. None if unparseable."""
    m = _DT_RE.match((text or "").strip())
    if not m:
        return None
    day, hh, mm, ap = int(m.group(1)), int(m.group(2)), int(m.group(3)), m.group(4).lower()
    hh = (hh % 12) + (12 if ap == "pm" else 0)
    try:
        cand = now.replace(day=day, hour=hh, minute=mm, second=0, microsecond=0)
    except ValueError:
        return None
    if cand > now + timedelta(days=1):  # day belongs to the previous month
        prev_month_end = now.replace(day=1) - timedelta(days=1)
        try:
            cand = cand.replace(year=prev_month_end.year, month=prev_month_end.month)
        except ValueError:
            return None
    return cand.isoformat(sep=" ", timespec="minutes")


def _parse_vicall(html):
    """Parse the state page into [{wmo, name, obs_time, rain_since_9am_mm}].
    Columns are selected by their `headers` id suffix (reorder-proof)."""
    soup = BeautifulSoup(html, "lxml")
    now = datetime.now()
    rows = []
    for tr in soup.find_all("tr"):
        th = tr.find("th")
        if not th:
            continue
        a = th.find("a", href=True)
        if not a:
            continue
        wmo_match = _WMO_RE.search(a["href"])
        if not wmo_match:
            continue
        dt_raw = rain_raw = None
        for td in tr.find_all("td"):
            hdrs = td.get("headers") or []
            if any(h.endswith("-datetime") for h in hdrs):
                dt_raw = td.get_text(strip=True)
            elif any(h.endswith("-rainsince9am") for h in hdrs):
                rain_raw = td.get_text(strip=True)
        obs_time = _parse_obs_time(dt_raw, now)
        if not obs_time:
            continue  # no usable observation time -> nothing to record/dedupe
        rows.append({
            "wmo": wmo_match.group(1),
            "name": th.get_text(strip=True),
            "obs_time": obs_time,
            "rain_since_9am_mm": _to_float(rain_raw),
        })
    return rows


def _seed_coords(wmos):
    """Back-fill coordinates for stations not yet in the registry, a few per
    cycle, from their per-station JSON."""
    known = set(database.read_df("SELECT wmo FROM aws_stations")["wmo"].astype(str))
    todo = [w for w in dict.fromkeys(wmos) if w not in known][:_COORD_SEED_PER_CYCLE]
    for wmo in todo:
        raw = _fetch(STATION_JSON.format(wmo=wmo), timeout=20)
        if not raw:
            continue
        try:
            d = json.loads(raw)["observations"]["data"][0]
        except (ValueError, KeyError, IndexError):
            continue
        database.insert_rows("aws_stations", [{
            "wmo": wmo, "name": d.get("name"),
            "latitude": d.get("lat"), "longitude": d.get("lon"),
        }], ignore_duplicates=True)


def fetch_aws_rainfall():
    """Fetch the state page, store new AWS rain readings, back-fill coords.
    Returns the number of new readings inserted."""
    now = datetime.now().isoformat(sep=" ", timespec="seconds")
    html = _fetch(VICALL_URL)
    if not html:
        return 0
    parsed = _parse_vicall(html)
    inserted = database.insert_rows("rainfall_aws", [{
        "wmo": r["wmo"], "name": r["name"],
        "rain_since_9am_mm": r["rain_since_9am_mm"],
        "obs_time": r["obs_time"], "timestamp": now,
    } for r in parsed], ignore_duplicates=True)
    _seed_coords([r["wmo"] for r in parsed])
    log.info("AWS rainfall: %d stations parsed, %d new reading(s)",
             len(parsed), inserted)
    return inserted

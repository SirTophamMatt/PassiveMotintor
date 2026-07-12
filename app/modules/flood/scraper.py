"""BoM Victoria river gauge scraper.

Each cycle fetches the flood-warning summary tables (one latest reading per
station) and stores them against their true observation time, so repeat
scrapes of the same reading are de-duplicated. For stations at or near flood
level, the per-station history page is backfilled once so graphs show a trend
immediately. A heartbeat row is written every cycle to prove the monitor ran.
"""
import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

import requests
from bs4 import BeautifulSoup

from app import database

log = logging.getLogger(__name__)

BOM_BASE = "http://www.bom.gov.au"
BOM_URLS = [
    f"{BOM_BASE}/cgi-bin/wrap_fwo.pl?IDV60078.html",
    f"{BOM_BASE}/cgi-bin/wrap_fwo.pl?IDV60079.html",
    f"{BOM_BASE}/cgi-bin/wrap_fwo.pl?IDV60201.html",
    f"{BOM_BASE}/cgi-bin/wrap_fwo.pl?IDV60147.html",
    f"{BOM_BASE}/cgi-bin/wrap_fwo.pl?IDV60148.html",
    f"{BOM_BASE}/cgi-bin/wrap_fwo.pl?IDV60149.html",
    f"{BOM_BASE}/cgi-bin/wrap_fwo.pl?IDV60150.html",
    f"{BOM_BASE}/cgi-bin/wrap_fwo.pl?IDV60151.html",
]

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

# td cells expected per data row, in order
CELL_FIELDS = [
    "station_name", "station_type", "time_day", "height_m", "gauge_datum",
    "tendency", "crossing_m", "classification", "recent_data",
]

# Columns actually stored in flood_observations (history_url is scrape-only).
OBS_COLUMNS = [
    "event", "catchment", "station_name", "station_type", "time_day",
    "height_m", "gauge_datum", "tendency", "crossing_m", "classification",
    "recent_data", "timestamp",
]

# Backfill a station's history when its latest height is within this fraction
# of its minor flood level (i.e. flooding or approaching).
NEAR_FLOOD_FRACTION = 0.9

# --- Outlier guard --------------------------------------------------------
# BoM occasionally publishes a garbage reading (e.g. a 2 m river gauge briefly
# reporting ~1000 m), which auto-scales a station's graph into uselessness. We
# drop a reading that jumps more than MAX_HEIGHT_JUMP_M from the station's last
# known good height. The check is RELATIVE, so datum-referenced reservoir gauges
# (which sit steadily at hundreds of m) are never touched — only sudden spikes.
# MAX_PLAUSIBLE_HEIGHT_M is an absolute backstop for a station with no prior
# history: no Victorian gauge, staff or datum-referenced, reads above it.
MAX_HEIGHT_JUMP_M = 50.0
MAX_PLAUSIBLE_HEIGHT_M = 900.0

_WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}

# (event, station) pairs already backfilled this process — avoids re-fetching
# the history page every cycle.
_backfilled = set()
_backfill_lock = threading.Lock()


def _to_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_obs_time(time_day, now=None):
    """Resolve a summary-table label like '02.31PM Sun' to a real datetime
    string (to the minute). Returns None if it can't be parsed."""
    now = now or datetime.now()
    if not time_day or not time_day.strip():
        return None
    parts = time_day.strip().split()
    if len(parts) != 2:
        return None
    time_part, day_part = parts
    weekday = _WEEKDAYS.get(day_part[:3].lower())
    if weekday is None:
        return None
    try:
        t = datetime.strptime(time_part, "%I.%M%p")
    except ValueError:
        return None
    candidate = now.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
    for _ in range(7):  # find the most recent matching weekday, not in the future
        if candidate.weekday() == weekday and candidate <= now + timedelta(minutes=2):
            return candidate.isoformat(sep=" ", timespec="minutes")
        candidate -= timedelta(days=1)
    return None


def _history_url(recent_data_cell):
    for a in recent_data_cell.find_all("a"):
        if a.get_text(strip=True).lower() == "table":
            href = a.get("href")
            if href:
                return BOM_BASE + href if href.startswith("/") else href
    return None


def _fetch_single(url, scrape_ts):
    rows_out = []
    try:
        response = requests.get(url, headers=HEADERS, timeout=15)
        response.raise_for_status()
    except requests.RequestException as e:
        log.warning("Failed to fetch %s: %s", url, e)
        return rows_out

    soup = BeautifulSoup(response.text, "lxml")
    table = soup.find("table", {"class": "tabledata rhb"})
    if not table:
        log.warning("Observation table not found in %s", url)
        return rows_out

    current_catchment = ""
    for row in table.find_all("tr"):
        tds = row.find_all("td")
        if not tds:
            # Header row: column header has many <th>; a catchment label is a
            # single <th> (BoM dropped the old 'rowlevel1' class).
            ths = row.find_all("th")
            if len(ths) == 1:
                current_catchment = ths[0].get_text(strip=True)
            continue
        cells = [td.get_text(strip=True) for td in tds]
        cells = (cells + [None] * len(CELL_FIELDS))[: len(CELL_FIELDS)]
        record = dict(zip(CELL_FIELDS, cells))
        record["height_m"] = _to_float(record["height_m"])
        record["catchment"] = current_catchment
        # Stamp with the true observation time; fall back to scrape time.
        record["timestamp"] = _parse_obs_time(record["time_day"]) or scrape_ts
        # The Table history link, used for near-flood backfill (not stored).
        record["history_url"] = _history_url(tds[8]) if len(tds) > 8 else None
        rows_out.append(record)
    return rows_out


def _parse_history_dt(text):
    try:
        return datetime.strptime(text.strip(), "%d/%m/%Y %H:%M").isoformat(
            sep=" ", timespec="minutes")
    except (ValueError, AttributeError):
        return None


def _fetch_history(history_url):
    """Return [(obs_datetime_str, height)] from a station's .tbl.shtml page."""
    out = []
    try:
        resp = requests.get(history_url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
    except requests.RequestException as e:
        log.warning("History fetch failed %s: %s", history_url, e)
        return out
    soup = BeautifulSoup(resp.text, "lxml")
    for table in soup.find_all("table"):
        trs = table.find_all("tr")
        if len(trs) < 5:
            continue
        header = " ".join(c.get_text(strip=True).lower()
                          for c in trs[0].find_all(["th", "td"]))
        if "date" not in header:
            continue
        for tr in trs[1:]:
            cells = [c.get_text(strip=True) for c in tr.find_all(["td", "th"])]
            if len(cells) < 2:
                continue
            dt = _parse_history_dt(cells[0])
            height = _to_float(cells[1])
            if dt and height is not None:
                out.append((dt, height))
        break
    return out


def _backfill_station(event, station, catchment, history_url):
    """Insert a station's historical observations. Returns rows added."""
    history = _fetch_history(history_url)
    if not history:
        return 0
    rows = [{
        "event": event, "catchment": catchment, "station_name": station,
        "station_type": None, "time_day": dt, "height_m": height,
        "gauge_datum": None, "tendency": None, "crossing_m": None,
        "classification": None, "recent_data": "history", "timestamp": dt,
    } for dt, height in history]
    added = database.insert_rows("flood_observations", rows, ignore_duplicates=True)
    log.info("Backfilled %s: %d of %d history rows new", station, added, len(rows))
    return added


def _near_flood_targets(rows):
    """From this cycle's rows, pick stations at/near minor flood level that
    still need a one-time history backfill. Returns list of dicts."""
    from app.modules.flood import data as flood_data
    levels = flood_data.load_flood_levels()
    if not levels:
        return []
    targets, seen = [], set()
    for r in rows:
        station = r["station_name"]
        if station in seen or not r.get("history_url") or r["height_m"] is None:
            continue
        seen.add(station)
        lvl = levels.get(str(station).strip().lower())
        minor = lvl["minor"] if lvl else None
        if minor and r["height_m"] >= NEAR_FLOOD_FRACTION * minor:
            key = (r["event"], station)
            with _backfill_lock:
                if key in _backfilled:
                    continue
                _backfilled.add(key)
            targets.append(r)
    return targets


def _last_known_heights():
    """Map station_name -> its most recent plausible stored height, used as the
    baseline for the spike guard. Implausible stored values are excluded so a
    bad row that slipped in earlier can't poison the baseline."""
    df = database.read_df(
        "SELECT station_name, height_m FROM flood_observations "
        "WHERE id IN (SELECT MAX(id) FROM flood_observations "
        "  WHERE height_m IS NOT NULL AND height_m <= ? "
        "  GROUP BY station_name)",
        [MAX_PLAUSIBLE_HEIGHT_M])
    return {r["station_name"]: r["height_m"] for _, r in df.iterrows()}


def _reject_spikes(rows):
    """Drop readings that are implausible outliers relative to a station's last
    known height (or above an absolute ceiling when there's no history).
    Returns the kept rows; rejected ones are logged."""
    baseline = _last_known_heights()
    kept = []
    for r in rows:
        h = r.get("height_m")
        if h is None:
            kept.append(r)
            continue
        last = baseline.get(r.get("station_name"))
        if h > MAX_PLAUSIBLE_HEIGHT_M and last is None:
            log.warning("Rejected implausible height %.2f m for %s (no history, "
                        "> %.0f m ceiling)", h, r.get("station_name"),
                        MAX_PLAUSIBLE_HEIGHT_M)
            continue
        if last is not None and abs(h - last) > MAX_HEIGHT_JUMP_M:
            log.warning("Rejected spike height %.2f m for %s (jumped %.1f m from "
                        "last %.2f m)", h, r.get("station_name"),
                        abs(h - last), last)
            continue
        kept.append(r)
    return kept


def fetch_flood_data(event_name="live"):
    """Scrape all BoM pages, store new readings, write a heartbeat, and
    backfill history for near-flood stations. Returns rows added.

    ``event_name`` is the always-on 'live' bucket; date-range slicing into named
    events is applied later via tags rather than at collection time."""
    scrape_ts = datetime.now().isoformat(sep=" ", timespec="seconds")
    rows = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(_fetch_single, url, scrape_ts) for url in BOM_URLS]
        for future in as_completed(futures):
            rows.extend(future.result())

    for r in rows:
        r["event"] = event_name

    # Drop garbage spikes before anything downstream (storage, near-flood
    # backfill, graphs) ever sees them.
    rows = _reject_spikes(rows)

    obs_rows = [{c: r.get(c) for c in OBS_COLUMNS} for r in rows]
    inserted = database.insert_rows("flood_observations", obs_rows, ignore_duplicates=True)
    database.insert_rows("flood_heartbeat", [{
        "event": event_name, "timestamp": scrape_ts,
        "stations_seen": len(rows), "new_rows": inserted,
    }])
    log.info("Flood fetch for '%s': %d stations, %d new readings",
             event_name, len(rows), inserted)

    targets = _near_flood_targets(rows)
    if targets:
        log.info("Backfilling history for %d near-flood station(s)", len(targets))
        with ThreadPoolExecutor(max_workers=4) as pool:
            for _ in as_completed([
                pool.submit(_backfill_station, t["event"], t["station_name"],
                            t["catchment"], t["history_url"]) for t in targets]):
                pass
    return inserted

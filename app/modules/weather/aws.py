"""BoM AWS weather-observation scraper.

Captures the current observation for every Victorian Automatic Weather Station
(~104) in a single request to BoM's state observations page, so it is light on
BoM (one page/cycle, not 104 per-station calls). Each reading is stored against
its BoM observation time and de-duped, so polling more often than BoM updates
(~30 min) never adds duplicate rows — the data volume is set by BoM's cadence,
not ours.

Originally rain-only; extended in 2026-08 to the full published field set
(temperature, apparent temperature, dew point, RH, delta-T, wind, pressure,
daily max gust). The storage table kept its `rainfall_aws` name so existing
rainfall history, event totals and exports were untouched — see database.py.

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
# over the first few cycles rather than a 104-request burst on first run).
_COORD_SEED_PER_CYCLE = 25

# --- BoM header-suffix -> internal field -------------------------------------
# Every observation cell on the state page carries a `headers` attribute listing
# the ids of the column header(s) it belongs to, e.g.
#   <td headers="tMAL-wind tMAL-wind-spd-kmh tMAL-station-charlton">9</td>
# The `tMAL` part is the regional sub-table, so only the SUFFIX is stable. We
# match on the suffix (leading "-" included, which is what stops "-tmp" from
# matching "-apptmp"/"-lowtmp"/"-hightmp" and "-wind-dir" from matching
# "-highwind-dir"). This is position-independent, so BoM reordering, adding or
# removing columns cannot silently shift a value into the wrong field.
#
# Cells we deliberately don't store: -wind-spd-kts / -wind-gust-kts /
# -highwind-gust-kts (unit duplicates of the km/h values) and -lowtmp /
# -hightmp (daily temperature extremes; not in the operational field set).
FLOAT, TEXT, VALUE_TIME = "float", "text", "value_time"

FIELD_MAP = {
    "-tmp": ("temperature_c", FLOAT),
    "-apptmp": ("apparent_temperature_c", FLOAT),
    "-dewpoint": ("dew_point_c", FLOAT),
    "-relhum": ("relative_humidity_pct", FLOAT),
    "-delta-t": ("delta_t_c", FLOAT),
    "-wind-dir": ("wind_direction", TEXT),
    "-wind-spd-kmh": ("wind_speed_kmh", FLOAT),
    "-wind-gust-kmh": ("wind_gust_kmh", FLOAT),
    "-press-msl": ("pressure_msl_hpa", FLOAT),
    "-rainsince9am": ("rain_since_9am_mm", FLOAT),
    "-highwind-dir": ("max_gust_direction", TEXT),
    # One cell holding both the day's strongest gust and when it happened.
    "-highwind-gust-kmh": (("max_gust_kmh", "max_gust_time"), VALUE_TIME),
}
# Longest suffix first, so a future BoM column whose id ends with an existing
# shorter suffix can never win the match ahead of its own more specific one.
_ORDERED_FIELDS = sorted(FIELD_MAP.items(), key=lambda kv: -len(kv[0]))
# Every field this parser can emit — used to give absent columns an explicit
# None so a ragged station still binds NULL rather than dropping the column.
OBS_FIELDS = ["rain_since_9am_mm"] + [
    name for spec, kind in FIELD_MAP.values()
    for name in ((spec,) if isinstance(spec, str) else spec)
    if name != "rain_since_9am_mm"
]

# "4602:40pm" -> 46 km/h at 02:40pm. Anchored at BOTH ends and requiring a
# two-digit hour, which is what makes the split unambiguous: the value is
# greedy, so the engine backtracks until the remainder is a whole valid time
# ("4602:40pm" tries 4602, 460, then 46 -> "02:40pm"). BoM always zero-pads the
# hour (verified across every value+time cell on the page). If that ever
# changes the match simply fails and we fall back to treating the cell as a
# plain number, which loses the time rather than inventing a wrong value.
_VALUE_TIME_RE = re.compile(
    r"^(?P<val>-?\d+(?:\.\d+)?)(?P<time>\d{2}:\d{2}\s*(?:am|pm))$", re.I)

# BoM prints "-" for a field a station didn't report. It must stay NULL: a
# missing gust is not a calm day and a missing rain total is not a dry one.
_MISSING = {"", "-", "--", "n/a", "na"}


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


def _clean(text):
    """Cell text with BoM's missing-value markers normalised to None."""
    s = (text or "").strip()
    return None if s.lower() in _MISSING else s


def _parse_value_time(text):
    """Split a combined "<value><hh:mmam/pm>" cell into (value, time).
    Returns (None, None) when absent, and (value, None) when BoM published the
    number without a time."""
    s = _clean(text)
    if s is None:
        return None, None
    m = _VALUE_TIME_RE.match(s.replace(" ", ""))
    if not m:
        return _to_float(s), None
    return _to_float(m.group("val")), m.group("time").lower()


def _field_for(headers):
    """(field spec, kind) for an observation cell, from its `headers` ids."""
    for suffix, spec in _ORDERED_FIELDS:
        for h in headers:
            if h.endswith(suffix):
                return spec
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


def _parse_station_row(tr, now):
    """Parse one station's <tr> into an observation dict, or None if the row
    isn't a station row / has no usable observation time."""
    th = tr.find("th")
    if not th:
        return None
    a = th.find("a", href=True)
    if not a:
        return None
    wmo_match = _WMO_RE.search(a["href"])
    if not wmo_match:
        return None

    row = {f: None for f in OBS_FIELDS}
    dt_raw = None
    for td in tr.find_all("td"):
        hdrs = td.get("headers") or []
        if any(h.endswith("-datetime") for h in hdrs):
            dt_raw = td.get_text(strip=True)
            continue
        spec = _field_for(hdrs)
        if not spec:
            continue
        field, kind = spec
        text = td.get_text(strip=True)
        if kind is FLOAT:
            row[field] = _to_float(_clean(text))
        elif kind is TEXT:
            row[field] = _clean(text)
        else:  # VALUE_TIME
            row[field[0]], row[field[1]] = _parse_value_time(text)

    obs_time = _parse_obs_time(dt_raw, now)
    if not obs_time:
        return None  # no usable observation time -> nothing to record/dedupe
    row["wmo"] = wmo_match.group(1)
    row["name"] = th.get_text(strip=True)
    row["obs_time"] = obs_time
    return row


def _parse_vicall(html):
    """Parse the state page into a list of observation dicts (one per station).

    A station that BoM publishes malformed is skipped individually — it must
    never cost us the rest of the state's observations."""
    soup = BeautifulSoup(html, "lxml")
    now = datetime.now()
    rows, failed = [], 0
    for tr in soup.find_all("tr"):
        try:
            row = _parse_station_row(tr, now)
        except Exception:
            failed += 1
            log.debug("AWS: skipped an unparseable station row", exc_info=True)
            continue
        if row:
            rows.append(row)
    if failed:
        log.warning("AWS: %d station row(s) unparseable, %d stored", failed, len(rows))
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


def fetch_aws_observations():
    """Fetch the state page, store new AWS observations, back-fill coords.
    Returns the number of new readings inserted."""
    now = datetime.now().isoformat(sep=" ", timespec="seconds")
    html = _fetch(VICALL_URL)
    if not html:
        return 0
    parsed = _parse_vicall(html)
    inserted = database.insert_rows("rainfall_aws", [
        {**{f: r.get(f) for f in OBS_FIELDS},
         "wmo": r["wmo"], "name": r["name"],
         "obs_time": r["obs_time"], "timestamp": now}
        for r in parsed], ignore_duplicates=True)
    _seed_coords([r["wmo"] for r in parsed])
    log.info("AWS observations: %d stations parsed, %d new reading(s)",
             len(parsed), inserted)
    return inserted


# The collector, Admin "Fetch now" button and watchdog have called this since
# the rain-only days; kept as the public name so none of them had to change.
fetch_aws_rainfall = fetch_aws_observations

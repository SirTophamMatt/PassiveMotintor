"""External sources for the Operational Summary auto-fill.

Fetched ON DEMAND (someone pressed "Fill" on the summary page), never on a
timer, with a short timeout and a 10-minute in-process cache — the summary is
built once or twice a day, so this is a handful of requests a day.

**Built without sight of the live sources** (the development sandbox cannot
reach BoM or GA), so both parsers find content by what it says rather than
where it sits, and a response that cannot be parsed is saved to
``<data dir>/opsum_debug_<name>.<ext>`` with an error naming that file — a
layout change shows up as a clear failure, never as quietly empty fields. Same
approach as the CFA pager parser.

* **BoM state forecast** — the page behind the deck's "Weather for Victoria"
  link (``/vic/forecasts/state.shtml``), fetched like the AWS page the weather
  collector already reads. The first "Forecast for …" heading is today; the
  "Weather Situation" heading carries the synoptic text.
* **GA earthquakes** — Geoscience Australia's public WFS feed (GeoJSON). Only
  the geometry is relied on for location (a Victoria bounding box); magnitude,
  time and description are read from whichever of the usual property names is
  present.
"""
import json
import logging
import os
import re
import time
import urllib.request
from datetime import datetime, timedelta

from bs4 import BeautifulSoup

from app.config import BASE_DIR, load_config

log = logging.getLogger(__name__)

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
CACHE_SECONDS = 600
_cache = {}

# Victoria plus a margin (Murray border towns, Bass Strait, SA/NSW edges).
VIC_BBOX = (-39.6, -33.8, 140.6, 150.3)     # lat_min, lat_max, lon_min, lon_max


class SourceError(LookupError):
    """A source could not be fetched or understood; the message is user-facing."""


def _fetch(url, timeout):
    hit = _cache.get(url)
    if hit and time.time() - hit[0] < CACHE_SECONDS:
        return hit[1]
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
    except Exception as e:           # network, HTTP error, timeout
        raise SourceError(f"could not reach {url.split('/')[2]} ({e.__class__.__name__}: {e})")
    _cache[url] = (time.time(), body)
    return body


def _debug_dump(name, ext, body):
    path = os.path.join(BASE_DIR, f"opsum_debug_{name}.{ext}")
    try:
        with open(path, "wb") as fh:
            fh.write(body if isinstance(body, bytes) else str(body).encode("utf8"))
    except OSError:
        return None
    return path


def _clean(text):
    return re.sub(r"\s+", " ", text or "").strip()


# --------------------------------------------------------------------------- #
# BoM state forecast
# --------------------------------------------------------------------------- #
def parse_state_forecast(html):
    """``{"forecast": str, "forecast_heading": str, "situation": str}`` from
    the BoM state forecast page; keys missing when not found."""
    soup = BeautifulSoup(html, "lxml")
    out = {}
    headings = soup.find_all(re.compile(r"^h[1-4]$"))

    def body_after(h):
        parts = []
        for sib in h.find_next_siblings():
            if re.fullmatch(r"h[1-4]", sib.name or ""):
                break
            text = _clean(sib.get_text(" "))
            if text:
                parts.append(text)
        return "\n".join(parts)

    for h in headings:
        title = _clean(h.get_text(" "))
        low = title.lower()
        if "situation" not in out and low.startswith("weather situation"):
            text = body_after(h)
            if text:
                out["situation"] = text
        elif "forecast" not in out and low.startswith("forecast for"):
            text = body_after(h)
            if text:
                out["forecast"] = text
                out["forecast_heading"] = title
    return out


def state_forecast(cfg=None):
    cfg = cfg or load_config()
    o = cfg["opsum"]
    body = _fetch(o["bom_forecast_url"], o["fetch_timeout_seconds"])
    parsed = parse_state_forecast(body)
    if not parsed:
        path = _debug_dump("bom_forecast", "html", body)
        raise SourceError("BoM forecast page not understood"
                          + (f" — sample saved to {path}" if path else ""))
    return parsed


# --------------------------------------------------------------------------- #
# GA earthquakes
# --------------------------------------------------------------------------- #
_TIME_KEYS = ("epicentral_time", "origin_time", "event_time", "time", "datetime")
_MAG_KEYS = ("preferred_magnitude", "magnitude", "mag", "ml")
_DESC_KEYS = ("description", "place", "locality", "region", "name")


def _first(props, keys):
    for k in keys:
        v = props.get(k)
        if v not in (None, ""):
            return v
    return None


def _parse_time(value):
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):          # epoch ms
        return datetime.fromtimestamp(value / 1000 if value > 1e11 else value)
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.astimezone().replace(tzinfo=None) if dt.tzinfo else dt


def _point(geom):
    try:
        if geom["type"] == "Point":
            lon, lat = geom["coordinates"][:2]
            return float(lat), float(lon)
    except (KeyError, TypeError, ValueError, IndexError):
        pass
    return None


def parse_quakes(payload, now, hours=48, min_mag=2.5):
    """Victorian earthquakes in the last ``hours`` at or above ``min_mag``,
    newest first: ``[{time, mag, desc, depth}]``."""
    data = json.loads(payload) if isinstance(payload, (bytes, str)) else payload
    feats = data.get("features") if isinstance(data, dict) else None
    if not isinstance(feats, list):
        raise ValueError("no features list")
    since = now - timedelta(hours=hours)
    out = []
    lat0, lat1, lon0, lon1 = VIC_BBOX
    for f in feats:
        props = f.get("properties") or {}
        pt = _point(f.get("geometry") or {})
        if pt is None:
            try:
                pt = float(props.get("latitude")), float(props.get("longitude"))
            except (TypeError, ValueError):
                continue
        if not (lat0 <= pt[0] <= lat1 and lon0 <= pt[1] <= lon1):
            continue
        when = _parse_time(_first(props, _TIME_KEYS))
        try:
            mag = float(_first(props, _MAG_KEYS))
        except (TypeError, ValueError):
            continue
        if when is None or when < since or when > now + timedelta(hours=1) or mag < min_mag:
            continue
        depth = props.get("depth")
        try:
            depth = float(depth)
        except (TypeError, ValueError):
            depth = None
        out.append({"time": when, "mag": mag, "depth": depth,
                    "desc": _clean(str(_first(props, _DESC_KEYS) or
                                       f"{pt[0]:.2f}, {pt[1]:.2f}"))})
    out.sort(key=lambda q: q["time"], reverse=True)
    return out


def earthquakes(now=None, cfg=None):
    cfg = cfg or load_config()
    o = cfg["opsum"]
    now = now or datetime.now()
    body = _fetch(o["ga_quakes_url"], o["fetch_timeout_seconds"])
    try:
        return parse_quakes(body, now, float(o["earthquake_hours"]),
                            float(o["earthquake_min_magnitude"]))
    except (ValueError, TypeError, AttributeError) as e:
        path = _debug_dump("ga_quakes", "json", body)
        raise SourceError(f"GA earthquake feed not understood ({e})"
                          + (f" — sample saved to {path}" if path else ""))

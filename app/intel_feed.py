"""Intelligence Feed — the change-detection engine.

Every other page in this app answers *what is happening*. This one answers
**what just changed, by how much, and how fast** — the difference between

    Fire — Walwa

and

    12:43 — Walwa fire increased 38 ha
            214 -> 296 ha since 12:05
            Watch and Act remains current
            3 road disruptions within 10 km

A detector pass runs on its own collector thread (``intel`` in collector.py),
reads every module's stored data, and writes one ``intel_events`` row per
*significant* change. Nothing is written when nothing moved, so the table is a
change log, not a state dump — the same "change-only" discipline the watchdog
alerts and ``storm_alerts`` already follow.

Two things make the deltas possible:

* **Sources that already keep history** (``flood_observations``,
  ``power_timeseries``, ``storm_cells``, ``rainfall_aws``) are diffed straight
  out of their own tables.
* **Sources that UPSERT** (``fire_incidents``, ``power_outages``,
  ``road_disruptions``) keep no past values at all, so this module maintains
  ``intel_metrics`` — one row per entity/metric, written only when the value
  actually changes. That is what makes "214 -> 296 ha since 12:05" possible for
  a fire whose own row is overwritten every cycle.

**Restart-safe and replay-safe.** Every event is stamped with the SOURCE time
the change became true (not the time we noticed), detectors only look back
``intel.lookback_minutes``, and ``idx_intel_events_dedup`` is unique on
(hazard, entity, metric, kind, ts) — so re-running the pass over the same data,
or restarting the server mid-event, can never duplicate or re-fire an entry.

Cross-layer context lines ("3 road disruptions within 10 km") are NOT stored.
They are computed at read time from current data, so an entry's context stays
true as the situation develops.
"""
import json
import logging
import math
import re
from datetime import datetime, timedelta
from urllib.parse import quote

import numpy as np
import pandas as pd

from app import database
from app.config import load_config
from app.modules.flood import data as flood_data
from app.modules.flood import trend as flood_trend
from app.modules.weather import data as weather_data

log = logging.getLogger(__name__)

# Severity drives sort order, the card accent and the feed's severity filter.
CRITICAL, MAJOR, NOTABLE, INFO = 3, 2, 1, 0

SEVERITY_STYLE = {
    CRITICAL: ("Critical", "#d62728"),
    MAJOR: ("Major", "#ff7f0e"),
    NOTABLE: ("Notable", "#e6c700"),
    INFO: ("Info", "#5b8def"),
}

HAZARDS = ["fire", "flood", "storm", "weather", "power", "roads", "rainfall"]

HAZARD_LABEL = {
    "fire": "Fire", "flood": "Flood", "storm": "Storm", "weather": "Weather",
    "power": "Power", "roads": "Roads", "rainfall": "Rainfall",
}

HAZARD_PAGE = {
    "fire": "/fire", "flood": "/flood", "storm": "/storm", "weather": "/weather",
    "power": "/power", "roads": "/roads", "rainfall": "/weather",
}

# Radar product id prefix -> the name a human uses for it. Mirrors
# storm.scraper.RADAR_SITES; unknown ids fall back to the raw id.
RADAR_NAMES = {"IDR02": "Melbourne", "IDR14": "Mt Gambier", "IDR31": "Albany"}

# Units that must render at a FIXED number of decimals so a before/after pair
# aligns. Everything else uses the smart formatter.
_UNIT_DP = {"m": 2}

_CARDINALS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
              "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]

_HA_RE = re.compile(r"([\d,]+(?:\.\d+)?)\s*(?:ha|hectare)", re.IGNORECASE)

# Warning level -> ordinal, so an escalation is a NUMERIC comparison (lower is
# more severe, matching fire_data.WARNING_STYLE).
_WARNING_ORDINAL = {"emergency warning": 1, "evacuate": 1, "evacuation": 1,
                    "watch and act": 2, "advice": 3,
                    "community information": 3}

# When the flood projection cycle last ran. It is far heavier than the rest of
# a detector pass, so it runs on its own slower clock (see _projection_due).
_last_projection_run = None


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def _now():
    return datetime.now()


def _stamp(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _parse_ts(value):
    """Tolerant timestamp parse. Returns a datetime or None (never NaT — the
    same pandas-NaT lesson the scrapers learned)."""
    if value is None or value == "":
        return None
    try:
        parsed = pd.to_datetime(value, errors="coerce")
    except (ValueError, TypeError):
        return None
    if parsed is None or pd.isna(parsed):
        return None
    parsed = parsed.to_pydatetime()
    # Feeds mix naive local strings with tz-aware ones; compare on local wall
    # clock so a tz-aware value never raises against a naive one.
    return parsed.replace(tzinfo=None) if parsed.tzinfo else parsed


def _num(value):
    n = pd.to_numeric(value, errors="coerce")
    return None if pd.isna(n) else float(n)


def _fmt(value, dp=None):
    """Format a number the way an operator writes it: thousands separated, no
    pointless trailing zeros ( 296.0 -> '296', 6.37 -> '6.37', 5840 -> '5,840')."""
    if value is None:
        return "?"
    if dp is not None:
        return f"{value:,.{dp}f}"
    if abs(value - round(value)) < 1e-9:
        return f"{round(value):,}"
    return f"{value:,.2f}".rstrip("0").rstrip(".")


def _cardinal(bearing):
    if bearing is None:
        return None
    return _CARDINALS[int((float(bearing) % 360) / 22.5 + 0.5) % 16]


def _parse_ha(text):
    """Hectares out of the VicEmergency free-text size field, which arrives as
    '63 ha', '0.10 Ha.', '7223.24 Ha.' — or as 'Small'/'Medium', which carries
    no number and is correctly ignored."""
    if text is None:
        return None
    match = _HA_RE.search(str(text))
    if not match:
        return None
    try:
        return float(match.group(1).replace(",", ""))
    except ValueError:
        return None


def _place(location):
    """The short place name out of a VicEmergency location string
    ('Streatham - Yalla-Y-Poora Road' -> 'Streatham')."""
    text = str(location or "").strip()
    if not text:
        return "Unknown location"
    for sep in (" - ", ", "):
        if sep in text:
            return text.split(sep)[0].strip()
    return text


def _haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _haversine_km_series(lat, lon, lats, lons):
    """Distance from one point to a whole column of points, in km. NaN
    coordinates come back as inf so they never fall inside a radius."""
    p1 = math.radians(lat)
    p2 = np.radians(lats.to_numpy(dtype=float))
    dp = p2 - p1
    dl = np.radians(lons.to_numpy(dtype=float) - lon)
    a = np.sin(dp / 2) ** 2 + math.cos(p1) * np.cos(p2) * np.sin(dl / 2) ** 2
    km = 2 * 6371.0 * np.arcsin(np.sqrt(a))
    return pd.Series(np.where(np.isnan(km), np.inf, km), index=lats.index)


def _plural(n, singular, plural=None):
    return f"{n} {singular if n == 1 else (plural or singular + 's')}"


# --------------------------------------------------------------------------- #
# Journal read/write
# --------------------------------------------------------------------------- #
def record(hazard, kind, severity, headline, ts, entity_key=None,
           entity_name=None, metric=None, prev_value=None, new_value=None,
           unit=None, prev_label=None, new_label=None, since=None, rate=None,
           detail=None, latitude=None, longitude=None, url=None):
    """Write one change to the journal. INSERT OR IGNORE against the unique
    (hazard, entity, metric, kind, ts) index, so a repeated detector pass over
    unchanged data is a no-op. Returns True when a NEW row landed."""
    row = {
        "ts": _stamp(ts) if isinstance(ts, datetime) else str(ts),
        "detected_at": _stamp(_now()),
        "hazard": hazard,
        "entity_key": str(entity_key) if entity_key is not None else "",
        "entity_name": entity_name,
        "kind": kind,
        "severity": int(severity),
        "headline": headline,
        "metric": metric or "",
        "prev_value": prev_value,
        "new_value": new_value,
        "unit": unit,
        "prev_label": prev_label,
        "new_label": new_label,
        "since": _stamp(since) if isinstance(since, datetime) else since,
        "rate": rate,
        "detail": json.dumps(detail) if detail else None,
        "latitude": latitude,
        "longitude": longitude,
        "url": url,
    }
    return database.insert_rows("intel_events", [row], ignore_duplicates=True) > 0


def _event_exists(hazard, entity_key, kind, since_dt):
    """Whether this entity already produced this kind of event recently — the
    rate limiter that stops a steadily-rising gauge emitting every cycle."""
    df = database.read_df(
        "SELECT 1 FROM intel_events WHERE hazard = ? AND entity_key = ? "
        "AND kind = ? AND ts >= ? LIMIT 1",
        [hazard, str(entity_key), kind, _stamp(since_dt)])
    return not df.empty


def _metric_prev(hazard, entity_key, metric):
    """Last recorded (value, label, datetime) for an entity's metric, or None."""
    df = database.read_df(
        "SELECT value, label, ts FROM intel_metrics WHERE hazard = ? "
        "AND entity_key = ? AND metric = ? ORDER BY ts DESC, id DESC LIMIT 1",
        [hazard, str(entity_key), metric])
    if df.empty:
        return None
    row = df.iloc[0]
    return _num(row["value"]), row["label"], _parse_ts(row["ts"])


def _metric_record(hazard, entity_key, metric, value=None, label=None, ts=None):
    """Append a metric observation, but ONLY when it differs from the last one.
    That keeps intel_metrics proportional to change, not to poll frequency."""
    prev = _metric_prev(hazard, entity_key, metric)
    if prev is not None:
        prev_value, prev_label, _ = prev
        same_value = (prev_value is None and value is None) or (
            prev_value is not None and value is not None
            and abs(prev_value - value) < 1e-9)
        if same_value and (prev_label or None) == (label or None):
            return False
    database.insert_rows("intel_metrics", [{
        "hazard": hazard, "entity_key": str(entity_key), "metric": metric,
        "value": value, "label": label,
        "ts": _stamp(ts or _now()),
    }])
    return True


# --------------------------------------------------------------------------- #
# Detectors
# --------------------------------------------------------------------------- #
def _detect_fire(cfg, cutoff):
    """Fire/incident area growth and community-warning escalation."""
    settings = cfg["intel"]
    df = database.read_df(
        "SELECT source_id, feed_type, category1, warning_level, location, size, "
        "status, resources, latitude, longitude, created, updated, first_seen "
        "FROM fire_incidents WHERE resolved = 0 AND feed_type != 'burn-area'")
    if df.empty:
        return

    for _, row in df.iterrows():
        key = row.get("source_id")
        if not key:
            continue
        place = _place(row.get("location"))
        ts = (_parse_ts(row.get("updated")) or _parse_ts(row.get("first_seen"))
              or _now())
        lat, lon = _num(row.get("latitude")), _num(row.get("longitude"))

        # --- community warnings: level changes -------------------------------
        if str(row.get("feed_type") or "").lower() == "warning":
            level = str(row.get("warning_level") or "").strip()
            ordinal = _WARNING_ORDINAL.get(level.lower())
            if not level or ordinal is None:
                continue
            prev = _metric_prev("fire", key, "warning_level")
            _metric_record("fire", key, "warning_level", ordinal, level, ts)
            if prev is None:
                first_seen = _parse_ts(row.get("first_seen"))
                if first_seen and first_seen >= cutoff:
                    record("fire", "new", CRITICAL if ordinal == 1 else MAJOR,
                           f"New {level} issued", first_seen, entity_key=key,
                           entity_name=place, metric="warning_level",
                           new_label=level, latitude=lat, longitude=lon,
                           url="/fire",
                           detail=[f"{row.get('event') or 'Community warning'}"
                                   f" — {row.get('location') or place}"])
                continue
            prev_ordinal, prev_level, prev_ts = prev
            if prev_ordinal is None or prev_ordinal == ordinal:
                continue
            escalated = ordinal < prev_ordinal
            record("fire", "escalation" if escalated else "downgrade",
                   (CRITICAL if ordinal == 1 else MAJOR) if escalated else INFO,
                   f"Warning {'upgraded' if escalated else 'downgraded'} "
                   f"to {level} — {place}",
                   ts, entity_key=key, entity_name=place, metric="warning_level",
                   prev_label=prev_level, new_label=level, since=prev_ts,
                   latitude=lat, longitude=lon, url="/fire")
            continue

        # --- incidents: area growth ------------------------------------------
        area = _parse_ha(row.get("size"))
        if area is None:
            continue
        prev = _metric_prev("fire", key, "area_ha")
        _metric_record("fire", key, "area_ha", area, None, ts)
        if prev is None:
            continue
        prev_area, _, prev_ts = prev
        if prev_area is None or area <= prev_area:
            continue
        grew = area - prev_area
        floor = max(float(settings["fire_min_growth_ha"]),
                    prev_area * float(settings["fire_min_growth_pct"]) / 100.0)
        if grew < floor:
            continue

        kind_word = ("fire" if str(row.get("category1") or "").strip().lower()
                     == "fire" else str(row.get("category1") or "incident").lower())
        detail = []
        if row.get("status"):
            detail.append(f"Status: {row['status']}")
        resources = _num(row.get("resources"))
        if resources:
            detail.append(_plural(int(resources), "resource") + " assigned")
        record("fire", "growth",
               CRITICAL if area >= 1000 else (MAJOR if area >= 100 else NOTABLE),
               f"{place} {kind_word} increased {_fmt(grew)} ha",
               ts, entity_key=key, entity_name=place, metric="area_ha",
               prev_value=prev_area, new_value=area, unit="ha", since=prev_ts,
               rate=_growth_rate(prev_area, area, prev_ts, ts),
               detail=detail, latitude=lat, longitude=lon, url="/fire")


def _growth_rate(prev_value, new_value, prev_ts, ts):
    """'+38 ha/hr' style rate, or None when the interval is too short to carry
    a meaningful rate (a 2-minute gap extrapolates to nonsense)."""
    if not (prev_ts and ts) or ts <= prev_ts or prev_value is None:
        return None
    hours = (ts - prev_ts).total_seconds() / 3600.0
    if hours < 0.25:
        return None
    return f"+{(new_value - prev_value) / hours:,.0f} ha/hr"


def _detect_flood(cfg, cutoff):
    """Flood-class threshold crossings and rise rate, from the real
    observation history in flood_observations."""
    settings = cfg["intel"]
    levels = flood_data.load_flood_levels()
    if not levels:
        return
    window_start = _stamp(_now() - timedelta(
        minutes=max(int(settings["lookback_minutes"]),
                    int(settings["flood_rate_window_minutes"]))))
    df = database.read_df(
        "SELECT station_name, height_m, timestamp FROM flood_observations "
        "WHERE timestamp >= ? ORDER BY station_name, timestamp", [window_start])
    if df.empty:
        return
    df["height_m"] = pd.to_numeric(df["height_m"], errors="coerce")
    df = df.dropna(subset=["height_m"])
    coords = _gauge_coords()

    for station, group in df.groupby("station_name"):
        level = levels.get(str(station).strip().lower())
        if not level:
            continue
        readings = [(_parse_ts(r["timestamp"]), float(r["height_m"]))
                    for _, r in group.iterrows()]
        readings = [r for r in readings if r[0] is not None]
        if len(readings) < 2:
            continue
        station_key = str(station).strip().lower()
        lat, lon = coords.get(station_key, (None, None))
        latest_ts, latest_height = readings[-1]
        rate_text, rate_m_hr = _flood_rate(
            readings, int(settings["flood_rate_window_minutes"]))

        # --- class transitions between consecutive readings ------------------
        for (prev_ts, prev_height), (ts, height) in zip(readings, readings[1:]):
            if ts < cutoff:
                continue
            prev_priority, _, _ = flood_data.classify_station(prev_height, level)
            priority, label, _ = flood_data.classify_station(height, level)
            if priority == prev_priority:
                continue
            rising = priority < prev_priority   # lower priority = worse
            detail = []
            threshold = level.get({1: "major", 2: "moderate",
                                   3: "minor"}.get(priority, ""), None)
            if rising and threshold is not None and pd.notna(threshold):
                detail.append(f"{label.split()[0]} level is "
                              f"{_fmt(float(threshold))} m")
            crossing_analysis = None
            if rising:
                # Having crossed one class, the operative question is when the
                # NEXT one arrives — so the crossing entry carries the onward
                # projection too.
                crossing_analysis = _trend_for(station_key, level, cfg)
                detail += _trend_lines(station_key, crossing_analysis, cfg)
            record(
                "flood", "threshold" if rising else "receding",
                {1: CRITICAL, 2: MAJOR, 3: MAJOR}.get(priority, INFO) if rising
                else INFO,
                (f"{label.split()[0]} flood threshold crossed" if rising
                 else f"Fallen back to {label.lower()}"),
                ts, entity_key=station_key, entity_name=station,
                metric="height_m", prev_value=prev_height, new_value=height,
                unit="m", since=prev_ts,
                rate=_trend_rate_text(crossing_analysis, rate_text),
                detail=detail,
                latitude=lat, longitude=lon,
                url=_station_href(station))

        # --- fast rise without a crossing ------------------------------------
        if rate_m_hr is None or latest_ts < cutoff:
            continue
        if rate_m_hr < float(settings["flood_rise_m_per_hr"]):
            continue
        priority, label, _ = flood_data.classify_station(latest_height, level)
        minor = level.get("minor")
        approaching = (priority < 4 or
                       (minor is not None and pd.notna(minor)
                        and latest_height >= float(minor) * 0.9))
        if not approaching:
            continue
        suppress = timedelta(minutes=int(settings["repeat_suppress_minutes"]))
        if _event_exists("flood", station_key, "rising", _now() - suppress):
            continue
        # A class crossing already reports this gauge's height AND rate; a
        # second "rising quickly" card beside it is pure duplication.
        if _event_exists("flood", station_key, "threshold", _now() - suppress):
            continue
        window_ts, window_height = _rate_anchor(
            readings, int(settings["flood_rate_window_minutes"]))
        analysis = _trend_for(station_key, level, cfg)
        # Kept out of the f-string on purpose: a replacement field may only span
        # lines from Python 3.12 (PEP 701), and the Docker image is 3.11.
        trend_headline = (analysis or {}).get("headline", "rising").lower()
        headline = (f"{station} {trend_headline}"
                    if analysis and analysis.get("rate_m_hr")
                    else f"{station} rising quickly")
        record("flood", "rising", MAJOR if priority < 4 else NOTABLE,
               headline, latest_ts,
               entity_key=station_key, entity_name=station,
               metric="height_m", prev_value=window_height,
               new_value=latest_height, unit="m", since=window_ts,
               rate=_trend_rate_text(analysis, rate_text),
               detail=([label if priority < 4 else "Approaching minor flood level"]
                       + _trend_lines(station_key, analysis, cfg)),
               latitude=lat, longitude=lon,
               url=_station_href(station))


def _trend_for(station_key, level, cfg):
    """Rate-of-rise analysis for a feed entry, or None if it cannot be had.
    Never allowed to break a detector pass."""
    try:
        return flood_trend.analyse(station_key, levels=level, cfg=cfg)
    except Exception:
        log.exception("Trend analysis failed for %s", station_key)
        return None


def _trend_rate_text(analysis, fallback):
    """Prefer the trend engine's fitted rate for the display line.

    The feed used to quote its own rate over flood_rate_window_minutes while
    the station page quoted the trend fit over trend_window_minutes — two
    different numbers for the same gauge in the same app. The trend fit is the
    one the projection is built on, so it wins wherever both exist."""
    rate = (analysis or {}).get("rate_m_hr")
    if rate is None:
        return fallback
    if abs(rate) < 0.01:
        return "Steady"
    word = "Rising" if rate > 0 else "Falling"
    span = (analysis or {}).get("span_minutes")
    suffix = f" over the last {span} minutes" if span else ""
    return f"{word} {abs(rate):.2f} m/hr{suffix}"


def _trend_lines(station_key, analysis, cfg):
    """Acceleration, threshold distance, projected arrival and catchment
    rainfall as feed context lines. The projection line always carries its
    caveat inline — a feed entry gets read on its own, away from the station
    page where the full disclaimer lives."""
    lines = []
    if not analysis:
        return lines
    if analysis.get("accel_label") not in (None, "Unknown", "Steady"):
        lines.append(analysis["accel_label"])
    distance, target = analysis.get("distance_m"), analysis.get("target_name")
    if distance is not None and target and analysis.get("target_kind") == "class":
        lines.append(f"{distance:.2f} m below {str(target).title()}")
    if analysis.get("eta_point"):
        lines.append(
            f"{str(target).title()} potentially reached "
            f"{analysis['eta_early']:%H:%M}–{analysis['eta_late']:%H:%M} "
            "(trend projection, not an official forecast)")
    try:
        rainfall = flood_trend.catchment_rainfall(station_key, cfg=cfg)
    except Exception:
        log.exception("Catchment rainfall failed for %s", station_key)
        rainfall = None
    if rainfall and rainfall.get("max_mm"):
        where = rainfall.get("catchment") or "the surrounding catchment"
        lines.append(f"{rainfall['max_mm']:.0f} mm rain in {where} "
                     f"({rainfall['wettest']}, last "
                     f"{rainfall['window_hours']:g} h)")
    return lines


def _station_href(station_name):
    """Deep link to the gauge detail page. Mirrors pages/station.station_href
    (quoted lowercase name); replicated rather than imported so the engine
    stays independent of the page layer."""
    return "/flood/station/" + quote(str(station_name).strip().lower())


def _rate_anchor(readings, window_minutes):
    """The earliest reading inside the rate window (the point we measure FROM)."""
    latest_ts = readings[-1][0]
    start = latest_ts - timedelta(minutes=window_minutes)
    inside = [r for r in readings if r[0] >= start]
    return inside[0] if len(inside) >= 2 else readings[0]


def _flood_rate(readings, window_minutes):
    """('Rising 0.18 m/hr', 0.18) over the rate window. Rate is signed; the
    text says Rising/Falling. Returns (None, None) when it is not meaningful."""
    anchor_ts, anchor_height = _rate_anchor(readings, window_minutes)
    latest_ts, latest_height = readings[-1]
    hours = (latest_ts - anchor_ts).total_seconds() / 3600.0
    if hours < 5 / 60:
        return None, None
    rate = (latest_height - anchor_height) / hours
    if abs(rate) < 0.01:
        return "Steady", 0.0
    word = "Rising" if rate > 0 else "Falling"
    return f"{word} {abs(rate):.2f} m/hr", rate


def _gauge_coords():
    df = database.read_df(
        "SELECT station_key, latitude, longitude FROM gauge_coords "
        "WHERE latitude IS NOT NULL")
    return {r["station_key"]: (_num(r["latitude"]), _num(r["longitude"]))
            for _, r in df.iterrows()}


def _detect_power(cfg, cutoff):
    """Statewide outage trend (from power_timeseries) plus per-location
    escalation (via intel_metrics, since power_outages is an upsert)."""
    settings = cfg["intel"]
    window = int(settings["power_window_minutes"])
    df = database.read_df(
        "SELECT timestamp, customers_off FROM power_timeseries "
        "WHERE timestamp >= ? ORDER BY timestamp",
        [_stamp(_now() - timedelta(minutes=window * 2))])
    if not df.empty:
        df["customers_off"] = pd.to_numeric(df["customers_off"], errors="coerce")
        df = df.dropna(subset=["customers_off"])
    if len(df) >= 2:
        latest_ts = _parse_ts(df.iloc[-1]["timestamp"])
        latest = float(df.iloc[-1]["customers_off"])
        anchor = df[df["timestamp"] <= _stamp(latest_ts - timedelta(minutes=window))]
        anchor_row = anchor.iloc[-1] if not anchor.empty else df.iloc[0]
        anchor_ts = _parse_ts(anchor_row["timestamp"])
        previous = float(anchor_row["customers_off"])
        delta = latest - previous
        pct = (delta / previous * 100.0) if previous else None
        big_enough = (abs(delta) >= float(settings["power_min_delta"]) and
                      (pct is None or abs(pct) >= float(settings["power_min_pct"])))
        suppress = timedelta(minutes=int(settings["repeat_suppress_minutes"]))
        if (big_enough and latest_ts and latest_ts >= cutoff
                and not _event_exists("power", "statewide",
                                      "deteriorating" if delta > 0 else "improving",
                                      _now() - suppress)):
            minutes = max(1, round((latest_ts - anchor_ts).total_seconds() / 60))
            record("power",
                   "deteriorating" if delta > 0 else "improving",
                   MAJOR if delta > 0 and latest >= 5000 else NOTABLE,
                   ("Power situation deteriorating" if delta > 0
                    else "Power situation improving"),
                   latest_ts, entity_key="statewide",
                   entity_name="Victorian outages", metric="customers_off",
                   prev_value=previous, new_value=latest, unit="customers",
                   since=anchor_ts,
                   rate=(f"{pct:+.0f}% over {minutes} minutes"
                         if pct is not None else None),
                   url="/power")

    # --- per-location outages -------------------------------------------------
    outages = database.read_df(
        "SELECT location, customers_off, type, first_seen, last_seen "
        "FROM power_outages WHERE restored = 0")
    if outages.empty:
        return
    for _, row in outages.iterrows():
        location = str(row.get("location") or "").strip()
        if not location:
            continue
        customers = _num(row.get("customers_off"))
        if customers is None:
            continue
        ts = _parse_ts(row.get("last_seen")) or _now()
        prev = _metric_prev("power", location, "customers_off")
        _metric_record("power", location, "customers_off", customers, None, ts)
        threshold = float(settings["power_location_min_delta"])
        if prev is None:
            first_seen = _parse_ts(row.get("first_seen"))
            if (first_seen and first_seen >= cutoff and customers >= threshold):
                record("power", "new", NOTABLE,
                       f"New outage at {location}", first_seen,
                       entity_key=location, entity_name=location,
                       metric="customers_off", new_value=customers,
                       unit="customers",
                       detail=[f"{row.get('type') or 'Unplanned'} outage"],
                       url="/power")
            continue
        prev_customers, _, prev_ts = prev
        if prev_customers is None or ts < cutoff:
            continue
        delta = customers - prev_customers
        if abs(delta) < threshold:
            continue
        pct = (delta / prev_customers * 100.0) if prev_customers else None
        minutes = (max(1, round((ts - prev_ts).total_seconds() / 60))
                   if prev_ts else None)
        record("power", "growth" if delta > 0 else "recovery",
               MAJOR if customers >= 5000 else NOTABLE,
               (f"{location} outage {'grew' if delta > 0 else 'shrank'} by "
                f"{_fmt(abs(delta))} customers"),
               ts, entity_key=location, entity_name=f"{location} outage",
               metric="customers_off", prev_value=prev_customers,
               new_value=customers, unit="customers", since=prev_ts,
               rate=(f"{pct:+.0f}%" + (f" over {minutes} minutes" if minutes
                                       else "") if pct is not None else None),
               detail=[f"{row.get('type') or 'Unplanned'} outage"],
               url="/power")


def _detect_storm(cfg, cutoff):
    """Storm cells changing class between radar frames (storm_cells keeps one
    row per cell per frame, so this is a true frame-to-frame diff)."""
    settings = cfg["intel"]
    df = database.read_df(
        "SELECT cell_id, radar_id, frame_ts, classification, speed_kmh, "
        "bearing_deg, area_km2, max_level, latitude, longitude "
        "FROM storm_cells WHERE frame_ts >= ? ORDER BY cell_id, frame_ts",
        [_stamp(_now() - timedelta(minutes=int(settings["lookback_minutes"])))])
    if df.empty:
        return
    rank = {"weak": 1, "moderate": 2, "strong": 3}

    for cell_id, group in df.groupby("cell_id"):
        rows = list(group.itertuples())
        for previous, current in zip(rows, rows[1:]):
            ts = _parse_ts(current.frame_ts)
            if ts is None or ts < cutoff:
                continue
            before = str(previous.classification or "").lower()
            after = str(current.classification or "").lower()
            if before == after or after not in rank or before not in rank:
                continue
            intensified = rank[after] > rank[before]
            # Weak cells come and go constantly; only the moderate+ story is
            # worth a feed entry (same bar as the storm webhook alerts).
            if not intensified and rank[before] < 3:
                continue
            radar = RADAR_NAMES.get(str(current.radar_id or "")[:5],
                                    current.radar_id or "Radar")
            name = f"{radar} radar Cell {str(cell_id).split('-')[-1]}"
            speed, bearing = _num(current.speed_kmh), _num(current.bearing_deg)
            movement = None
            if speed and speed > 1:
                heading = _cardinal(bearing)
                movement = (f"Moving {heading} at {speed:.0f} km/h" if heading
                            else f"Moving at {speed:.0f} km/h")
            detail = []
            area = _num(current.area_km2)
            if area:
                detail.append(f"Echo area {_fmt(area)} km²")
            record("storm",
                   "intensification" if intensified else "weakening",
                   (CRITICAL if after == "strong" else MAJOR) if intensified
                   else INFO,
                   ("Storm cell intensified" if intensified
                    else "Storm cell weakening"),
                   ts, entity_key=str(cell_id), entity_name=name,
                   metric="classification",
                   prev_label=before.capitalize(), new_label=after.capitalize(),
                   since=_parse_ts(previous.frame_ts), rate=movement,
                   detail=detail, latitude=_num(current.latitude),
                   longitude=_num(current.longitude), url="/storm")


def _detect_weather(cfg, cutoff):
    """New, reissued and cancelled BoM warnings."""
    df = database.read_df(
        "SELECT warning_id, title, short_title, type, group_type, phase, "
        "issue_time, first_seen, last_seen, active FROM weather_warnings")
    if df.empty:
        return
    for _, row in df.iterrows():
        warning_id = row.get("warning_id")
        if not warning_id:
            continue
        title = row.get("short_title") or row.get("title") or "BoM warning"
        url = f"/weather/warning/{warning_id}"
        active = int(row.get("active") or 0)
        issue_ts = _parse_ts(row.get("issue_time"))
        first_seen = _parse_ts(row.get("first_seen"))

        if not active:
            cleared = _parse_ts(row.get("last_seen"))
            prev = _metric_prev("weather", warning_id, "active")
            _metric_record("weather", warning_id, "active", 0, "cancelled",
                           cleared or _now())
            if (prev and prev[0] == 1 and cleared and cleared >= cutoff):
                record("weather", "cleared", INFO,
                       "BoM warning no longer current", cleared,
                       entity_key=warning_id, entity_name=title,
                       metric="active", prev_label="Active",
                       new_label="Cancelled", url=url)
            continue

        _metric_record("weather", warning_id, "active", 1, "active",
                       first_seen or _now())
        # A reissue keeps the same BoM id but carries a new issue_time; the
        # per-version history in weather_warning_updates is the source of truth.
        prev_issue = _metric_prev("weather", warning_id, "issue_time")
        issue_stamp = _stamp(issue_ts) if issue_ts else None
        if issue_stamp:
            _metric_record("weather", warning_id, "issue_time", None,
                           issue_stamp, issue_ts)
        if prev_issue is None:
            if first_seen and first_seen >= cutoff:
                record("weather", "new",
                       MAJOR if str(row.get("group_type") or "").lower()
                       in ("major", "severe") else NOTABLE,
                       "New BoM warning issued",
                       issue_ts or first_seen, entity_key=warning_id,
                       entity_name=title, metric="issue_time",
                       new_label=weather_data._pretty_type(row.get("type")),
                       url=url)
            continue
        if issue_stamp and prev_issue[1] and prev_issue[1] != issue_stamp:
            versions = database.read_df(
                "SELECT COUNT(*) AS n FROM weather_warning_updates "
                "WHERE warning_id = ?", [warning_id])
            count = int(versions.iloc[0]["n"]) if not versions.empty else 0
            record("weather", "reissued", NOTABLE, "BoM warning reissued",
                   issue_ts, entity_key=warning_id, entity_name=title,
                   metric="issue_time", prev_label=prev_issue[1][11:16],
                   new_label=issue_stamp[11:16],
                   since=_parse_ts(prev_issue[1]),
                   detail=[f"Version {count}" if count else None,
                           weather_data._pretty_type(row.get("type"))],
                   url=url)


def _detect_roads(cfg, cutoff):
    """Road closures opening and clearing (road_disruptions is an upsert, so
    the closure state is tracked through intel_metrics)."""
    df = database.read_df(
        "SELECT source_id, road_name, location, is_closure, status, lga, "
        "disruption_type, ses_region, latitude, longitude, first_seen, "
        "last_seen, resolved FROM road_disruptions")
    if df.empty:
        return
    for _, row in df.iterrows():
        key = row.get("source_id")
        if not key:
            continue
        road = row.get("road_name") or row.get("location") or "Road"
        closed = int(row.get("is_closure") or 0) and not int(row.get("resolved") or 0)
        ts = _parse_ts(row.get("last_seen")) or _now()
        lat, lon = _num(row.get("latitude")), _num(row.get("longitude"))
        prev = _metric_prev("roads", key, "closed")
        _metric_record("roads", key, "closed", 1 if closed else 0,
                       "Closed" if closed else "Open", ts)
        if prev is None:
            first_seen = _parse_ts(row.get("first_seen"))
            if closed and first_seen and first_seen >= cutoff:
                record("roads", "new", NOTABLE, f"{road} closed", first_seen,
                       entity_key=key, entity_name=road, metric="closed",
                       latitude=lat, longitude=lon,
                       detail=[row.get("disruption_type"),
                               row.get("location"),
                               f"LGA: {row['lga']}" if row.get("lga") else None],
                       url="/roads")
            continue
        prev_closed = prev[0]
        if prev_closed is None or bool(prev_closed) == bool(closed) or ts < cutoff:
            continue
        record("roads", "new" if closed else "cleared",
               NOTABLE if closed else INFO,
               f"{road} {'closed' if closed else 'reopened'}", ts,
               entity_key=key, entity_name=road, metric="closed",
               prev_label="Closed" if prev_closed else "Open",
               new_label="Closed" if closed else "Open", since=prev[2],
               latitude=lat, longitude=lon,
               detail=[row.get("disruption_type"), row.get("location")],
               url="/roads")


def _detect_rainfall(cfg, cutoff):
    """Rainfall bursts on the AWS network, using the reset-proof positive-
    increment total so a 9am counter reset never reads as a downpour."""
    settings = cfg["intel"]
    window = int(settings["rain_window_minutes"])
    start = _stamp(_now() - timedelta(minutes=window))
    df = database.read_df(
        "SELECT wmo, name, rain_since_9am_mm, obs_time FROM rainfall_aws "
        "WHERE obs_time >= ? ORDER BY wmo, obs_time", [start])
    if df.empty:
        return
    df["rain_since_9am_mm"] = pd.to_numeric(df["rain_since_9am_mm"],
                                            errors="coerce")
    coords = database.read_df(
        "SELECT wmo, latitude, longitude FROM aws_stations "
        "WHERE latitude IS NOT NULL")
    positions = {str(r["wmo"]): (_num(r["latitude"]), _num(r["longitude"]))
                 for _, r in coords.iterrows()}
    suppress = timedelta(minutes=int(settings["repeat_suppress_minutes"]))

    for wmo, group in df.groupby("wmo"):
        if len(group) < 2:
            continue
        total = weather_data._window_total(group["rain_since_9am_mm"].tolist())
        if total < float(settings["rain_burst_mm"]):
            continue
        latest_ts = _parse_ts(group.iloc[-1]["obs_time"])
        if latest_ts is None or latest_ts < cutoff:
            continue
        if _event_exists("rainfall", wmo, "burst", _now() - suppress):
            continue
        name = group.iloc[-1]["name"] or wmo
        lat, lon = positions.get(str(wmo), (None, None))
        record("rainfall", "burst",
               MAJOR if total >= float(settings["rain_burst_mm"]) * 2 else NOTABLE,
               f"Heavy rainfall at {name}", latest_ts, entity_key=str(wmo),
               entity_name=name, metric="rain_mm", new_value=total, unit="mm",
               since=_parse_ts(group.iloc[0]["obs_time"]),
               detail=[f"Accumulated over the last {window} minutes"],
               latitude=lat, longitude=lon, url="/weather")


_DETECTORS = (_detect_fire, _detect_flood, _detect_storm, _detect_weather,
              _detect_power, _detect_roads, _detect_rainfall)


def detect():
    """One detection pass over every module. Each detector is isolated: a
    source that is empty, broken or mid-migration cannot take the feed down.
    Returns the number of new journal entries written."""
    cfg = load_config()
    cutoff = _now() - timedelta(minutes=int(cfg["intel"]["lookback_minutes"]))
    # Record and score flood trend projections BEFORE the detectors run, so a
    # flood entry written this pass can quote the projection made from the same
    # reading rather than the previous one. Gated to its own slower interval:
    # gauges report every ~15 min, so running this every 60 s redid the same
    # work against unchanged data — and at production scale that work took
    # longer than the interval itself.
    if _projection_due(cfg):
        try:
            flood_trend.run_projection_cycle(cfg)
        except Exception:
            log.exception("Flood projection cycle failed")
        finally:
            # Stamp AFTER the run, so a slow cycle spaces itself out rather
            # than starting again the instant it finishes.
            global _last_projection_run
            _last_projection_run = _now()
    before = _event_count()
    for detector in _DETECTORS:
        try:
            detector(cfg, cutoff)
        except Exception:
            log.exception("Intel detector %s failed", detector.__name__)
    written = _event_count() - before
    if written:
        log.info("Intelligence feed: %d new entr%s",
                 written, "y" if written == 1 else "ies")
    return written


def _projection_due(cfg):
    """Whether the flood projection cycle is due to run again.

    In-process only: a restart runs it once immediately, which is harmless and
    means a fresh deploy does not wait out the interval before projecting."""
    interval = float(cfg["intel"].get("projection_interval_seconds", 300))
    if _last_projection_run is None:
        return True
    return (_now() - _last_projection_run).total_seconds() >= interval


def _event_count():
    df = database.read_df("SELECT COUNT(*) AS n FROM intel_events")
    return int(df.iloc[0]["n"]) if not df.empty else 0


# --------------------------------------------------------------------------- #
# Cross-layer context (computed at read time, never stored)
# --------------------------------------------------------------------------- #
def _context_sources():
    """Current positions of every located layer, for proximity context."""
    roads = database.read_df(
        "SELECT road_name, latitude, longitude, is_closure FROM road_disruptions "
        "WHERE resolved = 0 AND latitude IS NOT NULL")
    outages = database.read_df(
        "SELECT o.location, o.customers_off, g.latitude, g.longitude "
        "FROM power_outages o JOIN geocode_cache g ON g.location = o.location "
        "WHERE o.restored = 0 AND g.latitude IS NOT NULL")
    gauges = flood_data.map_gauges()
    if not gauges.empty:
        gauges = gauges[gauges["priority"] < 4]
    fires = database.read_df(
        "SELECT location, category1, warning_level, feed_type, latitude, "
        "longitude FROM fire_incidents WHERE resolved = 0 "
        "AND feed_type != 'burn-area' AND latitude IS NOT NULL")
    return {"roads": roads, "outages": outages, "gauges": gauges, "fires": fires}


def _nearby_lines(lat, lon, radius_km, sources, hazard):
    """The '3 road disruptions within 10 km' lines for one entry. The entry's
    own hazard is skipped so a fire is not told about itself."""
    if lat is None or lon is None:
        return []

    def within(df, lat_col="latitude", lon_col="longitude"):
        # Vectorised on purpose: this runs for every rendered entry against
        # every located row of every layer, on a 20-second refresh. A per-row
        # .apply here costs a visible fraction of a second once the road feed
        # is carrying a thousand disruptions.
        if df is None or df.empty:
            return df
        lats = pd.to_numeric(df[lat_col], errors="coerce")
        lons = pd.to_numeric(df[lon_col], errors="coerce")
        return df[_haversine_km_series(lat, lon, lats, lons) <= radius_km]

    lines = []
    roads = within(sources["roads"])
    if roads is not None and not roads.empty:
        closures = int(pd.to_numeric(roads["is_closure"],
                                     errors="coerce").fillna(0).sum())
        text = _plural(len(roads), "road disruption")
        if closures:
            text += f" ({closures} full closure{'s' if closures > 1 else ''})"
        lines.append(f"{text} within {radius_km:g} km")

    outages = within(sources["outages"])
    if outages is not None and not outages.empty:
        customers = int(pd.to_numeric(outages["customers_off"],
                                      errors="coerce").fillna(0).sum())
        lines.append(f"{_plural(len(outages), 'power outage')} nearby — "
                     f"{_fmt(customers)} customers off")

    if hazard != "flood":
        gauges = within(sources["gauges"])
        if gauges is not None and not gauges.empty:
            lines.append(f"{_plural(len(gauges), 'gauge')} at or above minor "
                         f"flood level within {radius_km:g} km")

    if hazard != "fire":
        fires = within(sources["fires"])
        if fires is not None and not fires.empty:
            warnings = fires[fires["feed_type"] == "warning"]
            text = _plural(len(fires), "active incident")
            if not warnings.empty:
                text += f", {len(warnings)} under warning"
            lines.append(f"{text} within {radius_km:g} km")
    return lines


def _own_warning_line(entry, sources, radius_km):
    """'Watch and Act remains current' — the standing warning level covering a
    fire entry's own location, which is on a SEPARATE feed row from the fire."""
    if entry["hazard"] != "fire" or entry["latitude"] is None:
        return None
    # An entry that IS about a warning level must not restate it as context.
    if entry["metric"] == "warning_level":
        return None
    fires = sources["fires"]
    if fires is None or fires.empty:
        return None
    warnings = fires[fires["feed_type"] == "warning"]
    best = None
    for _, row in warnings.iterrows():
        level = str(row.get("warning_level") or "").strip()
        ordinal = _WARNING_ORDINAL.get(level.lower())
        if ordinal is None:
            continue
        distance = _haversine_km(entry["latitude"], entry["longitude"],
                                 float(row["latitude"]), float(row["longitude"]))
        if distance <= radius_km and (best is None or ordinal < best[0]):
            best = (ordinal, level)
    return f"{best[1]} remains current" if best else None


# --------------------------------------------------------------------------- #
# Read API
# --------------------------------------------------------------------------- #
def _text(row, key):
    """A row field as clean text, or None. Necessary because pandas reads a
    SQL NULL back as NaN — which is TRUTHY, so a plain `row.get(k) or ...`
    happily renders the string 'nan' into the feed."""
    value = row.get(key)
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    text = str(value).strip()
    return text or None


def _delta_line(row):
    """The quantified middle line: '214 -> 296 ha since 12:05', or
    'Moderate -> Strong'."""
    name = _text(row, "entity_name")
    prev_value, new_value = row.get("prev_value"), row.get("new_value")
    unit = _text(row, "unit") or ""
    prev_label, new_label = _text(row, "prev_label"), _text(row, "new_label")
    # Gauge heights are read as a pair, so they must line up decimal-for-decimal:
    # "6.10 → 6.37 m", never "6.1 → 6.37 m".
    dp = _UNIT_DP.get(unit)
    if pd.notna(prev_value) and pd.notna(new_value):
        body = (f"{_fmt(float(prev_value), dp)} → "
                f"{_fmt(float(new_value), dp)} {unit}").strip()
    elif pd.notna(new_value):
        body = f"{_fmt(float(new_value), dp)} {unit}".strip()
    elif prev_label and new_label:
        body = f"{prev_label} → {new_label}"
    elif new_label:
        body = new_label
    else:
        return None
    since = _parse_ts(_text(row, "since"))
    if since is not None:
        body += f" since {since.strftime('%H:%M')}"
    return f"{name}: {body}" if name else body


def entries(hours=24, hazards=None, min_severity=INFO, limit=120,
            with_context=True, context_limit=40):
    """Rendered feed entries, newest first.

    Each entry is a dict ready for the page: a time, a headline, one quantified
    delta line, and context lines. Context (proximity + standing warning level)
    is resolved live for the first ``context_limit`` entries only — that is the
    only expensive part and nobody reads past the top of a feed."""
    query = ["SELECT * FROM intel_events WHERE ts >= ?"]
    params = [_stamp(_now() - timedelta(hours=float(hours)))]
    if hazards:
        query.append("AND hazard IN (%s)" % ",".join("?" * len(hazards)))
        params += list(hazards)
    if min_severity:
        query.append("AND severity >= ?")
        params.append(int(min_severity))
    query.append("ORDER BY ts DESC, id DESC LIMIT ?")
    params.append(int(limit))
    df = database.read_df(" ".join(query), params)
    if df.empty:
        return []

    cfg = load_config()
    radius = float(cfg["intel"]["context_radius_km"])
    sources = _context_sources() if with_context else None

    out = []
    for position, (_, row) in enumerate(df.iterrows()):
        ts = _parse_ts(row["ts"])
        severity = int(row["severity"])
        entry = {
            "id": int(row["id"]),
            "ts": ts,
            "time": ts.strftime("%H:%M") if ts else "--:--",
            "date": ts.strftime("%a %d %b") if ts else "",
            "hazard": row["hazard"],
            "hazard_label": HAZARD_LABEL.get(row["hazard"], row["hazard"].title()),
            "kind": row["kind"],
            "metric": _text(row, "metric"),
            "severity": severity,
            "severity_label": SEVERITY_STYLE[severity][0],
            "colour": SEVERITY_STYLE[severity][1],
            "headline": row["headline"],
            "entity_name": _text(row, "entity_name"),
            "latitude": _num(row.get("latitude")),
            "longitude": _num(row.get("longitude")),
            "url": _text(row, "url") or HAZARD_PAGE.get(row["hazard"], "/"),
        }
        lines = []
        delta = _delta_line(row)
        if delta:
            lines.append(delta)
        rate = _text(row, "rate")
        if rate:
            lines.append(rate)
        detail = _text(row, "detail")
        for extra in (json.loads(detail) if detail else []):
            if extra:
                lines.append(str(extra))
        if sources is not None and position < context_limit:
            warning_line = _own_warning_line(entry, sources, radius)
            if warning_line:
                lines.append(warning_line)
            lines.extend(_nearby_lines(entry["latitude"], entry["longitude"],
                                       radius, sources, entry["hazard"]))
        entry["lines"] = lines
        out.append(entry)
    return out


def counts(hours=24):
    """Entries per severity in the window, for the feed's KPI row."""
    df = database.read_df(
        "SELECT severity, COUNT(*) AS n FROM intel_events WHERE ts >= ? "
        "GROUP BY severity", [_stamp(_now() - timedelta(hours=float(hours)))])
    tally = {level: 0 for level in SEVERITY_STYLE}
    for _, row in df.iterrows():
        tally[int(row["severity"])] = int(row["n"])
    return tally


def last_entry_time():
    df = database.read_df("SELECT MAX(ts) AS ts FROM intel_events")
    return _parse_ts(df.iloc[0]["ts"]) if not df.empty else None

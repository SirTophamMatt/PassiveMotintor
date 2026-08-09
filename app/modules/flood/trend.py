"""Flood rate-of-rise intelligence: rate, acceleration, threshold distance,
a trend projection (ETA) — and a verification record so the projections can be
scored against what actually happened.

    Rising rapidly
    +0.30 m/hr over the last 90 minutes
    0.45 m below Moderate
    Moderate potentially reached ~15:20-16:10
    38 mm rain in the surrounding catchment

**This is extrapolation, not hydrology.** It fits a straight line to recent
gauge readings and runs it forward. It knows nothing about catchment routing,
upstream tributaries, dam releases, tides or rainfall forecasts, all of which
BoM's actual flood forecasting models do. Every surface that shows an ETA MUST
carry ``DISCLAIMER``.

Honesty is enforced structurally rather than left to good intentions:

* **Every projection is written to ``flood_projections`` when it is made**, with
  the reading it was made from, and is later verified against the observations
  that followed (``verify_projections``). ``accuracy_summary`` then reports the
  real hit rate, the share that landed inside the quoted window, and the median
  error — so the projection's track record is visible next to the projection.
  Nothing here can quietly flatter itself.
* The quoted **range** comes from the standard error of the fitted slope plus,
  when the rise is accelerating, the faster arrival implied by that
  acceleration. A single number would imply a precision this method does not
  have.
* No ETA at all when the evidence is thin: too few readings, too short a
  window, a rate below ``trend_min_rate_m_hr``, or an arrival beyond
  ``trend_max_horizon_hours``.
"""
import logging
import math
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from app import database
from app.config import load_config
from app.modules.flood import data as flood_data

log = logging.getLogger(__name__)

DISCLAIMER = ("Trend projection — not an official flood forecast. "
              "Straight-line extrapolation of recent gauge readings; it does "
              "not model rainfall, catchment routing or upstream inflows. "
              "For official warnings and forecasts see the Bureau of "
              "Meteorology and VICSES.")

# Bumped whenever the maths changes, so accuracy can be compared like for like
# instead of mixing scores from two different methods in one average.
METHOD = "linear-se-v1"

CLASS_ORDER = ("minor", "moderate", "major")

# Verification outcomes.
PENDING, REACHED, NOT_REACHED, RECEDED = "pending", "reached", "not_reached", "receded"


# --------------------------------------------------------------------------- #
# Fitting
# --------------------------------------------------------------------------- #
def _ols(hours, heights):
    """Least-squares fit of height against hours.

    Returns (slope m/hr, intercept, standard error of the slope). The standard
    error is what turns a single ETA into an honest range; it is None when the
    fit has no residual degrees of freedom (n < 3) or the readings share one
    timestamp."""
    n = len(hours)
    x, y = np.asarray(hours, dtype=float), np.asarray(heights, dtype=float)
    sxx = float(((x - x.mean()) ** 2).sum())
    if n < 2 or sxx <= 0:
        return None, None, None
    slope = float(((x - x.mean()) * (y - y.mean())).sum() / sxx)
    intercept = float(y.mean() - slope * x.mean())
    if n < 3:
        return slope, intercept, None
    residuals = y - (intercept + slope * x)
    sse = float((residuals ** 2).sum())
    se = math.sqrt(sse / (n - 2) / sxx) if sse > 0 else 0.0
    return slope, intercept, se


def _rate_over(readings):
    """Fitted rate (m/hr) across a list of (datetime, height), or None."""
    if len(readings) < 2:
        return None
    t0 = readings[0][0]
    hours = [(t - t0).total_seconds() / 3600.0 for t, _ in readings]
    slope, _, _ = _ols(hours, [h for _, h in readings])
    return slope


# --------------------------------------------------------------------------- #
# The analysis
# --------------------------------------------------------------------------- #
def _next_target(current, levels, impacts=None):
    """The next threshold above the current height: the next flood class, or —
    once the gauge is already at Major — the next Local Flood Guide impact
    height, which is the thing an operator actually cares about from there.

    Returns (kind, name, height) or (None, None, None)."""
    if levels:
        for cls in CLASS_ORDER:
            value = levels.get(cls)
            if value is not None and pd.notna(value) and current < float(value):
                return "class", cls, float(value)
    if impacts is not None and not impacts.empty:
        above = impacts[impacts["height_m"] > current].sort_values("height_m")
        if not above.empty:
            row = above.iloc[0]
            return "impact", str(row["impact"])[:120], float(row["height_m"])
    return None, None, None


def _accel_label(recent, earlier, epsilon):
    if recent is None or earlier is None:
        return "Unknown", None
    delta = recent - earlier
    if delta > epsilon:
        return "Rate increasing", delta
    if delta < -epsilon:
        return "Rate easing", delta
    return "Steady", delta


def _headline(rate, rapid, accel_label):
    if rate is None:
        return "Insufficient data"
    if rate <= -0.02:
        return "Falling"
    if rate < 0.02:
        return "Steady"
    if rate >= rapid:
        return "Rising rapidly"
    if accel_label == "Rate increasing":
        return "Rising, accelerating"
    return "Rising"


def analyse(station_key, levels=None, impacts=None, cfg=None, now=None):
    """Rate-of-rise analysis for one gauge.

    Returns a dict that the station page, the briefing PDF and the Intelligence
    Feed all render from, or None when the gauge has no usable recent history.
    ``eta_*`` keys are absent unless a projection is genuinely supportable."""
    cfg = cfg or load_config()
    settings = cfg["flood"]
    now = now or datetime.now()
    station_key = str(station_key).strip().lower()

    window = int(settings["trend_window_minutes"])
    min_readings = int(settings["trend_min_readings"])
    min_rate = float(settings["trend_min_rate_m_hr"])
    rapid = float(settings["trend_rapid_rate_m_hr"])
    horizon = float(settings["trend_max_horizon_hours"])

    cutoff = now - timedelta(minutes=window)
    df = database.read_df(
        "SELECT timestamp, height_m FROM flood_observations "
        "WHERE LOWER(TRIM(station_name)) = ? AND timestamp >= ? "
        "ORDER BY timestamp", [station_key, cutoff.strftime("%Y-%m-%d %H:%M:%S")])
    if df.empty:
        return None
    df["height_m"] = pd.to_numeric(df["height_m"], errors="coerce")
    df["ts"] = pd.to_datetime(df["timestamp"], errors="coerce")
    df = df.dropna(subset=["height_m", "ts"])
    # De-duplicate: BoM occasionally republishes a reading, and repeated
    # identical timestamps would fake a vertical rise.
    df = df.drop_duplicates(subset=["ts"], keep="last").sort_values("ts")
    readings = [(t.to_pydatetime(), float(h))
                for t, h in zip(df["ts"], df["height_m"])]

    if levels is None:
        levels = flood_data.load_flood_levels().get(station_key)
    if impacts is None:
        impacts = flood_data.load_gauge_impacts(station_key)

    observed_at, current = readings[-1]
    span_hours = (observed_at - readings[0][0]).total_seconds() / 3600.0
    result = {
        "station_key": station_key,
        "observed_at": observed_at,
        "current_height": current,
        "readings": readings,
        "reading_count": len(readings),
        "window_minutes": window,
        "span_minutes": round(span_hours * 60),
        "levels": levels,
        "method": METHOD,
        "disclaimer": DISCLAIMER,
        "rate_m_hr": None, "rate_stderr": None,
        "accel_label": "Unknown", "accel_m_hr2": None,
        "target_kind": None, "target_name": None, "target_height": None,
        "distance_m": None, "eta_point": None, "eta_early": None,
        "eta_late": None, "eta_reason": None,
        "headline": "Insufficient data",
    }
    if len(readings) < min_readings or span_hours < 0.5:
        result["eta_reason"] = (
            f"Only {len(readings)} reading(s) in the last {window} minutes — "
            f"at least {min_readings} spanning 30 minutes are needed.")
        return result

    t0 = readings[0][0]
    hours = [(t - t0).total_seconds() / 3600.0 for t, _ in readings]
    heights = [h for _, h in readings]
    rate, _, stderr = _ols(hours, heights)
    result["rate_m_hr"] = rate
    result["rate_stderr"] = stderr

    # Acceleration: compare the fitted rate of the recent half against the
    # earlier half. More interpretable — and far more stable on noisy gauge
    # data — than fitting a quadratic and reading its second derivative.
    mid = len(readings) // 2
    earlier_rate = _rate_over(readings[:mid + 1])
    recent_rate = _rate_over(readings[mid:])
    epsilon = max(0.01, (stderr or 0.0))
    label, delta = _accel_label(recent_rate, earlier_rate, epsilon)
    result["accel_label"] = label
    result["rate_recent_m_hr"] = recent_rate
    result["rate_earlier_m_hr"] = earlier_rate
    if delta is not None and span_hours > 0:
        result["accel_m_hr2"] = delta / max(span_hours / 2, 1e-6)
    result["headline"] = _headline(rate, rapid, label)

    kind, name, target = _next_target(current, levels, impacts)
    result["target_kind"], result["target_name"] = kind, name
    result["target_height"] = target
    if target is None:
        result["eta_reason"] = "Already above every known threshold."
        return result
    distance = target - current
    result["distance_m"] = distance

    if rate is None or rate < min_rate:
        result["eta_reason"] = (
            "Not rising fast enough to project "
            f"(rate below {min_rate:g} m/hr).")
        return result

    # Point estimate: hold the current rate.
    t_point = distance / rate
    if t_point > horizon:
        result["eta_reason"] = (
            f"At the current rate the threshold is more than {horizon:g} "
            "hours away — too far out to project usefully.")
        return result

    # Range: the slope's own uncertainty, widened by the arrival implied by any
    # acceleration. A faster-than-linear rise arrives EARLY, so acceleration
    # moves the early bound, never the point estimate.
    candidates = [t_point]
    if stderr:
        fast = rate + stderr
        slow = rate - stderr
        if fast > 0:
            candidates.append(distance / fast)
        candidates.append(distance / slow if slow > min_rate else horizon)
    accel = result["accel_m_hr2"]
    if accel and accel > 0:
        # distance = rate*t + 0.5*accel*t^2, positive root.
        disc = rate ** 2 + 2 * accel * distance
        if disc > 0:
            t_accel = (-rate + math.sqrt(disc)) / accel
            if t_accel > 0:
                candidates.append(t_accel)

    t_early = min(candidates)
    t_late = max(candidates)

    # Floor the width. On a smoothly-rising gauge the residuals are tiny, the
    # slope's standard error collapses, and the band would tighten to a few
    # minutes either side of a 3-hour projection — precision this method has
    # not earned. The window is never narrower than +/- trend_min_range_pct of
    # the lead time, so how vague it looks scales with how far ahead it reaches.
    floor = t_point * float(settings["trend_min_range_pct"]) / 100.0
    t_early = max(min(t_early, t_point - floor), 1 / 60)
    t_late = min(max(t_late, t_point + floor), horizon)

    result["eta_point"] = observed_at + timedelta(hours=t_point)
    result["eta_early"] = observed_at + timedelta(hours=t_early)
    result["eta_late"] = observed_at + timedelta(hours=t_late)
    return result


# --------------------------------------------------------------------------- #
# Catchment rainfall
# --------------------------------------------------------------------------- #
def catchment_rainfall(station_key, cfg=None, now=None):
    """Rain accumulated near a gauge over the recent window.

    Uses the **AWS network** (103 VIC stations, all with coordinates) within
    ``trend_rainfall_radius_km`` of the gauge rather than the per-town
    ``rainfall_observations`` table, which is only seeded for a handful of
    locations. Totals use the reset-proof positive-increment sum, so the 9am
    counter reset never reads as a downpour.

    Returns None when the gauge has no coordinates or no station is in range."""
    cfg = cfg or load_config()
    now = now or datetime.now()
    radius = float(cfg["flood"]["trend_rainfall_radius_km"])
    hours = float(cfg["flood"]["trend_rainfall_window_hours"])
    station_key = str(station_key).strip().lower()

    gauge = database.read_df(
        "SELECT latitude, longitude FROM gauge_coords WHERE station_key = ? "
        "AND latitude IS NOT NULL", [station_key])
    if gauge.empty:
        return None
    lat, lon = float(gauge.iloc[0]["latitude"]), float(gauge.iloc[0]["longitude"])

    stations = database.read_df(
        "SELECT wmo, name, latitude, longitude FROM aws_stations "
        "WHERE latitude IS NOT NULL")
    if stations.empty:
        return None
    stations["distance_km"] = _haversine_series(
        lat, lon, stations["latitude"], stations["longitude"])
    near = stations[stations["distance_km"] <= radius]
    if near.empty:
        return None

    since = (now - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    from app.modules.weather import data as weather_data
    totals = []
    for _, row in near.sort_values("distance_km").iterrows():
        obs = database.read_df(
            "SELECT rain_since_9am_mm FROM rainfall_aws "
            "WHERE wmo = ? AND obs_time >= ? ORDER BY obs_time",
            [str(row["wmo"]), since])
        if len(obs) < 2:
            continue
        total = weather_data._window_total(
            pd.to_numeric(obs["rain_since_9am_mm"], errors="coerce").tolist())
        totals.append({"name": row["name"], "wmo": str(row["wmo"]),
                       "distance_km": float(row["distance_km"]),
                       "total_mm": float(total)})
    if not totals:
        return None
    catchment = database.read_df(
        "SELECT catchment FROM flood_observations "
        "WHERE LOWER(TRIM(station_name)) = ? AND catchment IS NOT NULL "
        "ORDER BY timestamp DESC LIMIT 1", [station_key])
    return {
        "stations": totals,
        "station_count": len(totals),
        # The wettest nearby station, not the mean: an average across a wide
        # radius hides the cell that is actually driving the rise.
        "max_mm": max(t["total_mm"] for t in totals),
        "wettest": max(totals, key=lambda t: t["total_mm"])["name"],
        "radius_km": radius,
        "window_hours": hours,
        "catchment": (catchment.iloc[0]["catchment"]
                      if not catchment.empty else None),
    }


def _haversine_series(lat, lon, lats, lons):
    p1 = math.radians(lat)
    p2 = np.radians(lats.to_numpy(dtype=float))
    dl = np.radians(lons.to_numpy(dtype=float) - lon)
    a = (np.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * np.cos(p2) * np.sin(dl / 2) ** 2)
    km = 2 * 6371.0 * np.arcsin(np.sqrt(a))
    return pd.Series(np.where(np.isnan(km), np.inf, km), index=lats.index)


# --------------------------------------------------------------------------- #
# Recording projections (so they can be scored later)
# --------------------------------------------------------------------------- #
def _fmt(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S") if isinstance(dt, datetime) else dt


def record_projection(analysis, station_name=None):
    """Persist a projection so it can be verified later. No-op unless the
    analysis actually produced an ETA.

    De-duped on (station, target, observed_at): one projection per gauge per
    threshold per observation, however often the detector runs."""
    if not analysis or not analysis.get("eta_point"):
        return False
    row = {
        "station_key": analysis["station_key"],
        "station_name": station_name or analysis["station_key"],
        "made_at": _fmt(datetime.now()),
        "observed_at": _fmt(analysis["observed_at"]),
        "current_height": analysis["current_height"],
        "target_kind": analysis["target_kind"],
        "target_name": analysis["target_name"],
        "target_height": analysis["target_height"],
        "distance_m": analysis["distance_m"],
        "rate_m_hr": analysis["rate_m_hr"],
        "rate_stderr": analysis["rate_stderr"],
        "accel_m_hr2": analysis["accel_m_hr2"],
        "accel_label": analysis["accel_label"],
        "readings_used": analysis["reading_count"],
        "span_minutes": analysis["span_minutes"],
        "eta_point": _fmt(analysis["eta_point"]),
        "eta_early": _fmt(analysis["eta_early"]),
        "eta_late": _fmt(analysis["eta_late"]),
        "lead_minutes": round(
            (analysis["eta_point"] - analysis["observed_at"]).total_seconds() / 60),
        "rainfall_mm": analysis.get("rainfall_mm"),
        "method": analysis["method"],
        "outcome": PENDING,
        "actual_ts": None, "error_minutes": None,
        "within_range": None, "verified_at": None,
    }
    return database.insert_rows("flood_projections", [row],
                                ignore_duplicates=True) > 0


def verify_projections(cfg=None, now=None, limit=None):
    """Score every pending projection against what the gauge actually did.

    A projection is REACHED when an observation at or above the target height
    arrives after the reading it was made from; NOT_REACHED once enough time
    has passed beyond the late bound that we would be fooling ourselves to keep
    waiting; RECEDED when the gauge instead fell well back before the deadline
    (a different kind of miss, worth separating — the projection was wrong, but
    because the event turned, not because the arithmetic was off).

    Returns the number of projections resolved."""
    cfg = cfg or load_config()
    now = now or datetime.now()
    grace = timedelta(hours=float(cfg["flood"]["projection_grace_hours"]))
    if limit is None:
        limit = int(cfg["intel"].get("projection_verify_limit", 100))

    pending = database.read_df(
        "SELECT * FROM flood_projections WHERE outcome = ? "
        "ORDER BY id LIMIT ?", [PENDING, int(limit)])
    if pending.empty:
        return 0

    resolved = 0
    for _, row in pending.iterrows():
        target = float(row["target_height"])
        observed_at = row["observed_at"]
        hit = database.read_df(
            "SELECT MIN(timestamp) AS ts FROM flood_observations "
            "WHERE LOWER(TRIM(station_name)) = ? AND timestamp > ? "
            "AND height_m >= ?", [row["station_key"], observed_at, target])
        actual = hit.iloc[0]["ts"] if not hit.empty else None

        if actual:
            actual_dt = pd.to_datetime(actual, errors="coerce")
            if pd.isna(actual_dt):
                continue
            actual_dt = actual_dt.to_pydatetime()
            eta_point = pd.to_datetime(row["eta_point"]).to_pydatetime()
            early = pd.to_datetime(row["eta_early"]).to_pydatetime()
            late = pd.to_datetime(row["eta_late"]).to_pydatetime()
            # Positive error = the threshold arrived LATER than projected
            # (we cried wolf early); negative = it beat the projection.
            error = (actual_dt - eta_point).total_seconds() / 60.0
            _resolve(row["id"], REACHED, _fmt(actual_dt), error,
                     1 if early <= actual_dt <= late else 0, now)
            resolved += 1
            continue

        deadline = pd.to_datetime(row["eta_late"]).to_pydatetime() + grace
        if now < deadline:
            continue  # still genuinely open — say nothing yet

        # Past the deadline and never reached. Did it recede instead?
        latest = database.read_df(
            "SELECT height_m FROM flood_observations "
            "WHERE LOWER(TRIM(station_name)) = ? AND timestamp > ? "
            "ORDER BY timestamp DESC LIMIT 1",
            [row["station_key"], observed_at])
        outcome = NOT_REACHED
        if not latest.empty:
            height = pd.to_numeric(latest.iloc[0]["height_m"], errors="coerce")
            if pd.notna(height) and float(height) < float(row["current_height"]):
                outcome = RECEDED
        _resolve(row["id"], outcome, None, None, 0, now)
        resolved += 1
    return resolved


def _resolve(projection_id, outcome, actual_ts, error_minutes, within_range, now):
    database.execute(
        "UPDATE flood_projections SET outcome = ?, actual_ts = ?, "
        "error_minutes = ?, within_range = ?, verified_at = ? WHERE id = ?",
        [outcome, actual_ts, error_minutes, within_range, _fmt(now),
         int(projection_id)])


def run_projection_cycle(cfg=None):
    """Record a projection for every gauge that currently supports one, then
    verify whatever has come due. Driven by the intel collector.

    Only gauges at or approaching a flood class are considered — projecting a
    dry-weather wobble on 480 gauges would be noise, and would pad the accuracy
    stats with trivial cases."""
    cfg = cfg or load_config()
    levels = flood_data.load_flood_levels()
    if not levels:
        return 0, 0
    latest = database.read_df(
        "SELECT station_name, height_m, MAX(timestamp) AS ts "
        "FROM flood_observations GROUP BY station_name")
    made = 0
    for _, row in latest.iterrows():
        station_name = row["station_name"]
        key = str(station_name).strip().lower()
        level = levels.get(key)
        if not level:
            continue
        height = pd.to_numeric(row["height_m"], errors="coerce")
        minor = level.get("minor")
        if pd.isna(height) or minor is None or pd.isna(minor):
            continue
        # Within 20% below minor, or already in flood.
        if float(height) < float(minor) * 0.8:
            continue
        try:
            analysis = analyse(key, levels=level, cfg=cfg)
        except Exception:
            log.exception("Trend analysis failed for %s", station_name)
            continue
        if analysis and record_projection(analysis, station_name):
            made += 1
    verified = verify_projections(cfg=cfg)
    if made or verified:
        log.info("Flood projections: %d recorded, %d verified", made, verified)
    return made, verified


# --------------------------------------------------------------------------- #
# Accuracy reporting — the back-check
# --------------------------------------------------------------------------- #
def accuracy_summary(station_key=None, days=90, method=METHOD):
    """How well the projections have actually done.

    ``hit_rate`` is the share of projections whose threshold was reached at
    all; ``within_range_rate`` is the share of THOSE that landed inside the
    quoted window — the number that says whether the range is honest.
    ``median_error_minutes`` is signed (positive = the water arrived later than
    projected, i.e. the projection was too eager)."""
    query = ["SELECT * FROM flood_projections WHERE outcome != ?"]
    params = [PENDING]
    if station_key:
        query.append("AND station_key = ?")
        params.append(str(station_key).strip().lower())
    if method:
        query.append("AND method = ?")
        params.append(method)
    if days:
        query.append("AND made_at >= datetime('now', 'localtime', ?)")
        params.append(f"-{int(days)} days")
    df = database.read_df(" ".join(query), params)

    empty = {"total": 0, "reached": 0, "not_reached": 0, "receded": 0,
             "hit_rate": None, "within_range_rate": None,
             "median_error_minutes": None, "median_abs_error_minutes": None,
             "pending": _pending_count(station_key), "by_lead": [],
             "by_target": [], "method": method}
    if df.empty:
        return empty

    reached = df[df["outcome"] == REACHED]
    errors = pd.to_numeric(reached["error_minutes"], errors="coerce").dropna()
    summary = dict(empty)
    summary.update({
        "total": len(df),
        "reached": len(reached),
        "not_reached": int((df["outcome"] == NOT_REACHED).sum()),
        "receded": int((df["outcome"] == RECEDED).sum()),
        "hit_rate": len(reached) / len(df) if len(df) else None,
        "within_range_rate": (
            float(pd.to_numeric(reached["within_range"],
                                errors="coerce").fillna(0).mean())
            if len(reached) else None),
        "median_error_minutes": float(errors.median()) if len(errors) else None,
        "median_abs_error_minutes": (float(errors.abs().median())
                                     if len(errors) else None),
        "by_lead": _bucket_by_lead(df),
        "by_target": _bucket_by_target(df),
    })
    return summary


def _pending_count(station_key=None):
    query = "SELECT COUNT(*) AS n FROM flood_projections WHERE outcome = ?"
    params = [PENDING]
    if station_key:
        query += " AND station_key = ?"
        params.append(str(station_key).strip().lower())
    df = database.read_df(query, params)
    return int(df.iloc[0]["n"]) if not df.empty else 0


_LEAD_BUCKETS = [(0, 60, "Under 1 hr"), (60, 180, "1–3 hr"),
                 (180, 360, "3–6 hr"), (360, 10 ** 9, "Over 6 hr")]


def _bucket_by_lead(df):
    """Accuracy split by how far ahead the call was made — a projection 20
    minutes out and one six hours out are not the same claim."""
    lead = pd.to_numeric(df["lead_minutes"], errors="coerce")
    out = []
    for low, high, label in _LEAD_BUCKETS:
        subset = df[(lead >= low) & (lead < high)]
        if subset.empty:
            continue
        reached = subset[subset["outcome"] == REACHED]
        errors = pd.to_numeric(reached["error_minutes"], errors="coerce").dropna()
        out.append({
            "label": label, "total": len(subset), "reached": len(reached),
            "hit_rate": len(reached) / len(subset),
            "within_range_rate": (
                float(pd.to_numeric(reached["within_range"],
                                    errors="coerce").fillna(0).mean())
                if len(reached) else None),
            "median_abs_error_minutes": (float(errors.abs().median())
                                         if len(errors) else None),
        })
    return out


def _bucket_by_target(df):
    out = []
    for name, subset in df.groupby("target_name"):
        reached = subset[subset["outcome"] == REACHED]
        errors = pd.to_numeric(reached["error_minutes"], errors="coerce").dropna()
        out.append({
            "label": str(name).title(), "total": len(subset),
            "reached": len(reached),
            "hit_rate": len(reached) / len(subset),
            "median_abs_error_minutes": (float(errors.abs().median())
                                         if len(errors) else None),
        })
    return sorted(out, key=lambda r: -r["total"])


def recent_projections(station_key=None, limit=25, verified_only=False):
    """Latest projections with their outcome, for the back-check tables."""
    query = ["SELECT * FROM flood_projections WHERE 1 = 1"]
    params = []
    if station_key:
        query.append("AND station_key = ?")
        params.append(str(station_key).strip().lower())
    if verified_only:
        query.append("AND outcome != ?")
        params.append(PENDING)
    query.append("ORDER BY made_at DESC, id DESC LIMIT ?")
    params.append(int(limit))
    return database.read_df(" ".join(query), params)

"""Event Replay data layer: what did Passive Monitor know at a given moment?

Reconstruction pulls from two kinds of storage, and which one is used per layer
is a deliberate decision, not an accident:

* **Sources that already keep true history** — flood observations, storm cells,
  AWS observations, and every module's per-cycle KPI timeseries — are read
  directly. They are the primary historical record for their own layer, they
  predate the state journal, and duplicating them into it would be pure waste.
* **UPSERT sources** — fire incidents, road disruptions, per-location power
  outages, weather warnings — have no past of their own, so they are read from
  `app.history`'s change journal.

That split is why historical KPIs can reach back further than the map can: the
timeseries tables have been recording counts since each module was built, while
per-entity geometry only exists from the day the journal started. `coverage()`
exists to state that difference plainly rather than let a partial
reconstruction pass for a complete one.

Nothing here fetches from an external source. Replay runs entirely from stored
data — playback must never hit BoM or VicEmergency.
"""
import logging
from datetime import datetime, timedelta

import pandas as pd

from app import database, history, intel_feed, tags
from app.modules.flood import data as flood_data

log = logging.getLogger(__name__)

# A gauge/station whose last reading is older than this before the replay
# moment is treated as not reporting, rather than drawn as though it were
# current. Without it, a gauge that fell silent in March would still be painted
# on an August replay.
STALE_READING_HOURS = 24
# Mirrors storm_data.ACTIVE_WINDOW_MINUTES: a cell unseen this long has gone.
STORM_ACTIVE_MINUTES = 20

_TS_FMT = "%Y-%m-%d %H:%M:%S"


def _stamp(value):
    if isinstance(value, datetime):
        return value.strftime(_TS_FMT)
    parsed = pd.to_datetime(value, errors="coerce")
    return (datetime.now() if pd.isna(parsed) else parsed.to_pydatetime()).strftime(_TS_FMT)


def _to_dt(value):
    parsed = pd.to_datetime(value, errors="coerce")
    return None if pd.isna(parsed) else parsed.to_pydatetime()


# --------------------------------------------------------------------------- #
# Events
# --------------------------------------------------------------------------- #
def event_options():
    """[(label, tag_id)] for the event selector, most recent first."""
    out = []
    for tag in tags.list_tags():
        label = tag["name"]
        start = _to_dt(tag["start_ts"])
        if start:
            label += "  ·  %s" % start.strftime("%d %b %Y")
        if not tag.get("end_ts"):
            label += "  (ongoing)"
        out.append((label, tag["id"]))
    return out


def event_window(tag_id):
    """{name, start, end, ongoing, duration} for an event tag, or None.

    An ongoing event replays from its start until now.
    """
    tag = tags.get_tag(tag_id)
    if not tag:
        return None
    start = _to_dt(tag["start_ts"])
    if start is None:
        return None
    ongoing = not tag.get("end_ts")
    end = datetime.now() if ongoing else _to_dt(tag["end_ts"])
    if end is None or end < start:
        end = start
    return {"id": tag["id"], "name": tag["name"], "start": start, "end": end,
            "ongoing": ongoing, "duration": end - start,
            "notes": tag.get("notes")}


def format_duration(delta):
    minutes = int(max(0, delta.total_seconds()) // 60)
    days, rem = divmod(minutes, 1440)
    hours, mins = divmod(rem, 60)
    if days:
        return "%dd %dh" % (days, hours)
    if hours:
        return "%dh %02dm" % (hours, mins)
    return "%dm" % mins


# --------------------------------------------------------------------------- #
# Historical layer frames — fed to the SHARED unified-map renderers
# --------------------------------------------------------------------------- #
def fire_at(when):
    return history.state_at(history.FIRE, when)


def roads_at(when):
    return history.state_at(history.ROADS, when)


def power_at(when):
    """Per-location outages as they stood, with coordinates.

    The journal already carries the coordinates it had at record time. A
    location geocoded only AFTER its outage was journalled would have been
    recorded without them, so the current cache fills those gaps — that is the
    one case where using today's data is right, because a town's coordinates
    are not a thing that changed during the event.
    """
    df = history.state_at(history.POWER, when)
    if df.empty or "latitude" not in df.columns:
        return df
    missing = df["latitude"].isna()
    if not missing.any():
        return df
    cache = database.read_df(
        "SELECT location, latitude AS _lat, longitude AS _lon FROM geocode_cache")
    if cache.empty:
        return df
    lookup = cache.set_index("location")
    df = df.copy()
    for idx in df.index[missing]:
        key = df.at[idx, "entity_key"]
        if key in lookup.index:
            df.at[idx, "latitude"] = lookup.at[key, "_lat"]
            df.at[idx, "longitude"] = lookup.at[key, "_lon"]
    return df


def weather_warnings_at(when):
    return history.state_at(history.WEATHER_WARNING, when)


def flood_at(when):
    """Gauges as they read at `when`, in the same shape `map_gauges()` returns
    so the shared flood renderer draws them unchanged.

    Read from `flood_observations`, which has always kept true per-reading
    history — this layer replays correctly for events long predating the
    journal.
    """
    stamp = _stamp(when)
    cutoff = _stamp(_to_dt(stamp) - timedelta(hours=STALE_READING_HOURS))
    df = database.read_df(
        "SELECT o.station_name, o.height_m, g.latitude, g.longitude FROM ("
        "  SELECT station_name, height_m, MAX(timestamp) AS ts "
        "  FROM flood_observations WHERE timestamp <= ? AND timestamp >= ? "
        "  GROUP BY station_name) o "
        "JOIN gauge_coords g ON g.station_key = LOWER(TRIM(o.station_name)) "
        "WHERE g.latitude IS NOT NULL AND g.longitude IS NOT NULL",
        [stamp, cutoff])
    if df.empty:
        return df
    levels = flood_data.load_flood_levels()
    df["height_m"] = pd.to_numeric(df["height_m"], errors="coerce")
    rows = [flood_data.classify_station(
        h, levels.get(str(n).strip().lower()))
        for n, h in zip(df["station_name"], df["height_m"])]
    df["priority"] = [r[0] for r in rows]
    df["label"] = [r[1] for r in rows]
    df["colour"] = [r[2] for r in rows]
    return df


def storm_at(when):
    """The latest observation of every cell seen shortly before `when`.
    Mirrors `storm_data.active_cells`, anchored to the replay moment."""
    stamp = _stamp(when)
    cutoff = _stamp(_to_dt(stamp) - timedelta(minutes=STORM_ACTIVE_MINUTES))
    return database.read_df(
        "SELECT c.* FROM storm_cells c JOIN ("
        "  SELECT cell_id, MAX(frame_ts) AS latest FROM storm_cells "
        "  WHERE frame_ts <= ? GROUP BY cell_id) t "
        "ON c.cell_id = t.cell_id AND c.frame_ts = t.latest "
        "WHERE c.frame_ts >= ? ORDER BY c.intensity_score DESC",
        [stamp, cutoff])


def aws_at(when):
    """Each AWS station's observation as at `when`, with registry coords."""
    stamp = _stamp(when)
    cutoff = _stamp(_to_dt(stamp) - timedelta(hours=STALE_READING_HOURS))
    df = database.read_df(
        "SELECT r.wmo, r.name, r.obs_time, r.rain_since_9am_mm, "
        "       r.temperature_c, r.relative_humidity_pct, r.wind_direction, "
        "       r.wind_speed_kmh, r.wind_gust_kmh, s.latitude, s.longitude "
        "FROM rainfall_aws r JOIN ("
        "  SELECT wmo, MAX(obs_time) AS mt FROM rainfall_aws "
        "  WHERE obs_time <= ? AND obs_time >= ? GROUP BY wmo) m "
        "ON r.wmo = m.wmo AND r.obs_time = m.mt "
        "LEFT JOIN aws_stations s ON s.wmo = r.wmo", [stamp, cutoff])
    if df.empty:
        return df
    for col in ("rain_since_9am_mm", "temperature_c", "relative_humidity_pct",
                "wind_speed_kmh", "wind_gust_kmh"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


# Layer key -> historical frame provider. Keys match the unified map's, so the
# same renderers draw both maps.
LAYER_PROVIDERS = {
    "fire": fire_at,
    "flood": flood_at,
    "roads": roads_at,
    "storm": storm_at,
    "power": power_at,
    "rain": aws_at,
    "wind": aws_at,
}


def frame_source(when):
    """A `source(key)` callable for `unified.map_figure`, bound to a moment."""
    def source(key):
        provider = LAYER_PROVIDERS.get(key)
        return provider(when) if provider else None
    return source


# --------------------------------------------------------------------------- #
# Historical KPIs
# --------------------------------------------------------------------------- #
def _latest_timeseries(table, when, columns, max_age_hours=6):
    """The most recent per-cycle KPI row at or before `when`.

    These timeseries tables are the honest historical KPI record: they have
    been written every cycle since each module was built, so KPIs replay
    correctly for events long predating the state journal. Rows older than
    `max_age_hours` are ignored — the collector wasn't running, and stale counts
    would misrepresent the moment.
    """
    stamp = _stamp(when)
    cutoff = _stamp(_to_dt(stamp) - timedelta(hours=max_age_hours))
    df = database.read_df(
        "SELECT %s FROM %s WHERE timestamp <= ? AND timestamp >= ? "
        "ORDER BY timestamp DESC LIMIT 1" % (", ".join(columns), table),
        [stamp, cutoff])
    return None if df.empty else df.iloc[0]


def flood_counts_at(when):
    """Gauges at/above each level at a past moment, from real observations."""
    df = flood_at(when)
    empty = {"minor": 0, "moderate": 0, "major": 0}
    if df.empty:
        return empty
    out = dict(empty)
    for priority in df["priority"]:
        if priority <= 3:
            out["minor"] += 1
        if priority <= 2:
            out["moderate"] += 1
        if priority == 1:
            out["major"] += 1
    return out


def historical_kpis(when):
    """[{label, value}] as the situation stood at `when`.

    Every figure is drawn from a record written at the time. "—" means the
    collector wasn't running then, which is a different and more honest answer
    than 0.
    """
    def count(value):
        return "—" if value is None or pd.isna(value) else "{:,}".format(int(value))

    power = _latest_timeseries("power_timeseries", when, ["customers_off"])
    fire = _latest_timeseries(
        "fire_timeseries", when,
        ["active_fires", "emergency_warnings", "watch_act"])
    roads = _latest_timeseries("road_timeseries", when, ["closures"])
    storm = _latest_timeseries("storm_timeseries", when, ["strong_cells"])
    flood = flood_counts_at(when)

    return [
        {"label": "Customers Off",
         "value": count(power["customers_off"] if power is not None else None)},
        {"label": "Active Fires",
         "value": count(fire["active_fires"] if fire is not None else None)},
        {"label": "Emergency Warnings",
         "value": count(fire["emergency_warnings"] if fire is not None else None)},
        {"label": "Watch & Act",
         "value": count(fire["watch_act"] if fire is not None else None)},
        {"label": "Gauges ≥ Minor", "value": str(flood["minor"])},
        {"label": "Road Closures",
         "value": count(roads["closures"] if roads is not None else None)},
        {"label": "Strong Storm Cells",
         "value": count(storm["strong_cells"] if storm is not None else None)},
    ]


# --------------------------------------------------------------------------- #
# Intelligence timeline — the narrative spine
# --------------------------------------------------------------------------- #
def timeline(start, end, limit=200, min_severity=intel_feed.NOTABLE):
    """Intelligence Feed entries inside the event, oldest first.

    Context lines are off: on a timeline the entry is a marker to seek to, and
    resolving proximity for hundreds of entries would be resolved against
    TODAY's layers anyway, which would be wrong for a historical moment.
    """
    try:
        entries = intel_feed.entries(since=start, until=end, limit=limit,
                                     min_severity=min_severity,
                                     with_context=False)
    except Exception:
        log.exception("Replay: timeline unavailable")
        return []
    return sorted(entries, key=lambda e: e["ts"] or start)


# --------------------------------------------------------------------------- #
# Honesty about what can actually be reconstructed
# --------------------------------------------------------------------------- #
def coverage(event_start):
    """What this event can and cannot be replayed from.

    Replay must never present a partial reconstruction as a full-fidelity one,
    so this returns the facts the page states verbatim rather than a vague
    disclaimer.
    """
    starts = history.history_starts()
    journal_from = max(starts.values()) if starts else None
    full = bool(journal_from and event_start and event_start >= journal_from)
    return {
        "journal_from": journal_from,
        "per_source": starts,
        "full_fidelity": full,
        # These layers kept true history long before the journal existed, so
        # they replay correctly for older events too.
        "always_available": ["Flood gauges", "Storm cells", "AWS observations",
                             "Intelligence Feed", "KPI history"],
        "journal_only": ["Fire incidents", "Road disruptions",
                         "Power outages (per location)", "Weather warnings"],
    }


def coverage_note(event_start):
    """One paragraph stating the limits, in plain words."""
    info = coverage(event_start)
    journal_from = info["journal_from"]
    if journal_from is None:
        return ("No incident/road/power state history has been recorded yet. "
                "This event can be replayed from the datasets that were always "
                "retained — flood gauges, storm cells, AWS observations, KPI "
                "history and the Intelligence Feed — but incident, road and "
                "power layers will be empty.")
    when = journal_from.strftime("%d %B %Y")
    if info["full_fidelity"]:
        return ("Full-fidelity incident/road/power replay available from %s. "
                "This event starts after that point, so all layers replay from "
                "recorded state." % when)
    return ("Full-fidelity incident/road/power replay available from %s. This "
            "event starts before that, so its incident, road and power layers "
            "are partial: only flood gauges, storm cells, AWS observations, KPI "
            "history and the Intelligence Feed were being retained at the time. "
            "Nothing is back-dated or inferred." % when)

"""Operational Summary auto-fill: suggested field values from what Passive
Monitor already collects, plus two on-demand external sources.

UI-free, like ``opsum``. ``suggest(d)`` returns, per field, a ``Suggestion``
(value + where it came from + the moment it describes) or a reason it could
not be filled; ``apply()`` merges suggestions into a draft without touching
anything already typed unless asked to.

**The deck's clocks are honoured, not approximated.** Warnings and going fires
are reported "as at 0830" (``opsum.snapshot_time``) and incident totals for
the "24 hours to 0600" (``opsum.stats_cutoff``). For a summary built after
those times the 0830 picture is RECONSTRUCTED from the state journal
(``history.state_at``) — the same machinery as Event Replay — rather than
reading whatever is live when someone clicks. Before 0830 the live state is
used and the suggestion says so. A moment the journal does not cover is
refused, never substituted.

**Every suggestion names its source**, because some of these differ from the
SCC's own figures by construction: VicEmergency is the PUBLIC feed (structure
fires in particular are often not published), and RFAs are not in any feed
this app reads, so those cells are left for the operator.

Each provider is isolated: a module that throws costs only its own fields.
"""
import logging
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta

from app import database, history, opsum
from app.config import load_config

log = logging.getLogger(__name__)

_TS = "%Y-%m-%d %H:%M:%S"


@dataclass
class Suggestion:
    key: str
    value: object          # same shape as the field (tables: None = no suggestion)
    source: str
    as_at: str = ""        # human label of the moment the value describes
    note: str = ""

    def describe(self):
        bits = [self.source]
        if self.as_at:
            bits.append(f"as at {self.as_at}")
        text = " · ".join(bits)
        return f"{text}. {self.note}" if self.note else text


@dataclass
class Context:
    d: object
    now: datetime
    cfg: dict
    snap: datetime             # the 0830 moment actually used
    snap_live: bool            # 0830 not reached yet: live state used instead
    cutoff: datetime           # end of the 24 h stats window actually used
    cutoff_partial: bool
    window_start: datetime
    misses: dict = field(default_factory=dict)


def _at(d, hhmm):
    h, m = (int(x) for x in str(hhmm).split(":")[:2])
    return datetime.combine(d, time(h, m))


def label(dt):
    return f"{dt:%H:%M} {dt:%a} {dt.day} {dt:%b}"


def context(d, now=None, cfg=None):
    d = opsum.parse_date(d)
    now = now or datetime.now()
    cfg = cfg or load_config()
    o = cfg["opsum"]
    snap = _at(d, o["snapshot_time"])
    cutoff = _at(d, o["stats_cutoff"])
    return Context(d=d, now=now, cfg=cfg,
                   snap=min(snap, now), snap_live=snap > now,
                   cutoff=min(cutoff, now), cutoff_partial=cutoff > now,
                   window_start=cutoff - timedelta(hours=24))


def _s(v):
    """NULL/NaN -> '' (pandas hands NULL TEXT back as NaN, which is truthy)."""
    if v is None or (isinstance(v, float) and v != v):
        return ""
    return str(v).strip()


# --------------------------------------------------------------------------- #
# VicEmergency: warnings + going fires as at 0830, fire totals to 0600
# --------------------------------------------------------------------------- #
WARNING_COLUMNS = {             # category1 (lower) -> column in the deck table
    "emergency warning": 0, "evacuate": 0, "evacuation": 0,
    "watch and act": 1,
    "advice": 2,
    "community information": 3, "community info": 3, "community update": 3,
}


def warning_counts(rows):
    counts = [0, 0, 0, 0]
    for r in rows:
        if _s(r.get("feed_type")).lower() != "warning":
            continue
        col = WARNING_COLUMNS.get(_s(r.get("category1")).lower())
        if col is not None:
            counts[col] += 1
    return counts


def going_fires(rows):
    return sum(1 for r in rows
               if _s(r.get("feed_type")).lower() not in ("warning", "burn-area")
               and _s(r.get("category1")).lower() == "fire"
               and _s(r.get("status")).lower() == "going")


def fire_kind(category2):
    """'grass' (grass/bush/scrub/forest), 'structure', or None."""
    c = _s(category2).lower()
    if "non-structure" in c or "non structure" in c:
        return None
    if "structure" in c or "building" in c or "house" in c:
        return "structure"
    if any(w in c for w in ("grass", "bush", "scrub", "forest", "crop", "wildfire")):
        return "grass"
    return None


def _fire_rows_at(ctx):
    """(rows, how) — the VicEmergency picture at the snapshot moment."""
    if ctx.snap_live:
        df = database.read_df(
            "SELECT feed_type, category1, status, warning_level FROM fire_incidents "
            "WHERE resolved = 0 AND feed_type != 'burn-area'")
        return df.to_dict("records"), "live"
    start = history.history_start(history.FIRE)
    if start is None or ctx.snap < start:
        raise LookupError(
            "the state journal does not reach back to "
            f"{label(ctx.snap)}" + (f" (it starts {label(start)})" if start else ""))
    df = history.state_at(history.FIRE, ctx.snap)
    return df.to_dict("records"), "journal"


def _fire_collector_ok(ctx):
    df = database.read_df("SELECT MAX(timestamp) AS ts FROM fire_timeseries")
    ts = None if df.empty else _s(df.iloc[0]["ts"])
    if not ts:
        raise LookupError("the VicEmergency collector has no data yet")
    return ts


def p_vicemergency(ctx):
    last = _fire_collector_ok(ctx)
    rows, how = _fire_rows_at(ctx)
    note = ("08:30 not reached yet — current state used. " if ctx.snap_live else "")
    if how == "live" and last < (ctx.now - timedelta(minutes=30)).strftime(_TS):
        note += f"VicEmergency collector last ran {last}. "
    src = "VicEmergency public feed" + (" (state journal)" if how == "journal" else "")
    return [
        Suggestion("warnings", [warning_counts(rows)], src, label(ctx.snap), note.strip()),
        Suggestion("ops_incidents", [[str(going_fires(rows)), None, None, None, None]],
                   src, label(ctx.snap), note.strip()),
    ]


def p_fire_totals(ctx):
    _fire_collector_ok(ctx)
    df = database.read_df(
        "SELECT category2 FROM fire_incidents "
        "WHERE feed_type NOT IN ('warning', 'burn-area') AND LOWER(category1) = 'fire' "
        "AND COALESCE(created, first_seen) > ? AND COALESCE(created, first_seen) <= ?",
        [ctx.window_start.strftime(_TS), ctx.cutoff.strftime(_TS)])
    kinds = [fire_kind(c) for c in df["category2"]] if not df.empty else []
    note = ("Public feed — structure fires in particular are often not published; "
            "check against the operational figures.")
    if ctx.cutoff_partial:
        note = f"Window not complete (06:00 not reached). {note}"
    return [Suggestion(
        "ops_incidents",
        [[None, str(kinds.count("grass")), str(kinds.count("structure")), None, None]],
        "VicEmergency public feed",
        f"{label(ctx.window_start)} – {label(ctx.cutoff)}", note)]


# --------------------------------------------------------------------------- #
# BoM warnings, roads, power
# --------------------------------------------------------------------------- #
def p_bom_warnings(ctx):
    from app.modules.weather import data as wdata
    df = wdata.active_warnings()
    if df.empty:
        return [Suggestion("bom_warnings", "Nil", "BoM warnings (Passive Monitor)",
                           label(ctx.now))]
    lines = []
    for type_label, group in df.groupby("type_label", sort=False):
        if lines:
            lines.append("")
        lines.append(f"# {type_label}")
        for _, r in group.iterrows():
            lines.append(_s(r.get("short_title")) or _s(r.get("title")))
    return [Suggestion("bom_warnings", "\n".join(lines), "BoM warnings (Passive Monitor)",
                       label(ctx.now))]


def p_roads(ctx):
    from app.modules.roads import data as rdata
    counts = rdata.latest_counts()
    closures = rdata.active_disruptions(closures_only=True)
    df = database.read_df("SELECT MAX(timestamp) AS ts FROM road_timeseries")
    if df.empty or not _s(df.iloc[0]["ts"]):
        raise LookupError("the VicRoads collector has no data (is the API key set?)")
    if not counts["total"]:
        value = "Nil significant unplanned disruptions."
    else:
        lines = [f"**{counts['closures']} full road closure"
                 f"{'s' if counts['closures'] != 1 else ''}** and {counts['other']} other "
                 "unplanned disruption" + ("s" if counts["other"] != 1 else "") + " statewide."]
        for _, r in closures.head(6).iterrows():
            where = ", ".join(x for x in (_s(r.get("road_name")), _s(r.get("lga"))) if x)
            kind = _s(r.get("disruption_type")).split(", ")[0]
            lines.append("  " + " — ".join(x for x in (where, kind) if x))
        if len(closures) > 6:
            lines.append(f"  … and {len(closures) - 6} more closures.")
        value = "\n".join(lines)
    return [Suggestion("road_network", value, "VicRoads unplanned disruptions",
                       label(ctx.now), "Replaces any standing Big Build note — "
                       "re-add it if still wanted.")]


def p_power(ctx):
    from app.modules.power import data as pdata
    totals = pdata.latest_totals()
    if not totals:
        raise LookupError("the power collector has no data")
    ts = _s(totals.get("timestamp"))
    if ts and ts < (ctx.now - timedelta(hours=3)).strftime(_TS):
        raise LookupError(f"power data is stale (last update {ts})")
    threshold = int(ctx.cfg["opsum"]["power_significant_customers"])
    df = pdata.active_outages(min_customers=threshold)
    total = int(float(totals.get("customers_off") or 0))
    if df.empty:
        value = f"Nil significant ({total:,} customers off supply statewide)."
    else:
        df = df.sort_values("customers_off", ascending=False)
        lines = [f"{total:,} customers off supply statewide."]
        for _, r in df.head(5).iterrows():
            lines.append(f"  {_s(r.get('location'))} — "
                         f"{int(float(r.get('customers_off') or 0)):,} customers")
        value = "\n".join(lines)
    return [Suggestion("power_disruptions", value, "EM-COP power outages",
                       label(datetime.strptime(ts, _TS)) if ts else "",
                       f"Significant = {threshold:,}+ customers at one location.")]


# --------------------------------------------------------------------------- #
# Flood snapshot slide
# --------------------------------------------------------------------------- #
def flood_rows(ctx):
    """Gauges at/above minor from their latest reading (within 24 h),
    most severe first, capped at ``opsum.flood_max_gauges``."""
    from app.modules.flood import data as fdata
    from app.modules.flood import trend
    levels = fdata.load_flood_levels()
    latest = database.read_df(
        "SELECT station_name, height_m, MAX(timestamp) AS ts "
        "FROM flood_observations GROUP BY station_name")
    if latest.empty:
        raise LookupError("no flood gauge readings stored")
    fresh = (ctx.now - timedelta(hours=24)).strftime(_TS)
    flooding = []
    for _, r in latest.iterrows():
        if _s(r["ts"]) < fresh:
            continue
        try:
            height = float(r["height_m"])
        except (TypeError, ValueError):
            continue
        key = _s(r["station_name"]).lower()
        prio, cls, _ = fdata.classify_station(height, levels.get(key))
        if prio <= 3:
            flooding.append((prio, -height, _s(r["station_name"]), height, cls))
    flooding.sort()
    cfg = ctx.cfg
    min_rate = float(cfg["flood"]["trend_min_rate_m_hr"])
    out = []
    for _, _, name, height, cls in flooding[:int(cfg["opsum"]["flood_max_gauges"])]:
        a = None
        try:
            a = trend.analyse(name.lower(), levels=levels.get(name.lower()), cfg=cfg,
                              now=ctx.now)
        except Exception:
            log.exception("Operational Summary: trend for %s failed", name)
        rate = (a or {}).get("rate_m_hr")
        if rate is None:
            tr = ""
        elif rate > min_rate:
            tr = "Rising"
        elif rate < -min_rate:
            tr = "Falling"
        else:
            tr = "Steady"
        outlook = ""
        if a and a.get("eta_early") and a.get("eta_late") and a.get("target_name"):
            outlook = (f"{str(a['target_name']).capitalize()} potentially reached "
                       f"{a['eta_early']:%H:%M}–{a['eta_late']:%H:%M} "
                       f"(trend projection — not an official forecast).")
        # The deck names the place ("Barham"), not the BoM station
        # ("Murray River at Barham"); gauge_town is the weather module's parser.
        from app.modules.weather import data as wdata
        short = wdata.gauge_town(name) or name
        out.append({"name": short, "height": f"{height:.2f} m",
                    "status": str(cls).split()[0].upper(), "trend": tr,
                    "outlook": outlook})
    return out, len(flooding)


def p_flood(ctx):
    rows, total = flood_rows(ctx)
    if not rows:
        raise LookupError("no gauge is at or above minor flood level")
    grid = [[None] * 5 for _ in range(5)]
    for j, r in enumerate(rows):
        for i, k in enumerate(("name", "height", "status", "trend", "outlook")):
            grid[i][j] = r[k]
    for j in range(len(rows), 5):            # clear leftover columns
        for i in range(5):
            grid[i][j] = ""
    note = f"{total} gauge(s) at or above minor; the {len(rows)} most severe shown."
    src = "BoM flood gauges (Passive Monitor)"
    return [
        Suggestion("flood_gauges", grid, src, label(ctx.now), note),
        Suggestion("flood_table_title", "Gauges at or above minor flood level", src),
        Suggestion("flood_overview_date", f"{ctx.d.day} {ctx.d:%B %Y}".upper(), src),
    ]


# --------------------------------------------------------------------------- #
# External (on demand)
# --------------------------------------------------------------------------- #
def p_forecast(ctx):
    from app import opsum_sources
    f = opsum_sources.state_forecast(ctx.cfg)
    src = "BoM state forecast"
    out = []
    if f.get("forecast"):
        out.append(Suggestion("wx_forecast", f["forecast"], src, label(ctx.now),
                              f"From “{f.get('forecast_heading', '')}”."))
    if f.get("situation"):
        out.append(Suggestion("wx_situation", f["situation"], src, label(ctx.now)))
    return out


def p_quakes(ctx):
    from app import opsum_sources
    quakes = opsum_sources.earthquakes(ctx.now, ctx.cfg)
    o = ctx.cfg["opsum"]
    if not quakes:
        value = "Nil"
    else:
        value = "\n".join(
            f"M{q['mag']:.1f} {q['desc']} — {q['time']:%H:%M %a} {q['time'].day} {q['time']:%b}"
            + (f", depth {q['depth']:.0f} km" if q.get("depth") is not None else "")
            for q in quakes)
    return [Suggestion("earthquakes", value, "Geoscience Australia", label(ctx.now),
                       f"Victoria, last {o['earthquake_hours']} h, "
                       f"magnitude {o['earthquake_min_magnitude']}+.")]


INTERNAL = [
    ("VicEmergency warnings / going fires", p_vicemergency, ("warnings", "ops_incidents")),
    ("VicEmergency fire totals", p_fire_totals, ("ops_incidents",)),
    ("BoM warnings", p_bom_warnings, ("bom_warnings",)),
    ("Road network", p_roads, ("road_network",)),
    ("Power disruptions", p_power, ("power_disruptions",)),
    ("Flood snapshot", p_flood, ("flood_gauges", "flood_table_title", "flood_overview_date")),
]
EXTERNAL = [
    ("BoM state forecast", p_forecast, ("wx_forecast", "wx_situation")),
    ("GA earthquakes", p_quakes, ("earthquakes",)),
]
AUTO_KEYS = sorted({k for _, _, keys in INTERNAL + EXTERNAL for k in keys})


def _merge(a, b):
    """Two suggestions for one table: cell-wise, first non-None wins."""
    grid = [[x if x is not None else y for x, y in zip(ra, rb)]
            for ra, rb in zip(a.value, b.value)]
    src = a.source if a.source == b.source else f"{a.source}; {b.source}"
    as_at = a.as_at if a.as_at == b.as_at else f"{a.as_at} / {b.as_at}"
    note = " ".join(n for n in dict.fromkeys((a.note, b.note)) if n)
    return Suggestion(a.key, grid, src, as_at, note)


def suggest(d, now=None, cfg=None, external=True):
    """``(suggestions, misses)``: ``{key: Suggestion}`` and ``{provider: reason}``."""
    ctx = context(d, now, cfg)
    out, misses = {}, {}
    for name, fn, _ in INTERNAL + (EXTERNAL if external else []):
        try:
            for s in fn(ctx) or []:
                if s.key in out and opsum.FIELDS[s.key].kind == "table":
                    out[s.key] = _merge(out[s.key], s)
                else:
                    out[s.key] = s
        except LookupError as e:
            misses[name] = str(e)
        except Exception as e:           # a broken module costs only its fields
            log.exception("Operational Summary auto-fill: %s failed", name)
            misses[name] = f"{e.__class__.__name__}: {e}"
    return out, misses


def unapplied(data, suggestions):
    """Keys whose suggestion still differs from the draft — i.e. what the
    operator has typed over. Equal values are not "kept", they agree."""
    out = []
    for key, s in suggestions.items():
        cur = data["fields"].get(key)
        if opsum.FIELDS.get(key) and opsum.FIELDS[key].kind == "table":
            if any(v is not None and v != cur[i][j]
                   for i, row in enumerate(s.value) for j, v in enumerate(row)
                   if i < len(cur) and j < len(cur[i])):
                out.append(key)
        elif cur != s.value:
            out.append(key)
    return out


def apply(data, suggestions, overwrite=False):
    """Merge ``suggestions`` into draft ``data`` (in place). Empty fields/cells
    only unless ``overwrite``. Returns the keys actually changed."""
    changed = []
    fields = data["fields"]
    for key, s in suggestions.items():
        f = opsum.FIELDS.get(key)
        if f is None:
            continue
        if f.kind == "table":
            grid = fields[key]
            hit = False
            for i, row in enumerate(s.value):
                for j, v in enumerate(row):
                    if v is None or i >= len(grid) or j >= len(grid[i]):
                        continue
                    if (overwrite or not str(grid[i][j]).strip()) and grid[i][j] != v:
                        grid[i][j] = v
                        hit = True
            if hit:
                changed.append(key)
        elif overwrite or not str(fields.get(key) or "").strip():
            if fields.get(key) != s.value:
                fields[key] = s.value
                changed.append(key)
    return changed

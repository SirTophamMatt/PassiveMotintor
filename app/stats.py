# Passive Monitor — Copyright (c) 2026 SirTophamMatt. All rights reserved.
"""Incident statistics for the /stats dashboard (UI-free).

Counts VicEmergency incidents that STARTED inside a period — an operational day
(24 h to 06:00, the SCC's stats clock), a financial year (1 Jul – 30 Jun) or an
event tag — plus what is active right now.

- **RFA** = an SES incident (`fire.data.agency_of` == "ses").
- **Structure fires** = Fire incidents whose category2 is a structure fire,
  split CFA / FRV. **Grass & scrub/bush fires** = grass/bush/scrub/forest fires,
  split CFA / FFMVIC (DEECA). The kind comes from `opsum_auto.fire_kind`, the
  same rule the Operational Summary uses, so the two can never disagree.
- An incident's start is `COALESCE(created, first_seen)` (the opsum rule).
- Warnings and historical burn areas are never incidents.

Everything here is the PUBLIC feed: structure fires in particular are often not
published, so every surface carries that caveat (`CAVEAT`).
"""
import json
import re
import threading
import time as _time
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

import pandas as pd

from app import database
from app.modules.fire import data as fire_data
from app.opsum_auto import fire_kind

TS = "%Y-%m-%d %H:%M:%S"
DAY_START = time(6, 0)          # the operational day runs 06:00 -> 06:00
CACHE_SECONDS = 60

PERIOD_DAY = "day"              # 24 h to 06:00 on a date
PERIOD_TODAY = "today"          # since 06:00 (in progress)
PERIOD_FY = "fy"
PERIOD_EVENT = "event"

FFM_LABEL = "FFMVIC / DEECA"
CAVEAT = ("Source: VicEmergency public feed. Not every incident is published — "
          "structure fires in particular are often left off — so check these "
          "against the agencies' operational figures.")

# Headline metrics, in display order: key -> label.
METRICS = [
    ("rfa", "SES RFAs"),
    ("structure", "Structure fires"),
    ("grass", "Grass & scrub/bush fires"),
    ("incidents", "All incidents"),
]


# --------------------------------------------------------------------------- #
# Periods
# --------------------------------------------------------------------------- #
@dataclass
class Period:
    kind: str
    start: datetime
    end: datetime                # may be in the future (in-progress period)
    label: str
    previous: tuple = None       # (start, end) of the comparison period, or None
    previous_label: str = ""

    def effective_end(self, now):
        return min(self.end, now)

    def partial(self, now):
        return self.end > now


def last_completed_day(now=None):
    """The date whose 06:00 closes the most recent complete operational day."""
    now = now or datetime.now()
    return now.date() if now.time() >= DAY_START else now.date() - timedelta(days=1)


def fy_start_year(d):
    """The calendar year a financial year starts in (FY 2026-27 -> 2026)."""
    return d.year if d.month >= 7 else d.year - 1


def fy_label(year):
    return f"FY {year}-{str(year + 1)[-2:]}"


def fy_options(now=None, first_year=None):
    """[(year, label)], newest first, from the first year with data to now."""
    current = fy_start_year((now or datetime.now()).date())
    first = min(first_year if first_year is not None else current, current)
    return [(y, fy_label(y)) for y in range(current, first - 1, -1)]


def _fmt(dt):
    return f"{dt:%H:%M %a} {dt.day} {dt:%b %Y}"


def day_period(d):
    """24 h to 06:00 on `d` — the window the SCC summary for `d` reports."""
    end = datetime.combine(d, DAY_START)
    start = end - timedelta(days=1)
    return Period(PERIOD_DAY, start, end, f"24 h to {_fmt(end)}",
                  (start - timedelta(days=1), start), "previous 24 h")


def today_period(now=None):
    now = now or datetime.now()
    start = datetime.combine(last_completed_day(now), DAY_START)
    end = start + timedelta(days=1)
    return Period(PERIOD_TODAY, start, end, f"Since {_fmt(start)} (in progress)",
                  (start - timedelta(days=1), start), "previous 24 h")


def fy_period(year, now=None):
    now = now or datetime.now()
    start = datetime(year, 7, 1)
    end = datetime(year + 1, 7, 1)
    # Compare like with like: the same span of the year before, so a part-way
    # financial year is never set against a whole one.
    span = min(end, now) - start
    prev_start = datetime(year - 1, 7, 1)
    return Period(PERIOD_FY, start, end, fy_label(year),
                  (prev_start, prev_start + span),
                  f"same point in {fy_label(year - 1)}")


def event_period(tag, now=None):
    now = now or datetime.now()
    start = datetime.strptime(tag["start_ts"], TS)
    end = datetime.strptime(tag["end_ts"], TS) if tag.get("end_ts") else now
    ongoing = "" if tag.get("end_ts") else " (ongoing)"
    return Period(PERIOD_EVENT, start, end,
                  f"{tag['name']}: {_fmt(start)} – "
                  f"{_fmt(end) if tag.get('end_ts') else 'now'}{ongoing}")


# --------------------------------------------------------------------------- #
# Classification
# --------------------------------------------------------------------------- #
def _s(v):
    if v is None or (isinstance(v, float) and v != v):
        return ""
    return str(v).strip()


def incident_type(r):
    """Category, with the sub-category when it adds something."""
    cat1 = _s(r.get("category1")) or "Unspecified"
    cat2 = _s(r.get("category2"))
    if cat2 and cat2.lower() != cat1.lower():
        return f"{cat1} – {cat2}"
    return cat1


def fire_kind_of(category1, category2):
    """(is_fire, kind) for an incident. Tolerant of where the feed puts the
    fire type: category1 is usually "Fire" with the type in category2, but an
    agency may publish the type as the category itself ("Structure Fire"),
    so both are read. An alarm is never a fire."""
    c1 = _s(category1).lower()
    if "alarm" in c1:
        return False, None
    kind = fire_kind(category2) if c1 == "fire" else None
    if kind is None and "fire" in c1:
        kind = fire_kind(c1) or fire_kind(f"{c1} {_s(category2).lower()}")
    return (c1 == "fire" or "fire" in c1), kind


ESTA = "esta"                   # a Triple Zero dispatch event, agency not identified
_ESTA_ID_RE = re.compile(r"^ESTA:(\d{9,10})$")
_FRV_STATION_RE = re.compile(r"^FS\d{1,3}$")
_CFA_BRIGADE_RE = re.compile(r"^[A-Z]{4}$")


def agency_for(r):
    """The agency a row is counted under. An `agency_hint` (from the pager,
    see `attribute_esta`) wins; then the feed's sourceOrg. FRV publishes no
    feed of its own: metro fire jobs arrive as raw ESTA dispatch events
    (sourceOrg VIC/ESTA) with no agency on them, so those are "esta" until
    the pager says whose job it was."""
    hint = _s(r.get("agency_hint"))
    if hint:
        return hint
    agency = fire_data.agency_of(r.get("source_org"))
    if agency == fire_data.AGENCY_OTHER and "ESTA" in _s(r.get("source_org")).upper():
        return ESTA
    return agency


def classify(r):
    """Every flag a row contributes to. Pure, so the rules are testable."""
    agency = agency_for(r)
    fire, kind = fire_kind_of(r.get("category1"), r.get("category2"))
    return {
        "agency": agency,
        "rfa": agency == "ses",
        "fire": fire,
        "kind": kind,
        "structure_cfa": kind == "structure" and agency == "cfa",
        "structure_frv": kind == "structure" and agency == "frv",
        "structure_esta": kind == "structure" and agency == ESTA,
        "grass_cfa": kind == "grass" and agency == "cfa",
        "grass_ffm": kind == "grass" and agency == "ffm",
        "grass_esta": kind == "grass" and agency == ESTA,
    }


def esta_f_number(source_id):
    """'ESTA:261014231' -> 'F261014231' (the pager's F-number), else None."""
    m = _ESTA_ID_RE.match(_s(source_id))
    return f"F{m.group(1)}" if m else None


def pager_agency(messages):
    """'frv' / 'cfa' / None from one job's pager messages [(brigade, units_json)].
    The bracketed brigade/station names whose area the job is in ([FS91] = FRV
    station 91, [MTEL] = CFA Mount Eliza); without one, the appliances paged
    decide, and a tie is left unattributed rather than guessed."""
    for brigade, _ in messages:
        b = _s(brigade).upper()
        if _FRV_STATION_RE.match(b):
            return "frv"
        if _CFA_BRIGADE_RE.match(b):
            return "cfa"
    services = {"FRV": 0, "CFA": 0}
    for _, units in messages:
        try:
            for u in json.loads(units or "[]"):
                if u.get("service") in services:
                    services[u["service"]] += 1
        except (TypeError, ValueError):
            continue
    if services["FRV"] != services["CFA"]:
        return "frv" if services["FRV"] > services["CFA"] else "cfa"
    return None


# A CFA pager cycle must have run within this long after an ESTA job started
# for "no page seen" to mean "not paged" rather than "we weren't looking".
PAGER_COVER_MINUTES = 30
HOW_PAGER = "pager"
HOW_NOT_PAGED = "not paged"


def _pager_covered(started, heartbeats):
    """For each start time: did a pager cycle run in (start, start + cover]?"""
    import numpy as np
    if not len(heartbeats):
        return [False] * len(started)
    hb = np.sort(pd.to_datetime(pd.Series(heartbeats), errors="coerce").dropna().values)
    st = pd.to_datetime(pd.Series(list(started)), errors="coerce")
    out = []
    for t in st:
        if pd.isna(t):
            out.append(False)
            continue
        i = np.searchsorted(hb, np.datetime64(t), side="right")
        out.append(bool(i < len(hb) and hb[i] <= np.datetime64(
            t + timedelta(minutes=PAGER_COVER_MINUTES))))
    return out


def attribute_esta(df):
    """Add `agency_hint` / `agency_how` to ESTA rows (Triple Zero dispatch
    events, which carry no agency).

    The CFA pager (Mazzanet) carries every CFA job and the FRV stations that
    were CFA stations (Dandenong, Boronia, …), but NOT the ex-MFB metro
    stations. So an ESTA job:
    - on the pager -> the agency the page names (`pager_agency`);
    - NOT on the pager, while the pager collector was running -> FRV
      (an ex-MFB metro job);
    - not on the pager and the pager wasn't being collected then -> left
      unidentified, because a CFA job would look the same."""
    if df.empty or "source_id" not in df:
        return df
    df = df.copy()
    df["f_number"] = df["source_id"].map(esta_f_number)
    df["agency_hint"] = None
    df["agency_how"] = None
    esta = df["f_number"].notna()
    if not esta.any():
        return df
    fnums = sorted(set(df.loc[esta, "f_number"]))
    found = {}
    try:
        for i in range(0, len(fnums), 500):
            chunk = fnums[i:i + 500]
            rows = database.read_df(
                "SELECT f_number, brigade, units_json FROM pager_messages "
                f"WHERE f_number IN ({','.join('?' * len(chunk))}) ORDER BY sent_at",
                chunk)
            for f, g in rows.groupby("f_number"):
                found[f] = pager_agency(list(zip(g["brigade"], g["units_json"])))
        starts = pd.to_datetime(df.loc[esta, "started"], errors="coerce").dropna()
        heartbeats = []
        if not starts.empty:
            heartbeats = database.read_df(
                "SELECT timestamp FROM pager_heartbeat WHERE timestamp > ? AND timestamp <= ?",
                [starts.min().strftime(TS),
                 (starts.max() + timedelta(minutes=PAGER_COVER_MINUTES)).strftime(TS)],
            )["timestamp"].tolist()
    except Exception:                   # no pager tables yet
        return df
    paged = df["f_number"].isin(found.keys())
    df.loc[paged, "agency_hint"] = df.loc[paged, "f_number"].map(found)
    df.loc[paged, "agency_how"] = HOW_PAGER
    unpaged = esta & ~paged
    if unpaged.any():
        covered = _pager_covered(df.loc[unpaged, "started"], heartbeats)
        idx = df.index[unpaged][covered]
        df.loc[idx, "agency_hint"] = "frv"
        df.loc[idx, "agency_how"] = HOW_NOT_PAGED
    return df


def counted_as(r):
    """The headline figure a classified row lands in (for the check table)."""
    if r.get("rfa"):
        return "SES RFA"
    via = {HOW_PAGER: " — via CFA pager",
           HOW_NOT_PAGED: " — ESTA job not on the CFA pager (ex-MFB area)"
           }.get(_s(r.get("agency_how")), "")
    for key, label in (("structure_cfa", "Structure fire (CFA)"),
                       ("structure_frv", "Structure fire (FRV)"),
                       ("structure_esta", "Structure fire (ESTA — agency not identified)"),
                       ("grass_cfa", "Grass & scrub/bush (CFA)"),
                       ("grass_ffm", f"Grass & scrub/bush ({FFM_LABEL})"),
                       ("grass_esta", "Grass & scrub/bush (ESTA — agency not identified)")):
        if r.get(key):
            return label + via
    if r.get("fire"):
        kind = {"structure": "structure", "grass": "grass/bush"}.get(r.get("kind"))
        agency = fire_data.AGENCY_LABELS.get(r.get("agency"), "other agency")
        return (f"Fire — {kind} fire but agency is {agency}" if kind
                else "Fire — other type (not structure or grass/bush)")
    return "Other incident"


def classification_check(df, limit=40):
    """[(source_org, category1, category2, count, counted_as)] — what the feed
    actually called each incident and where it was counted, so a feed wording
    the rules don't recognise is visible on the page instead of reading as 0."""
    if df.empty:
        return []
    keys = pd.DataFrame({
        "org": df["source_org"].map(_s), "c1": df["category1"].map(_s),
        "c2": df["category2"].map(_s), "as_": df.apply(counted_as, axis=1)})
    g = (keys.groupby(["org", "c1", "c2", "as_"]).size()
         .reset_index(name="n").sort_values(["n", "org"], ascending=[False, True]))
    return [(r.org or "—", r.c1 or "—", r.c2 or "—", int(r.n), r.as_)
            for r in g.head(limit).itertuples()]


def _frame(df):
    """Add the classification columns to a raw incident frame."""
    if df.empty:
        cols = ["agency", "rfa", "fire", "kind", "structure_cfa", "structure_frv",
                "structure_esta", "grass_cfa", "grass_ffm", "grass_esta",
                "structure", "grass", "agency_hint", "agency_how"]
        return df.assign(**{c: pd.Series(dtype=object) for c in cols})
    df = attribute_esta(df)
    flags = pd.DataFrame([classify(r) for r in df.to_dict("records")], index=df.index)
    out = pd.concat([df, flags], axis=1)
    out["structure"] = out["structure_cfa"] | out["structure_frv"] | out["structure_esta"]
    out["grass"] = out["grass_cfa"] | out["grass_ffm"] | out["grass_esta"]
    return out


_INCIDENT_SQL = ("feed_type NOT IN ('warning', 'burn-area')")
_STARTED = "COALESCE(created, first_seen)"


def incidents_started(start, end):
    """Incidents that started in (start, end], classified."""
    df = database.read_df(
        f"SELECT source_id, category1, category2, source_org, location, status, "
        f"resolved, {_STARTED} AS started FROM fire_incidents "
        f"WHERE {_INCIDENT_SQL} AND {_STARTED} > ? AND {_STARTED} <= ?",
        [start.strftime(TS), end.strftime(TS)])
    return _frame(df)


def active_incidents():
    df = database.read_df(
        "SELECT source_id, category1, category2, source_org, location, status, "
        f"resolved, {_STARTED} AS started FROM fire_incidents "
        f"WHERE resolved = 0 AND {_INCIDENT_SQL}")
    return _frame(df)


def totals(df):
    """Headline counts for a classified frame."""
    def n(col):
        return int(df[col].sum()) if not df.empty else 0
    t = {
        "incidents": len(df),
        "rfa": n("rfa"),
        "fires": n("fire"),
        "structure_cfa": n("structure_cfa"),
        "structure_frv": n("structure_frv"),
        "grass_cfa": n("grass_cfa"),
        "grass_ffm": n("grass_ffm"),
        "structure_esta": n("structure_esta"),
        "grass_esta": n("grass_esta"),
    }
    t["structure"] = t["structure_cfa"] + t["structure_frv"] + t["structure_esta"]
    t["grass"] = t["grass_cfa"] + t["grass_ffm"] + t["grass_esta"]
    return t


# --------------------------------------------------------------------------- #
# Trend buckets
# --------------------------------------------------------------------------- #
def bucket_unit(start, end):
    span = end - start
    if span <= timedelta(days=2):
        return "hour"
    if span <= timedelta(days=92):
        return "day"
    return "month"


def bucket_edges(start, end, unit):
    """Left edges of each bucket covering [start, end). Days run 06:00->06:00."""
    edges = []
    if unit == "hour":
        t = start.replace(minute=0, second=0, microsecond=0)
        while t < end:
            edges.append(t)
            t += timedelta(hours=1)
    elif unit == "day":
        t = datetime.combine(start.date(), DAY_START)
        if t > start:
            t -= timedelta(days=1)
        while t < end:
            edges.append(t)
            t += timedelta(days=1)
    else:
        t = datetime(start.year, start.month, 1)
        while t < end:
            edges.append(t)
            t = datetime(t.year + (t.month == 12), t.month % 12 + 1, 1)
    return edges


def bucket_label(edge, unit):
    if unit == "hour":
        return f"{edge:%a %H:00}"
    if unit == "day":
        return f"{edge:%a} {edge.day} {edge:%b}"
    return f"{edge:%b %Y}"


def trend(df, start, end):
    """Per-bucket counts of each headline metric -> DataFrame (bucket, label, …)."""
    unit = bucket_unit(start, end)
    edges = bucket_edges(start, end, unit)
    out = pd.DataFrame({"bucket": edges,
                        "label": [bucket_label(e, unit) for e in edges]})
    for key, _ in METRICS:
        out[key] = 0
    if df.empty or not edges:
        return out, unit
    started = pd.to_datetime(df["started"], errors="coerce")
    idx = pd.Series(pd.DatetimeIndex(edges).searchsorted(started, side="right") - 1,
                    index=df.index)
    # An incident at exactly a bucket's left edge belongs to the bucket before
    # (windows are (start, end]) — except the very first edge.
    on_edge = started.isin(edges) & (idx > 0)
    idx[on_edge] -= 1
    valid = (idx >= 0) & started.notna()
    for key, _ in METRICS:
        col = df[key] if key != "incidents" else pd.Series(True, index=df.index)
        counts = idx[valid & col.astype(bool)].value_counts()
        for i, c in counts.items():
            out.at[int(i), key] = int(c)
    return out, unit


def active_trend(start, end):
    """Active incidents / active fires over the period from fire_timeseries,
    aggregated in SQL (a FY is ~175k cycles) to the peak per hour or day."""
    unit = "hour" if end - start <= timedelta(days=14) else "day"
    width = 13 if unit == "hour" else 10
    df = database.read_df(
        f"SELECT SUBSTR(timestamp, 1, {width}) AS b, "
        "MAX(total_active - COALESCE(emergency_warnings, 0) - COALESCE(watch_act, 0) "
        "    - COALESCE(advice, 0)) AS incidents, "
        "MAX(active_fires) AS fires FROM fire_timeseries "
        "WHERE timestamp > ? AND timestamp <= ? GROUP BY b ORDER BY b",
        [start.strftime(TS), end.strftime(TS)])
    if not df.empty:
        fmt = "%Y-%m-%d %H" if unit == "hour" else "%Y-%m-%d"
        df["bucket"] = pd.to_datetime(df["b"], format=fmt, errors="coerce")
    return df, unit


# --------------------------------------------------------------------------- #
# Breakdowns
# --------------------------------------------------------------------------- #
def top_types(df, mask=None, limit=10):
    sub = df if mask is None else df[mask]
    if sub.empty:
        return []
    counts = sub.apply(incident_type, axis=1).value_counts()
    return [(k, int(v)) for k, v in counts.head(limit).items()]


def locality(location):
    """'12 Smith St, Ferntree Gully' -> 'Ferntree Gully'."""
    text = _s(location)
    return text.rsplit(",", 1)[-1].strip().title() if text else ""


def top_localities(df, mask=None, limit=10):
    sub = df if mask is None else df[mask]
    if sub.empty:
        return []
    counts = sub["location"].map(locality).replace("", pd.NA).dropna().value_counts()
    return [(k, int(v)) for k, v in counts.head(limit).items()]


AGENCY_COLUMNS = [("cfa", "CFA"), ("frv", "FRV"), ("ffm", FFM_LABEL),
                  (ESTA, "ESTA (not identified)"), ("other", "Other")]
KIND_ROWS = [("structure", "Structure"), ("grass", "Grass & scrub/bush"),
             (None, "Other fires")]


def fire_matrix(df):
    """Fires by kind (rows) × agency (columns), plus row totals."""
    fires = df[df["fire"].astype(bool)] if not df.empty else df
    rows = []
    for kind, label in KIND_ROWS:
        if fires.empty:
            sub = fires
        elif kind is None:
            sub = fires[~fires["kind"].isin(["structure", "grass"])]
        else:
            sub = fires[fires["kind"] == kind]
        cells = {}
        for key, _ in AGENCY_COLUMNS:
            if sub.empty:
                cells[key] = 0
            elif key == "other":
                cells[key] = int((~sub["agency"].isin(["cfa", "frv", "ffm", ESTA])).sum())
            else:
                cells[key] = int((sub["agency"] == key).sum())
        rows.append({"kind": label, **cells, "total": int(len(sub))})
    return rows


def coverage_start():
    """When the fire collector's history begins (first incident seen)."""
    df = database.read_df("SELECT MIN(first_seen) AS t FROM fire_incidents")
    t = None if df.empty else _s(df.iloc[0]["t"])
    try:
        return datetime.strptime(t, TS) if t else None
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# Snapshot
# --------------------------------------------------------------------------- #
@dataclass
class Snapshot:
    period: Period
    now: datetime
    totals: dict
    previous: dict = None
    trend: pd.DataFrame = None
    unit: str = "day"
    active: dict = field(default_factory=dict)
    active_trend: pd.DataFrame = None
    active_unit: str = "hour"
    rfa_types: list = field(default_factory=list)
    rfa_localities: list = field(default_factory=list)
    fire_types: list = field(default_factory=list)
    fire_matrix: list = field(default_factory=list)
    check: list = field(default_factory=list)
    active_rfa_types: list = field(default_factory=list)
    coverage: datetime = None
    notes: list = field(default_factory=list)


def build(period, now=None):
    now = now or datetime.now()
    end = period.effective_end(now)
    df = incidents_started(period.start, end)
    snap = Snapshot(period=period, now=now, totals=totals(df))
    snap.trend, snap.unit = trend(df, period.start, end)
    if period.previous:
        pstart, pend = period.previous
        snap.previous = totals(incidents_started(pstart, min(pend, now)))
    act = active_incidents()
    snap.active = {"incidents": len(act),
                   "rfa": int(act["rfa"].sum()) if not act.empty else 0,
                   "fires": int(act["fire"].sum()) if not act.empty else 0}
    snap.active_rfa_types = top_types(act, act["rfa"].astype(bool) if not act.empty else None, 6)
    snap.active_trend, snap.active_unit = active_trend(period.start, end)
    if not df.empty:
        snap.rfa_types = top_types(df, df["rfa"].astype(bool))
        snap.rfa_localities = top_localities(df, df["rfa"].astype(bool))
        snap.fire_types = top_types(df, df["fire"].astype(bool))
    snap.fire_matrix = fire_matrix(df)
    snap.check = classification_check(df)
    snap.coverage = coverage_start()
    if period.partial(now):
        snap.notes.append(f"Period not complete — counted to {_fmt(now)}.")
    if snap.coverage is None:
        snap.notes.append("The VicEmergency collector has no incidents recorded yet.")
    elif snap.coverage > period.start:
        snap.notes.append(
            f"Collection starts {_fmt(snap.coverage)} — anything before that is "
            "not counted, so this period is PARTIAL.")
    if period.previous and snap.coverage and snap.coverage > period.previous[0]:
        snap.previous = None        # the comparison would be against missing data
        snap.notes.append(f"No comparison with the {period.previous_label}: "
                          "collection had not started.")
    return snap


_cache = {}
_lock = threading.Lock()


def cached(period, now=None):
    """build(), shared by every viewer for CACHE_SECONDS (a FY reads a year)."""
    key = (period.kind, period.start, period.end)
    with _lock:
        hit = _cache.get(key)
        if hit and _time.monotonic() - hit[0] < CACHE_SECONDS:
            return hit[1]
    snap = build(period, now)
    with _lock:
        if len(_cache) > 32:
            _cache.clear()
        _cache[key] = (_time.monotonic(), snap)
    return snap


def resolve_period(kind, day=None, fy=None, tag=None, now=None):
    """Turn the page's filter values into a Period (None for an unknown event)."""
    now = now or datetime.now()
    if kind == PERIOD_TODAY:
        return today_period(now)
    if kind == PERIOD_FY:
        return fy_period(int(fy) if fy else fy_start_year(now.date()), now)
    if kind == PERIOD_EVENT:
        return event_period(tag, now) if tag else None
    d = day if isinstance(day, date) else (
        date.fromisoformat(str(day)[:10]) if day else last_completed_day(now))
    return day_period(d)

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
HOW_ESTA_TYPE = "esta type"
# ESTA's own event type tells the two services' jobs apart: CFA jobs are
# "Building Fire" (the CFA feed's own word), ex-MFB FRV jobs "Structure Fire".
# Seen live 2026-10-14: every paged ESTA "Building Fire" was a CFA brigade's.
ESTA_CFA_TYPES = {"building fire"}


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
    # Only raw ESTA events are inferred: the CFA feed reuses ESTA:<n> ids, but
    # a CFA-feed job is CFA's whether or not the pager page was seen.
    raw_esta = df["source_org"].map(lambda v: "ESTA" in _s(v).upper())
    unpaged = esta & ~paged & raw_esta
    cfa_type = unpaged & df["category2"].map(lambda v: _s(v).lower() in ESTA_CFA_TYPES)
    df.loc[cfa_type, "agency_hint"] = "cfa"
    df.loc[cfa_type, "agency_how"] = HOW_ESTA_TYPE
    rest = unpaged & ~cfa_type
    if rest.any():
        covered = _pager_covered(df.loc[rest, "started"], heartbeats)
        idx = df.index[rest][covered]
        df.loc[idx, "agency_hint"] = "frv"
        df.loc[idx, "agency_how"] = HOW_NOT_PAGED
    return df


# --------------------------------------------------------------------------- #
# Duplicate ESTA events: every Triple Zero call is its own ESTA event, so one
# fire can arrive as several (5 for one Moonee Ponds fire in 4 minutes), plus
# the CFA feed's own record of the same job.
# --------------------------------------------------------------------------- #
DUP_WINDOW_MINUTES = 20
DUP_MAX_KM = 3


def _dup_key(r):
    c1 = _s(r.get("category1")).lower()
    kind = r.get("kind")
    sub = kind if isinstance(kind, str) and kind else _s(r.get("category2")).lower()
    return (c1, sub, locality(r.get("location")))


def _km(a, b):
    import math
    la1, lo1, la2, lo2 = (math.radians(x) for x in (a[0], a[1], b[0], b[1]))
    h = (math.sin((la2 - la1) / 2) ** 2
         + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2)
    return 6371 * 2 * math.asin(math.sqrt(min(1.0, h)))


def _coords(r):
    try:
        lat, lon = float(r.get("latitude")), float(r.get("longitude"))
    except (TypeError, ValueError):
        return None
    return None if lat != lat or lon != lon else (lat, lon)


def mark_duplicates(df):
    """Flag `duplicate` (+ `dup_of`) on the extra records of one incident: the
    same kind of job in the same locality within DUP_WINDOW_MINUTES of the
    previous record, where at least one of them is a raw ESTA event (two
    CFA-feed jobs are two jobs). Where both have coordinates they must also be
    within DUP_MAX_KM. The record kept is the agency's own (CFA feed) one,
    else a pager-attributed one, else the earliest."""
    df = df.copy()
    df["duplicate"] = False
    df["dup_of"] = None
    if df.empty:
        return df
    recs = df.to_dict("records")
    order = sorted(range(len(recs)), key=lambda i: _s(recs[i].get("started")))
    clusters = []                       # [key, last_time, [indices]]
    for i in order:
        r = recs[i]
        key = _dup_key(r)
        t = pd.to_datetime(r.get("started"), errors="coerce")
        if not key[2] or pd.isna(t):
            continue
        is_esta = "ESTA" in _s(r.get("source_org")).upper()
        here = _coords(r)
        joined = False
        for c in clusters:
            if c[0] != key or t - c[1] > timedelta(minutes=DUP_WINDOW_MINUTES):
                continue
            members = [recs[j] for j in c[2]]
            if not (is_esta or any("ESTA" in _s(m.get("source_org")).upper() for m in members)):
                continue
            pts = [p for p in (_coords(m) for m in members) if p]
            if here and pts and min(_km(here, p) for p in pts) > DUP_MAX_KM:
                continue
            c[1] = t
            c[2].append(i)
            joined = True
            break
        if not joined:
            clusters.append([key, t, [i]])
    for _, _, idx in clusters:
        if len(idx) < 2:
            continue

        def rank(j):
            m = recs[j]
            return ("ESTA" in _s(m.get("source_org")).upper(),
                    _s(m.get("agency_how")) != HOW_PAGER, _s(m.get("started")))
        keep = min(idx, key=rank)
        for j in idx:
            if j != keep:
                df.loc[df.index[j], "duplicate"] = True
                df.loc[df.index[j], "dup_of"] = recs[keep].get("source_id")
    return df


def counted(df):
    """The rows that count (duplicates removed)."""
    if df is None or df.empty or "duplicate" not in df:
        return df
    return df[~df["duplicate"].fillna(False).astype(bool)]


def counted_as(r):
    """The headline figure a classified row lands in (for the check table)."""
    if r.get("duplicate") is True:
        return "Not counted — duplicate ESTA event of " + _s(r.get("dup_of"))
    if r.get("rfa"):
        return "SES RFA"
    via = {HOW_PAGER: " — via CFA pager",
           HOW_NOT_PAGED: " — ESTA job not on the CFA pager (ex-MFB area)",
           HOW_ESTA_TYPE: " — ESTA event type Building Fire (CFA)",
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
                "structure", "grass", "agency_hint", "agency_how", "duplicate", "dup_of"]
        return df.assign(**{c: pd.Series(dtype=object) for c in cols})
    df = attribute_esta(df)
    flags = pd.DataFrame([classify(r) for r in df.to_dict("records")], index=df.index)
    out = pd.concat([df, flags], axis=1)
    out["structure"] = out["structure_cfa"] | out["structure_frv"] | out["structure_esta"]
    out["grass"] = out["grass_cfa"] | out["grass_ffm"] | out["grass_esta"]
    return mark_duplicates(out)


_INCIDENT_SQL = ("feed_type NOT IN ('warning', 'burn-area')")
_STARTED = "COALESCE(created, first_seen)"


def incidents_started(start, end):
    """Incidents that started in (start, end], classified."""
    df = database.read_df(
        f"SELECT source_id, category1, category2, source_org, location, status, "
        f"resolved, latitude, longitude, size, {_STARTED} AS started FROM fire_incidents "
        f"WHERE {_INCIDENT_SQL} AND {_STARTED} > ? AND {_STARTED} <= ?",
        [start.strftime(TS), end.strftime(TS)])
    return _frame(df)


def active_incidents():
    df = database.read_df(
        "SELECT source_id, category1, category2, source_org, location, status, "
        f"resolved, latitude, longitude, size, {_STARTED} AS started FROM fire_incidents "
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


# --------------------------------------------------------------------------- #
# Burn areas + reported hectares
# --------------------------------------------------------------------------- #
def reported_ha(df, mask=None):
    """(hectares, fires with a size) from the feed's reported size ('2.4 Ha.').
    Sizes like 'Small' carry no number and are left out, not read as 0."""
    from app.intel_feed import _parse_ha
    sub = df if mask is None else df[mask]
    if sub is None or sub.empty or "size" not in sub:
        return 0.0, 0
    vals = [v for v in (_parse_ha(x) for x in sub["size"]) if v is not None]
    return float(sum(vals)), len(vals)


# DEECA re-issues a burn's footprint under a NEW id each time it re-maps it
# (one Seacombe burn arrived as ~30 records of 800-950 ha), so records are
# grouped by name into one burn: same name, each record within
# BURN_GROUP_GAP_DAYS of the previous one. Its area is the UNION of its shapes.
BURN_GROUP_GAP_DAYS = 30
# The feed never says planned vs bushfire. A burn with a fire incident on it
# (within BURN_FIRE_KM, started BURN_FIRE_DAYS_BEFORE before its first record
# to BURN_FIRE_DAYS_AFTER after its last) is that fire's footprint.
BURN_FIRE_KM = 2.0
BURN_FIRE_DAYS_BEFORE = 3
BURN_FIRE_DAYS_AFTER = 2
BURN_PLANNED = "planned"
BURN_BUSHFIRE = "bushfire"
_PLANNED_RE = re.compile(r"planned|burn[ -]?off|fuel reduction", re.I)


def _burn_name(r):
    return " ".join((_s(r.get("headline")) or _s(r.get("location"))).lower().split())


def _burn_groups(df):
    """[[record dicts]] — one list per burn (records sorted by date)."""
    groups = []
    for _, g in df.sort_values("dated", kind="stable").groupby("name", sort=False):
        current, last = [], None
        for r in g.to_dict("records"):
            t = pd.to_datetime(r["dated"])
            if current and t - last > timedelta(days=BURN_GROUP_GAP_DAYS):
                groups.append(current)
                current = []
            current.append(r)
            last = t
        if current:
            groups.append(current)
    return groups


def _fires_near(t0, t1):
    """Fire incidents (any agency) started in [t0, t1] with a position."""
    df = database.read_df(
        "SELECT source_id, source_org, category1, category2, location, latitude, longitude, "
        f"{_STARTED} AS started FROM fire_incidents WHERE {_INCIDENT_SQL} "
        f"AND latitude IS NOT NULL AND {_STARTED} >= ? AND {_STARTED} <= ?",
        [t0.strftime(TS), t1.strftime(TS)])
    if df.empty:
        return df
    return df[[fire_kind_of(a, b)[0] for a, b in zip(df["category1"], df["category2"])]]


def burn_areas_started(start, end):
    """One row per BURN whose first dated record falls in (start, end]: merged
    footprint hectares, record count, and planned / bushfire with the reason.
    Undated records (the feed gave no date, so only when we first saw them
    is known) are left out and counted in `undated`."""
    import shapely
    from app.fire_areas import metric_ha, polygons_only, to_metric
    cols = ["burn_id", "name", "dated", "last_dated", "records", "ha", "kind",
            "matched", "in_feed"]
    look_back = start - timedelta(days=BURN_GROUP_GAP_DAYS)
    df = database.read_df(
        "SELECT source_id, location, headline, geometry, resolved, created, first_seen "
        "FROM fire_incidents WHERE feed_type = 'burn-area' "
        f"AND {_STARTED} > ? AND {_STARTED} <= ?",
        [look_back.strftime(TS), end.strftime(TS)])
    undated = 0
    if not df.empty:
        dated_mask = df["created"].map(lambda v: bool(_s(v)))
        undated = int((~dated_mask & (df["first_seen"] > start.strftime(TS))).sum())
        df = df[dated_mask].copy()
    if df.empty:
        out = pd.DataFrame(columns=cols)
        out.attrs["undated"] = undated
        return out
    df["dated"] = df["created"]
    df["name"] = [_burn_name(r) for r in df.to_dict("records")]
    groups = [g for g in _burn_groups(df)
              if start.strftime(TS) < g[0]["dated"] <= end.strftime(TS)]
    rows = []
    if groups:
        first = min(pd.to_datetime(g[0]["dated"]) for g in groups)
        last = max(pd.to_datetime(g[-1]["dated"]) for g in groups)
        fires = _fires_near(first - timedelta(days=BURN_FIRE_DAYS_BEFORE),
                            last + timedelta(days=BURN_FIRE_DAYS_AFTER))
    for g in groups:
        shapes = [m for m in (to_metric(polygons_only(r["geometry"])) for r in g) if m is not None]
        union = shapely.union_all(shapes) if shapes else None
        kind, matched = BURN_PLANNED, "no fire incident on it"
        if union is not None and not fires.empty:
            t0 = pd.to_datetime(g[0]["dated"]) - timedelta(days=BURN_FIRE_DAYS_BEFORE)
            t1 = pd.to_datetime(g[-1]["dated"]) + timedelta(days=BURN_FIRE_DAYS_AFTER)
            near = []
            for f in fires.to_dict("records"):
                ft = pd.to_datetime(f["started"], errors="coerce")
                if pd.isna(ft) or not (t0 <= ft <= t1):
                    continue
                pt = to_metric({"type": "Point",
                                "coordinates": [float(f["longitude"]), float(f["latitude"])]})
                if pt is not None and union.distance(pt) <= BURN_FIRE_KM * 1000:
                    near.append(f)
            if near:
                f = near[0]
                what = f"{_s(f['category2']) or _s(f['category1'])}, {_s(f['location'])} " \
                       f"({_s(f['source_org'])}, {_s(f['started'])[:16]})"
                if all(_PLANNED_RE.search(_s(x["category2"])) for x in near):
                    kind, matched = BURN_PLANNED, "planned burn incident: " + what
                else:
                    kind, matched = BURN_BUSHFIRE, what
        rows.append({
            "burn_id": g[-1]["source_id"], "name": _s(g[-1].get("headline")) or
            _s(g[-1].get("location")), "dated": g[0]["dated"], "last_dated": g[-1]["dated"],
            "records": len(g), "ha": metric_ha(union) if union is not None else 0.0,
            "kind": kind, "matched": matched,
            "in_feed": any(str(r.get("resolved")) in ("0", "0.0", "False") for r in g)})
    out = pd.DataFrame(rows, columns=cols)
    out.attrs["undated"] = undated
    return out


def burn_totals(df):
    if df is None or df.empty:
        return {"count": 0, "ha": 0.0, "planned": 0, "bushfire": 0,
                "planned_ha": 0.0, "bushfire_ha": 0.0,
                "undated": 0 if df is None else df.attrs.get("undated", 0)}
    p, b = df[df["kind"] == BURN_PLANNED], df[df["kind"] == BURN_BUSHFIRE]
    return {"count": len(df), "ha": float(df["ha"].sum()),
            "planned": len(p), "bushfire": len(b),
            "planned_ha": float(p["ha"].sum()), "bushfire_ha": float(b["ha"].sum()),
            "undated": df.attrs.get("undated", 0)}


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
    rows: pd.DataFrame = None          # classified incidents started in the period
    active_rows: pd.DataFrame = None   # classified incidents open now
    burn_rows: pd.DataFrame = None     # burn areas dated in the period
    burn: dict = field(default_factory=dict)
    previous_burn: dict = None
    grass_ha: tuple = (0.0, 0)         # (reported hectares, fires with a size)


def build(period, now=None):
    now = now or datetime.now()
    end = period.effective_end(now)
    full = incidents_started(period.start, end)
    df = counted(full)
    snap = Snapshot(period=period, now=now, totals=totals(df), rows=full)
    snap.trend, snap.unit = trend(df, period.start, end)
    if period.previous:
        pstart, pend = period.previous
        snap.previous = totals(counted(incidents_started(pstart, min(pend, now))))
    act_full = active_incidents()
    snap.active_rows = act_full
    act = counted(act_full)
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
    snap.check = classification_check(full)
    snap.burn_rows = burn_areas_started(period.start, end)
    snap.burn = burn_totals(snap.burn_rows)
    if snap.previous is not None and period.previous:
        pstart, pend = period.previous
        snap.previous_burn = burn_totals(burn_areas_started(pstart, min(pend, now)))
    if not df.empty:
        snap.grass_ha = reported_ha(df, df["grass"].fillna(False).astype(bool))
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


# --------------------------------------------------------------------------- #
# What makes up each card (admin "copy" button)
# --------------------------------------------------------------------------- #
# card key -> (label, which frame, flag column or None for every row)
CARD_ROWS = {
    "rfa": ("SES RFAs", "rows", "rfa"),
    "structure": ("Structure fires", "rows", "structure"),
    "grass": ("Grass & scrub/bush fires", "rows", "grass"),
    "incidents": ("All incidents", "rows", None),
    "active_incidents": ("Active open incidents", "active_rows", None),
    "active_rfa": ("Active SES RFAs", "active_rows", "rfa"),
    "active_fires": ("Active fires", "active_rows", "fire"),
    "burn": ("Burn areas", "burn_rows", None),
}
BURN_COPY_COLUMNS = ["First dated", "Last dated", "Burn area ID (latest)", "Name",
                     "Footprint records", "Hectares (merged)", "Type", "Why", "Still in feed"]
_AGENCY_NAMES = {**fire_data.AGENCY_LABELS, "ffm": FFM_LABEL,
                 ESTA: "Not identified (ESTA)"}
_HOW_NAMES = {HOW_PAGER: "CFA pager", HOW_NOT_PAGED: "Not on CFA pager (ex-MFB area)",
              HOW_ESTA_TYPE: "ESTA event type (Building Fire = CFA)"}
COPY_COLUMNS = ["Started", "Incident ID", "F-number", "Agency", "Agency from",
                "Source org", "Category", "Sub-category", "Location", "Status",
                "Reported size", "Open", "Counted as"]


def card_rows(snap, key, duplicates=False):
    """The incidents behind one card, oldest first (with `duplicates`, the
    duplicate ESTA events that were merged into them instead)."""
    _, frame, flag = CARD_ROWS[key]
    df = getattr(snap, frame)
    if df is None or df.empty:
        return pd.DataFrame()
    if flag:
        df = df[df[flag].fillna(False).astype(bool)]
    dup = df["duplicate"].fillna(False).astype(bool) if "duplicate" in df else False
    df = df[dup] if duplicates else df[~dup] if "duplicate" in df else df
    return df.sort_values("started", kind="stable")


def _cell(v):
    return " ".join(_s(v).split())       # no tabs / newlines inside a cell


def card_text(snap, key):
    """Tab-separated text (pastes straight into Excel) of one card's incidents,
    under a line naming the card, the period and the count."""
    label, frame, _ = CARD_ROWS[key]
    if key == "burn":
        return _burn_text(snap, label)
    df = card_rows(snap, key)
    scope = ("open now" if frame == "active_rows" else snap.period.label)
    lines = [f"{label} — {scope} — {len(df)} incident{'s' if len(df) != 1 else ''}",
             "\t".join(COPY_COLUMNS)]
    dups = card_rows(snap, key, duplicates=True)
    records = df.to_dict("records")
    if not dups.empty:
        records += [None] + dups.to_dict("records")
    for r in records:
        if r is None:
            lines += ["", f"Not counted — {len(dups)} duplicate ESTA event"
                          f"{'s' if len(dups) != 1 else ''} of the incidents above",
                      "\t".join(COPY_COLUMNS)]
            continue
        source = "Feed sourceOrg" if not _s(r.get("agency_how")) else \
            _HOW_NAMES.get(_s(r.get("agency_how")), _s(r.get("agency_how")))
        lines.append("\t".join(_cell(v) for v in (
            r.get("started"), r.get("source_id"), r.get("f_number"),
            _AGENCY_NAMES.get(r.get("agency"), r.get("agency")), source,
            r.get("source_org"), r.get("category1"), r.get("category2"),
            r.get("location"), r.get("status"), r.get("size"),
            "No" if str(r.get("resolved")) in ("1", "1.0", "True") else "Yes",
            counted_as(r))))
    return "\n".join(lines)


def _burn_text(snap, label):
    df = snap.burn_rows
    b = snap.burn or {}
    n = 0 if df is None else len(df)
    lines = [f"{label} — {snap.period.label} — {n} burn{'s' if n != 1 else ''}, "
             f"{b.get('ha', 0):,.1f} ha ({b.get('planned', 0)} planned, "
             f"{b.get('bushfire', 0)} bushfire)", "\t".join(BURN_COPY_COLUMNS)]
    if n:
        for r in df.sort_values("dated", kind="stable").to_dict("records"):
            lines.append("\t".join(_cell(v) for v in (
                r["dated"], r["last_dated"], r["burn_id"], r["name"], r["records"],
                f"{r['ha'] or 0:.1f}", "Bushfire" if r["kind"] == BURN_BUSHFIRE else "Planned burn",
                r["matched"], "Yes" if r["in_feed"] else "No")))
    if b.get("undated"):
        lines += ["", f"Not counted — {b['undated']} burn area record"
                      f"{'s' if b['undated'] != 1 else ''} the feed gave no date"]
    return "\n".join(lines)


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

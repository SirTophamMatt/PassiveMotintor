# Passive Monitor — Copyright (c) 2026 SirTophamMatt. All rights reserved.
"""Power outage identity — one ID per EM-COP outage, stable across cycles.

EM-COP's outage dashboard lists each outage as ONE table row: the localities
it affects, customers off, type and when it was first seen. The scraper used to
split that row into one ``power_outages`` row per locality and keep nothing
that said they belonged together — so a 12-suburb outage looked like 12
outages, each carrying the whole customer count.

This module keeps the row together:

* **One outage = one ``power_outage_events`` row** with a reference like
  ``PWR-261104-4XKQ`` (date first seen + four characters from an alphabet with
  no I/O/0/1, the feedback-reference convention — readable off a screen).
* **Its localities stay linked**: each ``power_outages`` row carries the
  ``outage_ref`` it was listed under, so any suburb leads to its outage and to
  every other suburb on it. A suburb on two outages has two rows, one per
  outage — which is what EM-COP actually said.

**EM-COP publishes no outage id of its own** (none that we have seen), so
identity is MATCHED each cycle (``match``): a row is the same outage as a known
one when the type agrees and either the first-seen minute agrees and they
share at least one locality, or their locality lists are mostly the same
(``SAME_LIST_MIN``) — which covers a first-seen time read differently on a
later cycle. Customers off is not part of identity (it moves as supply comes
back) beyond breaking a tie. Pairs are taken best-first and one-to-one, so two
outages that started in the same minute in the same suburb (planned works) keep
separate IDs. A known outage nobody matched is restored; one that comes back
within ``power.reopen_minutes`` keeps its ID rather than being re-issued.

**Rows stored before this existed** (``outage_ref`` NULL) are adopted on the
first cycle: still-active ones are grouped the old way (same customers, type
and first seen — ``legacy_key``) and matched like any other known outage.
"""
import json
import logging
import random
import sqlite3
from datetime import datetime, timedelta

import pandas as pd

from app import database, history
from app.localities import display_name, normalise

log = logging.getLogger(__name__)

REF_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
# Share of localities two lists must have in common to be one outage when the
# first-seen time does NOT agree.
SAME_LIST_MIN = 0.5
REOPEN_MINUTES = 30
_TS = "%Y-%m-%d %H:%M:%S"


# --------------------------------------------------------------------------- #
# Small pure helpers
# --------------------------------------------------------------------------- #
def _stamp(value=None):
    if value is None:
        return datetime.now().strftime(_TS)
    if isinstance(value, datetime) and not pd.isna(value):   # NaT is a datetime
        return value.strftime(_TS)
    parsed = pd.to_datetime(value, errors="coerce")
    return datetime.now().strftime(_TS) if pd.isna(parsed) else parsed.strftime(_TS)


def _text(value):
    """A clean string, "" for missing — None, NaN and pandas NaT included
    (str(NaT) is "NaT", the trap every collector here has hit once)."""
    if value is None or (isinstance(value, float) and value != value):
        return ""
    s = " ".join(str(value).replace("\xa0", " ").split())
    return "" if s.lower() in ("nan", "none", "nat") else s


def _num(value):
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    return None if n != n else n


def make_ref(first_seen=None):
    """A fresh outage reference. Uniqueness is the table's UNIQUE index;
    `sync` re-draws on the (rare) collision."""
    parsed = pd.to_datetime(first_seen, errors="coerce") if first_seen else None
    when = datetime.now() if parsed is None or pd.isna(parsed) else parsed
    return "PWR-%s-%s" % (when.strftime("%y%m%d"),
                          "".join(random.choice(REF_ALPHABET) for _ in range(4)))


def split_locations(raw):
    """EM-COP's locality list ("BELGRAVE, TECOMA, UPWEY") → ["BELGRAVE", …],
    de-duplicated, in the order listed."""
    out, seen = [], set()
    for part in str(raw or "").replace("\n", ",").replace(";", ",").split(","):
        name = _text(part)
        key = location_key(name)
        if name and key not in seen:
            seen.add(key)
            out.append(name)
    return out


def location_key(name):
    """Identity of a listed locality (case/punctuation-proof)."""
    return normalise(name) or _text(name).upper()


def _type_key(value):
    return _text(value).lower()


def _minute(value):
    return _stamp(value)[:16] if _text(value) else ""


def label(locations, limit=2):
    """"Belgrave, Tecoma +3" — how an outage is named on screens."""
    names = [display_name(n) for n in locations or [] if _text(n)]
    if not names:
        return "Outage"
    if len(names) <= limit + 1:
        return " & ".join(names) if len(names) == 2 else ", ".join(names)
    return "%s +%d" % (", ".join(names[:limit]), len(names) - limit)


def legacy_key(row):
    """How a pre-ID per-locality row is grouped with its siblings: rows of one
    outage share its count, type and start time (the old storm-wall rule)."""
    return ("legacy", _text(row.get("customers_off")), _type_key(row.get("type")),
            _minute(row.get("first_seen")))


def group_key(row):
    """The outage a per-locality row belongs to: its ID, or the legacy
    grouping for a row stored before IDs existed (old replay frames)."""
    ref = _text(row.get("outage_ref"))
    return ref if ref else legacy_key(row)


def _rows(df):
    if df is None or getattr(df, "empty", True):
        return []
    return df.to_dict("records")


def outage_count(df):
    """Distinct outages in a frame of per-locality rows."""
    return len({group_key(r) for r in _rows(df)})


def customers_total(df):
    """Customers off across per-locality rows, each OUTAGE counted once."""
    seen = {}
    for r in _rows(df):
        seen[group_key(r)] = _num(r.get("customers_off")) or 0
    return int(sum(seen.values()))


def groups(df):
    """{group key: [row dicts]} in first-seen order."""
    out = {}
    for r in _rows(df):
        out.setdefault(group_key(r), []).append(r)
    return out


# --------------------------------------------------------------------------- #
# Matching
# --------------------------------------------------------------------------- #
def _score(row, known):
    """How strongly a scraped row is `known`, or None if it cannot be."""
    if _type_key(row.get("type")) != _type_key(known.get("type")):
        return None
    a = {location_key(n) for n in row.get("locations") or []}
    b = {location_key(n) for n in known.get("locations") or []}
    union = a | b
    overlap = len(a & b) / len(union) if union else 0.0
    same_start = (_minute(row.get("first_seen")) == _minute(known.get("first_seen"))
                  and _minute(row.get("first_seen")) != "")
    if same_start and overlap > 0:
        score = 2.0 + overlap
    elif overlap >= SAME_LIST_MIN:
        score = 1.0 + overlap
    else:
        return None
    if _num(row.get("customers_off")) == _num(known.get("customers_off")):
        score += 0.05
    if known.get("restored"):
        score -= 0.5        # a live outage wins over reopening a restored one
    return score


def match(rows, known):
    """{row index: known index}, one-to-one, best pairs first."""
    pairs = []
    for i, row in enumerate(rows):
        for j, k in enumerate(known):
            s = _score(row, k)
            if s is not None:
                pairs.append((s, i, j))
    pairs.sort(key=lambda p: (-p[0], p[1], p[2]))
    taken_rows, taken_known, out = set(), set(), {}
    for _, i, j in pairs:
        if i in taken_rows or j in taken_known:
            continue
        taken_rows.add(i)
        taken_known.add(j)
        out[i] = j
    return out


# --------------------------------------------------------------------------- #
# Storing a cycle
# --------------------------------------------------------------------------- #
def _loads(raw):
    try:
        value = json.loads(raw) if raw else []
    except (TypeError, ValueError):
        return []
    return value if isinstance(value, list) else []


def _known(conn, now, reopen_minutes):
    """Active outages, recently restored ones (reopenable) and still-active
    legacy row groups — everything a scraped row may turn out to be."""
    cutoff = _stamp(now - timedelta(minutes=reopen_minutes))
    known = []
    cur = conn.execute(
        "SELECT outage_ref, type, first_seen, customers_off, peak_customers, "
        "       locations_json, all_locations_json, restored "
        "FROM power_outage_events WHERE restored = 0 OR restored_at >= ?", [cutoff])
    for ref, typ, first, cust, peak, locs, all_locs, restored in cur.fetchall():
        known.append({"ref": ref, "type": typ, "first_seen": first,
                      "customers_off": cust, "peak": peak,
                      "locations": _loads(locs), "all_locations": _loads(all_locs),
                      "restored": int(restored or 0)})
    legacy = {}
    cur = conn.execute(
        "SELECT id, location, customers_off, type, first_seen FROM power_outages "
        "WHERE restored = 0 AND outage_ref IS NULL ORDER BY id")
    for row_id, loc, cust, typ, first in cur.fetchall():
        key = legacy_key({"customers_off": cust, "type": typ, "first_seen": first})
        g = legacy.setdefault(key, {"ref": None, "type": typ, "first_seen": first,
                                    "customers_off": cust, "peak": cust,
                                    "locations": [], "all_locations": [],
                                    "restored": 0, "legacy_ids": []})
        g["locations"].append(loc)
        g["all_locations"].append(loc)
        g["legacy_ids"].append(row_id)
    return known + list(legacy.values())


def _insert_event(conn, row, now):
    for _ in range(10):
        ref = make_ref(row.get("first_seen"))
        try:
            conn.execute(
                "INSERT INTO power_outage_events (outage_ref, type, first_seen, "
                "last_seen, restored, customers_off, peak_customers, "
                "locations_json, all_locations_json, cells_json) "
                "VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?, ?)",
                [ref, row.get("type"), row.get("first_seen"), now,
                 row.get("customers_off"), row.get("customers_off"),
                 json.dumps(row["locations"]), json.dumps(row["locations"]),
                 row.get("cells_json")])
            return ref
        except sqlite3.IntegrityError:
            continue
    raise RuntimeError("could not allocate a unique outage reference")


def _sync_locations(conn, ref, row, now, reopen_cutoff):
    """Per-locality rows of one outage: update the listed ones, add new ones,
    restore any it no longer lists. A locality restored within the reopen
    window that is listed again gets its own row back (same rule as the
    outage itself), not a second row."""
    cur = conn.execute(
        "SELECT id, location, restored, last_seen FROM power_outages "
        "WHERE outage_ref = ? ORDER BY restored, last_seen DESC, id DESC", [ref])
    existing = {}
    for row_id, loc, restored, last_seen in cur.fetchall():
        existing.setdefault(location_key(loc), []).append(
            (row_id, int(restored or 0), str(last_seen or "")))
    listed = set()
    for name in row["locations"]:
        key = location_key(name)
        listed.add(key)
        rows_ = existing.get(key) or []
        active = next((r for r in rows_ if r[1] == 0), None)
        recent = next((r for r in rows_ if r[1] and r[2] >= reopen_cutoff), None)
        target = active or recent
        if target is not None:
            conn.execute(
                "UPDATE power_outages SET last_seen = ?, customers_off = ?, "
                "type = ?, restored = 0, duration_mins = NULL WHERE id = ?",
                [now, row.get("customers_off"), row.get("type"), target[0]])
        else:
            conn.execute(
                "INSERT INTO power_outages (location, customers_off, type, "
                "first_seen, last_seen, restored, outage_ref) "
                "VALUES (?, ?, ?, ?, ?, 0, ?)",
                [name, row.get("customers_off"), row.get("type"),
                 row.get("first_seen"), now, ref])
    for key, rows_ in existing.items():
        if key in listed:
            continue
        for row_id, restored, _ in rows_:
            if not restored:
                _restore_location(conn, row_id, now)


def _restore_location(conn, row_id, now):
    conn.execute(
        "UPDATE power_outages SET restored = 1, last_seen = ?, "
        "duration_mins = ROUND((julianday(?) - julianday(first_seen)) * 1440) "
        "WHERE id = ?", [now, now, row_id])


def sync(rows, now=None, header=None, reopen_minutes=REOPEN_MINUTES):
    """Store one cycle of EM-COP's outage table.

    `rows` are the table's rows, one per outage:
    ``{"locations": [...], "customers_off", "type", "first_seen",
    "cells": [...], "attrs": {...}}``. Returns {"outages", "new", "restored",
    "reopened", "adopted"} counts. Journals the per-locality state afterwards.
    """
    now_dt = now or datetime.now()
    stamp = _stamp(now_dt)
    rows = [dict(r, locations=list(r.get("locations") or []))
            for r in rows or [] if r.get("locations")]
    for r in rows:
        r["cells_json"] = json.dumps({"header": header or [],
                                      "cells": r.get("cells") or [],
                                      "attrs": r.get("attrs") or {}})
    counts = {"outages": len(rows), "new": 0, "restored": 0, "reopened": 0,
              "adopted": 0}
    reopen_cutoff = _stamp(now_dt - timedelta(minutes=reopen_minutes))
    conn = database.get_connection()
    try:
        known = _known(conn, now_dt, reopen_minutes)
        pairs = match(rows, known)
        matched_known = set(pairs.values())
        for i, row in enumerate(rows):
            j = pairs.get(i)
            k = known[j] if j is not None else None
            if k is None or k["ref"] is None:
                ref = _insert_event(conn, row, stamp)
                if k is None:
                    counts["new"] += 1
                else:   # a pre-ID outage: its rows join the new reference
                    counts["adopted"] += 1
                    ids = k["legacy_ids"]
                    conn.execute(
                        "UPDATE power_outages SET outage_ref = ? WHERE id IN (%s)"
                        % ",".join("?" * len(ids)), [ref] + ids)
            else:
                ref = k["ref"]
                counts["reopened"] += int(bool(k["restored"]))
                every = list(k["all_locations"])
                seen = {location_key(n) for n in every}
                every += [n for n in row["locations"] if location_key(n) not in seen]
                peak = max(_num(k.get("peak")) or 0, _num(row.get("customers_off")) or 0)
                conn.execute(
                    "UPDATE power_outage_events SET last_seen = ?, customers_off = ?, "
                    "peak_customers = ?, locations_json = ?, all_locations_json = ?, "
                    "cells_json = ?, restored = 0, restored_at = NULL, "
                    "duration_mins = NULL WHERE outage_ref = ?",
                    [stamp, row.get("customers_off"), int(peak),
                     json.dumps(row["locations"]), json.dumps(every),
                     row["cells_json"], ref])
            _sync_locations(conn, ref, row, stamp, reopen_cutoff)

        for j, k in enumerate(known):
            if j in matched_known or k["restored"]:
                continue
            if k["ref"] is None:
                for row_id in k["legacy_ids"]:
                    _restore_location(conn, row_id, stamp)
                continue
            counts["restored"] += 1
            conn.execute(
                "UPDATE power_outage_events SET restored = 1, restored_at = ?, "
                "last_seen = ?, duration_mins = "
                "ROUND((julianday(?) - julianday(first_seen)) * 1440) "
                "WHERE outage_ref = ?", [stamp, stamp, stamp, k["ref"]])
            cur = conn.execute("SELECT id FROM power_outages "
                               "WHERE outage_ref = ? AND restored = 0", [k["ref"]])
            for (row_id,) in cur.fetchall():
                _restore_location(conn, row_id, stamp)
        conn.commit()
    finally:
        conn.close()
    journal(stamp)
    return counts


# --------------------------------------------------------------------------- #
# State journal (Event Replay)
# --------------------------------------------------------------------------- #
def journal(now):
    """Record per-LOCALITY outage state changes for Event Replay.

    Still keyed by locality, as before IDs existed — so replay of older events
    and the replay export's per-location graphs read the same shape. A locality
    on two outages is ONE entity: customers summed across its outages (each is
    a separate outage there), and ``outages`` lists each one
    ``[ref, customers, type, first_seen]`` so replay can put the suburbs of one
    outage back together. Only what this cycle touched (``last_seen = now``)
    is read, so localities just restored get their tombstone in the same pass.
    Never fatal to collection.
    """
    try:
        df = database.read_df(
            "SELECT o.location, o.customers_off, o.type, o.first_seen, "
            "       o.restored, o.outage_ref, g.latitude, g.longitude "
            "FROM power_outages o "
            "LEFT JOIN geocode_cache g ON g.location = o.location "
            "WHERE o.last_seen = ?", [now])
        history.record_batch(history.POWER, journal_entities(df), effective_ts=now)
    except Exception:
        log.exception("Power: state journal write failed (collection unaffected)")


def journal_entities(df):
    """Per-locality journal entities from this cycle's per-locality rows."""
    out = []
    for location, grp in (df.groupby("location", sort=False) if df is not None
                          and not df.empty else []):
        active = grp[pd.to_numeric(grp["restored"], errors="coerce").fillna(0) == 0]
        rows = active if not active.empty else grp
        outages = sorted(
            [_text(r.get("outage_ref")),
             int(_num(r.get("customers_off")) or 0),
             _text(r.get("type")), _text(r.get("first_seen"))]
            for r in rows.to_dict("records"))
        types = sorted({o[2] for o in outages if o[2]})
        firsts = sorted(o[3] for o in outages if o[3])
        first = rows.iloc[0]
        out.append({
            "entity_key": str(location),
            "state": {"location": location,
                      "customers_off": int(sum(o[1] for o in outages)),
                      "type": " / ".join(types) or None,
                      "first_seen": firsts[0] if firsts else None,
                      "outages": outages},
            "active": not active.empty,
            "latitude": history._coord(first.get("latitude")),
            "longitude": history._coord(first.get("longitude")),
        })
    return out


def explode_journal(df):
    """Replay frame (one row per locality, as journalled) → one row per
    (outage, locality), each with ITS outage's customers/type/start. Rows
    journalled before IDs existed have no ``outages`` and pass through as they
    were (grouped later by `legacy_key`)."""
    if df is None or df.empty or "outages" not in df.columns:
        return df
    out = []
    for r in df.to_dict("records"):
        outages = r.get("outages")
        if not isinstance(outages, list) or not outages:
            out.append(r)
            continue
        for item in outages:
            if not isinstance(item, (list, tuple)) or len(item) < 4:
                continue
            ref, customers, typ, first = item[:4]
            out.append(dict(r, outage_ref=ref or None, customers_off=customers,
                            type=typ or None, first_seen=first or None))
    return pd.DataFrame(out, columns=list(df.columns) + (
        ["outage_ref"] if "outage_ref" not in df.columns else []))


# --------------------------------------------------------------------------- #
# One summary per outage (for maps, tables, the feed)
# --------------------------------------------------------------------------- #
def is_planned(value):
    return _type_key(value) == "planned"


def summaries(df):
    """One dict per outage from per-locality rows — boundaries attached
    (`localities.attach`) or not. Keys: key, ref, label, customers_off, type,
    first_seen, members (one per locality: location, display, status,
    latitude, longitude, geometry, locality_id), latitude/longitude (the mean
    of its members' points — where its marker goes), mapped, unmatched,
    ambiguous. Largest outage first."""
    out = []
    for key, rows in groups(df).items():
        members, pts = [], []
        for r in rows:
            name = _text(r.get("location"))
            lat, lon = _num(r.get("locality_lat")), _num(r.get("locality_lon"))
            if lat is None or lon is None:
                lat, lon = _num(r.get("latitude")), _num(r.get("longitude"))
            geometry = r.get("locality_geometry")
            members.append({
                "location": name,
                "display": _text(r.get("locality_name")) or display_name(name),
                "status": _text(r.get("locality_status")) or None,
                "latitude": lat, "longitude": lon,
                "geometry": geometry if isinstance(geometry, dict) else None,
                "locality_id": r.get("locality_id"),
            })
            if lat is not None and lon is not None:
                pts.append((lat, lon))
        customers = max((_num(r.get("customers_off")) or 0) for r in rows)
        first = rows[0]
        out.append({
            "key": key,
            "ref": _text(first.get("outage_ref")) or None,
            "label": label([m["location"] for m in members]),
            "customers_off": int(customers),
            "type": _text(first.get("type")) or None,
            "first_seen": _text(first.get("first_seen")) or None,
            "members": members,
            "latitude": sum(p[0] for p in pts) / len(pts) if pts else None,
            "longitude": sum(p[1] for p in pts) / len(pts) if pts else None,
            "mapped": sum(1 for m in members if m["geometry"]),
            "unmatched": [m["location"] for m in members if m["status"] == "unmatched"],
            "ambiguous": [m["location"] for m in members if m["status"] == "ambiguous"],
        })
    out.sort(key=lambda o: -o["customers_off"])
    return out

"""Generic entity state-change journal.

Most modules keep real history already: every flood reading, storm cell and AWS
observation is its own row, so "what did that gauge read at 14:30" is a query.
But fire incidents, road disruptions, per-location power outages and weather
warnings are stored as UPSERTs — one row per entity, overwritten in place. For
those, the past is simply gone, and no amount of UI work can reconstruct it.

This module is the missing piece. It records, per entity, the moments its state
CHANGED, so any past moment can be rebuilt as "the latest state at or before T".

Three properties it is built around:

* **Change-only.** A row is written when the state hash differs from the last
  recorded state for that entity. At a 60-second poll, snapshotting every cycle
  would cost ~1,440 rows per entity per day; a real incident produces a handful
  of rows over its entire life. This is what keeps replay affordable.
* **Tombstones.** An entity that resolves gets a final `active=0` row, so replay
  knows the difference between "it hadn't happened yet", "it was happening" and
  "it was over" — three different answers that a table of current rows collapses
  into one.
* **It never invents the past.** The journal starts when it starts. Current
  state is seeded once as the first known state, `history_availability` records
  when that was, and nothing is ever backdated. Replay is required to say so.
"""
import hashlib
import json
import logging
from datetime import datetime

import pandas as pd

from app import database

log = logging.getLogger(__name__)

# Known sources. Kept as constants so a typo becomes an import error rather
# than a silently empty replay layer.
FIRE, ROADS, POWER, WEATHER_WARNING = "fire", "roads", "power", "weather_warning"
SOURCES = (FIRE, ROADS, POWER, WEATHER_WARNING)

_TS_FMT = "%Y-%m-%d %H:%M:%S"


def _stamp(value=None):
    """Local-time 'YYYY-MM-DD HH:MM:SS'. The whole app stores naive Melbourne
    local time and compares timestamps as strings, so the journal does too —
    mixing in ISO-T or UTC here would silently break every BETWEEN in the app."""
    if value is None:
        return datetime.now().strftime(_TS_FMT)
    if isinstance(value, datetime):
        return value.strftime(_TS_FMT)
    parsed = pd.to_datetime(value, errors="coerce")
    if pd.isna(parsed):
        return datetime.now().strftime(_TS_FMT)
    return parsed.strftime(_TS_FMT)


def canonical_json(state):
    """Deterministic JSON for hashing: sorted keys, no insignificant spacing,
    and None/NaN normalised. Two dicts describing the same state must produce
    the same string no matter what order the scraper happened to build them in,
    or every cycle would look like a change."""
    def clean(value):
        if isinstance(value, dict):
            return {str(k): clean(v) for k, v in sorted(value.items())}
        if isinstance(value, (list, tuple)):
            return [clean(v) for v in value]
        if value is None:
            return None
        if isinstance(value, float):
            # NaN is not JSON and never equals itself — it would make every
            # comparison a change.
            return None if pd.isna(value) else value
        if isinstance(value, (int, bool, str)):
            return value
        if pd.isna(value):
            return None
        return str(value)

    return json.dumps(clean(state), sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def state_hash(state, active=True):
    """Hash identifying a state. `active` participates: a resolution that
    changes nothing else is still a change worth recording."""
    payload = "%s|%d" % (canonical_json(state), 1 if active else 0)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def _last_hash(source, entity_key):
    df = database.read_df(
        "SELECT state_hash FROM entity_state_history "
        "WHERE source = ? AND entity_key = ? "
        "ORDER BY effective_ts DESC, id DESC LIMIT 1", [source, str(entity_key)])
    return None if df.empty else df.iloc[0]["state_hash"]


def record_state(source, entity_key, state, effective_ts=None, active=True,
                 latitude=None, longitude=None, known_hash=None):
    """Record an entity's state if it changed. Returns True if a row was written.

    `known_hash` lets a batch caller supply the entity's last hash it already
    looked up, so a 1,000-entity cycle does one query instead of 1,001.
    """
    digest = state_hash(state, active)
    previous = known_hash if known_hash is not None else _last_hash(source, entity_key)
    if previous == digest:
        return False
    database.insert_rows("entity_state_history", [{
        "source": source,
        "entity_key": str(entity_key),
        "effective_ts": _stamp(effective_ts),
        "recorded_at": _stamp(),
        "active": 1 if active else 0,
        "state_json": canonical_json(state),
        "state_hash": digest,
        "latitude": latitude,
        "longitude": longitude,
    }], ignore_duplicates=True)
    return True


def _existing_hashes(source, entity_keys):
    """Last recorded hash for many entities in ONE query.

    The window function is the point: without it a cycle carrying a thousand
    road disruptions would issue a thousand ORDER BY ... LIMIT 1 lookups every
    poll, which is exactly the shape of query that took the VPS down in the
    flood-projection incident.
    """
    keys = [str(k) for k in entity_keys]
    if not keys:
        return {}
    placeholders = ",".join("?" * len(keys))
    df = database.read_df(
        "SELECT entity_key, state_hash FROM ("
        "  SELECT entity_key, state_hash, "
        "         ROW_NUMBER() OVER (PARTITION BY entity_key "
        "                            ORDER BY effective_ts DESC, id DESC) AS rn "
        "  FROM entity_state_history "
        "  WHERE source = ? AND entity_key IN (%s)"
        ") WHERE rn = 1" % placeholders, [source] + keys)
    return dict(zip(df["entity_key"].astype(str), df["state_hash"]))


def record_batch(source, entities, effective_ts=None):
    """Record a whole cycle's worth of entities, writing only what changed.

    `entities` is an iterable of dicts:
        {"entity_key", "state", "active", "latitude", "longitude",
         "effective_ts" (optional, per-entity source time)}

    Returns the number of state rows written. Also stamps
    `history_availability` the first time a source records anything, which is
    what lets Replay state honestly how far back it can reconstruct.
    """
    entities = list(entities)
    if not entities:
        return 0
    known = _existing_hashes(source, [e["entity_key"] for e in entities])
    written = 0
    for entity in entities:
        key = entity["entity_key"]
        if record_state(source, key, entity.get("state") or {},
                        effective_ts=entity.get("effective_ts", effective_ts),
                        active=entity.get("active", True),
                        latitude=entity.get("latitude"),
                        longitude=entity.get("longitude"),
                        known_hash=known.get(str(key))):
            written += 1
    if written:
        note_history_start(source)
    return written


def record_dataframe(source, df, key_col, state_cols, active_col=None,
                     active_value=0, ts_col=None, lat_col="latitude",
                     lon_col="longitude", effective_ts=None):
    """Journal a collector's post-cycle rows.

    Called with what the cycle actually touched (every source already stamps
    `last_seen`, so that set is free), which means an entity that dropped out of
    the feed and was just marked resolved gets its tombstone in the same pass as
    ordinary updates.

    `state_cols` is the MATERIAL state — deliberately not every column. Anything
    that moves every cycle (`last_seen`, and the feed's own `updated` stamp)
    must stay out of the hash or every poll would look like a change and the
    journal would degenerate into the snapshot table it exists to avoid. The
    feed's update time is the right `ts_col` instead: it is when the state
    became true, which is what replay needs.
    """
    if df is None or df.empty:
        return 0
    entities = []
    for _, row in df.iterrows():
        key = row.get(key_col)
        if key is None or (isinstance(key, float) and pd.isna(key)):
            continue
        active = True
        if active_col is not None:
            value = row.get(active_col)
            active = (0 if value is None or pd.isna(value) else value) == active_value
        entities.append({
            "entity_key": str(key),
            "state": {c: row.get(c) for c in state_cols},
            "active": active,
            "latitude": _coord(row.get(lat_col)) if lat_col else None,
            "longitude": _coord(row.get(lon_col)) if lon_col else None,
            "effective_ts": (row.get(ts_col) if ts_col and row.get(ts_col)
                             and not pd.isna(row.get(ts_col)) else effective_ts),
        })
    return record_batch(source, entities, effective_ts=effective_ts)


def _coord(value):
    if value is None or pd.isna(value):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def note_history_start(source, when=None, note=None):
    """Record when a source's journal began. Idempotent — only the first call
    for a source ever writes, so the start point can never drift later."""
    database.insert_rows("history_availability", [{
        "source": source,
        "available_from": _stamp(when),
        "note": note or "Journal started; earlier state was never recorded.",
    }], ignore_duplicates=True)


def history_start(source=None):
    """When full-fidelity history begins — for one source, or the LATEST start
    across all of them (the honest answer for a combined replay: the picture is
    only complete once every layer was journalling)."""
    if source:
        df = database.read_df(
            "SELECT available_from FROM history_availability WHERE source = ?",
            [source])
    else:
        df = database.read_df(
            "SELECT MAX(available_from) AS available_from FROM history_availability")
    if df.empty or pd.isna(df.iloc[0]["available_from"]):
        return None
    return _to_datetime(df.iloc[0]["available_from"])


def history_starts():
    """{source: datetime} for every journalling source."""
    df = database.read_df("SELECT source, available_from FROM history_availability")
    return {r["source"]: _to_datetime(r["available_from"])
            for _, r in df.iterrows()}


def _to_datetime(value):
    parsed = pd.to_datetime(value, errors="coerce")
    return None if pd.isna(parsed) else parsed.to_pydatetime()


def state_at(source, timestamp, include_inactive=False):
    """Every entity's state as it stood at `timestamp`.

    The latest row per entity at or before the moment — which is what "what did
    we know at 14:30" actually means. Entities that had not appeared yet are
    absent; entities already resolved come back with `active=0` and are dropped
    unless `include_inactive` is set.

    Returns a DataFrame with the journal columns plus the decoded `state`
    dict per row. Reconstruction happens in SQL (ROW_NUMBER over the source's
    index) so a replay tick reads only the rows it needs, never the whole
    history.
    """
    stamp = _stamp(timestamp)
    df = database.read_df(
        "SELECT entity_key, effective_ts, recorded_at, active, state_json, "
        "       latitude, longitude FROM ("
        "  SELECT *, ROW_NUMBER() OVER (PARTITION BY entity_key "
        "                               ORDER BY effective_ts DESC, id DESC) AS rn "
        "  FROM entity_state_history "
        "  WHERE source = ? AND effective_ts <= ?"
        ") WHERE rn = 1", [source, stamp])
    if df.empty:
        return _empty_state_frame()
    if not include_inactive:
        df = df[df["active"] == 1]
        if df.empty:
            return _empty_state_frame()
    return _expand(df)


def _empty_state_frame():
    return pd.DataFrame(columns=["entity_key", "effective_ts", "recorded_at",
                                 "active", "state_json", "latitude",
                                 "longitude", "state"])


def _expand(df):
    """Decode state_json into a `state` column, and lift its keys into real
    columns so replay can hand the frame straight to the map renderers."""
    df = df.copy()
    states = []
    for raw in df["state_json"]:
        try:
            states.append(json.loads(raw) if raw else {})
        except (ValueError, TypeError):
            states.append({})
    df["state"] = states
    keys = []
    for state in states:
        for key in state:
            if key not in keys:
                keys.append(key)
    for key in keys:
        if key in df.columns:
            continue  # never let stored state shadow a journal column
        df[key] = [state.get(key) for state in states]
    return df.reset_index(drop=True)


def states_between(source, start, end, include_inactive=True):
    """Every recorded state change for a source within a window, oldest first.
    Used to find the moments where the picture actually changed."""
    df = database.read_df(
        "SELECT entity_key, effective_ts, recorded_at, active, state_json, "
        "       latitude, longitude FROM entity_state_history "
        "WHERE source = ? AND effective_ts BETWEEN ? AND ? "
        "ORDER BY effective_ts, id", [source, _stamp(start), _stamp(end)])
    if df.empty:
        return _empty_state_frame()
    if not include_inactive:
        df = df[df["active"] == 1]
        if df.empty:
            return _empty_state_frame()
    return _expand(df)


def change_times(source, start, end):
    """The distinct moments a source's picture changed inside a window.

    Replay uses this to skip work: if the selected timestamp hasn't crossed a
    change, the previously reconstructed frame is still correct and nothing has
    to be re-queried or re-parsed.
    """
    df = database.read_df(
        "SELECT DISTINCT effective_ts FROM entity_state_history "
        "WHERE source = ? AND effective_ts BETWEEN ? AND ? "
        "ORDER BY effective_ts", [source, _stamp(start), _stamp(end)])
    if df.empty:
        return []
    return [_to_datetime(v) for v in df["effective_ts"] if _to_datetime(v)]


def entity_history(source, entity_key):
    """Every recorded state for one entity, oldest first — its life story."""
    df = database.read_df(
        "SELECT entity_key, effective_ts, recorded_at, active, state_json, "
        "       latitude, longitude FROM entity_state_history "
        "WHERE source = ? AND entity_key = ? ORDER BY effective_ts, id",
        [source, str(entity_key)])
    return _empty_state_frame() if df.empty else _expand(df)


def journal_summary():
    """Per-source row counts, entity counts and coverage — for the Admin page
    and for Replay's honesty banner."""
    df = database.read_df(
        "SELECT source, COUNT(*) AS rows, COUNT(DISTINCT entity_key) AS entities, "
        "       MIN(effective_ts) AS first_ts, MAX(effective_ts) AS last_ts "
        "FROM entity_state_history GROUP BY source")
    starts = history_starts()
    out = []
    for _, r in df.iterrows():
        out.append({
            "source": r["source"],
            "rows": int(r["rows"]),
            "entities": int(r["entities"]),
            "first_ts": _to_datetime(r["first_ts"]),
            "last_ts": _to_datetime(r["last_ts"]),
            "available_from": starts.get(r["source"]),
        })
    return out

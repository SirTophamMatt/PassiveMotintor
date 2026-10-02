# Passive Monitor — Copyright (c) 2026 SirTophamMatt. All rights reserved.
"""Road-disruption data queries and classification."""
import re

import pandas as pd

from app import database

# (sort priority, colour) — full closures sort first / red, other disruptions
# amber, mirroring the fire module's classify().
CLOSURE_STYLE = (1, "#d62728")
OTHER_STYLE = (3, "#ff7f0e")

_TS_COLS = ("start_time", "end_time", "created", "updated", "first_seen", "last_seen")

UNKNOWN_TYPE = "Unspecified"

# The scraper stores disruption_type as "eventType, eventSubType" (scraper
# _extract), so the PRIMARY type is everything before the first ", ". split_type
# below and this SQL must stay in step: the Python split classifies rows already
# loaded for the page, the SQL one aggregates the whole table for the filter list
# without dragging every row into pandas.
_TYPE_SQL = ("CASE WHEN instr(disruption_type, ', ') > 0 "
             "THEN substr(disruption_type, 1, instr(disruption_type, ', ') - 1) "
             "ELSE disruption_type END")


def classify(is_closure):
    """(priority, colour) for a disruption row."""
    return CLOSURE_STYLE if is_closure else OTHER_STYLE


def split_type(value):
    """'Flooding, Water over road' -> ('Flooding', 'Water over road').

    A feed field that arrived as a LIST is joined with the same ', ' separator,
    so a multi-valued eventType would split here too — rare, and it degrades to
    treating the first value as the type rather than to an error."""
    if value is None or value != value:            # None or NaN
        return UNKNOWN_TYPE, None
    text = str(value).strip()
    if not text:
        return UNKNOWN_TYPE, None
    primary, _, sub = text.partition(", ")
    return (primary.strip() or UNKNOWN_TYPE), (sub.strip() or None)


def filter_types(df, types):
    """Rows whose primary type is in `types` (no filtering when it's empty)."""
    if df is None or df.empty or not types:
        return df
    wanted = set(types)
    keep = [split_type(v)[0] in wanted for v in df["disruption_type"]]
    return df[keep]


def type_breakdown(df):
    """Per primary type: full-closure vs other counts, plus the sub-types behind
    them, sorted biggest total first — the stacked bar's source."""
    cols = ["type", "closures", "other", "total", "subtypes"]
    if df is None or df.empty:
        return pd.DataFrame(columns=cols)
    split = [split_type(v) for v in df["disruption_type"]]
    work = pd.DataFrame({
        "type": [s[0] for s in split],
        "subtype": [s[1] for s in split],
        "is_closure": df["is_closure"].fillna(0).astype(int).values,
    })
    rows = []
    for name, grp in work.groupby("type"):
        subs = grp["subtype"].dropna().value_counts()
        rows.append({
            "type": name,
            "closures": int(grp["is_closure"].sum()),
            "other": int((grp["is_closure"] == 0).sum()),
            "total": len(grp),
            "subtypes": ", ".join(f"{s} ({n})" for s, n in subs.items())
                        or "no sub-type given",
        })
    out = pd.DataFrame(rows, columns=cols)
    return out.sort_values(["total", "type"], ascending=[False, True],
                           ignore_index=True)


def type_options():
    """Every primary type the feed has produced, labelled with how many are
    active right now.

    Deliberately spans resolved rows too: the point is the catalogue of types
    this dataset contains, so a type that is quiet today still offers itself as
    a filter instead of vanishing from the list."""
    df = database.read_df(
        f"SELECT {_TYPE_SQL} AS type, "
        "SUM(CASE WHEN resolved = 0 THEN 1 ELSE 0 END) AS active, "
        "COUNT(*) AS seen FROM road_disruptions GROUP BY 1")
    if df.empty:
        return []
    df["type"] = df["type"].fillna(UNKNOWN_TYPE).replace("", UNKNOWN_TYPE)
    df = df.groupby("type", as_index=False)[["active", "seen"]].sum()
    df = df.sort_values(["active", "seen", "type"],
                        ascending=[False, False, True])
    return [{"label": f"{r['type']} ({int(r['active'])} active)",
             "value": r["type"]} for _, r in df.iterrows()]


def active_disruptions(closures_only=False):
    """Currently-active disruptions, most recently updated first."""
    query = "SELECT * FROM road_disruptions WHERE resolved = 0"
    if closures_only:
        query += " AND is_closure = 1"
    df = database.read_df(query + " ORDER BY is_closure DESC, updated DESC")
    if not df.empty:
        for col in _TS_COLS:
            if col in df.columns:
                df[col] = pd.to_datetime(df[col], format="ISO8601", errors="coerce")
    return df


def latest_counts():
    """Headline counts of active disruptions for KPI cards."""
    df = database.read_df(
        "SELECT is_closure FROM road_disruptions WHERE resolved = 0")
    if df.empty:
        return {"total": 0, "closures": 0, "other": 0}
    closures = int(df["is_closure"].fillna(0).sum())
    return {"total": len(df), "closures": closures, "other": len(df) - closures}


def load_road_timeseries(since=None):
    """Per-cycle KPI history for trend graphs."""
    query = "SELECT * FROM road_timeseries"
    params = []
    if since is not None:
        query += " WHERE timestamp >= ?"
        params.append(since.isoformat(sep=" ", timespec="seconds"))
    df = database.read_df(query + " ORDER BY timestamp", params)
    if not df.empty:
        df["timestamp"] = pd.to_datetime(df["timestamp"], format="ISO8601",
                                         errors="coerce")
    return df


def heartbeat_summary():
    """(cycle_count, last_timestamp) for the roads collector heartbeat."""
    df = database.read_df(
        "SELECT COUNT(*) AS n, MAX(timestamp) AS last FROM road_timeseries")
    if df.empty or not df.iloc[0]["n"]:
        return 0, None
    return int(df.iloc[0]["n"]), df.iloc[0]["last"]


# --------------------------------------------------------------------------- #
# Causes — for "roads cut by the event" views (the flood wall).
# --------------------------------------------------------------------------- #
# eventType/eventSubType are FREE TEXT in the v3 feed (the spec enumerates
# nothing), so a cause is matched by keyword. The type is matched as-is; the
# public description only through an explicit "due to / because of / caused by"
# phrase, because it also carries road names — "Cabbage Tree Road" must not make
# a crash a tree-down. A disruption matching none of these is "other".
CAUSES = [
    ("flooding", "Flooding", r"flood|water over|inundat|wash ?(?:out|away)|washed"),
    ("weather", "Weather / storm", r"weather|storm|wind|snow|ice|icy|hail|fog|"
                                   r"land ?slip|landslide|rain"),
    ("trees", "Trees / debris", r"trees?\b|fallen|vegetation|branch|debris"),
]
CAUSE_OTHER = "other"
CAUSE_LABELS = dict([(k, label) for k, label, _ in CAUSES] + [(CAUSE_OTHER, "Other causes")])
_CAUSE_RE = [(key, re.compile(pattern, re.I))
             for key, _, pattern in CAUSES]
_DUE_TO_RE = re.compile(
    r"(?:due to|because of|caused by|as a result of)\s+((?:[\w'-]+\s+){0,3}[\w'-]+)",
    re.I)


def causes_of(disruption_type, description=None):
    """The cause keys a disruption matches ({'flooding', ...}); {'other'} when
    it matches none."""
    texts = []
    if disruption_type is not None and disruption_type == disruption_type:
        texts.append(str(disruption_type))
    if description is not None and description == description:
        texts += [m.group(1) for m in _DUE_TO_RE.finditer(str(description))]
    found = {key for key, rx in _CAUSE_RE if any(rx.search(t) for t in texts)}
    return found or {CAUSE_OTHER}


def filter_causes(df, causes):
    """Rows matching any of `causes` (keys of CAUSES, plus 'other'). An empty
    or missing selection means no filtering."""
    if df is None or df.empty or not causes:
        return df
    wanted = set(causes)
    desc = df["description"] if "description" in df.columns else [None] * len(df)
    keep = [bool(causes_of(t, d) & wanted)
            for t, d in zip(df["disruption_type"], desc)]
    return df[keep]


def filter_since(df, start, end=None):
    """Rows that STARTED inside [start, end] — the feed's `created`, falling back
    to when we first saw it. `end` None = open-ended (an ongoing event)."""
    if df is None or df.empty or start is None:
        return df
    began = pd.to_datetime(df.get("created"), errors="coerce")
    if "first_seen" in df.columns:
        began = began.fillna(pd.to_datetime(df["first_seen"], errors="coerce"))
    keep = began >= pd.Timestamp(start)
    if end is not None:
        keep &= began <= pd.Timestamp(end)
    return df[keep.fillna(False)]

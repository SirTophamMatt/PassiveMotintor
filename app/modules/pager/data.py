"""Pager queries: the message log, jobs collated by F-number, escalations."""
import json
from datetime import datetime, timedelta

import pandas as pd

from app import database
from app.modules.pager import parse

_TS = "%Y-%m-%d %H:%M:%S"
_COLS = ("id, capcode, alias, sent_at, received_at, message, f_number, "
         "brigade, incident_type, priority, is_escalation, make_json, "
         "required_json, escalation, units_json")


def _since(hours, now=None):
    return ((now or datetime.now()) - timedelta(hours=hours)).strftime(_TS)


def recent_messages(hours=24, escalations_only=False, limit=2000, now=None):
    """The message log, newest first."""
    q = f"SELECT {_COLS} FROM pager_messages WHERE sent_at >= ?"
    if escalations_only:
        q += " AND is_escalation = 1"
    return database.read_df(q + " ORDER BY sent_at DESC, id DESC LIMIT ?",
                            [_since(hours, now), int(limit)])


def job_messages(f_number):
    """Every stored message for one job, oldest first (the job's story)."""
    return database.read_df(
        f"SELECT {_COLS} FROM pager_messages WHERE f_number = ? "
        "ORDER BY sent_at, id", [f_number])


def _loads(value, default):
    if value is None or value != value or value == "":
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def job_units(msgs):
    """Every unit paged to a job across its messages, first-paged first:
    ``(appliances_by_type, brigades, other_codes)``. Appliances are
    ``{type: [codes]}`` merged from the unit lists AND call signs named in
    REQUIRED requests, so a tanker is counted once however it was paged.

    ``brigades`` is every brigade/station involved: each message's own
    ``[XXXX]`` brigade, whole brigades paged (CMTEL -> MTEL) and the home
    brigade/station of every appliance (COROT1 -> CORO, P94 -> FS94)."""
    by_type, brigades, other, seen = {}, [], [], set()

    def add_brigade(b):
        if b and b == b and b not in brigades:       # b == b drops NaN
            brigades.append(b)

    def add_appliance(t, code):
        if code in seen:
            return
        seen.add(code)
        by_type.setdefault(t, []).append(code)

    for _, m in msgs.sort_values(["sent_at", "id"]).iterrows():
        add_brigade(m.get("brigade"))
        for u in _loads(m.get("units_json"), []):
            code = u.get("code")
            if u.get("kind") == "appliance":
                add_appliance(u.get("type") or code, code)
                add_brigade(u.get("brigade"))
            elif u.get("kind") == "brigade":
                add_brigade(u.get("brigade"))
            elif code not in other:
                other.append(code)
        for t, unit in _loads(m.get("required_json"), []):
            add_appliance(t, unit or f"{t} (unnamed)")
            called = parse.classify_unit(unit) if unit else None
            if called and called["kind"] == "appliance":
                add_brigade(called["brigade"])
    return by_type, brigades, other


def summarise_escalation(msgs):
    """Collapse a job's messages into its escalation picture.

    ``make`` is the LATEST make-up figure per appliance type (a job that went
    MAKE TANKERS 5 then MAKE TANKERS 8 stands at 8). ``attached`` is every
    appliance paged to the job, by type (see ``job_units``). ``matched`` holds
    the types with both a make-up request and appliances attached, i.e. where
    the paged appliances can be read against the make-up target."""
    make = {}
    for _, m in msgs.sort_values(["sent_at", "id"]).iterrows():
        for t, n in _loads(m.get("make_json"), {}).items():
            if n is not None or t not in make:
                make[t] = n
    attached = job_units(msgs)[0]
    requested = set()
    for v in msgs["required_json"]:
        requested.update(t for t, _ in _loads(v, []))
    matched = sorted(t for t in make if attached.get(t))
    return {"make": make, "attached": attached, "requested": requested,
            "matched": matched}


def escalation_text(summary):
    parts = []
    for t, n in summary["make"].items():
        paged = len(summary["attached"].get(t, []))
        target = f"MAKE {t}s {n}" if n is not None else f"MAKE {t}s"
        parts.append(f"{target} ({paged} paged)" if paged else target)
    for t in sorted(summary["requested"] - set(summary["make"])):
        parts.append(f"{t} req ×{len(summary['attached'].get(t, []))}")
    return " · ".join(parts)


def appliance_text(by_type):
    """'Pumper ×2 (P94, P62A) · Tanker ×1 (COROT1)'."""
    return " · ".join(f"{t} ×{len(codes)} ({', '.join(codes)})"
                      for t, codes in by_type.items())


def jobs(hours=24, escalated_only=False, now=None):
    """One row per F-number active in the window, most recent activity first.

    A job counts as active when any of its messages falls in the window, and
    then ALL its messages are summarised, so a job that started just before the
    window is not shown with its first page missing."""
    since = _since(hours, now)
    df = database.read_df(
        f"SELECT {_COLS} FROM pager_messages WHERE f_number IN "
        "(SELECT DISTINCT f_number FROM pager_messages "
        " WHERE f_number IS NOT NULL AND sent_at >= ?) ORDER BY sent_at, id",
        [since])
    cols = ["f_number", "first_sent", "last_sent", "messages", "capcodes",
            "brigade", "incident_type", "priority", "escalated", "escalation",
            "make_matched", "appliances", "first_message"]
    if df.empty:
        return pd.DataFrame(columns=cols)
    rows = []
    for fnum, g in df.groupby("f_number", sort=False):
        first = g.iloc[0]

        def first_valid(col):
            s = g[col].dropna()
            s = s[s != ""]
            return s.iloc[0] if not s.empty else None

        escalated = bool((g["is_escalation"] == 1).any())
        summary = summarise_escalation(g) if escalated else None
        by_type, brigades, _ = job_units(g)
        rows.append({
            "f_number": fnum,
            "first_sent": first["sent_at"],
            "last_sent": g["sent_at"].max(),
            "messages": int(g["message"].nunique()),
            "capcodes": int(g["capcode"].nunique()),
            # Every brigade on the job, the one in [..] on the first page first.
            "brigade": ", ".join(brigades),
            "incident_type": first_valid("incident_type"),
            "priority": first_valid("priority"),
            "escalated": escalated,
            "escalation": escalation_text(summary) if summary else "",
            "make_matched": ", ".join(summary["matched"]) if summary else "",
            "appliances": appliance_text(by_type),
            "first_message": first["message"],
        })
    out = pd.DataFrame(rows, columns=cols)
    if escalated_only:
        out = out[out["escalated"]]
    return out.sort_values("last_sent", ascending=False, ignore_index=True)


def counts(hours=24, now=None):
    since = _since(hours, now)
    df = database.read_df(
        "SELECT COUNT(*) AS messages, COUNT(DISTINCT f_number) AS jobs, "
        "COUNT(DISTINCT CASE WHEN is_escalation = 1 THEN f_number END) AS esc_jobs, "
        "SUM(is_escalation) AS esc_messages "
        "FROM pager_messages WHERE sent_at >= ?", [since])
    r = df.iloc[0]
    return {k: int(r[k] or 0) for k in ("messages", "jobs", "esc_jobs",
                                         "esc_messages")}


def escalations_after(last_id):
    """Escalation messages stored after `last_id` (watchdog notifications)."""
    return database.read_df(
        f"SELECT {_COLS} FROM pager_messages WHERE is_escalation = 1 AND id > ? "
        "ORDER BY id", [int(last_id)])


def max_id():
    df = database.read_df("SELECT MAX(id) AS m FROM pager_messages")
    v = df.iloc[0]["m"]
    return int(v) if v == v and v is not None else 0


def heartbeat_summary():
    """(cycle_count, last_timestamp) for the pager collector heartbeat."""
    df = database.read_df(
        "SELECT COUNT(*) AS n, MAX(timestamp) AS last FROM pager_heartbeat")
    if df.empty or not df.iloc[0]["n"]:
        return 0, None
    return int(df.iloc[0]["n"]), df.iloc[0]["last"]

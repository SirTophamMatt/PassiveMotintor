"""CFA pager collector: mazzanet.net.au/cfa/pager-cfa.php.

One GET per cycle (default every 4 minutes, ``pager.interval_minutes``). Every
message on the page is parsed (``parse.py``) and inserted into
``pager_messages`` with INSERT OR IGNORE on ``msg_hash``, so the overlap
between consecutive reads of the page adds nothing. A ``pager_heartbeat`` row
is written every cycle.

Scraping this site is done with the operator's permission. It is kept to one
plain request per cycle with an identifying User-Agent; nothing else on the
site is fetched.

If a non-empty page yields no messages, the page layout has probably changed:
the raw HTML is saved to ``pager_debug.html`` next to the database and the
cycle reports an error, so the problem shows on the Pager page and in /health
rather than looking like a quiet night.
"""
import hashlib
import json
import logging
import os
from datetime import datetime

import requests

from app import database
from app.config import BASE_DIR, load_config
from app.modules.pager import parse

log = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (compatible; Watchdesk-PassiveMonitor/1.0; "
                   "CFA pager log)"),
    "Accept": "text/html,application/xhtml+xml",
}
DEBUG_FILE = os.path.join(BASE_DIR, "pager_debug.html")
_TS = "%Y-%m-%d %H:%M:%S"


def _hash(capcode, sent_at, message, received):
    # A message with no readable time is keyed to the day it was first seen,
    # so the copy still on the page next cycle is recognised as the same one.
    when = sent_at.strftime(_TS) if sent_at else "undated:" + received[:10]
    raw = f"{capcode or ''}|{when}|{message}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def parsed_fields(message):
    """The columns derived from a message's text (everything but its identity
    and times), so a fresh insert and a re-parse can never disagree."""
    info = parse.parse_message(message)
    return {
        "f_number": info["f_number"],
        "brigade": info["brigade"],
        "incident_type": info["incident_type"],
        "priority": info["priority"],
        "is_escalation": int(info["is_escalation"]),
        "make_json": json.dumps(info["make"]) if info["make"] else None,
        "required_json": (json.dumps(info["required"])
                          if info["required"] else None),
        "escalation": parse.escalation_summary(info["make"], info["required"]),
        "units_json": json.dumps(info["units"]) if info["units"] else None,
        "parser_version": parse.PARSER_VERSION,
    }


def build_rows(page_rows, received):
    """Parsed page rows -> pager_messages dicts (pure; no I/O)."""
    out = []
    for r in page_rows:
        sent = r.get("sent_at")
        out.append({
            "msg_hash": _hash(r.get("capcode"), sent, r["message"], received),
            "capcode": r.get("capcode"),
            "alias": r.get("alias"),
            "sent_at": sent.strftime(_TS) if sent else received,
            "received_at": received,
            "message": r["message"],
            **parsed_fields(r["message"]),
        })
    return out


def reparse_stale(limit=5000):
    """Re-parse stored messages written by an older parser version, so a
    parser fix also corrects history. Bounded per call; returns rows updated."""
    df = database.read_df(
        "SELECT id, message FROM pager_messages WHERE parser_version < ? "
        "ORDER BY id LIMIT ?", [parse.PARSER_VERSION, int(limit)])
    if df.empty:
        return 0
    rows = [(i, parsed_fields(m)) for i, m in zip(df["id"], df["message"])]
    cols = list(rows[0][1])
    conn = database.get_connection()
    try:
        conn.executemany(
            "UPDATE pager_messages SET %s WHERE id = ?"
            % ", ".join(f"{c} = ?" for c in cols),
            [[f[c] for c in cols] + [int(i)] for i, f in rows])
        conn.commit()
    finally:
        conn.close()
    log.info("Pager: re-parsed %d stored message(s) with parser v%d",
             len(rows), parse.PARSER_VERSION)
    return len(rows)


def store(rows):
    """Insert rows, skipping ones already stored. Returns (new, new_escalations)."""
    if not rows:
        return 0, 0
    hashes = [r["msg_hash"] for r in rows]
    known = set()
    # Chunked: SQLite caps bound parameters per statement.
    for i in range(0, len(hashes), 500):
        chunk = hashes[i:i + 500]
        df = database.read_df(
            "SELECT msg_hash FROM pager_messages WHERE msg_hash IN (%s)"
            % ",".join("?" * len(chunk)), chunk)
        known.update(df["msg_hash"])
    fresh = [r for r in rows if r["msg_hash"] not in known]
    # De-dup within the batch too (the same row twice on one page).
    seen, unique = set(), []
    for r in fresh:
        if r["msg_hash"] not in seen:
            seen.add(r["msg_hash"])
            unique.append(r)
    database.insert_rows("pager_messages", unique, ignore_duplicates=True)
    return len(unique), sum(r["is_escalation"] for r in unique)


def fetch_pager_data():
    """One collection cycle. Returns the number of new messages stored."""
    cfg = load_config()["pager"]
    try:
        reparse_stale()
    except Exception:
        log.exception("Pager: re-parse of stored messages failed")
    url = (cfg.get("url") or "").strip()
    if not url:
        log.info("Pager: no URL configured — skipping")
        return 0
    resp = requests.get(url, headers=HEADERS,
                        timeout=cfg.get("timeout_seconds", 30))
    resp.raise_for_status()
    now = datetime.now()
    received = now.strftime(_TS)
    page_rows = parse.parse_page(resp.content, now=now)
    rows = build_rows(page_rows, received)
    new, new_esc = store(rows)
    database.insert_rows("pager_heartbeat", [{
        "timestamp": received, "rows_seen": len(rows), "new_rows": new,
        "new_escalations": new_esc}])
    log.info("Pager: %d on page, %d new (%d escalation)", len(rows), new, new_esc)
    if not rows and resp.content.strip():
        try:
            with open(DEBUG_FILE, "wb") as fh:
                fh.write(resp.content)
        except OSError:
            log.exception("Pager: could not save debug page")
        raise RuntimeError(
            "Pager page returned no messages the parser recognises — layout "
            f"may have changed. Raw page saved to {DEBUG_FILE}.")
    return new

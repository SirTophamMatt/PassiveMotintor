# Passive Monitor — Copyright (c) 2026 SirTophamMatt. All rights reserved.
"""FDR / TFB history in the shared SQLite database.

Change-only revisions: a rating or TFB row is written only when what the
source PUBLISHES changes. Re-reading identical content moves ``last_seen`` on
the current row (proof the value was still current then) and adds nothing, so
the table grows with amendments, not with the poll rate.

Two rules protect good data:
* an unrecognised value never supersedes a recognised one, and an UNKNOWN TFB
  never supersedes a YES/NO — a garbled or partial feed raises a flag instead;
* nothing is ever deleted. Superseded rows keep ``is_current = 0``; manual
  corrections live in ``fdr_overrides`` on top of the source rows.
"""
import hashlib
import json
import logging

from app import database
from app.modules.fdr import model

log = logging.getLogger(__name__)


def _hash(*parts):
    return hashlib.sha1(json.dumps(parts, sort_keys=True).encode()).hexdigest()


def connect():
    conn = database.get_connection()
    conn.row_factory = None
    return conn


# --------------------------------------------------------------------------- #
# Runs + raw bodies
# --------------------------------------------------------------------------- #
def start_run(trigger):
    conn = connect()
    try:
        cur = conn.execute(
            "INSERT INTO fdr_runs (started_at, trigger, status) VALUES (?, ?, 'running')",
            [model.iso(model.now()), trigger])
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def finish_run(run_id, status, counts, errors):
    database.execute(
        "UPDATE fdr_runs SET completed_at = ?, status = ?, records_retrieved = ?, "
        "records_inserted = ?, records_updated = ?, records_unchanged = ?, errors = ? "
        "WHERE run_id = ?",
        [model.iso(model.now()), status, counts.get("retrieved", 0),
         counts.get("inserted", 0), counts.get("updated", 0),
         counts.get("unchanged", 0), json.dumps(errors) if errors else None, run_id])


def save_raw(conn, source, url, body, when):
    sha = hashlib.sha1(body).hexdigest()
    row = conn.execute("SELECT id FROM fdr_raw WHERE sha1 = ?", [sha]).fetchone()
    if row:
        conn.execute("UPDATE fdr_raw SET last_fetched_at = ? WHERE id = ?",
                     [model.iso(when), row[0]])
        return row[0]
    cur = conn.execute(
        "INSERT INTO fdr_raw (sha1, source, url, first_fetched_at, last_fetched_at, body) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [sha, source, url, model.iso(when), model.iso(when), body])
    return cur.lastrowid


# --------------------------------------------------------------------------- #
# Revisions
# --------------------------------------------------------------------------- #
def record_rating(conn, valid_date, district, source, dr, *, issued, when,
                  raw_id=None, run_id=None, url=None):
    """Store one district's rating for a day. Returns
    'inserted' | 'updated' | 'unchanged' | 'kept' (unrecognised value not
    allowed to replace a recognised one)."""
    status = "ok" if dr.recognised and dr.ratings else "unrecognised"
    ratings = list(dr.ratings)
    h = _hash(ratings, status)
    vd = valid_date.isoformat()
    cur = conn.execute(
        "SELECT forecast_id, revision, content_hash, status FROM fdr_forecasts "
        "WHERE valid_date = ? AND district = ? AND source = ? AND is_current = 1",
        [vd, district, source]).fetchone()
    now_iso, now_epoch = model.iso(when), int(when.timestamp())
    if cur and cur[2] == h:
        conn.execute(
            "UPDATE fdr_forecasts SET last_seen_at = ?, last_seen_epoch = ?, "
            "seen_count = seen_count + 1 WHERE forecast_id = ?",
            [now_iso, now_epoch, cur[0]])
        return "unchanged"
    if cur and status != "ok" and cur[3] == "ok":
        add_flag(conn, vd, district, "unrecognised",
                 f"{source} published {dr.raw!r}; the earlier recognised rating "
                 "is kept", when)
        return "kept"
    revision = 1
    if cur:
        conn.execute("UPDATE fdr_forecasts SET is_current = 0, superseded_at = ? "
                     "WHERE forecast_id = ?", [now_iso, cur[0]])
        revision = cur[1] + 1
    c = conn.execute(
        "INSERT INTO fdr_forecasts (valid_date, district, source, revision, status, "
        "ratings_json, rating, rating_rank, multi, raw_text, issue_ts, retrieved_at, "
        "retrieved_epoch, last_seen_at, last_seen_epoch, source_url, raw_id, run_id, "
        "is_current, content_hash) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?)",
        [vd, district, source, revision, status, json.dumps(ratings),
         model.display(ratings) if ratings else None, model.rank_of(ratings),
         int(len(ratings) > 1), dr.raw, model.iso(issued), now_iso, now_epoch,
         now_iso, now_epoch, url, raw_id, run_id, h])
    if len(ratings) > 1:
        for r in ratings:
            conn.execute(
                "INSERT OR IGNORE INTO fdr_rating_details (forecast_id, valid_date, "
                "district, rating, applicable_area, applicable_period, source, basis) "
                "VALUES (?,?,?,?,?,?,?, 'multi-value')",
                [c.lastrowid, vd, district, r, None, None, source])
    if status != "ok":
        add_flag(conn, vd, district, "unrecognised",
                 f"{source} published {dr.raw!r} — not a recognised AFDRS rating", when)
    return "updated" if cur else "inserted"


def record_tfb(conn, valid_date, district, source, ts, *, issued, when,
               raw_id=None, run_id=None):
    vd = valid_date.isoformat()
    cur = conn.execute(
        "SELECT id, revision, declaration_status FROM fdr_tfb WHERE valid_date = ? "
        "AND district = ? AND source = ? AND is_current = 1",
        [vd, district, source]).fetchone()
    now_iso, now_epoch = model.iso(when), int(when.timestamp())
    if cur and cur[2] == ts.status:
        conn.execute("UPDATE fdr_tfb SET last_seen_at = ?, last_seen_epoch = ?, "
                     "seen_count = seen_count + 1 WHERE id = ?",
                     [now_iso, now_epoch, cur[0]])
        return "unchanged"
    if cur and ts.status == model.TFB_UNKNOWN and cur[2] != model.TFB_UNKNOWN:
        add_flag(conn, vd, district, "tfb-unclear",
                 f"{source}: {ts.raw!r}; the earlier {cur[2]} is kept", when)
        return "kept"
    revision = 1
    if cur:
        conn.execute("UPDATE fdr_tfb SET is_current = 0, superseded_at = ? WHERE id = ?",
                     [now_iso, cur[0]])
        revision = cur[1] + 1
    conn.execute(
        "INSERT INTO fdr_tfb (valid_date, district, source, revision, declaration_status, "
        "basis, raw_text, issue_ts, retrieved_at, retrieved_epoch, last_seen_at, "
        "last_seen_epoch, raw_id, run_id, is_current) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)",
        [vd, district, source, revision, ts.status, ts.basis, ts.raw,
         model.iso(issued), now_iso, now_epoch, now_iso, now_epoch, raw_id, run_id])
    if cur and cur[2] != ts.status:
        add_flag(conn, vd, district, "tfb-changed",
                 f"{source}: Total Fire Ban changed {cur[2]} -> {ts.status}", when)
    return "updated" if cur else "inserted"


# --------------------------------------------------------------------------- #
# Flags
# --------------------------------------------------------------------------- #
def add_flag(conn, valid_date, district, kind, detail, when=None):
    own = conn is None
    conn = conn or connect()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO fdr_flags (valid_date, district, kind, detail, "
            "created_at) VALUES (?,?,?,?,?)",
            # "" not NULL: UNIQUE treats NULLs as distinct, so an undated flag
            # would otherwise be raised again on every run.
            [valid_date or "", district or "", kind, detail,
             model.iso(when or model.now())])
        if own:
            conn.commit()
    finally:
        if own:
            conn.close()


def resolve_flag(flag_id):
    return database.execute(
        "UPDATE fdr_flags SET resolved_at = ? WHERE id = ? AND resolved_at IS NULL",
        [model.iso(model.now()), int(flag_id)])


def open_flags(limit=200):
    return database.read_df(
        "SELECT id, valid_date, district, kind, detail, created_at FROM fdr_flags "
        "WHERE resolved_at IS NULL ORDER BY valid_date DESC, id DESC LIMIT ?", [limit])


# --------------------------------------------------------------------------- #
# Overrides
# --------------------------------------------------------------------------- #
OVERRIDE_FIELDS = ("rating", "tfb", "multi", "comment")


def validate_override(valid_date, district, field, value):
    """(normalised value, error). Raises nothing."""
    if field not in OVERRIDE_FIELDS:
        return None, f"unknown field {field!r}"
    try:
        model.as_date(valid_date)
    except ValueError:
        return None, f"not a date: {valid_date!r}"
    if field in ("rating", "tfb") and district not in model.DISTRICTS:
        return None, f"{field} needs one of the nine districts"
    if field in ("multi", "comment") and district != "*":
        return None, f"{field} applies to the whole day (district '*')"
    v = (value or "").strip()
    if field == "rating":
        ratings, ok = model.parse_ratings(v)
        if not ok:
            return None, f"{v!r} is not an AFDRS rating (or ratings joined by /)"
        return json.dumps(ratings), None
    if field in ("tfb", "multi"):
        u = v.upper()
        if u in ("YES", "Y", "TRUE"):
            return "YES", None
        if u in ("NO", "N", "FALSE"):
            return "NO", None
        return None, f"{field} must be YES or NO"
    return v[:500], None


def add_override(valid_date, district, field, value, reason, entered_by=None):
    """Returns (ok, message)."""
    if not (reason or "").strip():
        return False, "A reason is required for every correction."
    norm, err = validate_override(valid_date, district, field, value)
    if err:
        return False, err
    vd = model.as_date(valid_date).isoformat()
    conn = connect()
    try:
        now = model.iso(model.now())
        # One active override per (date, district, field): a new one replaces it.
        conn.execute("UPDATE fdr_overrides SET active = 0, revoked_at = ? WHERE "
                     "valid_date = ? AND district = ? AND field = ? AND active = 1",
                     [now, vd, district, field])
        conn.execute(
            "INSERT INTO fdr_overrides (valid_date, district, field, value, reason, "
            "entered_by, entered_at) VALUES (?,?,?,?,?,?,?)",
            [vd, district, field, norm, reason.strip()[:500], entered_by, now])
        conn.commit()
    finally:
        conn.close()
    return True, f"Correction saved for {vd} {district} {field}."


def revoke_override(override_id):
    return database.execute(
        "UPDATE fdr_overrides SET active = 0, revoked_at = ? WHERE id = ? AND active = 1",
        [model.iso(model.now()), int(override_id)])


def overrides(start=None, end=None, active_only=True):
    sql = "SELECT * FROM fdr_overrides WHERE 1=1"
    params = []
    if active_only:
        sql += " AND active = 1"
    if start:
        sql += " AND valid_date >= ?"
        params.append(model.as_date(start).isoformat())
    if end:
        sql += " AND valid_date <= ?"
        params.append(model.as_date(end).isoformat())
    return database.read_df(sql + " ORDER BY valid_date DESC, id DESC", params)


# --------------------------------------------------------------------------- #
# Reads
# --------------------------------------------------------------------------- #
def forecasts(start, end, current_only=False):
    sql = ("SELECT * FROM fdr_forecasts WHERE valid_date BETWEEN ? AND ?" +
           (" AND is_current = 1" if current_only else "") +
           " ORDER BY valid_date, district, source, revision")
    return database.read_df(sql, [model.as_date(start).isoformat(),
                                  model.as_date(end).isoformat()])


def tfbs(start, end, current_only=False):
    sql = ("SELECT * FROM fdr_tfb WHERE valid_date BETWEEN ? AND ?" +
           (" AND is_current = 1" if current_only else "") +
           " ORDER BY valid_date, district, source, revision")
    return database.read_df(sql, [model.as_date(start).isoformat(),
                                  model.as_date(end).isoformat()])


def runs(limit=20):
    return database.read_df(
        "SELECT * FROM fdr_runs ORDER BY run_id DESC LIMIT ?", [limit])


def last_run():
    df = runs(1)
    return None if df.empty else df.iloc[0].to_dict()


def last_success():
    df = database.read_df(
        "SELECT completed_at FROM fdr_runs WHERE status IN ('ok', 'partial') "
        "AND trigger != 'workbook' ORDER BY run_id DESC LIMIT 1")
    return None if df.empty else df.iloc[0]["completed_at"]


def record_cell_updates(rows):
    return database.insert_rows("fdr_workbook_updates", rows)


def last_written(workbook_name, worksheet, cell):
    """The value this tool last wrote to a cell (not a dry run), or None."""
    df = database.read_df(
        "SELECT new_value FROM fdr_workbook_updates WHERE workbook_name = ? AND "
        "worksheet = ? AND cell_reference = ? AND dry_run = 0 "
        "ORDER BY update_id DESC LIMIT 1", [workbook_name, worksheet, cell])
    return None if df.empty else df.iloc[0]["new_value"]


def written_cells(workbook_name):
    """{(sheet, cell): last value written} for one workbook name."""
    df = database.read_df(
        "SELECT worksheet, cell_reference, new_value FROM fdr_workbook_updates "
        "WHERE workbook_name = ? AND dry_run = 0 ORDER BY update_id",
        [workbook_name])
    return {(r.worksheet, r.cell_reference): r.new_value for r in df.itertuples()}

"""Event tags: named date ranges applied over the continuously-collected data.

Collection now runs always-on (see app.collector). An "event" is no longer a
collection-time label; it is a tag — a name plus a start/end datetime — that
selects a slice of the flood and power data by timestamp. Tags are used to
filter the Flood view and to scope exports and reports.

An open-ended tag (end_ts NULL) means "ongoing"; its range extends to now.
"""
import logging
from datetime import datetime

from app import database

log = logging.getLogger(__name__)

_FMT = "%Y-%m-%d %H:%M:%S"


def _now_str():
    return datetime.now().strftime(_FMT)


def list_tags():
    """All tags, most recent first, as a list of dicts."""
    df = database.read_df(
        "SELECT id, name, start_ts, end_ts, notes, created_at "
        "FROM event_tags ORDER BY start_ts DESC, id DESC")
    return df.to_dict("records")


def get_tag(tag_id):
    df = database.read_df("SELECT * FROM event_tags WHERE id = ?", [tag_id])
    return None if df.empty else df.iloc[0].to_dict()


def create_tag(name, start_ts, end_ts=None, notes=None):
    """Create a tag. start_ts/end_ts are 'YYYY-MM-DD HH:MM:SS' strings (or
    date-only 'YYYY-MM-DD', normalised to whole-day bounds)."""
    name = (name or "").strip()
    if not name:
        raise ValueError("Tag name is required.")
    start_ts = _normalise(start_ts, end_of_day=False)
    end_ts = _normalise(end_ts, end_of_day=True) if end_ts else None
    if not start_ts:
        raise ValueError("A valid start date is required.")
    if end_ts and end_ts < start_ts:
        raise ValueError("End must be after start.")
    database.insert_rows("event_tags", [{
        "name": name, "start_ts": start_ts, "end_ts": end_ts,
        "notes": (notes or "").strip() or None, "created_at": _now_str(),
    }])
    log.info("Created tag '%s' (%s -> %s)", name, start_ts, end_ts or "ongoing")


def delete_tag(tag_id):
    database.execute("DELETE FROM event_tags WHERE id = ?", [tag_id])


def resolve_range(tag):
    """Return (start_ts, end_ts) strings for a tag dict, end defaulting to now
    for an ongoing tag."""
    start = tag["start_ts"]
    end = tag.get("end_ts") or _now_str()
    return start, end


def _normalise(value, end_of_day):
    """Accept a date ('2026-02-01') or datetime string and return a full
    'YYYY-MM-DD HH:MM:SS'. Date-only values expand to 00:00:00 (start) or
    23:59:59 (end)."""
    if not value:
        return None
    value = str(value).strip()
    for fmt in (_FMT, "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(value, fmt)
            if fmt == "%Y-%m-%d" and end_of_day:
                dt = dt.replace(hour=23, minute=59, second=59)
            return dt.strftime(_FMT)
        except ValueError:
            continue
    return None

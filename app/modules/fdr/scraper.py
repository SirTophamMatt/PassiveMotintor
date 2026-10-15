# Passive Monitor — Copyright (c) 2026 SirTophamMatt. All rights reserved.
"""One FDR/TFB collection run: fetch -> parse -> validate -> store -> flag.

Sources, in priority order (``resolve.SOURCE_PRIORITY``):

1. ``cfa-state`` — CFA's statewide "Total Fire Bans and Fire Danger Ratings"
   RSS (all nine districts, today + 3 days). Primary.
2. ``cfa-district`` — the nine per-district CFA feeds. OFF by default
   (``fdr.district_feeds``): all nine URLs answer (checked 15 Oct 2026), but
   it is nine more requests per run for what is normally the same information.
   When on, each is a cross-check; a disagreement with the state feed is
   flagged, never silently resolved.

A failed fetch or an unreadable body changes nothing already stored — the run
is recorded as failed/partial with the reason and the previous values stand.
"""
import json
import logging
import os

import requests

from app.config import BASE_DIR, load_config
from app.modules.fdr import model, parse, store

log = logging.getLogger(__name__)

STATE = "cfa-state"
DISTRICT = "cfa-district"
SLUGS = {
    "Mallee": "mallee", "Wimmera": "wimmera", "Northern Country": "northerncountry",
    "North Central": "northcentral", "North East": "northeast",
    "East Gippsland": "eastgippsland",
    "West and South Gippsland": "westandsouthgippsland", "Central": "central",
    "South West": "southwest",
}
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (compatible; Watchdesk-PassiveMonitor/1.0; "
                   "FDR statistics)"),
    "Accept": "application/rss+xml, application/xml, text/xml;q=0.9, */*;q=0.5",
}
DEBUG_DIR = BASE_DIR


def sources(cfg=None):
    """[(source, url, district_hint)] for this run."""
    cfg = (cfg or load_config())["fdr"]
    out = [(STATE, cfg["state_feed_url"], None)]
    if cfg.get("district_feeds"):
        for d in model.DISTRICTS:
            out.append((DISTRICT, cfg["district_feed_url"].format(slug=SLUGS[d]), d))
    return out


def http_fetch(url, timeout):
    resp = requests.get(url, headers=HEADERS, timeout=timeout)
    resp.raise_for_status()
    return resp.content


def _save_debug(source, body):
    path = os.path.join(DEBUG_DIR, f"fdr_debug_{source}.xml")
    try:
        with open(path, "wb") as fh:
            fh.write(body)
    except OSError:
        log.exception("FDR: could not save debug body")
    return path


def _store_feed(conn, source, url, hint, body, result, when, run_id, counts):
    raw_id = store.save_raw(conn, source, url, body, when)
    for day in result.days:
        expected = [hint] if hint else model.DISTRICTS
        for d in expected:
            dr = None if day.unissued else day.ratings.get(d)
            if dr is None and day.unissued:
                pass        # placeholder day: no forecast yet, nothing to store
            elif dr is None:
                store.add_flag(conn, day.valid_date.isoformat(), d, "missing",
                               f"{source}: no rating published for {d}", when)
                continue
            else:
                counts["retrieved"] += 1
                counts[_bucket(store.record_rating(
                    conn, day.valid_date, d, source, dr, issued=day.issued, when=when,
                    raw_id=raw_id, run_id=run_id, url=url))] += 1
            ts = day.tfb.get(d)
            if ts is not None:
                store.record_tfb(conn, day.valid_date, d, source, ts,
                                 issued=day.issued, when=when, raw_id=raw_id,
                                 run_id=run_id)
            elif day.valid_date <= when.date():
                # Future days often carry no TFB decision yet; that is normal.
                # For today it is worth a look — but it is UNKNOWN, never NO.
                store.add_flag(conn, day.valid_date.isoformat(), d, "tfb-missing",
                               f"{source}: no Total Fire Ban status for {d}", when)
        for p in day.problems:
            store.add_flag(conn, day.valid_date.isoformat(), hint, "feed-format",
                           f"{source}: {p}", when)


def _bucket(outcome):
    return {"inserted": "inserted", "updated": "updated"}.get(outcome, "unchanged")


def run(trigger="collector", fetcher=None, cfg=None, when=None):
    """Collect once. Returns a summary dict (also stored as an fdr_runs row).

    ``fetcher(url, timeout) -> bytes`` is injectable for tests and for the
    CLI's --from-file option."""
    cfg = cfg or load_config()
    fetcher = fetcher or http_fetch
    when = when or model.now()
    run_id = store.start_run(trigger)
    counts = {"retrieved": 0, "inserted": 0, "updated": 0, "unchanged": 0}
    errors, ok_sources, srcs = [], 0, sources(cfg)
    timeout = cfg["fdr"].get("timeout_seconds", 30)
    for source, url, hint in srcs:
        label = f"{source}{':' + hint if hint else ''}"
        try:
            body = fetcher(url, timeout)
        except Exception as e:  # network, HTTP status, timeout
            errors.append(f"{label}: fetch failed — {e}")
            log.warning("FDR: %s fetch failed: %s", label, e)
            continue
        result = parse.parse_feed(body, district_hint=hint, fetched_at=when)
        rated = sum(len(d.ratings) for d in result.days)
        if not rated:
            path = _save_debug(source if not hint else f"{source}_{SLUGS[hint]}", body)
            errors.append(f"{label}: no ratings recognised in the feed "
                          f"({result.item_count} item(s)); raw body saved to {path}. "
                          + "; ".join(result.problems[:3]))
            continue
        conn = store.connect()
        try:
            _store_feed(conn, source, url, hint, body, result, when, run_id, counts)
            for p in result.problems:
                store.add_flag(conn, None, hint, "feed-format", f"{label}: {p}", when)
            conn.commit()
            ok_sources += 1
        except Exception as e:
            conn.rollback()
            errors.append(f"{label}: could not store — {e}")
            log.exception("FDR: storing %s failed", label)
        finally:
            conn.close()
    try:
        from app.modules.fdr import resolve
        resolve.flag_conflicts(when)
    except Exception:
        log.exception("FDR: conflict check failed")
    status = ("ok" if ok_sources == len(srcs) and not errors else
              "partial" if ok_sources else "failed")
    store.finish_run(run_id, status, counts, errors)
    summary = {"run_id": run_id, "status": status, **counts, "errors": errors}
    log.info("FDR run %s (%s): %s", run_id, trigger, json.dumps(summary))
    if cfg["fdr"].get("auto_update_workbook") and cfg["fdr"].get("workbook_path") \
            and ok_sources:
        try:
            from app.modules.fdr import workbook
            res = workbook.update_file(cfg["fdr"]["workbook_path"], run_id=run_id,
                                       cfg=cfg)
            summary["workbook"] = res.summary()
        except Exception as e:
            log.exception("FDR: workbook update failed")
            summary["workbook"] = {"ok": False, "error": str(e)}
    return summary


def collect():
    """Collector entry point: raise on a failed run so the status shows it."""
    summary = run("collector")
    if summary["status"] == "failed":
        raise RuntimeError("; ".join(summary["errors"]) or "FDR run failed")
    return summary

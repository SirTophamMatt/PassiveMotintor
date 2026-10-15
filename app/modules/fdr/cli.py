# Passive Monitor — Copyright (c) 2026 SirTophamMatt. All rights reserved.
"""Command line for the FDR tool — scheduled (Task Scheduler / cron) and manual.

    python -m app.modules.fdr status
    python -m app.modules.fdr fetch
    python -m app.modules.fdr update-excel --workbook "FDR ... 2026-27.xlsx" [--dry-run]
    python -m app.modules.fdr run --workbook PATH [--dry-run]
    python -m app.modules.fdr backfill --start 2026-07-01 --end 2026-10-15 --workbook PATH
    python -m app.modules.fdr validate --workbook PATH
    python -m app.modules.fdr inspect --workbook PATH
    python -m app.modules.fdr show --date 2026-10-16
    python -m app.modules.fdr override add --date 2026-10-16 --district Central \
            --field rating --value "High/Extreme" --reason "CFA amendment 13:40"
    python -m app.modules.fdr override list | revoke --id 3
    python -m app.modules.fdr flags [--resolve ID]
    python -m app.modules.fdr export --start 2026-07-01 --end 2027-06-30 --out fdr.csv

Data lives where the web app keeps it (``UM_DATA_DIR``, else next to the app),
in the same ``unified_monitor.db``. Exit codes: 0 ok · 1 failed · 2 completed
but something needs a person · 3 another run holds the lock.
"""
import argparse
import csv
import json
import logging
import logging.handlers
import os
import sys
import time
from datetime import timedelta

from app import database
from app.config import BASE_DIR, load_config
from app.modules.fdr import model, resolve, scraper, store, workbook

log = logging.getLogger("fdr")
LOCK_FILE = os.path.join(BASE_DIR, "fdr.lock")
LOG_FILE = os.path.join(BASE_DIR, "fdr.log")
STALE_LOCK_SECONDS = 30 * 60


def _setup_logging(verbose):
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    fh = logging.handlers.RotatingFileHandler(LOG_FILE, maxBytes=2_000_000,
                                              backupCount=5, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(fmt)
    sh.setLevel(logging.WARNING if not verbose else logging.DEBUG)
    root.addHandler(sh)


class Lock:
    """One run at a time (Task Scheduler + a manual run, two cron entries).

    A lock older than STALE_LOCK_SECONDS is assumed abandoned (a killed run)."""

    def __enter__(self):
        for _ in range(2):
            try:
                fd = os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, f"{os.getpid()} {time.time()}".encode())
                os.close(fd)
                return self
            except FileExistsError:
                try:
                    age = time.time() - os.path.getmtime(LOCK_FILE)
                except OSError:
                    continue
                if age > STALE_LOCK_SECONDS:
                    log.warning("Removing stale lock (%.0f min old)", age / 60)
                    try:
                        os.remove(LOCK_FILE)
                    except OSError:
                        pass
                    continue
                raise SystemExit(_out(f"Another FDR run is in progress ({LOCK_FILE}).", 3))
        raise SystemExit(_out("Could not take the run lock.", 3))

    def __exit__(self, *exc):
        try:
            os.remove(LOCK_FILE)
        except OSError:
            pass


def _out(msg, code=None):
    print(msg)
    return code


def _workbook_arg(args, cfg):
    path = getattr(args, "workbook", None) or cfg["fdr"].get("workbook_path")
    if not path:
        raise SystemExit(_out("No workbook given (--workbook or config "
                              "fdr.workbook_path).", 1))
    return path


def _print_result(res):
    if res.layout:
        for line in res.layout.describe():
            print("  " + line)
    if res.error:
        print(f"WORKBOOK NOT UPDATED: {res.error}")
        return 1
    p = res.plan
    verb = "Would write" if res.dry_run else "Wrote"
    print(f"{verb} {len(p.changes)} cell(s); {len(p.blocked)} left alone; "
          f"{len(p.review)} need review.")
    for c in p.changes:
        print(f"  {c.cell:>6} {c.valid_date}  {c.old!r} -> {c.new!r}  ({c.reason})")
    for cell, d, old, new, why in p.blocked:
        print(f"  LEFT {cell:>6} {d}  {old!r} (would be {new!r}): {why}")
    for d, note in p.review:
        print(f"  REVIEW {d}: {note}")
    if p.no_data_summary():
        print("  " + p.no_data_summary())
    if res.backup:
        print(f"Backup of the original: {res.backup}")
    return 2 if (p.blocked or p.review) else 0


def cmd_fetch(args, cfg):
    fetcher = None
    if args.from_file:
        # Offline: parse a saved copy of the feed (e.g. downloaded in a
        # browser). Read inside the fetcher so a missing file is recorded as
        # a failed fetch, exactly like an unreachable source.
        def fetcher(url, timeout):
            with open(args.from_file, "rb") as fh:
                return fh.read()
    s = scraper.run("cli", fetcher=fetcher, cfg=cfg)
    print(f"Run {s['run_id']}: {s['status']} — {s['retrieved']} values read, "
          f"{s['inserted']} new, {s['updated']} amended, {s['unchanged']} unchanged.")
    for e in s["errors"]:
        print("  ERROR " + e)
    return {"ok": 0, "partial": 2}.get(s["status"], 1)


def cmd_update(args, cfg, start=None, end=None):
    path = _workbook_arg(args, cfg)
    res = workbook.update_file(path, cfg=cfg, dry_run=args.dry_run, force=args.force,
                               start=start or args.start, end=end or args.end)
    return _print_result(res)


def cmd_run(args, cfg):
    code = cmd_fetch(args, cfg)
    if code == 1:
        print("Fetch failed — the workbook is still updated from what is stored.")
    wcode = cmd_update(args, cfg)
    return max(code, wcode)


def cmd_backfill(args, cfg):
    print("Backfill uses ONLY values already stored by this tool (or entered as "
          "corrections). CFA publishes no archive, so dates before collection "
          "started stay blank — nothing is reconstructed.")
    days = resolve.resolve_range(args.start, args.end)
    have = sum(1 for r in days.values() if any(d.ratings for d in r.districts.values()))
    print(f"{have} of {len(days)} day(s) in range have stored data.")
    return cmd_update(args, cfg, args.start, args.end)


def cmd_validate(args, cfg):
    code = 0
    # Database
    df = database.read_df(
        "SELECT valid_date, district, source, COUNT(*) n FROM fdr_forecasts "
        "WHERE is_current = 1 GROUP BY valid_date, district, source HAVING n > 1")
    if not df.empty:
        print(f"DB: {len(df)} key(s) with more than one current revision")
        code = 1
    bad = database.read_df(
        "SELECT COUNT(*) n FROM fdr_forecasts WHERE district NOT IN (%s)"
        % ",".join("?" * len(model.DISTRICTS)), model.DISTRICTS).iloc[0]["n"]
    if bad:
        print(f"DB: {bad} row(s) with an unknown district")
        code = 1
    print("DB: OK" if code == 0 else "DB: problems found")
    path = getattr(args, "workbook", None) or cfg["fdr"].get("workbook_path")
    if path:
        res = workbook.update_file(path, cfg=cfg, dry_run=True)
        code = max(code, _print_result(res))
    return code


def cmd_inspect(args, cfg):
    lay = workbook.inspect(_workbook_arg(args, cfg), cfg)
    for line in lay.describe():
        print(line)
    for col, vals in sorted(lay.allowed.items()):
        print(f"Dropdown {col}: {', '.join(vals)}")
    print(f"Formula cells in target columns: {len(lay.formulas)}; "
          f"formulas in workbook: {lay.formula_count}")
    return 0


def cmd_status(args, cfg):
    last = store.last_run()
    print(f"Database: {database.DB_FILE}")
    print(f"Last run: {json.dumps(last, default=str) if last else 'never'}")
    print(f"Last successful read: {store.last_success() or 'never'}")
    t = model.today()
    for d, rec in resolve.resolve_range(t, t + timedelta(days=3)).items():
        cells = ", ".join(f"{x.district}={x.label or '—'}{'*' if x.state != 'confirmed' else ''}"
                          for x in rec.districts.values())
        print(f"{d} TFB={rec.tfb_any or '?'} Multi={rec.multi or '?'}: {cells}")
    print("(* = not yet confirmed for the day)")
    print(f"Open review flags: {len(store.open_flags())}")
    return 0


def cmd_show(args, cfg):
    rec = resolve.resolve_day(args.date)
    print(f"{rec.date} ({rec.stage}) TFB={rec.tfb_any or '?'} "
          f"{rec.tfb_districts or ''} Multi={rec.multi or '?'} ({rec.multi_source})")
    for x in rec.districts.values():
        print(f"  {x.district:<26} {x.label or '—':<14} {x.state:<12} "
              f"TFB={x.tfb or '?'} rev={x.revisions} {x.multi_detail or ''} "
              f"{'; '.join(x.notes)}")
    for n in rec.review:
        print("  REVIEW " + n)
    if rec.comment():
        print("  Comment: " + rec.comment())
    return 0


def cmd_override(args, cfg):
    if args.action == "add":
        ok, msg = store.add_override(args.date, args.district, args.field, args.value,
                                     args.reason, entered_by=args.by or os.environ.get(
                                         "USERNAME") or os.environ.get("USER"))
        print(msg)
        return 0 if ok else 1
    if args.action == "revoke":
        n = store.revoke_override(args.id)
        print("Revoked." if n else "No active correction with that id.")
        return 0 if n else 1
    df = store.overrides(active_only=not args.all)
    print(df.to_string(index=False) if not df.empty else "No corrections.")
    return 0


def cmd_flags(args, cfg):
    if args.resolve:
        store.resolve_flag(args.resolve)
    df = store.open_flags()
    print(df.to_string(index=False) if not df.empty else "Nothing needs review.")
    return 0


def cmd_export(args, cfg):
    """One row per date x district — for Power BI / seasonal statistics."""
    days = resolve.resolve_range(args.start, args.end)
    with open(args.out, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["date", "season", "district", "rating", "highest_rating",
                    "rating_rank", "state", "multi", "multi_detail", "tfb",
                    "tfb_state", "source", "revisions"])
        for d, rec in sorted(days.items()):
            for x in rec.districts.values():
                top = model.highest(x.ratings)
                w.writerow([d.isoformat(), model.season_of(d), x.district, x.label,
                            top or "", model.RANK.get(top, ""), x.state,
                            int(x.multi), x.multi_detail or "", x.tfb or "",
                            x.tfb_state, x.source or "", x.revisions])
    print(f"Wrote {args.out}")
    return 0


def build_parser():
    p = argparse.ArgumentParser(prog="python -m app.modules.fdr",
                                description="Victorian Fire Danger Ratings + TFB")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    def wb(sp, dates=True):
        sp.add_argument("--workbook", help="path to the FDR statistics workbook")
        sp.add_argument("--dry-run", action="store_true",
                        help="show what would change; write nothing")
        sp.add_argument("--force", action="store_true",
                        help="also overwrite values this tool did not write")
        if dates:
            sp.add_argument("--start")
            sp.add_argument("--end")

    f = sub.add_parser("fetch", help="collect from CFA now")
    f.add_argument("--from-file", help="parse a saved copy of the feed instead")
    wb(sub.add_parser("update-excel", help="write stored values into the workbook"))
    r = sub.add_parser("run", help="fetch, then update the workbook")
    r.add_argument("--from-file")
    wb(r)
    b = sub.add_parser("backfill", help="re-apply STORED values for a date range")
    wb(b, dates=False)
    b.add_argument("--start", required=True)
    b.add_argument("--end", required=True)
    v = sub.add_parser("validate", help="check the database and the workbook")
    v.add_argument("--workbook")
    i = sub.add_parser("inspect", help="show how the workbook was understood")
    i.add_argument("--workbook")
    sub.add_parser("status", help="last run and the current forecast")
    s = sub.add_parser("show", help="one day's resolved record")
    s.add_argument("--date", required=True)
    o = sub.add_parser("override", help="manual corrections")
    o.add_argument("action", choices=["add", "list", "revoke"])
    o.add_argument("--date")
    o.add_argument("--district", default="*")
    o.add_argument("--field", choices=store.OVERRIDE_FIELDS)
    o.add_argument("--value")
    o.add_argument("--reason")
    o.add_argument("--by")
    o.add_argument("--id", type=int)
    o.add_argument("--all", action="store_true")
    fl = sub.add_parser("flags", help="list (or resolve) review flags")
    fl.add_argument("--resolve", type=int)
    e = sub.add_parser("export", help="CSV of resolved records (Power BI etc.)")
    e.add_argument("--start", required=True)
    e.add_argument("--end", required=True)
    e.add_argument("--out", required=True)
    return p


COMMANDS = {"fetch": cmd_fetch, "update-excel": cmd_update, "run": cmd_run,
            "backfill": cmd_backfill, "validate": cmd_validate, "inspect": cmd_inspect,
            "status": cmd_status, "show": cmd_show, "override": cmd_override,
            "flags": cmd_flags, "export": cmd_export}
LOCKED = {"fetch", "update-excel", "run", "backfill"}


def main(argv=None):
    args = build_parser().parse_args(argv)
    _setup_logging(args.verbose)
    # No startup snapshot: a 30-minute schedule would rotate the 15 kept
    # backups out within a day. The web app's own boot still takes them.
    database.init_db(backup=False)
    cfg = load_config()
    fn = COMMANDS[args.cmd]
    try:
        if args.cmd in LOCKED:
            with Lock():
                return fn(args, cfg) or 0
        return fn(args, cfg) or 0
    except workbook.WorkbookError as e:
        print(f"ERROR: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())

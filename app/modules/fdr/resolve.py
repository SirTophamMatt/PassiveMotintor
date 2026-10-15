# Passive Monitor — Copyright (c) 2026 SirTophamMatt. All rights reserved.
"""What a day's FDR/TFB record IS, from stored revisions + manual overrides.

UI-free and the single answer used by the Intel page, the CLI and the
workbook writer, so they can never disagree.

Rules
-----
* **Source hierarchy** ``SOURCE_PRIORITY``: the CFA statewide feed, then the
  per-district feed. Where both have a value and they differ the district is a
  CONFLICT: nothing is selected, it goes to review.
* **Confirmed vs preliminary.** A forecast for day D counts as the day's
  record only if it was still being published within ``confirm_window_hours``
  before D began, or during D (``last_seen``). A 4-day-ahead forecast nobody
  re-checked (collector down) stays *preliminary* and is never written as fact.
* **Multi Day** = more than one rating applied within a district on the day
  (the workbook's own note on Multi Day: "did the FDR change during the day"):
  (a) the source published more than one rating for the district, or
  (b) the rating was amended DURING the day — the value current at midnight
      differs from one issued after ``multi_amendment_after`` (06:00 default;
      times are the BoM "forecast issued at" time the feed states, else when
      the tool first saw the revision;
      an overnight revision is the day's forecast being finalised, not a
      second rating). Both are recorded in fdr_rating_details.
  Multi Day is NO only when all nine districts are confirmed with one rating
  each; otherwise it is UNKNOWN (blank), never assumed NO.
* **TFB.** YES when any district has a confirmed YES; NO only when all nine
  are confirmed NO. Anything else is UNKNOWN — a missing declaration is not a
  NO.
* **Overrides** (fdr_overrides) sit on top of all of the above and are shown
  as such.
"""
import json
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta

from app.config import load_config
from app.modules.fdr import model, store

SOURCE_PRIORITY = ["cfa-state", "cfa-district"]
SOURCE_LABEL = {"cfa-state": "CFA statewide feed", "cfa-district": "CFA district feed",
                "override": "manual correction"}


@dataclass
class DistrictDay:
    district: str
    ratings: list = field(default_factory=list)
    source: str = None
    state: str = "none"        # confirmed | preliminary | unrecognised | conflict | none
    multi: bool = False
    multi_detail: str = None
    # The rating in force at the END of the day (or now): what the workbook
    # records when the rating changed during the day. Equals ``ratings``
    # unless an amendment changed it.
    latest: list = field(default_factory=list)
    issued: str = None         # BoM "forecast issued at" of the current revision
    tfb: str = None            # YES | NO | None (unknown)
    tfb_state: str = "none"    # confirmed | preliminary | none
    tfb_source: str = None
    revisions: int = 0
    last_seen: str = None
    notes: list = field(default_factory=list)

    @property
    def label(self):
        return model.display(self.ratings) if self.ratings else ""


@dataclass
class DayRecord:
    date: object
    stage: str                 # past | today | future
    districts: dict = field(default_factory=dict)
    multi: str = None          # YES | NO | None
    multi_source: str = None
    tfb_any: str = None        # YES | NO | None
    tfb_districts: list = field(default_factory=list)
    review: list = field(default_factory=list)
    comment_extra: str = ""
    no_data: bool = False      # nothing stored at all (e.g. before collection began)
    # Forecast view for days not yet started: TFB from any (incl. preliminary)
    # district value — what CFA currently says, which may still change.
    tfb_forecast: str = None

    @property
    def forecast_issued(self):
        """Latest BoM issue time among the districts (ISO string) or None."""
        times = [d.issued for d in self.districts.values()
                 if isinstance(d.issued, str) and d.issued]
        return max(times) if times else None

    @property
    def complete(self):
        return all(d.state == "confirmed" for d in self.districts.values())

    def comment(self):
        """The tool's own note for the workbook Comments cell (may be '')."""
        parts = []
        for d in self.districts.values():
            if d.multi and d.multi_detail:
                parts.append(f"{d.district}: {d.multi_detail}")
        if self.comment_extra:
            parts.append(self.comment_extra)
        return "; ".join(parts)


def _cfg(cfg):
    return (cfg or load_config())["fdr"]


def _confirmed(last_seen_epoch, d, window_h):
    return last_seen_epoch >= (model.day_start(d) - timedelta(hours=window_h)).timestamp()


def _fmt(epoch):
    return datetime.fromtimestamp(epoch, model.TZ).strftime("%H:%M")


def effective_epoch(row):
    """When a revision took effect: the BoM forecast issue time the feed
    states, else when this tool first saw it. An issue time later than the
    sighting would be nonsense (clock skew), so the sighting wins then."""
    seen = row["retrieved_epoch"]
    ts = row.get("issue_ts")
    if isinstance(ts, str) and ts:
        try:
            return min(seen, datetime.fromisoformat(ts).timestamp())
        except ValueError:
            pass
    return seen


def _same_day_changes(rows, d, after):
    """Ratings in force during day D for one source, from its revision rows
    (ok rows only, ordered by revision): [(ratings, from_epoch, to_epoch)]."""
    start, end = model.day_start(d).timestamp(), model.day_end(d).timestamp()
    cutoff = datetime.combine(d, after, tzinfo=model.TZ).timestamp()
    eff = [(effective_epoch(r), r) for r in rows]
    before = [r for t, r in eff if t < start]
    during = [(t, r) for t, r in eff if start <= t < end]
    seq = ([(start, before[-1])] if before else []) + during
    if not seq:
        return []
    # Revisions issued before the cutoff finalise the day's forecast: they
    # replace, rather than add to, what came before.
    while len(seq) > 1 and seq[1][0] < cutoff:
        seq = seq[1:]
    out = []
    for i, (t, r) in enumerate(seq):
        ratings = json.loads(r["ratings_json"] or "[]")
        frm = max(t, start) if i else start
        to = seq[i + 1][0] if i + 1 < len(seq) else end
        if out and out[-1][0] == ratings:
            out[-1] = (ratings, out[-1][1], to)
        else:
            out.append((ratings, frm, to))
    return out


def resolve_range(start, end, cfg=None, now=None):
    """{date: DayRecord} for every date in [start, end]."""
    c = _cfg(cfg)
    window = float(c.get("confirm_window_hours", 12))
    after = time.fromisoformat(c.get("multi_amendment_after", "06:00"))
    use_amend = c.get("multi_from_amendment", True)
    now = now or model.now()
    start, end = model.as_date(start), model.as_date(end)
    # Rows from before the range matter for "what was current at midnight".
    fc = store.forecasts(start - timedelta(days=7), end)
    tf = store.tfbs(start, end)
    ov = store.overrides(start, end)
    fc_by = {}
    for r in fc.to_dict("records"):
        fc_by.setdefault((r["valid_date"], r["district"], r["source"]), []).append(r)
    tf_by = {}
    for r in tf.to_dict("records"):
        if r["is_current"]:
            tf_by[(r["valid_date"], r["district"], r["source"])] = r
    ov_by = {}
    for r in ov.to_dict("records"):
        ov_by[(r["valid_date"], r["district"], r["field"])] = r

    out = {}
    d = start
    while d <= end:
        out[d] = _resolve_day(d, fc_by, tf_by, ov_by, window, after, use_amend, now)
        d += timedelta(days=1)
    return out


def resolve_day(d, cfg=None, now=None):
    d = model.as_date(d)
    return resolve_range(d, d, cfg, now)[d]


def _resolve_day(d, fc_by, tf_by, ov_by, window, after, use_amend, now):
    vd = d.isoformat()
    stage = "past" if d < now.date() else "today" if d == now.date() else "future"
    rec = DayRecord(date=d, stage=stage)
    for dist in model.DISTRICTS:
        dd = DistrictDay(dist)
        current = {}
        for src in SOURCE_PRIORITY:
            rows = fc_by.get((vd, dist, src), [])
            cur = [r for r in rows if r["is_current"]]
            if cur:
                current[src] = (cur[-1], rows)
        ok = {s: v for s, v in current.items() if v[0]["status"] == "ok"}
        if ok:
            src = next(s for s in SOURCE_PRIORITY if s in ok)
            row, rows = ok[src]
            dd.ratings = json.loads(row["ratings_json"] or "[]")
            dd.latest = list(dd.ratings)
            dd.issued = row.get("issue_ts") if isinstance(row.get("issue_ts"), str) else None
            dd.source = src
            dd.revisions = int(row["revision"])
            dd.last_seen = row["last_seen_at"]
            dd.state = ("confirmed" if _confirmed(row["last_seen_epoch"], d, window)
                        else "preliminary")
            others = {s: json.loads(v[0]["ratings_json"] or "[]")
                      for s, v in ok.items() if s != src}
            for s, r in others.items():
                if sorted(r) != sorted(dd.ratings):
                    dd.state = "conflict"
                    dd.notes.append(
                        f"{SOURCE_LABEL[src]} {model.display(dd.ratings)} vs "
                        f"{SOURCE_LABEL[s]} {model.display(r)}")
            if len(dd.ratings) > 1:
                dd.multi = True
                dd.multi_detail = f"{model.display(dd.ratings, ' and ')} (published)"
            elif use_amend:
                ok_rows = [r for r in rows if r["status"] == "ok"]
                spans = _same_day_changes(ok_rows, d, after)
                if len(spans) > 1:
                    dd.multi = True
                    dd.ratings = sorted({x for s in spans for x in s[0]},
                                        key=lambda r: -model.RANK[r])
                    dd.latest = list(spans[-1][0])
                    # "High → Extreme at 13:30" — each change and the time it
                    # took effect (BoM issue time, else when it was seen).
                    dd.multi_detail = model.display(spans[0][0]) + "".join(
                        f" → {model.display(rt)} at {_fmt(frm)}"
                        for rt, frm, _ in spans[1:]) + " (changed during the day)"
        elif current:
            row = current[next(s for s in SOURCE_PRIORITY if s in current)][0]
            dd.state = "unrecognised"
            dd.notes.append(f"published {row['raw_text']!r}")
            dd.last_seen = row["last_seen_at"]
        # TFB
        for src in SOURCE_PRIORITY:
            t = tf_by.get((vd, dist, src))
            if t is not None and t["declaration_status"] in (model.TFB_YES, model.TFB_NO):
                dd.tfb = t["declaration_status"]
                dd.tfb_source = src
                dd.tfb_state = ("confirmed" if _confirmed(t["last_seen_epoch"], d, window)
                                else "preliminary")
                break
        # Overrides
        o = ov_by.get((vd, dist, "rating"))
        if o is not None:
            dd.ratings = json.loads(o["value"])
            dd.latest = list(dd.ratings)
            dd.source, dd.state = "override", "confirmed"
            dd.multi = len(dd.ratings) > 1
            dd.multi_detail = (f"{model.display(dd.ratings, ' and ')} (manual correction)"
                               if dd.multi else None)
            dd.notes.append(f"corrected: {o['reason']}")
        o = ov_by.get((vd, dist, "tfb"))
        if o is not None:
            dd.tfb, dd.tfb_source, dd.tfb_state = o["value"], "override", "confirmed"
        rec.districts[dist] = dd

    ds = rec.districts.values()
    confirmed_tfb = [x for x in ds if x.tfb_state == "confirmed"]
    rec.tfb_districts = [x.district for x in confirmed_tfb if x.tfb == model.TFB_YES]
    if any(x.tfb == model.TFB_YES for x in ds):
        rec.tfb_forecast = "YES"
    elif all(x.tfb == model.TFB_NO for x in ds):
        rec.tfb_forecast = "NO"
    if rec.tfb_districts:
        rec.tfb_any = "YES"
    elif len(confirmed_tfb) == len(model.DISTRICTS):
        rec.tfb_any = "NO"
    if any(x.multi for x in ds if x.state == "confirmed"):
        rec.multi, rec.multi_source = "YES", "derived"
    elif all(x.state == "confirmed" for x in ds):
        rec.multi, rec.multi_source = "NO", "derived"
    o = ov_by.get((vd, "*", "multi"))
    if o is not None:
        rec.multi, rec.multi_source = o["value"], "override"
    o = ov_by.get((vd, "*", "comment"))
    if o is not None:
        rec.comment_extra = o["value"] or ""

    rec.no_data = (all(x.state == "none" for x in ds) and
                   all(x.tfb is None for x in ds))
    if stage != "future" and not rec.no_data:
        for x in ds:
            if x.state in ("conflict", "unrecognised"):
                rec.review.append(f"{x.district}: {x.state} — " + "; ".join(x.notes))
            elif x.state == "preliminary":
                rec.review.append(f"{x.district}: only a preliminary forecast was "
                                  f"seen (last {x.last_seen})")
            elif x.state == "none":
                rec.review.append(f"{x.district}: no rating collected")
        if rec.tfb_any is None:
            rec.review.append("Total Fire Ban status not confirmed for every district")
    return rec


def flag_conflicts(now=None):
    """Raise review flags for conflicts in the live window."""
    now = now or model.now()
    days = resolve_range(now.date() - timedelta(days=1), now.date() + timedelta(days=4),
                         now=now)
    conn = store.connect()
    try:
        for d, rec in days.items():
            for x in rec.districts.values():
                if x.state == "conflict":
                    store.add_flag(conn, d.isoformat(), x.district, "conflict",
                                   "; ".join(x.notes), now)
        conn.commit()
    finally:
        conn.close()


def history(d, district=None):
    """Every stored revision (ratings + TFB) for a date, oldest first."""
    vd = model.as_date(d).isoformat()
    fc = store.forecasts(vd, vd)
    tf = store.tfbs(vd, vd)
    if district:
        fc = fc[fc["district"] == district]
        tf = tf[tf["district"] == district]
    return fc, tf

"""Operational Summary — the model behind the SCC State Operational Summary deck.

UI-free, like ``briefing`` / ``feedback``: the Intel Tool page edits a draft,
``opsum_pptx`` renders a draft into the SCC's own PowerPoint template. Both
work from the field map defined here, so the form and the deck cannot drift.

**One draft per summary date** (``opsum_drafts``), all fields in one JSON blob.
Opening a date with no draft starts one: fields marked ``carry`` (controllers,
restrictions, deployments, activation grid, the roster image, ...) are copied
from the most recent earlier summary, everything else starts from its default.
Issuing copies the draft into ``opsum_versions`` so the deck as issued can be
rebuilt later even if the draft is edited again.

**Images** are stored content-addressed under ``<data dir>/opsum_images`` (sha1
of the bytes), so carrying an image forward to the next day is free and an
uploaded file is never overwritten in place.

Field kinds
    text    multi-line body. One line per paragraph; the paragraph keeps the
            template's bullet and font. ``  indented`` lines are sub-bullets,
            ``# Heading`` lines are bold unbulleted headings, a blank line is a
            spacer, ``**bold**`` and ``[label](https://...)`` work inline.
    line    a single inline value (``**bold**`` works).
    table   a fixed grid of inline values, token ``{{key.row.col}}``.
    colors  a fixed grid of colour choices (the Ambulance Victoria workload
            chart), token ``{{key.row.col}}``.
    grid    named cells set to Not Active / Active / Readiness (the activation
            slide), matched by cell label.
    image   an uploaded picture placed in the template's picture frame.
"""
import datetime
import hashlib
import json
import logging
import os
import re
import sqlite3
from dataclasses import dataclass, field

from app import database
from app.config import BASE_DIR, BUNDLE_DIR

log = logging.getLogger(__name__)

TEMPLATE_PATH = os.path.join(BUNDLE_DIR, "seed", "opsum_template.pptx")
IMAGE_DIR = os.path.join(BASE_DIR, "opsum_images")

MAX_TEXT = 20000
MAX_LINE = 300
MAX_IMAGE_BYTES = 15 * 1024 * 1024
SNAPSHOT_TIME = "0830"

GRID_STATES = ("Not Active", "Active", "Readiness")

# Ambulance Victoria workload cells. Only orange appears in the source deck;
# the rest follow the usual escalation palette. "" = the template's own fill.
WORKLOAD_COLOURS = {"": None, "Green": "00B050", "Yellow": "FFC000",
                    "Orange": "ED7D31", "Red": "FF0000"}

# Flood class -> fill, exactly as the source deck colours its Status row.
FLOOD_STATUS_FILLS = {"MINOR": "00B050", "MODERATE": "ED7D31", "MAJOR": "FF0000"}

RCC_LABELS = ["RCC – BSW", "RCC – GMP", "RCC – LMR", "RCC – HUM",
              "RCC – GIP", "RCC – SMR", "RCC – EMR", "RCC – NWM"]
ICC_LABELS = [
    "Alexandra", "Ararat", "Bairnsdale", "Ballarat", "Benalla",
    "Epsom", "Bendoc", "Colac", "Corryong", "Dandenong",
    "Erica", "Ferntree Gully", "Geelong", "Gisborne", "Heyfield",
    "Heywood", "Horsham", "Mansfield", "Mildura", "Noojee",
    "Orbost", "Ovens", "Seymour", "Shepparton", "Sunshine",
    "Swifts Creek", "Tallangatta", "Warragul", "Wangaratta", "Warrnambool",
    "Wodonga", "Woori Yallock",
]

PB_ROWS = ["BSW | South West", "GR | West", "LM | North West", "HR | North East",
           "GIP | South East", "PPR | (none)", "Total | Total"]
PB_COLS = ["FFMVic no. (cont.)", "FFMVic ha", "CFA no. (cont.)", "CFA ha"]


@dataclass
class Field:
    key: str
    label: str
    kind: str = "text"
    default: object = ""
    carry: bool = False
    help: str = ""
    rows: list = field(default_factory=list)     # table/colors row labels
    cols: list = field(default_factory=list)     # table/colors column labels
    labels: list = field(default_factory=list)   # grid cell labels


@dataclass
class Slide:
    key: str
    title: str
    fields: list
    optional: bool = False       # template slide named OPS_OPTIONAL:<key>
    note: str = ""


def _t(key, label, **kw):
    return Field(key, label, "text", **kw)


def _l(key, label, **kw):
    return Field(key, label, "line", **kw)


def _img(key, label, **kw):
    return Field(key, label, "image", default=None, **kw)


SLIDES = [
    Slide("header", "Header (every slide) + menu", [
        _l("as_at", "Current as at", help="Printed as “Current as at …” on every slide."),
        _l("scc_class1", "SCC activation — Class 1", carry=True),
        _l("scc_class2", "SCC activation — Class 2", carry=True),
    ]),
    Slide("key_points", "Key points", [
        _t("kp_last24", "Last 24 hours"),
        _t("kp_sig_incidents", "Significant operational incidents"),
        _t("kp_today", "Today"),
        _t("kp_outlook", "Outlook"),
        Field("warnings", "Community information & warnings (at 0830)", "table",
              rows=["Count"], cols=["Emergency Warning", "Watch & Act", "Advice",
                                    "Community Info"]),
        Field("ops_incidents", "Operational incidents 24 hours to 0600", "table",
              rows=["Count"], cols=["Going fires (0830)", "Grass / bushfires",
                                    "Structure fires", "Active RFAs (0830)",
                                    "Total RFAs"]),
    ]),
    Slide("weather", "Weather and seasonal forecasts", [
        _t("bom_warnings", "BoM warnings",
           help="Use “# Warning name” for each heading, bullets under it."),
        _t("wx_forecast", "Forecast for Victoria"),
        _t("wx_situation", "Weather situation"),
        _l("fire_wx_issued", "Fire weather briefing — heading suffix",
           help="Printed after “SCC FIRE WEATHER INTELLIGENCE BRIEFING |”."),
        _t("fire_wx_text", "Fire weather briefing — text"),
        _l("severe_wx_issued", "Severe weather briefing — heading suffix",
           help="e.g. ISSUED 16 SEPTEMBER"),
        _t("severe_wx_text", "Severe weather briefing — text"),
        _img("readiness", "Readiness levels table (image)"),
    ]),
    Slide("health_transport", "Public health and transport", [
        _t("health_alerts", "Health alerts and advisories"),
        _t("metro_status", "Metro service status"),
        _t("vline_status", "V/Line service status"),
        _t("airports", "Major airports"),
        _t("road_network", "Road network"),
        _t("asthma_text", "Thunderstorm asthma risk forecast", carry=True,
           default="The Epidemic Thunderstorm Asthma Risk forecast runs from "
                   "1 October to 31 December."),
        _t("thunderstorm_text", "Thunderstorm forecast"),
        _img("transport_strip_1", "Transport image strip 1", carry=True),
        _img("transport_strip_2", "Transport image strip 2", carry=True),
    ]),
    Slide("stats", "Operational statistics, Ambulance Victoria, other hazards", [
        _img("ops_stats", "Operational statistics (image)"),
        _l("av_month", "AV reporting month", help="e.g. September 2026"),
        Field("av_workload", "AV workload and demand (ERP escalations)", "colors",
              rows=["Metro Region", "Hume and Loddon Mallee"],
              cols=[f"{d} {h}" for d in range(1, 8) for h in ("AM", "PM")],
              help="Columns are the 7 days ending on the summary date."),
        Field("av_code1", "AV Code 1 responses", "table",
              rows=["This year", "Last year"], cols=[f"Day {d}" for d in range(1, 8)],
              help="Columns are the 7 days ending two days before the summary date."),
        _img("av_legend", "AV legend (image)", carry=True),
        _t("earthquakes", "Recent earthquakes (last 48 hrs)"),
        _t("tsunami", "Tsunami"),
        _t("flood_scenarios", "Flood scenarios"),
        _t("terror_level", "National terrorism threat level", carry=True),
        _t("power_disruptions", "Power disruptions"),
    ]),
    Slide("fire", "Fire restrictions and deployments", [
        _t("tfb_fdr", "Fire danger ratings and Total Fire Ban"),
        _img("fdr_map", "Fire danger rating map (image)"),
        _img("cfa_restrictions_map", "CFA fire restrictions map (image)", carry=True),
        Field("deeca", "DEECA — in prohibited period", "table", carry=True,
              rows=["Hume", "Gippsland", "Alpine Resorts"], cols=["In prohibited period"]),
        _img("planned_burns_map", "Planned burning map (image)"),
        Field("aircraft", "Aircraft", "table",
              rows=["Count"], cols=["Dispatched", "Planned dispatch", "Standby"]),
        _t("iccs_activated", "ICCs activated", carry=True,
           default="Refer to Incident Control Centres"),
        _t("deploy_interstate", "Personnel deployed interstate", carry=True),
        _t("deploy_international", "Personnel deployed internationally", carry=True),
    ]),
    Slide("planned_burning", "Planned burning", optional=True,
          note="Off-season the SCC leaves this slide out.", fields=[
        Field("pb_yday", "Planned burning summary — yesterday", "table",
              rows=PB_ROWS, cols=PB_COLS),
        Field("pb_ytd", "Planned burning summary — YTD", "table", carry=True,
              rows=["Burns ignited", "Operations ignited", "Hectares treated",
                    "Priority burns ignited"], cols=["FFMVic", "CFA"]),
        Field("pb_today", "Planned burning scheduled — today", "table",
              rows=PB_ROWS, cols=PB_COLS),
        _img("pb_progress", "Priority burn progress bars (image)"),
        _t("pb_situation", "Situation"),
        _t("pb_risks", "Risks and issues"),
    ]),
    Slide("media", "Media highlights and upcoming events", [
        _t("media", "Media highlights",
           help="“**1. Headline**”, the summary on the next line, a blank line between items."),
        _t("events", "Upcoming events in Victoria", carry=True),
        _img("social_1", "Social media highlight 1"),
        _img("social_2", "Social media highlight 2"),
        _img("social_3", "Social media highlight 3"),
        _img("social_4", "Social media highlight 4"),
    ]),
    Slide("activation", "SCC 4 day activation levels and current emergencies", [
        _img("scc_roster", "SCC activation levels / roster (image)", carry=True),
        _t("src_current", "Current State Response Controller", carry=True),
        _l("src_incoming_date", "Incoming SRC — date", carry=True, help="e.g. 20/09/2026"),
        _t("src_incoming", "Incoming State Response Controller", carry=True),
        _t("emv_duty_commissioner", "EMV Duty Commissioner", carry=True),
        Field("rcc", "Regional Control Centres", "grid", carry=True, labels=RCC_LABELS),
        Field("icc", "Incident Control Centres", "grid", carry=True, labels=ICC_LABELS),
    ]),
    Slide("avian", "Avian influenza outbreak", optional=True, fields=[
        _t("avian_situation", "Current / past biosecurity outbreak", carry=True),
        _img("avian_map", "Statewide situation overview (image)", carry=True),
        _t("avian_other_states", "Other states", carry=True),
    ]),
    Slide("flood", "Statewide flood snapshot", optional=True, fields=[
        _l("flood_overview_date", "Flood overview — date", carry=True,
           help="Printed after “STATE FLOOD OVERVIEW |”."),
        _img("flood_map", "State flood overview (image)", carry=True),
        _l("flood_timeline_title", "Flood timeline — title", carry=True,
           help="Printed after “Flood timeline –”, e.g. murray river | 19 December 2022"),
        _img("flood_timeline", "Flood timeline (image)", carry=True),
        _img("flood_legend", "Flood legend / BoM warnings note (image)", carry=True),
        _l("flood_table_title", "Gauge table title", carry=True,
           help="e.g. Murray River Flooding Overview"),
        Field("flood_gauges", "Gauges", "table", carry=True,
              rows=["Gauge", "Height", "Status", "Trend", "Outlook"],
              cols=[f"Gauge {i}" for i in range(1, 6)],
              help="Status MINOR / MODERATE / MAJOR colours the cell."),
    ]),
]

FIELDS = {f.key: f for s in SLIDES for f in s.fields}
OPTIONAL_SLIDES = [s.key for s in SLIDES if s.optional]


# --------------------------------------------------------------------------- #
# Dates and derived values
# --------------------------------------------------------------------------- #
def parse_date(value):
    """``YYYY-MM-DD`` (or a date) -> date; anything else raises ValueError."""
    if isinstance(value, datetime.date):
        return value
    return datetime.date.fromisoformat(str(value)[:10])


def default_as_at(d):
    return f"{SNAPSHOT_TIME}hrs on {d:%A} {d.day} {d:%B %Y}"


def derived_tokens(d):
    """Values the template needs that follow from the date alone."""
    d = parse_date(d)
    out = {"year_this": str(d.year), "year_last": str(d.year - 1)}
    for i in range(7):
        out[f"av_days.{i}"] = (d - datetime.timedelta(days=6 - i)).strftime("%d/%m")
        out[f"code1_days.{i}"] = (d - datetime.timedelta(days=8 - i)).strftime("%d/%m")
    return out


def column_labels(f, d):
    """Column labels for a table/colors field on a given date (the AV grids
    are dated, the rest are fixed)."""
    tok = derived_tokens(d)
    if f.key == "av_workload":
        return [f"{tok[f'av_days.{i}']} {h}" for i in range(7) for h in ("AM", "PM")]
    if f.key == "av_code1":
        return [tok[f"code1_days.{i}"] for i in range(7)]
    return list(f.cols)


def row_labels(f, d):
    if f.key == "av_code1":
        d = parse_date(d)
        return [str(d.year), str(d.year - 1)]
    return list(f.rows)


# --------------------------------------------------------------------------- #
# Draft shape
# --------------------------------------------------------------------------- #
def _empty_value(f):
    if f.kind in ("table", "colors"):
        return [["" for _ in f.cols] for _ in f.rows]
    if f.kind == "grid":
        return {label: GRID_STATES[0] for label in f.labels}
    if f.kind == "image":
        return None
    return f.default or ""


def blank(d):
    d = parse_date(d)
    fields = {k: _empty_value(f) for k, f in FIELDS.items()}
    fields["as_at"] = default_as_at(d)
    fields["av_month"] = f"{d:%B %Y}"
    return {"fields": fields, "optional": {k: False for k in OPTIONAL_SLIDES}}


def _clean_str(v, limit):
    if v is None or (isinstance(v, float) and v != v):     # NaN (see CLAUDE.md)
        return ""
    return str(v).replace("\r\n", "\n").replace("\r", "\n")[:limit]


def _clean_image(v):
    if not isinstance(v, dict):
        return None
    sha, ext = str(v.get("sha", "")), str(v.get("ext", ""))
    if re.fullmatch(r"[0-9a-f]{40}", sha) and ext in IMAGE_TYPES.values():
        return {"sha": sha, "ext": ext, "name": _clean_str(v.get("name"), 120)}
    return None


def normalise(data, d):
    """Coerce anything (a stored blob, a form submission) into a complete,
    well-typed draft. Unknown keys are dropped; missing ones take defaults;
    tables are forced to their declared shape. Values arrive from a browser,
    so this is also the length/shape validation."""
    out = blank(d)
    data = data if isinstance(data, dict) else {}
    src = data.get("fields") if isinstance(data.get("fields"), dict) else {}
    for key, f in FIELDS.items():
        if key not in src:
            continue
        v = src[key]
        if f.kind == "text":
            out["fields"][key] = _clean_str(v, MAX_TEXT)
        elif f.kind == "line":
            out["fields"][key] = _clean_str(v, MAX_LINE).replace("\n", " ")
        elif f.kind in ("table", "colors"):
            grid = out["fields"][key]
            rows = v if isinstance(v, list) else []
            for i in range(len(f.rows)):
                row = rows[i] if i < len(rows) and isinstance(rows[i], list) else []
                for j in range(len(f.cols)):
                    cell = _clean_str(row[j] if j < len(row) else "", MAX_LINE)
                    if f.kind == "colors" and cell not in WORKLOAD_COLOURS:
                        cell = ""
                    grid[i][j] = cell
        elif f.kind == "grid":
            states = v if isinstance(v, dict) else {}
            for label in f.labels:
                s = states.get(label)
                if s in GRID_STATES:
                    out["fields"][key][label] = s
        elif f.kind == "image":
            out["fields"][key] = _clean_image(v)
    opt = data.get("optional") if isinstance(data.get("optional"), dict) else {}
    for k in OPTIONAL_SLIDES:
        out["optional"][k] = bool(opt.get(k, False))
    return out


# --------------------------------------------------------------------------- #
# Storage
# --------------------------------------------------------------------------- #
def _now():
    return datetime.datetime.now().isoformat(sep=" ", timespec="seconds")


def _row(sql, params):
    conn = database.get_connection()
    try:
        conn.row_factory = sqlite3.Row
        r = conn.execute(sql, params).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def load(d):
    """The stored draft for date ``d`` as ``(data, meta)``, or ``(None, None)``."""
    d = parse_date(d)
    r = _row("SELECT * FROM opsum_drafts WHERE summary_date = ?", (d.isoformat(),))
    if not r:
        return None, None
    try:
        data = json.loads(r["data_json"])
    except ValueError:
        log.error("Operational Summary draft %s is not valid JSON; starting blank", d)
        data = {}
    meta = {k: r[k] for k in ("summary_date", "status", "version", "created_at",
                              "updated_at", "issued_at")}
    return normalise(data, d), meta


def previous(d):
    """The most recent summary before ``d`` as ``(date, data)``, or None."""
    d = parse_date(d)
    r = _row("SELECT summary_date FROM opsum_drafts WHERE summary_date < ? "
             "ORDER BY summary_date DESC LIMIT 1", (d.isoformat(),))
    if not r:
        return None
    prev = parse_date(r["summary_date"])
    return prev, load(prev)[0]


def start(d):
    """A new draft for ``d``: defaults, plus every ``carry`` field and the
    optional-slide switches from the previous summary. Not saved."""
    d = parse_date(d)
    data = blank(d)
    prev = previous(d)
    carried_from = None
    if prev:
        carried_from, pdata = prev
        for key, f in FIELDS.items():
            if f.carry:
                data["fields"][key] = pdata["fields"][key]
        data["optional"] = dict(pdata["optional"])
    return data, carried_from


def open_draft(d):
    """What the page shows for ``d``: the saved draft, else a fresh start.
    Returns ``(data, meta, carried_from)``; ``meta`` is None when unsaved."""
    data, meta = load(d)
    if data is not None:
        return data, meta, None
    data, carried_from = start(d)
    return data, None, carried_from


def save(d, data):
    """Upsert the draft; returns the new version number. Saving an issued
    summary keeps it issued (the issued copy in opsum_versions is untouched)."""
    d = parse_date(d)
    blob = json.dumps(normalise(data, d), ensure_ascii=False, sort_keys=True)
    now = _now()
    conn = database.get_connection()
    try:
        conn.execute(
            "INSERT INTO opsum_drafts (summary_date, data_json, created_at, updated_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(summary_date) DO UPDATE SET "
            "data_json = excluded.data_json, updated_at = excluded.updated_at, "
            "version = opsum_drafts.version + 1",
            (d.isoformat(), blob, now, now))
        conn.commit()
        return conn.execute("SELECT version FROM opsum_drafts WHERE summary_date = ?",
                            (d.isoformat(),)).fetchone()[0]
    finally:
        conn.close()


def issue(d, data):
    """Save, mark issued, and keep an immutable copy of what was issued."""
    d = parse_date(d)
    version = save(d, data)
    now = _now()
    blob = json.dumps(normalise(data, d), ensure_ascii=False, sort_keys=True)
    conn = database.get_connection()
    try:
        conn.execute("UPDATE opsum_drafts SET status = 'issued', issued_at = ? "
                     "WHERE summary_date = ?", (now, d.isoformat()))
        conn.execute("INSERT INTO opsum_versions (summary_date, issued_at, data_json) "
                     "VALUES (?, ?, ?)", (d.isoformat(), now, blob))
        conn.commit()
    finally:
        conn.close()
    return version


def recent(limit=14):
    conn = database.get_connection()
    try:
        return [{"summary_date": r[0], "status": r[1], "updated_at": r[2]}
                for r in conn.execute(
                    "SELECT summary_date, status, updated_at FROM opsum_drafts "
                    "ORDER BY summary_date DESC LIMIT ?", (limit,))]
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Images
# --------------------------------------------------------------------------- #
IMAGE_TYPES = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif",
               "image/bmp": "bmp"}


def _sniff(blob):
    if blob.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if blob.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if blob[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if blob.startswith(b"BM"):
        return "image/bmp"
    return None


def store_image(blob, name=""):
    """Validate and store uploaded image bytes; returns the image reference
    kept in the draft. Raises ValueError with a user-facing message.

    The type is decided by the file's own magic bytes, never its name or the
    browser's claim, so only real PNG/JPEG/GIF/BMP data is ever stored or
    served back."""
    if not blob:
        raise ValueError("The file is empty.")
    if len(blob) > MAX_IMAGE_BYTES:
        raise ValueError(f"Images must be under {MAX_IMAGE_BYTES // (1024 * 1024)} MB.")
    ctype = _sniff(blob)
    if ctype is None:
        raise ValueError("Only PNG, JPEG, GIF or BMP images can be used.")
    sha = hashlib.sha1(blob).hexdigest()
    ext = IMAGE_TYPES[ctype]
    os.makedirs(IMAGE_DIR, exist_ok=True)
    path = os.path.join(IMAGE_DIR, f"{sha}.{ext}")
    if not os.path.exists(path):
        tmp = path + ".part"
        with open(tmp, "wb") as fh:
            fh.write(blob)
        os.replace(tmp, path)
    return {"sha": sha, "ext": ext, "name": _clean_str(os.path.basename(name or ""), 120)}


def image_path(ref):
    """Filesystem path of a stored image reference, or None if missing/invalid."""
    ref = _clean_image(ref)
    if not ref:
        return None
    path = os.path.join(IMAGE_DIR, f"{ref['sha']}.{ref['ext']}")
    return path if os.path.exists(path) else None


def image_file(filename):
    """Path for a served image filename (``<sha1>.<ext>``), strictly validated."""
    m = re.fullmatch(r"([0-9a-f]{40})\.(png|jpg|gif|bmp)", filename or "")
    if not m:
        return None
    return image_path({"sha": m.group(1), "ext": m.group(2)})


def filename(d):
    d = parse_date(d)
    return f"{d.isoformat()}_SCC-State_Operational_Summary.pptx"

"""Pure parsing for the Mazzanet CFA pager page (no I/O, no DB).

Two layers, kept apart so each can be tested on its own:

- ``parse_page(html)`` turns the page into raw rows: capcode, sent time, alias
  and message text.
- ``parse_message(text)`` reads the CFA meaning out of one message: the job's
  F-number, brigade, incident type, priority, and any escalation (``MAKE
  TANKERS 5`` make-up requests, or ``TANKER TRAWT1 REQUIRED`` requests for a
  specific appliance).

The page layout was not visible when this was written (the development sandbox
cannot reach mazzanet.net.au), so ``parse_page`` does NOT bind to column
positions or CSS classes. Each table cell is identified by what it contains: a
capcode is 6-9 digits and nothing else, a time cell holds a clock time, the
message is the longest remaining text. A table that is reordered or gains a
column therefore still parses. If no table rows are found it falls back to
reading ``capcode time message`` lines from the page text. When both find
nothing, the scraper saves the raw page for inspection instead of guessing.
"""
import re
from datetime import datetime, timedelta

from bs4 import BeautifulSoup

# CFA job number: F + YYMMDD + 3-4 digit sequence (F090202691, F2609241234).
F_NUMBER_RE = re.compile(r"\bF\d{9,10}\b")
CAPCODE_RE = re.compile(r"^\d{6,9}$")
TIME_RE = re.compile(r"\b(\d{1,2}):(\d{2})(?::(\d{2}))?\b")
NUM_DATE_RE = re.compile(r"\b(\d{1,4})[-/.](\d{1,2})[-/.](\d{2,4})\b")
NAME_DATE_RE = re.compile(
    r"\b(\d{1,2})\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*"
    r"(?:\s+(\d{4}))?\b", re.I)
_MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct",
     "nov", "dec"), start=1)}

# Appliance types that count as an escalation, most specific first so
# "PUMPER TANKER" is not read as a Pumper followed by a Tanker. To track
# another resource (strike teams, aircraft ...), add a row here.
APPLIANCE_TYPES = [
    ("Pumper Tanker", r"PUMPER[\s-]?TANKERS?"),
    ("Ultralight", r"ULTRA[\s-]?LIGHTS?|ULTRALITES?"),
    ("Pumper", r"PUMPERS?"),
    ("Tanker", r"TANKERS?"),
]
_TYPE_ALT = "|".join(f"(?P<t{i}>{pat})" for i, (_, pat) in enumerate(APPLIANCE_TYPES))
_TYPE_RE = re.compile(rf"\b(?:{_TYPE_ALT})\b", re.I)

# "MAKE TANKERS 5", "MAKE 5 TANKERS", "MAKE PUMPERS 2 TANKERS 4",
# "MAKE UP TANKERS TO 6". Each MAKE starts a segment that runs to the next
# sentence break or the next MAKE; every type/number pair inside it counts.
_MAKE_RE = re.compile(r"\bMAKE\b(?:\s+UP\b)?(?P<body>.*?)(?=\bMAKE\b|[.;/]|$)", re.I)
_PAIR_RE = re.compile(
    rf"(?:(?P<pre>\d{{1,3}})\s*)?(?:{_TYPE_ALT})\b(?:\s*(?:TO|X|=|:)?\s*(?P<post>\d{{1,3}})\b)?",
    re.I)
_REQUIRED_RE = re.compile(r"\b(?:REQUIRED|REQ'?D|REQ|REQUESTED)\b", re.I)
_REQ_ITEM_RE = re.compile(
    rf"\b(?:{_TYPE_ALT})\b(?:\s+(?P<unit>[A-Z]{{3,6}}\d{{1,2}})\b)?", re.I)
# Brigade / appliance call signs: letters then a digit or two (CRAN1, TRAWT1).
_CALLSIGN_RE = re.compile(r"^[A-Z]{3,6}\d{1,2}$")
# Incident type codes follow the F-number: STRUC1, G&SC1, ALARC1, NOSTC1 ...
_INCIDENT_RE = re.compile(r"^[A-Z&]{3,7}\d{1,2}$")

PRIORITY_PREFIXES = [("@@", "Emergency"), ("HB", "Non-emergency"),
                     ("QD", "Admin")]


def _type_label(match):
    for i, (label, _) in enumerate(APPLIANCE_TYPES):
        if match.group(f"t{i}"):
            return label
    return None


# --------------------------------------------------------------------------- #
# Times
# --------------------------------------------------------------------------- #
def _year(y):
    y = int(y)
    return 2000 + y if y < 100 else y


def parse_sent_time(text, now=None):
    """Datetime from a cell like '14:23:05 24-09-26', '24/09/2026 14:23',
    '2026-09-24 14:23:05' or 'Wed 24 Sep 14:23'. Dates are read day-first
    (Australian), except a leading 4-digit year. With no date the time is taken
    as today, or yesterday if that would put it more than an hour in the future
    (a page read just after midnight still carries late-evening messages).
    Returns None when there is no clock time."""
    if not text:
        return None
    now = now or datetime.now()
    t = TIME_RE.search(text)
    if not t:
        return None
    hh, mm, ss = int(t.group(1)), int(t.group(2)), int(t.group(3) or 0)
    if hh > 23 or mm > 59 or ss > 59:
        return None
    # Take the date from outside the matched time so '14:23:05' is never
    # mistaken for part of one.
    rest = text[:t.start()] + " " + text[t.end():]
    date = None
    d = NUM_DATE_RE.search(rest)
    if d:
        a, b, c = d.groups()
        try:
            if len(a) == 4:
                date = datetime(int(a), int(b), int(c))
            else:
                date = datetime(_year(c), int(b), int(a))
        except ValueError:
            date = None
    if date is None:
        n = NAME_DATE_RE.search(rest)
        if n:
            day, mon, year = n.groups()
            try:
                date = datetime(int(year) if year else now.year,
                                _MONTHS[mon[:3].lower()], int(day))
            except ValueError:
                date = None
            if date is not None and not year and date - now > timedelta(days=1):
                date = date.replace(year=date.year - 1)   # Dec page read in Jan
    if date is None:
        cand = now.replace(hour=hh, minute=mm, second=ss, microsecond=0)
        if cand - now > timedelta(hours=1):
            cand -= timedelta(days=1)
        return cand
    return date.replace(hour=hh, minute=mm, second=ss)


# --------------------------------------------------------------------------- #
# Page -> rows
# --------------------------------------------------------------------------- #
def _clean(text):
    return re.sub(r"\s+", " ", text or "").strip()


def _row_from_cells(cells, now):
    """One message from a row's cell texts, identified by content not position."""
    cells = [c for c in (_clean(c) for c in cells) if c]
    if not cells:
        return None
    capcode = next((c for c in cells if CAPCODE_RE.match(c)), None)
    time_cell = next((c for c in cells if c != capcode and len(c) <= 40
                      and TIME_RE.search(c) and parse_sent_time(c, now)), None)
    rest = [c for c in cells if c not in (capcode, time_cell)]
    if not rest:
        return None
    message = max(rest, key=len)
    if len(message) < 6:
        return None
    alias = " / ".join(c for c in rest if c is not message) or None
    if capcode is None:
        # Capcode sharing a cell with something else ("0240608 CFA HQ").
        for c in (time_cell or "", alias or ""):
            m = re.search(r"\b\d{7}\b", c)
            if m:
                capcode = m.group(0)
                break
    return {
        "capcode": capcode,
        "alias": alias,
        "sent_at": parse_sent_time(time_cell, now) if time_cell else None,
        "message": message,
    }


_LINE_RE = re.compile(
    r"^(?P<cap>\d{6,9})\s+(?P<time>\d{1,2}:\d{2}(?::\d{2})?"
    r"(?:\s+\d{1,4}[-/.]\d{1,2}[-/.]\d{2,4})?)\s+(?P<msg>.{6,})$")
_LINE_TIME_FIRST_RE = re.compile(
    r"^(?P<time>\d{1,2}:\d{2}(?::\d{2})?(?:\s+\d{1,4}[-/.]\d{1,2}[-/.]\d{2,4})?)"
    r"\s+(?P<cap>\d{6,9})\s+(?P<msg>.{6,})$")


def _rows_from_text(text, now):
    rows = []
    for line in text.splitlines():
        line = _clean(line)
        m = _LINE_RE.match(line) or _LINE_TIME_FIRST_RE.match(line)
        if m:
            rows.append({"capcode": m.group("cap"), "alias": None,
                         "sent_at": parse_sent_time(m.group("time"), now),
                         "message": m.group("msg")})
    return rows


def parse_page(html, now=None):
    """Every pager message on the page as dicts of capcode / alias / sent_at /
    message, in page order. Header rows (``<th>``) and rows without a message
    are skipped."""
    now = now or datetime.now()
    if isinstance(html, bytes):
        html = html.decode("utf-8", errors="replace")
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    rows = []
    for tr in soup.find_all("tr"):
        # Only this row's own cells: a nested table would otherwise be read
        # twice, once as part of its parent row.
        tds = tr.find_all("td", recursive=False)
        if not tds or tr.find("table"):
            continue
        row = _row_from_cells([td.get_text(" ") for td in tds], now)
        if row:
            rows.append(row)
    if not rows:
        rows = _rows_from_text(soup.get_text("\n"), now)
    return rows


# --------------------------------------------------------------------------- #
# Message -> meaning
# --------------------------------------------------------------------------- #
def parse_escalation(text):
    """Escalation content of one message.

    Returns ``{"make": {type: count}, "required": [(type, unit or None)]}``.
    ``make`` is a make-up request (``MAKE TANKERS 5`` = bring the job to five
    tankers); ``required`` lists specific appliances paged to the job
    (``TANKER TRAWT1 REQUIRED``). A type named without a number after MAKE is
    recorded with count None rather than dropped."""
    text = text or ""
    make = {}
    for seg in _MAKE_RE.finditer(text):
        for pair in _PAIR_RE.finditer(seg.group("body")):
            label = _type_label(pair)
            num = pair.group("post") or pair.group("pre")
            count = int(num) if num else None
            if label not in make or (count is not None and (make[label] or 0) < count):
                make[label] = count
    required = []
    if _REQUIRED_RE.search(text):
        # Drop the MAKE segments first so "MAKE TANKERS 5 ... REQUIRED" is not
        # also counted as a request for one unnamed tanker.
        stripped = _MAKE_RE.sub(" ", text)
        # finditer is left-to-right and non-overlapping, and the alternation
        # tries the two-word types first, so "PUMPER TANKER" is one request.
        for m in _REQ_ITEM_RE.finditer(stripped):
            required.append((_type_label(m), m.group("unit")))
    return {"make": make, "required": required}


def parse_message(text):
    """CFA fields read out of one message's text. Missing parts are None."""
    text = _clean(text)
    upper = text.upper()
    priority = None
    for prefix, label in PRIORITY_PREFIXES:
        if upper.startswith(prefix):
            priority = label
            break
    f_match = F_NUMBER_RE.search(upper)
    f_number = f_match.group(0) if f_match else None
    brigade = incident_type = None
    if f_match:
        before = upper[:f_match.start()].replace("@@", " ").split()
        after = upper[f_match.end():].split()
        if before and _CALLSIGN_RE.match(before[-1]):
            brigade = before[-1]
        if after and _INCIDENT_RE.match(after[0]):
            incident_type = after[0]
            # "@@ALERT F2609... CRAN1 STRUC1": brigade after the F-number.
            if brigade is None and len(after) > 1 and _INCIDENT_RE.match(after[1]) \
                    and _CALLSIGN_RE.match(after[0]):
                brigade, incident_type = after[0], after[1]
    esc = parse_escalation(upper)
    return {
        "f_number": f_number,
        "brigade": brigade,
        "incident_type": incident_type,
        "priority": priority,
        "make": esc["make"],
        "required": esc["required"],
        "is_escalation": bool(esc["make"] or esc["required"]),
    }


def escalation_summary(make, required):
    """Short human text: 'MAKE Tanker 5, Pumper 2 · Tanker TRAWT1 req'."""
    parts = []
    if make:
        parts.append("MAKE " + ", ".join(
            f"{t} {n}" if n is not None else t for t, n in make.items()))
    if required:
        parts.append(", ".join(f"{t} {u} req" if u else f"{t} req"
                               for t, u in required))
    return " · ".join(parts) or None

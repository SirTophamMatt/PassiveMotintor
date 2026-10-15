# Passive Monitor — Copyright (c) 2026 SirTophamMatt. All rights reserved.
"""CFA "Total Fire Bans and Fire Danger Ratings" RSS -> structured days.

Feed (RSS 2.0, CFA says it refreshes every minute):
``https://www.cfa.vic.gov.au/cfa/rssfeed/tfbfdrforecast_rss.xml`` — one
``<item>`` per forecast day (today + the next three), the date in the item
title (``Thursday, 16 October 2025``; sometimes prefixed ``Today,``), and an
HTML ``<description>`` holding a Total Fire Ban section followed by a "Fire
Danger Ratings" section of ``District: RATING`` lines. CFA also publishes one
feed per district (``<slug>-firedistrict_rss.xml``) in the same family.

The live markup could not be fetched from the development sandbox (the host is
blocked there), so the structure above comes from CFA's RSS page, the
data.vic.gov.au catalogue and an open-source consumer of the same feed. The
parser therefore identifies everything by CONTENT, never by position:

* the date from any ``D Month YYYY`` in the title, cross-checked against a
  weekday name if the title has one (a mismatch rejects the day);
* districts by name (``model.districts_in``: longest-first, so "North Central"
  never also reads as "Central");
* ratings by the AFDRS words (``model.parse_ratings``); anything else in the
  value marks it unrecognised rather than being guessed;
* TFB status from explicit YES/NO wording, per district, with a statewide
  "no Total Fire Bans" sentence accepted as an explicit NO for every district
  not otherwise named. A district the TFB text does not mention is UNKNOWN,
  never NO.

If a non-empty feed yields no district ratings at all, the caller saves the raw
body and errors — a format change must be visible, not look like a quiet day.
"""
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from email.utils import parsedate_to_datetime

from bs4 import BeautifulSoup
from lxml import etree

from app.modules.fdr import model

_DATE_RE = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]{3,9})\.?,?\s+(\d{4})\b")
_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday",
             "saturday", "sunday"]
_MONTHS = {m: i + 1 for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july",
     "august", "september", "october", "november", "december"])}
_RATINGS_HEADING = re.compile(r"fire\s+danger\s+ratings?", re.I)

_YES = re.compile(r"\byes\b", re.I)
_NEG = re.compile(r"\b(no|not|nil|none)\b", re.I)
_POS = re.compile(r"\b(declared|in force|applies|current)\b", re.I)
_TFB_WORD = re.compile(r"total\s+fire\s+bans?|\btfbs?\b", re.I)
_STATEWIDE_NO = re.compile(
    r"\bno\s+(total\s+fire\s+bans?|tfbs?)\b|\bthere\s+(are|is)\s+(currently\s+)?no\s+"
    r"(total\s+fire\s+bans?|tfbs?)\b|\b(total\s+fire\s+bans?|tfbs?)\s+(has|have)\s+not\s+been",
    re.I)
_STATEWIDE_YES = re.compile(
    r"\b(whole|all|entire)\s+(of\s+)?(the\s+)?(state|victoria)\b|\ball\s+(fire\s+)?"
    r"districts\b|\bstate-?wide\b", re.I)
_BLOCK_TAGS = ("p", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6",
               "ul", "ol", "table", "section")


@dataclass
class DistrictRating:
    ratings: list            # canonical AFDRS ratings, highest first
    recognised: bool         # False -> value needs review, ratings may be []
    raw: str                 # the source text it came from


@dataclass
class TfbStatus:
    status: str              # model.TFB_YES / TFB_NO
    basis: str               # "district" | "statewide statement" | "district feed"
    raw: str


@dataclass
class DayFeed:
    valid_date: date
    title: str
    issued: datetime = None          # item pubDate (feed time), if any
    ratings: dict = field(default_factory=dict)   # district -> DistrictRating
    tfb: dict = field(default_factory=dict)       # district -> TfbStatus
    text: str = ""
    problems: list = field(default_factory=list)


@dataclass
class FeedResult:
    channel_issued: datetime = None
    days: list = field(default_factory=list)
    problems: list = field(default_factory=list)
    item_count: int = 0


# --------------------------------------------------------------------------- #
def _rfc822(text):
    if not text:
        return None
    try:
        dt = parsedate_to_datetime(text.strip())
    except (TypeError, ValueError, IndexError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=model.TZ)
    return dt.astimezone(model.TZ)


def title_date(title):
    """(date | None, problem | None) from an item title."""
    m = _DATE_RE.search(title or "")
    if not m:
        return None, f"no date in item title {title!r}"
    month = _MONTHS.get(m.group(2).lower()) or next(
        (v for k, v in _MONTHS.items() if k.startswith(m.group(2).lower()[:3])), None)
    if not month:
        return None, f"unreadable month in item title {title!r}"
    try:
        d = date(int(m.group(3)), month, int(m.group(1)))
    except ValueError:
        return None, f"impossible date in item title {title!r}"
    named = [w for w in _WEEKDAYS if re.search(r"\b" + w + r"\b", title, re.I)]
    if named and _WEEKDAYS[d.weekday()] not in named:
        return None, (f"item title {title!r}: {named[0].title()} does not fall on "
                      f"{d.isoformat()}")
    return d, None


def html_lines(fragment):
    """Description HTML -> its text lines, one per block element / <br>.

    Inline tags (``<strong>Mallee:</strong> HIGH``) stay on one line; table
    cells are joined with ':' so ``<td>Mallee</td><td>HIGH</td>`` reads as
    ``Mallee: HIGH``."""
    soup = BeautifulSoup(fragment or "", "lxml")
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for cell in soup.find_all(["td", "th"]):
        cell.append(": ")
    for tag in soup.find_all(_BLOCK_TAGS):
        tag.append("\n")
    text = soup.get_text("").replace(" ", " ")
    out = []
    for line in text.splitlines():
        line = re.sub(r"\s+", " ", line).strip(" :")
        line = re.sub(r"(\s*:\s*)+", ": ", line)
        if line:
            out.append(line)
    return out


def _value_after(line, districts):
    """The part of a district line that is its value."""
    if ":" in line:
        return line.rsplit(":", 1)[1].strip()
    out = line
    for d in districts:
        out = re.sub(re.escape(d), " ", out, flags=re.I)
    return out.strip(" -–—")


def _tfb_of(text):
    """YES / NO / None from one statement."""
    if _YES.search(text):
        return model.TFB_YES
    if _NEG.search(text):
        return model.TFB_NO
    if _POS.search(text) and _TFB_WORD.search(text):
        return model.TFB_YES
    return None


def parse_day_text(lines, district_hint=None):
    """(ratings, tfb, problems) from one item's description lines."""
    # The section heading names no district and carries no rating itself
    # ("Fire Danger Rating: EXTREME" on a district feed is a value, not a heading).
    split = next((i for i, l in enumerate(lines) if _RATINGS_HEADING.search(l)
                  and not model.districts_in(l)
                  and not model.parse_ratings(_RATINGS_HEADING.sub(" ", l))[0]), None)
    tfb_lines = lines[:split] if split is not None else []
    rating_lines = lines[split + 1:] if split is not None else lines
    problems = []

    ratings = {}
    for line in rating_lines:
        names = model.districts_in(line)
        if not names and district_hint and (
                _RATINGS_HEADING.search(line) or model.parse_ratings(line)[0]):
            names = [district_hint]
        if not names:
            continue
        value = _value_after(line, names)
        found, ok = model.parse_ratings(value)
        if not found and not value:
            continue
        for d in names:
            if d in ratings:
                prev = ratings[d]
                merged = list(dict.fromkeys(prev.ratings + found))
                ratings[d] = DistrictRating(
                    sorted(merged, key=lambda r: -model.RANK[r]),
                    prev.recognised and ok, prev.raw + " | " + line)
                problems.append(f"{d} listed more than once in the ratings")
            else:
                ratings[d] = DistrictRating(found, ok, line)

    tfb, statewide = {}, None
    # Without a ratings heading there is no TFB section to read: TFB lines are
    # then picked out by wording from the whole text.
    candidates = tfb_lines if split is not None else [
        l for l in lines if _TFB_WORD.search(l)]
    for line in candidates:
        names = model.districts_in(line)
        if not names and district_hint and _TFB_WORD.search(line):
            st = _tfb_of(line)
            if st:
                tfb[district_hint] = TfbStatus(st, "district feed", line)
            continue
        if not names:
            if _STATEWIDE_NO.search(line):
                statewide = TfbStatus(model.TFB_NO, "statewide statement", line)
            elif _TFB_WORD.search(line) and _STATEWIDE_YES.search(line) \
                    and not _NEG.search(line):
                statewide = TfbStatus(model.TFB_YES, "statewide statement", line)
            continue
        st = _tfb_of(_value_after(line, names) if len(names) == 1 else line)
        if not st:
            continue
        for d in names:
            if d in tfb and tfb[d].status != st:
                problems.append(f"{d}: contradictory Total Fire Ban lines")
                tfb[d] = TfbStatus(model.TFB_UNKNOWN, "contradictory", tfb[d].raw
                                   + " | " + line)
            else:
                tfb[d] = TfbStatus(st, "district", line)
    if statewide:
        for d in model.DISTRICTS:
            tfb.setdefault(d, statewide)
    tfb = {d: s for d, s in tfb.items() if s.status != model.TFB_UNKNOWN or
           s.basis == "contradictory"}
    return ratings, tfb, problems


def parse_feed(body, district_hint=None, fetched_at=None):
    """Raw feed bytes -> FeedResult. Never raises on content; problems are
    reported in ``FeedResult.problems`` / ``DayFeed.problems``."""
    fetched_at = fetched_at or model.now()
    res = FeedResult()
    try:
        parser = etree.XMLParser(recover=True, resolve_entities=False,
                                 no_network=True)
        root = etree.fromstring(body, parser=parser)
    except (etree.XMLSyntaxError, ValueError) as e:
        res.problems.append(f"feed is not readable XML: {e}")
        return res
    if root is None:
        res.problems.append("feed is empty or not XML")
        return res
    channel = root.find("channel")
    if channel is not None:
        res.channel_issued = _rfc822(channel.findtext("lastBuildDate")) or \
            _rfc822(channel.findtext("pubDate"))
    items = root.findall(".//item")
    res.item_count = len(items)
    earliest = fetched_at.date() - timedelta(days=1)
    latest = fetched_at.date() + timedelta(days=7)
    seen = set()
    for item in items:
        title = (item.findtext("title") or "").strip()
        desc = item.findtext("description") or ""
        lines = html_lines(desc)
        d, problem = title_date(title)
        if d is None and lines:
            d, _ = title_date(lines[0])
        if d is None:
            res.problems.append(problem)
            continue
        if not (earliest <= d <= latest):
            res.problems.append(f"item {title!r}: {d.isoformat()} is outside the "
                                f"plausible window {earliest}..{latest}; ignored")
            continue
        if d in seen:
            res.problems.append(f"two items for {d.isoformat()}; the first is used")
            continue
        seen.add(d)
        ratings, tfb, problems = parse_day_text(lines, district_hint)
        res.days.append(DayFeed(
            valid_date=d, title=title,
            issued=_rfc822(item.findtext("pubDate")) or res.channel_issued,
            ratings=ratings, tfb=tfb, text="\n".join(lines), problems=problems))
    return res

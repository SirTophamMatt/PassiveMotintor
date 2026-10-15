# Passive Monitor — Copyright (c) 2026 SirTophamMatt. All rights reserved.
"""Victorian fire weather districts and the AFDRS rating hierarchy.

Pure data + string handling; no I/O. Everything that turns source or workbook
text into a district or a rating goes through here, so the feed parser, the
database and the workbook writer cannot disagree about what "W&S Gippsland" or
"HIGH" means.
"""
import re
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Australia/Melbourne")

# The nine fire weather districts, in the order CFA lists them.
DISTRICTS = [
    "Mallee",
    "Wimmera",
    "Northern Country",
    "North Central",
    "North East",
    "East Gippsland",
    "West and South Gippsland",
    "Central",
    "South West",
]

# Other spellings seen in headers and feeds. Matched case-insensitively after
# collapsing whitespace and treating "&" as "and".
_ALIASES = {
    "Northern Country": ["northern country", "nth country", "n country",
                         "northern ctry"],
    "North Central": ["north central", "nth central", "n central"],
    "North East": ["north east", "northeast", "north-east", "nth east", "ne"],
    "East Gippsland": ["east gippsland", "e gippsland", "east gipps"],
    "West and South Gippsland": [
        "west and south gippsland", "west & south gippsland",
        "w and s gippsland", "w&s gippsland", "west and sth gippsland",
        "west gippsland", "west/south gippsland", "w & s gippsland",
        "west and south gipps", "wsg"],
    "South West": ["south west", "southwest", "south-west", "sth west", "sw"],
    "Mallee": ["mallee"],
    "Wimmera": ["wimmera"],
    "Central": ["central"],
}

# AFDRS (Australian Fire Danger Rating System, since Sept 2022). Rank is the
# hierarchy used for comparison: higher = more dangerous. "No Rating" is a real
# published rating (rank 0) — it is NOT the same as "nothing published", which
# is represented by the absence of a rating and never by No Rating.
RATINGS = ["No Rating", "Moderate", "High", "Extreme", "Catastrophic"]
RANK = {r: i for i, r in enumerate(RATINGS)}

# Pre-2022 rating words. Under AFDRS they should never appear; if they do the
# value is treated as unrecognised (review), never mapped onto a new rating —
# "Very High" is not "High", and "Severe" is not "Extreme".
LEGACY_RATINGS = ["low-moderate", "low moderate", "very high", "severe",
                  "code red"]

# AFDRS colours (as used on CFA/AFAC products) for display only.
COLOURS = {
    "No Rating": "#FFFFFF",
    "Moderate": "#64BF30",
    "High": "#FEDD00",
    "Extreme": "#F47E20",
    "Catastrophic": "#AD0909",
}
TEXT_ON = {"No Rating": "#333333", "Moderate": "#000000", "High": "#000000",
           "Extreme": "#000000", "Catastrophic": "#FFFFFF"}

TFB_YES, TFB_NO, TFB_UNKNOWN = "YES", "NO", "UNKNOWN"

# Rating words as they are searched for in free text: longest first so
# "NO RATING" is never read as anything else.
_RATING_RE = re.compile(r"\b(no[\s-]*rating|catastrophic|extreme|high|moderate)\b",
                        re.I)
_QUALIFIER_RE = re.compile(
    r"\b(except|excluding|parts?|areas?|north|south|east|west|until|from|after|"
    r"before|morning|afternoon|evening|am|pm|upgrade\w*|downgrade\w*|amend\w*|"
    r"revis\w*)\b", re.I)
_LEGACY_RE = re.compile(r"\b(" + "|".join(re.escape(x).replace(r"\ ", r"[\s-]*")
                                           for x in LEGACY_RATINGS) + r")\b", re.I)


def _norm(text):
    t = (text or "").replace("&", " and ").replace(" ", " ")
    t = re.sub(r"[^a-z/ -]+", " ", t.lower())
    return re.sub(r"\s+", " ", t).strip()


# Alias -> district, longest alias first (so "north central" wins over
# "central" when both could match).
_ALIAS_LIST = sorted(((_norm(a), d) for d, al in _ALIASES.items() for a in al),
                     key=lambda x: -len(x[0]))


def normalise_district(text):
    """A header/feed label -> canonical district name, or None.

    Exact match on the whole label only (after normalisation), so a header such
    as "Central Comments" is not mistaken for the Central district."""
    n = _norm(text)
    if not n:
        return None
    for alias, district in _ALIAS_LIST:
        if n == alias:
            return district
    return None


def districts_in(text):
    """Every district NAMED inside free text, in order of appearance.

    Longest names are matched first and their span removed, so "North
    Central" never also yields "Central", and "West and South Gippsland" never
    yields "South West". Two-letter aliases (NE, SW) are not searched for in
    free text — too easy to hit by accident."""
    n = " " + _norm(text) + " "
    found = []
    for alias, district in _ALIAS_LIST:
        if len(alias) <= 3:
            continue
        pat = " " + alias + " "
        while pat in n:
            i = n.index(pat)
            found.append((i, district))
            n = n[:i] + " " + "#" * (len(pat) - 2) + " " + n[i + len(pat):]
    seen, out = set(), []
    for _, d in sorted(found):
        if d not in seen:
            seen.add(d)
            out.append(d)
    return out


def parse_ratings(value):
    """Free text -> (ratings, recognised).

    ``ratings`` is every distinct AFDRS rating named in the text, highest
    first. ``recognised`` is False when the text holds something that is not a
    rating (legacy words, a typo, an empty value) — the caller must then treat
    the value as needing review rather than guess."""
    text = (value or "").strip()
    if not text:
        return [], False
    if _LEGACY_RE.search(text):
        return [], False
    found = []
    for m in _RATING_RE.finditer(text):
        word = re.sub(r"[\s-]+", " ", m.group(1).lower())
        r = "No Rating" if word.startswith("no") else word.title()
        if r not in found:
            found.append(r)
    if not found:
        return [], False
    # A trailing remark after a dash or bracket ("HIGH - see cfa.vic.gov.au")
    # does not change the rating it follows — unless it names a rating, or
    # qualifies WHERE or WHEN the rating applies, which needs a human.
    parts = re.split(r"\s+[-–—]\s+|\s*\(", text, maxsplit=1)
    if len(parts) == 2 and not _RATING_RE.search(parts[1]) \
            and not _QUALIFIER_RE.search(parts[1]):
        text = parts[0]
    # Anything left once ratings and harmless joiners are removed means the
    # value says more than we understand.
    rest = _RATING_RE.sub(" ", text)
    rest = re.sub(r"(?i)\b(and|to|or|then|rating|fire danger|fdr)\b", " ", rest)
    rest = re.sub(r"[\s/,&+\-–—:;.()]+", "", rest)
    return sorted(found, key=lambda r: -RANK[r]), rest == ""


def highest(ratings):
    return max(ratings, key=lambda r: RANK[r]) if ratings else None


def rank_of(ratings):
    return RANK[highest(ratings)] if ratings else None


def display(ratings, sep="/"):
    """The ratings as one label, lowest first: "High/Extreme"."""
    return sep.join(sorted(ratings, key=lambda r: RANK[r]))


# --------------------------------------------------------------------------- #
# Time helpers — every stored timestamp carries its offset.
# --------------------------------------------------------------------------- #
def now():
    return datetime.now(TZ)


def today():
    return now().date()


def iso(dt):
    return dt.astimezone(TZ).isoformat(timespec="seconds") if dt else None


def day_start(d):
    """Midnight at the start of a Victorian calendar day (DST-aware)."""
    return datetime.combine(d, time(0, 0), tzinfo=TZ)


def day_end(d):
    return day_start(d + timedelta(days=1))


def as_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def season_of(d):
    """Financial-year fire season label for a date: 1 Jul 2026 -> '2026-27'."""
    start = d.year if d.month >= 7 else d.year - 1
    return f"{start}-{str(start + 1)[2:]}"

"""Extract height->impact tables from Local Flood Guide PDFs (final pass).

Matches each table to a BoM station from the app's Flood Levels.xlsx using
river+location token scoring, with flood-class level agreement as a
tie-breaker, plus manual overrides for hand-checked cases.

Output: lfg_impacts.json seed + lfg_review.txt for manual QA.
"""
import json
import os
import re
import sys
from datetime import date

import fitz
import pandas as pd

LFG_DIR = r"C:\Users\vicvxtq\Downloads\Work\Work\Intel\Tools\Passive Monitor\LFG"
LEVELS_XLSX = r"C:\Users\vicvxtq\Downloads\Work\Work\Intel\Tools\Passive Monitor\unified_monitor\seed\Flood Levels.xlsx"
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "lfg_impacts.json")
REVIEW = os.path.join(HERE, "lfg_review.txt")

HEIGHT_LINE = re.compile(
    r"^\s*(\d{1,3}(?:\.\d{1,3})?)\s*(?:m\b|metres?\b)\s*(?:or)?\s*[\*\^~#]?\s*$", re.I)
NUM_ONLY_LINE = re.compile(r"^\s*(\d{1,3}(?:\.\d{1,3})?)\s*$")
AHD_LINE = re.compile(r"^\s*\d{1,3}(?:\.\d{1,3})?\s*m?\s*AHD\s*$", re.I)
HEIGHT_CELL = re.compile(
    r"^\s*(\d{1,3}(?:\.\d{1,3})?)\s*(?:m\b|metres?\b)?\s*(?:or\s+[\d.]+\s*m?\s*AHD)?\s*[\*\^~#]?\s*$",
    re.I)
CLASS_ROW = re.compile(r"^[\W]*(minor|moderate|major)\s+flood\s+level\b", re.I)

BOILERPLATE = ("floodwater can", "warning means", "vicemergency app",
               "flood warning means", "emergency.vic.gov.au")

LOC_STOPWORDS = {"weir", "reservior", "reservoir", "upstream", "downstream",
                 "hg", "tw", "st", "street", "hwy", "highway", "bridge", "rd",
                 "road", "ave", "gauge", "wharf", "town", "the", "at", "d",
                 "s", "u", "east", "west", "north", "south", "marina",
                 "retarding", "basin", "flinders", "melville"}

# Hand-checked mappings: source PDF -> station name (or None to drop the file).
# Verified by reading the guide text; see session notes.
OVERRIDES = {
    # Two-gauge side-by-side tables (Benalla + Gowangardie columns) parse as
    # garbage; the Benalla gauge table is already covered by the Benalla LFG.
    "Congupna Local Flood Guide LFG.pdf": None,
    "Tallgaroopna Local Flood Guide LFG.pdf": None,
    # Duplicate file
    "Local Flood Guide - Gippsland Lakes (1).pdf": None,
    # Umbrella guide for 5 lake gauges; each gauge has its own site guide.
    "Local Flood Guide - Gippsland Lakes.pdf": None,
    # Heights are at gauges the app doesn't track (no BoM station in the list):
    "Barwon Heads_Ocean Grove Local Flood Guide (LFG).pdf": None,   # Sheepwash Rd gauge
    "Culgoa Local Flood Guide.pdf": None,                            # Warne gauge
    "Dadswells Bridge Local Flood Guide LFG.pdf": None,              # no gauge named
    "Jeparit Local Flood Guide.pdf": None,                           # no gauge named
    "Merri-bek Local Flood Guide LFG.pdf": None,                     # Sussex St gauge
    # Five per-gauge historic tables on mixed datums parse unreliably; the
    # Kerang gauge is covered by the Kerang LFG.
    "Murrabit and Benjeroop Local Flood Guide.pdf": None,
    # 'Carisbrook Gauge on McCallum Creek' is the town's own gauge
    "Carisbrook Local Flood Guide.pdf": "Mccallums Creek at Carisbrook",
    # Police Rd retarding basin table: class levels match Rowville exactly
    "City of Casey Local Flood Guide.pdf": "Dandenong Creek at Rowville",
    # Dual-column table; parsed rows are the Chifley Drive (Maribyrnong) column
    "Moonee Valley Local Flood Guide LFG.pdf": "Maribyrnong River at Maribyrnong",
    # Guide names the 'Loddon River downstream Laanecoorie' gauge = the TW station
    "Serpentine Local Flood Guide LFG.pdf": "Loddon River at Laanecoorie Reservior (TW)",
    "Bridgewater Local Flood Guide LFG.pdf": "Loddon River at Laanecoorie Reservior (TW)",
    # 'the prediction will be based on the Euston Gauge' (metres column = local datum)
    "Robinvale Local Flood Guide LFG.pdf": "Murray River at Euston Weir",
    # No matching BoM station in the app's flood levels list:
    "Apollo Bay Local Flood Guide.pdf": None,
    "Loch Sport Local Flood Guide LFG.pdf": None,
    "Horsham Local Flood Guide LFG.pdf": "Wimmera River at Horsham (Walmer)",
    "Dimboola Local Flood Guide LFG.pdf": "Wimmera River at Dimboola (Upstream)",
    "Shelford Local Flood Guide.pdf": "Leigh River at Shelford (Hwy Bridge)",
    "Lara Local Flood Guide LFG.pdf": "Hovells Creek at Lara (Flinders Ave)",
    "Numurkah Local Flood Guide LFG.pdf": "Broken Creek at Numurkah (Melville St)",
    "Wickliffe Local Flood Guide.pdf": "Hopkins River at Wickliffe",
    "Katamatite Local Flood Guide LFG.pdf": "Broken Creek at Katamatite",
    "Clayton and East Oakleigh Local Flood Guide LFG.pdf":
        "Clayton South Drain at Clayton (Retarding Basin)",
}


def norm(s):
    s = str(s).lower().replace("'", "").replace("’", "")
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", s)).strip()


def load_stations():
    df = pd.read_excel(LEVELS_XLSX)
    stations, seen = [], set()
    for _, row in df.iterrows():
        name = str(row.get("Station Name", "")).strip()
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        n = norm(name)
        if " at " in n:
            river, loc = n.split(" at ", 1)
        else:
            river, loc = n, ""
        loc_tokens = [t for t in loc.split() if t not in LOC_STOPWORDS and len(t) >= 3]
        stations.append({
            "name": name, "key": name.lower(), "norm": n,
            "river": river.strip(), "loc": loc.strip(), "loc_tokens": loc_tokens,
            "minor": pd.to_numeric(row.get("Minor Flood Level"), errors="coerce"),
            "moderate": pd.to_numeric(row.get("Moderate Flood Level"), errors="coerce"),
            "major": pd.to_numeric(row.get("Major Flood Level"), errors="coerce"),
        })
    return stations


def town_from_filename(fname):
    t = os.path.splitext(fname)[0]
    t = re.sub(r"local\s+flood\s+guide?", "", t, flags=re.I)
    t = re.sub(r"\(?\bLFG\b\)?", "", t)
    t = t.strip(" -_")
    t = t.replace("_", " / ")
    return re.sub(r"\s+", " ", t).strip(" -_")


def parse_text_rows(text):
    """Line-based parser for the borderless (old-format) tables. Handles the
    dual-column 'AHD / metres' style by letting a numeric-only line replace a
    height whose impact text hasn't started yet."""
    rows, current = [], None
    for ln in text.splitlines():
        if AHD_LINE.match(ln):
            continue
        hm = HEIGHT_LINE.match(ln)
        nm = NUM_ONLY_LINE.match(ln)
        if hm:
            if current and current[1].strip():
                rows.append(current)
            current = [float(hm.group(1)), ""]
        elif nm and current is not None and not current[1].strip():
            # e.g. Robinvale: '52.43 m' (AHD) then '10.59' (local metres) —
            # the later value is the gauge height BoM reports.
            current = [float(nm.group(1)), ""]
        elif current is not None:
            stripped = ln.strip()
            if stripped:
                current[1] += (" " if current[1] else "") + stripped
    if current and current[1].strip():
        rows.append(current)
    return rows


def parse_tables(page):
    """Bordered tables on a page -> list of (context_text, rows).

    context_text is the page text above the table (plus any non-height header
    cells), which is where the gauge is named — used for per-table station
    association so multi-gauge guides (e.g. Murrabit's five tables) split
    correctly."""
    import fitz as _fitz
    out = []
    try:
        tabs = page.find_tables()
    except Exception:
        return out
    prev_bottom = 0.0
    for t in sorted(tabs, key=lambda t: t.bbox[1]):
        data = t.extract()
        t_rows, header_bits = [], []
        for r in data:
            cells = [re.sub(r"\s+", " ", str(c)).strip() if c else "" for c in r]
            if not cells:
                continue
            hm = HEIGHT_CELL.match(cells[0]) if cells[0] else None
            impact = " ".join(c for c in cells[1:] if c).strip()
            if hm and impact:
                t_rows.append([float(hm.group(1)), impact])
            else:
                header_bits.append(" ".join(c for c in cells if c))
        if not t_rows:
            prev_bottom = max(prev_bottom, t.bbox[3])
            continue
        clip = _fitz.Rect(0, prev_bottom, page.rect.width, t.bbox[1])
        above = page.get_text(clip=clip) if clip.height > 5 else ""
        out.append((above + " " + " ".join(header_bits), t_rows))
        prev_bottom = max(prev_bottom, t.bbox[3])
    return out


# Section headings that leak into the last row of a table; cut the text there.
TRAILING_SECTIONS = re.compile(
    r"\s*(?:2012 flood observations|The following table provide).*$", re.I)

def clean_rows(rows, impact_leading_number=False):
    out, seen = [], set()
    for h, impact in rows:
        impact = re.sub(r"\s+", " ", str(impact)).strip()
        if impact_leading_number:
            # Dual-datum tables (e.g. Robinvale): col0 is metres AHD, the
            # BoM gauge height leads the impact cell — use that instead.
            m = re.match(r"^(\d{1,3}\.\d{1,2})\s*m?\s+(\S.*)$", impact)
            if m and not re.match(r"^\d{3,4}\b", m.group(1)):
                h, impact = float(m.group(1)), m.group(2)
        impact = TRAILING_SECTIONS.sub("", impact).strip()
        low = impact.lower()
        if any(b in low for b in BOILERPLATE):
            continue
        if not (0.1 <= h <= 150) or len(impact) < 4:
            continue
        key = (round(h, 2), low[:40])
        if key in seen:
            continue
        seen.add(key)
        out.append({"height_m": round(h, 2), "impact": impact})
    return out


def class_levels_from_rows(rows):
    levels = {}
    for r in rows:
        m = CLASS_ROW.match(r["impact"])
        if m:
            levels.setdefault(m.group(1).lower(), r["height_m"])
    return levels


def levels_agree(extracted, s, tol=0.15):
    n = 0
    for k in ("minor", "moderate", "major"):
        v, xv = extracted.get(k), s.get(k)
        if v is not None and xv is not None and not pd.isna(xv) and abs(v - float(xv)) <= tol:
            n += 1
    return n


def candidates(text_norm, stations):
    """Stations plausibly referenced by this text."""
    out = []
    for s in stations:
        if len(s["river"]) < 4 or s["river"] not in text_norm:
            continue
        if s["loc_tokens"]:
            if any(t in text_norm for t in s["loc_tokens"]):
                out.append(s)
        elif s["norm"] in text_norm:
            out.append(s)
    return out


def score(s, ext_levels, fname_norm, page_hit):
    sc = 4 * levels_agree(ext_levels, s)
    sc += 2 * all(t in fname_norm for t in s["loc_tokens"][:1]) if s["loc_tokens"] else 0
    if s["loc_tokens"] and any(t in fname_norm for t in s["loc_tokens"]):
        sc += 3
    if page_hit:
        sc += 1
    return sc


def gauge_label_from_text(text):
    pats = [
        r"([A-Z][A-Za-z' ]+?(?:River|Creek|Drain|Aqueduct|Lake|Weir|Break|Strait|Straight))\s+flood levels at (?:the\s+)?([A-Z][\w' \-]+?)(?:\s+gauge|\s*$|\s*\n)",
        r"the\s+([A-Z][\w' \-]+?)\s+gauge\s+(?:at|on|in)\s+(?:the\s+)?([A-Z][A-Za-z' ]+)",
        r"floodwater at the\s+([A-Z][\w' \-]+?)\s+gauge",
    ]
    for p in pats:
        m = re.search(p, text)
        if m:
            return " / ".join(x.strip() for x in m.groups() if x)
    return None


def process_pdf(fname, stations):
    path = os.path.join(LFG_DIR, fname)
    doc = fitz.open(path)
    page_texts = [p.get_text() for p in doc]
    fname_norm = norm(fname)

    override = OVERRIDES.get(fname, "\x00")
    if override is None:
        doc.close()
        return []
    forced = None
    if override != "\x00":
        forced = next(s for s in stations if s["name"] == override)

    # Files whose tables carry a second datum/gauge column: the height BoM
    # reports leads the impact cell, not column 0.
    leading_num = fname in ("Robinvale Local Flood Guide LFG.pdf",
                            "Moonee Valley Local Flood Guide LFG.pdf")
    # Units to associate: bordered tables individually (with their own header
    # context), else the whole page for borderless text-format tables.
    units = []  # (page_no, context_text, rows)
    for i, page in enumerate(doc):
        tables = parse_tables(page)
        good = [(ctx, clean_rows([tuple(r) for r in rows],
                                 impact_leading_number=leading_num))
                for ctx, rows in tables]
        good = [(ctx, rows) for ctx, rows in good if len(rows) >= 2]
        if good:
            units.extend((i, ctx, rows) for ctx, rows in good)
            continue
        rows = [r for r in parse_text_rows(page_texts[i]) if r]
        rows = clean_rows([tuple(r) for r in rows],
                          impact_leading_number=leading_num)
        if len(rows) >= 3:
            units.append((i, page_texts[i], rows))
    doc.close()
    if not units:
        return []

    doc_norm = norm(" ".join(page_texts))
    blocks, order = {}, []
    for pno, ctx, rows in units:
        ext = class_levels_from_rows(rows)
        if forced is not None:
            best = forced
        else:
            best = None
            # Try the unit's own context first, then earlier pages, then doc.
            search_texts = [ctx] + [page_texts[b] for b in range(pno, -1, -1)]
            for si, text in enumerate(search_texts):
                cands = candidates(norm(text), stations)
                if cands:
                    cands.sort(key=lambda s: -score(s, ext, fname_norm, si <= 1))
                    best = cands[0]
                    break
            if best is None:
                cands = candidates(doc_norm, stations)
                if cands:
                    cands.sort(key=lambda s: -score(s, ext, fname_norm, False))
                    best = cands[0]
        label = best["name"] if best else (
            gauge_label_from_text(ctx) or gauge_label_from_text(page_texts[pno]) or "?")
        if label not in blocks:
            blocks[label] = {"rows": [], "station": best}
            order.append(label)
        blocks[label]["rows"].extend(rows)

    results = []
    for label in order:
        b = blocks[label]
        rows = clean_rows([(r["height_m"], r["impact"]) for r in b["rows"]])
        rows.sort(key=lambda r: -r["height_m"])
        results.append({
            "gauge": label,
            "station": b["station"],
            "guide_levels": class_levels_from_rows(rows),
            "impacts": rows,
        })
    return results


def main():
    stations = load_stations()
    guides, skipped = [], []
    for fname in sorted(os.listdir(LFG_DIR)):
        low = fname.lower()
        if not low.endswith(".pdf"):
            continue
        if "local flood guide" not in low and "lfg" not in low:
            continue
        try:
            blocks = process_pdf(fname, stations)
        except Exception as e:
            skipped.append((fname, f"error: {e}"))
            continue
        if not blocks:
            if OVERRIDES.get(fname, "\x00") is None:
                skipped.append((fname, "dropped (override)"))
            else:
                skipped.append((fname, "no impact table found"))
            continue
        town = town_from_filename(fname)
        for gb in blocks:
            st = gb["station"]
            guides.append({
                "source_pdf": fname, "town": town,
                "gauge_name": st["name"] if st else gb["gauge"],
                "station_key": st["key"] if st else None,
                "guide_levels": gb["guide_levels"],
                "impacts": gb["impacts"],
            })

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump({"generated": str(date.today()),
                   "source": "VICSES Local Flood Guides (LFG folder)",
                   "guides": guides}, f, indent=1, ensure_ascii=False)

    # Review file: full dump for manual QA
    with open(REVIEW, "w", encoding="utf-8") as f:
        for g in guides:
            f.write(f"\n{'='*100}\n{g['source_pdf']}  ->  {g['gauge_name']}"
                    f"  (matched={bool(g['station_key'])})\n"
                    f"  town={g['town']}  guide_levels={g['guide_levels']}\n")
            for r in g["impacts"]:
                f.write(f"   {r['height_m']:>7.2f}  {r['impact'][:150]}\n")
        f.write("\n\nSKIPPED:\n")
        for fn, why in skipped:
            f.write(f"  {fn}: {why}\n")

    matched = [g for g in guides if g["station_key"]]
    n_imp = sum(len(g["impacts"]) for g in matched)
    print(f"blocks: {len(guides)}  matched: {len(matched)}  impact rows: {n_imp}")
    print("\nUnmatched:")
    for g in guides:
        if not g["station_key"]:
            print(f"  {g['gauge_name'][:55]:<55} [{g['source_pdf'][:45]}]")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()

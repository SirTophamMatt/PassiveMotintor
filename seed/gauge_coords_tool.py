"""One-off tool: match flood gauges to coordinates from BoM Water Data Online.

Our flood gauges are keyed by their BoM flood-warning station NAME and carry no
coordinates, so they can't be mapped. BoM's Water Data Online KiWIS service
(getStationList) returns every station's name + lat/lon. This tool matches our
gauge names to those stations and writes ``seed/gauge_coords.json`` (loaded into
the ``gauge_coords`` table on boot, same policy as flood levels / LFG impacts).

Why name-matching (not the number on the flood page): BoM's flood-warning
station numbers (e.g. 582015 for "Murray River at Biggara", product IDV67209)
are a DIFFERENT scheme from Water Data Online's AWRC numbers (Biggara = 401012),
so they don't cross-reference. Names do — once the watercourse word ("River"/
"Creek") and "@"/"at" formatting are normalised away.

Matching tiers (most trustworthy first):
  * exact  — normalised key identical                     -> confidence high
  * subset — one side's tokens contain the other's, same
             watercourse + >=2 shared tokens (place
             variants like "Charlton Town" vs "Charlton")  -> confidence medium
  * fuzzy  — difflib ratio >= FUZZY_CUTOFF                 -> confidence low
Unmatched gauges are written with null coords so the manual-fill list is visible.

Usage:
  # fetch KiWIS live (needs outbound HTTPS; works on the server):
  python seed/gauge_coords_tool.py
  # or match against a pre-downloaded station list (KiWIS json array-of-arrays):
  python seed/gauge_coords_tool.py --stations bom_kiwis.json
Options: --db <path> (default: ../unified_monitor.db), --out <path>.
"""
import argparse
import difflib
import json
import os
import re
import sqlite3
from datetime import datetime

KIWIS_URL = ("http://www.bom.gov.au/waterdata/services?service=kisters"
             "&type=queryServices&request=getStationList&format=json&kvp=true"
             "&returnfields=station_no,station_name,station_latitude,"
             "station_longitude")

# Victorian bounding box — keeps matching to VIC stations (a gauge name can
# collide with an interstate one) and shrinks the fuzzy search pool.
VIC_BBOX = (-39.3, -33.8, 140.5, 150.5)  # lat_min, lat_max, lon_min, lon_max
FUZZY_CUTOFF = 0.85

# Watercourse-type + connector words KiWIS drops or abbreviates, so we remove
# them from both sides before comparing ("Aberfeldy River at Beardmore" and
# "ABERFELDY @BEARDMORE" both reduce to "aberfeldy beardmore").
STOP = {"at", "river", "creek", "ck", "r", "rv", "crk", "cr", "riv", "ri",
        "lake", "lk", "drain", "channel", "chl", "weir", "br", "bridge",
        "d", "u", "us", "ds"}

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(os.path.dirname(HERE), "unified_monitor.db")
DEFAULT_OUT = os.path.join(HERE, "gauge_coords.json")


def _toks(s):
    s = s.lower().replace("@", " at ")
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return [t for t in s.split() if t and t not in STOP]


def _key(s):
    return " ".join(_toks(s))


def load_gauges(db_path):
    """(station_key, station_name) for every gauge with flood levels."""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "SELECT station_key, station_name FROM flood_levels "
            "WHERE station_name IS NOT NULL ORDER BY station_name").fetchall()
    finally:
        con.close()
    return rows


def load_stations(path=None):
    """KiWIS stations as a list of (no, name, lat, lon) inside the VIC bbox,
    with coordinates. Fetches live if no path is given."""
    if path:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    else:
        import urllib.request
        req = urllib.request.Request(KIWIS_URL,
                                     headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=120) as resp:
            raw = json.loads(resp.read())
    rows = raw[1:] if raw and raw[0] and raw[0][0] == "station_no" else raw
    la_min, la_max, lo_min, lo_max = VIC_BBOX
    out = []
    for row in rows:
        no, name, la, lo = row[0], row[1], row[2], row[3]
        try:
            la, lo = float(la), float(lo)
        except (TypeError, ValueError):
            continue
        if la_min <= la <= la_max and lo_min <= lo <= lo_max:
            out.append((no, name, la, lo))
    return out


def build_index(stations):
    by_key = {}       # normalised key -> (no, name, lat, lon)
    tokset = {}       # normalised key -> set(tokens)
    for no, name, la, lo in stations:
        k = _key(name)
        if not k:
            continue
        by_key.setdefault(k, (no, name, la, lo))
        tokset.setdefault(k, set(_toks(name)))
    return by_key, tokset


def match_one(gauge_name, by_key, tokset, all_keys):
    gk = _key(gauge_name)
    gt = set(_toks(gauge_name))
    if not gk:
        return None
    # exact
    if gk in by_key:
        return _rec(by_key[gk], "exact", "high", 1.0)
    # token-subset (place variants), requiring the watercourse token to agree
    g0 = _toks(gauge_name)[0] if _toks(gauge_name) else ""
    for k, ts in tokset.items():
        if ts and (gt <= ts or ts <= gt) and g0 in ts and len(gt & ts) >= 2:
            return _rec(by_key[k], "subset", "medium",
                        round(len(gt & ts) / max(len(gt | ts), 1), 2))
    # fuzzy
    m = difflib.get_close_matches(gk, all_keys, n=1, cutoff=FUZZY_CUTOFF)
    if m:
        ratio = difflib.SequenceMatcher(None, gk, m[0]).ratio()
        conf = "medium" if ratio >= 0.92 else "low"
        return _rec(by_key[m[0]], "fuzzy", conf, round(ratio, 3))
    return None


def _rec(station, method, confidence, score):
    no, name, la, lo = station
    return {"kiwis_no": no, "kiwis_name": name, "latitude": la,
            "longitude": lo, "method": method, "confidence": confidence,
            "score": score}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stations", help="Pre-downloaded KiWIS json (array-of-arrays)")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--out", default=DEFAULT_OUT)
    args = ap.parse_args()

    gauges = load_gauges(args.db)
    stations = load_stations(args.stations)
    by_key, tokset = build_index(stations)
    all_keys = list(by_key.keys())
    print(f"Gauges: {len(gauges)}  |  VIC KiWIS stations with coords: {len(stations)}")

    records, counts = [], {"high": 0, "medium": 0, "low": 0, "unmatched": 0}
    for station_key, station_name in gauges:
        m = match_one(station_name, by_key, tokset, all_keys)
        rec = {"station_key": station_key, "station_name": station_name}
        if m:
            rec.update(m)
            counts[m["confidence"]] += 1
        else:
            rec.update({"kiwis_no": None, "kiwis_name": None, "latitude": None,
                        "longitude": None, "method": "unmatched",
                        "confidence": "none", "score": 0})
            counts["unmatched"] += 1
        records.append(rec)

    payload = {
        "generated": datetime.now().isoformat(sep=" ", timespec="seconds"),
        "source": "BoM Water Data Online KiWIS getStationList",
        "note": ("Coordinates matched by station NAME (flood-warning numbers do "
                 "not cross-reference to Water Data Online AWRC numbers). "
                 "Spot-check 'low'/'medium' confidence rows; fill 'unmatched' "
                 "by hand (add latitude/longitude, set confidence 'manual')."),
        "summary": {"total": len(gauges), **counts},
        "gauges": records,
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)

    tot = len(gauges)
    matched = tot - counts["unmatched"]
    print(f"Matched {matched}/{tot} ({100*matched//tot}%): "
          f"high={counts['high']} medium={counts['medium']} low={counts['low']} "
          f"| unmatched={counts['unmatched']}")
    print(f"Wrote {args.out}")


if __name__ == "__main__":
    main()

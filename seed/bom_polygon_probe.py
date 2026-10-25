"""One-off probe: does BoM publish the DRAWN area of a warning as coordinates?

Run it on the server (the dev sandbox cannot reach BoM) WHILE a severe
thunderstorm warning is current, ideally:

    docker compose exec app python seed/bom_polygon_probe.py
    # or, outside Docker:  python seed/bom_polygon_probe.py [out_dir]

It looks in every place BoM might carry a polygon and saves every raw response
to OUT_DIR (default ./bom_probe) so the answer can be checked by hand:

  1. api.weather.bom.gov.au/v1/warnings  + /warnings/{id} for each VIC warning
  2. the warning's XML product   www.bom.gov.au/fwo/<product id>.xml
  3. the FTP warning directory   ftp.bom.gov.au/anon/gen/fwo/  (any file whose
     name starts with the product id — CAP or other variants)

For each response it reports any key/element that looks like geometry
(polygon, coordinates, geometry, circle, area with numbers) and any run of
lat,lon pairs inside Victoria's bounding box. Nothing is written to the DB.
Stdlib only.
"""
import json
import os
import re
import sys
import urllib.request

API = "https://api.weather.bom.gov.au/v1"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
FTP_DIR = "ftp://ftp.bom.gov.au/anon/gen/fwo/"
# Severe thunderstorm (IDV21033 / IDV21037), severe weather (IDV21035), plus
# whatever products the API reports as current.
DEFAULT_PRODUCTS = ["IDV21033", "IDV21037", "IDV21035"]

GEO_KEYS = re.compile(r"polygon|coordinates|geometry|geojson|circle|"
                      r"latitude|longitude|\blat\b|\blon\b|\bwkt\b", re.I)
# "lat,lon" or "lon,lat" pairs inside a generous Victoria box.
PAIR = re.compile(r"(-3[3-9]\.\d+)[ ,]+(1[45]\d\.\d+)|(1[45]\d\.\d+)[ ,]+(-3[3-9]\.\d+)")


_fetched = []


def fetch(url, timeout=30):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read()
    _fetched.append(url)
    return body


def save(out, name, body):
    path = os.path.join(out, re.sub(r"[^\w.-]", "_", name))
    with open(path, "wb") as f:
        f.write(body)
    return path


def json_geo_paths(obj, path=""):
    """Every key path in a JSON document whose key looks like geometry."""
    found = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{path}.{k}"
            if GEO_KEYS.search(str(k)):
                found.append((p, json.dumps(v)[:160]))
            found += json_geo_paths(v, p)
    elif isinstance(obj, list):
        for i, v in enumerate(obj[:50]):
            found += json_geo_paths(v, f"{path}[{i}]")
    return found


def text_report(label, body):
    text = body.decode("utf-8", "replace")
    tags = sorted(set(m.group(0) for m in re.finditer(
        r"<[\w:]*(?:polygon|coordinates|geometry|circle|area)[\w:]*[ >]", text, re.I)))
    pairs = PAIR.findall(text)
    print(f"  {label}: {len(body):,} bytes; geometry-like tags: {tags or 'none'}; "
          f"Victorian lat/lon pairs: {len(pairs)}")
    if pairs:
        print("    first pairs:", [",".join(x for x in p if x) for p in pairs[:4]])
    return bool(tags) or len(pairs) >= 3


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else "bom_probe"
    os.makedirs(out, exist_ok=True)
    hits = []
    products = list(DEFAULT_PRODUCTS)

    print("1. BoM API warnings")
    try:
        body = fetch(API + "/warnings")
        save(out, "api_warnings.json", body)
        warnings = [w for w in json.loads(body).get("data", [])
                    if w.get("state") == "VIC"]
        print(f"  {len(warnings)} VIC warnings")
        for w in warnings:
            wid = w.get("id", "")
            print(f"  - {wid}  {w.get('type')}  {w.get('title')}")
            pid = wid.split(".")[0] if wid else ""
            if re.match(r"IDV\d{5}$", pid) and pid not in products:
                products.append(pid)
            try:
                detail = fetch(f"{API}/warnings/{wid}")
                save(out, f"api_warning_{wid}.json", detail)
                doc = json.loads(detail)
                geo = json_geo_paths(doc)
                for p, v in geo:
                    print(f"      key {p} = {v}")
                if text_report("detail", detail) or geo:
                    hits.append(f"API detail {wid}")
            except Exception as e:
                print(f"      detail failed: {e}")
    except Exception as e:
        print(f"  /warnings failed: {e}")

    print("\n2. Warning XML products")
    for pid in products:
        for url in (f"https://www.bom.gov.au/fwo/{pid}.xml",
                    f"{FTP_DIR}{pid}.xml"):
            try:
                body = fetch(url)
                save(out, url.split("//", 1)[1], body)
                if text_report(url, body):
                    hits.append(url)
            except Exception as e:
                print(f"  {url}: {e}")

    print("\n3. FTP directory variants (CAP etc.)")
    try:
        listing = fetch(FTP_DIR, timeout=60).decode("utf-8", "replace")
        save(out, "ftp_listing.txt", listing.encode())
        names = sorted(set(re.findall(r"(IDV2103\d[\w.]*)", listing)))
        print(f"  files for IDV2103x: {names or 'none'}")
        for name in names:
            if name.endswith(".xml") and name[:8] in products and \
                    name != name[:8] + ".xml":
                try:
                    body = fetch(FTP_DIR + name)
                    save(out, "ftp_" + name, body)
                    if text_report(name, body):
                        hits.append(FTP_DIR + name)
                except Exception as e:
                    print(f"  {name}: {e}")
    except Exception as e:
        print(f"  listing failed: {e}")

    if not _fetched:
        print("\nRESULT: could not reach BoM at all -- no answer (network?)")
    else:
        print("\nRESULT:", ("geometry found in -> " + "; ".join(hits)) if hits
              else f"no coordinates in any of {len(_fetched)} BoM responses")
    print(f"Raw responses saved in {os.path.abspath(out)}/ -- send these over "
          "if the result is unclear.")


if __name__ == "__main__":
    main()

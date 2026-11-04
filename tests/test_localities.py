"""Suburb / locality boundaries (app/localities.py): loading from the sources
it was built for WITHOUT seeing them live, name matching, and the refusals
that keep bad data out."""
import io
import json
import zipfile

import pytest

from app import localities as L


def _square(lon, lat, d=0.02):
    return {"type": "Polygon", "coordinates": [[[lon, lat], [lon + d, lat],
                                                [lon + d, lat + d], [lon, lat + d],
                                                [lon, lat]]]}


def _feature(name, lon, lat, **props):
    return {"type": "Feature", "properties": dict({"SAL_NAME_2021": name,
                                                   "STATE_CODE_2021": "2"}, **props),
            "geometry": _square(lon, lat)}


GOLDEN_BALLARAT = _feature("Golden Point (Ballarat - Vic.)", 143.86, -37.58)
GOLDEN_CASTLEMAINE = _feature("Golden Point (Mount Alexander - Vic.)", 144.25, -37.08)
WENDOUREE = _feature("Wendouree", 143.82, -37.54)
CAMPBELLS_CREEK = _feature("Campbells Creek", 144.20, -37.09)


# --------------------------------------------------------------------------- #
# Names
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("name, key", [
    ("BELGRAVE", "BELGRAVE"), ("Belgrave", "BELGRAVE"),
    ("St Kilda (Vic.)", "SAINT KILDA"), ("ST KILDA", "SAINT KILDA"),
    ("Mt Eliza", "MOUNT ELIZA"), ("MOUNT ELIZA", "MOUNT ELIZA"),
    ("BELGRAVE SOUTH 3160", "BELGRAVE SOUTH"), ("O'Connor", "OCONNOR"),
    ("Golden Point (Ballarat - Vic.)", "GOLDEN POINT"),
    ("Wy Yung", "WY YUNG"), ("Lakes Entrance", "LAKES ENTRANCE"),
])
def test_normalise_is_the_same_on_both_sides(name, key):
    assert L.normalise(name) == key


def test_qualifier_and_display():
    assert L.split_qualifier("Golden Point (Ballarat - Vic.)") == ("Golden Point", "Ballarat")
    assert L.split_qualifier("Richmond (Vic.)") == ("Richmond", "")
    assert L.display_name("MCCRAE") == "McCrae"
    assert L.display_name("BELGRAVE SOUTH 3160") == "Belgrave South"
    assert L.display_name("Golden Point (Ballarat - Vic.)") == "Golden Point (Ballarat)"
    assert L.display_name("St Kilda East") == "St Kilda East"


# --------------------------------------------------------------------------- #
# Features → rows
# --------------------------------------------------------------------------- #
def test_rows_detect_fields_filter_state_and_simplify():
    nsw = _feature("Albury", 146.9, -36.08, STATE_CODE_2021="1")
    detailed = {"type": "Feature", "properties": {"sal_name_2021": "Wiggly",
                                                  "state_code_2021": "2"},
                "geometry": {"type": "Polygon", "coordinates": [[
                    [145.0 + i * 0.0001, -37.0 + (0.00001 if i % 2 else 0)]
                    for i in range(500)] + [[145.05, -36.95], [145.0, -37.0]]]}}
    rows, skipped = L.features_to_rows([WENDOUREE, nsw, detailed], "t", simplify_m=50)
    assert [r["name"] for r in rows] == ["Wendouree", "Wiggly"]
    assert skipped == {"other state": 1}
    wiggly = json.loads(rows[1]["geometry"])
    assert len(wiggly["coordinates"][0]) < 20            # 500 jitter points gone
    flat = [c for ring in wiggly["coordinates"] for pt in ring for c in pt]
    assert all(round(c, 4) == c for c in flat)
    assert rows[0]["area_km2"] == pytest.approx(2.22 * 1.77, rel=0.05)
    assert rows[0]["name_key"] == "WENDOUREE"


def test_unknown_state_code_falls_back_to_the_bbox():
    rows, _ = L.features_to_rows([_feature("Wendouree", 143.82, -37.54, STATE_CODE_2021="VIC-02")])
    assert len(rows) == 1


def test_outside_victoria_dropped_without_a_state_field():
    far = {"type": "Feature", "properties": {"name": "Perth"}, "geometry": _square(115.8, -31.9)}
    near = {"type": "Feature", "properties": {"name": "Ararat"}, "geometry": _square(142.9, -37.3)}
    rows, skipped = L.features_to_rows([far, near])
    assert [r["name"] for r in rows] == ["Ararat"] and skipped == {"outside Victoria": 1}


def test_missing_name_field_names_what_it_saw():
    with pytest.raises(L.LocalityError, match="locality-name property.*colour"):
        L.features_to_rows([{"type": "Feature", "properties": {"colour": "red"},
                             "geometry": _square(145, -37)}])


def test_projected_coordinates_are_refused():
    vicgrid = {"type": "Feature", "properties": {"name": "X"},
               "geometry": {"type": "Polygon", "coordinates": [[
                   [2500000, 2400000], [2501000, 2400000], [2501000, 2401000],
                   [2500000, 2400000]]]}}
    with pytest.raises(L.LocalityError, match="not longitude/latitude"):
        L.features_to_rows([vicgrid])


def test_vicmap_style_duplicates_get_their_lga():
    a = {"type": "Feature", "properties": {"LOCALITY_NAME": "SPRING HILL", "LGA_NAME": "HEPBURN"},
         "geometry": _square(144.3, -37.4)}
    b = {"type": "Feature", "properties": {"LOCALITY_NAME": "SPRING HILL", "LGA_NAME": "MELTON"},
         "geometry": _square(144.6, -37.6)}
    rows, _ = L.features_to_rows([a, b])
    assert sorted(r["display_name"] for r in rows) == ["Spring Hill (Hepburn)",
                                                       "Spring Hill (Melton)"]


def test_esri_rings_with_a_hole():
    outer = [[145.0, -37.0], [145.0, -36.9], [145.1, -36.9], [145.1, -37.0], [145.0, -37.0]]  # cw
    hole = [[145.04, -36.96], [145.06, -36.96], [145.06, -36.94], [145.04, -36.94],
            [145.04, -36.96]]                                                             # ccw
    feats = L.features_of({"features": [{"attributes": {"NAME": "Ring"},
                                         "geometry": {"rings": [outer, hole]}}]})
    assert feats[0]["properties"] == {"NAME": "Ring"}
    assert feats[0]["geometry"]["type"] == "Polygon"
    assert len(feats[0]["geometry"]["coordinates"]) == 2


# --------------------------------------------------------------------------- #
# Storage
# --------------------------------------------------------------------------- #
def test_load_replaces_and_refuses_a_truncated_set(db):
    n, msg = L.load_features([GOLDEN_BALLARAT, GOLDEN_CASTLEMAINE, WENDOUREE,
                              CAMPBELLS_CREEK], "first")
    assert n == 4 and "Loaded 4" in msg
    with pytest.raises(L.LocalityError, match="not replacing"):
        L.load_features([WENDOUREE], "partial")
    assert L.count() == 4
    L.load_features([WENDOUREE], "forced", force=True)
    assert L.count() == 1 and L.status()["source"] == "forced"


def test_no_victorian_polygons_is_an_error(db):
    with pytest.raises(L.LocalityError, match="No Victorian locality polygons"):
        L.load_features([_feature("Albury", 146.9, -36.08, STATE_CODE_2021="1"),
                         _feature("Hobart", 147.3, -42.9, STATE_CODE_2021="6")], "x")


# --------------------------------------------------------------------------- #
# Matching
# --------------------------------------------------------------------------- #
@pytest.fixture
def loaded(db):
    L.load_features([GOLDEN_BALLARAT, GOLDEN_CASTLEMAINE, WENDOUREE,
                     CAMPBELLS_CREEK], "test")


def test_resolve_unique_unmatched_and_no_data(db):
    assert L.resolve(["WENDOUREE"])[0].status == L.NO_DATA
    L.load_features([WENDOUREE], "t")
    m, missing = L.resolve(["WENDOUREE", "NOWHERE"])
    assert m.status == L.MATCHED and m.display == "Wendouree"
    assert missing.status == L.UNMATCHED and missing.id is None


def test_shared_name_decided_by_the_outages_other_suburbs(loaded):
    near_ballarat = L.resolve(["GOLDEN POINT", "WENDOUREE"])[0]
    near_castlemaine = L.resolve(["CAMPBELLS CREEK", "GOLDEN POINT"])[1]
    assert near_ballarat.display == "Golden Point (Ballarat)"
    assert near_castlemaine.display == "Golden Point (Mount Alexander)"
    assert near_ballarat.candidates == 2


def test_shared_name_alone_uses_the_geocode_or_stays_ambiguous(loaded):
    alone = L.resolve(["GOLDEN POINT"])[0]
    assert alone.status == L.AMBIGUOUS and alone.id is None
    hinted = L.resolve(["GOLDEN POINT"], hints={"GOLDEN POINT": (-37.07, 144.26)})[0]
    assert hinted.display == "Golden Point (Mount Alexander)"


def test_listed_qualifier_narrows(loaded):
    m = L.resolve(["Golden Point (Ballarat)"])[0]
    assert m.status == L.MATCHED and m.display == "Golden Point (Ballarat)"


def test_attach_groups_by_outage(loaded):
    import pandas as pd
    df = pd.DataFrame([
        {"location": "GOLDEN POINT", "latitude": None, "longitude": None},
        {"location": "WENDOUREE", "latitude": None, "longitude": None},
        {"location": "GOLDEN POINT", "latitude": None, "longitude": None},
        {"location": "CAMPBELLS CREEK", "latitude": None, "longitude": None}])
    out = L.attach(df, ["one", "one", "two", "two"])
    assert list(out["locality_name"]) == ["Golden Point (Ballarat)", "Wendouree",
                                         "Golden Point (Mount Alexander)",
                                         "Campbells Creek"]
    assert out.iloc[0]["locality_geometry"]["type"] == "Polygon"


def test_cache_follows_a_reload(loaded):
    assert L.resolve(["WENDOUREE"])[0].status == L.MATCHED
    L.load_features([CAMPBELLS_CREEK], "other", force=True)
    assert L.resolve(["WENDOUREE"])[0].status == L.UNMATCHED


def test_bounds_view_frames_the_shapes():
    center, zoom = L.bounds_view([_square(145.0, -37.0, 0.05)])
    assert center["lat"] == pytest.approx(-36.975, abs=0.01)
    assert 9 < zoom < 13
    assert L.bounds_view([]) == (None, None)


# --------------------------------------------------------------------------- #
# Files
# --------------------------------------------------------------------------- #
def test_import_geojson_file(db, tmp_path):
    path = tmp_path / "localities.geojson"
    path.write_text(json.dumps({"type": "FeatureCollection",
                                "features": [WENDOUREE, CAMPBELLS_CREEK]}))
    n, _ = L.import_path(str(path))
    assert n == 2 and L.status()["source"] == "localities.geojson"


def _shapefile_zip(path, prj=None):
    import shapefile
    shp, shx, dbf = io.BytesIO(), io.BytesIO(), io.BytesIO()
    with shapefile.Writer(shp=shp, shx=shx, dbf=dbf, shapeType=shapefile.POLYGON) as w:
        w.field("LOCALITY_N", "C", 60)
        for name, lon, lat in (("BELGRAVE", 145.35, -37.92), ("TECOMA", 145.34, -37.90)):
            w.poly([[[lon, lat], [lon, lat + 0.02], [lon + 0.02, lat + 0.02],
                     [lon + 0.02, lat], [lon, lat]]])
            w.record(name)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("VMADMIN/locality_polygon.shp", shp.getvalue())
        z.writestr("VMADMIN/locality_polygon.shx", shx.getvalue())
        z.writestr("VMADMIN/locality_polygon.dbf", dbf.getvalue())
        if prj:
            z.writestr("VMADMIN/locality_polygon.prj", prj)


def test_import_zipped_shapefile(db, tmp_path):
    path = tmp_path / "vicmap.zip"
    _shapefile_zip(path, prj='GEOGCS["GDA2020",DATUM["GDA2020"]]')
    n, _ = L.import_path(str(path))       # LOCALITY_N: the .dbf-truncated name
    assert n == 2
    assert L.resolve(["Belgrave"])[0].display == "Belgrave"


def test_name_field_override():
    feat = {"type": "Feature", "properties": {"LABEL_TXT": "Ararat", "name": "wrong"},
            "geometry": _square(142.9, -37.3)}
    rows, _ = L.features_to_rows([feat], name_field="LABEL_TXT")
    assert rows[0]["name"] == "Ararat"


def test_projected_shapefile_refused(db, tmp_path):
    path = tmp_path / "mga.zip"
    _shapefile_zip(path, prj='PROJCS["GDA2020 / MGA zone 55",GEOGCS["GDA2020"]]')
    with pytest.raises(L.LocalityError, match="MGA zone 55"):
        L.import_path(str(path))


def test_unsupported_file(tmp_path):
    p = tmp_path / "x.kml"
    p.write_text("<kml/>")
    with pytest.raises(L.LocalityError, match="Unsupported"):
        L.read_file(str(p))


# --------------------------------------------------------------------------- #
# Fetching (no network: urlopen is replaced)
# --------------------------------------------------------------------------- #
class _Resp:
    def __init__(self, body):
        self.body = body if isinstance(body, bytes) else json.dumps(body).encode()

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _fake_urlopen(handler, calls):
    def urlopen(req, timeout=None):
        url = req.full_url
        calls.append(url)
        return _Resp(handler(url))
    return urlopen


ARCGIS = {"source": "arcgis", "arcgis_url": "https://geo.example/arcgis/rest/services/SAL/MapServer/0",
          "arcgis_where": "state_code_2021 = '2'", "batch_size": 2,
          "timeout_seconds": 5, "simplify_m": 50, "name_field": ""}


def test_fetch_arcgis_ids_then_batches(db, monkeypatch):
    feats = {1: WENDOUREE, 2: CAMPBELLS_CREEK, 3: GOLDEN_BALLARAT}

    def handler(url):
        if "returnIdsOnly=true" in url:
            assert "where=state_code_2021" in url
            return {"objectIdFieldName": "OBJECTID", "objectIds": [3, 1, 2]}
        assert "outSR=4326" in url and "f=geojson" in url
        ids = [int(x) for x in url.split("objectIds=")[1].split("&")[0].split("%2C")]
        return {"type": "FeatureCollection", "features": [feats[i] for i in ids]}
    calls = []
    monkeypatch.setattr(L.urllib.request, "urlopen", _fake_urlopen(handler, calls))
    n, msg = L.fetch_and_load(ARCGIS)
    assert n == 3 and len(calls) == 3                # ids + 2 batches of 2
    assert "geo.example" in msg


def test_fetch_arcgis_falls_back_to_esri_json(db, monkeypatch):
    ring = [[143.82, -37.54], [143.82, -37.52], [143.84, -37.52], [143.84, -37.54],
            [143.82, -37.54]]

    def handler(url):
        if "returnIdsOnly=true" in url:
            return {"objectIds": [1]}
        if "f=geojson" in url:
            return {"error": {"code": 400, "message": "Invalid format"}}
        return {"features": [{"attributes": {"SAL_NAME_2021": "Wendouree"},
                              "geometry": {"rings": [ring]}}]}
    monkeypatch.setattr(L.urllib.request, "urlopen", _fake_urlopen(handler, []))
    n, _ = L.fetch_and_load(ARCGIS)
    assert n == 1


def test_fetch_arcgis_reports_a_refused_query(db, monkeypatch):
    monkeypatch.setattr(L.urllib.request, "urlopen", _fake_urlopen(
        lambda url: {"error": {"code": 400, "message": "Unable to complete operation.",
                               "details": ["Invalid field: state_code_2021"]}}, []))
    with pytest.raises(L.LocalityError, match="Invalid field.*arcgis_where"):
        L.fetch_and_load(ARCGIS)


def test_fetch_non_json_saved_for_diagnosis(db, monkeypatch):
    monkeypatch.setattr(L.urllib.request, "urlopen",
                        _fake_urlopen(lambda url: b"<html>maintenance</html>", []))
    with pytest.raises(L.LocalityError, match="localities_debug_arcgis"):
        L.fetch_and_load(ARCGIS)
    with open(L._debug_path("arcgis"), "rb") as f:
        assert b"maintenance" in f.read()


def test_fetch_geojson_follows_wfs_paging(db, monkeypatch):
    pages = {0: [WENDOUREE, CAMPBELLS_CREEK], 2: [GOLDEN_BALLARAT]}

    def handler(url):
        start = int(url.split("startIndex=")[1]) if "startIndex=" in url else 0
        return {"type": "FeatureCollection", "numberMatched": 3,
                "numberReturned": len(pages[start]), "features": pages[start]}
    calls = []
    monkeypatch.setattr(L.urllib.request, "urlopen", _fake_urlopen(handler, calls))
    n, _ = L.fetch_and_load({"source": "geojson", "simplify_m": 50,
                             "geojson_url": "https://wfs.example/wfs?typeNames=x",
                             "timeout_seconds": 5, "name_field": ""})
    assert n == 3 and len(calls) == 2


def test_run_reports_failure_without_raising(db, monkeypatch):
    monkeypatch.setattr(L.urllib.request, "urlopen", _fake_urlopen(
        lambda url: (_ for _ in ()).throw(OSError("blocked")), []))
    ok, msg = L.run(L.fetch_and_load, ARCGIS)
    assert not ok and "Could not reach geo.example" in msg
    assert L.status()["last_error"] == msg


def test_admin_status_not_shown_to_anonymous(db):
    from app.factory import create_app
    client = create_app(autostart=False).server.test_client()
    r = client.post("/_dash-update-component", json={
        "output": "admin-localities-status.children",
        "outputs": {"id": "admin-localities-status", "property": "children"},
        "inputs": [{"id": "admin-localities-fetch", "property": "n_clicks", "value": 1},
                   {"id": "admin-status-interval", "property": "n_intervals", "value": 0}],
        "changedPropIds": ["admin-localities-fetch.n_clicks"], "state": []})
    assert r.status_code in (200, 204)
    assert "Downloading" not in r.get_data(as_text=True)
    assert not L.status()["running"]

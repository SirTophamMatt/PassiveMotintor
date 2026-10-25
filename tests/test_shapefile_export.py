"""Shapefile export for EM-COP's layer upload (.shp/.shx/.dbf/.prj + .sld)."""
import io
import zipfile

import pytest
import shapefile
from shapely.geometry import LinearRing, shape

from app.modules.storm import data as storm_data
from app.modules.weather import data as weather_data
from app.modules.weather import warning_area as wa
from app.shapefile_export import (PRJ_WGS84, Field, Layer, ascii_text,
                                  layer_files, zip_layers)
from tests.test_warning_area import (ISSUE, MESSAGE, _vicemergency_copy,
                                     _warning)

SQUARE = [[146.0, -36.0], [147.0, -36.0], [147.0, -37.0], [146.0, -37.0],
          [146.0, -36.0]]                      # anticlockwise (GeoJSON order)
HOLE = [[146.4, -36.4], [146.4, -36.6], [146.6, -36.6], [146.6, -36.4],
        [146.4, -36.4]]
FIELDS = [Field("NAME", "C", 40, lambda p: p.get("name")),
          Field("AREA_KM2", "N", 12, lambda p: p.get("km2"), decimals=1)]


def _feature(geometry, **props):
    return {"type": "Feature", "geometry": geometry, "properties": props}


def _read(files, name):
    return shapefile.Reader(shp=io.BytesIO(files[f"{name}.shp"]),
                            shx=io.BytesIO(files[f"{name}.shx"]),
                            dbf=io.BytesIO(files[f"{name}.dbf"]))


def _unzip(data):
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        return {n: z.read(n) for n in z.namelist()}


# --- the writer -------------------------------------------------------------
def test_one_layer_is_the_five_files_em_cop_asks_for():
    files = layer_files(Layer("areas", [_feature(
        {"type": "Polygon", "coordinates": [SQUARE]}, name="A", km2=12.34)], FIELDS))
    assert sorted(files) == ["areas.dbf", "areas.prj", "areas.shp", "areas.shx",
                             "areas.sld"]
    assert files["areas.prj"].decode() == PRJ_WGS84
    r = _read(files, "areas")
    assert r.shapeType == shapefile.POLYGON and len(r) == 1
    assert r.record(0)["NAME"] == "A" and r.record(0)["AREA_KM2"] == 12.3
    assert shape(r.shape(0).__geo_interface__).equals(
        shape({"type": "Polygon", "coordinates": [SQUARE]}))


def test_rings_follow_the_shapefile_winding_rule():
    files = layer_files(Layer("a", [_feature(
        {"type": "Polygon", "coordinates": [SQUARE, HOLE[::-1]]})], FIELDS))
    s = _read(files, "a").shape(0)
    outer = s.points[:s.parts[1]]
    hole = s.points[s.parts[1]:]
    assert not LinearRing(outer).is_ccw      # outer clockwise
    assert LinearRing(hole).is_ccw           # hole anticlockwise


def test_multipolygon_and_collection_parts():
    far = [[c[0] + 2, c[1]] for c in SQUARE]
    coll = {"type": "GeometryCollection", "geometries": [
        {"type": "Polygon", "coordinates": [SQUARE]},
        {"type": "Polygon", "coordinates": [[[149.9, -37.5], [149.9, -37.5]]]},
        {"type": "Point", "coordinates": [146.9, -36.8]}]}
    files = layer_files(Layer("a", [
        _feature({"type": "MultiPolygon", "coordinates": [[SQUARE], [far]]}),
        _feature(coll)], FIELDS))
    r = _read(files, "a")
    assert len(r) == 2
    assert len(r.shape(0).parts) == 2 and len(r.shape(1).parts) == 1


def test_a_layer_with_nothing_to_draw_writes_nothing():
    point = _feature({"type": "Point", "coordinates": [146, -36]})
    assert layer_files(Layer("a", [point, _feature(None)], FIELDS)) == {}
    assert zip_layers([Layer("a", [point], FIELDS)], "now") is None


def test_text_is_ascii_one_line_and_cut_to_size():
    assert ascii_text("Whole districts — drawn ~16,273 km² ≥ x", 100) == \
        "Whole districts - drawn ~16,273 km2 >= x"
    assert ascii_text("a\nb\t c", 10) == "a b c"
    assert ascii_text("x" * 300, 254) == "x" * 254
    assert ascii_text(None, 5) == ""


def test_numbers_tolerate_missing_and_nan():
    files = layer_files(Layer("a", [_feature(
        {"type": "Polygon", "coordinates": [SQUARE]}, km2=float("nan"))], FIELDS))
    assert _read(files, "a").record(0)["AREA_KM2"] is None


def test_zip_has_a_readme_naming_each_box():
    data = zip_layers([Layer("areas", [_feature(
        {"type": "Polygon", "coordinates": [SQUARE]})], FIELDS, title="T"),
        Layer("empty", [], FIELDS, title="E")], "2026-10-25 12:00")
    files = _unzip(data)
    assert "areas.shp" in files and not any(n.startswith("empty") for n in files)
    readme = files["README.txt"].decode()
    assert "shx file    -> <layer>.shx" in readme
    assert "areas: T (1 area)" in readme and "empty: none right now" in readme


def test_gdal_reads_it_back():
    pyogrio = pytest.importorskip("pyogrio")
    import tempfile
    files = layer_files(Layer("areas", [_feature(
        {"type": "Polygon", "coordinates": [SQUARE, HOLE]}, name="A", km2=1.5)],
        FIELDS))
    with tempfile.TemporaryDirectory() as d:
        for name, data in files.items():
            with open(f"{d}/{name}", "wb") as fh:
                fh.write(data)
        info = pyogrio.read_info(f"{d}/areas.shp")
        assert info["geometry_type"] == "Polygon" and info["features"] == 1
        assert "4326" in info["crs"]
        assert list(info["fields"]) == ["NAME", "AREA_KM2"]


# --- BoM warning areas --------------------------------------------------------
def _two_warnings():
    _warning(); wa.record("IDV21033", ISSUE, MESSAGE, ISSUE)
    _warning("IDV21037"); _vicemergency_copy("IDV21037")


def test_all_warnings_drawn_and_districts_are_separate_layers(db):
    _two_warnings()
    files = _unzip(weather_data.warning_areas_shapefile(generated="now"))
    drawn = _read(files, "bom_warning_areas")
    districts = _read(files, "bom_warning_districts")
    assert [r["WARN_ID"] for r in drawn.records()] == ["IDV21033"]
    assert [r["WARN_ID"] for r in districts.records()] == ["IDV21037"]
    rec = drawn.record(0)
    assert rec["AREA_SRC"] == "traced" and rec["AREA_KM2"] > 15_000
    assert rec["ISSUED"] == "2026-10-25 10:16"
    assert "not available" in districts.record(0)["NOTE"]
    assert b"stroke-dasharray" in files["bom_warning_districts.sld"]
    assert b"stroke-dasharray" not in files["bom_warning_areas.sld"]


def test_each_warning_has_its_own_shapefile_named_for_its_source(db):
    _two_warnings()
    one = _unzip(weather_data.warning_areas_shapefile(warning_id="IDV21033"))
    assert {n for n in one if n != "README.txt"} == {
        f"IDV21033_drawn.{e}" for e in ("shp", "shx", "dbf", "prj", "sld")}
    assert [r["WARN_ID"] for r in _read(one, "IDV21033_drawn").records()] == \
        ["IDV21033"]
    other = _unzip(weather_data.warning_areas_shapefile(warning_id="IDV21037"))
    assert "IDV21037_districts.shp" in other


def test_unknown_or_unsafe_warning_ids_get_nothing(db):
    _two_warnings()
    for wid in ("IDV99999", "../etc/passwd", "IDV21033.zip\r\nX: y", ""):
        assert weather_data.warning_areas_shapefile(warning_id=wid) is None


def test_no_areas_no_zip(db):
    _warning("IDV29000")
    assert weather_data.warning_areas_shapefile() is None


def test_routes_serve_the_zips(db):
    import dash

    from app.pages import weather as page
    _two_warnings()
    app = dash.Dash(__name__)
    app.layout = dash.html.Div()
    page.register_callbacks(app)
    client = app.server.test_client()
    all_ = client.get(page.AREAS_SHAPEFILE_ROUTE)
    assert all_.status_code == 200 and all_.mimetype == "application/zip"
    assert "attachment" in all_.headers["Content-Disposition"]
    one = client.get(page.AREA_SHAPEFILE_PREFIX + "IDV21033.zip")
    assert one.status_code == 200
    assert "IDV21033_drawn.shp" in _unzip(one.data)
    assert "IDV21033_" in one.headers["Content-Disposition"]
    assert client.get(page.AREA_SHAPEFILE_PREFIX + "IDV00000.zip").status_code == 404


def test_each_warning_line_links_its_own_shapefile(db):
    from app.pages import weather as page
    _two_warnings()
    _warning("IDV29000")
    df = weather_data.current_warning_areas()
    links = {}
    for _, r in df.iterrows():
        line = page._area_line(r)
        anchors = [c for c in line.children if hasattr(c, "href")]
        links[r["warning_id"]] = anchors[0].href if anchors else None
    assert links == {"IDV21033": "/weather/warning-areas/IDV21033.zip",
                     "IDV21037": "/weather/warning-areas/IDV21037.zip",
                     "IDV29000": None}


# --- storm impact areas -------------------------------------------------------
def test_storm_impact_areas_export(db, monkeypatch):
    feature = _feature({"type": "Polygon", "coordinates": [SQUARE]}, cell_id="c1",
                       radar_id="IDR023", classification="strong",
                       intensity_score=55.27, area_km2=120.0, speed_kmh=None,
                       bearing_deg=45, valid_from="2026-10-25 12:00:00",
                       projection_minutes=30)
    monkeypatch.setattr(storm_data, "impact_featurecollection", lambda: {
        "type": "FeatureCollection", "generated": "2026-10-25 12:00:00",
        "features": [feature]})
    files = _unzip(storm_data.impact_shapefile())
    rec = _read(files, "storm_impact_areas").record(0)
    assert rec["CLASS"] == "strong" and rec["SCORE"] == 55.3
    assert rec["SPEED_KMH"] is None and rec["BEARING"] == 45
    monkeypatch.setattr(storm_data, "impact_featurecollection", lambda: {
        "type": "FeatureCollection", "generated": "x", "features": []})
    assert storm_data.impact_shapefile() is None

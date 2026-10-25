"""BoM warning areas traced from the warning's map image.

Fixture: the 25 Oct 2026 10:16 Severe Thunderstorm Warning (IDV21033) map,
North East + East Gippsland. Its text names Wodonga, Corryong, Bright, Falls
Creek, Mt Hotham and Omeo.
"""
import base64
import json

import cv2
import numpy as np
import pytest
from shapely.geometry import Point, shape

from app import database, history
from app.modules.weather import data as weather_data
from app.modules.weather import scraper as weather_scraper
from app.modules.weather import warning_area as wa
from tests.conftest import FIXTURE_DIR, load_fixture

MAP = "bom_warning_map_IDV21033.png"
ISSUE = "2026-10-25 10:16:00"


def _img():
    return cv2.imread(f"{FIXTURE_DIR}/{MAP}")


def _html(*images):
    """A warning message embedding the given PNG bytes, as BoM sends it."""
    tags = "".join('<img src="data:image/png;base64,%s"/>'
                   % base64.b64encode(raw).decode() for raw in images)
    return "<p>Severe thunderstorms are likely ...</p>" + tags


def _png(img):
    return cv2.imencode(".png", img)[1].tobytes()


MESSAGE = _html(load_fixture(MAP))


# --- calibration ------------------------------------------------------------
def test_reference_map_calibrates_against_the_town_dots():
    cal = wa.calibrate(_img())
    assert cal is not None
    assert len(cal.towns) >= 35
    assert cal.rms_px < 1.0          # ~1.3 km per pixel
    assert {"Melbourne", "Wodonga", "Mildura", "Orbost"} <= set(cal.towns)


def test_calibration_round_trips_a_town():
    cal = wa.calibrate(_img())
    x, y = cal.to_px(-37.814, 144.963)          # Melbourne
    lon, lat = cal.to_lonlat(x, y)
    assert abs(lon - 144.963) < 1e-9 and abs(lat - -37.814) < 1e-9


@pytest.mark.parametrize("scale", [0.75, 1.5, 2.0])
def test_a_resized_map_still_calibrates_and_traces_the_same_area(scale):
    img = cv2.resize(_img(), None, fx=scale, fy=scale,
                     interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_NEAREST)
    cal = wa.calibrate(img)
    assert cal is not None
    area = wa.area_km2(wa.trace_image(img, cal))
    assert 14_500 < area < 17_500


def test_a_reframed_map_is_refused_not_traced_against_the_wrong_frame():
    assert wa.calibrate(_img()[40:, 30:]) is None


def test_a_picture_that_is_not_the_map_is_refused():
    assert wa.calibrate(np.full((400, 600, 3), 255, np.uint8)) is None


# --- tracing ----------------------------------------------------------------
def test_traces_the_drawn_area():
    res = wa.trace_message(MESSAGE)
    assert res.status == wa.TRACED
    assert res.geometry["type"] == "Polygon"
    assert 15_500 < res.area_km2 < 17_000
    assert res.towns >= 35 and res.images == 1


def test_the_towns_the_warning_names_are_inside_and_others_are_not():
    area = shape(wa.trace_message(MESSAGE).geometry)
    for lat, lon in [(-36.196, 147.902), (-36.729, 146.961), (-36.866, 147.278),
                     (-36.976, 147.134), (-37.099, 147.597)]:  # Corryong..Omeo
        assert area.contains(Point(lon, lat))
    for lat, lon in [(-36.358, 146.313), (-37.052, 146.088), (-37.457, 147.253),
                     (-37.823, 147.611), (-37.814, 144.963)]:  # Wangaratta..Melb
        assert not area.contains(Point(lon, lat))
    # Wodonga is on the Murray, which is the area's northern edge.
    assert area.exterior.distance(Point(146.888, -36.121)) < 0.02


def test_the_legend_swatch_is_not_part_of_the_area():
    minx, miny, maxx, maxy = shape(wa.trace_message(MESSAGE).geometry).bounds
    assert minx > 146 and miny > -37.5   # the swatch sits off Portland


def test_far_smaller_than_the_districts_it_names():
    # VicEmergency's copy is the whole North East + East Gippsland districts,
    # ~44,700 km2; the drawn area is about a third of that.
    assert wa.trace_message(MESSAGE).area_km2 < 20_000


def test_no_image_no_map():
    assert wa.trace_message("<p>Sheep graziers warning</p>").status == wa.NO_MAP
    assert wa.trace_message(None).status == wa.NO_MAP
    assert wa.trace_message('<img src="data:image/png;base64,!!!!">').status == wa.NO_MAP


def test_image_that_is_not_a_map_is_uncalibrated():
    res = wa.trace_message(_html(_png(np.full((300, 300, 3), 255, np.uint8))))
    assert res.status == wa.UNCALIBRATED and res.images == 1


def test_calibrated_map_with_no_fill_is_no_area():
    img = _img()
    yellow = wa._colour_mask(img, wa.FILL_RGB, wa.FILL_TOL).astype(bool)
    img[yellow] = (212, 242, 253)              # repaint as land
    res = wa.trace_message(_html(_png(img)))
    assert res.status == wa.NO_AREA and res.towns >= 35


def test_a_logo_beside_the_map_does_not_stop_the_trace():
    logo = _png(np.full((60, 60, 3), (41, 221, 250), np.uint8))
    res = wa.trace_message(_html(logo, load_fixture(MAP)))
    assert res.status == wa.TRACED and res.images == 2


# --- storage + collector ----------------------------------------------------
def _areas():
    return database.read_df("SELECT * FROM weather_warning_areas")


def test_record_stores_one_row_per_version_and_is_idempotent(db):
    wa.record("IDV21033", ISSUE, MESSAGE, ISSUE)
    wa.record("IDV21033", ISSUE, MESSAGE, ISSUE)
    df = _areas()
    assert len(df) == 1
    row = df.iloc[0]
    assert row["status"] == wa.TRACED and row["method"] == wa.METHOD
    assert json.loads(row["geojson"])["type"] == "Polygon"


def test_record_never_raises(db, monkeypatch):
    def boom(_):
        raise RuntimeError("bad image")
    monkeypatch.setattr(wa, "trace_message", boom)
    assert wa.record("IDV21033", ISSUE, MESSAGE, ISSUE).status == wa.ERROR
    assert _areas().iloc[0]["status"] == wa.ERROR


def test_collector_traces_a_new_issue_once(db, monkeypatch):
    detail = {"data": {"message": MESSAGE, "issue_time": "2026-10-24T23:16:06Z",
                       "phase": "new", "title": "Severe Thunderstorm Warning"}}
    monkeypatch.setattr(weather_scraper, "_get", lambda path: detail)
    weather_scraper._store_detail("IDV21033", ISSUE)
    weather_scraper._store_detail("IDV21033", ISSUE)   # same issue again
    df = _areas()
    assert len(df) == 1 and df.iloc[0]["status"] == wa.TRACED


def test_backfill_traces_versions_stored_before_tracing(db):
    database.insert_rows("weather_warning_updates", [
        {"warning_id": "IDV21033", "issue_time": ISSUE, "message": MESSAGE},
        {"warning_id": "IDV21033", "issue_time": "2026-10-25 09:00:00",
         "message": "<p>no map</p>"}])
    assert wa.backfill(ISSUE) == 2
    assert wa.backfill(ISSUE) == 0
    assert sorted(_areas()["status"]) == [wa.NO_MAP, wa.TRACED]


# --- data: current areas + export -------------------------------------------
def _warning(wid="IDV21033", issue=ISSUE):
    database.insert_rows("weather_warnings", [{
        "warning_id": wid, "type": "severe_thunderstorm_warning",
        "title": "Severe Thunderstorm Warning", "issue_time": issue,
        "expiry_time": "2026-10-25 16:15:00", "active": 1}])


DISTRICTS = {"type": "GeometryCollection", "geometries": [
    {"type": "Polygon", "coordinates": [[[146, -36], [148, -36], [148, -37.6],
                                         [146, -37.6], [146, -36]]]},
    {"type": "Polygon", "coordinates": [[[149.9, -37.5], [149.9, -37.5]]]},
    {"type": "Point", "coordinates": [146.9, -36.8]}]}


def _vicemergency_copy(wid="IDV21033"):
    database.insert_rows("fire_incidents", [{
        "source_id": wid + "/892e8db67d86", "feed_type": "incident",
        "category1": "Met", "source_org": "AU/BOM",
        "geometry": json.dumps(DISTRICTS), "resolved": 0}])


def test_current_area_is_the_traced_one(db):
    _warning(); _vicemergency_copy()
    wa.record("IDV21033", ISSUE, MESSAGE, ISSUE)
    row = weather_data.current_warning_areas().iloc[0]
    assert row["area_source"] == weather_data.AREA_TRACED
    assert json.loads(row["geojson"])["type"] == "Polygon"


def test_falls_back_to_districts_only_when_not_traced(db):
    _warning(); _vicemergency_copy()
    wa.record("IDV21033", ISSUE, "<p>no map</p>", ISSUE)
    row = weather_data.current_warning_areas().iloc[0]
    assert row["area_source"] == weather_data.AREA_DISTRICTS
    geom = json.loads(row["geojson"])
    assert geom["type"] == "MultiPolygon"
    assert len(geom["coordinates"]) == 1     # degenerate ring + point dropped
    assert row["detail"] == "the warning carries no map image"


def test_a_warning_with_neither_has_no_area(db):
    _warning()
    row = weather_data.current_warning_areas().iloc[0]
    assert row["area_source"] is None and row["geojson"] is None


def test_featurecollection_labels_where_each_area_came_from(db):
    _warning(); wa.record("IDV21033", ISSUE, MESSAGE, ISSUE)
    _warning("IDV21037"); _vicemergency_copy("IDV21037")
    _warning("IDV29000")                      # no area at all: left out
    fc = weather_data.warning_areas_featurecollection()
    props = {f["properties"]["warning_id"]: f["properties"] for f in fc["features"]}
    assert set(props) == {"IDV21033", "IDV21037"}
    assert props["IDV21033"]["area_source"] == "traced"
    assert props["IDV21033"]["area_km2"] > 15_000
    assert props["IDV21037"]["area_source"] == "districts"
    assert props["IDV21037"]["area_km2"] is None
    assert "not available" in props["IDV21037"]["area_note"]
    json.dumps(fc)  # serialisable as-is


# --- map --------------------------------------------------------------------
def test_map_layer_draws_traced_solid_and_districts_dashed(db):
    from app.pages import unified
    _warning(); wa.record("IDV21033", ISSUE, MESSAGE, ISSUE)
    _warning("IDV21037"); _vicemergency_copy("IDV21037")
    traces, fills = unified.render_bomwarn(weather_data.current_warning_areas())
    assert [f["type"] for f in fills] == ["fill", "line", "fill", "line"]
    assert "dash" not in fills[1]["line"] and fills[3]["line"]["dash"]
    assert fills[0]["opacity"] > fills[2]["opacity"]
    names = {t.name for t in traces}
    assert names == {"BoM warning area", "BoM warning (whole districts)"}


def test_fire_layer_no_longer_fills_bom_district_polygons():
    from app.pages import unified
    import pandas as pd
    df = pd.DataFrame([{"feed_type": "incident", "category1": "Met",
                        "category2": "Thunderstorm", "latitude": -36.8,
                        "longitude": 146.9, "geometry": json.dumps(DISTRICTS)}])
    _, fills = unified.render_fire(df)
    assert fills == []


def test_bomwarn_is_a_default_layer_with_a_replay_provider():
    from app import replay
    from app.pages import unified
    assert "bomwarn" in unified.DEFAULT_LAYERS
    assert "bomwarn" in unified.RENDERERS and "bomwarn" in replay.LAYER_PROVIDERS


# --- replay -----------------------------------------------------------------
def test_replay_shows_the_area_of_the_issue_current_at_the_moment(db):
    from app import replay
    later = "2026-10-25 12:30:00"
    for issue in (ISSUE, later):
        history.record_state(history.WEATHER_WARNING, "IDV21033",
                             {"title": "Severe Thunderstorm Warning",
                              "type": "severe_thunderstorm_warning",
                              "issue_time": issue}, effective_ts=issue)
    wa.record("IDV21033", ISSUE, MESSAGE, ISSUE)
    wa.record("IDV21033", later, "<p>no map</p>", later)

    at_11 = replay.bomwarn_at("2026-10-25 11:00:00")
    assert len(at_11) == 1 and at_11.iloc[0]["issue_time"] == ISSUE
    # 13:00: the current issue had no traceable map, so nothing is drawn — the
    # 10:16 area is NOT carried forward as if it were still the drawn area.
    assert replay.bomwarn_at("2026-10-25 13:00:00").empty
    assert replay.bomwarn_at("2026-10-25 10:00:00").empty   # not issued yet

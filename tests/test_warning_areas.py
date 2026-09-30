"""Warning areas on every map: denser fill + outline, no centre dot, and a
pulse layer for warnings first seen in the last few minutes."""
import json
import os
from datetime import datetime, timedelta

import pandas as pd

from app.pages import fire as fire_page
from app.pages import unified

NOW = datetime(2026, 10, 1, 12, 0, 0)
ASSETS = os.path.join(os.path.dirname(__file__), "..", "assets")


def _area(lon=145.0, lat=-37.0, d=0.2):
    ring = [[lon - d, lat - d], [lon + d, lat - d], [lon + d, lat + d],
            [lon - d, lat + d], [lon - d, lat - d]]
    # VicEmergency wraps the area and its point in a GeometryCollection.
    return json.dumps({"type": "GeometryCollection", "geometries": [
        {"type": "Point", "coordinates": [lon, lat]},
        {"type": "Polygon", "coordinates": [ring]}]})


def _row(sid, level=None, geometry=None, first_seen=NOW - timedelta(hours=2),
         category="Flooding"):
    warning = level is not None
    return {"source_id": sid, "feed_type": "warning" if warning else "incident",
            "warning_level": level, "category1": level or category,
            "location": sid.title(), "latitude": -37.0, "longitude": 145.0,
            "geometry": geometry, "first_seen": first_seen, "status": None,
            "size": None}


def _frame(*rows):
    df = pd.DataFrame(list(rows))
    df["Kind"] = df.apply(fire_page._kind, axis=1)
    return df


def test_only_warnings_with_an_area_lose_their_dot():
    df = _frame(_row("area", "Advice", _area()), _row("pointonly", "Advice"),
                _row("fire", None, _area(), category="Fire"))
    hidden = df.apply(fire_page.hidden_point, axis=1).tolist()
    assert hidden == [True, False, False]     # incidents always keep theirs
    assert not fire_page.has_area(json.dumps({"type": "Point",
                                              "coordinates": [145, -37]}))
    assert not fire_page.has_area("not json")


def test_warning_areas_get_a_denser_fill_and_an_outline():
    df = _frame(_row("a", "Watch and Act", _area()))
    layers = fire_page.warning_area_layers(df, now=NOW)
    assert [l["type"] for l in layers] == ["fill", "line"]
    fill, line = layers
    assert fill["opacity"] == fire_page.WARNING_FILL_OPACITY > 0.25
    assert line["line"]["width"] == fire_page.WARNING_LINE_WIDTH >= 3
    assert fill["color"] == line["color"] == fire_page.KIND_COLOURS["Watch and Act"]
    assert "name" not in fill                  # an old warning does not pulse


def test_a_new_warning_gets_its_own_pulse_layers_until_the_deadline():
    seen = NOW - timedelta(seconds=40)
    df = _frame(_row("old", "Advice", _area()),
                _row("new", "Emergency Warning", _area(), first_seen=seen))
    layers = fire_page.warning_area_layers(df, now=NOW)
    pulsing = [l for l in layers if l.get("name", "").startswith(fire_page.PULSE_PREFIX)]
    assert [l["type"] for l in pulsing] == ["fill", "line"]
    deadline = int(pulsing[0]["name"][len(fire_page.PULSE_PREFIX):])
    assert deadline == int((seen + timedelta(seconds=fire_page.PULSE_SECONDS))
                           .timestamp() * 1000)
    # Past the window it is drawn like any other warning.
    later = NOW + timedelta(seconds=fire_page.PULSE_SECONDS)
    assert not any("name" in l for l in fire_page.warning_area_layers(df, now=later))


def test_pulse_can_be_switched_off_and_tolerates_missing_first_seen():
    df = _frame(_row("new", "Advice", _area(), first_seen=NOW))
    assert not any("name" in l for l in
                   fire_page.warning_area_layers(df, now=NOW, pulse=False))
    assert fire_page.warning_area_layers(df.drop(columns=["first_seen"]), now=NOW)


def test_unified_map_hides_area_dots_but_keeps_hover_and_legend():
    df = pd.DataFrame([_row("area", "Advice", _area()),
                       _row("fire", None, None, category="Fire")])
    traces, fills = unified.render_fire(df)
    advice = [t for t in traces if t.name == "Advice"]
    hover = [t for t in advice if t.marker.opacity == 0]
    legend = [t for t in advice if t.showlegend is not False]
    assert hover and hover[0].text and hover[0].showlegend is False
    assert legend and legend[0].marker.opacity is None   # a solid swatch
    fire = [t for t in traces if t.name == "Fire"]
    assert fire and fire[0].marker.opacity is None
    assert {"fill", "line"} <= {f["type"] for f in fills}


def test_fire_page_map_hides_area_dots_too():
    df = pd.DataFrame([_row("area", "Watch and Act", _area()),
                       _row("fire", None, None, category="Fire")])
    fig = fire_page._map_figure(df, dark=True)
    visible = [t for t in fig.data if getattr(t.marker, "opacity", None) != 0]
    assert all("Watch and Act" != t.name for t in visible)
    assert any(getattr(t.marker, "opacity", None) == 0 for t in fig.data)
    assert any(l.type == "line" for l in fig.layout.map.layers)


def test_pulse_script_matches_the_server_contract():
    js = open(os.path.join(ASSETS, "map_pulse.js"), encoding="utf-8").read()
    assert f'"{fire_page.PULSE_PREFIX}"' in js
    assert "plotly-layout-layer-" in js
    assert "prefers-reduced-motion" in js

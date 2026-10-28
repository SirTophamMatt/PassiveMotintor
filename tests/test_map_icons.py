"""Map iconography: the icon catalogue, the classifiers that pick an icon, the
renderers that use them, and the browser/PDF plumbing that makes them draw."""
import json
import os
import xml.etree.ElementTree as ET

import pandas as pd
import plotly.graph_objects as go
import pytest

from app import map_icons
from app.pages import fire as fire_page
from app.pages import fire_wall, flood_wall, map_key, roads as roads_page, storm_wall
from app.pages import unified

HERE = os.path.dirname(__file__)
ASSETS = os.path.join(HERE, "..", "assets")


def _symbols(traces):
    out = set()
    for t in traces:
        sym = getattr(t.marker, "symbol", None) if t.marker else None
        if isinstance(sym, str):
            out.add(sym)
        elif sym is not None:
            out.update(sym)
    return {s for s in out if s and s.startswith(map_icons.PREFIX)}


# --- catalogue -----------------------------------------------------------------

def test_every_icon_is_well_formed_svg():
    assert len(map_icons.ICONS) > 40
    for name in map_icons.ICONS:
        assert name.startswith(map_icons.PREFIX)
        root = ET.fromstring(map_icons.svg(name))      # parses, i.e. no format slip
        assert root.get("viewBox") == "0 0 32 32"
        assert "{" not in map_icons.svg(name)          # every placeholder filled


def test_every_icon_belongs_to_a_listed_group():
    titles = [t for t, _ in map_icons.GROUPS]
    assert len(titles) == len(set(titles))
    for spec in map_icons.ICONS.values():
        assert spec["group"] in titles


def test_glyph_ink_is_dark_on_light_fills():
    # Advice yellow and Watch-and-Act orange carry a dark glyph, like the AWS
    # signage; Emergency red keeps white.
    assert map_icons.ICONS["wd-warn-advice-fire"]["ink"] == map_icons.INK_DARK
    assert map_icons.ICONS["wd-warn-watchact-fire"]["ink"] == map_icons.INK_DARK
    assert map_icons.ICONS["wd-warn-emergency-fire"]["ink"] == map_icons.INK_LIGHT


def test_catalogue_json_round_trips():
    data = json.loads(map_icons.catalogue_json())
    assert set(data) == set(map_icons.ICONS)


# --- classification -----------------------------------------------------------

@pytest.mark.parametrize("cat1, cat2, icon", [
    ("Fire", "Bushfire", "wd-inc-fire"),
    ("Fire", "Grass Fire", "wd-inc-fire"),
    ("Fire", "Building Fire", "wd-inc-structure-fire"),
    ("Fire", "Structure Fire", "wd-inc-structure-fire"),
    ("Fire", "Burn Off", "wd-inc-burn-off"),
    ("Fire", "Planned Burn", "wd-inc-burn-off"),
    ("Fire", "Vehicle Fire", "wd-inc-vehicle-fire"),
    ("Fire", "Non Structure Fire", "wd-inc-fire-other"),
    ("Fire", None, "wd-inc-fire"),
    ("Tree Down", "Tree Down", "wd-inc-tree"),
    ("Accident / Rescue", "Road Accident", "wd-inc-accident"),
    ("Rescue", "Rescue Vehicle Into Structure", "wd-inc-rescue"),
    ("Hazardous Material", "Hazardous Material", "wd-inc-hazmat"),
    ("Flooding", "Flooding", "wd-inc-flood"),
    ("Building Damage", "Roof", "wd-inc-damage"),
    ("Alarm", "Fire Alarm", "wd-inc-alarm"),       # an alarm is never a fire
    ("Medical", "Medical", "wd-inc-medical"),
    ("Met", "Strong Wind", "wd-inc-met"),
    ("Other", "Other", "wd-inc-other"),
])
def test_incident_icon(cat1, cat2, icon):
    assert map_icons.incident_icon({"category1": cat1, "category2": cat2}) == icon


def test_incident_icon_follows_the_map_kind():
    # The map's Kind wins: a non-fire row the page classed as Met is weather.
    row = {"category1": "Other", "category2": "Strong Wind"}
    assert map_icons.incident_icon(row, "Met (weather)") == "wd-inc-met"


def test_every_real_feed_incident_has_an_icon():
    with open(os.path.join(HERE, "fixtures", "osom_incidents_sample.json")) as f:
        data = json.load(f)
    feats = data["features"] if isinstance(data, dict) else data
    for feat in feats:
        icon = map_icons.incident_icon(feat.get("properties", feat))
        assert icon in map_icons.ICONS


@pytest.mark.parametrize("row, hazard", [
    ({"event": "Bushfire"}, "fire"),
    ({"category2": "Riverine Flood"}, "flood"),
    ({"event": "Severe Thunderstorm"}, "storm"),
    ({"event": "Extreme Heat"}, "heat"),
    ({"event": "Smoke"}, "fire"),
    ({"event": "Tsunami"}, "flood"),
    ({"event": "Earthquake"}, "other"),
    # The hazard fields win over free text: a flood warning for "Fire Station
    # Rd" stays a flood warning (same rule as fire_areas.is_fire_warning).
    ({"event": "Flood", "headline": "Fire Station Rd"}, "flood"),
    ({"headline": "Grass fire near Avoca"}, "fire"),
    ({}, "other"),
])
def test_warning_hazard(row, hazard):
    assert map_icons.warning_hazard(row) == hazard


def test_warning_icon_names_exist_for_every_level_and_hazard():
    for level in map_icons.LEVEL_COLOURS:
        icon = map_icons.warning_icon(level, {"event": "Flood"})
        assert icon in map_icons.ICONS and icon.endswith("-flood")
    assert map_icons.warning_icon("Watch and Act", pd.Series({"event": "Fire"})) \
        == "wd-warn-watchact-fire"


def test_gauge_and_band_icons():
    assert map_icons.gauge_icon("Major flooding") == "wd-gauge-major"
    assert map_icons.gauge_icon("Below flood level") == "wd-gauge-below"
    assert map_icons.rain_icon(0.2) == "wd-rain-0"
    assert map_icons.rain_icon(15) == "wd-rain-2"
    assert map_icons.rain_icon(120) == "wd-rain-4"
    assert map_icons.gust_band(40) == 0 and map_icons.gust_band(99) == 2
    assert map_icons.gust_band(130) == 3


def test_downwind_degrees():
    # A northerly blows TOWARDS the south.
    assert map_icons.downwind_degrees("N") == 180
    assert map_icons.downwind_degrees("SW") == 45
    assert map_icons.downwind_degrees("nnw") == pytest.approx(157.5)
    assert map_icons.downwind_degrees("CALM") is None
    assert map_icons.downwind_degrees(None) is None


# --- renderers ----------------------------------------------------------------

def _fire_frame():
    rows = [
        dict(feed_type="incident", category1="Fire", category2="Bushfire"),
        dict(feed_type="incident", category1="Fire", category2="Burn Off"),
        dict(feed_type="incident", category1="Tree Down", category2="Tree Down"),
        dict(feed_type="incident", category1="Met", category2="Strong Wind"),
        dict(feed_type="warning", warning_level="Emergency Warning", event="Flood"),
        dict(feed_type="warning", warning_level="Advice", event="Bushfire"),
    ]
    for i, r in enumerate(rows):
        r.update(latitude=-37 - i * 0.1, longitude=145.0, location="P%d" % i,
                 geometry=None, status=None, size=None)
    return pd.DataFrame(rows)


def test_unified_fire_layer_uses_typed_icons():
    traces, _ = unified.render_fire(_fire_frame())
    syms = _symbols(traces)
    assert {"wd-inc-fire", "wd-inc-burn-off", "wd-inc-tree", "wd-inc-met",
            "wd-warn-emergency-flood", "wd-warn-advice-fire"} <= syms
    # The legend swatch stays the KIND colour even when one point (a burn-off)
    # has its own icon colour.
    fire = next(t for t in traces if t.name == "Fire")
    assert fire.marker.color == fire_page.KIND_COLOURS["Fire"]


def test_fire_page_map_uses_the_same_icons():
    fig = fire_page._map_figure(_fire_frame(), dark=False)
    assert {"wd-inc-fire", "wd-warn-emergency-flood"} <= _symbols(fig.data)


def _all_layer_frames():
    line = json.dumps({"type": "LineString",
                       "coordinates": [[145.0, -37.0], [145.1, -37.1], [145.2, -37.2]]})
    return {
        "fire": _fire_frame(),
        "flood": pd.DataFrame([dict(label=l, latitude=-37, longitude=145,
                                    station_name="G", height_m=1.0)
                               for l in ("Major flooding", "Moderate flooding",
                                         "Minor flooding", "Below flood level")]),
        "roads": pd.DataFrame([
            dict(is_closure=1, geometry=line, latitude=None, longitude=None,
                 road_name="A", location="X", disruption_type="Flooding"),
            dict(is_closure=0, geometry=None, latitude=-37.5, longitude=145.5,
                 road_name="B", location="Y", disruption_type="Roadworks")]),
        "storm": pd.DataFrame([dict(classification=c, latitude=-37, longitude=145,
                                    cell_id=c, intensity_score=1, area_km2=10,
                                    impact_geojson=None)
                               for c in ("strong", "moderate", "weak")]),
        "power": pd.DataFrame([dict(latitude=-37, longitude=145, customers_off=c,
                                    location="L", type="Unplanned")
                               for c in (5, 500, 5000)]),
        "rain": pd.DataFrame([dict(latitude=-37, longitude=145, rain_since_9am_mm=r,
                                   name="S") for r in (1, 8, 20, 40, 70)]),
        "wind": pd.DataFrame([dict(latitude=-37, longitude=145, wind_gust_kmh=g,
                                   wind_direction=d, wind_speed_kmh=20, name="S",
                                   obs_time="t")
                              for g, d in ((45, "N"), (70, "CALM"), (110, "W"))]),
        "bomwarn": None,
    }


def test_every_icon_a_renderer_draws_is_in_the_catalogue():
    frames = _all_layer_frames()
    traces, _ = unified.build_layers(list(unified.RENDERERS), source=frames.get)
    used = _symbols(traces)
    assert used <= set(map_icons.ICONS)
    for expected in ("wd-gauge-major", "wd-road-closure", "wd-road-other",
                     "wd-storm-strong", "wd-power-outage", "wd-rain-4",
                     "wd-gust-0", "wd-gust-1-calm", "wd-gust-3"):
        assert expected in used


def test_icon_traces_have_one_size_and_overlap():
    # Plotly divides marker.size by 10 for an icon: an array would be NaN, and
    # without allowoverlap MapLibre hides icons that collide.
    frames = _all_layer_frames()
    traces, _ = unified.build_layers(list(unified.RENDERERS), source=frames.get)
    icon_traces = [t for t in traces if map_icons.is_icon_trace(t)]
    assert icon_traces
    for t in icon_traces:
        assert isinstance(t.marker.size, (int, float))
        assert t.marker.allowoverlap is True


def test_power_icons_grow_with_outage_size():
    traces, _ = unified.render_power(_all_layer_frames()["power"])
    sizes = [t.marker.size for t in traces]
    assert sizes == sorted(sizes, reverse=True) and len(set(sizes)) == 3
    assert [t.showlegend for t in traces].count(True) == 1


def test_wind_arrows_point_downwind():
    traces, _ = unified.render_wind(_all_layer_frames()["wind"])
    by_symbol = {t.marker.symbol: t for t in traces}
    assert by_symbol["wd-gust-0"].marker.angle == (180.0,)   # northerly -> south
    assert by_symbol["wd-gust-1-calm"].marker.angle is None   # no fake heading


def test_closed_road_gets_a_sign_on_the_road():
    traces, _ = unified.render_roads(_all_layer_frames()["roads"])
    sign = next(t for t in traces if t.marker and t.marker.symbol == "wd-road-closure")
    assert sign.lat == (-37.1,) and sign.lon == (145.1,)      # the middle vertex
    assert sign.showlegend is False                           # the line has it


def test_line_midpoint():
    assert roads_page.line_midpoint([]) is None
    assert roads_page.line_midpoint([[[1, 2]], [[3, 4], [5, 6], [7, 8]]]) == (5.0, 6.0)


# --- static (PDF) fallback ---------------------------------------------------

def test_static_figure_turns_icons_into_coloured_dots():
    fig = fire_page._map_figure(_fire_frame(), dark=False)
    map_icons.static_figure(fig)
    assert not any(map_icons.is_icon_trace(t) for t in fig.data)
    fire = next(t for t in fig.data if t.name == "Fire")
    assert fire.marker.symbol == "circle"
    # Each point keeps its own icon colour (the burn-off is not fire-orange).
    assert map_icons.BURN_OFF in fire.marker.color


def test_static_figure_leaves_plain_figures_alone():
    fig = go.Figure(go.Scattermap(lat=[1], lon=[2], marker=dict(size=9, color="red")))
    map_icons.static_figure(fig)
    assert fig.data[0].marker.size == 9 and fig.data[0].marker.symbol is None


def test_pdf_renderer_flattens_icons(monkeypatch):
    from app import reporting
    seen = {}

    def fake_to_image(self, **kw):
        seen["icons"] = any(map_icons.is_icon_trace(t) for t in self.data)
        return b"png"
    monkeypatch.setattr(go.Figure, "to_image", fake_to_image)
    reporting._fig_png(fire_page._map_figure(_fire_frame(), dark=False))
    assert seen == {"icons": False}


# --- browser plumbing + key ---------------------------------------------------

def test_icons_are_inlined_into_every_page():
    from app.factory import create_app
    index = create_app().index_string
    assert "window.WD_MAP_ICONS=" in index
    start = index.index("window.WD_MAP_ICONS=") + len("window.WD_MAP_ICONS=")
    payload = index[start:index.index(";</script>", start)]
    assert "</" not in payload                      # cannot close the <script>
    assert set(json.loads(payload)) == set(map_icons.ICONS)
    assert index.index("WD_MAP_ICONS") < index.index("{%scripts%}")


def test_registrar_script_reads_the_inlined_icons():
    with open(os.path.join(ASSETS, "map_icons.js")) as f:
        js = f.read()
    assert "window.WD_MAP_ICONS" in js
    assert "styleimagemissing" in js and "addImage" in js
    assert "pixelRatio: 2" in js and map_icons.RASTER_PX == 64


def _strings(component):
    if isinstance(component, str):
        yield component
    elif isinstance(component, (list, tuple)):
        for c in component:
            yield from _strings(c)
    elif hasattr(component, "children"):
        yield from _strings(component.children)


def test_map_key_lists_every_icon():
    text = "\n".join(_strings(map_key.layout()))
    for name, spec in map_icons.ICONS.items():
        assert spec["label"] in text, name


def test_wall_chip_icons_exist():
    for layers in (flood_wall.LAYERS, fire_wall.LAYERS, storm_wall.LAYERS):
        for _key, _label, _colour, shape in layers:
            if shape.startswith(map_icons.PREFIX):
                assert shape in map_icons.ICONS

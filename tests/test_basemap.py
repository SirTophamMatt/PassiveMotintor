"""Wall map light/dark choice (app/basemap.py) and its wiring into the walls."""
from datetime import date, datetime

import pytest

from app import basemap, sound_alerts
from app.pages import fire_wall, flood_wall, news_wall, storm_wall, wall


def _hm(dt):
    return dt.hour * 60 + dt.minute


@pytest.mark.parametrize("day, rise, sset", [
    # Published Melbourne times (Geoscience Australia), local clock.
    (date(2026, 6, 21), "07:35", "17:08"),     # winter solstice, AEST
    (date(2026, 12, 21), "05:54", "20:41"),    # summer solstice, AEDT
    (date(2026, 10, 22), "06:26", "19:44"),
])
def test_melbourne_sun_times_within_two_minutes(day, rise, sset):
    r, s = basemap.sun_times(day)
    for got, want in ((r, rise), (s, sset)):
        h, m = map(int, want.split(":"))
        assert abs(_hm(got) - (h * 60 + m)) <= 2, (got, want)


def test_sun_mode_is_dark_only_between_sunset_and_sunrise():
    noon = datetime(2026, 10, 22, 12, 0)
    late = datetime(2026, 10, 22, 22, 0)
    early = datetime(2026, 10, 22, 5, 0)
    assert basemap.style(basemap.SUN, False, noon) == basemap.LIGHT_STYLE
    assert basemap.style(basemap.SUN, True, noon) == basemap.LIGHT_STYLE   # theme ignored
    assert basemap.style(basemap.SUN, False, late) == basemap.DARK_STYLE
    assert basemap.style(basemap.SUN, False, early) == basemap.DARK_STYLE


def test_fixed_and_theme_modes():
    assert basemap.style(basemap.LIGHT, True) == basemap.LIGHT_STYLE
    assert basemap.style(basemap.DARK, False) == basemap.DARK_STYLE
    assert basemap.style(basemap.THEME, True) == basemap.DARK_STYLE
    assert basemap.style(basemap.THEME, False) == basemap.LIGHT_STYLE
    # An unknown or not-yet-restored value keeps the map as it always was.
    assert basemap.style(None, True) == basemap.LIGHT_STYLE


def test_options_cover_every_mode():
    assert [o["value"] for o in basemap.options()] == [
        basemap.LIGHT, basemap.DARK, basemap.THEME, basemap.SUN]


def _json(component):
    import json

    import plotly
    return json.dumps(component, cls=plotly.utils.PlotlyJSONEncoder)


@pytest.mark.parametrize("layout, control", [
    (wall.layout, "wall-map-style"),
    (flood_wall.layout, "fw-map-style"),
    (fire_wall.layout, "fiw-map-style"),
    (storm_wall.layout, "stw-map-style"),
    (news_wall.layout, "nw-map-style"),
])
def test_every_wall_has_a_map_option(layout, control, db):
    from dash import Dash
    Dash(__name__)                 # the wall header resolves asset URLs
    assert f'"id": "{control}"' in _json(layout())


def test_map_figure_uses_the_chosen_basemap(db):
    from app.pages import unified
    fig = unified.map_figure([], False, basemap=basemap.DARK_STYLE)
    assert fig.layout.map.style == basemap.DARK_STYLE
    assert unified.map_figure([], True).layout.map.style == basemap.LIGHT_STYLE


def test_story_map_carries_the_style(db):
    entry = {"id": "x", "latitude": -37.8, "longitude": 145.0}
    fig = news_wall.story_map(entry, False, style=basemap.DARK_STYLE)
    assert fig.layout.map.style == basemap.DARK_STYLE


def test_every_sound_category_has_a_play_button():
    import re
    found = re.findall(r'"data-sound": "(\w+)"', _json(sound_alerts.toggle_button()))
    assert found == [value for value, _ in sound_alerts.CATEGORIES]

# Passive Monitor — Copyright (c) 2026 SirTophamMatt. All rights reserved.
"""Map key (public, /map/key): every icon, line and area the maps draw.

Rendered straight from the icon catalogue (`app/map_icons.py`), so the key can
never list an icon the maps don't use or miss one they do.
"""
from dash import html

from app import map_icons

PATH = "/map/key"

# How to read a symbol — the grammar, with one example icon each.
GRAMMAR = [
    ("wd-warn-watchact-other", "Triangle", "A warning — Advice, Watch and Act or "
     "Emergency Warning, in its level's colour."),
    ("wd-inc-other", "Circle", "An incident: something happening at a place."),
    ("wd-gauge-minor", "Square", "A monitored asset: a flood gauge or the "
     "power network."),
    ("wd-storm-strong", "Hexagon", "A storm cell tracked on radar."),
    ("wd-road-closure", "Road signs", "Red no-entry disc = road closed; amber "
     "diamond = other disruption."),
    ("wd-gust-2", "Drop / arrow", "A weather observation: rain since 9am, or a "
     "gust arrow pointing the way the wind blows."),
]

# Lines and areas: (swatch style, label).
AREAS = [
    ({"background": "rgba(214,39,40,0.35)", "border": "2px solid #d62728"},
     "Warning area — fill and outline in the warning level's colour. A new "
     "warning's area breathes for its first 3 minutes."),
    ({"background": "rgba(250,221,41,0.35)", "border": "2px solid #b8860b"},
     "BoM warning area, as drawn by the Bureau"),
    ({"background": "rgba(250,221,41,0.08)", "border": "2px dashed #9a7d0a"},
     "BoM warning — whole forecast districts (the drawn area was not available)"),
    ({"background": "#d62728", "height": "5px", "borderRadius": "3px"},
     "Road closed — the closed section of road"),
    ({"background": "#ff7f0e", "height": "3px", "borderRadius": "2px"},
     "Other road disruption — the affected section of road"),
    ({"background": "rgba(214,39,40,0.15)", "border": "1px solid #d62728"},
     "Storm cell impact area — where the cell is projected to pass"),
    ({"background": "rgba(255,87,34,0.25)", "border": "1px solid #ff5722"},
     "Fire / incident area published by the feed"),
    ({"background": "rgba(141,110,99,0.35)"},
     "Burn area (historical DELWP / DEECA footprint)"),
]


def _icon_row(name, px=30, label=None):
    return html.Div([map_icons.icon_img(name, px),
                     html.Span(label or map_icons.ICONS[name]["label"])],
                    className="mapkey-item")


def layout():
    groups = []
    for title, note in map_icons.GROUPS:
        names = [n for n, spec in map_icons.ICONS.items() if spec["group"] == title]
        groups.append(html.Div([
            html.H3(title),
            html.P(note, className="muted"),
            html.Div([_icon_row(n) for n in names], className="mapkey-grid"),
        ], className="graph-card mapkey-group"))
    return html.Div([
        html.H2("Map key"),
        html.P(["Every map in Watchdesk uses one set of symbols. The ",
                html.B("shape"), " says what kind of thing it is, the ",
                html.B("colour"), " is its level or class, and the ",
                html.B("symbol inside"), " is the hazard or incident type. "
                "Hover any marker on a map for its details."], className="muted"),
        html.Div([html.Div([map_icons.icon_img(n, 34),
                            html.Div([html.B(shape), html.Div(text, className="muted")])],
                           className="mapkey-item")
                  for n, shape, text in GRAMMAR],
                 className="graph-card mapkey-grid mapkey-grammar"),
        *groups,
        html.Div([
            html.H3("Lines and areas"),
            html.Div([html.Div([html.Span(className="mapkey-swatch", style=style),
                                html.Span(label)], className="mapkey-item")
                      for style, label in AREAS], className="mapkey-grid"),
        ], className="graph-card mapkey-group"),
        html.P("Symbols are drawn for Watchdesk and are not the official "
               "VicEmergency or Bureau of Meteorology icons. Warning colours "
               "follow the Australian Warning System.", className="muted",
               style={"fontSize": "12px"}),
    ], className="mapkey-page")

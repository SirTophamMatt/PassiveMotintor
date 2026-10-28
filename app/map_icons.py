# Passive Monitor — Copyright (c) 2026 SirTophamMatt. All rights reserved.
"""Map iconography: one icon set for every marker on every map.

The grammar is fixed so a symbol can be read before its colour is:

    SHAPE  = what kind of thing it is
        triangle  warning (the Australian Warning System motif)
        circle    incident (something happening at a place)
        square    monitored asset — flood gauge, power network
        hexagon   radar storm cell
        road signs: red no-entry disc = closure, amber diamond = disruption
        drop / arrow  weather observations (rain, wind gust — the arrow points
                      the way the wind is blowing)
    COLOUR = the level or class it already had (AWS red/orange/yellow, flood
             class, storm class) — never changed by the icon
    GLYPH  = the hazard (flame, waves, tree, car, storm cloud …)

All drawings are original, licence-clean SVG. They reach the map without a
Mapbox token: a Scattermap trace names an icon in `marker.symbol`, Plotly turns
that into a MapLibre symbol layer asking for the image by name, and
`assets/map_icons.js` registers every icon on every map (the SVGs are inlined
into the page by `factory`, from `catalogue_json()`). An unregistered image
draws NOTHING, so anything rendered without the browser script — the kaleido
PDF maps — goes through `static_figure()`, which turns icons back into dots.

Plotly draws the icon at `marker.size / 10` × the image's natural size; the
images are 64 px at pixelRatio 2 = 32 CSS px, so `marker()` takes the size in
CSS pixels and converts. A symbol trace needs ONE size per trace (Plotly
divides marker.size by 10 — an array would become NaN), so layers that scale
by magnitude bucket into a trace per size.
"""
import base64
import json
import re

PREFIX = "wd-"
RASTER_PX = 64      # image pixels per icon (pixelRatio 2 → 32 CSS px)
NATURAL_PX = 32     # CSS px at marker.size 10

INK_DARK = "#1b1b1b"
INK_LIGHT = "#ffffff"

# --- shapes (32 x 32 viewBox) --------------------------------------------------
# Each is (outline path, glyph box (cx, cy, size)). The glyph box is where a
# 24-unit glyph is drawn — lower in a triangle, whose visual centre is low.
_SHAPES = {
    "triangle": ('<path d="M16 2.6 30.2 28.4H1.8Z"/>', (16, 19.6, 12.5)),
    "circle": ('<circle cx="16" cy="16" r="13.4"/>', (16, 16, 15.5)),
    "square": ('<rect x="2.8" y="2.8" width="26.4" height="26.4" rx="5.5"/>',
               (16, 16, 16.5)),
    "diamond": ('<path d="M16 1.6 30.4 16 16 30.4 1.6 16Z"/>', (16, 16, 12.5)),
    "hexagon": ('<path d="M16 2 28.6 9.2V22.8L16 30 3.4 22.8V9.2Z"/>',
                (16, 16, 15)),
    "drop": ('<path d="M16 2.2C12 8.6 6.8 13.8 6.8 19.8a9.2 9.2 0 0 0 18.4 0'
             'C25.2 13.8 20 8.6 16 2.2Z"/>', (16, 20, 9)),
    # Points NORTH; Plotly rotates it per point (marker.angle).
    "arrow": ('<path d="M16 1.8 27 29 16 22.6 5 29Z"/>', (16, 18, 0)),
}

# --- glyphs (24 x 24, drawn in {ink}; {bg} cuts back into the shape colour) --
_GLYPHS = {
    "flame": '<path d="M12 1.5c.6 3.4 3.3 5.2 4.9 7.6 1.5 2.2 2.1 4 2.1 5.9a7 7 '
             '0 0 1-14 0c0-2.6 1.3-4.6 2.9-6.1.1 1.8.8 3.1 2.1 3.6-.4-3.9.2-7.6 '
             '2-11z"/>',
    "house_fire": '<path d="M12 2.5 1.5 11.5h3v10h15v-10h3z"/><path fill="{bg}" '
                  'd="M12 9.6c.3 1.7 1.8 2.6 2.6 3.8.7 1 1 2 1 3a3.6 3.6 0 0 1-7.2 '
                  '0c0-1.3.6-2.3 1.4-3 0 .9.4 1.6 1.1 1.8-.2-2 .1-3.8 1.1-5.6z"/>',
    "house_damage": '<path d="M12 2.5 1.5 11.5h3v10h15v-10h3z"/><path d="M13 7.5 '
                    '10.6 12l3.2 2.6-2.3 3.4 1 3.5" fill="none" stroke="{bg}" '
                    'stroke-width="1.9" stroke-linejoin="round"/>',
    # A leaning pine on a ground line. Recognisable as a tree first: on these
    # maps a tree is only ever a "tree down" job.
    "tree_down": '<g transform="rotate(16 12 21)"><path d="M12 1 17.2 7.4h-2.4'
                 'l3.8 5h-2.5l4.4 5.4H3.5l4.4-5.4H5.4l3.8-5H6.8z"/><rect x="10.6" '
                 'y="17.5" width="2.8" height="3.6"/></g><path d="M2 21.8h20" '
                 'stroke="{ink}" stroke-width="2.2" stroke-linecap="round"/>',
    "waves": '<path d="M2 6.5c2.5-2.5 4.5-2.5 6.7 0s4.2 2.5 6.6 0 4.2-2.5 6.7 0M2 '
             '12c2.5-2.5 4.5-2.5 6.7 0s4.2 2.5 6.6 0 4.2-2.5 6.7 0M2 17.5c2.5-2.5 '
             '4.5-2.5 6.7 0s4.2 2.5 6.6 0 4.2-2.5 6.7 0" fill="none" stroke="{ink}" '
             'stroke-width="2.7" stroke-linecap="round"/>',
    "car": '<path d="M4.5 11 6.6 5.6a2 2 0 0 1 1.9-1.3h7a2 2 0 0 1 1.9 1.3l2.1 '
           '5.4H21a1 1 0 0 1 1 1v5.5a1 1 0 0 1-1 1h-1v2a1 1 0 0 1-1 1h-2a1 1 0 0 '
           '1-1-1v-2H8v2a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1v-2H3a1 1 0 0 1-1-1V12a1 1 '
           '0 0 1 1-1z"/><path fill="{bg}" d="M7.3 10.5 8.7 6.6h6.6l1.4 3.9z"/>'
           '<circle cx="6.5" cy="14.7" r="1.6" fill="{bg}"/><circle cx="17.5" '
           'cy="14.7" r="1.6" fill="{bg}"/>',
    "hazmat": '<path d="M12 1.5 22.5 12 12 22.5 1.5 12z"/><path fill="{bg}" '
              'd="M10.8 6.3h2.4l-.4 7.7h-1.6z"/><circle cx="12" cy="17" r="1.45" '
              'fill="{bg}"/>',
    "rescue": '<circle cx="12" cy="12" r="7.8" fill="none" stroke="{ink}" '
              'stroke-width="4.6"/><path d="M6.2 6.2 8.7 8.7M17.8 6.2l-2.5 2.5M6.2 '
              '17.8l2.5-2.5M17.8 17.8l-2.5-2.5" stroke="{bg}" stroke-width="2.3"/>',
    "alarm": '<path d="M12 2.5A1.5 1.5 0 0 1 13.5 4v.6a6.5 6.5 0 0 1 5 6.4v4.5l2 '
             '2.5v1h-17v-1l2-2.5V11a6.5 6.5 0 0 1 5-6.4V4A1.5 1.5 0 0 1 12 2.5z"/>'
             '<path d="M9.5 20.5h5a2.5 2.5 0 0 1-5 0z"/>',
    "medical": '<path d="M9 3h6v6h6v6h-6v6H9v-6H3V9h6z"/>',
    "cloud": '<path d="M6.5 19a4.5 4.5 0 0 1-.6-9 6.5 6.5 0 0 1 12.4-1.4A5.2 5.2 0 '
             '0 1 17.5 19z"/>',
    "storm": '<path d="M6.5 14.5a4 4 0 0 1-.5-8 6 6 0 0 1 11.6-1.3 4.7 4.7 0 0 1-.1 '
             '9.3z"/><path d="M12.6 11.5 8.3 17.6h3.2l-1.4 5.4 5.8-7.2h-3.3l1.8-4.3z" '
             'stroke="{bg}" stroke-width="1.3" stroke-linejoin="round"/>',
    "wind": '<path d="M2 8.5h11.5a3 3 0 1 0-3-3M2 13h16.5a3 3 0 1 1-3 3M2 17.5h7" '
            'fill="none" stroke="{ink}" stroke-width="2.5" stroke-linecap="round"/>',
    "heat": '<path d="M10 3.5a2 2 0 0 1 4 0v10.2a4.5 4.5 0 1 1-4 0z"/><path '
            'd="M15.8 5h3.5M15.8 8.5h2.5M15.8 12h3.5" stroke="{ink}" '
            'stroke-width="1.8" stroke-linecap="round"/>',
    "exclaim": '<path d="M10.3 2.5h3.4l-.6 12.2h-2.2z"/><circle cx="12" cy="19.3" '
               'r="2.05"/>',
    "power_off": '<path d="M13.5 1.5 4.5 13.5h6l-1.5 9 9.5-12.5h-6.2z"/><path '
                 'd="M3.5 3.5 20.5 20.5" stroke="{bg}" stroke-width="4.4" '
                 'stroke-linecap="round"/><path d="M3.5 3.5 20.5 20.5" '
                 'stroke="{ink}" stroke-width="2.1" stroke-linecap="round"/>',
    "gauge": '<rect x="6.5" y="1.5" width="4.2" height="17" rx="1"/><path d="M10.7 '
             '4.5h5.3M10.7 8.5h3.6M10.7 12.5h5.3M10.7 16.5h3.6" stroke="{ink}" '
             'stroke-width="2" stroke-linecap="round"/><path d="M1.8 21.6c2.4-2 '
             '4.3-2 6.6 0s4.2 2 6.6 0 4.2-2 6.6 0" fill="none" stroke="{ink}" '
             'stroke-width="2.2" stroke-linecap="round"/>',
    "bar": '<rect x="3" y="9.3" width="18" height="5.4" rx="1.2"/>',
    "dot": '<circle cx="12" cy="12" r="4.8"/>',
}

# --- colours (the levels/classes the maps already used) -----------------------
EMERGENCY = "#d62728"
WATCH_ACT = "#ff7f0e"
ADVICE = "#e6c700"
FIRE = "#ff5722"
BURN_OFF = "#b0643a"
OTHER_INCIDENT = "#5b8def"
MET = "#0ca678"
FLOOD_MAJOR, FLOOD_MODERATE, FLOOD_MINOR, FLOOD_BELOW = (
    "#d62728", "#ff7f0e", "#e6c700", "#5b8def")
STORM = {"strong": "#d62728", "moderate": "#ff7f0e", "weak": "#5b8def"}
ROAD_CLOSURE = "#d62728"
ROAD_OTHER = "#ff7f0e"
POWER = "#7048e8"
# Observation bands (lower bound, colour) — the Blues / YlOrRd ramps the
# graduated dots used, cut into steps a reader can name.
RAIN_BANDS = [(0, "#c6dbef"), (5, "#6baed6"), (15, "#3182bd"),
              (30, "#08519c"), (50, "#08306b")]
GUST_BANDS = [(40, "#fed976"), (60, "#fd8d3c"), (80, "#e31a1c"), (100, "#800026")]

LEVEL_COLOURS = {"Emergency Warning": EMERGENCY, "Watch and Act": WATCH_ACT,
                 "Advice": ADVICE}
WARNING_HAZARDS = ("fire", "flood", "storm", "heat", "other")
_HAZARD_GLYPH = {"fire": "flame", "flood": "waves", "storm": "storm",
                 "heat": "heat", "other": "exclaim"}
_LEVEL_SLUG = {"Emergency Warning": "emergency", "Watch and Act": "watchact",
               "Advice": "advice"}

# --- the catalogue -------------------------------------------------------------
# name -> dict(shape, fill, glyph, ink, label, group). Order = the key's order.
ICONS = {}
GROUPS = []   # (group title, explanation) in display order


def _ink_for(fill):
    """Dark glyph on a light fill (Advice yellow, Watch-and-Act orange), white
    otherwise — by luminance, so a new colour picks its own ink."""
    r, g, b = (int(fill[i:i + 2], 16) / 255 for i in (1, 3, 5))
    return INK_DARK if (0.299 * r + 0.587 * g + 0.114 * b) > 0.55 else INK_LIGHT


def _add(group, name, shape, fill, glyph, label):
    ICONS[PREFIX + name] = dict(shape=shape, fill=fill, glyph=glyph,
                                ink=_ink_for(fill), label=label, group=group)


def _build():
    g = "Warnings (Australian Warning System)"
    GROUPS.append((g, "Triangle in the warning level's colour; the symbol "
                      "inside is the hazard. A warning with a drawn area shows "
                      "the area instead of a marker."))
    for level, colour in LEVEL_COLOURS.items():
        for hz in WARNING_HAZARDS:
            _add(g, "warn-%s-%s" % (_LEVEL_SLUG[level], hz), "triangle", colour,
                 _HAZARD_GLYPH[hz], "%s — %s" % (level, {
                     "fire": "fire", "flood": "flood", "storm": "storm / severe "
                     "weather", "heat": "heat", "other": "other hazard"}[hz]))
    g = "Incidents (VicEmergency)"
    GROUPS.append((g, "Circle; colour is the kind (fire, other incident, "
                      "weather) and the symbol is the incident type."))
    for name, fill, glyph, label in INCIDENT_TYPES:
        _add(g, "inc-" + name, "circle", fill, glyph, label)
    g = "Flood gauges"
    GROUPS.append((g, "Square in the flood class colour."))
    for name, fill, label in (("major", FLOOD_MAJOR, "Major flooding"),
                              ("moderate", FLOOD_MODERATE, "Moderate flooding"),
                              ("minor", FLOOD_MINOR, "Minor flooding"),
                              ("below", FLOOD_BELOW, "Below flood level")):
        _add(g, "gauge-" + name, "square", fill, "gauge", label)
    g = "Roads"
    GROUPS.append((g, "Road-sign shapes. Where the feed gives the road, the "
                      "road itself is highlighted and the sign sits on it."))
    _add(g, "road-closure", "circle", ROAD_CLOSURE, "bar", "Road closed")
    _add(g, "road-other", "diamond", ROAD_OTHER, "exclaim",
         "Other road disruption (lanes, delays, hazards)")
    g = "Storm cells (radar)"
    GROUPS.append((g, "Hexagon in the cell's class colour; the shaded "
                      "polygon around a cell is its projected impact area."))
    for cls in ("strong", "moderate", "weak"):
        _add(g, "storm-" + cls, "hexagon", STORM[cls], "storm",
             "%s storm cell" % cls.capitalize())
    g = "Power"
    GROUPS.append((g, "Square; size grows with customers off supply."))
    _add(g, "power-outage", "square", POWER, "power_off", "Power outage")
    g = "Weather observations (AWS)"
    GROUPS.append((g, "Raw, non-quality-controlled BoM observations. Rain drops "
                      "are shaded by rain since 9am; gust arrows point the way "
                      "the wind is blowing."))
    for i, (lo, colour) in enumerate(RAIN_BANDS):
        hi = RAIN_BANDS[i + 1][0] if i + 1 < len(RAIN_BANDS) else None
        _add(g, "rain-%d" % i, "drop", colour, None,
             "Rain %s mm" % ("%g–%g" % (lo, hi) if hi else "%g+" % lo))
    gust_ranges = []
    for i, (lo, colour) in enumerate(GUST_BANDS):
        hi = GUST_BANDS[i + 1][0] if i + 1 < len(GUST_BANDS) else None
        gust_ranges.append((i, colour, "%g–%g" % (lo, hi) if hi else "%g+" % lo))
    for i, colour, rng in gust_ranges:
        _add(g, "gust-%d" % i, "arrow", colour, None, "Gust %s km/h" % rng)
    for i, colour, rng in gust_ranges:
        _add(g, "gust-%d-calm" % i, "circle", colour, "wind",
             "Gust %s km/h, direction not reported" % rng)


# (slug, fill, glyph, label). Classification is `incident_icon()`.
INCIDENT_TYPES = [
    ("fire", FIRE, "flame", "Bushfire / grass / scrub fire"),
    ("structure-fire", FIRE, "house_fire", "Structure / building fire"),
    ("vehicle-fire", FIRE, "car", "Vehicle fire"),
    ("burn-off", BURN_OFF, "flame", "Planned burn / burn-off"),
    ("fire-other", FIRE, "dot", "Other fire"),
    ("tree", OTHER_INCIDENT, "tree_down", "Tree down"),
    ("flood", OTHER_INCIDENT, "waves", "Flooding"),
    ("damage", OTHER_INCIDENT, "house_damage", "Building damage"),
    ("accident", OTHER_INCIDENT, "car", "Road accident"),
    ("rescue", OTHER_INCIDENT, "rescue", "Rescue"),
    ("hazmat", OTHER_INCIDENT, "hazmat", "Hazardous material"),
    ("alarm", OTHER_INCIDENT, "alarm", "Alarm"),
    ("medical", OTHER_INCIDENT, "medical", "Medical"),
    ("other", OTHER_INCIDENT, "dot", "Other incident"),
    ("met", MET, "wind", "Weather (BoM, via VicEmergency)"),
]

_build()


# --- classification -----------------------------------------------------------

def _text(v):
    if v is None or v != v:  # NaN
        return ""
    return str(v).strip().lower()


# Tried against category2 first (the specific type), then category1.
_INCIDENT_RULES = [
    ("alarm", re.compile(r"alarm")),
    ("accident", re.compile(r"accident|crash|collision")),
    ("rescue", re.compile(r"rescue|trapped|entrap")),
    ("tree", re.compile(r"\btree")),
    ("flood", re.compile(r"flood|water|sandbag|inundat")),
    ("hazmat", re.compile(r"hazard|hazmat|chemical|\bgas\b|spill|leak")),
    ("damage", re.compile(r"damage|roof|building|structure|collapse")),
    ("medical", re.compile(r"medical|ambulance|cardiac")),
]
_FIRE_RULES = [
    ("burn-off", re.compile(r"burn[- ]?off|planned|prescribed|\bburn\b")),
    # "Non Structure Fire" is neither a structure nor a grass fire (stats.py).
    ("fire-other", re.compile(r"non[- ]?structure")),
    ("structure-fire", re.compile(r"structure|building|house|residential|"
                                  r"factory|shed|industrial")),
    ("vehicle-fire", re.compile(r"vehicle|car\b|truck")),
    ("fire", re.compile(r"bush|grass|scrub|forest|crop|hay|stubble|vegetation")),
]


def incident_icon(row, kind=None):
    """Icon name for a VicEmergency incident row. `kind` is the map's own Kind
    ("Fire" / "Other incident" / "Met (weather)") when the caller has it."""
    cat1, cat2 = _text(row.get("category1")), _text(row.get("category2"))
    if kind is None:
        kind = ("Fire" if cat1 == "fire" else
                "Met (weather)" if "met" in (cat1, cat2) else "Other incident")
    if kind == "Fire":
        for text in (cat2, cat1):
            for slug, rx in _FIRE_RULES:
                if rx.search(text):
                    return PREFIX + "inc-" + slug
        return PREFIX + "inc-fire" if not cat2 or cat2 == "fire" \
            else PREFIX + "inc-fire-other"
    if kind.startswith("Met"):
        return PREFIX + "inc-met"
    for text in (cat2, cat1):
        for slug, rx in _INCIDENT_RULES:
            if rx.search(text):
                return PREFIX + "inc-" + slug
    return PREFIX + "inc-other"


_HAZARD_RULES = [
    ("fire", re.compile(r"fire|burn|smoke")),
    ("flood", re.compile(r"flood|riverine|dam\b|inundat|tide|surge|tsunami")),
    ("heat", re.compile(r"heat")),
    ("storm", re.compile(r"storm|wind|thunder|hail|rain|snow|weather|severe|"
                         r"cyclone|blizzard|tree|coastal|land ?slip")),
]


def warning_hazard(row):
    """fire / flood / storm / heat / other for a warning, read from its HAZARD
    fields (event, category2) and only from the headline when there is none —
    the same rule as fire_areas.is_fire_warning, so a flood warning for
    "Fire Station Rd" stays a flood warning."""
    hazard = " ".join(_text(row.get(c)) for c in ("event", "category2")).strip()
    text = hazard or " ".join(_text(row.get(c)) for c in ("headline", "category1"))
    for hz, rx in _HAZARD_RULES:
        if rx.search(text):
            return hz
    return "other"


def warning_icon(level, row=None):
    """Icon name for a warning at an AWS level, with the hazard glyph."""
    slug = _LEVEL_SLUG.get(level, "advice")
    return "%swarn-%s-%s" % (PREFIX, slug, warning_hazard({} if row is None else row))


def gauge_icon(label):
    """Icon for a flood-gauge class label ("Major flooding" … "Below flood level")."""
    text = _text(label)
    for cls in ("major", "moderate", "minor"):
        if text.startswith(cls):
            return PREFIX + "gauge-" + cls
    return PREFIX + "gauge-below"


def _band(value, bands):
    idx = 0
    for i, (lo, _) in enumerate(bands):
        if value >= lo:
            idx = i
    return idx


def rain_icon(mm):
    return "%srain-%d" % (PREFIX, _band(float(mm), RAIN_BANDS))


def rain_band(mm):
    return _band(float(mm), RAIN_BANDS)


def gust_band(kmh):
    return _band(float(kmh), GUST_BANDS)


_COMPASS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
            "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]


def downwind_degrees(direction):
    """Bearing the wind is blowing TOWARDS, from BoM's compass point (the
    direction it comes FROM), or None for calm / not reported."""
    d = str(direction or "").strip().upper()
    if d not in _COMPASS:
        return None
    return (_COMPASS.index(d) * 22.5 + 180) % 360


# --- rendering ----------------------------------------------------------------

def svg(name):
    """Standalone SVG for one icon (32 x 32)."""
    spec = ICONS[name]
    outline, (cx, cy, size) = _SHAPES[spec["shape"]]
    # Dark halo first, then the white-edged shape: legible on light tiles,
    # dark tiles and over coloured warning fills alike.
    parts = [
        '<g fill="none" stroke="rgba(0,0,0,0.45)" stroke-width="4.2" '
        'stroke-linejoin="round">%s</g>' % outline,
        '<g fill="%s" stroke="#ffffff" stroke-width="1.9" '
        'stroke-linejoin="round">%s</g>' % (spec["fill"], outline),
    ]
    if spec["glyph"] and size:
        glyph = _GLYPHS[spec["glyph"]].format(ink=spec["ink"], bg=spec["fill"])
        k = size / 24.0
        parts.append('<g transform="translate(%.2f %.2f) scale(%.4f)" fill="%s">'
                     '%s</g>' % (cx - size / 2, cy - size / 2, k, spec["ink"], glyph))
    return ('<svg xmlns="http://www.w3.org/2000/svg" width="32" height="32" '
            'viewBox="0 0 32 32">%s</svg>' % "".join(parts))


def data_uri(name):
    return "data:image/svg+xml;base64," + base64.b64encode(
        svg(name).encode("utf-8")).decode("ascii")


def icon_img(name, px=18, **style):
    """An <img> of one icon for HTML legends and the map key."""
    from dash import html
    return html.Img(src=data_uri(name), width=px, height=px, alt="",
                    title=ICONS[name]["label"],
                    style=dict({"verticalAlign": "middle", "flex": "none"}, **style))


def catalogue_json():
    """{name: svg} for the browser registrar (inlined into every page)."""
    return json.dumps({name: svg(name) for name in ICONS}, separators=(",", ":"))


def colour(name):
    return ICONS[name]["fill"]


def size_for(px):
    """marker.size that draws an icon `px` CSS pixels across."""
    return px * 10.0 / NATURAL_PX


def marker(symbol, px=22, colour=None, **extra):
    """marker dict for a Scattermap icon trace. `symbol` is one name or a list
    (one per point); `colour` is the LEGEND swatch (Plotly's legend can only
    draw a dot) and defaults to the first icon's own colour."""
    first = symbol[0] if isinstance(symbol, (list, tuple)) and symbol else symbol
    return dict(symbol=list(symbol) if isinstance(symbol, tuple) else symbol,
                size=size_for(px), allowoverlap=True,
                color=colour or ICONS.get(first, {}).get("fill", "#888888"),
                **extra)


def is_icon_trace(trace):
    sym = getattr(getattr(trace, "marker", None), "symbol", None)
    if isinstance(sym, str):
        return sym.startswith(PREFIX)
    try:
        return any(isinstance(s, str) and s.startswith(PREFIX) for s in sym)
    except TypeError:
        return False


def static_figure(fig):
    """The figure with every icon marker turned back into a dot, for renderers
    without the browser registrar (kaleido PDFs). Returns `fig` (modified in
    place); a figure with no icon traces is untouched."""
    for trace in fig.data:
        if not is_icon_trace(trace):
            continue
        size = trace.marker.size if trace.marker.size is not None else 10
        sym = trace.marker.symbol
        # Per-point icons can differ in colour (a burn-off in the Fire trace);
        # the dot keeps each one's own colour, not the legend swatch.
        colours = (trace.marker.color if isinstance(sym, str) else
                   [ICONS.get(s, {}).get("fill", trace.marker.color) for s in sym])
        trace.update(marker=dict(symbol="circle", size=size * NATURAL_PX / 10 * 0.55,
                                 color=colours, angle=None, allowoverlap=None))
    return fig

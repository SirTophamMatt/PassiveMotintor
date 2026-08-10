"""Event Replay (public, read-only): how did the situation develop?

Pick an event tag, drag a timestamp, and see the map, the KPIs and the
Intelligence Feed as they stood at that moment. Built for after-action review,
training and event reconstruction.

Two things this page is careful about:

* **It never fetches.** Every frame comes from stored data (`app.replay`), so
  scrubbing or playing back cannot put load on BoM or VicEmergency, and a
  replay of last month is not quietly coloured by today's feed.
* **It never overstates what it can rebuild.** Per-entity incident/road/power
  state only exists from the day the change journal started; the coverage
  banner says so for the selected event rather than letting a partial
  reconstruction pass as a complete one.

The map is drawn by the *same* renderers as the live Unified Map
(`unified.map_figure` with a historical `source`), so the two can never drift
apart visually.
"""
import logging
from datetime import timedelta

from dash import ALL, Input, Output, State, ctx, dcc, html

from app import replay as replay_data
from app import ui
from app.pages import unified

log = logging.getLogger(__name__)

# Slider granularity. Five minutes is finer than any collector's cadence, so
# nothing is skipped, without giving the slider tens of thousands of stops.
STEP_MINUTES = 5
# One UI tick per second represents this many minutes of event time at 1x.
PLAY_MINUTES_PER_TICK = 5
TICK_MS = 1000
SPEEDS = [1, 2, 4]

SEVERITY_COLOUR = {3: "#d62728", 2: "#ff7f0e", 1: "#e6c700", 0: "#5b8def"}


def layout():
    return html.Div([
        html.H2("Event Replay"),
        html.Div([
            html.Div([
                html.H4("Event"),
                dcc.Dropdown(id="replay-event", className="dropdown",
                             placeholder="Select a tagged event…",
                             options=[{"label": label, "value": value}
                                      for label, value in
                                      replay_data.event_options()]),
                html.Div(id="replay-event-meta", className="muted",
                         style={"marginTop": "8px", "fontSize": "12px"}),
            ], className="panel"),
            html.Div([
                html.H4("Layers"),
                dcc.Checklist(id="replay-layers", options=unified.LAYER_OPTIONS,
                              value=unified.DEFAULT_LAYERS,
                              labelStyle={"display": "block"}),
            ], className="panel"),
        ], className="panel-row"),

        html.Div(id="replay-coverage", className="muted",
                 style={"margin": "10px 0", "fontSize": "12px",
                        "fontStyle": "italic"}),

        html.Div([
            html.Div([
                html.Div(id="replay-selected-time",
                         style={"fontSize": "22px", "fontWeight": "600"}),
                html.Div(id="replay-position", className="muted",
                         style={"fontSize": "12px"}),
            ], style={"minWidth": "220px"}),
            html.Div([
                html.Button("◀ 15m", id="replay-back", n_clicks=0,
                            className="btn", style={"marginRight": "6px"}),
                html.Button("▶ Play", id="replay-play", n_clicks=0,
                            className="btn", style={"marginRight": "6px"}),
                html.Button("Pause", id="replay-pause", n_clicks=0,
                            className="btn", style={"marginRight": "6px"}),
                html.Button("15m ▶", id="replay-forward", n_clicks=0,
                            className="btn", style={"marginRight": "14px"}),
                dcc.RadioItems(id="replay-speed",
                               options=[{"label": " %dx" % s, "value": s}
                                        for s in SPEEDS],
                               value=1, inline=True,
                               labelStyle={"marginRight": "10px"}),
            ], style={"display": "flex", "alignItems": "center",
                      "flexWrap": "wrap"}),
        ], style={"display": "flex", "gap": "24px", "alignItems": "center",
                  "flexWrap": "wrap", "margin": "6px 0"}),

        dcc.Slider(id="replay-slider", min=0, max=1, step=STEP_MINUTES, value=0,
                   marks={}, tooltip={"placement": "bottom"},
                   updatemode="drag"),

        # Disabled unless playing, so an idle page does no work at all.
        dcc.Interval(id="replay-tick", interval=TICK_MS, disabled=True),
        dcc.Store(id="replay-playing", data=False),

        html.Div(id="replay-kpis", className="kpi-row",
                 style={"marginTop": "12px"}),

        html.Div([
            html.Div(dcc.Graph(id="replay-map", style={"height": "70vh"},
                               config=ui.MAP_CONFIG),
                     className="graph-card", style={"flex": "3 1 640px"}),
            html.Div([
                html.H4("Intelligence timeline", style={"marginTop": 0}),
                html.Div("Click an entry to jump the replay to that moment.",
                         className="muted", style={"fontSize": "12px"}),
                html.Div(id="replay-timeline",
                         style={"maxHeight": "62vh", "overflowY": "auto",
                                "marginTop": "8px"}),
            ], className="panel", style={"flex": "1 1 320px"}),
        ], style={"display": "flex", "gap": "12px", "flexWrap": "wrap",
                  "marginTop": "12px"}),
    ])


def _marks(window):
    """~8 readable marks across the event, whatever its length."""
    total = int(window["duration"].total_seconds() // 60)
    if total <= 0:
        return {0: window["start"].strftime("%H:%M")}
    count = 8
    step = max(STEP_MINUTES, (total // count // STEP_MINUTES or 1) * STEP_MINUTES)
    long_event = window["duration"] > timedelta(days=1)
    marks = {}
    offset = 0
    while offset <= total:
        when = window["start"] + timedelta(minutes=offset)
        marks[offset] = when.strftime("%d %b" if long_event else "%H:%M")
        offset += step
    marks[total] = (window["start"] + timedelta(minutes=total)).strftime(
        "%d %b" if long_event else "%H:%M")
    return marks


def _timeline(entries, selected):
    """The Intelligence Feed as the event's narrative spine."""
    if not entries:
        return html.Div("No intelligence events recorded in this period.",
                        className="muted")
    rows = []
    for index, entry in enumerate(entries):
        ts = entry["ts"]
        is_near = ts is not None and abs((ts - selected).total_seconds()) <= 300
        rows.append(html.Button([
            html.Div([
                html.Span(ts.strftime("%d %b %H:%M") if ts else "—",
                          style={"fontWeight": "600"}),
                html.Span(entry["severity_label"], className="feed-badge",
                          style={"background": SEVERITY_COLOUR.get(entry["severity"]),
                                 "marginLeft": "8px"}),
            ]),
            html.Div(entry["headline"], style={"fontSize": "12px"}),
        ],
            id={"type": "replay-seek", "index": index},
            n_clicks=0,
            className="replay-seek" + (" replay-seek-active" if is_near else ""),
        ))
    return html.Div(rows)


def register_callbacks(app):
    @app.callback(
        Output("replay-slider", "min"),
        Output("replay-slider", "max"),
        Output("replay-slider", "marks"),
        Output("replay-slider", "value"),
        Output("replay-event-meta", "children"),
        Output("replay-coverage", "children"),
        Output("replay-playing", "data", allow_duplicate=True),
        Input("replay-event", "value"),
        prevent_initial_call="initial_duplicate")
    def choose_event(tag_id):
        if not tag_id:
            return (0, 1, {}, 0, "Select an event to replay.",
                    "", False)
        window = replay_data.event_window(tag_id)
        if not window:
            return 0, 1, {}, 0, "Event not found.", "", False
        total = int(window["duration"].total_seconds() // 60)
        meta = html.Div([
            html.Div(window["name"], style={"fontWeight": "600",
                                            "fontSize": "14px"}),
            html.Div("Start: %s" % window["start"].strftime("%d %b %Y %H:%M")),
            html.Div("End: %s%s" % (
                window["end"].strftime("%d %b %Y %H:%M"),
                "  (ongoing — replays to now)" if window["ongoing"] else "")),
            html.Div("Duration: %s"
                     % replay_data.format_duration(window["duration"])),
        ])
        # Selecting a new event always stops playback: leaving it running would
        # start scrubbing through an event nobody asked to watch.
        return (0, max(total, 1), _marks(window), 0, meta,
                replay_data.coverage_note(window["start"]), False)

    @app.callback(
        Output("replay-playing", "data"),
        Output("replay-tick", "disabled"),
        Input("replay-play", "n_clicks"),
        Input("replay-pause", "n_clicks"),
        Input("replay-slider", "value"),
        State("replay-slider", "max"),
        State("replay-playing", "data"),
        prevent_initial_call=True)
    def toggle_play(_play, _pause, value, maximum, playing):
        trigger = ctx.triggered_id
        if trigger == "replay-play":
            # Replaying from the very end would sit there doing nothing.
            playing = not (value is not None and maximum and value >= maximum)
        elif trigger == "replay-pause":
            playing = False
        elif value is not None and maximum and value >= maximum:
            playing = False        # auto-pause at the end of the event
        return bool(playing), not bool(playing)

    @app.callback(
        Output("replay-slider", "value", allow_duplicate=True),
        Input("replay-tick", "n_intervals"),
        Input("replay-back", "n_clicks"),
        Input("replay-forward", "n_clicks"),
        State("replay-slider", "value"),
        State("replay-slider", "max"),
        State("replay-speed", "value"),
        prevent_initial_call=True)
    def advance(_tick, _back, _forward, value, maximum, speed):
        value = value or 0
        maximum = maximum or 0
        trigger = ctx.triggered_id
        if trigger == "replay-back":
            step = -15
        elif trigger == "replay-forward":
            step = 15
        else:
            step = PLAY_MINUTES_PER_TICK * int(speed or 1)
        return max(0, min(maximum, value + step))

    @app.callback(
        Output("replay-slider", "value", allow_duplicate=True),
        Input({"type": "replay-seek", "index": ALL}, "n_clicks"),
        State("replay-event", "value"),
        prevent_initial_call=True)
    def seek_to_event(clicks, tag_id):
        from dash import no_update

        if not tag_id or not clicks or not any(clicks):
            return no_update
        trigger = ctx.triggered_id
        if not trigger:
            return no_update
        window = replay_data.event_window(tag_id)
        if not window:
            return no_update
        entries = replay_data.timeline(window["start"], window["end"])
        index = trigger["index"]
        if index >= len(entries):
            return no_update
        ts = entries[index]["ts"]
        if ts is None:
            return no_update
        offset = int((ts - window["start"]).total_seconds() // 60)
        total = int(window["duration"].total_seconds() // 60)
        return max(0, min(total, offset))

    @app.callback(
        Output("replay-selected-time", "children"),
        Output("replay-position", "children"),
        Output("replay-kpis", "children"),
        Output("replay-map", "figure"),
        Output("replay-timeline", "children"),
        Input("replay-slider", "value"),
        Input("replay-event", "value"),
        Input("replay-layers", "value"),
        Input("theme-store", "data"))
    def render(offset, tag_id, layers, dark):
        dark = bool(dark)
        layers = layers if layers is not None else unified.DEFAULT_LAYERS
        if not tag_id:
            empty = unified.map_figure([], dark, source=lambda _k: None,
                                       uirevision="replay-map")
            return ("—", "Select an event to begin.", [], empty,
                    html.Div("No event selected.", className="muted"))

        window = replay_data.event_window(tag_id)
        if not window:
            empty = unified.map_figure([], dark, source=lambda _k: None,
                                       uirevision="replay-map")
            return ("—", "Event not found.", [], empty, html.Div())

        selected = window["start"] + timedelta(minutes=int(offset or 0))
        selected = min(selected, window["end"])

        kpis = [ui.kpi_card(k["label"], k["value"])
                for k in replay_data.historical_kpis(selected)]
        figure = unified.map_figure(
            layers, dark, source=replay_data.frame_source(selected),
            uirevision="replay-map")
        entries = replay_data.timeline(window["start"], window["end"])
        position = "%s into the event · %s of %s" % (
            replay_data.format_duration(selected - window["start"]),
            selected.strftime("%d %b"),
            replay_data.format_duration(window["duration"]))
        return (selected.strftime("%d %b %Y  %H:%M"), position, kpis, figure,
                _timeline(entries, selected))

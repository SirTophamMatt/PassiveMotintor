"""Intelligence Feed page (public, read-only) — route ``/feed``.

A reverse-chronological log of what CHANGED, quantified. Each entry is a
timestamp, a headline that states the movement, and the numbers behind it:

    12:43 — Walwa fire increased 38 ha
            214 → 296 ha since 12:05
            Watch and Act remains current
            3 road disruptions within 10 km

Everything comes from ``app/intel_feed.py``; this file is presentation only.
Genuinely-new entries slot in with the same CSS animation as the sidebar log
(``feed-slot-in``), tracked per-browser in a ``dcc.Store`` of seen ids so the
backlog does not mass-animate on first paint or when a filter changes.

Not to be confused with ``/intel`` — that is the password-gated Intel Tool
(the burnt-area chart generator), which predates this page.
"""
from dash import Input, Output, State, callback_context, dcc, html

from app import intel_feed
from app.collector import manager

WINDOW_OPTIONS = [
    {"label": "Last hour", "value": 1},
    {"label": "Last 6 hours", "value": 6},
    {"label": "Last 24 hours", "value": 24},
    {"label": "Last 7 days", "value": 168},
]

SEVERITY_OPTIONS = [
    {"label": "Everything", "value": intel_feed.INFO},
    {"label": "Notable and above", "value": intel_feed.NOTABLE},
    {"label": "Major and above", "value": intel_feed.MAJOR},
    {"label": "Critical only", "value": intel_feed.CRITICAL},
]


def layout():
    return html.Div([
        html.H2("Intelligence Feed"),
        html.P("What changed, by how much, and how fast — across every "
               "monitored layer.", className="muted"),
        html.Div([
            html.Div([
                html.H4("Detector"),
                html.Div(id="feed-collector-status"),
                html.Div("Reads collected data and journals movement. "
                         "Start/stop from the Admin page.",
                         className="muted",
                         style={"marginTop": "8px", "fontSize": "12px"}),
            ], className="panel"),
            html.Div([
                html.H4("Window"),
                dcc.RadioItems(id="feed-window", options=WINDOW_OPTIONS,
                               value=24, className="radio-row"),
                html.H4("Significance", style={"marginTop": "10px"}),
                dcc.RadioItems(id="feed-severity", options=SEVERITY_OPTIONS,
                               value=intel_feed.INFO, className="radio-row"),
            ], className="panel"),
            html.Div([
                html.H4("Layers"),
                dcc.Checklist(
                    id="feed-hazards",
                    options=[{"label": " " + intel_feed.HAZARD_LABEL[h],
                              "value": h} for h in intel_feed.HAZARDS],
                    value=list(intel_feed.HAZARDS),
                    className="check-row"),
            ], className="panel"),
        ], className="panel-row"),
        dcc.Interval(id="feed-interval", interval=20_000, n_intervals=0),
        # Ids already painted in THIS browser tab, so only genuinely-new
        # entries animate (the server is stateless about who has seen what).
        dcc.Store(id="feed-seen", data=None),
        html.Div(id="feed-kpis", className="kpi-row"),
        html.Div(id="feed-entries", className="feed"),
    ])


def _kpis(tally, last_ts):
    cards = []
    for level in (intel_feed.CRITICAL, intel_feed.MAJOR, intel_feed.NOTABLE,
                  intel_feed.INFO):
        label, colour = intel_feed.SEVERITY_STYLE[level]
        cards.append(html.Div([
            html.Div(label, className="kpi-label"),
            html.Div(str(tally.get(level, 0)), className="kpi-value"),
        ], className="kpi-card", style={"borderTop": f"4px solid {colour}"}))
    cards.append(html.Div([
        html.Div("Last change", className="kpi-label"),
        html.Div(last_ts.strftime("%H:%M") if last_ts else "—",
                 className="kpi-value"),
    ], className="kpi-card"))
    return cards


def _entry_card(entry, is_new):
    classes = "feed-entry" + (" feed-entry-new" if is_new else "")
    return html.Div([
        html.Div([
            html.Span(entry["time"], className="feed-time"),
            html.Span(entry["hazard_label"], className="feed-chip",
                      style={"borderColor": entry["colour"],
                             "color": entry["colour"]}),
            dcc.Link(entry["headline"], href=entry["url"],
                     className="feed-headline"),
        ], className="feed-head"),
        html.Div([html.Div(line, className="feed-line")
                  for line in entry["lines"]], className="feed-body"),
    ], className=classes, key=str(entry["id"]),
        style={"borderLeftColor": entry["colour"]})


def _empty(hours):
    window = next((o["label"].lower() for o in WINDOW_OPTIONS
                   if o["value"] == hours), "the window")
    return html.Div([
        html.Div("Nothing has changed in " + window + ".",
                 className="feed-empty-title"),
        html.Div("A quiet feed means the picture is stable — entries appear "
                 "when a value actually moves.", className="muted"),
    ], className="panel")


def register_callbacks(app):
    @app.callback(
        Output("feed-entries", "children"),
        Output("feed-kpis", "children"),
        Output("feed-collector-status", "children"),
        Output("feed-seen", "data"),
        Input("feed-interval", "n_intervals"),
        Input("feed-window", "value"),
        Input("feed-severity", "value"),
        Input("feed-hazards", "value"),
        State("feed-seen", "data"))
    def refresh(_tick, hours, min_severity, hazards, seen):
        from app import ui
        status = manager.status()["intel"]
        pill = html.Div([
            ui.status_pill(status["running"], "Detecting", "Stopped"),
            html.Span(f"  last pass {status.get('last_run') or '—'}",
                      className="muted", style={"fontSize": "12px"}),
        ])
        entries = intel_feed.entries(
            hours=hours or 24,
            hazards=hazards if hazards else ["__none__"],
            min_severity=min_severity or intel_feed.INFO)
        tally = intel_feed.counts(hours=hours or 24)
        kpis = _kpis(tally, intel_feed.last_entry_time())
        if not entries:
            return _empty(hours), kpis, pill, seen or []

        ids = [str(e["id"]) for e in entries]
        # Only a TICK can reveal genuinely-new entries. First paint seeds
        # silently, and a filter change re-seeds — widening the window must not
        # animate a screenful of old entries as if they just happened.
        tick = (callback_context.triggered_id == "feed-interval")
        new_ids = (set(ids) - set(seen or [])) if (tick and seen is not None) \
            else set()
        cards = [_entry_card(e, str(e["id"]) in new_ids) for e in entries]
        return cards, kpis, pill, ids

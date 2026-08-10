"""Briefing Mode (public, read-only): the screen you stand in front of.

Deliberately NOT another Overview. Overview answers "what is happening"; this
answers "what matters right now, what changed recently, and what should I be
watching" — and it is usable on its own, before anyone generates a PDF.

Every operational statement on this page comes from `app.briefing`, the same
model the PDF renders, so the printed pack can never disagree with the screen
it came from. This module contains presentation only: no thresholds, no
wording decisions, no aggregation.
"""
from dash import Input, Output, State, dcc, html

from app import briefing as briefing_model

SEVERITY_COLOUR = {3: "#d62728", 2: "#ff7f0e", 1: "#e6c700", 0: "#5b8def"}
LEVEL_COLOUR = {"Emergency Warning": "#d62728", "Watch and Act": "#ff7f0e",
                "Advice": "#e6c700"}
STATE_COLOUR = {"ok": "#2ca02c", "stale": "#ff7f0e", "never": "#d62728"}


def layout():
    return html.Div([
        html.H2("Operational Briefing"),
        html.Div([
            html.Div([
                html.H4("Briefing"),
                html.Label("Changes since"),
                dcc.Dropdown(
                    id="briefing-window",
                    options=[{"label": "Last " + label, "value": minutes}
                             for minutes, label in briefing_model.WINDOW_OPTIONS],
                    value=briefing_model.DEFAULT_WINDOW_MINUTES,
                    clearable=False, className="dropdown"),
                html.Div([
                    html.Button("Refresh Briefing", id="briefing-refresh-btn",
                                n_clicks=0, className="btn",
                                style={"marginRight": "8px"}),
                    html.Button("Export PDF", id="briefing-pdf-btn", n_clicks=0,
                                className="btn", style={"marginRight": "8px"}),
                    html.Button("Copy briefing text", id="briefing-copy-btn",
                                n_clicks=0, className="btn"),
                ], style={"marginTop": "10px"}),
                html.Div(id="briefing-pdf-status", className="muted",
                         style={"marginTop": "6px"}),
                dcc.Download(id="briefing-pdf-download"),
            ], className="panel"),
            html.Div([
                html.H4("Generated"),
                html.Div(id="briefing-generated"),
                html.Div(id="briefing-staleness", style={"marginTop": "8px"}),
            ], className="panel"),
        ], className="panel-row"),

        # Auto-refresh is slow on purpose: a briefing is read aloud, and having
        # the numbers move mid-sentence is worse than being 60 seconds behind.
        dcc.Interval(id="briefing-interval", interval=60_000, n_intervals=0),

        html.H3("Current Situation", style={"marginTop": "18px"}),
        html.Div(id="briefing-situation"),

        html.H3("Significant Changes", style={"marginTop": "18px"}),
        html.Div(id="briefing-changes-intro", className="muted"),
        html.Div(id="briefing-changes", className="feed feed-compact"),

        html.H3("Active Warnings", style={"marginTop": "18px"}),
        html.Div(id="briefing-warnings"),

        html.H3("Emerging Consequences", style={"marginTop": "18px"}),
        html.Div("Cross-layer situations assembled by rule from stored data — "
                 "no generated text.", className="muted",
                 style={"fontSize": "12px"}),
        html.Div(id="briefing-consequences"),

        html.H3("Watch Points", style={"marginTop": "18px"}),
        html.Div("Not yet at the most severe threshold. Projections are "
                 "Passive Monitor's own extrapolation, not official forecasts, "
                 "and are labelled as such.", className="muted",
                 style={"fontSize": "12px"}),
        html.Div(id="briefing-watch"),

        html.H3("Weather Observations", style={"marginTop": "18px"}),
        html.Div(id="briefing-weather", className="kpi-row"),
        html.Div("Raw BoM automatic weather station observations — not "
                 "quality-controlled, not an official warning product.",
                 className="muted",
                 style={"fontSize": "12px", "fontStyle": "italic"}),

        html.H3("Data Freshness", style={"marginTop": "18px"}),
        html.Div(id="briefing-sources", className="panel-row"),

        # Holds the plain-text briefing for the copy button; the clipboard
        # write itself is done by dcc.Clipboard against this target.
        html.Div([
            dcc.Clipboard(id="briefing-clipboard", target_id="briefing-text",
                          style={"display": "none"}),
            html.Pre(id="briefing-text",
                     style={"display": "none", "whiteSpace": "pre-wrap"}),
        ]),
    ])


# --------------------------------------------------------------------------- #
# Renderers — one per section, each taking only the snapshot
# --------------------------------------------------------------------------- #
def _situation(snapshot):
    if not snapshot.situation:
        return html.Div("No module data available.", className="muted")
    groups = []
    for name in _ordered_groups(snapshot.situation):
        cards = [_kpi_with_detail(k)
                 for k in snapshot.situation if k.group == name]
        groups.append(html.Div([
            html.Div(name, className="muted",
                     style={"fontSize": "12px", "margin": "0 0 4px 2px"}),
            html.Div(cards, className="kpi-row"),
        ], style={"marginBottom": "10px"}))
    return html.Div(groups)


def _ordered_groups(kpis):
    seen = []
    for k in kpis:
        if k.group not in seen:
            seen.append(k.group)
    return seen


def _changes(snapshot):
    if not snapshot.changes:
        return html.Div(
            "No significant changes detected in the last %s."
            % snapshot.window_label, className="muted")
    return html.Div([_change_row(c) for c in snapshot.changes])


def _change_row(change):
    return html.Div([
        html.Div([
            html.Span(change.time, className="feed-time"),
            html.Span(change.severity_label, className="feed-badge",
                      style={"background": SEVERITY_COLOUR.get(change.severity),
                             "marginLeft": "8px"}),
            html.Span(change.hazard_label, className="muted",
                      style={"marginLeft": "8px", "fontSize": "12px"}),
        ]),
        dcc.Link(change.headline, href=change.url, className="feed-headline"),
        html.Div([html.Div(line, className="feed-line") for line in change.lines]),
    ], className="feed-entry",
        style={"borderLeft": "3px solid %s" % SEVERITY_COLOUR.get(change.severity),
               "paddingLeft": "10px", "marginBottom": "10px"})


def _warnings(snapshot):
    blocks = []
    for source in ("VicEmergency", "BoM"):
        active = snapshot.warnings_from(source)
        rows = []
        if source == "VicEmergency":
            # Grouped by level: the split is the operational point of the
            # section, and a flat list buries an Emergency Warning among Advices.
            for level in briefing_model.VICEMERGENCY_LEVELS:
                at_level = [w for w in active if w.level == level]
                if not at_level:
                    continue
                rows.append(html.Div(
                    "%s (%d)" % (level, len(at_level)),
                    className="briefing-level",
                    style={"color": LEVEL_COLOUR.get(level)}))
                rows += [_warning_row(w) for w in at_level]
            if snapshot.omitted_advice:
                rows.append(html.Div(
                    "… and %d further Advice warning(s) not listed."
                    % snapshot.omitted_advice, className="muted",
                    style={"fontSize": "12px", "marginTop": "6px"}))
        else:
            rows = [_warning_row(w) for w in active]
        blocks.append(html.Div([
            html.H5(source, style={"margin": "0 0 4px"}),
            html.Div(rows) if rows else html.Div("None active.",
                                                 className="muted"),
        ], className="panel"))
    return html.Div(blocks, className="panel-row")


# BoM warning titles list every affected district and run to several hundred
# characters. The full text is one click away on the warning's own page; a
# briefing needs the warning identifiable, not verbatim.
_TITLE_MAX = 110


def _shorten(text):
    text = str(text or "")
    return text if len(text) <= _TITLE_MAX else text[:_TITLE_MAX].rstrip() + "…"


def _warning_row(w):
    meta = []
    if w.issued:
        meta.append("issued %s" % w.issued.strftime("%d %b %H:%M"))
    if w.expires:
        meta.append("expires %s" % w.expires.strftime("%d %b %H:%M"))
    if w.action:
        meta.append(w.action)
    body = [html.Span(w.kind, style={"fontWeight": "600"}),
            html.Span(" — " + _shorten(w.title) if w.title else "")]
    return html.Div([
        dcc.Link(body, href=w.url, className="briefing-warning", title=w.title)
        if w.url else html.Div(body, className="briefing-warning"),
        html.Div(" · ".join(meta), className="muted",
                 style={"fontSize": "11px"}),
    ], style={"marginBottom": "6px"})


def _consequences(snapshot):
    if not snapshot.consequences:
        return html.Div("No significant cross-layer consequences currently "
                        "identified.", className="muted")
    return html.Div([
        html.Div([
            html.H5(c.title, style={"margin": "0 0 4px"}),
            html.Ul([html.Li(line) for line in c.lines],
                    style={"margin": "0 0 0 18px"}),
        ], className="panel") for c in snapshot.consequences
    ], className="panel-row")


def _watch(snapshot):
    if not snapshot.watch_points:
        return html.Div("Nothing currently approaching a threshold.",
                        className="muted")
    return html.Div([
        html.Div([
            html.Div([
                html.Span(_watch_tag(w), className="feed-badge",
                          style={"background": ("#5b8def"
                                                if w.kind == briefing_model.PROJECTION
                                                else "#2ca02c")}),
                html.Span(w.title, style={"fontWeight": "600",
                                          "marginLeft": "8px"}),
            ]),
            html.Ul([html.Li(line) for line in w.lines],
                    style={"margin": "4px 0 0 18px"}),
        ], className="panel") for w in snapshot.watch_points
    ], className="panel-row")


def _watch_tag(w):
    return ("Trend projection" if w.kind == briefing_model.PROJECTION
            else "Observed")


def _weather(snapshot):
    if not snapshot.weather:
        return [html.Div("No AWS observations available.", className="muted")]
    return [_kpi_with_detail(k) for k in snapshot.weather]


def _kpi_with_detail(kpi):
    """KPI card with the station on its own muted line. Same shape as the
    Weather page's observation cards, so the two read alike."""
    style = {"borderTop": "4px solid %s" % kpi.accent} if (kpi.accent and kpi.alert) else {}
    body = [html.Div(kpi.label, className="kpi-label"),
            html.Div(kpi.value, className="kpi-value")]
    if kpi.detail:
        body.append(html.Div(kpi.detail, className="muted",
                             style={"fontSize": "11px", "marginTop": "2px"}))
    return html.Div(body, className="kpi-card", style=style)


def _sources(snapshot):
    if not snapshot.sources:
        return [html.Div("No collector status available.", className="muted")]
    rows = []
    for s in snapshot.sources:
        rows.append(html.Div([
            html.Span("●", style={"color": STATE_COLOUR.get(s.state),
                                  "marginRight": "6px"}),
            html.Span(s.name, style={"fontWeight": "600"}),
            html.Span(" — updated %s" % briefing_model.describe_age(s.age_minutes)),
            html.Span(" (%s)" % s.detail, className="muted") if s.detail else "",
        ], style={"marginBottom": "4px"}))
    return [html.Div(rows, className="panel")]


def _staleness_banner(snapshot):
    """A briefing must never let old data look current."""
    stale = snapshot.stale_sources
    if not stale:
        return html.Div("All sources reporting normally.", className="muted",
                        style={"fontSize": "12px"})
    return html.Div(
        "⚠ %d source(s) stale or not reporting: %s. Figures drawn from them "
        "may be out of date." % (len(stale), ", ".join(s.name for s in stale)),
        className="error-text", style={"fontSize": "12px"})


def register_callbacks(app):
    @app.callback(
        Output("briefing-generated", "children"),
        Output("briefing-staleness", "children"),
        Output("briefing-situation", "children"),
        Output("briefing-changes-intro", "children"),
        Output("briefing-changes", "children"),
        Output("briefing-warnings", "children"),
        Output("briefing-consequences", "children"),
        Output("briefing-watch", "children"),
        Output("briefing-weather", "children"),
        Output("briefing-sources", "children"),
        Output("briefing-text", "children"),
        Input("briefing-interval", "n_intervals"),
        Input("briefing-refresh-btn", "n_clicks"),
        Input("briefing-window", "value"))
    def refresh(_interval, _clicks, window_minutes):
        snapshot = briefing_model.build_briefing_snapshot(
            window_minutes=window_minutes or briefing_model.DEFAULT_WINDOW_MINUTES)
        generated = html.Div([
            html.Div("Generated at", className="muted",
                     style={"fontSize": "12px"}),
            html.Div(snapshot.generated_label,
                     style={"fontSize": "18px", "fontWeight": "600"}),
            html.Div("Changes since: last %s" % snapshot.window_label,
                     className="muted", style={"fontSize": "12px"}),
        ])
        intro = ("Detected changes in the last %s, most severe first."
                 % snapshot.window_label)
        return (generated, _staleness_banner(snapshot), _situation(snapshot),
                intro, _changes(snapshot), _warnings(snapshot),
                _consequences(snapshot), _watch(snapshot), _weather(snapshot),
                _sources(snapshot), briefing_model.briefing_text(snapshot))

    @app.callback(
        Output("briefing-pdf-download", "data"),
        Output("briefing-pdf-status", "children"),
        Input("briefing-pdf-btn", "n_clicks"),
        State("briefing-window", "value"),
        prevent_initial_call=True)
    def make_pdf(_clicks, window_minutes):
        from dash import no_update

        from app import reporting
        try:
            snapshot = briefing_model.build_briefing_snapshot(
                window_minutes=window_minutes
                or briefing_model.DEFAULT_WINDOW_MINUTES)
            filename, pdf_bytes = reporting.build_briefing_pdf(snapshot)
        except reporting.ReportingUnavailable as e:
            return no_update, "⚠ %s" % e
        except Exception as e:
            return no_update, "⚠ Could not build briefing: %s" % e
        return dcc.send_bytes(pdf_bytes, filename), "✅ Briefing PDF generated."

# Passive Monitor — Copyright (c) 2026 SirTophamMatt. All rights reserved.
"""Incident Statistics page (public, read-only) — renders `app/stats.py`.

Filter by an operational day (24 h to 06:00), since 06:00 today, a financial
year or an event tag. Number cards (with the split by agency, the change against
the previous period and a sparkline), the active-now cards, trend charts and
breakdown tables all come from one `stats.Snapshot`.
"""
from datetime import date, datetime

import plotly.graph_objects as go
from dash import Input, Output, dcc, html

from app import stats, tags as tag_store, ui

# Categorical slots in fixed order (dataviz reference palette), light / dark.
COLOURS = {
    "rfa":       ("#2a78d6", "#3987e5"),
    "structure": ("#eb6834", "#d95926"),
    "grass":     ("#1baf7a", "#199e70"),
    "incidents": ("#eda100", "#c98500"),
}
ACTIVE_COLOURS = {"incidents": COLOURS["incidents"], "fires": COLOURS["structure"]}

PERIOD_OPTIONS = [
    {"label": "24 h to 06:00", "value": stats.PERIOD_DAY},
    {"label": "Since 06:00 (in progress)", "value": stats.PERIOD_TODAY},
    {"label": "Financial year", "value": stats.PERIOD_FY},
    {"label": "Event", "value": stats.PERIOD_EVENT},
]


def _colour(key, dark):
    light, dk = COLOURS[key]
    return dk if dark else light


def _fy_options():
    cov = stats.coverage_start()
    first = stats.fy_start_year(cov.date()) if cov else None
    return [{"label": label, "value": y} for y, label in stats.fy_options(first_year=first)]


def _event_options():
    return [{"label": t["name"] + ("" if t.get("end_ts") else " (ongoing)"),
             "value": t["id"]} for t in tag_store.list_tags()]


def layout():
    fy = _fy_options()
    return html.Div([
        html.H2("Incident Statistics"),
        html.P("Incidents that started in the chosen period — SES requests for "
               "assistance, structure fires and grass & scrub/bush fires — and "
               "what is active right now.", className="muted"),
        dcc.Interval(id="stats-interval", interval=60_000, n_intervals=0),
        html.Div([
            html.Div([
                html.Label("Period", className="stats-filter-label"),
                dcc.RadioItems(id="stats-period", options=PERIOD_OPTIONS,
                               value=stats.PERIOD_DAY, inline=True,
                               className="stats-period",
                               persistence=True, persistence_type="local"),
            ], className="stats-filter"),
            html.Div([
                html.Label("24 h ending 06:00 on", className="stats-filter-label"),
                dcc.DatePickerSingle(id="stats-day", display_format="DD/MM/YYYY",
                                     date=stats.last_completed_day().isoformat(),
                                     max_date_allowed=stats.last_completed_day().isoformat(),
                                     first_day_of_week=1),
            ], id="stats-day-box", className="stats-filter"),
            html.Div([
                html.Label("Financial year", className="stats-filter-label"),
                dcc.Dropdown(id="stats-fy", options=fy, clearable=False,
                             value=fy[0]["value"] if fy else None,
                             style={"minWidth": "160px"}),
            ], id="stats-fy-box", className="stats-filter", style={"display": "none"}),
            html.Div([
                html.Label("Event", className="stats-filter-label"),
                dcc.Dropdown(id="stats-event", options=_event_options(),
                             placeholder="Choose an event tag",
                             style={"minWidth": "260px"}),
            ], id="stats-event-box", className="stats-filter", style={"display": "none"}),
        ], className="panel stats-filters"),
        html.Div(id="stats-period-label", className="stats-period-label"),
        html.Div(id="stats-notes"),

        html.H3("Started in this period"),
        html.Div(id="stats-cards", className="stats-card-row"),

        html.H3("Active now"),
        html.Div(id="stats-active", className="stats-card-row"),

        html.Div([
            html.Div(dcc.Graph(id="stats-trend", config={"displayModeBar": False}),
                     className="graph-card"),
            html.Div(dcc.Graph(id="stats-active-trend", config={"displayModeBar": False}),
                     className="graph-card"),
        ], className="graph-grid"),

        html.H3("Breakdown"),
        html.Div(id="stats-breakdown", className="panel-row"),
        html.P(stats.CAVEAT, className="muted stats-caveat"),
    ])


# --------------------------------------------------------------------------- #
# Cards
# --------------------------------------------------------------------------- #
def _delta(now_v, prev_v, label):
    if prev_v is None:
        return None
    diff = now_v - prev_v
    if diff == 0:
        text, cls = "No change", "stats-delta-flat"
    else:
        arrow = "▲" if diff > 0 else "▼"
        pct = f" ({diff / prev_v:+.0%})" if prev_v else ""
        text = f"{arrow} {abs(diff):,}{pct}"
        cls = "stats-delta-up" if diff > 0 else "stats-delta-down"
    return html.Div([html.Span(text, className=cls),
                     html.Span(f" vs {label} ({prev_v:,})", className="muted")],
                    className="stats-delta")


def _sparkline(trend, key, colour, unit):
    if trend is None or trend.empty or len(trend) < 2:
        return None
    fig = go.Figure(go.Bar(
        x=trend["label"], y=trend[key], marker_color=colour,
        hovertemplate="%{x}: %{y}<extra></extra>"))
    fig.update_layout(height=56, margin=dict(l=0, r=0, t=4, b=0), bargap=0.15,
                      paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                      xaxis=dict(visible=False), yaxis=dict(visible=False),
                      showlegend=False)
    return dcc.Graph(figure=fig, config={"displayModeBar": False},
                     className="stats-spark")


def stat_card(label, value, colour, split=None, delta=None, spark=None, foot=None):
    """A number card: label, big value, optional split / delta / sparkline."""
    body = [html.Div(label, className="kpi-label"),
            html.Div(f"{value:,}", className="kpi-value")]
    if split:
        body.append(html.Div([html.Span([html.Strong(f"{v:,}"), f" {k}"],
                                        className="stats-split-item")
                              for k, v in split], className="stats-split"))
    if delta is not None:
        body.append(delta)
    if spark is not None:
        body.append(spark)
    if foot:
        body.append(html.Div(foot, className="muted stats-foot"))
    return html.Div(body, className="kpi-card stats-card",
                    style={"borderTop": f"4px solid {colour}"})


def period_cards(snap, dark):
    t, p = snap.totals, snap.previous
    lab = snap.period.previous_label

    def card(key, label, split=None):
        colour = _colour(key, dark)
        return stat_card(label, t[key], colour, split=split,
                         delta=_delta(t[key], p[key], lab) if p else None,
                         spark=_sparkline(snap.trend, key, colour, snap.unit))
    return [
        card("rfa", "SES RFAs"),
        card("structure", "Structure fires",
             [("CFA", t["structure_cfa"]), ("FRV", t["structure_frv"])]),
        card("grass", "Grass & scrub/bush fires",
             [("CFA", t["grass_cfa"]), (stats.FFM_LABEL, t["grass_ffm"])]),
        card("incidents", "All incidents",
             [("fire" if t["fires"] == 1 else "fires", t["fires"])]),
    ]


def active_cards(snap, dark):
    a = snap.active
    rfa_foot = (" · ".join(f"{k} {v}" for k, v in snap.active_rfa_types[:3])
                if snap.active_rfa_types else None)
    return [
        stat_card("Active open incidents", a["incidents"], _colour("incidents", dark),
                  foot="Every agency, warnings excluded"),
        stat_card("Active SES RFAs", a["rfa"], _colour("rfa", dark), foot=rfa_foot),
        stat_card("Active fires", a["fires"], _colour("structure", dark),
                  foot="Fire incidents only — never warnings"),
    ]


# --------------------------------------------------------------------------- #
# Charts + tables
# --------------------------------------------------------------------------- #
_UNIT_WORD = {"hour": "hour", "day": "day (06:00–06:00)", "month": "month"}


_PERIOD_MS = {"hour": 3_600_000, "day": 86_400_000, "month": "M1"}
_HOVER_FMT = {"hour": "%a %d %b %H:00", "day": "%a %d %b (from 06:00)", "month": "%b %Y"}


def trend_figure(snap, dark):
    fig = go.Figure()
    tr = snap.trend
    for key, label in stats.METRICS:
        if key == "incidents":
            continue
        fig.add_bar(x=tr["bucket"], y=tr[key], name=label,
                    marker_color=_colour(key, dark),
                    xperiod=_PERIOD_MS[snap.unit], xperiodalignment="middle",
                    hovertemplate="%{x|" + _HOVER_FMT[snap.unit] + "}<br>" + label
                    + ": %{y}<extra></extra>")
    top = max([int(tr[k].max()) for k, _ in stats.METRICS if k != "incidents"]
              + [0]) if not tr.empty else 0
    fig.update_layout(title=f"Started per {_UNIT_WORD[snap.unit]}", height=340,
                      barmode="group", bargap=0.2, bargroupgap=0.06,
                      legend=dict(orientation="h", y=1.0, yanchor="bottom", x=0),
                      yaxis=dict(title="Incidents", rangemode="tozero",
                                 dtick=1 if top <= 5 else None,
                                 range=[0, 1] if top == 0 else None),
                      xaxis=dict(type="date"))
    fig = ui.apply_theme(fig, dark)
    fig.update_layout(margin=dict(t=80))
    return fig


def active_trend_figure(snap, dark):
    df = snap.active_trend
    fig = go.Figure()
    if df is None or df.empty:
        fig.update_layout(title="Active over the period — no collector data")
    else:
        for key, label in (("incidents", "Active incidents"), ("fires", "Active fires")):
            light, dk = ACTIVE_COLOURS[key]
            fig.add_scatter(x=df["bucket"], y=df[key], name=label, mode="lines",
                            line=dict(color=dk if dark else light, width=2,
                                      shape="hv"),
                            hovertemplate="%{x|%a %d %b %H:%M}<br>" + label
                            + ": %{y}<extra></extra>")
        fig.update_layout(title=f"Active over the period (peak per {snap.active_unit})")
    fig.update_layout(height=340, legend=dict(orientation="h", y=1.0, yanchor="bottom", x=0),
                      yaxis=dict(title="Active", rangemode="tozero"),
                      hovermode="x unified")
    fig = ui.apply_theme(fig, dark)
    fig.update_layout(margin=dict(t=80))
    return fig


def _list_table(rows, head, empty):
    if not rows:
        return html.Div(empty, className="muted")
    return html.Table([
        html.Thead(html.Tr([html.Th(head), html.Th("Count", className="num")])),
        html.Tbody([html.Tr([html.Td(k), html.Td(f"{v:,}", className="num")])
                    for k, v in rows]),
    ], className="trend-table stats-table")


def _matrix_table(rows):
    cols = stats.AGENCY_COLUMNS
    return html.Table([
        html.Thead(html.Tr([html.Th("Fire type")]
                           + [html.Th(label, className="num") for _, label in cols]
                           + [html.Th("Total", className="num")])),
        html.Tbody([html.Tr([html.Td(r["kind"])]
                            + [html.Td(f"{r[k]:,}", className="num") for k, _ in cols]
                            + [html.Td(html.Strong(f"{r['total']:,}"), className="num")])
                    for r in rows]),
    ], className="trend-table stats-table")


def breakdown(snap):
    return [
        html.Div([html.H4("Fires by type and agency"), _matrix_table(snap.fire_matrix)],
                 className="panel stats-wide"),
        html.Div([html.H4("SES RFAs by type"),
                  _list_table(snap.rfa_types, "Type", "No SES RFAs in this period.")],
                 className="panel"),
        html.Div([html.H4("SES RFAs by locality"),
                  _list_table(snap.rfa_localities, "Locality",
                              "No SES RFAs in this period.")], className="panel"),
        html.Div([html.H4("Fires by category"),
                  _list_table(snap.fire_types, "Category", "No fires in this period.")],
                 className="panel"),
    ]


def _notes(snap):
    return [html.Div(n, className="stats-note") for n in snap.notes]


def register_callbacks(app):
    @app.callback(
        Output("stats-day-box", "style"),
        Output("stats-fy-box", "style"),
        Output("stats-event-box", "style"),
        Input("stats-period", "value"))
    def show_filters(kind):
        def vis(on):
            return {} if on else {"display": "none"}
        return (vis(kind == stats.PERIOD_DAY), vis(kind == stats.PERIOD_FY),
                vis(kind == stats.PERIOD_EVENT))

    @app.callback(
        Output("stats-period-label", "children"),
        Output("stats-notes", "children"),
        Output("stats-cards", "children"),
        Output("stats-active", "children"),
        Output("stats-trend", "figure"),
        Output("stats-active-trend", "figure"),
        Output("stats-breakdown", "children"),
        Input("stats-interval", "n_intervals"),
        Input("stats-period", "value"),
        Input("stats-day", "date"),
        Input("stats-fy", "value"),
        Input("stats-event", "value"),
        Input("theme-store", "data"))
    def refresh(_, kind, day, fy, event, dark):
        dark = bool(dark)
        tag = tag_store.get_tag(event) if (kind == stats.PERIOD_EVENT and event) else None
        try:
            day_value = date.fromisoformat(str(day)[:10]) if day else None
        except ValueError:
            day_value = None
        period = stats.resolve_period(kind, day=day_value, fy=fy, tag=tag)
        if period is None:
            empty = ui.apply_theme(go.Figure(), dark)
            msg = html.Div("Choose an event tag (event tags are created on Admin).",
                           className="stats-note")
            return "", msg, [], [], empty, empty, []
        snap = stats.cached(period, datetime.now())
        return (period.label, _notes(snap), period_cards(snap, dark),
                active_cards(snap, dark), trend_figure(snap, dark),
                active_trend_figure(snap, dark), breakdown(snap))

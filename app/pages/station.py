"""Station detail page (public): one gauge, its own graph, a linear flood
gauge "stick" showing where the water is against the flood class levels, and
the watch points / expected impacts extracted from the VICSES Local Flood
Guides (seed/lfg_impacts.json). Routed as /flood/station/<station_key>.
"""
import logging
import textwrap
from urllib.parse import quote, unquote

import pandas as pd
import plotly.graph_objects as go
from dash import Input, Output, State, dcc, html

from app import ui
from app.modules.flood import data as flood_data
from app.modules.flood import trend

log = logging.getLogger(__name__)

HISTORY_CHOICES = [("7", "Past 7 days"), ("30", "Past 30 days"),
                   ("90", "Past 90 days"), ("all", "All data")]

CLASS_COLOURS = {"major": "#d62728", "moderate": "#ff7f0e",
                 "minor": "#e6c700", "below": "#9aa0a6"}


def path_for(station_name):
    """URL for a station's detail page."""
    return "/flood/station/" + quote(str(station_name).strip().lower())


def key_from_path(pathname):
    return unquote(pathname[len("/flood/station/"):]).strip().lower()


def _class_of(height, levels):
    """Which flood class band a height sits in ('major'/'moderate'/'minor'/'below')."""
    if levels:
        for cls in ("major", "moderate", "minor"):
            value = levels.get(cls)
            if value is not None and pd.notna(value) and height >= value:
                return cls
    return "below"


def _wrap(text, width=64):
    return "<br>".join(textwrap.wrap(str(text), width))


def build_gauge_stick(current, levels, impacts_df, dark, title="Gauge"):
    """A vertical 'flood gauge stick': class-level bands, the current water
    level, and a marker per Local Flood Guide impact height (hover for the
    impact text)."""
    fig = go.Figure()

    heights = []
    if current is not None and pd.notna(current):
        heights.append(float(current))
    lv = {k: (float(levels[k]) if levels and levels.get(k) is not None
              and pd.notna(levels.get(k)) else None)
          for k in ("minor", "moderate", "major")} if levels else {}
    heights += [v for v in lv.values() if v is not None]
    if impacts_df is not None and not impacts_df.empty:
        heights += impacts_df["height_m"].dropna().tolist()
    if not heights:
        heights = [0.0, 1.0]

    lo, hi = min(heights), max(heights)
    span = max(hi - lo, 0.5)
    top = hi + span * 0.10
    base = lo - span * 0.15
    if lo >= 0 and base < 0 and lo < 20:
        base = 0.0  # normal river gauges read from ~0; AHD gauges keep offset

    # Class bands (background)
    band_alpha = 0.28 if dark else 0.22
    bands = []
    if lv.get("minor") is not None:
        upper = lv.get("moderate") if lv.get("moderate") is not None else (
            lv.get("major") if lv.get("major") is not None else top)
        bands.append((lv["minor"], upper, f"rgba(230,199,0,{band_alpha})"))
    if lv.get("moderate") is not None:
        upper = lv.get("major") if lv.get("major") is not None else top
        bands.append((lv["moderate"], upper, f"rgba(255,127,14,{band_alpha})"))
    if lv.get("major") is not None:
        bands.append((lv["major"], top, f"rgba(214,39,40,{band_alpha})"))
    for y0, y1, colour in bands:
        fig.add_shape(type="rect", x0=0.18, x1=0.62, y0=y0, y1=min(y1, top),
                      fillcolor=colour, line_width=0, layer="below")

    # The stick outline
    outline = "#aab2bd" if dark else "#5f6368"
    fig.add_shape(type="rect", x0=0.18, x1=0.62, y0=base, y1=top,
                  line=dict(color=outline, width=2), layer="below")

    # Water column
    if current is not None and pd.notna(current) and current > base:
        fig.add_shape(type="rect", x0=0.18, x1=0.62, y0=base,
                      y1=min(float(current), top),
                      fillcolor="rgba(31,119,180,0.55)", line_width=0)
        fig.add_shape(type="line", x0=0.10, x1=0.70, y0=float(current),
                      y1=float(current),
                      line=dict(color="#1f77b4", width=3))
        fig.add_annotation(x=0.10, y=float(current), text=f"<b>{current:.2f} m</b>",
                           showarrow=False, xanchor="right",
                           font=dict(color="#1f77b4", size=13))

    # Class level lines + labels
    for cls, colour in (("minor", "#e6c700"), ("moderate", "#ff7f0e"),
                        ("major", "#d62728")):
        v = lv.get(cls)
        if v is None:
            continue
        fig.add_shape(type="line", x0=0.18, x1=0.62, y0=v, y1=v,
                      line=dict(color=colour, width=2, dash="dash"))
        fig.add_annotation(x=0.64, y=v, text=f"{cls.title()} {v:g} m",
                           showarrow=False, xanchor="left",
                           font=dict(color=colour, size=11))

    # Impact markers (hover = the impact text)
    if impacts_df is not None and not impacts_df.empty:
        imp = impacts_df.dropna(subset=["height_m"])
        fig.add_trace(go.Scatter(
            x=[0.15] * len(imp), y=imp["height_m"],
            mode="markers",
            marker=dict(symbol="triangle-right", size=11,
                        color="#1f77b4" if not dark else "#7ab8e8"),
            hovertext=[f"<b>{h:.2f} m</b><br>{_wrap(t)}"
                       for h, t in zip(imp["height_m"], imp["impact"])],
            hoverinfo="text", showlegend=False))

    fig.update_xaxes(visible=False, range=[-0.15, 1.15], fixedrange=True)
    fig.update_yaxes(title="Gauge height (m)", range=[base, top],
                     fixedrange=True)
    fig.update_layout(title=title, height=560, showlegend=False,
                      hoverlabel=dict(align="left"))
    return ui.apply_theme(fig, dark)


def _history_figure(hist, station_name, label, levels, dark, analysis=None):
    from app.pages.flood import _station_figure
    fig = _station_figure(hist, station_name, label, levels, dark)
    fig.update_layout(height=560)
    _add_rainfall_overlay(fig, hist, station_name)
    if analysis:
        _add_projection(fig, analysis)
    return fig


def _add_projection(fig, analysis):
    """Draw the trend projection as a cone from the last reading to the target
    threshold: a dashed centre line at the fitted rate, bounded by the early
    and late arrivals. Drawn only when a projection was actually supportable,
    so a steady gauge's graph stays clean."""
    if not analysis or not analysis.get("eta_point"):
        return
    start_t, start_h = analysis["observed_at"], analysis["current_height"]
    target = analysis["target_height"]
    # The bounds first, as a filled cone, so the dashed centre line sits on top.
    fig.add_trace(go.Scatter(
        x=[start_t, analysis["eta_early"], analysis["eta_late"], start_t],
        y=[start_h, target, target, start_h],
        fill="toself", fillcolor="rgba(214,39,40,0.13)",
        line=dict(width=0), hoverinfo="skip",
        name="Projection range", showlegend=False))
    fig.add_trace(go.Scatter(
        x=[start_t, analysis["eta_point"]], y=[start_h, target],
        mode="lines", line=dict(color="#d62728", width=2, dash="dot"),
        name=f"Trend projection → {str(analysis['target_name']).title()}",
        hovertext=(f"Projected {str(analysis['target_name']).title()} "
                   f"({target:.2f} m) between "
                   f"{analysis['eta_early']:%H:%M} and "
                   f"{analysis['eta_late']:%H:%M}<br>"
                   "Trend projection — not an official flood forecast"),
        hoverinfo="text"))
    fig.update_layout(showlegend=True, legend=dict(orientation="h", y=1.08))


def _add_rainfall_overlay(fig, hist, station_name):
    """Overlay rain-since-9am for the nearest monitored town on a secondary
    axis — the upstream leading indicator. Only shown when there's actual rain
    in the displayed window, so dry periods stay uncluttered."""
    from app.modules.weather import data as weather_data
    loc = weather_data.location_for_gauge(station_name)
    if not loc:
        return
    rain = weather_data.rainfall_history(loc["location_key"], days=None)
    if rain.empty:
        return
    if not hist.empty:
        rain = rain[rain["timestamp"] >= hist["timestamp"].min()]
    rain = rain.dropna(subset=["rain_since_9am_mm"])
    if rain.empty or rain["rain_since_9am_mm"].max() <= 0:
        return
    fig.add_trace(go.Scatter(
        x=rain["timestamp"], y=rain["rain_since_9am_mm"], yaxis="y2",
        name=f"Rain @ {loc['name']} (mm)", mode="lines",
        line=dict(color="#1f77b4", width=1, dash="dot"),
        fill="tozeroy", fillcolor="rgba(31,119,180,0.12)"))
    fig.update_layout(
        yaxis2=dict(title="Rain since 9am (mm)", overlaying="y", side="right",
                    showgrid=False, rangemode="tozero"),
        showlegend=True, legend=dict(orientation="h", y=1.08))


def _fmt_signed(value, unit="", dp=2):
    return "—" if value is None else f"{value:+.{dp}f} {unit}".strip()


def _trend_panel(analysis, rainfall):
    """Rate / acceleration / threshold distance / projection, plus the readings
    the numbers were computed from — showing the working, so the projection can
    be judged rather than believed."""
    if not analysis:
        return html.Div("Not enough recent observations to compute a trend.",
                        className="muted")

    accel = analysis["accel_label"]
    accel_colour = {"Rate increasing": "#d62728", "Rate easing": "#2ca02c"}.get(
        accel, "#9aa0a6")
    rate = analysis["rate_m_hr"]
    target = analysis["target_name"]

    if analysis.get("eta_point"):
        eta_value = (f"{analysis['eta_early']:%H:%M}–{analysis['eta_late']:%H:%M}")
        eta_label = f"{str(target).title()} potentially reached"
        eta_colour = "#d62728"
    else:
        eta_value = "—"
        eta_label = "No projection"
        eta_colour = None

    if analysis["distance_m"] is None:
        distance = "—"
        distance_label = "Threshold distance"
    else:
        distance = f"{analysis['distance_m']:.2f} m"
        distance_label = f"Below {str(target).title()}"

    cards = html.Div([
        ui.kpi_card("Rate", _fmt_signed(rate, "m/hr"),
                    "#d62728" if rate and rate > 0 else "#9aa0a6"),
        ui.kpi_card("Acceleration", accel, accel_colour),
        ui.kpi_card(distance_label, distance,
                    CLASS_COLOURS.get(str(target), None)
                    if analysis["target_kind"] == "class" else None),
        ui.kpi_card(eta_label, eta_value, eta_colour),
    ], className="kpi-row")

    summary_bits = [html.Strong(analysis["headline"])]
    if rate is not None and analysis["span_minutes"]:
        summary_bits.append(html.Span(
            f" · {_fmt_signed(rate, 'm/hr')} over the last "
            f"{analysis['span_minutes']} minutes"))
    if rainfall and rainfall.get("max_mm"):
        where = rainfall.get("catchment") or "the surrounding catchment"
        summary_bits.append(html.Span(
            f" · {rainfall['max_mm']:.0f} mm rain in {where} "
            f"({rainfall['wettest']}, "
            f"{rainfall['station_count']} station"
            f"{'s' if rainfall['station_count'] != 1 else ''} within "
            f"{rainfall['radius_km']:g} km, last "
            f"{rainfall['window_hours']:g} h)"))
    summary = html.Div(summary_bits, className="trend-summary")

    body = [cards, summary]
    if analysis.get("eta_reason") and not analysis.get("eta_point"):
        body.append(html.Div(analysis["eta_reason"], className="muted",
                             style={"marginTop": "4px"}))
    body.append(html.Div([
        html.Span("Trend projection — not an official flood forecast. ",
                  className="trend-disclaimer-strong"),
        html.Span(
            "Straight-line extrapolation of the readings below; it does not "
            "model rainfall, catchment routing or upstream inflows. For "
            "official warnings and forecasts see the Bureau of Meteorology "
            "and VICSES."),
    ], className="trend-disclaimer"))
    body.append(_readings_table(analysis["readings"]))
    return html.Div(body)


def _readings_table(readings):
    """The measurements the fit used, with each step's change — which is what
    makes 'Rate increasing' legible rather than an assertion."""
    if not readings:
        return html.Div()
    rows = [html.Tr([html.Th("Time"), html.Th("Height"), html.Th("Change")])]
    previous = None
    for ts, height in readings[-12:]:
        if previous is None:
            delta = ""
        else:
            step = height - previous
            delta = html.Span(f"{step:+.2f} m",
                              className="trend-up" if step > 0 else
                              ("trend-down" if step < 0 else "muted"))
        rows.append(html.Tr([
            html.Td(f"{ts:%H:%M}", className="trend-time"),
            html.Td(f"{height:.2f} m", className="trend-height"),
            html.Td(delta),
        ]))
        previous = height
    return html.Div([
        html.Div("Measurements used", className="trend-table-title"),
        html.Table(rows, className="trend-table"),
    ])


def _accuracy_panel(summary, title, blurb):
    """The back-check: how the projections have actually performed."""
    if not summary or not summary["total"]:
        pending = summary["pending"] if summary else 0
        return html.Div([
            html.H4(title),
            html.Div(
                f"No projections have been verified yet"
                + (f" ({pending} awaiting their outcome)." if pending
                   else " — one is scored as soon as its window closes."),
                className="muted"),
        ], className="panel")

    def pct(value):
        return "—" if value is None else f"{value * 100:.0f}%"

    def minutes(value):
        return "—" if value is None else f"{value:+.0f} min"

    rows = [html.Tr([html.Th("Lead time"), html.Th("Projections"),
                     html.Th("Reached"), html.Th("In window"),
                     html.Th("Median error")])]
    for bucket in summary["by_lead"]:
        rows.append(html.Tr([
            html.Td(bucket["label"]),
            html.Td(str(bucket["total"])),
            html.Td(pct(bucket["hit_rate"])),
            html.Td(pct(bucket["within_range_rate"])),
            html.Td("—" if bucket["median_abs_error_minutes"] is None
                    else f"{bucket['median_abs_error_minutes']:.0f} min"),
        ]))

    return html.Div([
        html.H4(title),
        html.Div([
            ui.kpi_card("Verified", str(summary["total"])),
            ui.kpi_card("Threshold reached", pct(summary["hit_rate"])),
            ui.kpi_card("Inside quoted window", pct(summary["within_range_rate"])),
            ui.kpi_card("Median error",
                        "—" if summary["median_abs_error_minutes"] is None
                        else f"{summary['median_abs_error_minutes']:.0f} min"),
            ui.kpi_card("Bias", minutes(summary["median_error_minutes"])),
        ], className="kpi-row"),
        html.Div(blurb, className="muted", style={"marginBottom": "8px"}),
        html.Table(rows, className="trend-table") if summary["by_lead"] else html.Div(),
        html.Div(f"{summary['pending']} projection(s) still open · "
                 f"method {summary['method']} · a positive bias means the water "
                 "arrived later than projected.",
                 className="muted", style={"marginTop": "8px",
                                           "fontSize": "12px"}),
    ], className="panel")


def _impact_table(impacts_df, current, levels):
    """Watch points / impacts as an HTML table, severity-coloured, with the
    rows the water has already reached flagged."""
    header = html.Tr([html.Th("Height"), html.Th("Status"),
                      html.Th("Expected impacts / previous floods"),
                      html.Th("Guide")])
    rows = [header]
    for _, r in impacts_df.iterrows():
        h = r["height_m"]
        cls = _class_of(h, levels)
        reached = current is not None and pd.notna(current) and current >= h
        badge = html.Span("● reached", className="impact-reached") if reached else ""
        rows.append(html.Tr([
            html.Td(f"{h:.2f} m", className="impact-height",
                    style={"borderLeft": f"6px solid {CLASS_COLOURS[cls]}"}),
            html.Td(badge),
            html.Td(r["impact"]),
            html.Td(r["town"], className="muted"),
        ], className="impact-row-reached" if reached else ""))
    return html.Table(rows, className="impact-table")


def layout(station_key):
    return html.Div([
        dcc.Store(id="station-key", data=station_key),
        dcc.Interval(id="station-interval", interval=60_000, n_intervals=0),
        html.Div([
            dcc.Link("← Flood Monitor", href="/flood", className="muted"),
        ]),
        html.Div([
            html.H2(id="station-title", style={"display": "inline-block"}),
            html.Button("⤓ Gauge Briefing PDF", id="station-pdf-btn",
                        className="btn btn-primary",
                        style={"float": "right", "marginTop": "6px"}),
            dcc.Download(id="station-pdf-download"),
            html.Div(id="station-pdf-status", className="muted",
                     style={"clear": "both"}),
        ]),
        html.Div(id="station-kpis", className="kpi-row"),
        html.Div([
            html.Label("History window ", className="muted"),
            dcc.Dropdown(id="station-history-window",
                         options=[{"label": lbl, "value": v}
                                  for v, lbl in HISTORY_CHOICES],
                         value="30", clearable=False, className="dropdown",
                         style={"width": "180px", "display": "inline-block",
                                "verticalAlign": "middle"}),
        ], style={"margin": "8px 0"}),
        html.H3("Rate of rise"),
        html.Div(id="station-trend"),
        html.Div([
            html.Div(dcc.Graph(id="station-stick"),
                     className="graph-card station-stick-card"),
            html.Div(dcc.Graph(id="station-graph"),
                     className="graph-card station-graph-card"),
        ], className="station-row"),
        html.Div(id="station-accuracy", style={"marginTop": "6px"}),
        html.H3("Watch points & expected impacts"),
        html.Div(id="station-impacts"),
        html.Div(id="station-source", className="muted",
                 style={"marginTop": "10px", "fontSize": "12px"}),
    ])


def register_callbacks(app):
    @app.callback(
        Output("station-title", "children"),
        Output("station-kpis", "children"),
        Output("station-trend", "children"),
        Output("station-accuracy", "children"),
        Output("station-stick", "figure"),
        Output("station-graph", "figure"),
        Output("station-impacts", "children"),
        Output("station-source", "children"),
        Input("station-interval", "n_intervals"),
        Input("station-history-window", "value"),
        Input("theme-store", "data"),
        State("station-key", "data"))
    def refresh(_, window, dark, station_key):
        dark = bool(dark)
        latest = flood_data.station_latest(station_key)
        station_name = (latest or {}).get("station_name") or station_key.title()
        levels = flood_data.load_flood_levels().get(station_key)
        impacts = flood_data.load_gauge_impacts(station_key)

        current = latest["height_m"] if latest else None
        priority, label, colour = flood_data.classify_station(
            current if current is not None else float("nan"), levels)

        kpis = [
            ui.kpi_card("Latest height",
                        f"{current:.2f} m" if current is not None and
                        pd.notna(current) else "—", colour),
            ui.kpi_card("Classification", label, colour),
            ui.kpi_card("Tendency", (latest or {}).get("tendency") or "—"),
            ui.kpi_card("Last observation",
                        str((latest or {}).get("timestamp") or "no data")),
        ]
        if levels:
            for cls in ("minor", "moderate", "major"):
                v = levels.get(cls)
                if v is not None and pd.notna(v):
                    kpis.append(ui.kpi_card(f"{cls.title()} level", f"{v:g} m",
                                            CLASS_COLOURS[cls]))

        # Rate-of-rise analysis drives the trend panel AND the projection cone
        # on the history graph, so it is computed once here.
        try:
            analysis = trend.analyse(station_key, levels=levels, impacts=impacts)
            rainfall = trend.catchment_rainfall(station_key)
        except Exception:
            log.exception("Trend analysis failed for %s", station_key)
            analysis, rainfall = None, None
        trend_panel = _trend_panel(analysis, rainfall)
        accuracy = _accuracy_panel(
            trend.accuracy_summary(station_key=station_key),
            "Projection track record at this gauge",
            "Every projection this page has shown was recorded when it was "
            "made and scored against what the gauge actually did.")

        stick = build_gauge_stick(current, levels, impacts, dark,
                                  title="Flood gauge")
        days = None if window == "all" else int(window)
        hist = flood_data.station_history(station_key, days)
        if hist.empty and days:
            hist = flood_data.station_history(station_key, None)
        if hist.empty:
            graph = ui.apply_theme(go.Figure(layout=dict(
                title="No observations recorded for this station yet",
                height=560)), dark)
        else:
            graph = _history_figure(hist, station_name, label, levels, dark,
                                    analysis=analysis)

        if impacts.empty:
            table = html.Div("No Local Flood Guide impact information is "
                             "available for this gauge.", className="muted")
            source = ""
        else:
            table = _impact_table(impacts, current, levels)
            guides = impacts[["town", "source_pdf"]].drop_duplicates()
            source = ("Impact information extracted from VICSES Local Flood "
                      "Guide(s): " +
                      "; ".join(f"{t} ({s})" for t, s in
                                zip(guides["town"], guides["source_pdf"])) +
                      ". Impacts are indicative — no two floods are the same.")
        return (station_name, kpis, trend_panel, accuracy, stick, graph,
                table, source)

    @app.callback(
        Output("station-pdf-download", "data"),
        Output("station-pdf-status", "children"),
        Input("station-pdf-btn", "n_clicks"),
        State("station-key", "data"),
        prevent_initial_call=True)
    def make_pdf(_, station_key):
        from dash import no_update

        from app import reporting
        try:
            filename, pdf_bytes = reporting.build_station_pdf(station_key)
        except reporting.ReportingUnavailable as e:
            return no_update, f"⚠ {e}"
        except Exception as e:
            return no_update, f"⚠ Could not build briefing: {e}"
        return dcc.send_bytes(pdf_bytes, filename), "✅ Briefing generated."

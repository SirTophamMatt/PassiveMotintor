"""Station detail page (public): one gauge, its own graph, a linear flood
gauge "stick" showing where the water is against the flood class levels, and
the watch points / expected impacts extracted from the VICSES Local Flood
Guides (seed/lfg_impacts.json). Routed as /flood/station/<station_key>.
"""
import textwrap
from urllib.parse import quote, unquote

import pandas as pd
import plotly.graph_objects as go
from dash import Input, Output, State, dcc, html

from app import ui
from app.modules.flood import data as flood_data

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


def _history_figure(hist, station_name, label, levels, dark):
    from app.pages.flood import _station_figure
    fig = _station_figure(hist, station_name, label, levels, dark)
    fig.update_layout(height=560)
    return fig


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
        html.Div([
            html.Div(dcc.Graph(id="station-stick"),
                     className="graph-card station-stick-card"),
            html.Div(dcc.Graph(id="station-graph"),
                     className="graph-card station-graph-card"),
        ], className="station-row"),
        html.H3("Watch points & expected impacts"),
        html.Div(id="station-impacts"),
        html.Div(id="station-source", className="muted",
                 style={"marginTop": "10px", "fontSize": "12px"}),
    ])


def register_callbacks(app):
    @app.callback(
        Output("station-title", "children"),
        Output("station-kpis", "children"),
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
            graph = _history_figure(hist, station_name, label, levels, dark)

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
        return station_name, kpis, stick, graph, table, source

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

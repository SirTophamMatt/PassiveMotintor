"""Analytics page (admin-only): page views, unique visitors, top pages.

Privacy-preserving — built entirely from the local page_views table (daily
salted visitor hashes, no raw IPs). No third-party trackers.
"""
import plotly.express as px
from dash import Input, Output, dcc, html

from app import analytics, ui


def layout():
    return html.Div([
        html.H2("Analytics"),
        html.P("Visitor metrics for the public dashboards. Privacy-preserving: "
               "counts come from local logs using daily-rotating hashed visitor "
               "IDs — no IP addresses or personal data are stored.",
               className="muted"),
        dcc.Interval(id="analytics-interval", interval=60_000, n_intervals=0),
        html.Div(id="analytics-kpis", className="kpi-row"),
        html.H3("Views & visitors (last 30 days)"),
        html.Div(dcc.Graph(id="analytics-trend"), className="graph-card"),
        html.H3("Top pages (last 7 days)"),
        html.Div(dcc.Graph(id="analytics-top"), className="graph-card"),
    ])


def register_callbacks(app):
    @app.callback(
        Output("analytics-kpis", "children"),
        Output("analytics-trend", "figure"),
        Output("analytics-top", "figure"),
        Input("analytics-interval", "n_intervals"),
        Input("theme-store", "data"))
    def refresh(_, dark):
        dark = bool(dark)
        s = analytics.summary()
        kpis = [
            ui.kpi_card("Views (24h)", f"{s['24h']['views']:,}", "#1f77b4"),
            ui.kpi_card("Visitors (24h)", f"{s['24h']['visitors']:,}", "#2ca02c"),
            ui.kpi_card("Views (7d)", f"{s['7d']['views']:,}"),
            ui.kpi_card("Visitors (7d)", f"{s['7d']['visitors']:,}"),
            ui.kpi_card("Views (30d)", f"{s['30d']['views']:,}"),
        ]

        daily = analytics.views_by_day(days=30)
        if daily.empty:
            trend = px.line(title="No page views recorded yet")
        else:
            trend = px.line(
                daily, x="day", y=["views", "visitors"],
                labels={"day": "Date", "value": "Count", "variable": ""},
                title="Daily views and unique visitors", markers=True)
            trend.for_each_trace(lambda t: t.update(
                name={"views": "Views", "visitors": "Unique visitors"}.get(t.name, t.name)))
        trend.update_layout(height=340, legend=dict(orientation="h", y=1.12))

        top = analytics.top_pages(days=7)
        if top.empty:
            top_fig = px.bar(title="No page views recorded yet")
        else:
            top_fig = px.bar(
                top.sort_values("views"), x="views", y="path", orientation="h",
                labels={"views": "Views", "path": ""},
                hover_data=["visitors"], title="Most-viewed pages")
            top_fig.update_layout(height=380)

        return kpis, ui.apply_theme(trend, dark), ui.apply_theme(top_fig, dark)

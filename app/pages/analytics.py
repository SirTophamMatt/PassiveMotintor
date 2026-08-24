"""Analytics page (admin-only): page views, unique visitors, visitor origin.

Self-hosted, no third-party trackers — every figure comes from the local
``page_views`` table. Visitor origin is resolved from a TRUNCATED IP (see
``app/geoip.py``), so the geography here is accurate to a city and to a network,
and deliberately no finer.
"""
import plotly.express as px
from dash import Input, Output, dcc, html

from app import analytics, geoip, ui


def layout():
    return html.Div([
        html.H2("Analytics"),
        html.P("Visitor metrics for the public dashboards. Self-hosted, with no "
               "third-party trackers: unique visitors are counted with a "
               "daily-rotating salted hash, and locations are resolved from the "
               "visitor's IP with its host part removed — a whole network, "
               "never an individual address.", className="muted"),
        dcc.Interval(id="analytics-interval", interval=60_000, n_intervals=0),
        html.Div(id="analytics-kpis", className="kpi-row"),
        html.H3("Views & visitors (last 30 days)"),
        html.Div(dcc.Graph(id="analytics-trend"), className="graph-card"),
        html.H3("Top pages (last 7 days)"),
        html.Div(dcc.Graph(id="analytics-top"), className="graph-card"),

        html.H3("Where visitors are (last 30 days)"),
        html.Div(id="analytics-geo-note", className="muted"),
        html.Div(dcc.Graph(id="analytics-geo-map", config=ui.MAP_CONFIG),
                 className="graph-card"),
        html.Div([
            html.Div([
                html.H4("By country"),
                html.Div(dcc.Graph(id="analytics-countries"),
                         className="graph-card"),
            ], className="panel"),
            html.Div([
                html.H4("Top cities"),
                html.Div(id="analytics-cities"),
            ], className="panel"),
        ], className="panel-row"),
        html.H4("Busiest networks"),
        html.P("Truncated to the network, with the operator where the provider "
               "reports one. A single network responsible for a large share of "
               "views is usually a crawler rather than an audience.",
               className="muted"),
        html.Div(id="analytics-networks"),
    ])


def _table(df, columns, empty="Nothing recorded yet."):
    """A plain themed table. These rows are short lists of labelled counts,
    which a DataTable would over-engineer; `trend-table` is the existing style
    for exactly this (see the station page) so the two cannot drift apart."""
    if df.empty:
        return html.Div(empty, className="muted")
    header = html.Tr([html.Th(label) for label, _ in columns])
    rows = [html.Tr([html.Td(fmt(r)) for _, fmt in columns])
            for _, r in df.iterrows()]
    return html.Table([html.Thead(header), html.Tbody(rows)],
                      className="trend-table")


def _place(r):
    """City, state, country — skipping the parts the provider didn't give."""
    return " · ".join(str(r[k]) for k in ("city", "region", "country")
                      if r.get(k))


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

    @app.callback(
        Output("analytics-geo-note", "children"),
        Output("analytics-geo-map", "figure"),
        Output("analytics-countries", "figure"),
        Output("analytics-cities", "children"),
        Output("analytics-networks", "children"),
        Input("analytics-interval", "n_intervals"),
        Input("theme-store", "data"))
    def refresh_geo(_, dark):
        dark = bool(dark)
        cov = analytics.location_coverage(days=30)

        if not geoip.enabled():
            note = ("Location lookup is turned off in Settings — views are "
                    "still counted, but no new locations are being resolved.")
        elif cov["total"] == 0:
            note = "No public page views in the last 30 days."
        else:
            # State the share explicitly: the first view from any new network
            # lands before its lookup completes, and private/LAN traffic never
            # resolves at all, so these charts are always a subset.
            note = ("%d of %d views (%.0f%%) have a resolved location. The rest "
                    "are private-network traffic, addresses the provider could "
                    "not place, or a network's first view — locations resolve in "
                    "the background, so those appear from the next view on."
                    % (cov["located"], cov["total"], cov["pct"]))

        points = analytics.located_points(days=30)
        if points.empty:
            geo_fig = px.scatter_mapbox(lat=[], lon=[], zoom=1)
        else:
            points = points.assign(place=points.apply(_place, axis=1))
            geo_fig = px.scatter_mapbox(
                points, lat="latitude", lon="longitude", size="views",
                color="visitors", hover_name="place",
                hover_data={"visitors": True, "views": True,
                            "latitude": False, "longitude": False},
                color_continuous_scale="Blues", size_max=28, zoom=2,
                center={"lat": -30.0, "lon": 140.0})
        # open-street-map needs no Mapbox token, same as every other map here.
        geo_fig.update_layout(mapbox_style="open-street-map", height=420,
                              margin=dict(l=0, r=0, t=10, b=0))

        countries = analytics.by_country(days=30)
        if countries.empty:
            country_fig = px.bar(title="No locations resolved yet")
        else:
            country_fig = px.bar(
                countries.sort_values("visitors"), x="visitors", y="country",
                orientation="h", labels={"visitors": "Unique visitors",
                                         "country": ""},
                hover_data=["views"])
            country_fig.update_layout(height=360)

        cities = _table(analytics.by_city(days=30), [
            ("Place", _place),
            ("Visitors", lambda r: f"{int(r['visitors']):,}"),
            ("Views", lambda r: f"{int(r['views']):,}"),
        ], empty="No cities resolved yet.")

        networks = _table(analytics.top_networks(days=30), [
            ("Network", lambda r: str(r["ip_prefix"])),
            ("Operator", lambda r: str(r["org"] or "—")),
            ("Location", lambda r: _place(r) or "—"),
            ("Visitors", lambda r: f"{int(r['visitors']):,}"),
            ("Views", lambda r: f"{int(r['views']):,}"),
        ], empty="No networks recorded yet.")

        return (note, geo_fig, ui.apply_theme(country_fig, dark), cities,
                networks)

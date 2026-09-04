"""Weather page (public, read-only): BoM warnings for Victoria.

Rainfall (per monitored location, derived from the flood gauges) is added in a
later slice; this slice covers the warnings feed. Collection is always-on and
managed from the Admin page.
"""
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from dash import Input, Output, State, dash_table, dcc, html
from dash.exceptions import PreventUpdate

from app import ui
from app.collector import manager
from app.modules.weather import data as weather_data

WARNING_PATH = "/weather/warning/"
MELB_CENTER = {"lat": -37.0, "lon": 145.0}
RAIN_COLUMNS = [
    ("name", "Location"), ("catchment", "Catchment"),
    ("rain_since_9am_mm", "Rain since 9am (mm)"),
    ("forecast_max_mm", "Forecast today (mm)"), ("forecast_chance", "Chance (%)"),
]


def warning_path_for(warning_id):
    return WARNING_PATH + str(warning_id)


def warning_id_from_path(pathname):
    if pathname and pathname.startswith(WARNING_PATH):
        return pathname[len(WARNING_PATH):]
    return None

# BoM's warning_group_type (major/minor/...) is intentionally NOT shown: it
# would be confused with the flood gauge's Minor/Moderate/Major classification.
# It's still stored and used internally (sort order, alert upgrade detection).
TABLE_COLUMNS = [
    ("type_label", "Type"), ("title", "Warning"),
    ("issue_time", "Issued"), ("expiry_time", "Expires"),
]


def layout():
    return html.Div([
        html.H2("Weather Warnings"),
        html.Div([
            html.Div([
                html.H4("Collector"),
                html.Div(id="weather-collector-status"),
                html.Div("BoM warnings for Victoria (api.weather.bom.gov.au). "
                         "Collection is managed on the Admin page.",
                         className="muted",
                         style={"marginTop": "8px", "fontSize": "12px"}),
            ], className="panel"),
            html.Div([
                html.H4("Filters"),
                html.Label("Warning type"),
                dcc.Dropdown(id="weather-type-filter", placeholder="All types",
                             clearable=True, className="dropdown"),
            ], className="panel"),
        ], className="panel-row"),
        dcc.Interval(id="weather-interval", interval=60_000, n_intervals=0),
        html.Div(id="weather-summary", className="muted", style={"margin": "10px 0"}),
        html.Div(id="weather-kpis", className="kpi-row"),
        dash_table.DataTable(
            id="weather-table",
            page_size=15,
            filter_action="native",
            sort_action="native",
            style_cell={"whiteSpace": "normal", "height": "auto"},
        ),
        html.H3("Rainfall", style={"marginTop": "18px"}),
        html.Div("Rain since 9am and today's forecast for towns near the flood "
                 "gauges — an upstream leading indicator.", className="muted"),
        html.Div(id="weather-rain-summary", className="muted",
                 style={"margin": "6px 0"}),
        html.Div(dcc.Graph(id="weather-rain-map", style={"height": "520px"},
                           config=ui.MAP_CONFIG),
                 className="graph-card"),
        dash_table.DataTable(
            id="weather-rain-table", page_size=15,
            filter_action="native", sort_action="native"),
        html.H3("AWS Weather Observations", style={"marginTop": "18px"}),
        html.Div("Every Victorian Automatic Weather Station (~104): temperature, "
                 "humidity, wind, pressure and rain since 9am, in one statewide "
                 "fetch per cycle. Recorded and tagged like flood/power for "
                 "after-the-fact analysis; rainfall totals survive the 9am reset.",
                 className="muted"),
        html.Div(AWS_CAVEAT, className="muted",
                 style={"margin": "6px 0", "fontSize": "12px",
                        "fontStyle": "italic"}),
        html.Div(id="weather-aws-kpis", className="kpi-row"),
        html.Div(id="weather-aws-summary", className="muted",
                 style={"margin": "6px 0"}),
        html.Div([
            html.Label("Map metric", style={"marginRight": "10px"}),
            dcc.RadioItems(id="weather-aws-metric",
                           options=[{"label": " " + m["label"], "value": key}
                                    for key, m in AWS_METRICS.items()],
                           value="rain", inline=True,
                           labelStyle={"marginRight": "14px"}),
        ], style={"margin": "8px 0"}),
        html.Div(dcc.Graph(id="weather-aws-map", style={"height": "560px"},
                           config=ui.MAP_CONFIG),
                 className="graph-card"),
        html.Div(id="weather-aws-significant", style={"marginTop": "12px"}),
        dash_table.DataTable(
            id="weather-aws-table", page_size=20,
            filter_action="native", sort_action="native"),
    ])


# Shown wherever AWS observations are presented: these are raw instrument
# readings straight off BoM's state page, not a quality-controlled warning
# product, and nothing here should be read as an official warning.
AWS_CAVEAT = ("Raw automatic weather station observations as published by the "
              "Bureau of Meteorology — not quality-controlled, and not an "
              "official warning product.")

# Selectable map metrics. Same station set every time — only the colour/size
# encoding changes, so switching metric never moves or drops a marker.
AWS_METRICS = {
    "rain": {"label": "Rainfall", "column": "rain_since_9am_mm",
             "title": "Rain since 9am (mm)", "scale": "Blues", "unit": "mm",
             "size_by_value": True},
    "gust": {"label": "Wind Gust", "column": "wind_gust_kmh",
             "title": "Current wind gust (km/h)", "scale": "YlOrRd",
             "unit": "km/h", "size_by_value": True},
    "temp": {"label": "Temperature", "column": "temperature_c",
             "title": "Air temperature (°C)", "scale": "RdYlBu_r",
             "unit": "°C", "size_by_value": False},
    "rh": {"label": "Relative Humidity", "column": "relative_humidity_pct",
           "title": "Relative humidity (%)", "scale": "BrBG", "unit": "%",
           "size_by_value": False},
}

AWS_COLUMNS = [
    ("name", "Station", "text"),
    ("temperature_c", "Temp (°C)", "numeric"),
    ("relative_humidity_pct", "RH (%)", "numeric"),
    ("wind", "Wind", "text"),
    ("wind_gust_kmh", "Gust (km/h)", "numeric"),
    ("rain_since_9am_mm", "Rain 9am (mm)", "numeric"),
    ("pressure_msl_hpa", "Pressure (hPa)", "numeric"),
    ("obs_time", "Observed", "text"),
]


def _obs_kpi(label, value, station, accent=None):
    """KPI card carrying the station the reading came from — an extreme is only
    meaningful with its location, and "82 km/h" alone isn't actionable."""
    style = {"borderTop": f"4px solid {accent}"} if accent else {}
    return html.Div([
        html.Div(label, className="kpi-label"),
        html.Div(value, className="kpi-value"),
        html.Div(station or "—", className="muted",
                 style={"fontSize": "11px", "marginTop": "2px"}),
    ], className="kpi-card", style=style)


def _num(value, fmt="{:.1f}", unit=""):
    """Formatted number, or an em-dash when the station didn't report it."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return "—"
    try:
        text = fmt.format(float(value))
    except (TypeError, ValueError):
        return "—"
    if not unit:
        return text
    # Percent sits tight against the number ("95%"); every other unit is spaced.
    return text + unit if unit == "%" else text + " " + unit


def _wind_text(row):
    """Compact wind cell, e.g. 'NW 35 km/h'. CALM is published as a direction
    with no speed, so it's shown on its own rather than as '0 km/h'."""
    direction = row.get("wind_direction")
    speed = row.get("wind_speed_kmh")
    if (direction is None or pd.isna(direction)) and pd.isna(speed):
        return "—"
    if isinstance(direction, str) and direction.upper() == "CALM":
        return "Calm"
    bits = []
    if direction is not None and not pd.isna(direction):
        bits.append(str(direction))
    if not pd.isna(speed):
        bits.append("%.0f km/h" % speed)
    return " ".join(bits) or "—"


def _aws_hover(r):
    """Full observation for one station — the same text regardless of which
    metric is being mapped, so the map stays readable without marker labels."""
    lines = ["<b>%s</b>" % (r.get("name") or "AWS"),
             "Observed %s" % (r.get("obs_time") or "—"),
             "Temp %s · RH %s" % (_num(r.get("temperature_c"), unit="°C"),
                                  _num(r.get("relative_humidity_pct"), "{:.0f}", "%")),
             "Wind %s · gust %s" % (_wind_text(r),
                                    _num(r.get("wind_gust_kmh"), "{:.0f}", "km/h")),
             "Rain since 9am %s" % _num(r.get("rain_since_9am_mm"), unit="mm")]
    return "<br>".join(lines)


def _empty_map(title, dark):
    fig = px.scatter_map(
        pd.DataFrame({"latitude": [], "longitude": []}),
        lat="latitude", lon="longitude", zoom=5.2, center=MELB_CENTER,
        map_style="open-street-map", title=title)
    return ui.apply_theme(fig, dark)


def _aws_map(df, metric_key, dark):
    """AWS stations coloured by the selected metric. The station set is fixed —
    stations that didn't report the chosen metric stay on the map in grey so
    the network's coverage is always visible."""
    metric = AWS_METRICS.get(metric_key) or AWS_METRICS["rain"]
    if df.empty or df[["latitude", "longitude"]].dropna().empty:
        return _empty_map("AWS observations (locating stations…)", dark)
    plot = df.dropna(subset=["latitude", "longitude"]).copy()
    col = metric["column"]
    values = pd.to_numeric(plot[col], errors="coerce")
    hover = [_aws_hover(r) for _, r in plot.iterrows()]

    fig = go.Figure()
    missing = plot[values.isna()]
    if not missing.empty:
        fig.add_trace(go.Scattermap(
            mode="markers", lat=missing["latitude"], lon=missing["longitude"],
            name="Not reporting", marker=dict(size=6, color="#9aa0a6"),
            hoverinfo="text",
            text=[h for h, m in zip(hover, values.isna()) if m]))
    reporting = plot[values.notna()]
    if not reporting.empty:
        vals = values[values.notna()]
        if metric["size_by_value"]:
            span = float(vals.max()) or 1.0
            sizes = [8 + 20 * (float(v) / span) for v in vals]
        else:
            sizes = 11
        fig.add_trace(go.Scattermap(
            mode="markers", lat=reporting["latitude"], lon=reporting["longitude"],
            name=metric["label"],
            marker=dict(size=sizes, color=vals, colorscale=metric["scale"],
                        showscale=True,
                        colorbar=dict(title=metric["unit"], thickness=12)),
            hoverinfo="text",
            text=[h for h, m in zip(hover, values.notna()) if m]))
    fig.update_layout(
        title=metric["title"], showlegend=False,
        map=dict(style="open-street-map", center=MELB_CENTER, zoom=5.2),
        # Pinned so switching metric or auto-refreshing keeps the user's view.
        uirevision="aws-observations")
    return ui.apply_theme(fig, dark)


def _significant_observations(summary):
    """Ranked 'what stands out right now' lists. Deliberately descriptive: no
    severity is assigned and no threshold is invented — these are observations,
    not warnings."""
    blocks = []
    gusts = summary.get("top_gusts") or []
    if gusts:
        blocks.append(html.Div([
            html.H5("Highest current wind gusts", style={"margin": "0 0 6px"}),
            html.Ol([html.Li("%s — %.0f km/h" % (g["station"], g["value"]))
                     for g in gusts], style={"margin": "0 0 0 18px"}),
        ], className="panel"))
    humidity = summary.get("lowest_humidities") or []
    if humidity:
        blocks.append(html.Div([
            html.H5("Lowest relative humidity", style={"margin": "0 0 6px"}),
            html.Ol([html.Li("%s — %.0f%%" % (h["station"], h["value"]))
                     for h in humidity], style={"margin": "0 0 0 18px"}),
        ], className="panel"))
    if not blocks:
        return html.Div("No AWS observations available yet.", className="muted")
    return html.Div([
        html.H4("Significant Observations", style={"margin": "0 0 8px"}),
        html.Div(blocks, className="panel-row"),
    ])


def _rain_map(df, dark):
    if df.empty or df[["latitude", "longitude"]].dropna().empty:
        fig = px.scatter_map(
            pd.DataFrame({"latitude": [], "longitude": []}),
            lat="latitude", lon="longitude", zoom=5.2, center=MELB_CENTER,
            map_style="open-street-map",
            title="Rainfall (no locations resolved yet)")
        return ui.apply_theme(fig, dark)
    plot = df.dropna(subset=["latitude", "longitude"]).copy()
    plot["_size"] = plot["rain_since_9am_mm"].fillna(0).clip(lower=0) + 2
    fig = px.scatter_map(
        plot, lat="latitude", lon="longitude",
        color="rain_since_9am_mm", size="_size", size_max=24,
        color_continuous_scale="Blues", hover_name="name",
        hover_data={"catchment": True, "rain_since_9am_mm": True,
                    "forecast_max_mm": True, "forecast_chance": True,
                    "latitude": False, "longitude": False, "_size": False},
        zoom=5.2, center=MELB_CENTER, map_style="open-street-map",
        title="Rain since 9am (mm)")
    return ui.apply_theme(fig, dark)


def register_callbacks(app):
    @app.callback(
        Output("weather-collector-status", "children"),
        Output("weather-type-filter", "options"),
        Input("weather-interval", "n_intervals"))
    def collector_status(_):
        s = manager.status()["weather"]
        parts = [html.Strong("Status: "), ui.status_pill(s["running"])]
        if s.get("last_run"):
            parts.append(html.Span(
                f" — last cycle {s['last_run']} ({s.get('runs', 0)} total)"))
        if s.get("last_error"):
            parts.append(html.Div(f"⚠ {s['last_error']}", className="error-text",
                                  style={"marginTop": "4px"}))
        options = [{"label": label, "value": value}
                   for label, value in weather_data.warning_types()]
        return html.Div(parts), options

    @app.callback(
        Output("weather-summary", "children"),
        Output("weather-kpis", "children"),
        Output("weather-table", "data"),
        Output("weather-table", "columns"),
        Output("weather-table", "style_table"),
        Output("weather-table", "style_cell"),
        Output("weather-table", "style_header"),
        Output("weather-table", "style_data"),
        Output("weather-table", "style_data_conditional"),
        Input("weather-interval", "n_intervals"),
        Input("weather-type-filter", "value"),
        Input("theme-store", "data"))
    def refresh(_, warning_type, dark):
        dark = bool(dark)
        styles = ui.table_styles(dark)
        base_cell = {"whiteSpace": "normal", "height": "auto"}
        base_cell.update(styles["style_cell"])

        counts = weather_data.warning_counts()
        kpis = [
            ui.kpi_card("Active Warnings", str(counts["total"]),
                        "#d62728" if counts["total"] else "#2ca02c"),
            ui.kpi_card("Flood Warnings", str(counts["flood"]),
                        "#1f77b4" if counts["flood"] else None),
            ui.kpi_card("Severe Weather", str(counts["severe"]),
                        "#ff7f0e" if counts["severe"] else None),
        ]

        cycles, last_hb = weather_data.heartbeat_summary()
        summary = f"{counts['total']} active BoM warning(s) for Victoria. "
        if cycles:
            summary += f"Monitor ran {cycles} cycle(s), last {last_hb}."

        # Rows still come back most-serious-first (active_warnings sorts by the
        # stored group_type), but the level itself isn't shown.
        df = weather_data.active_warnings(warning_type=warning_type)
        style_out = (styles["style_table"], base_cell,
                     styles.get("style_header", {}), styles.get("style_data", {}))
        if df.empty:
            return (summary, kpis, [], [], *style_out, [])

        table_df = df.copy()
        table_df["issue_time"] = table_df["issue_time"].dt.strftime("%d %b %H:%M")
        table_df["expiry_time"] = table_df["expiry_time"].dt.strftime("%d %b %H:%M")
        # Link the warning title to its detail/history page.
        table_df["title"] = df.apply(
            lambda r: f"[{str(r['title']).replace('[', '(').replace(']', ')')}]"
                      f"({warning_path_for(r['warning_id'])})", axis=1)
        table_df = table_df[[c for c, _ in TABLE_COLUMNS]]
        columns = [{"name": name, "id": col,
                    **({"presentation": "markdown"} if col == "title" else {})}
                   for col, name in TABLE_COLUMNS]
        return (summary, kpis, table_df.to_dict("records"), columns,
                *style_out, [])

    @app.callback(
        Output("weather-rain-summary", "children"),
        Output("weather-rain-map", "figure"),
        Output("weather-rain-table", "data"),
        Output("weather-rain-table", "columns"),
        Output("weather-rain-table", "style_table"),
        Output("weather-rain-table", "style_cell"),
        Output("weather-rain-table", "style_header"),
        Output("weather-rain-table", "style_data"),
        Input("weather-interval", "n_intervals"),
        Input("theme-store", "data"))
    def refresh_rain(_, dark):
        dark = bool(dark)
        styles = ui.table_styles(dark)
        style_out = (styles["style_table"], styles["style_cell"],
                     styles.get("style_header", {}), styles.get("style_data", {}))
        df = weather_data.latest_rainfall()
        n, wettest, wmm = weather_data.rainfall_summary()
        summary = f"{n} location(s) monitored."
        if wettest and pd.notna(wmm):
            summary += f"  Wettest since 9am: {wettest} ({wmm:.1f} mm)."
        fig = _rain_map(df, dark)
        if df.empty:
            return (summary, fig, [], [], *style_out)
        tdf = df[[c for c, _ in RAIN_COLUMNS]].copy()
        columns = [{"name": name, "id": col} for col, name in RAIN_COLUMNS]
        return (summary, fig, tdf.to_dict("records"), columns, *style_out)

    @app.callback(
        Output("weather-aws-kpis", "children"),
        Output("weather-aws-summary", "children"),
        Output("weather-aws-map", "figure"),
        Output("weather-aws-significant", "children"),
        Output("weather-aws-table", "data"),
        Output("weather-aws-table", "columns"),
        Output("weather-aws-table", "style_table"),
        Output("weather-aws-table", "style_cell"),
        Output("weather-aws-table", "style_header"),
        Output("weather-aws-table", "style_data"),
        Input("weather-interval", "n_intervals"),
        Input("weather-aws-metric", "value"),
        Input("theme-store", "data"))
    def refresh_aws(_, metric_key, dark):
        dark = bool(dark)
        styles = ui.table_styles(dark)
        style_out = (styles["style_table"], styles["style_cell"],
                     styles.get("style_header", {}), styles.get("style_data", {}))
        df = weather_data.latest_aws_observations()
        s = weather_data.aws_weather_summary()

        def card(label, entry, fmt, unit, accent=None):
            if not entry:
                return _obs_kpi(label, "—", None)
            return _obs_kpi(label, _num(entry["value"], fmt, unit),
                            entry["station"], accent)

        kpis = [
            card("Strongest Gust", s["strongest_gust"], "{:.0f}", "km/h", "#ff7f0e"),
            card("Lowest RH", s["lowest_humidity"], "{:.0f}", "%", "#8c6d3f"),
            card("Warmest", s["warmest"], "{:.1f}", "°C", "#d62728"),
            card("Wettest since 9am", s["wettest"], "{:.1f}", "mm", "#1f77b4"),
        ]

        summary = "%d AWS station(s) recorded, %d reporting recently." % (
            s["stations"], s["reporting"])
        if s["stale"]:
            summary += (" %d station(s) have not reported in the last %d hours "
                        "and are excluded from the current-conditions figures."
                        % (s["stale"], weather_data.AWS_STALE_HOURS))
        if s["max_daily_gust"]:
            g = s["max_daily_gust"]
            summary += "  Highest gust today: %s at %s%s." % (
                _num(g["value"], "{:.0f}", "km/h"), g["station"],
                (" (%s)" % g["max_gust_time"]) if g.get("max_gust_time") else "")

        fig = _aws_map(df, metric_key, dark)
        significant = _significant_observations(s)
        if df.empty:
            return (kpis, summary, fig, significant, [], [], *style_out)

        tdf = df.copy()
        tdf["wind"] = [_wind_text(r) for _, r in tdf.iterrows()]
        tdf["obs_time"] = pd.to_datetime(
            tdf["obs_time"], format="ISO8601", errors="coerce"
        ).dt.strftime("%d %b %H:%M")
        tdf = tdf[[c for c, _, _ in AWS_COLUMNS]]
        # Numeric columns stay numeric (units live in the header) so the
        # table's native sort orders 9.0 below 10.1 instead of lexically.
        columns = [{"name": name, "id": col, "type": kind}
                   for col, name, kind in AWS_COLUMNS]
        return (kpis, summary, fig, significant, tdf.to_dict("records"),
                columns, *style_out)

    @app.callback(
        Output("warning-message-frame", "srcDoc"),
        Input("warning-version-select", "value"),
        State("warning-detail-id", "data"),
        prevent_initial_call=True)
    def show_version(issue_time, warning_id):
        if not warning_id or not issue_time:
            raise PreventUpdate
        msg = weather_data.warning_version_message(warning_id, issue_time)
        return msg or "<p>No text recorded for this version.</p>"


def warning_detail_layout(warning_id):
    """Detail page for one warning: full BoM text (images render inline) plus a
    version selector to replay how the warning developed."""
    d = weather_data.warning_detail(warning_id)
    if not d:
        return html.Div([
            html.H2("Warning not found"),
            dcc.Link("← Back to Weather Warnings", href="/weather",
                     className="nav-link"),
        ])
    hist = weather_data.warning_history(warning_id)
    versions = []
    if not hist.empty:
        for ts, phase in zip(hist["issue_time"], hist["phase"]):
            if pd.isna(ts):
                continue
            versions.append({
                "label": ts.strftime("%d %b %Y %H:%M")
                         + (f"  ·  {phase}" if phase else ""),
                "value": ts.strftime("%Y-%m-%d %H:%M:%S")})
    latest_msg = d.get("message") or "<p>No detailed text recorded yet.</p>"
    meta = (f"Issued {d.get('issue_time') or '—'}  ·  "
            f"Expires {d.get('expiry_time') or '—'}  ·  "
            f"Phase: {d.get('phase') or '—'}")
    return html.Div([
        dcc.Link("← Back to Weather Warnings", href="/weather", className="nav-link"),
        html.H2(weather_data._pretty_type(d.get("type"))),
        html.H4(d.get("title") or ""),
        html.Div(meta, className="muted"),
        dcc.Store(id="warning-detail-id", data=str(warning_id)),
        html.Label("Version (issued)", style={"marginTop": "10px",
                                              "display": "block"}),
        dcc.Dropdown(id="warning-version-select", options=versions,
                     value=(versions[0]["value"] if versions else None),
                     clearable=False, className="dropdown",
                     style={"maxWidth": "420px"}),
        html.Div(f"{len(versions)} version(s) recorded — select one to see how "
                 "the warning read at that time.", className="muted",
                 style={"fontSize": "12px", "margin": "4px 0"}),
        # Iframe (sandboxed, scripts blocked) faithfully renders BoM's HTML;
        # any embedded base64 images display inline.
        html.Iframe(id="warning-message-frame", srcDoc=latest_msg, sandbox="",
                    style={"width": "100%", "height": "640px",
                           "border": "1px solid #333d4d", "borderRadius": "6px",
                           "background": "#fff", "marginTop": "8px"}),
    ])

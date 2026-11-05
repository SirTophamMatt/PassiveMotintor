# Passive Monitor — Copyright (c) 2026 SirTophamMatt. All rights reserved.
"""Power Outage page (public, read-only): KPIs, trends, map, durations, and
the list of OUTAGES — each with its ID and the suburbs it affects.

An outage is one row of EM-COP's outage table (power/outages.py gives it an ID
that holds from cycle to cycle). The map fills each outage's suburbs (locality
boundaries, app/localities.py) in its duration colour and puts ONE bubble per
outage; picking an outage — in the table or on the map — outlines its suburbs,
frames the map on them and lists every locality on it, with links to any other
outage sharing one of those suburbs.

Collection is controlled from the Admin page; this page shows a read-only
collector status line.
"""
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from dash import ALL, Input, Output, State, ctx, dash_table, dcc, html
from dash.exceptions import PreventUpdate

from app import localities, ui
from app.collector import manager
from app.modules.power import data as power_data
from app.modules.power import outages

TREND_WINDOWS = [
    ("Past Hour", pd.Timedelta(hours=1)),
    ("Past 6 Hours", pd.Timedelta(hours=6)),
    ("Past 24 Hours", pd.Timedelta(hours=24)),
    ("Past Week", pd.Timedelta(days=7)),
]

SERIES_COLOURS = {
    "customers_off": "#1f77b4",
    "planned": "#2ca02c",
    "unplanned": "#d62728",
}
SERIES_LABELS = {
    "customers_off": "Customers Off",
    "planned": "Planned",
    "unplanned": "Unplanned",
}

VIC_VIEW = ({"lat": -37.8136, "lon": 144.9631}, 6)
SELECTED_LINE = "#111111"
FILL_OPACITY = 0.28

TABLE_COLUMNS = [("outage_ref", "Outage ID"), ("localities", "Localities"),
                 ("customers", "Customers off"), ("peak", "Peak"),
                 ("type", "Type"), ("since", "First seen"),
                 ("duration", "Duration"), ("mapped", "Outlined")]


def layout():
    return html.Div([
        html.H2("Power Outages"),
        html.Div([
            html.Div([
                html.H4("Collector"),
                html.Div(id="power-collector-status"),
                html.Div("Collection is managed on the Admin page.",
                         className="muted",
                         style={"marginTop": "8px", "fontSize": "12px"}),
            ], className="panel"),
            html.Div([
                html.H4("Filters"),
                dcc.Checklist(id="power-filters", value=[], options=[
                    {"label": " Hide planned outages from map", "value": "hide_planned"},
                    {"label": " Only outages with 50+ customers", "value": "large_only"},
                ]),
            ], className="panel"),
            html.Div([
                html.H4("Suburb boundaries"),
                html.Div(id="power-boundary-status", className="muted",
                         style={"fontSize": "13px"}),
            ], className="panel"),
        ], className="panel-row"),
        dcc.Interval(id="power-interval", interval=60_000, n_intervals=0),
        dcc.Store(id="power-selected"),
        html.Div(id="power-kpis", className="kpi-row"),
        html.Div(id="power-trend-graphs", className="graph-grid"),
        html.Div([
            html.Div(dcc.Graph(id="power-map", style={"height": "650px"},
                               config=ui.MAP_CONFIG),
                     style={"flex": "3", "minWidth": "320px"}),
            html.Div(dcc.Graph(id="power-durations", style={"height": "650px"}),
                     style={"flex": "2", "minWidth": "300px"}),
        ], style={"display": "flex", "gap": "16px", "flexWrap": "wrap"}),
        html.Div([
            html.H3("Outages", style={"display": "inline-block", "marginRight": "12px"}),
            # Always in the layout (hidden until something is selected): a
            # callback whose Input is missing at first render never fires.
            html.Button("Clear selection", id="power-clear", className="btn",
                        style={"display": "none"}),
        ]),
        html.Div("One row per outage as EM-COP lists it. Click a row (or an "
                 "outage on the map) to outline its suburbs and see every "
                 "locality on it.", className="muted",
                 style={"fontSize": "12px", "marginBottom": "6px"}),
        dash_table.DataTable(
            id="power-outage-table", page_size=15, sort_action="native",
            filter_action="native",
            columns=[{"name": n, "id": c} for c, n in TABLE_COLUMNS],
            style_cell_conditional=[{"if": {"column_id": "localities"},
                                     "whiteSpace": "normal", "minWidth": "280px"}]),
        html.Div(id="power-outage-detail", style={"marginTop": "12px"}),
    ])


# --------------------------------------------------------------------------- #
# Figures
# --------------------------------------------------------------------------- #
def _trend_figure(df, title, dark):
    fig = px.line(
        df, x="timestamp", y=list(SERIES_COLOURS),
        title=title, labels={"timestamp": "Time", "value": "Customers", "variable": ""},
        color_discrete_map=SERIES_COLOURS)
    fig.for_each_trace(lambda t: t.update(name=SERIES_LABELS.get(t.name, t.name)))
    if not df.empty and df["customers_off"].notna().any():
        peak_idx = df["customers_off"].idxmax()
        peak_val = df.at[peak_idx, "customers_off"]
        peak_time = df.at[peak_idx, "timestamp"]
        label = f"Peak {peak_val:,.0f}"
        if pd.notna(peak_time):
            # 24-hour "hrs" style; date included so the Past Week peak is clear.
            label += f" at {peak_time:%H%Mhrs, %d %b}"
        fig.add_scatter(
            x=[peak_time], y=[peak_val],
            mode="markers+text", marker=dict(size=11, symbol="diamond", color="orange"),
            text=[label], textposition="top center", name="Peak")
    fig.update_layout(height=320, legend=dict(orientation="h", y=1.12))
    return ui.apply_theme(fig, dark)


def _duration_mins(first_seen):
    first = pd.to_datetime(first_seen, errors="coerce")
    if pd.isna(first):
        return None
    return max((pd.Timestamp.now() - first).total_seconds() // 60, 0)


def _fmt_duration(minutes):
    if minutes is None or minutes != minutes:
        return "—"
    minutes = int(minutes)
    h, m = divmod(minutes, 60)
    if h >= 48:
        return f"{h // 24} d {h % 24} h"
    return f"{h} h {m:02d} m" if h else f"{m} m"


def _fmt_since(value):
    t = pd.to_datetime(value, errors="coerce")
    return "—" if pd.isna(t) else f"{t:%H:%M} {t.day} {t:%b}"


def _hover(o, minutes):
    names = [m["display"] for m in o["members"]]
    shown, more = names[:10], len(names) - 10
    lines = [f"<b>Outage {o['ref']}</b>" if o["ref"] else "<b>Outage</b>",
             f"{o['customers_off']:,} customers off · {o['type'] or 'Unplanned'}",
             f"Since {_fmt_since(o['first_seen'])} ({_fmt_duration(minutes)})",
             f"{len(names)} {'locality' if len(names) == 1 else 'localities'}: "
             + ", ".join(shown) + (f" +{more} more" if more > 0 else "")]
    return "<br>".join(lines)


def _fill(geoms, colour, opacity, width=1.2, line_colour=None):
    if not geoms:
        return []
    fc = {"type": "FeatureCollection",
          "features": [{"type": "Feature", "geometry": g, "properties": {}}
                       for g in geoms]}
    out = []
    if opacity:
        out.append({"sourcetype": "geojson", "type": "fill", "below": "traces",
                    "color": colour, "opacity": opacity, "source": fc})
    out.append({"sourcetype": "geojson", "type": "line", "below": "traces",
                "color": line_colour or colour, "line": {"width": width},
                "source": fc})
    return out


def _map_figure(outage_rows, dark, selected=None):
    """Suburbs filled in each outage's duration colour, one bubble per outage
    sized by customers off, the SELECTED outage outlined and framed.
    `outage_rows` are per-locality rows (power_data.map_outages); rows without
    boundaries still draw as bubbles."""
    items = outages.summaries(outage_rows) if outage_rows is not None else []
    located = [o for o in items if o["latitude"] is not None]
    center, zoom = VIC_VIEW
    if not located:
        fig = go.Figure(go.Scattermap(lat=[], lon=[], mode="markers",
                                      showlegend=False, hoverinfo="skip"))
        fig.update_layout(map=dict(style="open-street-map", center=center, zoom=zoom),
                          title="Active Outages (no geocoded locations yet)")
        return ui.apply_theme(fig, dark)

    biggest = max(o["customers_off"] for o in located) or 1
    by_bucket, fills, points = {}, {}, {}
    chosen = None
    for o in located:
        minutes = _duration_mins(o["first_seen"])
        bucket = power_data.duration_bucket(minutes if minutes is not None else 0)
        by_bucket.setdefault(bucket, []).append((o, _hover(o, minutes)))
        for m in o["members"]:
            if m["geometry"]:
                fills.setdefault(bucket, {}).setdefault(m["locality_id"], m["geometry"])
            if m["latitude"] is not None and len(o["members"]) > 1:
                slot = points.setdefault(m["display"], [m["latitude"], m["longitude"], []])
                slot[2].append(f"Outage {o['ref'] or '—'} · {o['customers_off']:,} "
                               f"customers off · {len(o['members'])} localities")
        if selected and o["ref"] == selected:
            chosen = o

    fig = go.Figure()
    layers = []
    for _, label, colour in power_data.DURATION_BUCKETS:
        layers += _fill(list(fills.get(label, {}).values()), colour, FILL_OPACITY)
    if chosen is not None:
        shapes = [m["geometry"] for m in chosen["members"] if m["geometry"]]
        layers += _fill(shapes, SELECTED_LINE, 0, width=3)
        view = localities.bounds_view(shapes) if shapes else (None, None)
        if view[0] is None:
            pts = [(m["latitude"], m["longitude"]) for m in chosen["members"]
                   if m["latitude"] is not None]
            view = ({"lat": sum(p[0] for p in pts) / len(pts),
                     "lon": sum(p[1] for p in pts) / len(pts)}, 11) if pts else VIC_VIEW
        center, zoom = view
    if points:
        fig.add_trace(go.Scattermap(
            mode="markers", lat=[v[0] for v in points.values()],
            lon=[v[1] for v in points.values()], showlegend=False,
            marker=dict(size=14, color="#7048e8", opacity=0), hoverinfo="text",
            text=["<b>%s</b><br>%s" % (k, "<br>".join(dict.fromkeys(v[2])))
                  for k, v in points.items()], name="Suburb"))
    for _, label, colour in power_data.DURATION_BUCKETS:
        sub = by_bucket.get(label)
        if not sub:
            continue
        fig.add_trace(go.Scattermap(
            mode="markers", name=label,
            lat=[o["latitude"] for o, _ in sub], lon=[o["longitude"] for o, _ in sub],
            marker=dict(size=[8 + 22 * (max(o["customers_off"], 1) / biggest) ** 0.5
                              for o, _ in sub],
                        color=colour, opacity=0.85),
            hoverinfo="text", text=[t for _, t in sub],
            customdata=[o["ref"] or "" for o, _ in sub]))
    map_cfg = dict(style="open-street-map", center=center, zoom=zoom)
    if layers:
        map_cfg["layers"] = layers
    fig.update_layout(
        map=map_cfg, title="Active Outages — suburbs affected",
        legend=dict(orientation="h", y=1.02, title_text="Duration"),
        # assets/graph_calm.js: move the map when the selection changes, keep
        # the viewer's pan/zoom while it does not.
        meta={"wdFocus": selected or "state"})
    return ui.apply_theme(fig, dark)


def _durations_figure(events, dark):
    df = events if events is not None else pd.DataFrame()
    if not df.empty:
        df = df[~df["type"].fillna("").str.strip().str.lower().eq("planned")].copy()
    if df.empty:
        fig = px.bar(title="No active unplanned outages")
        return ui.apply_theme(fig, dark)
    df["Duration"] = df["duration_mins"].apply(power_data.duration_bucket)
    df = df.sort_values("duration_mins", ascending=False).head(40)
    # The ID's tail keeps two outages with the same suburbs apart on the axis.
    df["Outage"] = [f"{lab} · {str(ref)[-4:]}"
                    for lab, ref in zip(df["label"], df["outage_ref"])]
    fig = px.bar(
        df, y="Outage", x="duration_mins", orientation="h",
        color="Duration", color_discrete_map=power_data.DURATION_COLOUR_MAP,
        labels={"duration_mins": "Duration (minutes)", "Outage": ""},
        hover_data={"outage_ref": True, "customers_off": True,
                    "locality_count": True, "Outage": False},
        title="Unplanned Outage Durations")
    fig.update_layout(yaxis=dict(autorange="reversed"), legend_title_text="Duration")
    return ui.apply_theme(fig, dark)


# --------------------------------------------------------------------------- #
# Table + detail
# --------------------------------------------------------------------------- #
def table_rows(events, summaries):
    """DataTable records: one per outage."""
    by_ref = {o["ref"]: o for o in summaries if o["ref"]}
    out = []
    for r in (events.to_dict("records") if events is not None and not events.empty else []):
        ref = r.get("outage_ref")
        o = by_ref.get(ref)
        names = [localities.display_name(n) for n in r.get("locations") or []]
        mapped = f"{o['mapped']}/{len(o['members'])}" if o else "—"
        out.append({
            "id": ref, "outage_ref": ref,
            "localities": f"{len(names)}: " + ", ".join(names) if names else "—",
            "customers": int(r.get("customers_off") or 0),
            "peak": int(r.get("peak_customers") or 0),
            "type": r.get("type") or "",
            "since": _fmt_since(r.get("first_seen")),
            "duration": _fmt_duration(r.get("duration_mins")),
            "mapped": mapped,
        })
    return out


def boundary_status(summaries):
    s = localities.status()
    if not s["count"]:
        text = ("Not loaded — outages are shown as points. An admin can "
                "download them on Admin → Power collection.")
        if s.get("last_error"):
            text += f" Last attempt: {s['last_error']}"
        return text
    members = [m for o in summaries for m in o["members"]]
    outlined = sum(1 for m in members if m["geometry"])
    text = (f"{s['count']:,} localities loaded ({s.get('source') or 'unknown source'},"
            f" {str(s.get('loaded_at') or '')[:16]}).")
    if members:
        text += f" Outlined now: {outlined} of {len(members)} listed localities."
    unmatched = sorted({m["location"] for m in members if m["status"] == "unmatched"})
    ambiguous = sorted({m["location"] for m in members if m["status"] == "ambiguous"})
    if unmatched:
        text += " No boundary for: " + ", ".join(unmatched[:12]) + (
            f" +{len(unmatched) - 12} more" if len(unmatched) > 12 else "") + "."
    if ambiguous:
        text += " Name shared by several localities, not placed: " + ", ".join(
            ambiguous[:8]) + "."
    return text


STATUS_LABELS = {localities.MATCHED: "outlined", localities.AMBIGUOUS:
                 "shared name — not placed", localities.UNMATCHED: "no boundary",
                 localities.NO_DATA: "boundaries not loaded"}


def _locality_list(rows, ref):
    """Each locality on the outage with its match status and links to any
    other active outage it is also on."""
    items = []
    for r in rows:
        name = r.get("location")
        status = r.get("locality_status") or localities.NO_DATA
        others = [x for x in power_data.outages_for_location(name) if x != ref]
        bits = [html.Strong(r.get("locality_name") or localities.display_name(name)),
                html.Span(f" — {STATUS_LABELS.get(status, status)}", className="muted")]
        if (r.get("locality_name") or "") and localities.normalise(
                r.get("locality_name")) != localities.normalise(name):
            bits.append(html.Span(f" (listed as {name})", className="muted"))
        if others:
            bits.append(html.Span(" · also on "))
            for i, other in enumerate(others):
                if i:
                    bits.append(html.Span(", "))
                bits.append(html.Button(other, className="btn btn-link",
                                        id={"type": "power-outage-link", "ref": other},
                                        style={"padding": "0 2px"}))
        items.append(html.Li(bits))
    return html.Ul(items, style={"margin": "4px 0 8px 18px", "padding": 0})


def outage_detail(ref):
    if not ref:
        return html.Div("Select an outage in the table or on the map to see "
                        "every locality on it.", className="muted")
    o = power_data.outage(ref)
    if o is None:
        return html.Div(f"Outage {ref} is not in the database.", className="muted")
    rows = o["rows"].to_dict("records") if not o["rows"].empty else []
    now_rows = [r for r in rows if not int(r.get("restored") or 0)]
    past_rows = [r for r in rows if int(r.get("restored") or 0)]
    restored = int(o.get("restored") or 0)
    status = (f"Restored {_fmt_since(o.get('restored_at'))}" if restored
              else "Active")
    head = [
        html.Div([html.H3(f"Outage {ref}", style={"margin": "0 12px 0 0",
                                                    "display": "inline"}),
                  html.Span(status, className="muted")]),
        html.Div(f"{int(o.get('customers_off') or 0):,} customers off "
                 f"(peak {int(o.get('peak_customers') or 0):,}) · "
                 f"{o.get('type') or 'Unplanned'} · first seen "
                 f"{_fmt_since(o.get('first_seen'))} · last seen "
                 f"{_fmt_since(o.get('last_seen'))} · "
                 f"{_fmt_duration(o.get('duration_mins'))}",
                 style={"margin": "6px 0"}),
        html.Div(f"Localities listed now ({len(now_rows)})" if not restored
                 else f"Localities when restored ({len(now_rows) or len(past_rows)})",
                 className="kpi-label"),
        _locality_list(now_rows or past_rows if restored else now_rows, ref),
    ]
    if past_rows and not restored:
        head += [html.Div(f"No longer listed ({len(past_rows)})",
                          className="kpi-label"),
                 _locality_list(past_rows, ref)]
    cells = o.get("cells") or {}
    if cells.get("cells"):
        header = cells.get("header") or []
        pairs = [(header[i] if i < len(header) and header[i] else f"Column {i + 1}", c)
                 for i, c in enumerate(cells["cells"])]
        head.append(html.Details([
            html.Summary("As published by EM-COP"),
            html.Table([html.Tr([html.Th(k, style={"textAlign": "left",
                                                   "paddingRight": "12px"}),
                                 html.Td(v)]) for k, v in pairs],
                       style={"fontSize": "13px"}),
        ], style={"marginTop": "6px"}))
    return html.Div(head, className="panel")


# --------------------------------------------------------------------------- #
# Callbacks
# --------------------------------------------------------------------------- #
def register_callbacks(app):
    @app.callback(
        Output("power-collector-status", "children"),
        Input("power-interval", "n_intervals"))
    def collector_status(_):
        s = manager.status()["power"]
        parts = [html.Strong("Status: "), ui.status_pill(s["running"])]
        if s.get("last_run"):
            parts.append(html.Span(
                f" — last cycle {s['last_run']} ({s.get('runs', 0)} total)"))
        if s.get("last_error"):
            parts.append(html.Div(f"⚠ {s['last_error']}", className="error-text",
                                  style={"marginTop": "4px"}))
        return html.Div(parts)

    @app.callback(
        Output("power-kpis", "children"),
        Output("power-trend-graphs", "children"),
        Input("power-interval", "n_intervals"),
        Input("power-filters", "value"),
        Input("theme-store", "data"))
    def refresh(_, filters, dark):
        df = power_data.load_timeseries()
        dark = bool(dark)
        events = power_data.outage_events()
        n_outages = len(events)
        n_localities = (len({localities.normalise(n) for v in events["locations"]
                             for n in v}) if n_outages else 0)
        if df.empty:
            kpis = [ui.kpi_card("Power Data", "No data yet — start collection")]
            trends = []
        else:
            latest = df.iloc[-1]

            def fmt(value):
                return f"{value:,.0f}" if pd.notna(value) else "—"

            kpis = [
                ui.kpi_card("Customers Off", fmt(latest["customers_off"]), "#1f77b4"),
                ui.kpi_card("Power Dependant Off", fmt(latest["power_dependant_off"])),
                ui.kpi_card("Planned", fmt(latest["planned"]), "#2ca02c"),
                ui.kpi_card("Unplanned", fmt(latest["unplanned"]), "#d62728"),
                ui.kpi_card("Outages", f"{n_outages:,}", "#7048e8"),
                ui.kpi_card("Localities Affected", f"{n_localities:,}", "#7048e8"),
            ]
            now = df["timestamp"].max()
            trends = [
                html.Div(dcc.Graph(figure=_trend_figure(
                    df[df["timestamp"] >= now - window], title, dark)),
                    className="graph-card")
                for title, window in TREND_WINDOWS
            ]
        return kpis, trends

    @app.callback(
        Output("power-map", "figure"),
        Output("power-durations", "figure"),
        Output("power-outage-table", "data"),
        Output("power-outage-table", "style_header"),
        Output("power-outage-table", "style_data"),
        Output("power-outage-table", "style_cell"),
        Output("power-outage-table", "style_data_conditional"),
        Output("power-outage-table", "style_filter"),
        Output("power-boundary-status", "children"),
        Input("power-interval", "n_intervals"),
        Input("power-filters", "value"),
        Input("theme-store", "data"),
        Input("power-selected", "data"))
    def refresh_outages(_, filters, dark, selected):
        dark = bool(dark)
        filters = filters or []
        kwargs = dict(include_planned="hide_planned" not in filters,
                      min_customers=50 if "large_only" in filters else 0)
        rows = power_data.map_outages(**kwargs)
        events = power_data.outage_events(**kwargs)
        items = outages.summaries(rows)
        st = ui.table_styles(dark)
        highlight = ([{"if": {"filter_query": '{outage_ref} = "%s"' % selected},
                       "fontWeight": "bold",
                       "borderLeft": "4px solid var(--accent, #7048e8)"}]
                     if selected else [])
        return (_map_figure(rows, dark, selected), _durations_figure(events, dark),
                table_rows(events, items), st.get("style_header", {}),
                st.get("style_data", {}), st["style_cell"], highlight,
                st.get("style_filter", {}), boundary_status(items))

    @app.callback(
        Output("power-selected", "data"),
        Input("power-outage-table", "active_cell"),
        Input("power-map", "clickData"),
        Input({"type": "power-outage-link", "ref": ALL}, "n_clicks"),
        Input("power-clear", "n_clicks"),
        State("power-selected", "data"),
        prevent_initial_call=True)
    def select(cell, click, _links, _clear, current):
        trigger = ctx.triggered_id
        if trigger == "power-clear":
            return None
        if trigger == "power-outage-table":
            return (cell or {}).get("row_id") or current
        if trigger == "power-map":
            for point in (click or {}).get("points") or []:
                ref = point.get("customdata")
                if isinstance(ref, list):
                    ref = ref[0] if ref else None
                if ref:
                    return ref
            raise PreventUpdate
        if isinstance(trigger, dict) and trigger.get("type") == "power-outage-link":
            if not any(t.get("value") for t in ctx.triggered):
                raise PreventUpdate   # a button re-rendered, not clicked
            return trigger.get("ref")
        raise PreventUpdate

    @app.callback(
        Output("power-outage-detail", "children"),
        Output("power-clear", "style"),
        Input("power-selected", "data"),
        Input("power-interval", "n_intervals"))
    def detail(ref, _):
        return outage_detail(ref), ({} if ref else {"display": "none"})

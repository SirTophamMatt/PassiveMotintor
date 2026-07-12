"""Overview PDF report for briefings.

Renders the current Overview — headline KPIs, power trend, outage map and
flooding-station graphs — into a single branded PDF that can be dropped into a
briefing pack or copied into other products.

Plotly figures are rendered to PNG via kaleido (bundled with the app) and laid
out with reportlab (pure Python — no system libraries needed on the server).
Both imports are done lazily so the rest of the app still runs if they are not
installed in a given environment.
"""
import io
import logging
from datetime import datetime

import pandas as pd

from app.config import load_config
from app.modules.fire import data as fire_data
from app.modules.flood import data as flood_data
from app.modules.power import data as power_data
from app.modules.weather import data as weather_data

log = logging.getLogger(__name__)

# Print-friendly: light theme, fixed size.
_FIG_W, _FIG_H = 1000, 420


class ReportingUnavailable(RuntimeError):
    """Raised when kaleido/reportlab are not installed."""


def _fig_png(fig, width=_FIG_W, height=_FIG_H):
    try:
        return fig.to_image(format="png", width=width, height=height, scale=2)
    except Exception as e:  # kaleido missing or render failure
        raise ReportingUnavailable(
            "Could not render a chart to image. Ensure 'kaleido' is installed "
            f"(pip install kaleido). Underlying error: {e}") from e


def build_overview_pdf():
    """Return (filename, bytes) for a PDF snapshot of the Overview."""
    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import getSampleStyleSheet
        from reportlab.lib.units import mm
        from reportlab.platypus import (Image, Paragraph, SimpleDocTemplate,
                                        Spacer, Table, TableStyle)
    except ImportError as e:
        raise ReportingUnavailable(
            "The 'reportlab' package is required for PDF reports "
            "(pip install reportlab).") from e

    # Import figure builders here to avoid a circular import at module load.
    from app.pages import flood as flood_page
    from app.pages import power as power_page

    cfg = load_config()
    styles = getSampleStyleSheet()
    story = []
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    story.append(Paragraph("Passive Monitor — Overview Briefing", styles["Title"]))
    story.append(Paragraph(f"Generated {now}", styles["Normal"]))
    story.append(Spacer(1, 8 * mm))

    # --- KPIs -----------------------------------------------------------------
    totals = power_data.latest_totals() or {}
    flooding = flood_data.flooding_station_count()

    def fmt(v):
        return f"{int(v):,}" if v is not None else "—"

    off = totals.get("customers_off")
    high = cfg["alerts"]["high_customers_off"]
    low = cfg["alerts"]["low_customers_off"]
    if off is not None and off > high:
        alert = "HIGH ALERT"
    elif off is not None and off > low:
        alert = "Low alert"
    else:
        alert = "Normal"

    fire_counts = fire_data.latest_counts()
    wcounts = weather_data.warning_counts()
    kpi_rows = [
        ["Customers Off", fmt(off), "Alert Level", alert],
        ["Power Dependant Off", fmt(totals.get("power_dependant_off")),
         "Stations Flooding", str(flooding)],
        ["Planned", fmt(totals.get("planned")),
         "Unplanned", fmt(totals.get("unplanned"))],
        ["Active Fires", str(fire_counts["active_fires"]),
         "Emergency / Watch & Act",
         str(fire_counts["emergency"] + fire_counts["watch_act"])],
        ["BoM Warnings (VIC)", str(wcounts["total"]),
         "Flood Warnings", str(wcounts["flood"])],
    ]
    kpi_table = Table(kpi_rows, colWidths=[45 * mm, 40 * mm, 45 * mm, 40 * mm])
    kpi_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#eef2f7")),
        ("BACKGROUND", (2, 0), (2, -1), colors.HexColor("#eef2f7")),
        ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
        ("FONTNAME", (2, 0), (2, -1), "Helvetica-Bold"),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#c8d0da")),
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    story.append(kpi_table)
    story.append(Spacer(1, 6 * mm))

    def add_fig(fig, caption):
        png = _fig_png(fig)
        story.append(Paragraph(caption, styles["Heading3"]))
        story.append(Image(io.BytesIO(png), width=180 * mm, height=75 * mm))
        story.append(Spacer(1, 5 * mm))

    # --- Power trend + map ----------------------------------------------------
    df = power_data.load_timeseries()
    if not df.empty:
        now_ts = df["timestamp"].max()
        window = next((w for t, w in power_page.TREND_WINDOWS if t == "Past 24 Hours"),
                      None)
        trend_df = df[df["timestamp"] >= now_ts - window] if window is not None else df
        add_fig(power_page._trend_figure(trend_df, "Power — Past 24 Hours", dark=False),
                "Power Outage Trend")

    outages = power_data.active_outages(include_planned=True)
    add_fig(power_page._map_figure(outages, dark=False), "Active Outages Map")

    # --- Flooding stations ----------------------------------------------------
    flooding_stations = flood_data.current_flooding_stations(max_stations=6)
    if flooding_stations:
        story.append(Paragraph("Flooding Stations", styles["Heading2"]))
        for station, hist, label, colour, levels in flooding_stations:
            add_fig(flood_page._station_figure(hist, station, label, levels, dark=False),
                    f"{station} — {label}")
    else:
        story.append(Paragraph("No stations currently at or above flood level.",
                               styles["Italic"]))

    # --- BoM warnings ---------------------------------------------------------
    def _simple_table(header, rows, widths):
        cell = styles["Normal"]
        data = [[Paragraph(f"<b>{h}</b>", cell) for h in header]]
        data += [[Paragraph(str(c), cell) for c in r] for r in rows]
        t = Table(data, colWidths=widths, repeatRows=1)
        t.setStyle(TableStyle([
            ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#c8d0da")),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eef2f7")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("FONTSIZE", (0, 0), (-1, -1), 9),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]))
        return t

    story.append(Spacer(1, 4 * mm))
    story.append(Paragraph("Active BoM Warnings (VIC)", styles["Heading2"]))
    wdf = weather_data.active_warnings()
    if wdf.empty:
        story.append(Paragraph("No active BoM warnings.", styles["Italic"]))
    else:
        rows = [[r["type_label"], r["title"],
                 r["issue_time"].strftime("%d %b %H:%M")
                 if pd.notna(r["issue_time"]) else "—"]
                for _, r in wdf.head(15).iterrows()]
        story.append(_simple_table(["Type", "Warning", "Issued"],
                                   rows, [38 * mm, 112 * mm, 30 * mm]))

    # --- Fire incidents & warnings -------------------------------------------
    story.append(Spacer(1, 4 * mm))
    story.append(Paragraph("Active Fire Incidents & Warnings", styles["Heading2"]))
    fdf = fire_data.active_incidents()
    if fdf.empty:
        story.append(Paragraph("No active incidents.", styles["Italic"]))
    else:
        fdf = fdf.assign(_fire=(fdf["category1"].fillna("").str.lower() != "fire"))
        fdf = fdf.sort_values(["_fire", "category1"]).head(20)
        rows = [[r["category1"] or "—", r["location"] or "—",
                 r["status"] or "—"] for _, r in fdf.iterrows()]
        story.append(_simple_table(["Category", "Location", "Status"],
                                   rows, [42 * mm, 100 * mm, 38 * mm]))

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4,
                            topMargin=15 * mm, bottomMargin=15 * mm,
                            leftMargin=15 * mm, rightMargin=15 * mm,
                            title="Passive Monitor Overview")
    doc.build(story)
    buffer.seek(0)

    filename = f"passive_monitor_overview_{datetime.now():%Y%m%d_%H%M}.pdf"
    return filename, buffer.getvalue()


def build_fire_pdf():
    """Return (filename, bytes) for a Fire / Incidents situation report: headline
    counts, the active community warnings (worst first), and active incidents
    (fires first). The state map is included when kaleido can render it, but its
    absence never blocks the report — the tables are the substance."""
    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.lib.units import mm
        from reportlab.platypus import (Image, Paragraph, SimpleDocTemplate,
                                        Spacer, Table, TableStyle)
    except ImportError as e:
        raise ReportingUnavailable(
            "The 'reportlab' package is required for PDF reports "
            "(pip install reportlab).") from e

    styles = getSampleStyleSheet()
    cell = ParagraphStyle("fire", parent=styles["Normal"], fontSize=9, leading=11.5)
    story = []
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    story.append(Paragraph("Passive Monitor — Fire / Incidents Situation Report",
                           styles["Title"]))
    story.append(Paragraph(f"Generated {now}", styles["Normal"]))
    story.append(Spacer(1, 6 * mm))

    # --- Headline counts ------------------------------------------------------
    c = fire_data.latest_counts()
    kpi_rows = [
        ["Active Fires", str(c["active_fires"]), "Emergency Warnings", str(c["emergency"])],
        ["Watch & Act", str(c["watch_act"]), "Advice", str(c["advice"])],
        ["Total Active Events", str(c["total"]), "", ""],
    ]
    kpi = Table(kpi_rows, colWidths=[45 * mm, 40 * mm, 45 * mm, 40 * mm])
    kpi.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#eef2f7")),
        ("BACKGROUND", (2, 0), (2, -1), colors.HexColor("#eef2f7")),
        ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
        ("FONTNAME", (2, 0), (2, -1), "Helvetica-Bold"),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#c8d0da")),
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    story.append(kpi)
    story.append(Spacer(1, 5 * mm))

    df = fire_data.active_incidents()
    warnings = df[df["feed_type"] == "warning"].copy() if not df.empty else df
    incidents = df[df["feed_type"] != "warning"].copy() if not df.empty else df

    def _table(rows, widths):
        t = Table(rows, colWidths=widths, repeatRows=1)
        t.setStyle(TableStyle([
            ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#c8d0da")),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eef2f7")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]))
        return t

    # --- Community warnings (worst first) -------------------------------------
    story.append(Paragraph("Community warnings", styles["Heading2"]))
    if warnings is None or warnings.empty:
        story.append(Paragraph("No active community warnings.", styles["Italic"]))
    else:
        warnings["_prio"] = warnings["warning_level"].map(
            lambda lv: fire_data.classify(lv)[0])
        warnings = warnings.sort_values(["_prio", "location"])
        header = [Paragraph(f"<b>{h}</b>", cell) for h in
                  ("Level", "Location", "Event", "Action")]
        rows = [header]
        row_styles = []
        for i, (_, r) in enumerate(warnings.iterrows(), start=1):
            colour = fire_data.classify(r["warning_level"])[1]
            rows.append([
                Paragraph(str(r["warning_level"] or "—"), cell),
                Paragraph(str(r["location"] or "—"), cell),
                Paragraph(str(r["event"] or "—"), cell),
                Paragraph(str(r["action"] or "—"), cell),
            ])
            row_styles.append(
                ("TEXTCOLOR", (0, i), (0, i), colors.HexColor(colour)))
            row_styles.append(("FONTNAME", (0, i), (0, i), "Helvetica-Bold"))
        t = _table(rows, [30 * mm, 62 * mm, 45 * mm, 45 * mm])
        t.setStyle(TableStyle(row_styles))
        story.append(t)
    story.append(Spacer(1, 5 * mm))

    # --- Active incidents (fires first) ---------------------------------------
    story.append(Paragraph("Active incidents", styles["Heading2"]))
    if incidents is None or incidents.empty:
        story.append(Paragraph("No active incidents.", styles["Italic"]))
    else:
        incidents["_fire"] = (incidents["category1"].fillna("").str.lower()
                              != "fire")  # False (fires) sort first
        incidents = incidents.sort_values(["_fire", "category1", "location"]).head(50)
        header = [Paragraph(f"<b>{h}</b>", cell) for h in
                  ("Category", "Location", "Status", "Size")]
        rows = [header]
        for _, r in incidents.iterrows():
            rows.append([
                Paragraph(str(r["category1"] or "—"), cell),
                Paragraph(str(r["location"] or "—"), cell),
                Paragraph(str(r["status"] or "—"), cell),
                Paragraph(str(r["size"] or "—"), cell),
            ])
        story.append(_table(rows, [40 * mm, 70 * mm, 42 * mm, 30 * mm]))
    story.append(Spacer(1, 5 * mm))

    # --- State map (best-effort; omitted if kaleido can't render) --------------
    if not df.empty and df[["latitude", "longitude"]].dropna().shape[0]:
        try:
            from app.pages import fire as fire_page
            png = _fig_png(fire_page._map_figure(df, dark=False))
            story.append(Paragraph("Active incidents & warnings map", styles["Heading3"]))
            story.append(Image(io.BytesIO(png), width=180 * mm, height=110 * mm))
        except ReportingUnavailable as e:
            log.info("Fire sitrep map skipped (%s)", e)

    story.append(Spacer(1, 4 * mm))
    story.append(Paragraph(
        "Source: VicEmergency public feed. Situational awareness only — always "
        "act on official warnings at emergency.vic.gov.au.",
        ParagraphStyle("src", parent=styles["Normal"], fontSize=7.5,
                       textColor=colors.HexColor("#5f6368"))))

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4,
                            topMargin=15 * mm, bottomMargin=15 * mm,
                            leftMargin=15 * mm, rightMargin=15 * mm,
                            title="Passive Monitor Fire Situation Report")
    doc.build(story)
    buffer.seek(0)
    filename = f"fire_situation_report_{datetime.now():%Y%m%d_%H%M}.pdf"
    return filename, buffer.getvalue()


_BAND_COLOURS = {"major": "#d62728", "moderate": "#ff7f0e",
                 "minor": "#e6c700", "below": "#9aa0a6"}


def _stick_drawing(current, levels, impacts, width=170, height=300):
    """The linear flood-gauge stick as a reportlab vector Drawing (no kaleido:
    plotly image export hangs on some hosts, and vectors print crisper)."""
    import pandas as pd
    from reportlab.graphics.shapes import Drawing, Line, Polygon, Rect, String
    from reportlab.lib import colors

    lv = {k: (float(levels[k]) if levels and levels.get(k) is not None
              and pd.notna(levels.get(k)) else None)
          for k in ("minor", "moderate", "major")} if levels else {}
    heights = [v for v in lv.values() if v is not None]
    if current is not None and pd.notna(current):
        heights.append(float(current))
    if impacts is not None and not impacts.empty:
        heights += impacts["height_m"].dropna().tolist()
    if not heights:
        heights = [0.0, 1.0]
    lo, hi = min(heights), max(heights)
    span = max(hi - lo, 0.5)
    top, base = hi + span * 0.10, lo - span * 0.15

    x0, x1 = 55, 120           # stick body
    pad_bottom, pad_top = 16, 8
    plot_h = height - pad_bottom - pad_top

    def y(v):
        return pad_bottom + (v - base) / (top - base) * plot_h

    d = Drawing(width, height)

    def band(y0v, y1v, hexcolour):
        c = colors.HexColor(hexcolour)
        d.add(Rect(x0, y(y0v), x1 - x0, y(min(y1v, top)) - y(y0v),
                   fillColor=colors.Color(c.red, c.green, c.blue, alpha=0.22),
                   strokeColor=None))

    if lv.get("minor") is not None:
        band(lv["minor"], lv.get("moderate") or lv.get("major") or top,
             _BAND_COLOURS["minor"])
    if lv.get("moderate") is not None:
        band(lv["moderate"], lv.get("major") or top, _BAND_COLOURS["moderate"])
    if lv.get("major") is not None:
        band(lv["major"], top, _BAND_COLOURS["major"])

    # water column
    if current is not None and pd.notna(current) and current > base:
        d.add(Rect(x0, y(base), x1 - x0, y(min(float(current), top)) - y(base),
                   fillColor=colors.Color(0.12, 0.47, 0.71, alpha=0.55),
                   strokeColor=None))
        d.add(Line(x0 - 14, y(current), x1 + 6, y(current),
                   strokeColor=colors.HexColor("#1f77b4"), strokeWidth=2))
        d.add(String(x0 - 16, y(current) - 3, f"{current:.2f} m",
                     fontName="Helvetica-Bold", fontSize=8,
                     fillColor=colors.HexColor("#1f77b4"), textAnchor="end"))

    # stick outline
    d.add(Rect(x0, y(base), x1 - x0, y(top) - y(base),
               fillColor=None, strokeColor=colors.HexColor("#5f6368"),
               strokeWidth=1.2))

    # class level lines + labels
    for cls in ("minor", "moderate", "major"):
        v = lv.get(cls)
        if v is None:
            continue
        colour = colors.HexColor(_BAND_COLOURS[cls])
        d.add(Line(x0, y(v), x1, y(v), strokeColor=colour, strokeWidth=1.2,
                   strokeDashArray=[3, 2]))
        d.add(String(x1 + 4, y(v) - 3, f"{cls.title()} {v:g} m",
                     fontName="Helvetica", fontSize=7, fillColor=colour))

    # impact markers (heights are detailed in the table)
    if impacts is not None and not impacts.empty:
        for h in impacts["height_m"].dropna():
            d.add(Polygon([x0 - 8, y(h) - 3, x0 - 8, y(h) + 3, x0 - 2, y(h)],
                          fillColor=colors.HexColor("#1f77b4"),
                          strokeColor=None))

    # scale (base / top)
    d.add(String(x0 - 16, y(base) - 3, f"{base:.1f}", fontName="Helvetica",
                 fontSize=7, fillColor=colors.HexColor("#5f6368"),
                 textAnchor="end"))
    d.add(String(x0 - 16, y(top) - 3, f"{top:.1f}", fontName="Helvetica",
                 fontSize=7, fillColor=colors.HexColor("#5f6368"),
                 textAnchor="end"))
    return d


def _trend_drawing(hist, levels, width=330, height=300):
    """Recent height history as a reportlab vector line chart."""
    import pandas as pd
    from reportlab.graphics.shapes import Drawing, Line, PolyLine, Rect, String
    from reportlab.lib import colors

    d = Drawing(width, height)
    pad_l, pad_r, pad_b, pad_t = 34, 8, 24, 8
    plot_w, plot_h = width - pad_l - pad_r, height - pad_b - pad_t

    ts = pd.to_datetime(hist["timestamp"])
    xs = ts.astype("int64") / 1e9
    ys = hist["height_m"].astype(float)
    lv = [float(levels[k]) for k in ("minor", "moderate", "major")
          if levels and levels.get(k) is not None and pd.notna(levels.get(k))]
    ylo = min(ys.min(), min(lv) if lv else ys.min())
    yhi = max(ys.max(), max(lv) if lv else ys.max())
    yspan = max(yhi - ylo, 0.3)
    ylo, yhi = ylo - yspan * 0.08, yhi + yspan * 0.08
    xlo, xhi = xs.min(), xs.max()
    if xhi <= xlo:
        xlo, xhi = xlo - 1800, xhi + 1800

    def X(v):
        return pad_l + (v - xlo) / (xhi - xlo) * plot_w

    def Y(v):
        return pad_b + (v - ylo) / (yhi - ylo) * plot_h

    d.add(Rect(pad_l, pad_b, plot_w, plot_h, fillColor=None,
               strokeColor=colors.HexColor("#c8d0da"), strokeWidth=0.8))

    # y gridlines + labels
    for i in range(5):
        v = ylo + (yhi - ylo) * i / 4
        d.add(Line(pad_l, Y(v), pad_l + plot_w, Y(v),
                   strokeColor=colors.HexColor("#e3e8ee"), strokeWidth=0.5))
        d.add(String(pad_l - 4, Y(v) - 3, f"{v:.2f}", fontName="Helvetica",
                     fontSize=7, fillColor=colors.HexColor("#5f6368"),
                     textAnchor="end"))

    # x tick labels
    for i in range(4):
        v = xlo + (xhi - xlo) * i / 3
        label = pd.to_datetime(v, unit="s").strftime("%d %b %H:%M")
        d.add(String(min(X(v), pad_l + plot_w - 20), pad_b - 12, label,
                     fontName="Helvetica", fontSize=7,
                     fillColor=colors.HexColor("#5f6368"),
                     textAnchor="middle" if 0 < i < 3 else
                     ("start" if i == 0 else "end")))

    # flood class levels
    for cls in ("minor", "moderate", "major"):
        v = (levels or {}).get(cls)
        if v is None or pd.isna(v) or not (ylo <= float(v) <= yhi):
            continue
        colour = colors.HexColor(_BAND_COLOURS[cls])
        d.add(Line(pad_l, Y(float(v)), pad_l + plot_w, Y(float(v)),
                   strokeColor=colour, strokeWidth=1, strokeDashArray=[3, 2]))
        d.add(String(pad_l + 2, Y(float(v)) + 2, cls.title(),
                     fontName="Helvetica", fontSize=7, fillColor=colour))

    points = []
    for xv, yv in zip(xs, ys):
        points += [X(xv), Y(yv)]
    if len(points) >= 4:
        d.add(PolyLine(points, strokeColor=colors.HexColor("#1f77b4"),
                       strokeWidth=1.4))
    elif len(points) == 2:
        d.add(Rect(points[0] - 2, points[1] - 2, 4, 4,
                   fillColor=colors.HexColor("#1f77b4"), strokeColor=None))
    return d


def build_station_pdf(station_key):
    """Return (filename, bytes) for a detailed one-gauge flood briefing:
    current state, the linear gauge stick, the recent trend, and the Local
    Flood Guide watch points / expected impacts. Drawn with reportlab vector
    graphics only — no kaleido/Chromium needed."""
    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.lib.units import mm
        from reportlab.platypus import (Paragraph, SimpleDocTemplate,
                                        Spacer, Table, TableStyle)
    except ImportError as e:
        raise ReportingUnavailable(
            "The 'reportlab' package is required for PDF reports "
            "(pip install reportlab).") from e

    import pandas as pd

    from app.pages import station as station_page

    station_key = str(station_key).strip().lower()
    latest = flood_data.station_latest(station_key)
    station_name = (latest or {}).get("station_name") or station_key.title()
    levels = flood_data.load_flood_levels().get(station_key)
    impacts = flood_data.load_gauge_impacts(station_key)
    current = latest["height_m"] if latest else None
    priority, label, colour = flood_data.classify_station(
        current if current is not None else float("nan"), levels)

    styles = getSampleStyleSheet()
    cell_style = ParagraphStyle("impact", parent=styles["Normal"], fontSize=9,
                                leading=11.5)
    story = []
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    story.append(Paragraph(f"Flood Gauge Briefing — {station_name}",
                           styles["Title"]))
    story.append(Paragraph(f"Generated {now} — Passive Monitor",
                           styles["Normal"]))
    story.append(Spacer(1, 6 * mm))

    # --- Current state -------------------------------------------------------
    def lv(key):
        v = (levels or {}).get(key)
        return f"{v:g} m" if v is not None and pd.notna(v) else "—"

    state_rows = [
        ["Latest height",
         f"{current:.2f} m" if current is not None and pd.notna(current) else "—",
         "Classification", label],
        ["Last observation", str((latest or {}).get("timestamp") or "no data"),
         "Tendency", (latest or {}).get("tendency") or "—"],
        ["Minor level", lv("minor"), "Moderate level", lv("moderate")],
        ["Major level", lv("major"), "Catchment",
         (latest or {}).get("catchment") or "—"],
    ]
    state = Table(state_rows, colWidths=[38 * mm, 52 * mm, 38 * mm, 52 * mm])
    state.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#eef2f7")),
        ("BACKGROUND", (2, 0), (2, -1), colors.HexColor("#eef2f7")),
        ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
        ("FONTNAME", (2, 0), (2, -1), "Helvetica-Bold"),
        ("TEXTCOLOR", (3, 0), (3, 0), colors.HexColor(colour)),
        ("FONTNAME", (3, 0), (3, 0), "Helvetica-Bold"),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#c8d0da")),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    story.append(state)
    story.append(Spacer(1, 5 * mm))

    # --- Gauge stick + trend graph side by side (vector drawings) ------------
    stick_cell = _stick_drawing(current, levels, impacts,
                                width=185, height=310)
    hist = flood_data.station_history(station_key, days=30)
    if hist.empty:
        hist = flood_data.station_history(station_key)

    caption = ParagraphStyle("cap", parent=styles["Normal"], fontSize=9,
                             fontName="Helvetica-Bold")
    if not hist.empty:
        graph_cell = _trend_drawing(hist, levels, width=320, height=310)
        pair = Table([[Paragraph("Flood gauge", caption),
                       Paragraph("Height — last 30 days", caption)],
                      [stick_cell, graph_cell]],
                     colWidths=[68 * mm, 116 * mm])
        pair.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))
        story.append(pair)
    else:
        story.append(Paragraph("Flood gauge", caption))
        story.append(stick_cell)
        story.append(Paragraph("No observations recorded for this station yet.",
                               styles["Italic"]))
    story.append(Spacer(1, 5 * mm))

    # --- Watch points / impacts ----------------------------------------------
    story.append(Paragraph("Watch points and expected impacts",
                           styles["Heading2"]))
    if impacts.empty:
        story.append(Paragraph(
            "No Local Flood Guide impact information is available for this "
            "gauge.", styles["Italic"]))
    else:
        band_colours = {"major": "#d62728", "moderate": "#ff7f0e",
                        "minor": "#e6c700", "below": "#9aa0a6"}
        header = [Paragraph("<b>Height</b>", cell_style),
                  Paragraph("<b>Status</b>", cell_style),
                  Paragraph("<b>Expected impacts / previous floods</b>", cell_style)]
        rows, row_styles = [header], []
        for i, (_, r) in enumerate(impacts.iterrows(), start=1):
            h = r["height_m"]
            reached = (current is not None and pd.notna(current)
                       and current >= h)
            cls = station_page._class_of(h, levels)
            rows.append([
                Paragraph(f"<b>{h:.2f} m</b>", cell_style),
                Paragraph("reached" if reached else "", cell_style),
                Paragraph(str(r["impact"]), cell_style),
            ])
            row_styles.append(
                ("TEXTCOLOR", (0, i), (0, i), colors.HexColor(band_colours[cls])))
            if reached:
                row_styles.append(
                    ("BACKGROUND", (0, i), (-1, i), colors.HexColor("#ddeaf6")))
        imp_table = Table(rows, colWidths=[22 * mm, 18 * mm, 142 * mm],
                          repeatRows=1)
        imp_table.setStyle(TableStyle([
            ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#c8d0da")),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eef2f7")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ] + row_styles))
        story.append(imp_table)
        story.append(Spacer(1, 4 * mm))
        guides = impacts[["town", "source_pdf"]].drop_duplicates()
        source = ("Impact information extracted from VICSES Local Flood "
                  "Guide(s): " +
                  "; ".join(f"{t} ({s})" for t, s in
                            zip(guides["town"], guides["source_pdf"])) +
                  ". Impacts are indicative only — no two floods are the same.")
        story.append(Paragraph(source, ParagraphStyle(
            "src", parent=styles["Normal"], fontSize=7.5,
            textColor=colors.HexColor("#5f6368"))))

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4,
                            topMargin=14 * mm, bottomMargin=14 * mm,
                            leftMargin=13 * mm, rightMargin=13 * mm,
                            title=f"Flood Gauge Briefing — {station_name}")
    doc.build(story)
    buffer.seek(0)

    safe = "".join(c if c.isalnum() or c in " -_" else "_"
                   for c in station_name)[:60].strip().replace(" ", "_")
    filename = f"gauge_briefing_{safe}_{datetime.now():%Y%m%d_%H%M}.pdf"
    return filename, buffer.getvalue()

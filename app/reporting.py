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

from app.config import load_config
from app.modules.flood import data as flood_data
from app.modules.power import data as power_data

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

    kpi_rows = [
        ["Customers Off", fmt(off), "Alert Level", alert],
        ["Power Dependant Off", fmt(totals.get("power_dependant_off")),
         "Stations Flooding", str(flooding)],
        ["Planned", fmt(totals.get("planned")),
         "Unplanned", fmt(totals.get("unplanned"))],
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

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4,
                            topMargin=15 * mm, bottomMargin=15 * mm,
                            leftMargin=15 * mm, rightMargin=15 * mm,
                            title="Passive Monitor Overview")
    doc.build(story)
    buffer.seek(0)

    filename = f"passive_monitor_overview_{datetime.now():%Y%m%d_%H%M}.pdf"
    return filename, buffer.getvalue()

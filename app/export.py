"""Data export: bundle a date range (usually a tag) into a single XLSX file.

One workbook with a Summary sheet plus a sheet per selected module. Uses
openpyxl (already a dependency) via pandas, so no extra system packages are
needed on the server.
"""
import io
import logging
from datetime import datetime

import pandas as pd

from app.modules.flood import data as flood_data
from app.modules.power import data as power_data
from app.modules.weather import data as weather_data

log = logging.getLogger(__name__)


def _safe_name(text):
    keep = "".join(c if c.isalnum() or c in " -_" else "_" for c in str(text))
    return keep.strip().replace(" ", "_") or "export"


def build_export(start_ts, end_ts, label="range",
                 include_flood=True, include_power=True, include_rainfall=True):
    """Return (filename, bytes) for an XLSX export of everything in
    [start_ts, end_ts]."""
    flood_df = (flood_data.load_observations(start_ts, end_ts)
                if include_flood else pd.DataFrame())
    power_ts = (power_data.load_timeseries_range(start_ts, end_ts)
                if include_power else pd.DataFrame())
    power_out = (power_data.outages_in_range(start_ts, end_ts)
                 if include_power else pd.DataFrame())
    rain_raw = (weather_data.load_aws_range(start_ts, end_ts)
                if include_rainfall else pd.DataFrame())
    rain_totals = (weather_data.aws_event_total(start_ts, end_ts)
                   if include_rainfall else pd.DataFrame())

    summary = pd.DataFrame([
        {"Field": "Tag / label", "Value": label},
        {"Field": "Range start", "Value": start_ts},
        {"Field": "Range end", "Value": end_ts},
        {"Field": "Generated", "Value": datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
        {"Field": "Flood observations", "Value": len(flood_df)},
        {"Field": "Power readings", "Value": len(power_ts)},
        {"Field": "Power outages", "Value": len(power_out)},
        {"Field": "AWS rainfall readings", "Value": len(rain_raw)},
    ])

    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        summary.to_excel(writer, sheet_name="Summary", index=False)
        if include_flood:
            (flood_df if not flood_df.empty else pd.DataFrame(
                columns=["event", "station_name", "height_m", "timestamp"])
             ).to_excel(writer, sheet_name="Flood Observations", index=False)
        if include_power:
            (power_ts if not power_ts.empty else pd.DataFrame(
                columns=["timestamp", "customers_off"])
             ).to_excel(writer, sheet_name="Power Timeseries", index=False)
            (power_out if not power_out.empty else pd.DataFrame(
                columns=["location", "customers_off", "first_seen"])
             ).to_excel(writer, sheet_name="Power Outages", index=False)
        if include_rainfall:
            # Event totals are 9am-reset-proof (sum of positive increments).
            (rain_totals if not rain_totals.empty else pd.DataFrame(
                columns=["wmo", "name", "total_mm"])
             ).to_excel(writer, sheet_name="Rainfall Event Totals", index=False)
            (rain_raw if not rain_raw.empty else pd.DataFrame(
                columns=["wmo", "name", "rain_since_9am_mm", "obs_time"])
             ).to_excel(writer, sheet_name="AWS Rainfall Readings", index=False)
    buffer.seek(0)

    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    filename = f"passive_monitor_{_safe_name(label)}_{stamp}.xlsx"
    log.info("Built export '%s' (%d flood, %d power ts, %d outages, %d rainfall)",
             filename, len(flood_df), len(power_ts), len(power_out), len(rain_raw))
    return filename, buffer.getvalue()

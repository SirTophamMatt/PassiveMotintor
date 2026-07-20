"""Storm tracker data queries and cell classification styling."""
import json
import os
from datetime import datetime, timedelta, timezone

import pandas as pd

from app import database

# classification -> (sort priority, hex colour). Lower priority = more severe,
# mirroring the fire/flood classify conventions.
CLASS_STYLE = {
    "strong": (1, "#d62728"),
    "moderate": (2, "#ff7f0e"),
    "weak": (3, "#5b8def"),
}

ACTIVE_WINDOW_MINUTES = 20  # a cell unseen this long is no longer "active"


def classify(classification):
    return CLASS_STYLE.get(classification, (4, "#9aa0a6"))


def active_cells(window_minutes=ACTIVE_WINDOW_MINUTES):
    """The latest observation of every cell seen in the recent window,
    strongest first."""
    cutoff = (datetime.now() - timedelta(minutes=window_minutes)
              ).isoformat(sep=" ", timespec="seconds")
    df = database.read_df(
        "SELECT c.* FROM storm_cells c JOIN ("
        "  SELECT cell_id, MAX(frame_ts) AS latest FROM storm_cells"
        "  GROUP BY cell_id) t"
        " ON c.cell_id = t.cell_id AND c.frame_ts = t.latest"
        " WHERE c.frame_ts >= ? ORDER BY c.intensity_score DESC", [cutoff])
    return df


def latest_counts():
    """Headline counts for KPI cards."""
    df = active_cells()
    if df.empty:
        return {"total": 0, "strong": 0, "moderate": 0, "max_intensity": 0}
    cls = df["classification"].fillna("")
    return {
        "total": len(df),
        "strong": int((cls == "strong").sum()),
        "moderate": int((cls == "moderate").sum()),
        "max_intensity": int(df["intensity_score"].max() or 0),
    }


def recent_alerts(limit=25):
    return database.read_df(
        "SELECT timestamp, cell_id, alert_type, classification, message "
        "FROM storm_alerts ORDER BY timestamp DESC, id DESC LIMIT ?", [limit])


def cell_history(hours=6, top_n=12):
    """Per-cell intensity over the recent window for the trend graph, limited
    to the strongest cells so the chart stays readable."""
    cutoff = (datetime.now() - timedelta(hours=hours)
              ).isoformat(sep=" ", timespec="seconds")
    df = database.read_df(
        "SELECT frame_ts, cell_id, intensity_score, classification "
        "FROM storm_cells WHERE frame_ts >= ? ORDER BY frame_ts", [cutoff])
    if df.empty:
        return df
    top = (df.groupby("cell_id")["intensity_score"].max()
           .nlargest(top_n).index)
    df = df[df["cell_id"].isin(top)].copy()
    df["frame_ts"] = pd.to_datetime(df["frame_ts"], format="ISO8601",
                                    errors="coerce")
    return df


def impact_featurecollection():
    """GeoJSON FeatureCollection of the current impact-area polygons (latest
    observation of each active moderate/strong cell). Loads straight into
    any GIS / EM-COP / geojson.io."""
    df = active_cells()
    features = []
    if not df.empty and "impact_geojson" in df.columns:
        for raw in df["impact_geojson"].dropna():
            try:
                features.append(json.loads(raw))
            except ValueError:
                continue
    return {
        "type": "FeatureCollection",
        "generated": datetime.now().isoformat(sep=" ", timespec="seconds"),
        "features": features,
    }


def heartbeat_summary():
    """(cycle_count, last_timestamp) for the storm collector heartbeat."""
    df = database.read_df(
        "SELECT COUNT(*) AS n, MAX(timestamp) AS last FROM storm_timeseries")
    if df.empty or not df.iloc[0]["n"]:
        return 0, None
    return int(df.iloc[0]["n"]), df.iloc[0]["last"]


def annotated_frames(radar_id):
    """Filenames of the saved annotated loop frames, oldest first, with a
    display label parsed from the embedded UTC stamp (shown as local time)."""
    from app.modules.storm.scraper import STORM_FRAMES_DIR
    try:
        names = sorted(f for f in os.listdir(STORM_FRAMES_DIR)
                       if f.startswith(f"annotated_{radar_id}_")
                       and f.endswith(".png"))
    except OSError:
        return []
    frames = []
    for name in names:
        stamp = name.rsplit("_", 1)[-1].removesuffix(".png")
        try:
            label = (datetime.strptime(stamp, "%Y%m%d%H%M")
                     .replace(tzinfo=timezone.utc).astimezone()
                     .strftime("%d %b %H:%M"))
        except ValueError:
            label = stamp
        frames.append({"file": name, "label": label})
    return frames

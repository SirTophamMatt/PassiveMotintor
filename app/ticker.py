"""Live shell widgets: the sidebar incident log and the bottom news ticker.

Both are part of the app shell (every page) and refresh on one interval.

**Sidebar log** — a running feed of VicEmergency incidents as they arrive
(newest first, coloured by kind).

**News ticker** — a scrolling bar of NEW triggers, each stamped with the time
it triggered:
  - new BoM warnings and new VicEmergency community warnings,
  - flood gauges CROSSING into flood (the crossing time is the stamp).
An item stays on the ticker for TICKER_WINDOW_MINUTES. Exceptions that pin
the ticker open (and turn it red) for as long as they are active:
  - a VicEmergency **Emergency Warning** (incl. Evacuate), or
  - a BoM warning carrying the **Standard Emergency Warning Signal** (SEWS —
    detected from the warning body text).
Everything is computed from stored feed timestamps (first_seen / observation
times), so a server restart neither re-fires old items nor loses active ones.
"""
import logging
from datetime import datetime, timedelta

import pandas as pd
from dash import Input, Output, html

from app import database
from app.modules.fire import data as fire_data
from app.modules.flood import data as flood_data

log = logging.getLogger(__name__)

TICKER_WINDOW_MINUTES = 5
LOG_ROWS = 14

_EMERGENCY_LEVELS = {"emergency warning", "evacuate", "evacuation"}
_SEWS_TEXT = "standard emergency warning signal"


def _parse_ts(value):
    try:
        return datetime.fromisoformat(str(value))
    except (ValueError, TypeError):
        return None


def _hhmm(dt):
    return dt.strftime("%H:%M") if dt else "--:--"


# --------------------------------------------------------------------------
# Sidebar incident log
# --------------------------------------------------------------------------

def incident_log():
    """Latest VicEmergency incidents (not warnings/burn areas), newest first."""
    df = database.read_df(
        "SELECT category1, location, status, first_seen FROM fire_incidents "
        "WHERE feed_type NOT IN ('warning', 'burn-area') "
        "ORDER BY first_seen DESC, id DESC LIMIT ?", [LOG_ROWS])
    if df.empty:
        return [html.Div("No incidents yet.", className="side-log-empty")]
    rows = []
    for _, r in df.iterrows():
        _, colour = fire_data.classify(None, r.get("category1"))
        ts = _parse_ts(r.get("first_seen"))
        rows.append(html.Div([
            html.Span("●", className="side-log-dot", style={"color": colour}),
            html.Span(_hhmm(ts), className="side-log-time"),
            html.Span(f" {r.get('category1') or 'Incident'} — "
                      f"{r.get('location') or 'unknown'}",
                      className="side-log-text",
                      title=f"{r.get('category1')} — {r.get('location')} "
                            f"({r.get('status') or 'active'})"),
        ], className="side-log-row"))
    return rows


# --------------------------------------------------------------------------
# Ticker items
# --------------------------------------------------------------------------

def _bom_items(now, cutoff):
    items = []
    df = database.read_df(
        "SELECT title, type, message, first_seen FROM weather_warnings "
        "WHERE active = 1")
    for _, r in df.iterrows():
        ts = _parse_ts(r.get("first_seen"))
        body = f"{r.get('title') or ''} {r.get('message') or ''}".lower()
        sews = _SEWS_TEXT in body
        if not sews and (ts is None or ts < cutoff):
            continue
        title = r.get("title") or "BoM warning"
        items.append({
            "ts": ts, "emergency": sews,
            "text": ("🔴 SEWS — " if sews else "") + f"BoM: {title}",
        })
    return items


def _vicemergency_items(now, cutoff):
    items = []
    df = database.read_df(
        "SELECT warning_level, location, event, first_seen FROM fire_incidents "
        "WHERE feed_type = 'warning' AND resolved = 0")
    for _, r in df.iterrows():
        ts = _parse_ts(r.get("first_seen"))
        level = str(r.get("warning_level") or "Warning")
        emergency = level.strip().lower() in _EMERGENCY_LEVELS
        if not emergency and (ts is None or ts < cutoff):
            continue
        detail = f" ({r.get('event')})" if r.get("event") else ""
        items.append({
            "ts": ts, "emergency": emergency,
            "text": ("🔴 " if emergency else "") +
                    f"VicEmergency {level} — "
                    f"{r.get('location') or 'VIC'}{detail}",
        })
    return items


def _flood_crossing_items(now, cutoff):
    """Gauges whose latest reading is at/above minor AND whose crossing into
    flood happened inside the window. The crossing reading's own observation
    time is the stamp."""
    levels = flood_data.load_flood_levels()
    if not levels:
        return []
    latest = database.read_df(
        "SELECT station_name, height_m, MAX(timestamp) AS ts "
        "FROM flood_observations GROUP BY station_name")
    items = []
    for _, row in latest.iterrows():
        lv = levels.get(str(row["station_name"]).strip().lower())
        height = pd.to_numeric(row["height_m"], errors="coerce")
        priority, label, _ = flood_data.classify_station(height, lv)
        if priority >= 4 or lv is None or pd.isna(lv.get("minor")):
            continue
        station = row["station_name"]
        below = database.read_df(
            "SELECT MAX(timestamp) AS t FROM flood_observations "
            "WHERE station_name = ? AND height_m < ?",
            [station, float(lv["minor"])])
        last_below = below.iloc[0]["t"] if not below.empty else None
        if last_below:
            crossed = database.read_df(
                "SELECT MIN(timestamp) AS t FROM flood_observations "
                "WHERE station_name = ? AND timestamp > ?",
                [station, last_below])
            crossing_ts = _parse_ts(crossed.iloc[0]["t"] if not crossed.empty
                                    else None)
        else:
            crossing_ts = None  # has never been below minor — not a crossing
        if crossing_ts is None or crossing_ts < cutoff:
            continue
        items.append({
            "ts": crossing_ts, "emergency": False,
            "text": f"🌊 {station} — {label}",
        })
    return items


def ticker_state():
    """(items, emergency_active) for the ticker, newest first."""
    now = datetime.now()
    cutoff = now - timedelta(minutes=TICKER_WINDOW_MINUTES)
    items = []
    for source in (_bom_items, _vicemergency_items, _flood_crossing_items):
        try:
            items.extend(source(now, cutoff))
        except Exception:
            log.exception("Ticker source %s failed", source.__name__)
    items.sort(key=lambda i: i["ts"] or datetime.min, reverse=True)
    return items, any(i["emergency"] for i in items)


# --------------------------------------------------------------------------
# Shell components + callback
# --------------------------------------------------------------------------

def render_ticker(items, emergency):
    if not items:
        return [], "ticker ticker-hidden"
    spans = []
    for i in items:
        spans.append(html.Span(f"[{_hhmm(i['ts'])}] ", className="ticker-time"))
        spans.append(html.Span(i["text"]))
        spans.append(html.Span("   •   ", className="ticker-sep"))
    # Longer content scrolls slower to stay readable.
    duration = max(25, 9 * len(items))
    track = html.Div(spans, className="ticker-track",
                     style={"animationDuration": f"{duration}s"})
    label = "⚠ ALERTS" if emergency else "LATEST"
    cls = "ticker ticker-emergency" if emergency else "ticker"
    return [html.Div(label, className="ticker-label"), track], cls


def register_callbacks(app):
    @app.callback(
        Output("sidebar-live-log", "children"),
        Output("news-ticker", "children"),
        Output("news-ticker", "className"),
        Input("live-tick", "n_intervals"))
    def refresh(_):
        try:
            log_rows = incident_log()
        except Exception:
            log.exception("Sidebar incident log failed")
            log_rows = []
        try:
            items, emergency = ticker_state()
        except Exception:
            log.exception("Ticker refresh failed")
            items, emergency = [], False
        children, cls = render_ticker(items, emergency)
        return log_rows, children, cls

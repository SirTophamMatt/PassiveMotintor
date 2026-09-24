"""Opt-in alert sounds: which alertable things are active right now.

The sounds themselves live in ``assets/alert_sounds.js``; this module decides
what is worth a sound. It is shell-level like ``app/ticker.py`` — the toggle
sits in the sidebar and one callback rides the shared ``live-tick``.

**Off by default, per browser.** The on/off choice is a ``localStorage`` store,
so nobody hears anything they did not ask for, and a wall display that was
switched on stays on across reloads.

**Newness is decided in the browser, not here.** The server returns the full
set of currently-active alertable items, each with a stable ``key``; the page
remembers which keys it has already seen and sounds only for keys it has not.
That is deliberately unlike the sidebar log's server-global seen-set: every
viewer has their own "already heard it", and a restart re-fires nothing. The
first snapshot after the page loads (or sounds are switched on) only seeds —
opening the dashboard mid-event must not play the whole backlog.

A key encodes the entity AND its level, so an escalation (Advice -> Watch and
Act, Minor -> Moderate) is a new key and sounds again. ``entity`` without the
level is what the page uses to notice something has ended: a ``resolvable``
entity that disappears from the set plays the ``resolved`` sound.

The flood/warning queries only run while the viewer has sounds on — the
callback returns ``None`` otherwise, so the feature costs nothing for everyone
who leaves it off.
"""
import hashlib
import logging

import pandas as pd
from dash import ClientsideFunction, Input, Output, State, dcc, html

from app import database
from app.modules.flood import data as flood_data

log = logging.getLogger(__name__)

# Sound names are the keys of SOUNDS in assets/alert_sounds.js.
_VIC_LEVEL_SOUND = {
    "emergency warning": "emergency",
    "evacuate": "emergency",
    "evacuation": "emergency",
    "watch and act": "escalation",
}
_SEWS_TEXT = "standard emergency warning signal"
PAGER_LOOKBACK = 20


def _event(key, entity, sound, resolvable):
    return {"key": key, "entity": entity, "sound": sound, "resolvable": resolvable}


def _vicemergency():
    df = database.read_df(
        "SELECT source_id, warning_level FROM fire_incidents "
        "WHERE feed_type = 'warning' AND resolved = 0")
    out = []
    for _, r in df.iterrows():
        level = str(r.get("warning_level") or "Advice").strip()
        entity = f"vic:{r.get('source_id')}"
        sound = _VIC_LEVEL_SOUND.get(level.lower(), "advice")
        out.append(_event(f"{entity}:{level.lower()}", entity, sound, True))
    return out


def _bom():
    # SEWS is matched in SQL so the (large, image-laden) bodies never leave
    # the database on a 20 s tick.
    df = database.read_df(
        "SELECT warning_id, "
        "       INSTR(LOWER(COALESCE(title, '') || ' ' || COALESCE(message, '')), ?) > 0 "
        "         AS sews "
        "FROM weather_warnings WHERE active = 1", [_SEWS_TEXT])
    out = []
    for _, r in df.iterrows():
        entity = f"bom:{r.get('warning_id')}"
        sews = bool(r.get("sews"))
        out.append(_event(f"{entity}:{'sews' if sews else 'on'}", entity,
                          "emergency" if sews else "advice", True))
    return out


def _flood():
    """Gauges at or above Minor. Not resolvable: gauges hover around a level
    for hours, and a chime every time one dips back under Minor would train
    people to ignore the sound."""
    levels = flood_data.load_flood_levels()
    if not levels:
        return []
    latest = database.read_df(
        "SELECT station_name, height_m, MAX(timestamp) AS ts "
        "FROM flood_observations GROUP BY station_name")
    out = []
    heights = pd.to_numeric(latest["height_m"], errors="coerce") \
        if not latest.empty else []
    for name, height in zip(latest.get("station_name", []), heights):
        key = str(name).strip().lower()
        priority, label, _ = flood_data.classify_station(height, levels.get(key))
        if priority <= 3:
            entity = f"flood:{key}"
            out.append(_event(f"{entity}:{label.lower()}", entity,
                              "escalation", False))
    return out


def _pager():
    df = database.read_df(
        "SELECT id FROM pager_messages WHERE is_escalation = 1 "
        "ORDER BY id DESC LIMIT ?", [PAGER_LOOKBACK])
    return [_event(f"pager:{int(i)}", f"pager:{int(i)}", "pager", False)
            for i in df.get("id", [])]


def _system():
    """A collector reporting an error. Keyed on the error text, so a collector
    failing the same way every cycle sounds once, not every cycle."""
    from app.collector import manager
    out = []
    for name, st in manager.status().items():
        err = st.get("last_error") if isinstance(st, dict) else None
        if err:
            digest = hashlib.sha1(str(err).encode("utf-8")).hexdigest()[:10]
            out.append(_event(f"sys:{name}:{digest}", f"sys:{name}",
                              "system", False))
    return out


SOURCES = (_vicemergency, _bom, _flood, _pager, _system)


def current_events():
    """Every alertable item active now. A failing source is skipped alone."""
    events = []
    for source in SOURCES:
        try:
            events.extend(source())
        except Exception:
            log.exception("Alert-sound source %s failed", source.__name__)
    return events


# --------------------------------------------------------------------------
# Shell components + callbacks
# --------------------------------------------------------------------------

def components():
    """Stores + the sidebar toggle, mounted by ``factory._shell_layout``."""
    return [
        dcc.Store(id="sound-enabled", data=False, storage_type="local"),
        dcc.Store(id="sound-events"),
        dcc.Store(id="sound-played"),
    ]


def toggle_button():
    return html.Button("🔇 Sounds off", id="sound-toggle", n_clicks=0,
                       className="btn sound-btn",
                       title="Play a sound when a new warning, flood crossing "
                             "or pager escalation arrives (this browser only)")


def register_callbacks(app):
    # Toggle + label are clientside: the click is the user gesture the browser
    # needs before it will play audio, and it has to reach WDSounds directly.
    app.clientside_callback(
        ClientsideFunction("wdsound", "toggle"),
        Output("sound-enabled", "data"),
        Input("sound-toggle", "n_clicks"),
        State("sound-enabled", "data"),
        prevent_initial_call=True)
    app.clientside_callback(
        ClientsideFunction("wdsound", "label"),
        Output("sound-toggle", "children"),
        Output("sound-toggle", "className"),
        Input("sound-enabled", "data"))

    @app.callback(
        Output("sound-events", "data"),
        Input("live-tick", "n_intervals"),
        Input("sound-enabled", "data"))
    def refresh(_, enabled):
        # None (not no_update) when off, so the page forgets what it has seen
        # and switching back on seeds silently instead of replaying.
        return current_events() if enabled else None

    app.clientside_callback(
        ClientsideFunction("wdsound", "onEvents"),
        Output("sound-played", "data"),
        Input("sound-events", "data"))

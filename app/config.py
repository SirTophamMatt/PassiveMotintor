"""Configuration handling.

Settings (including EM-COP credentials) live in config.json next to the app.
That file is gitignored and is created on first save from the Settings page.
"""
import copy
import json
import logging
import os
import sys
import threading

log = logging.getLogger(__name__)

if getattr(sys, "frozen", False):
    # Running as a PyInstaller bundle. Writable files (config, db, log) live
    # next to the .exe; read-only bundled files (assets) live in _MEIPASS.
    BASE_DIR = os.path.dirname(sys.executable)
    BUNDLE_DIR = sys._MEIPASS
else:
    BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    BUNDLE_DIR = BASE_DIR

# In a container/server deployment, point writable state (config.json,
# unified_monitor.db, log, backups) at a mounted volume via UM_DATA_DIR so it
# survives redeploys. Bundled read-only assets stay in BUNDLE_DIR.
_DATA_DIR = os.environ.get("UM_DATA_DIR")
if _DATA_DIR:
    os.makedirs(_DATA_DIR, exist_ok=True)
    BASE_DIR = _DATA_DIR

CONFIG_FILE = os.path.join(BASE_DIR, "config.json")

# The old projects live one level above the unified_monitor folder.
LEGACY_ROOT = os.path.dirname(BASE_DIR)

DEFAULTS = {
    "emcop": {
        "username": "",
        "password": "",
        "login_url": "https://cop.em.vic.gov.au/sadisplay/nicslogin.seam",
        "power_url": "https://cop.em.vic.gov.au/sadisplay/poweroutages/",
        "after_login_url": "https://app.prod.cop.em.vic.gov.au/sadisplay/feeds/vic-incident-admin/index.html",
    },
    "flood": {
        "interval_minutes": 5,
        # Web deployment is always-on: start flood collection automatically
        # when the server boots (run_web.py). Ignored by the desktop build.
        "autostart": True,
    },
    "fire": {
        # VicEmergency incident/warning feed. Public, no credentials; always-on.
        "interval_minutes": 3,
        "autostart": True,
    },
    "weather": {
        # BoM warnings + rainfall (api.weather.bom.gov.au). Public, no creds.
        # Kept gentle on BoM: warnings + rainfall every 10 min.
        "interval_minutes": 10,
        "autostart": True,
        # Cap on rainfall locations polled per cycle (derived from flood gauge
        # towns). Each polls 2 endpoints, so this bounds load on BoM.
        "max_rainfall_locations": 40,
    },
    "rainfall": {
        # BoM AWS rainfall network (~101 VIC stations) via the state obs page,
        # one request per cycle. Dedup on BoM's obs time bounds storage.
        "interval_minutes": 15,
        "autostart": True,
    },
    "storm": {
        # BoM radar storm-cell tracker. Each entry is a BoM product id whose
        # last digit encodes zoom (…1=512 km, 2=256, 3=128, 4=64); frames are
        # published ~every 5 min, so 5-min polling catches each new frame.
        # Every listed radar is tracked each cycle (cells/frames are stored
        # per radar). IDR023 = Melbourne 128 km, IDR313 = Albany WA 128 km,
        # IDR143 = Mt Gambier 128 km (covers SW Victoria — Portland/Warrnambool).
        "radar_ids": ["IDR023", "IDR313", "IDR143"],
        "interval_minutes": 5,
        "autostart": True,
        # Site coords for radars beyond the built-ins (app/modules/storm/
        # scraper.py RADAR_SITES), keyed by IDRxx prefix: {"IDR68": [lat, lon]}.
        # Needed to georeference cells / impact polygons for that radar.
        "radar_sites": {},
    },
    "roads": {
        # VicRoads / Transport Victoria "Unplanned Disruptions - Road" API
        # (GeoJSON, DoT + council roads). Unlike the fire/BoM feeds this one
        # needs a free API key: request it via the Data Exchange Platform
        # (https://data-exchange.vicroads.vic.gov.au/) then set feed_url + api_key
        # here (or on the Settings page). Until BOTH are set the collector
        # log-and-skips — nothing crashes on a fresh deploy.
        #   feed_url: the v3 "Unplanned Disruptions - Road" endpoint (default below)
        #   api_key : sent in the "KeyId" request header
        "feed_url": ("https://api.opendata.transport.vic.gov.au"
                     "/api/opendata/roads/disruptions/unplanned/v3"),
        "api_key": "",
        "interval_minutes": 3,
        "autostart": True,
        # Server-side paging (API params 'page'/'limit'). page_limit 0 = use the
        # API default page size (100) and follow meta.total_pages; set a positive
        # value to force a page size. max_pages caps the paging loop.
        "page_limit": 0,
        "max_pages": 20,
    },
    "power": {
        "interval_seconds": 60,
        "max_new_geocodes_per_cycle": 10,
        # EM-COP fingerprints headless Chrome and drops the session, so the
        # scraper runs a visible browser window by default.
        "headless": False,
        # Seconds to wait after a successful login before opening the dashboard,
        # letting the session fully establish (avoids a forbidden.seam bounce).
        "post_login_seconds": 8,
        # Start power collection on server boot too. Safe before credentials
        # are set: start_power() checks credentials_set() and simply logs and
        # skips if they are missing, so nothing crashes on a fresh deploy.
        "autostart": True,
    },
    "alerts": {
        "high_customers_off": 20000,
        "low_customers_off": 10000,
    },
    "fire_alerts": {
        # Alert on new incidents of these categories (case-insensitive) in
        # addition to any new/upgraded community warning.
        "alert_categories": ["Fire"],
    },
    "notify": {
        # Incoming-webhook URL (Slack / Teams / Discord). Empty = disabled.
        "webhook_url": "",
        # auto = detect Discord from the URL, otherwise send {"text": ...}.
        "webhook_format": "auto",
        "on_power_alert": True,
        "on_flood_alert": True,
        "on_fire_alert": True,
        "on_weather_alert": True,
        "on_storm_alert": True,
        "on_roads_alert": True,
        "on_watchdog": True,
        # Master pause: when true, suppress ALL notifications (the admin test
        # button still works — it uses force=True). Toggled from the Admin page.
        "paused": False,
    },
    "web": {
        # Admin password hash set from the Admin page. The UM_ADMIN_PASSWORD
        # environment variable, if set, takes precedence over this.
        "admin_password_hash": "",
    },
}

_lock = threading.Lock()


def _merge(base, override):
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config():
    with _lock:
        if os.path.exists(CONFIG_FILE):
            try:
                # utf-8-sig tolerates a BOM if the file was edited in Notepad/PowerShell
                with open(CONFIG_FILE, "r", encoding="utf-8-sig") as f:
                    return _merge(DEFAULTS, json.load(f))
            except (OSError, json.JSONDecodeError) as e:
                log.error("Could not read config.json (%s); using defaults", e)
        return copy.deepcopy(DEFAULTS)


def save_config(cfg):
    with _lock:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)


def credentials_set(cfg=None):
    cfg = cfg or load_config()
    return bool(cfg["emcop"]["username"] and cfg["emcop"]["password"])

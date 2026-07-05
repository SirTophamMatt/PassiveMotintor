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
    "notify": {
        # Incoming-webhook URL (Slack / Teams / Discord). Empty = disabled.
        "webhook_url": "",
        # auto = detect Discord from the URL, otherwise send {"text": ...}.
        "webhook_format": "auto",
        "on_power_alert": True,
        "on_flood_alert": True,
        "on_watchdog": True,
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

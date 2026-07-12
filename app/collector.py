"""Background data collection.

Collection runs in daemon threads owned by the server process, independent
of any browser/dashboard session — closing the dashboard tab does not stop
collection. Each collector keeps its own status for display in the UI.
"""
import logging
import threading
import time
from datetime import datetime

from app.config import load_config, credentials_set
from app.modules.fire import scraper as fire_scraper
from app.modules.flood import scraper as flood_scraper
from app.modules.flood.data import LIVE_EVENT
from app.modules.power.scraper import PowerScraper
from app.modules.weather import aws as aws_rainfall
from app.modules.weather import scraper as weather_scraper

log = logging.getLogger(__name__)


class _Collector(threading.Thread):
    def __init__(self, name, interval_seconds, work, on_stop=None):
        super().__init__(name=name, daemon=True)
        self.interval = interval_seconds
        self.work = work
        self.on_stop = on_stop
        self._stop_event = threading.Event()
        # last_cycle_ts / started_ts are time.time() floats for the watchdog:
        # a cycle that ERRORS still advances last_cycle_ts (the thread is alive
        # and retrying); only a HUNG cycle lets it go stale.
        self.status = {"last_run": None, "last_error": None, "runs": 0,
                       "last_cycle_ts": None, "started_ts": None}

    def run(self):
        self.status["started_ts"] = time.time()
        while not self._stop_event.is_set():
            try:
                self.work()
                self.status["last_run"] = datetime.now().strftime("%H:%M:%S")
                self.status["runs"] += 1
                self.status["last_error"] = None
            except Exception as e:
                log.exception("%s cycle failed", self.name)
                self.status["last_error"] = str(e)
            self.status["last_cycle_ts"] = time.time()
            self._stop_event.wait(self.interval)
        if self.on_stop:
            try:
                self.on_stop()
            except Exception:
                log.exception("%s cleanup failed", self.name)

    def stop(self):
        self._stop_event.set()


class CollectorManager:
    def __init__(self):
        self._lock = threading.Lock()
        self._flood = None
        self.flood_event = None
        self._power = None
        self._power_scraper = None
        self._fire = None
        self._weather = None
        self._rainfall = None
        # Desired state, tracked so the watchdog can tell "admin stopped this
        # on purpose" (leave it alone) from "it should be running" (restart).
        # None = no explicit action yet, fall back to the autostart config.
        self._flood_desired = None
        self._power_desired = None
        self._fire_desired = None
        self._weather_desired = None
        self._rainfall_desired = None

    # --- flood ---------------------------------------------------------------
    def start_flood(self):
        """Start always-on flood collection. Readings are stored under the
        fixed LIVE_EVENT bucket; slicing into events is done later via tags."""
        with self._lock:
            self._flood_desired = True
            if self._flood and self._flood.is_alive():
                return False, "Flood collection is already running."
            cfg = load_config()
            interval = max(1, cfg["flood"]["interval_minutes"]) * 60
            self.flood_event = LIVE_EVENT
            self._flood = _Collector(
                "flood-collector", interval,
                lambda: flood_scraper.fetch_flood_data(LIVE_EVENT))
            self._flood.start()
            return True, "Flood collection started."

    def stop_flood(self):
        with self._lock:
            self._flood_desired = False
            if self._flood:
                self._flood.stop()
                self._flood = None
            return True, "Flood collection stopped."

    def restart_flood(self):
        """Stop-then-start, used by the watchdog on a stalled collector."""
        self.stop_flood()
        return self.start_flood()

    # --- fire ----------------------------------------------------------------
    def start_fire(self):
        """Start always-on VicEmergency incident/warning collection."""
        with self._lock:
            self._fire_desired = True
            if self._fire and self._fire.is_alive():
                return False, "Fire collection is already running."
            cfg = load_config()
            interval = max(1, cfg["fire"]["interval_minutes"]) * 60
            self._fire = _Collector(
                "fire-collector", interval, fire_scraper.fetch_fire_data)
            self._fire.start()
            return True, "Fire collection started."

    def stop_fire(self):
        with self._lock:
            self._fire_desired = False
            if self._fire:
                self._fire.stop()
                self._fire = None
            return True, "Fire collection stopped."

    def restart_fire(self):
        """Stop-then-start, used by the watchdog on a stalled collector."""
        self.stop_fire()
        return self.start_fire()

    # --- weather -------------------------------------------------------------
    def start_weather(self):
        """Start always-on BoM weather (warnings + rainfall) collection."""
        with self._lock:
            self._weather_desired = True
            if self._weather and self._weather.is_alive():
                return False, "Weather collection is already running."
            cfg = load_config()
            interval = max(1, cfg["weather"]["interval_minutes"]) * 60
            self._weather = _Collector(
                "weather-collector", interval, weather_scraper.fetch_weather_data)
            self._weather.start()
            return True, "Weather collection started."

    def stop_weather(self):
        with self._lock:
            self._weather_desired = False
            if self._weather:
                self._weather.stop()
                self._weather = None
            return True, "Weather collection stopped."

    def restart_weather(self):
        """Stop-then-start, used by the watchdog on a stalled collector."""
        self.stop_weather()
        return self.start_weather()

    # --- rainfall (BoM AWS network) ------------------------------------------
    def start_rainfall(self):
        """Start always-on AWS rainfall-network collection."""
        with self._lock:
            self._rainfall_desired = True
            if self._rainfall and self._rainfall.is_alive():
                return False, "Rainfall collection is already running."
            cfg = load_config()
            interval = max(1, cfg["rainfall"]["interval_minutes"]) * 60
            self._rainfall = _Collector(
                "rainfall-collector", interval, aws_rainfall.fetch_aws_rainfall)
            self._rainfall.start()
            return True, "Rainfall collection started."

    def stop_rainfall(self):
        with self._lock:
            self._rainfall_desired = False
            if self._rainfall:
                self._rainfall.stop()
                self._rainfall = None
            return True, "Rainfall collection stopped."

    def restart_rainfall(self):
        """Stop-then-start, used by the watchdog on a stalled collector."""
        self.stop_rainfall()
        return self.start_rainfall()

    def fetch_rainfall_now(self):
        """Manual one-off AWS rainfall fetch (Admin button). Runs inline."""
        try:
            n = aws_rainfall.fetch_aws_rainfall()
            return True, f"Fetched rainfall — {n} new reading(s)."
        except Exception as e:
            log.exception("Manual rainfall fetch failed")
            return False, f"Rainfall fetch failed: {e}"

    # --- power ---------------------------------------------------------------
    def start_power(self):
        with self._lock:
            if self._power and self._power.is_alive():
                return False, "Power collection is already running."
            cfg = load_config()
            if not credentials_set(cfg):
                return False, "EM-COP credentials are not set. Add them in Settings first."
            self._power_desired = True
            interval = max(15, cfg["power"]["interval_seconds"])
            self._power_scraper = PowerScraper(cfg)
            self._power = _Collector(
                "power-collector", interval,
                self._power_scraper.scrape_cycle,
                on_stop=self._power_scraper.stop)
            self._power.start()
            return True, "Power collection started (browser login may take ~30s)."

    def stop_power(self):
        with self._lock:
            self._power_desired = False
            if self._power:
                self._power.stop()
                self._power = None
            return True, "Power collection stopped."

    def restart_power(self):
        """Stop-then-start for the watchdog. The old scraper's Chrome is quit
        in a side thread: if the old collector thread is hung mid-cycle it
        holds the scraper lock, and we must not let the watchdog block on it."""
        old_scraper = self._power_scraper
        self.stop_power()
        if old_scraper is not None:
            threading.Thread(target=old_scraper.stop, daemon=True,
                             name="power-scraper-cleanup").start()
        return self.start_power()

    # --- desired state (watchdog) ---------------------------------------------
    def flood_wanted(self, cfg):
        if self._flood_desired is not None:
            return self._flood_desired
        return cfg["flood"].get("autostart", True)

    def power_wanted(self, cfg):
        if self._power_desired is not None:
            return self._power_desired
        return cfg["power"].get("autostart", False)

    def fire_wanted(self, cfg):
        if self._fire_desired is not None:
            return self._fire_desired
        return cfg["fire"].get("autostart", True)

    def weather_wanted(self, cfg):
        if self._weather_desired is not None:
            return self._weather_desired
        return cfg["weather"].get("autostart", True)

    def rainfall_wanted(self, cfg):
        if self._rainfall_desired is not None:
            return self._rainfall_desired
        return cfg["rainfall"].get("autostart", True)

    # --- autostart --------------------------------------------------------------
    def autostart(self):
        """Start collectors flagged for auto-start in config. Called once by the
        web entry point so an always-on deployment begins collecting on boot.
        Failures are logged, never fatal to server startup."""
        cfg = load_config()
        if cfg["flood"].get("autostart", True):
            try:
                ok, msg = self.start_flood()
                log.info("Autostart flood: %s", msg)
            except Exception:
                log.exception("Autostart flood failed")
        if cfg["fire"].get("autostart", True):
            try:
                ok, msg = self.start_fire()
                log.info("Autostart fire: %s", msg)
            except Exception:
                log.exception("Autostart fire failed")
        if cfg["weather"].get("autostart", True):
            try:
                ok, msg = self.start_weather()
                log.info("Autostart weather: %s", msg)
            except Exception:
                log.exception("Autostart weather failed")
        if cfg["rainfall"].get("autostart", True):
            try:
                ok, msg = self.start_rainfall()
                log.info("Autostart rainfall: %s", msg)
            except Exception:
                log.exception("Autostart rainfall failed")
        if cfg["power"].get("autostart", False):
            try:
                ok, msg = self.start_power()
                log.info("Autostart power: %s", msg)
            except Exception:
                log.exception("Autostart power failed")

    # --- status ----------------------------------------------------------------
    def status(self):
        flood_alive = self._flood is not None and self._flood.is_alive()
        power_alive = self._power is not None and self._power.is_alive()
        fire_alive = self._fire is not None and self._fire.is_alive()
        return {
            "flood": {
                "running": flood_alive,
                "event": self.flood_event if flood_alive else None,
                **(self._flood.status if self._flood else {}),
            },
            "power": {
                "running": power_alive,
                **(self._power.status if self._power else {}),
            },
            "fire": {
                "running": fire_alive,
                **(self._fire.status if self._fire else {}),
            },
            "weather": {
                "running": self._weather is not None and self._weather.is_alive(),
                **(self._weather.status if self._weather else {}),
            },
            "rainfall": {
                "running": self._rainfall is not None and self._rainfall.is_alive(),
                **(self._rainfall.status if self._rainfall else {}),
            },
        }


manager = CollectorManager()

"""Background data collection.

Collection runs in daemon threads owned by the server process, independent
of any browser/dashboard session — closing the dashboard tab does not stop
collection. Each collector keeps its own status for display in the UI.
"""
import logging
import threading
from datetime import datetime

from app.config import load_config, credentials_set
from app.modules.flood import scraper as flood_scraper
from app.modules.flood.data import LIVE_EVENT
from app.modules.power.scraper import PowerScraper

log = logging.getLogger(__name__)


class _Collector(threading.Thread):
    def __init__(self, name, interval_seconds, work, on_stop=None):
        super().__init__(name=name, daemon=True)
        self.interval = interval_seconds
        self.work = work
        self.on_stop = on_stop
        self._stop_event = threading.Event()
        self.status = {"last_run": None, "last_error": None, "runs": 0}

    def run(self):
        while not self._stop_event.is_set():
            try:
                self.work()
                self.status["last_run"] = datetime.now().strftime("%H:%M:%S")
                self.status["runs"] += 1
                self.status["last_error"] = None
            except Exception as e:
                log.exception("%s cycle failed", self.name)
                self.status["last_error"] = str(e)
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

    # --- flood ---------------------------------------------------------------
    def start_flood(self):
        """Start always-on flood collection. Readings are stored under the
        fixed LIVE_EVENT bucket; slicing into events is done later via tags."""
        with self._lock:
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
            if self._flood:
                self._flood.stop()
                self._flood = None
            return True, "Flood collection stopped."

    # --- power ---------------------------------------------------------------
    def start_power(self):
        with self._lock:
            if self._power and self._power.is_alive():
                return False, "Power collection is already running."
            cfg = load_config()
            if not credentials_set(cfg):
                return False, "EM-COP credentials are not set. Add them in Settings first."
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
            if self._power:
                self._power.stop()
                self._power = None
            return True, "Power collection stopped."

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
        }


manager = CollectorManager()

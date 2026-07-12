"""Supervisor: collector watchdog + threshold alerting.

One daemon thread, started by the web entry point alongside autostart, that
every minute:

1. **Watchdog** — checks each collector that *should* be running (per config
   autostart / the admin's last start/stop action) and restarts it if the
   thread died or a cycle has not completed for ~3 intervals (hung scrape).
   Restarts are rate-limited to avoid thrashing a persistently-broken scraper.
   Also lets power start automatically once credentials appear in Settings.

2. **Alerts** — watches the data itself and sends notifications (app.notify)
   on state CHANGES only, so a webhook gets one message per event, not one
   per minute: customers-off crossing the low/high thresholds (and recovery),
   stations entering / escalating / clearing flood levels, collector errors,
   and every watchdog restart.

All state is in-process; a reboot re-announces current conditions once, which
for an ops tool is a feature (the monitor tells you where things stand).
"""
import logging
import threading
import time
from datetime import datetime

from app import database, notify
from app.collector import manager
from app.config import credentials_set, load_config

log = logging.getLogger(__name__)

CHECK_INTERVAL = 60          # seconds between supervisor passes
MAX_RESTARTS_PER_HOUR = 4    # per collector; beyond this, log and wait


def _now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class Supervisor(threading.Thread):
    def __init__(self):
        super().__init__(name="supervisor", daemon=True)
        self._stop_event = threading.Event()
        self._restarts = {"flood": [], "power": [], "fire": []}
        self._last_power_level = None      # None until first evaluation
        self._flooding = {}                # station -> (priority, label)
        self._first_flood_check = True
        self._fire_events = {}             # source_id -> (priority, label)
        self._first_fire_check = True
        self._last_errors = {"flood": None, "power": None, "fire": None}
        self.state = {"started": None, "checks": 0, "last_check": None,
                      "flood_restarts": 0, "power_restarts": 0,
                      "fire_restarts": 0, "last_action": None}

    def ensure_started(self):
        if not self.is_alive():
            try:
                self.start()
                log.info("Supervisor started (watchdog + alerts, every %ds)",
                         CHECK_INTERVAL)
            except RuntimeError:
                pass  # already started once; threads are single-use

    def stop(self):
        self._stop_event.set()

    def run(self):
        self.state["started"] = _now_str()
        while not self._stop_event.is_set():
            try:
                self._check_collectors()
            except Exception:
                log.exception("Watchdog collector check failed")
            try:
                self._check_alerts()
            except Exception:
                log.exception("Watchdog alert check failed")
            self.state["checks"] += 1
            self.state["last_check"] = datetime.now().strftime("%H:%M:%S")
            self._stop_event.wait(CHECK_INTERVAL)

    # --- watchdog --------------------------------------------------------------
    def _can_restart(self, which):
        now = time.time()
        self._restarts[which] = [t for t in self._restarts[which]
                                 if now - t < 3600]
        return len(self._restarts[which]) < MAX_RESTARTS_PER_HOUR

    def _record_restart(self, which, reason, message):
        self._restarts[which].append(time.time())
        self.state[f"{which}_restarts"] += 1
        action = f"{_now_str()} — restarted {which}: {reason}"
        self.state["last_action"] = action
        log.warning("Watchdog %s (result: %s)", action, message)
        notify.send(f"🔄 Watchdog restarted {which} collection — {reason}. "
                    f"({message})", kind="watchdog")

    def _stall_reason(self, s, interval):
        """None if healthy, else a human-readable reason to restart."""
        if not s["running"]:
            return "collector thread is not running"
        now = time.time()
        last = s.get("last_cycle_ts") or s.get("started_ts")
        if last and now - last > max(3 * interval, 900):
            return f"no completed cycle in {int((now - last) // 60)} minutes"
        return None

    def _check_collectors(self):
        cfg = load_config()
        status = manager.status()

        if manager.flood_wanted(cfg):
            reason = self._stall_reason(status["flood"],
                                        max(1, cfg["flood"]["interval_minutes"]) * 60)
            if reason and self._can_restart("flood"):
                ok, msg = manager.restart_flood()
                self._record_restart("flood", reason, msg)

        if manager.power_wanted(cfg) and credentials_set(cfg):
            reason = self._stall_reason(status["power"],
                                        max(15, cfg["power"]["interval_seconds"]))
            if reason and self._can_restart("power"):
                ok, msg = manager.restart_power()
                self._record_restart("power", reason, msg)

        if manager.fire_wanted(cfg):
            reason = self._stall_reason(status["fire"],
                                        max(1, cfg["fire"]["interval_minutes"]) * 60)
            if reason and self._can_restart("fire"):
                ok, msg = manager.restart_fire()
                self._record_restart("fire", reason, msg)

        # Notify once per DISTINCT collector error (a failing-every-cycle
        # scraper should ping you once, not every minute).
        for which in ("flood", "power", "fire"):
            err = status[which].get("last_error")
            if err and err != self._last_errors[which]:
                notify.send(f"⚠ {which} collector error: {err}", kind="watchdog")
            self._last_errors[which] = err

    # --- alerts ----------------------------------------------------------------
    def _check_alerts(self):
        cfg = load_config()
        self._check_power_alert(cfg)
        self._check_flood_alert(cfg)
        self._check_fire_alert(cfg)

    def _check_power_alert(self, cfg):
        from app.modules.power import data as power_data
        totals = power_data.latest_totals()
        if not totals or totals.get("customers_off") is None:
            return
        off = int(totals["customers_off"])
        high = cfg["alerts"]["high_customers_off"]
        low = cfg["alerts"]["low_customers_off"]
        level = "high" if off > high else ("low" if off > low else "normal")

        if self._last_power_level is None:
            # First evaluation after boot: announce only if already elevated.
            if level != "normal":
                notify.send(f"Monitor started — power is at {level.upper()} "
                            f"alert: {off:,} customers off supply.",
                            kind="power_alert", cfg=cfg)
        elif level != self._last_power_level:
            if level == "high":
                notify.send(f"🔴 HIGH ALERT — {off:,} customers off supply "
                            f"(threshold {high:,}).", kind="power_alert", cfg=cfg)
            elif level == "low":
                notify.send(f"🟠 Low alert — {off:,} customers off supply "
                            f"(threshold {low:,}).", kind="power_alert", cfg=cfg)
            else:
                notify.send(f"🟢 Power recovered to normal — {off:,} customers "
                            "off supply.", kind="power_alert", cfg=cfg)
        self._last_power_level = level

    def _check_flood_alert(self, cfg):
        from app.modules.flood import data as flood_data
        levels = flood_data.load_flood_levels()
        if not levels:
            return
        latest = database.read_df(
            "SELECT station_name, height_m, MAX(timestamp) AS ts "
            "FROM flood_observations GROUP BY station_name")
        current = {}
        for _, row in latest.iterrows():
            lv = levels.get(str(row["station_name"]).strip().lower())
            priority, label, _ = flood_data.classify_station(row["height_m"], lv)
            if priority < 4:
                current[row["station_name"]] = (priority, label)

        if self._first_flood_check:
            if current:
                worst = sorted(current.values())[0][1]
                notify.send(f"Monitor started — {len(current)} station(s) "
                            f"currently at/above flood level (worst: {worst}).",
                            kind="flood_alert", cfg=cfg)
        else:
            escalated = [
                f"{name}: {label}"
                for name, (priority, label) in sorted(current.items())
                if name not in self._flooding or priority < self._flooding[name][0]
            ]
            cleared = [name for name in self._flooding if name not in current]
            if escalated:
                notify.send("🌊 Flood level reached/escalated — "
                            + "; ".join(escalated[:10])
                            + (f" (+{len(escalated) - 10} more)"
                               if len(escalated) > 10 else ""),
                            kind="flood_alert", cfg=cfg)
            if cleared:
                notify.send(f"✅ Below flood level again: "
                            + ", ".join(sorted(cleared)[:10])
                            + (f" (+{len(cleared) - 10} more)"
                               if len(cleared) > 10 else ""),
                            kind="flood_alert", cfg=cfg)
        self._flooding = current
        self._first_flood_check = False

    def _check_fire_alert(self, cfg):
        from app.modules.fire import data as fire_data
        alert_cats = {c.strip().lower() for c in
                      cfg.get("fire_alerts", {}).get("alert_categories", [])}
        df = fire_data.active_incidents()
        current = {}
        for _, row in df.iterrows():
            is_warning = row.get("feed_type") == "warning"
            cat = str(row.get("category1") or "").strip().lower()
            if not is_warning and cat not in alert_cats:
                continue  # only warnings + configured incident categories alert
            priority, _ = fire_data.classify(row.get("warning_level"),
                                             row.get("category1"))
            if is_warning:
                label = (f"{row.get('warning_level') or 'Warning'} — "
                         f"{row.get('location') or 'VIC'}")
            else:
                label = (f"{row.get('category1')} at "
                         f"{row.get('location') or 'unknown'} "
                         f"({row.get('status') or 'active'})")
            current[row["source_id"]] = (priority, label)

        if self._first_fire_check:
            if current:
                worst = sorted(current.values())[0][1]
                notify.send(f"Monitor started — {len(current)} active fire/warning "
                            f"event(s) (worst: {worst}).", kind="fire_alert", cfg=cfg)
        else:
            escalated = [
                label for sid, (priority, label)
                in sorted(current.items(), key=lambda kv: kv[1])
                if sid not in self._fire_events
                or priority < self._fire_events[sid][0]
            ]
            cleared = [self._fire_events[sid][1] for sid in self._fire_events
                       if sid not in current]
            if escalated:
                notify.send("🔥 New/escalated incident or warning — "
                            + "; ".join(escalated[:10])
                            + (f" (+{len(escalated) - 10} more)"
                               if len(escalated) > 10 else ""),
                            kind="fire_alert", cfg=cfg)
            if cleared:
                notify.send("✅ Cleared: " + ", ".join(cleared[:10])
                            + (f" (+{len(cleared) - 10} more)"
                               if len(cleared) > 10 else ""),
                            kind="fire_alert", cfg=cfg)
        self._fire_events = current
        self._first_fire_check = False


supervisor = Supervisor()

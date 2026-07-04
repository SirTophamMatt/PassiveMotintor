"""EM-COP power outage scraper.

Maintains a headless Chrome session logged into EM-COP, scrapes headline
outage totals plus the outage table each cycle, updates the outage tracker
and geocode cache in SQLite.
"""
import logging
import os
import threading
import time
from datetime import datetime, timedelta

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from webdriver_manager.chrome import ChromeDriverManager

from app import database

log = logging.getLogger(__name__)

USER_SELECTOR = "#nicsUsername"
PASS_SELECTOR = "#nicsPassword"
SUBMIT_SELECTOR = "#form-signin > div:nth-child(4) > button"

TOTALS_SELECTORS = {
    "customers_off": "#total",
    "power_dependant_off": "#totalp",
    "planned": "#planned",
    "unplanned": "#unplanned",
}


class DashboardNotFoundError(RuntimeError):
    """The power dashboard page/elements were not where we expected them."""


def _safe_int(text):
    try:
        return int(str(text).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def parse_first_seen(text):
    """Parses EM-COP 'first seen' strings like '09:00' or '10:17 14/05'."""
    now = datetime.now()
    text = (text or "").strip()
    try:
        if " " in text:
            dt = datetime.strptime(text, "%H:%M %d/%m").replace(year=now.year)
            if dt > now:  # date wrapped around a year boundary
                dt = dt.replace(year=now.year - 1)
        else:
            t = datetime.strptime(text, "%H:%M")
            dt = now.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
            if dt > now:
                dt -= timedelta(days=1)
        return dt
    except ValueError:
        log.warning("Could not parse First Seen value '%s'; using now", text)
        return now


class PowerScraper:
    """Owns the browser session. All methods run on the collector thread."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.driver = None
        self._geolocator = None
        self._lock = threading.Lock()
        # Window handles: the EM-COP session tab is kept open and untouched;
        # the dashboard is loaded in a separate tab so the session tab never
        # navigates away from EM-COP (which appears to drop authentication).
        self._session_handle = None
        self._dashboard_handle = None

    # --- session management -------------------------------------------------
    def _init_driver(self):
        options = Options()
        # EM-COP detects headless Chrome and silently drops the session, so the
        # browser is visible by default. Set power.headless=true in config only
        # if EM-COP later stops fingerprinting it.
        if self.cfg["power"].get("headless"):
            options.add_argument("--headless=new")
        options.add_argument("--disable-gpu")
        options.add_argument("--window-size=1920,1080")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        # reduce automation fingerprint (keeps the session alive)
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        options.add_experimental_option("useAutomationExtension", False)
        service = Service(ChromeDriverManager().install())
        driver = webdriver.Chrome(service=service, options=options)
        driver.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument",
            {"source": "Object.defineProperty(navigator, 'webdriver', "
                       "{get: () => undefined})"})
        return driver

    def _login(self, attempts=3):
        emcop = self.cfg["emcop"]
        username = emcop["username"]
        for attempt in range(1, attempts + 1):
            log.info("Logging in to EM-COP as '%s' (attempt %d/%d)",
                     username, attempt, attempts)
            self.driver.get(emcop["login_url"])
            time.sleep(2)
            self.driver.find_element(By.CSS_SELECTOR, USER_SELECTOR).send_keys(username)
            self.driver.find_element(By.CSS_SELECTOR, PASS_SELECTOR).send_keys(emcop["password"])
            self.driver.find_element(By.CSS_SELECTOR, SUBMIT_SELECTOR).click()
            time.sleep(3)
            url = self.driver.current_url
            if "forbidden.seam" in url:
                # EM-COP sometimes bounces a fresh session here; back off and retry
                log.warning("Login as '%s' redirected to forbidden.seam — "
                            "waiting 10s before retry", username)
                time.sleep(10)
                continue
            if emcop["login_url"] in url:
                raise RuntimeError(
                    f"EM-COP login as '{username}' appears to have failed "
                    "(still on login page). Check credentials in Settings.")
            # Let the EM-COP session fully establish before navigating to the
            # dashboard — opening it too soon can land on forbidden.seam.
            settle = self.cfg["power"].get("post_login_seconds", 8)
            log.info("EM-COP login OK as '%s', now at %s — settling %ds",
                     username, url, settle)
            time.sleep(settle)
            return
        raise RuntimeError(
            f"EM-COP kept redirecting '{username}' to forbidden.seam after "
            f"{attempts} attempts — the account may lack dashboard access or "
            "be logged in elsewhere.")

    def ensure_session(self):
        if self.driver is None:
            log.info("Starting browser for power scraping...")
            self.driver = self._init_driver()
            self._login()
            # Remember the tab that holds the live EM-COP session. We leave it
            # parked on the post-login page and never navigate it away.
            self._session_handle = self.driver.current_window_handle
            self._dashboard_handle = None

    def _open_dashboard(self):
        """Load the power dashboard in a SECOND tab, keeping the EM-COP session
        tab parked. Reuses the dashboard tab across cycles; reopens it if the
        user/browser closed it."""
        handles = self.driver.window_handles
        if self._dashboard_handle and self._dashboard_handle in handles:
            self.driver.switch_to.window(self._dashboard_handle)
            self.driver.get(self.cfg["emcop"]["power_url"])  # refresh in-tab
        else:
            self.driver.switch_to.window(self._session_handle)
            self.driver.switch_to.new_window("tab")
            self._dashboard_handle = self.driver.current_window_handle
            self.driver.get(self.cfg["emcop"]["power_url"])

    def stop(self):
        with self._lock:
            if self.driver is not None:
                try:
                    self.driver.quit()
                except Exception:
                    pass
                self.driver = None
                self._session_handle = None
                self._dashboard_handle = None

    # --- scraping -----------------------------------------------------------
    def _dashboard_state(self):
        """'ready' if #total is populated, 'empty' if present but blank,
        'absent' if not in the current document."""
        elements = self.driver.find_elements(By.CSS_SELECTOR, "#total")
        if not elements:
            return "absent"
        return "ready" if elements[0].text.strip() else "empty"

    def _switch_to_dashboard(self, timeout=30):
        """Waits for the dashboard KPIs, checking iframes/frames too.

        The dashboard page is static HTML whose values are filled by AJAX,
        so we wait for content rather than sleeping a fixed time. On failure
        the DOM is dumped to power_debug.html and the error says whether the
        KPI element was missing entirely or just never populated.
        """
        driver = self.driver
        deadline = time.time() + timeout
        last_state = "absent"
        while time.time() < deadline:
            driver.switch_to.default_content()
            state = self._dashboard_state()
            if state == "ready":
                return
            last_state = state
            frames = (driver.find_elements(By.TAG_NAME, "iframe")
                      + driver.find_elements(By.TAG_NAME, "frame"))
            for frame in frames:
                driver.switch_to.default_content()
                try:
                    driver.switch_to.frame(frame)
                    state = self._dashboard_state()
                    if state == "ready":
                        return
                    if state == "empty":
                        last_state = "empty"
                except Exception:
                    continue
            time.sleep(1)
        driver.switch_to.default_content()

        from app.config import BASE_DIR
        debug_file = os.path.join(BASE_DIR, "power_debug.html")
        try:
            with open(debug_file, "w", encoding="utf-8") as f:
                f.write(driver.page_source)
        except OSError:
            debug_file = "(could not write debug file)"

        if last_state == "empty":
            detail = ("The dashboard page loaded but its numbers were never "
                      "filled in — the page's data feed may be failing in the "
                      "automated browser.")
        else:
            detail = ("The dashboard KPI elements are not in the page that the "
                      "automated browser receives.")
        raise DashboardNotFoundError(
            f"{detail} Browser is at '{driver.current_url}' "
            f"(title: '{driver.title}'). The page HTML as seen by the scraper "
            f"was saved to {debug_file} for comparison.")

    def _scrape_totals(self):
        data = {"timestamp": datetime.now().isoformat(sep=" ", timespec="seconds")}
        for field, selector in TOTALS_SELECTORS.items():
            try:
                text = self.driver.find_element(By.CSS_SELECTOR, selector).text
                data[field] = _safe_int(text)
            except Exception as e:
                log.warning("Could not read %s (%s): %s", field, selector, e)
                data[field] = None
        return data

    def _scrape_outage_table(self):
        records = []
        rows = self.driver.find_elements(By.CSS_SELECTOR, "#outagedashboard tbody tr")
        for row in rows:
            cols = row.find_elements(By.TAG_NAME, "td")
            if len(cols) < 6:
                continue
            spans = cols[0].find_elements(By.TAG_NAME, "span")
            if spans and spans[0].get_attribute("title"):
                raw_locations = spans[0].get_attribute("title")
            else:
                raw_locations = cols[0].text
            customers_off = _safe_int(cols[2].text) or 0
            outage_type = cols[4].text.strip()
            first_seen = parse_first_seen(cols[5].text)
            for loc in raw_locations.split(","):
                loc = loc.strip().replace("\xa0", " ")
                if loc:
                    records.append({
                        "location": loc,
                        "customers_off": customers_off,
                        "type": outage_type,
                        "first_seen": first_seen.isoformat(sep=" ", timespec="seconds"),
                    })
        return records

    # --- persistence --------------------------------------------------------
    @staticmethod
    def _update_tracker(current):
        """Sync the power_outages table with the currently visible outages."""
        now = datetime.now().isoformat(sep=" ", timespec="seconds")
        conn = database.get_connection()
        try:
            active = {
                row[1]: row[0]
                for row in conn.execute(
                    "SELECT id, location FROM power_outages WHERE restored = 0")
            }
            current_locations = set()
            for rec in current:
                current_locations.add(rec["location"])
                if rec["location"] in active:
                    conn.execute(
                        "UPDATE power_outages SET last_seen = ?, customers_off = ? WHERE id = ?",
                        [now, rec["customers_off"], active[rec["location"]]])
                else:
                    conn.execute(
                        "INSERT INTO power_outages "
                        "(location, customers_off, type, first_seen, last_seen, restored) "
                        "VALUES (?, ?, ?, ?, ?, 0)",
                        [rec["location"], rec["customers_off"], rec["type"],
                         rec["first_seen"], now])
            # anything previously active but no longer listed has been restored
            for location, row_id in active.items():
                if location not in current_locations:
                    conn.execute(
                        "UPDATE power_outages SET restored = 1, last_seen = ?, "
                        "duration_mins = ROUND((julianday(?) - julianday(first_seen)) * 1440) "
                        "WHERE id = ?",
                        [now, now, row_id])
            conn.commit()
        finally:
            conn.close()

    def _geocode_new_locations(self, records):
        limit = self.cfg["power"].get("max_new_geocodes_per_cycle", 10)
        cached = set(
            database.read_df("SELECT location FROM geocode_cache")["location"].tolist())
        new_locations = [r["location"] for r in records if r["location"] not in cached]
        if not new_locations:
            return
        if self._geolocator is None:
            from geopy.geocoders import Nominatim
            self._geolocator = Nominatim(user_agent="unified_monitor")
        for loc in dict.fromkeys(new_locations):  # de-dupe, keep order
            if limit <= 0:
                break
            lat = lon = None
            try:
                # geopy defaults to a 1s timeout, which Nominatim routinely
                # exceeds; give it room so lookups actually succeed.
                geo = self._geolocator.geocode(
                    f"{loc}, Victoria, Australia", timeout=10)
                if geo:
                    lat, lon = geo.latitude, geo.longitude
            except Exception as e:
                log.warning("Geocoding failed for %s: %s", loc, e)
            database.insert_rows(
                "geocode_cache",
                [{"location": loc, "latitude": lat, "longitude": lon}],
                ignore_duplicates=True)
            limit -= 1
            time.sleep(1)  # respect Nominatim rate limit

    # --- one collection cycle -------------------------------------------------
    def scrape_cycle(self):
        with self._lock:
            self.ensure_session()
            try:
                # Load the dashboard in its own tab; the EM-COP session tab
                # stays parked so authentication is not lost by navigating away.
                self._open_dashboard()
                if "forbidden.seam" in self.driver.current_url:
                    self.stop()
                    raise RuntimeError(
                        f"Redirected to forbidden.seam as "
                        f"'{self.cfg['emcop']['username']}' — session dropped, "
                        "will re-login on the next cycle.")
                self._switch_to_dashboard()
                totals = self._scrape_totals()
                outages = self._scrape_outage_table()
            except DashboardNotFoundError:
                # session is fine, the URL is wrong — keep it for next cycle
                raise
            except Exception:
                # session may have expired — drop it so next cycle re-logs-in
                self.stop()
                raise
        if all(totals.get(f) is None for f in TOTALS_SELECTORS):
            raise RuntimeError("Dashboard found but no totals could be read — "
                               "not storing an empty reading.")
        database.insert_rows("power_timeseries", [totals])
        self._update_tracker(outages)
        self._geocode_new_locations(outages)
        log.info("Power cycle complete: %s customers off, %d outage locations",
                 totals.get("customers_off"), len(outages))

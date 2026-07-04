"""EM-COP quick-launch: opens a visible Chrome window and logs in.

This replaces the old '#Passive Monitor.py' tkinter tool. Runs in a
background thread so the dashboard stays responsive; the browser window
is left open for the user.
"""
import logging
import threading
import time

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from webdriver_manager.chrome import ChromeDriverManager

log = logging.getLogger(__name__)

# Keep references so launched browsers aren't garbage-collected (and closed).
_drivers = []
_status = {"message": "", "busy": False}
_lock = threading.Lock()


def get_status():
    with _lock:
        return dict(_status)


def _set_status(message, busy):
    with _lock:
        _status["message"] = message
        _status["busy"] = busy


def _launch(cfg):
    emcop = cfg["emcop"]
    username = emcop["username"]
    try:
        _set_status(f"Launching browser (logging in as '{username}')...", True)
        options = Options()
        options.add_argument("--start-maximized")
        driver = webdriver.Chrome(
            service=Service(ChromeDriverManager().install()), options=options)
        _drivers.append(driver)

        for attempt in range(1, 4):
            log.info("EM-COP quick-launch login as '%s' (attempt %d/3)",
                     username, attempt)
            driver.get(emcop["login_url"])
            time.sleep(3)
            driver.find_element(By.ID, "nicsUsername").send_keys(username)
            driver.find_element(By.ID, "nicsPassword").send_keys(
                emcop["password"] + Keys.RETURN)
            time.sleep(5)
            if "forbidden.seam" not in driver.current_url:
                break
            # EM-COP sometimes bounces a fresh session here; back off and retry
            _set_status(f"'{username}' hit forbidden.seam — retrying in 10s "
                        f"(attempt {attempt}/3)...", True)
            time.sleep(10)
        else:
            _set_status(f"Launch failed: '{username}' kept landing on "
                        "forbidden.seam after 3 attempts.", False)
            return

        if emcop.get("after_login_url"):
            driver.execute_script(
                f"window.open('{emcop['after_login_url']}', '_blank');")
        _set_status(f"EM-COP opened and logged in as '{username}'.", False)
        log.info("EM-COP quick-launch complete as '%s'", username)
    except Exception as e:
        log.exception("EM-COP quick-launch failed")
        _set_status(f"Launch failed (user '{username}'): {e}", False)


def launch_emcop(cfg):
    """Starts the launch in a background thread. Returns immediately."""
    if get_status()["busy"]:
        return "A launch is already in progress."
    if not (cfg["emcop"]["username"] and cfg["emcop"]["password"]):
        return "EM-COP credentials are not set. Add them on the Settings page first."
    threading.Thread(target=_launch, args=(cfg,), daemon=True).start()
    return "Launching EM-COP in a new browser window..."

"""EM-COP quick-launch: opens a visible Chrome window and logs in.

This replaces the old '#Passive Monitor.py' tkinter tool. Runs in a
background thread so the dashboard stays responsive; the browser window
is left open for the user.

It is a desktop-build feature: the window opens on whatever machine runs the
app, so on the server it lands inside Xvfb where nobody can see it. That is
worth saying out loud in the status rather than reporting success, and it is
why Chrome is started through app.chrome — as root in the container the plain
options would not start a browser at all.
"""
import logging
import os
import threading
import time

from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys

from app import chrome

log = logging.getLogger(__name__)

# Keep references so launched browsers aren't garbage-collected (and closed).
# Each entry is (driver, profile_dir) — the profile is this browser's alone.
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


def _on_desktop():
    """True in the desktop build, where the launched window appears in front of
    the person who clicked. Set by run_desktop.py."""
    return os.environ.get("UM_DESKTOP") == "1"


def _prune_drivers():
    """Forget browsers the user has closed, releasing their profile dirs.
    Without this, every click leaks a Chrome for the life of the process."""
    alive = []
    for driver, profile_dir in _drivers:
        try:
            if driver.window_handles:
                alive.append((driver, profile_dir))
                continue
        except Exception:
            pass    # window gone, or the browser died — either way, clean up
        try:
            driver.quit()
        except Exception:
            pass
        chrome.release(profile_dir)
    _drivers[:] = alive


def _launch(cfg):
    emcop = cfg["emcop"]
    username = emcop["username"]
    try:
        _prune_drivers()
        if not chrome.has_display():
            _set_status(
                "Cannot launch: this host has no display for a browser window "
                "(no DISPLAY — on the server that means Xvfb is not running). "
                "Open EM-COP in your own browser instead.", False)
            return
        _set_status(f"Launching browser (logging in as '{username}')...", True)
        options = Options()
        options.add_argument("--start-maximized")
        driver, profile_dir = chrome.start(options, "um-emcop-profile-")
        _drivers.append((driver, profile_dir))

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
        if _on_desktop():
            _set_status(f"EM-COP opened and logged in as '{username}'.", False)
        else:
            # Saying "opened" here would send someone hunting for a window that
            # only exists inside the server's virtual display.
            _set_status(
                f"Logged in as '{username}' in a browser ON THE SERVER — there "
                "is no window on your machine to see. Use this only to check "
                "the credentials work.", False)
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

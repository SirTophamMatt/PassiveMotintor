"""Shared Chrome/ChromeDriver startup.

The power scraper and the EM-COP quick-launch both drive a real Chrome, and
both have to survive the server deployment (Docker, root, no seat) as well as
the desktop build. The parts that actually break — the throwaway profile, the
sandbox switch, the verbose driver log — live here so the two cannot drift.

Background: "session not created: Chrome instance exited" means Chrome died
before chromedriver could attach, and the reason is never in the Selenium
traceback. See the 2026-09-20 section in CLAUDE.md.
"""
import logging
import os
import shutil
import tempfile

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager

from app import config

log = logging.getLogger(__name__)

CHROMEDRIVER_LOG = os.path.join(config.BASE_DIR, "chromedriver.log")
CHROMEDRIVER_LOG_MAX_BYTES = 2_000_000


def has_display():
    """Whether a visible Chrome has somewhere to draw. Windows always does; on
    Linux it means Xvfb (the container) or a real desktop session."""
    return os.name != "posix" or bool(os.environ.get("DISPLAY"))


def _is_root():
    """Chrome refuses to start its sandbox as root, which is exactly how it
    runs in the container. Desktop Linux is not root and keeps the sandbox."""
    return os.name == "posix" and hasattr(os, "geteuid") and os.geteuid() == 0


def trim_log():
    """Selenium appends to the log, so a cycle-per-minute scraper would grow it
    without bound. Only the most recent startup matters for diagnosis."""
    try:
        if os.path.getsize(CHROMEDRIVER_LOG) > CHROMEDRIVER_LOG_MAX_BYTES:
            os.remove(CHROMEDRIVER_LOG)
    except OSError:
        pass


def log_tail(lines=3):
    """The last few chromedriver lines, for pasting into a status message —
    the actual reason Chrome exited is there, not in the Selenium exception."""
    try:
        with open(CHROMEDRIVER_LOG, encoding="utf-8", errors="replace") as f:
            tail = [ln.strip() for ln in f.readlines()[-lines:] if ln.strip()]
    except OSError:
        tail = []
    if not tail:
        return f"(no chromedriver log at {CHROMEDRIVER_LOG})"
    return f"ChromeDriver log ({CHROMEDRIVER_LOG}) ends: " + " | ".join(tail)


def start(options, profile_prefix="um-chrome-profile-"):
    """Start Chrome on `options` and return (driver, profile_dir).

    The caller owns the profile directory: pass it to release() once the driver
    has quit. On failure this raises RuntimeError carrying the chromedriver log
    tail, so the reason reaches the UI instead of only 'Chrome instance exited'.
    """
    if _is_root():
        # Only where it is forced. Leaving the sandbox on where it can run is
        # worth more than one consistent argument list.
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
    # Each session gets its own throwaway profile. On the shared default
    # profile, a Chrome left behind by a failed run still holds its
    # SingletonLock and the next Chrome exits on startup until something
    # clears it by hand.
    profile_dir = tempfile.mkdtemp(prefix=profile_prefix)
    options.add_argument(f"--user-data-dir={profile_dir}")
    trim_log()
    service = Service(ChromeDriverManager().install(),
                      service_args=["--verbose"],
                      log_output=CHROMEDRIVER_LOG)
    try:
        driver = webdriver.Chrome(service=service, options=options)
    except Exception as e:
        release(profile_dir)
        raise RuntimeError(f"{e} {log_tail()}") from e
    return driver, profile_dir


def release(profile_dir):
    """Delete a throwaway profile. Skipping it leaks a few MB per run onto the
    data volume."""
    if profile_dir:
        shutil.rmtree(profile_dir, ignore_errors=True)

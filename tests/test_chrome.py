"""Chrome startup: the parts that made the server fail silently.

No real browser is started here — these cover the guards around it, which are
what decide whether a failure is diagnosable or just "session not created:
Chrome instance exited".
"""
import os

import pytest

from app import chrome
from app.modules.emcop import launcher


# ------------------------------------------------------------------ display --
def test_windows_always_has_a_display(monkeypatch):
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.delenv("DISPLAY", raising=False)
    assert chrome.has_display()


def test_posix_without_display_has_nowhere_to_draw(monkeypatch):
    monkeypatch.setattr(os, "name", "posix")
    monkeypatch.delenv("DISPLAY", raising=False)
    assert not chrome.has_display()

    monkeypatch.setenv("DISPLAY", ":99")
    assert chrome.has_display()


# --------------------------------------------------------------- driver log --
def test_log_tail_names_the_file_when_there_is_none(monkeypatch, tmp_path):
    monkeypatch.setattr(chrome, "CHROMEDRIVER_LOG", str(tmp_path / "nope.log"))
    assert "nope.log" in chrome.log_tail()


def test_log_tail_returns_the_last_lines(monkeypatch, tmp_path):
    path = tmp_path / "chromedriver.log"
    path.write_text("first\nsecond\nDevToolsActivePort file doesn't exist\n")
    monkeypatch.setattr(chrome, "CHROMEDRIVER_LOG", str(path))
    tail = chrome.log_tail(lines=2)
    assert "DevToolsActivePort" in tail
    assert "first" not in tail


def test_oversized_log_is_dropped_not_grown(monkeypatch, tmp_path):
    path = tmp_path / "chromedriver.log"
    path.write_text("x" * 50)
    monkeypatch.setattr(chrome, "CHROMEDRIVER_LOG", str(path))
    monkeypatch.setattr(chrome, "CHROMEDRIVER_LOG_MAX_BYTES", 10)
    chrome.trim_log()
    assert not path.exists()


# ------------------------------------------------------------------ start() --
class FakeOptions:
    def __init__(self):
        self.arguments = []

    def add_argument(self, arg):
        self.arguments.append(arg)


class FakeManager:
    def install(self):
        return "chromedriver"


@pytest.fixture
def stub_chromedriver(monkeypatch, tmp_path):
    """Everything up to the browser itself, so start() can be exercised
    without a Chrome on the machine running the suite."""
    monkeypatch.setattr(chrome, "CHROMEDRIVER_LOG", str(tmp_path / "cd.log"))
    monkeypatch.setattr(chrome, "ChromeDriverManager", lambda *a, **k: FakeManager())
    monkeypatch.setattr(chrome, "Service", lambda *a, **k: object())


def test_each_run_gets_its_own_profile(monkeypatch, stub_chromedriver):
    monkeypatch.setattr(chrome.webdriver, "Chrome",
                        lambda service, options: "driver")
    options = FakeOptions()
    driver, profile_dir = chrome.start(options, "um-test-profile-")
    assert driver == "driver"
    assert f"--user-data-dir={profile_dir}" in options.arguments
    assert os.path.isdir(profile_dir)

    chrome.release(profile_dir)
    assert not os.path.exists(profile_dir)


def test_a_failed_start_explains_itself_and_cleans_up(monkeypatch, stub_chromedriver):
    def boom(service, options):
        raise Exception("session not created: Chrome instance exited.")

    monkeypatch.setattr(chrome.webdriver, "Chrome", boom)
    made = []
    real_mkdtemp = chrome.tempfile.mkdtemp
    monkeypatch.setattr(chrome.tempfile, "mkdtemp",
                        lambda **kw: made.append(real_mkdtemp(**kw)) or made[-1])

    with pytest.raises(RuntimeError) as excinfo:
        chrome.start(FakeOptions(), "um-test-profile-")

    # The bare Selenium message is the useless half; the log pointer is the fix.
    assert "Chrome instance exited" in str(excinfo.value)
    assert "chromedriver log" in str(excinfo.value).lower()
    assert not os.path.exists(made[0])


# ----------------------------------------------------------------- launcher --
class FakeDriver:
    def __init__(self, handles):
        self._handles = handles
        self.quit_called = False

    @property
    def window_handles(self):
        if self._handles is None:
            raise Exception("no such session")
        return self._handles

    def quit(self):
        self.quit_called = True


def test_closed_browsers_are_not_kept_forever(monkeypatch, tmp_path):
    open_profile = tmp_path / "open"
    closed_profile = tmp_path / "closed"
    open_profile.mkdir()
    closed_profile.mkdir()
    still_open = FakeDriver(["window-1"])
    closed = FakeDriver(None)
    monkeypatch.setattr(launcher, "_drivers",
                        [(still_open, str(open_profile)),
                         (closed, str(closed_profile))])

    launcher._prune_drivers()

    assert launcher._drivers == [(still_open, str(open_profile))]
    assert closed.quit_called
    assert not still_open.quit_called
    assert open_profile.exists()
    assert not closed_profile.exists()


def test_launch_refuses_rather_than_dying_with_no_display(monkeypatch):
    monkeypatch.setattr(launcher, "_drivers", [])
    monkeypatch.setattr(chrome, "has_display", lambda: False)
    monkeypatch.setattr(chrome, "start", lambda *a, **k: pytest.fail(
        "should not reach Chrome with no display"))

    launcher._launch({"emcop": {"username": "someone", "password": "pw"}})

    status = launcher.get_status()
    assert "DISPLAY" in status["message"]
    assert not status["busy"]

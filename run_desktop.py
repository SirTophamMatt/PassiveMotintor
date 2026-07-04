"""Desktop entry point: runs the same app inside a native window.

The Dash server runs on localhost in a background thread and a pywebview
window points at it — one codebase for both desktop and web. The window is
frameless with a custom dark title bar (see factory._titlebar); window
controls below call back into pywebview.
"""
import os

# Must be set before importing app.factory, which reads it at import time to
# decide whether to render the custom title bar.
os.environ["UM_DESKTOP"] = "1"

import logging
import socket
import threading
import time

import webview
from waitress import serve

from app.factory import create_app

HOST = "127.0.0.1"
PORT = 8091


# The window is held at module level, NOT on the js_api object below. pywebview
# serializes the exposed api object's state to JavaScript; a window reference
# there drags in the native .NET window whose Rectangle.Empty self-references,
# causing infinite recursion ("maximum recursion depth exceeded") on some
# WebView2/accessibility setups. Keeping it module-level avoids that entirely.
_window = None


class WindowControls:
    """Exposed to JS as pywebview.api.* for the custom title-bar buttons.
    Holds no window reference (see note above) — only a plain bool."""

    def __init__(self):
        self._maximized = False

    def minimize(self):
        if _window:
            _window.minimize()

    def toggle_maximize(self):
        if not _window:
            return
        if self._maximized:
            _window.restore()
        else:
            _window.maximize()
        self._maximized = not self._maximized

    def close(self):
        if _window:
            _window.destroy()


def _wait_for_server(timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((HOST, PORT), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.2)
    return False


def main():
    app = create_app()
    threading.Thread(
        target=lambda: serve(app.server, host=HOST, port=PORT, threads=8),
        daemon=True).start()
    if not _wait_for_server():
        raise SystemExit("Server failed to start — see unified_monitor.log")

    global _window
    controls = WindowControls()
    _window = webview.create_window(
        "Passive Monitor", f"http://{HOST}:{PORT}",
        width=1480, height=950, min_size=(1100, 700),
        frameless=True,            # hide the native title bar / border
        easy_drag=False,           # drag only via the title bar's drag region
        background_color="#0f141b",  # matches the dark theme (no white flash)
        js_api=controls)
    try:
        webview.start()
    except Exception:
        logging.getLogger("desktop").critical(
            "pywebview window crashed", exc_info=True)
        raise


if __name__ == "__main__":
    main()

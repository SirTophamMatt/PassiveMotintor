"""assets/graph_calm.js is browser-only; these pin the parts a refactor could
silently drop (it was verified in a real browser when written)."""
import os

JS = open(os.path.join(os.path.dirname(__file__), "..", "assets", "graph_calm.js"),
          encoding="utf-8").read()


def test_wraps_plotly_react_once_and_early():
    assert "Plotly.react = calm" in JS and "__wdCalm" in JS
    # Installed when window.Plotly is ASSIGNED, not by polling after the first
    # draw — an unwrapped first draw has no signature to compare against.
    assert 'Object.defineProperty(window, "Plotly"' in JS


def test_skips_identical_and_defers_while_hidden():
    assert "sig === gd.__wdSig" in JS
    assert "document.hidden" in JS and "visibilitychange" in JS
    assert "__wdPending" in JS


def test_first_draw_always_goes_through():
    # dcc.Graph binds its event handlers after the first draw; skipping it
    # would leave a blank, dead graph.
    assert "!gd._fullLayout" in JS

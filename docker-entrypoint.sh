#!/bin/sh
# Container entrypoint: hold an X display open for the power scraper's visible
# Chrome, then run the web app in the foreground.
#
# This used to be an inline CMD that backgrounded Xvfb once. Two ways that bit:
# a container restart reuses the writable layer, so Xvfb's lock file from the
# previous boot made it refuse to start; and nothing noticed if Xvfb later
# died. Either way Chrome had no display, exited on startup, and every power
# cycle failed with 'session not created: Chrome instance exited'.
set -e

DISPLAY_NUM=99
export DISPLAY=":${DISPLAY_NUM}"
LOCK="/tmp/.X${DISPLAY_NUM}-lock"
SOCK="/tmp/.X11-unix/X${DISPLAY_NUM}"

# Supervisor: clear any stale lock, start Xvfb, and start it again if it dies.
# 'wait' also reaps it, so a crashed Xvfb does not linger as a zombie.
(
  while true; do
    rm -f "$LOCK" "$SOCK"
    Xvfb "$DISPLAY" -screen 0 1920x1080x24 -nolisten tcp &
    wait $! || true
    echo "entrypoint: Xvfb exited, restarting in 2s" >&2
    sleep 2
  done
) &

# Give the display a moment to come up before the app's collectors start; the
# scraper reports a missing DISPLAY clearly, but there is no reason to make it.
i=0
while [ ! -e "$SOCK" ] && [ "$i" -lt 50 ]; do
  sleep 0.2
  i=$((i + 1))
done
[ -e "$SOCK" ] || echo "entrypoint: WARNING - X display $DISPLAY never came up" >&2

exec python run_web.py --host 0.0.0.0 --port 8050

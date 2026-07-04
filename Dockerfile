# Passive Monitor web server.
#
# Includes Google Chrome + Xvfb because the power scraper drives a *visible*
# Chrome (EM-COP drops headless sessions). Flood collection is pure requests and
# needs none of that, so if you only run flood you can strip the chrome/xvfb
# lines for a much smaller image.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    UM_DATA_DIR=/data

# System deps: chrome for the power scraper, xvfb for a virtual display,
# curl for the container healthcheck, fonts so rendered charts/PDFs look right.
RUN apt-get update && apt-get install -y --no-install-recommends \
        wget gnupg curl xvfb xauth \
        fonts-liberation fonts-dejavu-core \
        libnss3 libxss1 libasound2 libgbm1 libgtk-3-0 \
    && wget -q -O /tmp/chrome.deb \
        https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb \
    && apt-get install -y --no-install-recommends /tmp/chrome.deb \
    && rm -f /tmp/chrome.deb \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# Writable state (db, config, log, backups) lives on a mounted volume.
RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8050

# Xvfb gives the power scraper's Chrome a display to attach to. Started
# directly in the background (NOT via xvfb-run, whose wrapper script has been
# seen hanging without ever launching the app); flood-only deployments can
# drop Xvfb and run `python run_web.py ...` directly.
CMD ["/bin/sh", "-c", "Xvfb :99 -screen 0 1920x1080x24 -nolisten tcp & export DISPLAY=:99; exec python run_web.py --host 0.0.0.0 --port 8050"]

# Passive Monitor — Unified Dashboard

A single dashboard combining the previously separate **Flood Monitor**,
**Power Outage Dashboard**, and **EM-COP quick-launch** tools. One codebase
runs both as a **standalone desktop app** and as a **web app**.

## Setup

```powershell
cd unified_monitor
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

The power scraper and EM-COP quick-launch need Google Chrome installed
(the matching driver is downloaded automatically).

## Running

| Mode | Command | Notes |
|------|---------|-------|
| Desktop | `python run_desktop.py` | Native window, server stays on localhost only |
| Web | `python run_web.py` | Serves on `0.0.0.0:8050` for other machines; use `--host`/`--port` to change |

First run:
1. Open **Settings** and enter your EM-COP credentials (saved to a local,
   gitignored `config.json` — never hardcoded, never committed).
2. Optionally open **Import Data** to pull in history from the old projects
   (flood DB/CSVs, flood levels spreadsheet, power CSVs, geocode cache).
   The defaults point at the old folders; imports are safe to re-run.

## Pages

- **Overview** — combined KPIs (customers off, alert level, stations flooding),
  collector status, and the EM-COP quick-launch button.
- **Flood Monitor** — start/stop BoM gauge collection per event, latest-reading
  table, per-station height graphs with minor/moderate/major lines, flooding-only
  filter. Duplicate readings are de-duplicated at insert time.
- **Power Outages** — start/stop EM-COP scraping (headless Chrome), KPIs,
  trend graphs (1h/6h/24h/week), outage map by duration, duration bar chart.
- **Import Data** — one-click legacy imports.
- **Settings** — credentials, URLs, intervals, alert thresholds.

## Architecture

```
run_desktop.py / run_web.py     entry points (pywebview shell vs waitress server)
app/factory.py                  Dash app, routing, theming
app/collector.py                background collection threads (independent of UI)
app/database.py                 single SQLite DB (unified_monitor.db, WAL mode)
app/config.py                   config.json load/save
app/importer.py                 legacy data import
app/modules/flood/              BoM scraper + queries
app/modules/power/              EM-COP Selenium scraper + outage tracker + geocoding
app/modules/emcop/              visible-browser quick-launch
app/pages/                      one file per dashboard page
assets/style.css                light/dark theme
```

Key differences from the old projects:

- **No hardcoded credentials** — everything sensitive lives in `config.json`.
- **One SQLite database** instead of scattered CSVs; flood readings are
  de-duplicated (the old CSVs grew to 12&nbsp;MB+ from repeated rows).
- **Collection runs in background threads**, not inside page-render callbacks,
  so the UI stays responsive and closing the browser tab doesn't stop collection.
- Geocode cache moved from JSON into the database.
- Logging to `unified_monitor.log` instead of scattered prints.

## Building the desktop .exe

```powershell
pip install pyinstaller
.\build_exe.ps1            # or: python -m PyInstaller PassiveMonitor.spec --noconfirm
```

Output: **`dist\PassiveMonitor\PassiveMonitor.exe`** — double-click to launch, no
command line needed. The whole `dist\PassiveMonitor\` folder is the app (keep it
together); it runs without Python installed (Chrome is still required for the
scrapers).

Notes:
- The build is configured by `PassiveMonitor.spec` (collects Dash/Plotly/pywebview
  data files and bundles `assets/`). Edit the spec, not the command line.
- When frozen, `config.json`, `unified_monitor.db`, and `unified_monitor.log` are
  written **next to the .exe** (in `dist\PassiveMonitor\`), so settings and data
  persist there.
- First launch may trigger SmartScreen ("unknown publisher") since the exe is
  unsigned — choose *More info → Run anyway*. A locked-down SOE may block unsigned
  exes outright; in that case run from Python instead.

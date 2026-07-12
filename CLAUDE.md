# Unified Monitor

One dashboard combining **Flood Monitor** (BoM Victorian river-gauge scraping),
**Power Outages** (EM-COP outage scraping), and **EM-COP quick-launch**. Single codebase
runs as both a desktop window and a web server.

## Stack
Python 3.11 · Dash 2.17 / Plotly · pandas · Selenium + webdriver-manager · BeautifulSoup/lxml ·
pywebview (desktop) · waitress (web) · SQLite (WAL). Deps in `requirements.txt`. Needs Google
Chrome installed for the power scraper / EM-COP launch (chromedriver auto-managed via `~/.wdm`).

## Run
- Web: `python run_web.py [--host H --port P]` (waitress)
- Desktop: `python run_desktop.py` (pywebview window over a localhost server)
- Setup: `python -m venv .venv` → activate → `pip install -r requirements.txt`
- Build .exe: `.\build_exe.ps1` (uses `PassiveMonitor.spec`) → `dist\PassiveMonitor\PassiveMonitor.exe`.
  When frozen, config/db/log are written next to the .exe.

## Layout
- `run_web.py` / `run_desktop.py` — entry points
- `app/factory.py` — Dash app, routing, theme
- `app/collector.py` — background collection threads (run independent of the UI)
- `app/database.py` — single SQLite `unified_monitor.db` (WAL)
- `app/config.py` — `config.json` load/save
- `app/importer.py` + `app/pages/importer_page.py` — legacy data import
- `app/modules/{flood,power,emcop}/` — `scraper.py` + `data.py` per module
- `app/pages/` — one file per page (overview, flood, power, importer_page, settings)
- `assets/style.css` — light/dark theme

## Conventions
- **Credentials** (EM-COP user/pass) live only in `config.json`, written via the Settings page.
  Never hardcode secrets; `config.json` is gitignored.
- **Code → git; data → local.** `.gitignore` excludes `config.json`, `*.db*`, `*.csv`, `*.log`,
  `backups/`. The DB regenerates on first run; legacy history loads via the Import Data page.
- **Never delete `unified_monitor.db`** — it holds all the user's collected events (flood data is
  namespaced by `event`, all events persist and stay selectable). When cleaning up test artifacts
  only remove `*.db-wal`, `*.db-shm`, `*.log` — never the `.db`. On startup `init_db` snapshots the
  DB to `backups/` (keeps last 15) as insurance.
- **One SQLite DB** for all modules. Flood readings are stamped with their **true BoM
  observation time** (parsed from the summary table; backfill uses the history page's full
  datetime) and **de-duped** on (event, station, timestamp, height) — re-scraping the same
  reading adds nothing. A `flood_heartbeat` row is written every cycle (continuity proof).
- **Flood outlier guard:** each scrape drops a reading that jumps more than
  `MAX_HEIGHT_JUMP_M` (50 m) from a station's last known good height, or exceeds the
  `MAX_PLAUSIBLE_HEIGHT_M` (900 m) ceiling when the station has no history — this kills BoM
  garbage spikes (a 2 m gauge briefly reporting ~1000 m) before storage/graphs. The check is
  RELATIVE, so datum-referenced reservoir gauges (steadily hundreds of m) are never touched.
  See `_reject_spikes` in `modules/flood/scraper.py`.
- **Flood near-flood backfill:** when a station is at/within 90% of its minor level, its
  per-station BoM history page (`.tbl.shtml`) is fetched once so graphs show a trend
  immediately. Backfilled stations are tracked in-process to avoid re-fetching.
- **Collection runs in background threads**, not in render callbacks — closing the UI tab does
  not stop collection.
- Power scraper runs a **visible** Chrome with automation-fingerprint suppression (EM-COP
  detects headless and drops the session). `power.headless` in config can re-enable headless
  if that ever changes. It keeps the EM-COP login tab parked and loads the outage dashboard
  in a **second tab**, because navigating the session tab away from EM-COP drops auth.
- BoM flood data is public (no auth). Melbourne timezone for power timestamps.

## Current status
Both modules work. The power `forbidden.seam` blocker was resolved in mid-2026 — power
scraping populates KPIs again. Power occasionally needs a human to sign into EM-COP and
acknowledge system messages; this is handled **out-of-band** in a separate browser (the
scraper's own Chrome runs headless under Xvfb on the server, with no in-tool interaction).
If a session drops, the scraper re-logs-in on the next cycle.

## Web deployment (built mid-2026 — see DEPLOY.md / unified-monitor-web-deployment memory)
- **Always-on collection.** `run_web.py` → `create_app(autostart=True)` → `collector.autostart()`
  starts flood and power on boot (config `flood.autostart` / `power.autostart`, both default true;
  power log-and-skips until EM-COP creds are set). Flood collects continuously under the fixed
  `LIVE_EVENT="live"` bucket — no per-event collection any more.
- **Public dashboards + admin login.** Overview / Flood / Power are public read-only; Start/Stop,
  Settings, Import, tags and export live on `/admin` behind an admin password (`app/auth.py`,
  env `UM_ADMIN_PASSWORD` or config hash; `UM_SECRET_KEY` for the session cookie). Desktop build
  is implicitly admin.
- **Event tags = date ranges** (`event_tags` table, `app/tags.py`): a tag (name + start + end) slices
  flood+power by timestamp for viewing and export; old named events auto-migrate to tags on first
  start. **Export** (`app/export.py`) → one XLSX per tag/range. **Overview briefing PDF**
  (`app/reporting.py`, kaleido+reportlab).
- **Hosting:** Dockerfile (Chrome+Xvfb, `xvfb-run python run_web.py`) + docker-compose (app+Caddy,
  `./data:/data` volume) + Caddyfile (auto-HTTPS). `UM_DATA_DIR` points writable state at the volume.
  `/health` returns JSON + 200/503 for uptime monitors.

## Watchdog + notifications (built 2026-07-04)
- `app/watchdog.py` — `supervisor` daemon thread (started with web autostart): every 60s
  restarts stalled/dead collectors (rate-limited 4/hr each; respects the admin's explicit
  stop via `manager.flood_wanted()/power_wanted()`; auto-starts power once creds appear)
  and sends alerts on state CHANGES only: customers-off crossing low/high thresholds (+
  recovery), stations entering/escalating/clearing flood levels, new collector errors,
  watchdog restarts. State is in-process; `/health` exposes a `watchdog` block.
- `app/notify.py` — webhook sender (Slack/Teams `{"text"}`, Discord `{"content"}` auto-detected).
  Config `notify.webhook_url` + per-kind toggles, set on the Settings page; test button on Admin.
- Flood levels seed (`seed/Flood Levels.xlsx`) reloads on EVERY boot (source of truth);
  admin can re-import manually from the Import page.

## Station detail pages + LFG impacts (built 2026-07-05)
- **Gauge pages:** every gauge is clickable (flood-page graph cards + table station names,
  overview flooding cards) → `/flood/station/<station_key>` (`app/pages/station.py`).
  Page shows a **linear flood-gauge stick** (Plotly: class bands, current water level,
  hoverable impact markers), the station history graph (7/30/90d/all), severity-coloured
  **watch points & expected impacts** table (rows the water has reached are flagged), and a
  **Gauge Briefing PDF** button (`reporting.build_station_pdf`).
- **LFG impact data:** `seed/lfg_impacts.json` — height→impact rows extracted from the
  VICSES Local Flood Guide PDFs in `../LFG/` (86 guide tables, 77 BoM stations, ~540 rows).
  Reloaded into the `gauge_impacts` table on every boot (same policy as flood levels).
  `seed/lfg_extract_tool.py` is the one-off extraction script — re-run it if the LFG folder
  gets new guides (it contains hand-checked per-file overrides; unmatched/scanned guides are
  documented there). Guides with no BoM gauge (urban flash-flood LFGs) are intentionally absent.

## Fire / Incidents module (built 2026-07-12)
- **Source:** the public VicEmergency GeoJSON feed (`emergency.vic.gov.au/public/osom-geojson.json`,
  no auth, served **gzip** — decompress the raw bytes). All live incidents + community warnings
  state-wide. `app/modules/fire/{scraper,data}.py`, page `app/pages/fire.py`, route `/fire`.
- **Model:** upsert each feature into `fire_incidents` on its stable feed `id`; an event that
  drops out of the feed (or goes Safe/Complete) is marked `resolved=1`, never deleted. A per-cycle
  `fire_timeseries` row holds KPI counts + doubles as the heartbeat. Planned-burn boundary polygons
  (`feedType == "burn-area"`, ~60/cycle) are **skipped** — static plan data, not events. All other
  categories are kept; the page presents fire first via the category filter.
- **Feed quirks handled:** warning level lives in `category1` (Advice / Watch and Act / Emergency
  Warning); `cap.event`/`cap.severity` carry the hazard + severity; geometry is a mix of
  Point/Polygon/GeometryCollection (marker uses a vertex **centroid**); datetimes mix `Z` and
  `+10:00` (normalised to one local-time seconds string — same pandas-NaT lesson as flood);
  `sizeFmt` is sometimes a **list** (`['63 ha']`) so free-text fields are scalar-coerced before
  binding.
- **Wiring:** always-on collector (`fire.interval_minutes`=3, `fire.autostart`=true), watchdog
  supervision + `fire_alert` notifications (new/escalated warnings and new fires in
  `fire_alerts.alert_categories`, default `["Fire"]`; `notify.on_fire_alert`), `/health` exposes
  `fire_running`/`fire_last_heartbeat`/`fire_last_error`, Overview gets Active-Fires /
  Emergency-&-Watch-Act KPIs + a fire map + collector line.

## Fire module — polygon rendering + burn scars (built 2026-07-12)
- **Geometry stored + rendered.** `fire_incidents.geometry` holds the raw GeoJSON (non-Point
  geometries only; points stay as centroid markers). `_ensure_column` migration adds the column
  to pre-existing DBs. `fire.py:_map_figure` draws filled polygon overlays per kind via
  `mapbox_layers` (magic-underscore, so map style/zoom are preserved), markers on top.
- **Burn areas** (`feedType == "burn-area"`, sourceOrg VIC/DELWP): confirmed to be **historical
  DELWP burn-area footprints** (named, past-dated, big polygons — ~60/cycle), NOT planned-burn
  *incidents*. Now stored (no longer skipped) and excluded from live counts/table/alerts
  (`active_incidents`/`latest_counts` filter `feed_type != 'burn-area'`); surfaced only via
  `data.burn_areas()` and the Fire page **"Show burn areas (historical)"** toggle (default off).

## Fire module — remaining refinements (ops feedback 2026-07-12)
1. **Separate warning types from incident types.** Advice, Community Information,
   Watch and Act, Emergency, Evacuation are *warning* levels (`feedType == "warning"`,
   level in `category1`) and should be grouped/filtered apart from *incident* categories
   (Fire, Hazmat, Tree Down, ...). Reflect this split in the map legend, filters and KPIs.
2. **Per-kind map layer toggles** — extend the single burn-areas toggle to a checkbox per kind
   (each warning level, fire, other incident) to show/hide that layer.
3. **VicEmergency iconography** — prefer the **Australian Warning System (AWS)** standard
   symbols (published by AFAC / state emergency services, intended for public reuse) mapped to
   Advice/Watch&Act/Emergency, rather than lifting VicEmergency's own branded sprite assets
   (copyright). Use as marker symbols on the map + legend.

## Backlog (not started)
Full flood+power PDF *sitrep* (beyond the Overview snapshot) · flood map view (needs gauge
lat/longs — BoM KiWIS `getStationList` likely has them; email to BoM drafted 2026-07-04) ·
event timeline/compare · BoM forecast overlay · data retention/archive · deploy pipeline
(GitHub Action + Watchtower) · in-browser file upload on Import page · auto-tagging of events ·
viewer roles + audit log · log rotation/capped backups · power-dependent-customer 24h focus.

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
- Tests: `pip install -r requirements-dev.txt` → `python -m pytest` (from `unified_monitor/`).
  Tests redirect `UM_DATA_DIR` to a temp dir in `tests/conftest.py` **at import time**, so a run
  can never touch the real `unified_monitor.db`.
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
- `app/briefing.py` — operational briefing model (UI-free; the `/briefing` page and the briefing
  PDF both render from it)
- `app/history.py` — generic entity state-change journal (what makes Event Replay possible)
- `app/replay.py` — historical reconstruction for `/replay` (UI-free)
- `app/pages/` — one file per page (overview, flood, power, importer_page, settings)
- `assets/style.css` — light/dark theme
- `tests/` — pytest suite + HTML fixtures (`tests/fixtures/`); not shipped in the Docker image

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
  start. Tags are **editable after the fact** on Admin (`tags.update_tag` / `end_tag_now`) — rename,
  move the dates, clear the end date to reopen as ongoing, or **End now** to close an event that was
  started while it was already running. The edit form carries optional `HH:MM` time boxes because
  a date on its own normalises to whole-day bounds, which would otherwise round an exact end time
  out to 23:59:59 on the next save. **Export** (`app/export.py`) → one XLSX per tag/range. **Overview briefing PDF**
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
  Emergency-Warnings / Watch-&-Act KPIs + a fire map + collector line. The warning levels are
  **never summed into one card** (2026-08-10) — the two levels ask for different actions, so a
  combined figure lets either one over-represent the other.

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

## Fire module — polish pass (built 2026-07-12)
- **Warning-polygon render fix.** VicEmergency wraps a warning's area in a `GeometryCollection`
  (Point + Polygon); Mapbox GL can't fill a GeometryCollection, so `fire.py:_polygons` flattens
  each geometry to its Polygon/MultiPolygon parts before building the fill layer. Warning areas
  now render.
- **Warning vs incident separation.** `_kind` resolves every `feed_type == "warning"` row to one
  of the three AWS levels (via `_WARNING_ALIASES`: Evacuation→Emergency, Community
  Information→Advice); incidents are Fire or Other. Map `category_orders` + KPIs keep warnings
  and incidents grouped apart.
- **Per-kind map layer toggles.** The Filters panel's "Map layers" checklist toggles each kind
  (Emergency / Watch & Act / Advice / Fire / Other incidents) plus historical burn areas on the
  map; the table always lists the full category-filtered set. Default = all but burn areas.
- **AWS-styled legend.** `_aws_legend()` renders a licence-clean key using AWS colours + the
  warning-triangle motif (Plotly's own legend is hidden). NOTE: true VicEmergency sprite icons
  on the map markers need a **Mapbox access token** (open-street-map tiles only support circle
  markers) — deferred; the AWS colour/shape key is the token-free stand-in.

## Weather module — BoM warnings (Phase 2a, built 2026-07-12)
- **Source:** `api.weather.bom.gov.au/v1` (public JSON behind the BoM website; no auth,
  undocumented so parse defensively). `app/modules/weather/{scraper,data}.py`, page
  `app/pages/weather.py`, route `/weather`. Uses `urllib` (not requests) — same as fire.
- **Warnings:** `GET /warnings`, filtered to `state == "VIC"`, upserted into `weather_warnings`
  on the BoM `id`; a warning no longer live (or past expiry) is marked `active=0`, kept.
  `warning_group_type` (major/moderate/minor/severe) drives severity colour + KPIs. Timestamps
  normalised to one local-seconds format (pandas-NaT lesson). Endpoints proven: `/warnings`,
  `/locations?search=`, `/locations/{geohash6}/observations` (`rain_since_9am` mm),
  `/locations/{geohash}/forecasts/daily` (rain amount/chance).
- **Wiring:** always-on collector (`weather.interval_minutes`=10, `weather.autostart`=true; gentle
  on BoM), watchdog supervision + `weather_alert` notifications (new/upgraded warnings, cleared),
  `/health` `weather_running`/`weather_last_heartbeat`/`weather_last_error`, Overview "BoM
  Warnings" KPI + collector line, Admin Start/Stop + autostart.
- **BoM level hidden from UI:** `warning_group_type` (major/minor) is stored + used internally
  (sort order, alert upgrade detection) but NOT displayed — it would be confused with the flood
  gauge's Minor/Moderate/Major classification.
- **Warning detail + history:** each cycle also fetches `/warnings/{id}` (full HTML `message`;
  severe-weather bodies embed base64 images). Latest text is stored on `weather_warnings.message`;
  every reissue (same id, new `issue_time`) is appended to `weather_warning_updates` (unique on
  warning_id+issue_time) so development can be replayed. Detail page `/weather/warning/<id>`
  (`weather.warning_detail_layout`, routed in factory) renders the message in a **sandboxed
  iframe** (images show inline) with a **version selector**; the warnings table links to it.
- **Schema ready for 2b:** `weather_locations` (town/catchment -> geohash cache) and
  `rainfall_observations` (rain_since_9am + forecast per location, de-duped) tables exist but are
  not yet populated.

## Overview briefing PDF (updated 2026-08-10)
`reporting.build_overview_pdf` now also includes fire + weather + rainfall: KPI rows for Active
Fires / Emergency Warnings / Watch & Act / Advice / BoM Warnings / Flood Warnings / AWS Rain
Stations + Wettest since 9am (the warning levels are listed separately — same reasoning as the
Overview cards; Advice is carried here so the level breakdown fills the label/value grid); text
tables of **Active BoM Warnings**, **Active Fire Warnings** (coloured level, separate
from) **Active Fire Incidents** (Met excluded), and **AWS Rainfall — wettest stations** (tables, so
they render even where kaleido can't).

## Fire trend chart (updated 2026-07-12)
`fire.py:_trend_figure` splits the old mixed "All active" line into **Incidents** (= total_active −
warnings) vs **Warnings** (= emergency+watch_act+advice) totals, plus Fires and the three
warning-level lines, colour-matched to the map kinds.

## Analytics (built 2026-07-12)
- **Privacy-preserving, self-hosted, no third-party trackers.** `app/analytics.py` +
  `page_views` table. Views are logged from the URL-change `route` callback (Dash is an SPA, so no
  per-page GET) via `analytics.record_view`. A visitor is only a **daily salted hash of
  IP+User-Agent** (`UM_SECRET_KEY` salt) — no raw IP/PII, rotates daily, so unique-visitor counts
  work without identifying anyone. `/_dash*`, `/assets`, `/health` ignored.
- **Admin page `/analytics`** (`app/pages/analytics.py`, in RESTRICTED): views/visitors KPIs
  (24h/7d/30d), a daily views+visitors trend, and a top-pages bar (public views only).

## Weather module — rainfall (Phase 2b, built 2026-07-12)
- **Locations derived from flood gauges.** `ensure_locations()` (one-time seed) extracts a town
  from each gauge name via `weather_data.gauge_town` (handles "... at X", "Downstream of X",
  strips "(HG)"/"(TW)"), resolves it to a BoM geohash via `/locations?search=`, and caches it in
  `weather_locations`. Coords come from **decoding the geohash** (`_geohash_decode`) — no extra
  API call. Capped by `weather.max_rainfall_locations` (default 40) for politeness.
- **Rainfall polling.** `fetch_rainfall` polls `/observations` (`rain_since_9am`) +
  `/forecasts/daily` (today's max mm + chance) per location each cycle into `rainfall_observations`
  (de-duped on location+timestamp). Wired into `fetch_weather_data` after warnings.
- **Weather page** gains a Rainfall section: a map coloured by rain-since-9am and a table
  (location / catchment / rain / forecast / chance), plus a "wettest" summary line.
- **Gauge overlay (the payoff).** `station._add_rainfall_overlay` matches a gauge to its town's
  rainfall (`data.location_for_gauge`) and draws rain-since-9am on a **secondary y-axis** of the
  station history graph — only when there's actual rain in the window (dry periods stay clean).
- Not done: rainfall on the station **PDF** (vector `_trend_drawing`), per-catchment rollups.

## AWS rainfall network (Phase 2b+, built 2026-07-12)
- **Source:** all ~101 VIC Automatic Weather Stations in **one request** to BoM's state obs page
  (`vic/observations/vicall.shtml`). `app/modules/weather/aws.py`. Cells selected by `headers`-id
  suffix (`-rainsince9am`, `-datetime`) so it's reorder-proof; WMO id from each station's link.
  Coords aren't on the page — back-filled a few/cycle from per-station JSON into `aws_stations`.
- **Storage:** every reading in `rainfall_aws`, **de-duped on (wmo, obs_time)** — so polling more
  often than BoM updates (~30 min) never adds rows. Volume ≈ 4,850 rows/day, ~1.77M/yr (~250 MB/yr,
  cadence-independent). Retention: keep everything.
- **9am reset → event totals:** the raw rain-since-9am counter is stored; totals for any window are
  `data.aws_event_total` = sum of **positive increments** (a drop = the 9am reset, post-reset value
  is fresh rain). Reset-proof across any number of 9am boundaries. `_window_total` unit-verified.
- **Dedicated `rainfall` collector** (15-min, `rainfall.autostart`; own start/stop/restart in
  `collector.py`, watchdog supervision, `/health` `rainfall_running`) + an Admin **"Fetch now"**
  button (`manager.fetch_rainfall_now`).
- **Tagged like flood/power:** `rainfall_aws.timestamp` slices by `event_tags`; export
  (`app/export.py`, Admin Rainfall checkbox) adds **Rainfall Event Totals** (reset-proof) + **AWS
  Rainfall Readings** sheets.

## AWS weather observations (Phase A, built 2026-08-10)
Expanded the rain-only AWS collector into the full BoM observation set. **Same one statewide page
fetch per cycle** — every new field came out of HTML already being downloaded, so BoM load is
unchanged.
- **Fields:** air temp, apparent temp, dew point, RH, delta-T, wind dir/speed/gust, MSL pressure,
  rain since 9am, plus the daily highest gust (direction / km/h / time).
- **Parsing by `headers` id suffix, not column position** (`FIELD_MAP` in `weather/aws.py`).
  Each `<td>` lists the ids of the header cells it belongs to (`tMAL-wind-spd-kmh`); the `tMAL`
  regional prefix varies so only the suffix is stable. The **leading `-` in each suffix is load-
  bearing**: it's what stops `-tmp` matching `-apptmp`/`-lowtmp`/`-hightmp` and `-wind-dir`
  matching `-highwind-dir`. Suffixes are matched longest-first. Reordered/added/removed columns
  therefore cannot shift a value into the wrong field — covered by `vicall_reordered.html`.
- **Deliberately not stored:** the `-kts` cells (unit duplicates of km/h) and `-lowtmp`/`-hightmp`
  (daily temperature extremes).
- **Combined value+time cells.** BoM prints the daily max gust as one string, `4602:40pm` = 46 km/h
  at 02:40pm. Split by a regex anchored at BOTH ends requiring a **two-digit hour**, so the greedy
  value backtracks to the only whole-valid split (4602 → 460 → 46). BoM always zero-pads the hour;
  if that ever changes the match fails and the cell is read as a plain number, losing the time
  rather than storing a wrong value.
- **`-` means NULL, never 0.** A missing gust is not a calm night and a missing rain total is not a
  dry day. `_MISSING` normalises BoM's blank markers to None; a malformed value drops only its own
  field, and a station that throws mid-parse is skipped alone (`_parse_station_row` is wrapped) so
  one bad station never costs the state's other ~103 observations.
- **Storage: additive migration, table NOT renamed.** The 12 weather columns were added to
  `rainfall_aws` via the existing `_ensure_column` pattern (canonical list =
  `database.AWS_WEATHER_COLUMNS`). Every rainfall query, event total, export and Intelligence Feed
  detection kept working untouched and **no historical row was rewritten** — pre-Phase-A rows have
  NULL weather, which is the honest answer: it was never observed. `UNIQUE(wmo, obs_time)` holds.
- **Data API** (`weather/data.py`): `latest_aws_observations()` (newest row per station + registry
  coords), `aws_observation_history(wmo, start, end)`, `aws_weather_summary()`.
  `latest_aws_rainfall()` is now the rainfall-shaped *view* of the same query — identical columns
  and wettest-first order, so existing callers are unaffected.
- **Staleness guard:** `aws_weather_summary` only considers stations reporting within
  `AWS_STALE_HOURS` (6), so a dead station can't be published as the state's current warmest or
  windiest; the excluded count is shown on the page. The *daily* max gust is a since-midnight
  summary, so it deliberately still reads every station's latest row.
- **Weather page** "AWS Weather Observations": KPI row (strongest gust / lowest RH / warmest /
  wettest, each with its station), a **metric selector** (Rainfall · Wind Gust · Temperature · RH)
  that recolours a fixed station set — non-reporting stations stay on the map in grey so coverage
  is always visible — a full hover per station, ranked **Significant Observations**, and a table
  (Station · Temp · RH · Wind · Gust · Rain · Pressure · Observed). Numeric columns stay numeric
  with units in the header so native sort orders 9.0 below 10.1 instead of lexically.
- **Observations, not warnings.** No severity is assigned and no threshold invented; the page
  carries a standing caveat that these are raw, non-quality-controlled BoM observations.
- **Unified map** gains an optional "Weather observations (gusts)" layer, **off by default**
  (~104 stations would bury the hazard layers) and filtered to gusts ≥ 40 km/h. Reuses the
  existing `aws_stations` coords — no new geocoding.
- **Tests** (`tests/`, pytest — dev-only via `requirements-dev.txt`, excluded from the image):
  parser fixtures for normal / missing pressure / missing gust / missing temperature / reordered
  columns / malformed values / calm / no-obs-time, the value+time split, one-bad-station
  isolation, the additive migration on a pre-migration database (columns added, history intact,
  idempotent), de-dup, and the reset-proof event total re-checked against a migrated DB.
  `python -m pytest` from `unified_monitor/`.

## Storm tracker module (built 2026-07-21)
- **Ported from the standalone `../../storm Tracker` project**, reworked: NO Selenium. BoM
  publishes each radar frame as a transparent echo-only PNG at a deterministic URL
  (`reg.bom.gov.au/radar/{radar_id}.T.{YYYYMMDDHHMM}.png`, ~5-min cadence); the scraper
  probes the last 16 minute-stamps (skipping ones already in `storm_frames` — de-dup on the
  frame's OWN BoM timestamp, fixing the old fetch-time duplicate-frame bug) and processes new
  ones. Static map underlay (`/products/radar_transparencies/{radar_id}.{background,topography,
  locations}.png`) fetched once per radar, cached in memory.
- **CV pipeline** (`app/modules/storm/processing.py`): pixels matched to the standard 15-level
  BoM rain-rate palette (±30/channel; rain-free frames arrive as grayscale+alpha so
  `decode_frame` normalises every channel layout) — the old terrain/ocean/legend exclusion
  heuristics are gone because the echo layer has nothing else on it. Contours ≥90 px with
  fill-ratio ≥0.12 become cells; score = mean level ×4 + max level ×2.5 + capped area term;
  strong = level ≥12 (red), moderate = ≥8 (yellow). Palette bands NOT yet verified against a
  real storm (built on a rain-free day) — tune `PALETTE_TOLERANCE`/thresholds when one hits.
- **Tracking** (`tracker.py`): globally-nearest matching (not first-come greedy), cells coast
  3 missed frames before dropping (old code re-identified a cell after 1 miss), speed in real
  km/h from frame timestamps × km/px (radar id's last digit encodes zoom: …1=512 km range,
  2=256, 3=128, 4=64; frames 512 px), heading as compass bearing with circular-mean smoothing.
- **Storage:** `storm_frames` (processed-frame de-dup), `storm_cells` (one row per cell per
  frame), `storm_alerts` (change-only: first moderate/strong or escalation — never one row
  per frame a cell persists), `storm_timeseries` (per-cycle KPIs + heartbeat). Annotated
  composite frames (underlay + echoes + contours/tails/arrows/+30 min dashed prediction) go to
  `{BASE_DIR}/storm_frames/annotated_{radar}_{stamp}.png`, last 24 kept, gitignored.
- **Page `/storm`:** radar loop animated CLIENT-side (30 s server callback fills a
  `dcc.Store` with the frame list; a clientside callback cycles the `<img>` src at 650 ms with
  a hold on the newest frame — no server round-trip per animation tick). Frames served by a
  Flask route (`/storm-frames/<file>`, filename-regex-guarded). Plus active-cells table
  (speed/bearing as cardinal), alert log, per-cell intensity trend.
- **Multi-radar** (added 2026-07-21): `storm.radar_ids` is a LIST — every listed radar is
  tracked each cycle (per-radar tracker/underlay/frames; cells stored with their radar_id;
  legacy single `radar_id` string still accepted). Defaults: IDR023 (Melbourne 128 km) +
  IDR313 (Albany WA 128 km) + IDR143 (Mt Gambier 128 km — covers SW Victoria; site `IDR14`
  -37.75,140.77 added to `RADAR_SITES` 2026-07-22). The /storm page has a radar dropdown for
  the loop; tables mix radars with a Radar column. The tracked radar list is now
  editable from the **Settings page** ("Storm Radars", comma-separated) — it warns
  if a radar's site has no coords (cells won't georeference). NOTE: a Settings save
  writes the full merged config to config.json, so editing radars via the UI is the
  supported way to change them on a live deploy (a code-default change to
  `radar_ids` is overridden once config.json exists).
- **Wiring:** always-on collector (`storm.interval_minutes`=5, autostart), watchdog
  supervision + change-only `storm_alert` webhooks (new/intensifying
  moderate+ cells, strong cells clearing; weak never notifies), `/health`
  `storm_running`/`storm_last_heartbeat`/`storm_last_error`, Overview "Storm Cells (strong)"
  KPI + collector line, Admin start/stop/autostart panel. Deps: `opencv-python-headless`.
- **Amalgamation + impact areas (added 2026-07-21):** echo fragments within ~2×`CLUSTER_GAP_PX`
  (6 px = 6 km at 128 km zoom) are morphologically CLOSEd into one cell before contouring —
  a ragged band reads as a handful of cells, not dozens (live Albany: 13 → 5). Cell area is
  the REAL echo pixel count, not the merged hull. Annotation decluttered: weak = thin outline
  only; moderate/strong get the full product — fitted ellipse swept along the motion vector,
  hulled into a translucent **impact-area polygon** (BoM-tracker ellipse × NWS warning-polygon
  hybrid), dashed projected ellipse at +30 min, one compact label.
- **Merge/split hysteresis (added 2026-07-21):** echoes hovering around the cluster gap used
  to flap between one-storm and many-storms every frame, wrecking speed/bearing. Now dual
  thresholds with memory: new clusters form at `CLUSTER_GAP_PX` (6), but a coarse region
  (`CLUSTER_SPLIT_GAP_PX` 14) whose footprint was exactly ONE tracked cell last frame stays
  one cell until its parts truly separate; previously-separate cells are never force-merged.
  The previous frame's footprint label image round-trips scraper→detect_cells
  (`footprint_labels`/`_prev_labels`). Plus: centroids are reflectivity-WEIGHTED (track the
  core, not the outline), frame displacements implying >`MAX_SPEED_KMH` (160) are rejected
  from the motion estimate, and big merged complexes get an area-scaled match radius.
- **Georeferencing + GeoJSON export:** `RADAR_SITES` (scraper) maps IDRxx prefix → site
  lat/lon (Melbourne, Albany built in; extend via config `storm.radar_sites`);
  `px_to_latlon` is an equirectangular approx around the site. `storm_cells` gains
  latitude/longitude + `impact_geojson` (a full GeoJSON Feature per moderate/strong cell,
  lon/lat ring, properties incl. speed/bearing/valid_from; `_ensure_column` migrates old
  DBs). `/storm` "Impact areas (GeoJSON)" button downloads the active FeatureCollection —
  loads straight into geojson.io / QGIS / EM-COP.
- **Storm Briefing PDF** (`reporting.build_storm_pdf`, button on /storm): headline counts, a
  "How to read this briefing" legend (colour-coded STRONG/MODERATE/WEAK classes tied to BoM
  palette levels + a glossary of Score/Area/Movement/Position/Impact area), the tracked-cells
  table (severity-first, georeferenced position + fitted motion), active impact areas
  (with lon/lat bounds; full polygons via the GeoJSON button), the latest annotated frame per
  radar (reads the on-disk PNGs, so NO kaleido needed), and the change-only alert log.
- Not done: palette tuning against a live *severe* storm, storm cells on the fire/unified
  map (lat/lons now exist), storm cells in the XLSX export, impact-area history playback.

## Shell live widgets: sidebar incident log + news ticker (built 2026-07-21)
- `app/ticker.py`, rendered in the SHELL (`factory._shell_layout`, all pages), one 20 s
  `live-tick` interval drives both. STATELESS: everything derives from stored feed
  timestamps, so restarts neither re-fire old items nor drop active ones.
- **Sidebar log** (below the nav): last 14 VicEmergency *incidents* (not warnings/burn
  areas) newest-first — kind-coloured dot, HH:MM first_seen, category, location. Each row is
  React-keyed by its feed id; `incident_log` tracks shown ids in `_seen_incident_ids` and
  tags only genuinely-new rows with `side-log-new`, which triggers the CSS `feed-slot-in`
  animation (grows from height 0 at the top pushing the rest down, slides in from above with a
  fading accent glow) exactly once. Boot backlog seeds the set silently (no mass-animate);
  honours prefers-reduced-motion.
- **News ticker** (fixed bottom bar, CSS marquee, `.content` gets bottom padding):
  timestamped NEW triggers — new BoM warnings, new VicEmergency community warnings, and
  flood gauges CROSSING into flood (crossing reading's own obs time is the stamp; a gauge
  that has never been below minor doesn't count as a crossing). Items expire after
  `TICKER_WINDOW_MINUTES` (5). **Pinned open + red** (`ticker-emergency`) while a
  VicEmergency Emergency Warning (incl. Evacuate) or a BoM warning whose text carries the
  Standard Emergency Warning Signal (SEWS) is active — those items show the whole time
  they're active. Hidden entirely when empty. Scroll speed scales with item count.

## Roads module — VicRoads disruptions (built 2026-07-22)
- **Source:** the Transport Victoria **"Unplanned Disruptions - Road" v3** API
  (`api.opendata.transport.vic.gov.au/api/opendata/roads/disruptions/unplanned/v3`,
  DoT-managed *and* local-council roads, refreshed ~60s). Bound to the v3 OpenAPI
  (`seed/` copy of the spec is the reference). Needs a **free API key** (request
  via the Data Exchange Platform, https://data-exchange.vicroads.vic.gov.au/) sent
  in the **`KeyId`** header; the endpoint is the config default so only the key is
  needed. Both live in config (`roads.feed_url` / `roads.api_key`, set on the
  Settings page). Until the key is set the collector **log-and-skips** — a fresh
  deploy never crashes (same shape as power without EM-COP creds).
  `app/modules/roads/{scraper,data}.py`, page `app/pages/roads.py`, route `/roads`.
- **Response shape (v3):** an envelope `{meta, data: <FeatureCollection>, links}`
  — features are at `data.features` (`scraper._features_of` also tolerates a bare
  FeatureCollection/list). Paging follows `meta.total_pages` (`page`/`limit`,
  default page size 100; `roads.page_limit` forces a size, `roads.max_pages` caps
  the loop). Geometry is Point or LineString. The map **highlights the impacted
  road**: LineString disruptions render as coloured `go.Scattermapbox` line traces
  (closures red/width 5, other amber/width 3, hover keeps the road identifiable);
  only Point-only disruptions (no road segment in the feed) fall back to a marker
  dot. (`_map_figure`/`_line_segments`/`_hover` in `pages/roads.py`.)
- **Model:** upsert each feature into `road_disruptions` on its per-feature `id`
  (falls back to `impactId`/`eventId`); a disruption that drops out (road reopened)
  is marked `resolved=1`, never deleted. Properties are partly nested: road name
  from `closedRoadName`/`declaredRoadName`/`reference.localRoadName`, LGA from
  `reference.localGovernmentArea`, direction from `impact.direction`, lanes from
  `numberLanesImpacted`/`roadAccessType`, type from `eventType`+`eventSubType`.
  `is_closure` (see `_is_closure`) = live full closure: NOT `eventLocationStatus`
  Reopened/Inactive, NOT a partial/lane/reduced/shoulder `roadAccessType`, and
  either a `closedRoadName` is set or "clos" appears in access-type/event-type.
  A per-cycle `road_timeseries` row holds KPI counts + doubles as the heartbeat.
  Datetimes (`created`/`lastUpdated`/`endTime`) normalised to one local-seconds
  format (same pandas-NaT lesson as flood/fire). The v3 `reference` block's
  `closedRoadSESRegion`/`closedRoadTransportRegion` are stored (`ses_region`/
  `transport_region`; `_ensure_column` migrates existing DBs); SES Region shows in
  the page table for the SES grouping angle.
- **Type breakdown + filter** (built 2026-08-10): `disruption_type` is stored as
  `"eventType, eventSubType"`, so `data.split_type` treats everything before the
  first `", "` as the PRIMARY type (`_TYPE_SQL` does the same split in SQLite for
  the whole-table aggregate — keep the two in step). `type_breakdown` feeds a
  **stacked bar** on `/roads` (one bar per type, split full-closure vs other,
  sub-type counts in the hover) so "how many flooding disruptions, and how many of
  those actually close the road" is one read. The **Disruption type** multi-select
  filters map + chart + table together via `filter_types`; its options come from
  `type_options()`, which spans **resolved rows too** (labelled `Flooding (4
  active)`) so the list is the dataset's full catalogue of types rather than only
  what happens to be live. The v3 spec does NOT enumerate eventType — it is free
  text — so the list can only be derived from collected data.
- **Wiring:** always-on collector (`roads.interval_minutes`=3, autostart), watchdog
  supervision + change-only `roads_alert` webhooks (new full closures + reopenings;
  partial/lane disruptions never notify), `/health` exposes
  `roads_running`/`roads_last_heartbeat`/`roads_last_error`, Settings gains a
  "Road Disruptions (VicRoads)" panel + a "Road closure alerts" notify toggle.
- Not done: road disruptions on the unified/fire map + Overview KPI; the
  cross-layer correlation backlog item ("road cuts near rising gauges") can now
  join `road_disruptions` geometry against flood gauges via a shapely/STRtree pass.

## Unified map (built 2026-07-22)
- **One map, every located layer, toggleable.** `app/pages/unified.py`, route `/map`
  (2nd nav item, public). A single `go.Figure` assembled from per-layer builders,
  each reading straight from its module's data layer and REUSING the fire/roads
  render helpers so styling matches the per-hazard pages: **Fire** (kind-coloured
  markers + area fills via `fire._fill_layer`), **Roads** (highlighted line
  segments + point dots via `roads._line_segments`/`_hover`), **Storm** (cells
  sized by area + impact-polygon fills from `storm.impact_featurecollection`),
  **Power** (geocoded outage markers sized by customers-off), **Rainfall** (AWS
  stations with rain-since-9am, off by default). A `dcc.Checklist` toggles layer
  groups and Plotly's legend isolates individual traces.
- **View is pinned across refreshes:** `uirevision="unified-map"` so the 60s
  auto-refresh never resets pan/zoom (sit zoomed on a fireground while data
  updates). Uses `ui.MAP_CONFIG` for scroll-wheel zoom like every other map.
- **Flood gauges are intentionally excluded** — still no lat/lons (BoM KiWIS
  backlog item); a note on the page says so rather than faking positions.
- Fill layers are attached via `layout.mapbox.layers` (built INTO the mapbox dict,
  not a second `update_layout`, so the style/center/zoom aren't clobbered).

## Flood gauge coordinates + flood layer (built 2026-07-22)
- **The BoM flood feed carries no lat/lons**, so flood gauges couldn't be mapped.
  Fixed by matching our flood-warning gauge NAMES to **BoM Water Data Online
  (KiWIS `getStationList`)** station names, which do carry coordinates.
- **`seed/gauge_coords_tool.py`** — re-runnable matcher (like `lfg_extract_tool.py`).
  Fetches KiWIS (national; filtered to a VIC bbox — kept generous ON PURPOSE so
  NSW-administered **Murray border gauges** aren't dropped), normalises away the
  watercourse word + `@`/`at`, then matches exact → token-subset → fuzzy and writes
  **`seed/gauge_coords.json`** with a confidence flag + the matched KiWIS name/number
  for audit. Current result: **359/467 (77%)** — 182 high, 141 medium, 36 low;
  108 unmatched (reservoirs/retarding basins/`(Upstream)` variants) written with
  null coords for hand-filling. NOTE: the flood-warning station number on the BoM
  page (e.g. 582015) is NOT the KiWIS/AWRC number (Biggara = 401012) — they don't
  cross-reference, so matching is by NAME.
- **`gauge_coords` table** (station_key PK + lat/lon + kiwis_no/name + confidence),
  reloaded on boot from the seed via `importer.ensure_gauge_coords_seed()` — same
  source-of-truth policy as flood_levels / LFG impacts. Only matched gauges stored.
- **`flood.data.map_gauges()`** joins the latest reading per gauge to its coords and
  classifies it (Major/Moderate/Minor/Below via `classify_station`).
- **Flood layer on `/map`** (`unified._flood_layer`): gauge markers coloured by class
  (red/orange/yellow), below-level gauges as small blue dots for network context;
  a "Gauges ≥ Minor" KPI. Default on. To refresh coords, re-run the tool (on the
  server it fetches KiWIS live) and redeploy.

## Event Replay — /replay + the state journal (Phase C, built 2026-08-11)
Reconstructs what Passive Monitor knew at any past moment: *what did we know at 14:30, and how
did this develop between 06:00 and 18:00*. Built for after-action review, training and event
reconstruction.

### The state-change journal (`app/history.py`, `entity_state_history`)
The problem Replay could not have been built without solving first: **half the modules keep no
history.** Flood readings, storm cells and AWS observations are one row per observation, so their
past is a query. Fire incidents, road disruptions, per-location power outages and weather warnings
are UPSERTs — one row per entity, overwritten in place — so their past was simply gone.
- **A change journal, NOT a snapshot table.** A row is written only when an entity's material
  state differs from its last recorded state (sha1 over canonical JSON, `active` included in the
  hash so a resolution is always a change). At a 60-second poll a snapshot table would write
  ~1,440 rows/entity/day; a real incident produces a handful over its whole life. Verified against
  the live VicEmergency feed: cycle 1 wrote 82 rows, cycle 2 (105 features, all upserted) wrote
  **0**.
- **Canonicalisation matters more than it looks.** Keys are sorted and NaN is normalised to None —
  NaN never equals itself, so an unnormalised float would make every comparison a change and
  quietly turn the journal back into a snapshot table.
- **Tombstones.** A resolved entity gets a final `active=0` row, so replay distinguishes *hadn't
  happened yet* / *was happening* / *was over* — three answers a table of current rows collapses
  into one.
- **Effective vs recorded time.** `effective_ts` is the SOURCE's time (the feed's `updated`, BoM's
  `issue_time`); `recorded_at` is when we noticed. The feed's update stamp is deliberately NOT in
  the hash — it moves on every republish and would write a row every cycle.
- **Reconstruction is SQL.** `state_at()` is a `ROW_NUMBER() OVER (PARTITION BY entity_key ORDER BY
  effective_ts DESC)` filtered to `rn = 1`; batch de-dup lookups use one window-function query for
  the whole cycle rather than N per-entity `ORDER BY … LIMIT 1` calls — the exact query shape that
  took the VPS down in the 2026-08-09 projection incident.
- **Collector hooks are change-only and never fatal.** Each scraper journals what the cycle touched
  (`WHERE last_seen = now`, bookkeeping every source already maintains) so entities the
  resolve-sweep just closed get their tombstone in the same pass. Every hook is wrapped: a journal
  failure logs and never breaks collection.
- **It never invents the past.** `history_availability` stamps when each source started journalling,
  written once so the claimed window can never drift. Nothing is backdated.

### What replays from where (`app/replay.py`)
A deliberate split, not an accident: **KPIs reach further back than the map can.**
- **Already-historical sources** — `flood_observations`, `storm_cells`, `rainfall_aws`, and every
  module's per-cycle KPI timeseries — are read directly. They predate the journal, so KPIs and
  flood/storm/AWS layers replay correctly for events from long before it existed. Duplicating them
  into the journal would be pure waste.
- **UPSERT sources** — fire, roads, power, weather warnings — come from the journal.
- Stale-reading cutoffs (`STALE_READING_HOURS`, 24 h) stop a gauge that fell silent in March being
  painted on an August replay; KPI lookups ignore rows older than 6 h so a dead collector reads as
  **"—" not "0"** — *we weren't looking* is a different answer from *nothing was happening*.
- `coverage_note()` states the limits verbatim for the selected event. An event starting before the
  journal is described as **partial**, never as a full reconstruction.

### The map refactor (the important one)
`app/pages/unified.py` layer builders were `_fire_layer(on)` — fetching current state internally.
They are now **pure renderers** `render_fire(df)` … `render_wind(df)`, plus a `RENDERERS` registry
and `map_figure(on, dark, source=live_source, …)`. The live map passes `live_source`; Replay passes
`replay.frame_source(t)`. **One renderer, two clocks** — no copied map code, and the two maps
cannot drift apart. Storm impact polygons now come from the frame's own `impact_geojson` column
rather than a fresh `impact_featurecollection()` call, so a replayed frame draws the polygons that
existed *then*. `map_figure` also injects an empty `Scattermapbox` when a frame has no traces —
without it Plotly falls back to bare numbered axes, and a quiet moment is the normal case in replay.

### The page
Event selector (from `event_tags`; ongoing events replay to now) · coverage banner · 5-minute
slider with `updatemode="drag"` · `◀ 15m` / Play / Pause / `15m ▶` / 1×·2×·4× · historical KPI row ·
replay map with the Unified Map's own layer toggles · Intelligence timeline as the narrative spine,
click-to-seek, with the entry nearest the selected moment highlighted. Playback advances 5 min of
event time per 1 s tick × speed, **auto-pauses at the end**, and the `dcc.Interval` is disabled
whenever not playing so an idle page does no work. Timeline context lines are off: they would be
resolved against *today's* layers, which is wrong for a historical moment.
**Replay never fetches.** Everything comes from stored data — scrubbing and playback put zero load
on BoM or VicEmergency.

### Tests
`tests/test_history.py` (31) + `tests/test_replay.py` (30): change-only suppression, key-order and
NaN normalisation, the full appear→grow→escalate→resolve lifecycle, inclusive timestamp boundaries,
resolved entities leaving the map at the right moment (and still being known to have existed),
multiple entities reconstructed independently, source isolation, state keys never shadowing journal
columns, the availability stamp never drifting, event/ongoing-event windows, historical KPIs moving
with the slider, "—" vs "0", stale-gauge exclusion, coverage honesty, and a guard that a regression
reaching for live data would leave the replay map empty.

## Briefing Mode — /briefing (Phase B, built 2026-08-10)
Answers the three questions actually asked at a briefing — *what matters now · what changed ·
what should I be watching* — and is useful **on its own, before anyone generates a PDF**.
- **One model, two surfaces.** `app/briefing.py` `build_briefing_snapshot()` returns plain
  dataclasses (`Kpi`, `Change`, `Warning`, `Consequence`, `WatchPoint`, `SourceStatus`) with no
  Dash components and no reportlab flowables. `app/pages/briefing.py` and
  `reporting.build_briefing_pdf(snapshot)` BOTH render from it, so the printed pack can never
  disagree with the screen it came from. The page passes its own snapshot to the PDF builder
  rather than letting it rebuild.
- **No LLM anywhere.** Every sentence is assembled from stored values by fixed rules — same data
  in, same briefing out, every number traceable to a row.
- **Derived ≠ observed.** `WatchPoint.kind` is `OBSERVED` or `PROJECTION`; projections carry
  `flood_trend.DISCLAIMER` and render with a different badge in both surfaces. An extrapolated
  ETA is never presentable as a BoM forecast.
- **Sections:** current situation · significant changes · active warnings · emerging consequences ·
  watch points · weather observations · data freshness. Window selector 30 m / 1 h / 3 h / 6 h /
  12 h (default 1 h), Refresh, Export PDF, Copy briefing text (`dcc.Clipboard` over a hidden
  `briefing_text()` render), and a standing "Generated at" stamp.
- **Warning levels are never summed.** One Emergency Warning and two Advices need different
  responses, so a combined total would let either misrepresent the other. Flood gauges likewise
  report ≥ Minor / ≥ Moderate / ≥ Major as cumulative counts (`flood_data.flooding_breakdown`,
  which uses the MAX(timestamp) GROUP BY rather than reading 1.4M observations into pandas).
- **Advice cap.** During a widespread flood there are routinely 20+ Advices, all saying "Stay
  Informed"; printing them all pushes the Emergency Warnings off the page. Capped at
  `ADVICE_LIMIT` (5) with the omitted count stated explicitly.
- **BoM `group_type` is deliberately not shown as a "level"** — it reads minor/moderate/major and
  would be mistaken for the flood gauge classification (the Weather page omits it for the same
  reason). VicEmergency splits by level; BoM splits by warning TYPE. group_type still sorts.
- **Emerging Consequences is a geographic CLUSTER, not "a change plus its surroundings."** The
  first cut deduped each change against its own context lines and could never fire — the feed
  already appends that context to every entry. What it does now: cluster significant changes by
  `intel.context_radius_km`, keep only clusters spanning **two or more hazards**, and add the
  standing cross-layer context beneath. That is the thing the per-hazard views genuinely cannot
  show. The briefing therefore reads the feed with `with_context=False`, so a change carries only
  what it measured and no fact appears under two headings.
- **`intel_feed.entries()` gained `since`/`until`** so the window is anchored to the briefing's own
  reference time instead of a rolling "hours ago".
- **Data freshness is a first-class section**, and the stale banner sits directly under the PDF
  masthead: if a source is dead that must be known before anything below it is read. A source is
  stale at `3 ×` its configured interval (floor 10 min) and reports `never` when it has never run —
  silence is itself the finding. **A briefing must never let old data look current.**
- **`as_of=None` means live.** The model takes a reference clock rather than calling
  `datetime.now()` internally, which is what will let Phase C generate a briefing for a past
  moment. Until the state journal exists, state sections are live and the snapshot says so
  (`state_is_live=True`) instead of implying the KPIs are historical.
- **Degrades per section.** Every section is wrapped: a module that throws contributes nothing and
  is reported, rather than taking the briefing down.
- **PDF** reuses `_masthead` / `_page_furniture` / the newly module-level `_simple_table` (lifted
  out of `build_overview_pdf` so the reports can't drift into two table styles). Tables and text
  only — **no kaleido**, so the briefing still builds where the chart renderer is broken.
- **Tests:** `tests/test_briefing.py` (38) + `tests/test_briefing_pdf.py` (12) — no-data state,
  per-window change filtering, severity-then-recency ordering, warning-level separation and the
  Advice cap, cluster/no-cluster consequences, observed-vs-projection labelling, staleness, one
  broken module not taking the briefing down, `as_of`, PDF with every section empty, `&`/`<` in
  BoM titles, and an Overview-PDF regression guard for the `_simple_table` refactor.

## Intelligence Feed (built 2026-08-09)
- **The "what changed" layer.** Every other page answers *what is happening*; `/feed`
  (`app/pages/feed.py`, 2nd nav item, public) answers **what changed, by how much, how
  fast**. An entry is a time + a headline stating the movement + the numbers under it:
  `12:43 — Walwa fire increased 38 ha` / `214 → 296 ha since 12:05` / `Watch and Act
  remains current` / `3 road disruptions within 10 km`.
- **Engine `app/intel_feed.py`.** Detector pass reads every module's stored data and
  writes one `intel_events` row per *significant* change — change-only, like
  `storm_alerts` and the watchdog. Detectors: fire area growth + warning
  escalation/downgrade, flood class crossings + rise rate, statewide & per-location
  power moves, storm cell intensification, BoM warning new/reissue/cancel, road
  closure/reopen, AWS rainfall bursts. Each detector is isolated in a try/except so a
  broken source can't take the feed down.
- **THE KEY CONSTRAINT: `fire_incidents` / `power_outages` / `road_disruptions` are
  UPSERTs and keep no past values**, so "214 → 296 ha since 12:05" is impossible from
  them alone. `intel_metrics` (entity, metric, value, ts) fills that gap — a row is
  written ONLY when a value changes, so it stays proportional to change, not to poll
  rate. Sources that already keep history (`flood_observations`, `power_timeseries`,
  `storm_cells`, `rainfall_aws`) are diffed straight out of their own tables and need
  no metric rows.
- **Restart/replay safe.** `ts` is the SOURCE time the change became true (not detection
  time), detectors only look back `intel.lookback_minutes` (180), and
  `idx_intel_events_dedup` is UNIQUE on (hazard, entity, metric, kind, ts) — so a repeat
  pass, an overlapping cycle or a mid-event restart can never duplicate or re-fire an
  entry. A metric with no prior value emits nothing, so cold start seeds silently with
  no special-case flag. Continuing trends (rising gauge, growing outage, rain burst) are
  rate-limited by `intel.repeat_suppress_minutes`; a gauge that just crossed a class
  does NOT also emit "rising quickly" (the crossing entry carries the same numbers).
- **Cross-layer context is NOT stored** — the "3 road disruptions within 10 km" /
  "Watch and Act remains current" lines are resolved at READ time from current data
  (`_nearby_lines`, `_own_warning_line`, radius `intel.context_radius_km`), so an
  entry's context stays true as the situation develops. This is the long-standing
  cross-layer-correlation backlog item, landed. The haversine is **vectorised** on
  purpose: it runs per rendered entry × every located row of every layer, every 20 s.
- **Wiring:** own `intel` collector (`intel.interval_seconds`=60, autostart; fetches
  nothing, so it is cheap and started LAST in `autostart()`), watchdog supervision,
  `/health` `intel_running`/`intel_last_entry`/`intel_last_error`, Admin panel with
  Start/Stop/**Detect now** (runs inline), Overview gets a compact **"What changed"**
  panel (top 5, context skipped for speed) + an "Intelligence feed" collector line.
- **Name clash to remember:** `/intel` is the pre-existing password-gated *Intel Tool*
  (burnt-area chart generator). The feed is `/feed`; the engine is `app/intel_feed.py`,
  the page `app/pages/feed.py`.
- Thresholds all live under the config `intel` block (fire ha/%, flood m/hr, power
  customers/%, rain mm, windows, suppression).
- Not done: feed entries in the briefing PDFs / XLSX export, webhook notifications for
  feed entries (the watchdog already notifies separately — wiring both would
  double-notify), regional power rollup ("Gippsland outages") — `power_outages` has no
  region field, only a location string + geocode, so the statewide + per-location split
  is what is honest today.

## Flood rate-of-rise intelligence + projection verification (built 2026-08-09)
- **`app/modules/flood/trend.py`.** Turns "this gauge is at Minor" into rate, acceleration,
  threshold distance, a projected arrival window and catchment rainfall:
  `Rising rapidly · +0.30 m/hr over the last 90 minutes · 0.45 m below Moderate ·
  Moderate potentially reached 15:20–16:10 · 38 mm rain in the Broken Catchment`.
- **Maths.** Rate = OLS slope over `flood.trend_window_minutes` (180; gauges report every
  ~15 min so that is ~12 points). Acceleration compares the fitted slope of the recent half
  against the earlier half — far stabler on noisy gauge data than a quadratic's second
  derivative, and it yields the "Rate increasing / Steady / Rate easing" label directly.
  ETA point estimate holds the current rate; the RANGE comes from the slope's standard error
  plus, when accelerating, the earlier arrival implied by solving
  `d = rt + ½at²`. Acceleration only ever moves the EARLY bound, never the point estimate.
- **It refuses to project more often than it projects**, on purpose: fewer than
  `trend_min_readings` (4), a span under 30 min, a rate below `trend_min_rate_m_hr` (0.02 —
  otherwise dividing a distance by noise yields a confident-looking lie), or an arrival past
  `trend_max_horizon_hours` (12) all return a reason instead of a time.
- **Range floor.** On a smoothly-rising gauge the residuals vanish, the slope's standard
  error collapses and the window would tighten to a few minutes on a multi-hour projection.
  `trend_min_range_pct` (20%) floors the half-width at a share of the lead time, so vagueness
  scales with reach.
- **Target.** Next flood class above the current height; once past Major it falls through to
  the next **Local Flood Guide impact height**, which is what matters operationally from
  there (`target_kind` = class | impact). Many gauges have NaN class levels in the BoM seed,
  so this fallback fires more than you would expect.
- **Catchment rainfall** comes from the **AWS network** by radius
  (`trend_rainfall_radius_km` 50), NOT `rainfall_observations` — that table is seeded for a
  handful of towns (3 locations / 24 rows live), while `aws_stations` has all 103 with
  coordinates. Totals reuse the reset-proof positive-increment sum, and the **wettest** nearby
  station is reported rather than the mean (an average over a wide radius hides the cell
  actually driving the rise).
- **VERIFICATION — the back-check.** Every projection is written to `flood_projections` when
  it is made (unique on station+target+observed_at+method, so the detector can run as often
  as it likes without inflating the sample). `verify_projections` later scores each one
  against the observations that followed: `reached` (with signed `error_minutes` and whether
  it landed inside the quoted window), `not_reached` once past `eta_late` +
  `projection_grace_hours`, or `receded` when the river turned over instead — a different
  kind of miss, worth separating. `accuracy_summary` reports hit rate, in-window share,
  median absolute error and bias, bucketed by lead time and by target. `method` is a version
  tag (`linear-se-v1`) so changing the maths does not silently pollute the historical score.
- **The track record is published next to the projection** — a per-gauge panel on the station
  page and a statewide panel on `/flood`, both public. An ETA that cannot show its own hit
  rate is asking to be taken on trust.
- **`DISCLAIMER` travels with every ETA** (station page, feed entry, briefing PDF): "Trend
  projection — not an official flood forecast." The one genuinely dangerous output here is an
  extrapolated arrival time read as BoM hydrology, so the caveat is structural, not optional.
- **Wiring:** `run_projection_cycle` runs at the top of every intel detector pass (before the
  detectors, so a flood entry quotes the projection made from the same reading), gated to
  gauges within 20% below minor or already in flood. Station page gains a "Rate of rise"
  panel, a measurements-used table (with per-step change, which is what makes "Rate
  increasing" legible) and a projection cone on the history graph. The Intelligence Feed's
  flood entries carry acceleration / distance / ETA / rainfall lines, and `_trend_rate_text`
  makes the trend fit the SINGLE source of the displayed rate (the feed used to quote its own
  60-min window while the station page quoted the 180-min fit — two numbers for one gauge).
  The gauge briefing PDF carries the same block, disclaimer and track record.
- Config lives under `flood` (`trend_*`, `projection_grace_hours`).
- Not done: rainfall-informed projection (rain is shown as context, it does not feed the
  maths), per-catchment routing, projections for gauges with no class levels AND no LFG
  impacts, an accuracy trend over time, calibrating `trend_min_range_pct` from the observed
  in-window rate once real events have accumulated.

## POSTMORTEM — the projection cycle took the VPS down (2026-08-09, fixed same day)
- **What happened.** Shipping the flood trend work wedged the production server. The intel
  detector ran `run_projection_cycle` on its 60-second pass; at production scale one pass
  measured **103 s** (and worse with the original 500-verification cap), so the thread never
  finished before the next was due. It pegged a core and thrashed the disk alongside the
  Chrome/Xvfb power scraper. It degraded over time rather than failing immediately, because
  the cost scales with the backlog of pending projections.
- **Root cause: `LOWER(TRIM(station_name))` is not sargable.** Every per-gauge lookup in the
  app matches on it (`station_latest`, `station_history`, `trend.analyse`,
  `verify_projections`). No plain index on `station_name` can serve that expression, so
  SQLite scanned the whole table every time — ~390 ms per lookup at 1.4M rows, and the cycle
  issues hundreds per pass. It was invisible locally because the dev DB is 69k rows (21 ms),
  20x smaller.
- **Fix 1 — expression index.** `idx_flood_obs_station_ts ON flood_observations
  (LOWER(TRIM(station_name)), timestamp)`. SQLite indexes expressions, so the existing
  queries became sargable with no code change: 390 ms -> 0.1 ms, measured cycle 103.6 s ->
  3.1 s. It lives in `SCHEMA` (not `_ensure_column`) because `init_db` replays the whole
  schema on every boot, so existing DBs pick it up automatically; ~2-4 s to build on 1.4M
  rows. This also speeds up every gauge detail page.
- **Fix 2 — the cycle runs on its own slower clock.** `intel.projection_interval_seconds`
  (300) gated via `_projection_due`. Gauges only report every ~15 min, so running it every
  60 s repeated identical work against unchanged data. In-process state, so a restart runs it
  once immediately.
- **Fix 3 — bounded verification.** `intel.projection_verify_limit` (100, was a hardcoded
  500) so a backlog is worked off steadily instead of in one unbounded burst.
- **The lesson for anything new on the intel pass:** it runs every 60 s against tables that
  grow forever. Before adding work there, check the query plan against a PRODUCTION-SIZED
  table — `EXPLAIN QUERY PLAN` showing `SCAN` on `flood_observations`, `rainfall_aws` or
  `storm_cells` is a wedged server waiting to happen, and the dev DB is far too small to
  reveal it.

## Backlog (not started)
Full flood+power PDF *sitrep* (beyond the Overview snapshot) · dedicated flood map PAGE (gauge
lat/longs now exist via `gauge_coords`; flood gauges already render on `/map`) · hand-fill the
~108 unmatched gauge coords in `seed/gauge_coords.json` ·
event timeline/compare · BoM forecast overlay · data retention/archive · deploy pipeline
(GitHub Action + Watchtower) · in-browser file upload on Import page · auto-tagging of events ·
viewer roles + audit log · log rotation/capped backups · power-dependent-customer 24h focus ·
**per-catchment rainfall rollup** (aggregate rainfall_observations by weather_locations.catchment
for a "rain by catchment" summary + a per-catchment total on gauge pages) · rainfall on the
station briefing PDF · cross-layer correlation engine — DONE 2026-08-09 as the Intelligence Feed's read-time
context lines (see above); `/map` remains the shared visual canvas. ·
**Settings-save shouldn't freeze all defaults** (2026-07-22): `settings.save()` calls
`load_config()` (defaults merged with the existing file) then writes the WHOLE thing back to
config.json, so once a deploy has ever saved Settings, every code-default change is silently
overridden on that instance (this is why the IDR143 `radar_ids` default didn't reach the VPS —
had to be set via the new Settings field / a config.json edit). Fix: have `save()` persist only
the fields the form actually controls (load the RAW config.json, update those keys, write back),
so unset keys keep falling through to code DEFAULTS. Touches `app/config.py` (add a raw
load/save-partial helper) + `app/pages/settings.py`.

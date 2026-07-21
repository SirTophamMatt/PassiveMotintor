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

## Overview briefing PDF (updated 2026-07-12)
`reporting.build_overview_pdf` now also includes fire + weather + rainfall: KPI rows for Active
Fires / Emergency & Watch&Act / BoM Warnings / Flood Warnings / AWS Rain Stations + Wettest since
9am; text tables of **Active BoM Warnings**, **Active Fire Warnings** (coloured level, separate
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
- **Weather page** "AWS Rainfall Network" section: map coloured by rain-since-9am + table.
- **Tagged like flood/power:** `rainfall_aws.timestamp` slices by `event_tags`; export
  (`app/export.py`, Admin Rainfall checkbox) adds **Rainfall Event Totals** (reset-proof) + **AWS
  Rainfall Readings** sheets.

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
  IDR313 (Albany WA 128 km). The /storm page has a radar dropdown for the loop; tables mix
  radars with a Radar column.
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

## Backlog (not started)
Full flood+power PDF *sitrep* (beyond the Overview snapshot) · flood map view (needs gauge
lat/longs — BoM KiWIS `getStationList` likely has them; email to BoM drafted 2026-07-04) ·
event timeline/compare · BoM forecast overlay · data retention/archive · deploy pipeline
(GitHub Action + Watchtower) · in-browser file upload on Import page · auto-tagging of events ·
viewer roles + audit log · log rotation/capped backups · power-dependent-customer 24h focus ·
**per-catchment rainfall rollup** (aggregate rainfall_observations by weather_locations.catchment
for a "rain by catchment" summary + a per-catchment total on gauge pages) · rainfall on the
station briefing PDF · cross-layer correlation engine (outages inside flooded
catchments, road cuts near rising gauges) — the `/map` unified view now exists as the shared
canvas for this. (Unified map itself: DONE 2026-07-22, see above.)

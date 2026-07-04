# HANDOFF — Unified Monitor

Round-trip backup: work terminal → personal machine (fallback) → port back to a new
restricted SOE once it settles. **Not a one-way move.** Code → private git repo;
scraped/operational data stays local, out of version control.

Generated 2026-06-12 from live inspection. All paths relative to this folder
(`unified_monitor/`).

---

## 1. What it is

- **Name:** Unified Monitor (a.k.a. "Passive Monitor" unified dashboard).
- **Purpose:** One dashboard combining **Flood Monitor** (BoM Victorian river-gauge
  scraping) + **Power Outages** (EM-COP outage scraping) + **EM-COP quick-launch**.
  Runs as a **desktop window** and a **web server** from one codebase.
- **Stack:** Python 3.11 · Dash 2.17 / Plotly · pandas · Selenium + webdriver-manager ·
  BeautifulSoup/lxml · pywebview (desktop) · waitress (web) · SQLite (WAL).
- **Current state:** **Working, with one module blocked.**
  - Flood module: end-to-end OK (live BoM scrape verified, ~479 readings collected).
  - Overview / Settings / Import Data pages: OK.
  - Power module: **blocked on EM-COP access** — login works but the outage dashboard
    returns `forbidden.seam`; numbers never populate. See §6.

Layout:
- `run_web.py` (waitress, `--host/--port`) · `run_desktop.py` (pywebview window)
- `app/factory.py` (Dash app, routing, theme) · `app/collector.py` (background threads)
- `app/database.py` (single SQLite `unified_monitor.db`, WAL) · `app/config.py` (config I/O)
- `app/importer.py` + `app/pages/importer_page.py` (legacy import)
- `app/modules/{flood,power,emcop}/` (scraper + data per module)
- `app/pages/` (overview, flood, power, importer_page, settings) · `assets/style.css`

---

## 2. Environment to reproduce

- **Python 3.11.9.** (Work box uses the Microsoft Store build with `pip install --user`,
  no venv — don't replicate that. On personal, install plain CPython 3.11.x.)
- **pip 24.0.** **Git 2.54.0.** No `gh`, no `node` required.
- **Google Chrome** installed (work box: 149); chromedriver auto-managed by
  `webdriver-manager`, cached under `~/.wdm/`. **Driver major version must match Chrome.**
- Declared deps — `requirements.txt`:
  `dash>=2.17 · pandas>=2.0 · plotly>=5.18 · requests · beautifulsoup4 · lxml · openpyxl ·
  geopy · selenium · webdriver-manager · pywebview · waitress`
- Exact versions confirmed working (pin if you want reproducibility):
  `dash 2.17.1 · pandas 2.2.2 · plotly 5.20.0 · selenium 4.22.0 · webdriver-manager 4.0.2 ·
  pywebview 6.2.1 · waitress 3.0.2 · beautifulsoup4 4.12.2 · geopy 2.4.1 · openpyxl 3.1.2`
- **Recommended setup (personal):**
  `python -m venv .venv` → activate → `pip install -r requirements.txt`.
- **Corporate-net pip caveat (work box only):** SSL interception breaks pip; needed
  `--trusted-host pypi.org --trusted-host files.pythonhosted.org`. Personal shouldn't need it.

---

## 3. Auth & access basis

- **Access basis:** EM-COP (Victorian Emergency Management Common Operating Picture),
  under the user's own EM-COP login. BoM flood data is **public — no auth**.
- **What needs creds:** the Power module (scrape) and EM-COP quick-launch.
- **Where stored (values NOT in this doc):** `config.json` → `emcop.username` / `emcop.password`,
  written via the in-app **Settings** page. **Plaintext JSON, gitignored.** Currently populated.

⚠️ **Plaintext-credential flag:** `config.json` holds the EM-COP password in plaintext by
design (local, gitignored). Re-enter creds **fresh** via Settings on the new machine — do not
copy the stored value across. No credentials are hardcoded in this project's source (legacy
sibling scripts outside this folder are a separate matter).

---

## 4. Git & continuity

**Current state: NOT a git repo.** No `.git`, no remote, no branch, nothing committed —
loose files only. `.gitignore` already present and correct (excludes `config.json`, `*.db*`,
`*.csv`, `*.log`, `__pycache__/`, venv, build).

Setup as a private, round-trippable repo:
```
git init
git add app/ assets/ run_web.py run_desktop.py requirements.txt README.md .gitignore HANDOFF.md CLAUDE.md
git commit -m "Initial import of unified_monitor"
git branch -M main
```
- Create a **private** GitHub repo; push over HTTPS+PAT or SSH (no `gh` on either box).
  Use a **freshly created** credential on personal.
- **Verify `.gitignore` before first push** — `config.json` and all data/db/logs must stay out.
- **Round trip:** personal = `main`. When the new work SOE settles, `git clone`/pull back;
  if GitHub is filtered there, hand-carry a code-only zip and re-init.

**Data policy (stays local, never committed):**
- App regenerates `unified_monitor.db` on first run.
- Legacy history lives one level up (`../Flood Monitor/`, `../power_data.csv`,
  `../outage_tracker.csv`, `../geo_cache.json`); pull it in via the **Import Data** page
  (defaults point at those paths). `../Flood Monitor/Flood Levels.xlsx` is the flood-level
  reference needed for classification.

---

## 5. Restriction risk (survives a locked-down SOE?)

| Capability | Needs | Survives restricted SOE? | Workaround |
|---|---|---|---|
| Flood module | Python deps + outbound HTTP to bom.gov.au | ⚠️ Blocked installs / filtered net break it. | Pre-stage a venv or PyInstaller exe while you have rights; view imported history offline. |
| Power module | All above **+ Chrome + chromedriver (web download) + visible browser + EM-COP reachable** | ⚠️⚠️ Highest risk: needs Chrome, a driver fetch, a non-headless window, EM-COP not role/geo-blocked. | Pre-cache matching chromedriver under `~/.wdm`; if EM-COP unreachable, run the power module from personal instead. |
| Desktop mode | pywebview → OS WebView2 (present on Win10/11) | ✅ Usually fine, no extra install. | Use `run_web.py` as fallback. |
| Packaging | PyInstaller | ✅ if built **before** losing admin. | Build the exe now; sidesteps "no Python/installs" (Chrome still needed for scrape). |

**Pre-emptive while admin still available:** `pip download -r requirements.txt` to a wheels
folder; build a PyInstaller exe; copy cached `~/.wdm` chromedriver. Stash with the backup.

---

## 6. Decisions & context

Settled (don't re-litigate):
- **One codebase, two entry points** (pywebview desktop + waitress web) for max reuse of the
  Dash/Plotly code. Not FastAPI+React; not PWA-only.
- **Modules:** flood + power + EM-COP quick-launch. PDF reports deferred.
- **Credentials:** in-app Settings → gitignored `config.json`; no hardcoding.
- **Data:** fresh empty SQLite + on-demand Import Data page (not auto-import).
- Single SQLite DB (WAL) replaces scattered CSVs; flood readings **de-duped on insert**
  (legacy CSVs were ~70% duplicate rows). Collection runs in **background threads**, not in
  render callbacks, so closing the UI tab doesn't stop collection.
- Power scraper hardened: `forbidden.seam` retry (wait 10s, re-login), logs the username each
  attempt, **visible browser + automation-fingerprint suppression** (headless killed the session).

**Open blocker — EM-COP power scrape (where work stopped):**
- After login, `cop.em.vic.gov.au/sadisplay/poweroutages/` returns **forbidden.seam**
  (redirects to `prod.cop.em.vic.gov.au`). Dashboard HTML loads but its JS files are served the
  Forbidden page — the "© … M.I.T." line is the `Unexpected identifier 'Copyright'` console
  syntax error — so scripts never run and KPI numbers stay blank.
- Diagnosed: (1) **headless detection** drops the session — visible browser keeps it alive (now
  default); (2) even with a live session, `poweroutages/` is forbidden — likely a **moved host /
  stale URL** (new `prod.cop` / `app.prod.cop` hosts seen).
- Account in use: `apptest@mattlamont.me` (user confirms correct; worked previously).
- **Decided next approach:** drop DOM-scraping for the **direct data feed** — numbers come from
  `seam/resource/remoting/execute` POSTs (JBoss Seam Remoting); replay with `requests` + session
  cookies, no client JS. Unlocks per-supplier / cause / crew / ETR fields.
- **Waiting on user:** from a *working* browser on the live dashboard, provide (a) the exact
  address-bar URL and (b) a HAR ("Save all as HAR with content") or the page's `index.js`, to
  identify the real host + remoting method.

**Feature backlog (proposed, not started):** threshold notifications; unified flood+power PDF
sitrep; flood map view; event timeline/compare; BoM forecast overlay; web auth for team use;
data retention/archive; collector health watchdog; power-dependent-customer 24h focus.

---

## 7. Next steps

**Stand up on personal (fallback):**
1. Install Python 3.11.x; `python -m venv .venv`; activate; `pip install -r requirements.txt`.
2. Ensure Chrome installed; first power run lets webdriver-manager fetch the matching driver.
3. `python run_web.py` (or `run_desktop.py`). Settings → enter EM-COP creds fresh.
4. Copy legacy data folders over (or point at them) → Import Data page to load history.
5. `git init` + push to a new **private** repo per §4 with a fresh credential.

**Resume the actual work:**
6. Unblock power: capture the working dashboard URL + HAR/`index.js` → wire up the direct Seam
   Remoting feed in `app/modules/power/`.

**Port back to the new work SOE (later):**
7. `git clone`/pull the private repo (or hand-carry a code-only zip if GitHub is blocked).
8. If installs are locked: use pre-staged wheels / PyInstaller exe / cached `~/.wdm` driver.
9. Re-enter EM-COP creds via Settings; re-import or re-collect data locally.

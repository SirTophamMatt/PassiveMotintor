# Fire Danger Ratings & Total Fire Bans — Intel Tool

Collects the Victorian Fire Danger Ratings (FDR) and Total Fire Ban (TFB)
declarations for the nine fire weather districts, keeps a revision-tracked
history, and fills the **FDR Daily Statistics Spreadsheet** without touching
anything else in it.

* **Web:** Intel Tool → **Fire Danger Ratings** tab (`/intel/fdr`, behind the
  Intel password).
* **Command line / scheduled:** `python -m app.modules.fdr …`.
* **Code:** `app/modules/fdr/` (model, parse, store, scraper, resolve, workbook,
  cli) and `app/pages/fdr.py`. Tests: `tests/test_fdr.py`.

---

## 1. Discovery — what the sources provide

| Source | Machine-readable? | What it carries | Used |
|---|---|---|---|
| **CFA "Total Fire Bans and Fire Danger Ratings" RSS** — `https://www.cfa.vic.gov.au/cfa/rssfeed/tfbfdrforecast_rss.xml` | Yes — RSS 2.0, refreshed every minute (CFA) | One item per day (today + 3), all 9 districts: a TFB section then a "Fire Danger Ratings" section of `District: RATING` lines | **Primary** |
| CFA per-district feeds — `…/rssfeed/<slug>-firedistrict_rss.xml` | Yes, same family | One district each | Optional cross-check (off) |
| BoM | Fire weather products exist, but no FDR/TFB source was verified | — | Not used |
| VicEmergency | Incidents/warnings GeoJSON (already collected by the Fire module); no FDR/TFB in it | — | Not used |

How this was established, honestly:

* The development sandbox **cannot reach** cfa.vic.gov.au, bom.gov.au or
  emergency.vic.gov.au (the egress proxy blocks them), so the live XML was
  **not** inspected directly.
* The statewide feed URL and the district-feed family are listed on CFA's RSS
  page and in the Victorian Government data catalogue
  (data.vic.gov.au, "CFA data feeds for Incidents, Total Fire Bans & Fire
  Danger Ratings and latest news"). District URLs following the
  `<slug>-firedistrict_rss.xml` pattern are confirmed there for **Central,
  North Central and West and South Gippsland**; the other six are inferred
  from the pattern — which is why district feeds are off by default.
* The item structure (date in the title, TFB section before a "Fire Danger
  Ratings" heading, `District: RATING` lines) was confirmed from the source of
  an open-source Home Assistant integration that parses this exact feed
  (`joelg12345/victoria-fire-danger`).
* **CFA terms:** the feeds are for personal, non-commercial use, must be used
  as provided, and a website showing them should link back to CFA (the page
  does). Catalogue licence metadata disagrees (CC-BY 3.0 vs 4.0); CFA's own
  terms are the stricter, so they are what this tool follows.

Gaps that matter:

* **No archive.** CFA publishes today + 3 days only. Past ratings cannot be
  fetched, so **backfill can only re-apply what this tool already stored**
  (or what someone enters as a correction). Nothing is reconstructed.
* **Issue time.** Items carry a `pubDate`; with a feed regenerated every
  minute it may be the generation time rather than the forecast's issue time.
  It is stored (`issue_ts`) but revisions are detected by **content**, and
  "when we saw it" is always recorded alongside.
* **Multiple ratings per district.** The feed shows one line per district.
  Victoria rates per fire weather district, so a second rating in the same
  district on the same day is expected to appear either as a value naming more
  than one rating, or as an **amendment during the day**. Both are detected
  (see §4). If CFA ever represents it some other way, the value will not parse
  cleanly and is sent to review — never guessed.

## 2. Architecture

```
CFA RSS ──► scraper.run ──► parse.parse_feed ──► store (revisions, raw bodies, flags)
                                                     │
                       overrides (manual) ──────────►│
                                                     ▼
                                      resolve.resolve_range  (single source of truth)
                                         │                │
                          Intel page /intel/fdr     workbook.update_file ──► .xlsx
                                         │                │
                                       CLI (python -m app.modules.fdr)
```

* Lives inside Passive Monitor: one **always-on collector thread**
  (`fdr.interval_minutes`, default 30), watchdog supervision, `/health`
  fields `fdr_running` / `fdr_last_success` / `fdr_last_error`.
* Uses the app's single SQLite DB (`unified_monitor.db`, WAL) — tables
  prefixed `fdr_`. The CLI uses the same DB (`UM_DATA_DIR`).

### Database (`app/database.py`)

| Table | Purpose |
|---|---|
| `fdr_runs` | every run: trigger, status (ok/partial/failed), counts, errors |
| `fdr_raw` | each distinct feed body (sha1), so any value traces to the exact bytes |
| `fdr_forecasts` | rating **revisions** per (date, district, source): ratings, rank, multi, as-published text, issue/first-seen/last-seen times, `is_current` |
| `fdr_rating_details` | one row per rating when a district has more than one on a day (published multi-value or same-day amendment, with period) |
| `fdr_tfb` | TFB revisions per (date, district, source): YES/NO, basis (district line / statewide statement) |
| `fdr_overrides` | manual corrections with reason; revoked, never deleted |
| `fdr_flags` | review queue: conflicts, unrecognised values, missing districts, blocked workbook writes |
| `fdr_workbook_updates` | every cell written: workbook, sheet, cell, previous → new value, run |

All timestamps are ISO-8601 **with offset** (Australia/Melbourne), plus an
epoch column for ordering (offset strings do not sort across DST).
Uniqueness: `(valid_date, district, source, revision)`; a row is written only
when published content changes — a repeat read just advances `last_seen`.

## 3. Data quality rules

* **A failed or garbled retrieval never erases anything.** A fetch error
  records a failed run. A body with no recognisable ratings is saved to
  `fdr_debug_<source>.xml` and the run fails loudly. An unrecognised value
  never supersedes a recognised one; an unclear TFB never supersedes YES/NO.
* **Ratings** are AFDRS only: No Rating, Moderate, High, Extreme,
  Catastrophic (rank 0–4). Pre-2022 words (Very High, Severe, Code Red,
  Low-Moderate) are **unrecognised**, not mapped. "No Rating" is a published
  rating; *nothing published* is separate and never becomes No Rating.
* **TFB:** YES/NO only from explicit wording (per district, or a statewide
  "no Total Fire Bans" sentence). A district not mentioned is **UNKNOWN**.
* **Dates:** from the item title; a weekday that doesn't match the date
  rejects the item; dates outside yesterday…+7 days are ignored.
* **Confirmed vs preliminary:** a forecast is the day's record only if it was
  still being published within `confirm_window_hours` (12) before the day
  began, or during it. A 4-day-ahead value nobody re-checked stays
  *preliminary* and is never written to the workbook.
* **Conflicts** between sources are flagged and the district is held for
  review — no source is silently preferred.

## 4. Multi Day

Multi Day = more than one rating applied **within a district on the same
calendar day** (not "a multi-day forecast"). Detected from:

1. **A published value naming more than one rating** — `High and Extreme`,
   `HIGH/EXTREME`, or the district listed twice with different ratings.
2. **An amendment during the day** — the rating current at midnight differs
   from one first seen after `multi_amendment_after` (06:00). A revision seen
   before 06:00 is treated as the day's forecast being finalised, not a second
   rating. The comment records the periods: `Central: High until 13:40,
   Extreme from 13:40 (amended on the day)`. Times are when the tool *saw* the
   change, so they are only as precise as the collection interval.

Multi Day is **No** only when all nine districts are confirmed with one rating
each; otherwise it stays blank and the day is listed for review.

**What goes in the rating cell on a multi-rating day** (`multi_rating_display`):

* `auto` (default): if the column's dropdown (or existing values) has a
  combined entry for exactly those ratings (e.g. `High/Extreme`) it is used;
  otherwise the cell is **left blank**, Multi Day is set to Yes, and the
  Comments cell says which ratings applied — so nobody's reporting
  convention is assumed.
* `highest` — write the highest rating. `combined` — write `High/Extreme`.
  `review` — always leave blank.

## 5. The workbook

### Why it is not saved with openpyxl

openpyxl rewrites the whole file and silently drops what it doesn't model —
notably **dropdowns whose list is on another sheet** (stored as Excel
extensions; openpyxl even warns "Data Validation extension is not supported
and will be removed"), extension conditional formats, slicers, sparklines,
some images/charts and custom XML. A test demonstrates the loss.

So openpyxl is used **read-only** (discovery), and the write edits only the
target `<c>` elements inside the one worksheet's XML, sets
`fullCalcOnLoad="1"` so Excel recalculates the summaries on open, and copies
every other part of the file byte-for-byte.

### Discovery (nothing is assumed by position)

* Sheet + header row: the first row naming ≥ 7 of the 9 districts
  (aliases such as `W&S Gippsland`, `Nth Central` accepted).
* Date / TFB / Multi Day / Comments columns: by header text in that row or
  the two above it (merged headers).
* Each row's **date** is read from the date column (cached value; `=E12+1`
  chains are followed). Duplicate dates are reported.
* Dropdown lists (inline, range, named range, or Excel-extension) give the
  workbook's own spelling: `High` vs `HIGH`, `Yes` vs `Y`, combined values.
* TFB convention (`tfb_mode=auto`): Yes/No dropdown or values → **any**
  (Yes if any district has a TFB); district names in existing values →
  **districts**; numbers → **count**. Set it explicitly once confirmed.

`python -m app.modules.fdr inspect --workbook FILE` prints exactly what was
found — **run it on the real workbook first**.

### Write rules

* Only days that have started (`write_future_days` false) and only confirmed
  values. Unknown stays blank.
* Formula cells are never written.
* Empty cell → filled. A cell holding the value this tool last wrote there
  (per `fdr_workbook_updates`) → updated (amendment). Anything else was typed
  by a person → **left alone and flagged** (`--force` overrides).
* Comments: the person's text is kept; the tool's part is a trailing
  `[FDR] …` segment that is replaced, never duplicated.

### Safety sequence

refuse if open in Excel (`~$` owner file or a write lock) → inspect → plan →
write to a temp file beside the original → **validate** (zip integrity, same
parts, untouched parts byte-identical, same sheets, same formula count, every
changed cell reads back) → **back up the original** (`fdr_backups/`, last
`backups_keep`) → atomic replace. Any failure leaves the original untouched.

## 6. Using it

### Intel page (`/intel/fdr`)

Current forecast (4 days × 9 districts, AFDRS colours, TFB badges, preliminary
in italics, ⚠ conflict, ? unrecognised) · season record for any date range ·
revision history for a date · review queue · **Correct a record** (rating,
TFB, Multi Day, comment — reason required, revocable) · **Workbook**: upload →
see every cell that would change → download the filled copy. "Fetch now" runs
one collection.

### Command line

```bash
python -m app.modules.fdr status                       # last run + 4-day forecast
python -m app.modules.fdr fetch                        # collect now
python -m app.modules.fdr fetch --from-file feed.xml   # parse a saved copy
python -m app.modules.fdr inspect --workbook FILE      # how the workbook was understood
python -m app.modules.fdr run --workbook FILE --dry-run
python -m app.modules.fdr run --workbook FILE          # fetch + update
python -m app.modules.fdr update-excel --workbook FILE [--start D --end D] [--force]
python -m app.modules.fdr backfill --start 2026-07-01 --end 2026-10-15 --workbook FILE
python -m app.modules.fdr validate --workbook FILE
python -m app.modules.fdr show --date 2026-10-16
python -m app.modules.fdr override add --date 2026-10-16 --district Central \
        --field rating --value "High/Extreme" --reason "CFA amendment 13:40"
python -m app.modules.fdr override list | revoke --id 3
python -m app.modules.fdr flags [--resolve ID]
python -m app.modules.fdr export --start 2026-07-01 --end 2027-06-30 --out fdr.csv
```

Exit codes: `0` ok · `1` failed · `2` done but something needs a person ·
`3` another run holds the lock (`fdr.lock`; a lock older than 30 min is
treated as abandoned). Log: `fdr.log` (rotating, 2 MB × 5) next to the DB.

### Manual corrections

A correction never edits the source rows; it sits on top with a reason and is
shown as a correction everywhere. Revoking it restores the source value.
`--district '*'` for the whole-day fields (`multi`, `comment`).

## 7. Configuration (`config.json` → `fdr`)

| Key | Default | Meaning |
|---|---|---|
| `state_feed_url` | CFA statewide RSS | primary source |
| `district_feeds` | `false` | also read the nine district feeds as a cross-check |
| `interval_minutes` | `30` | collector cadence (TFBs can be declared any time; the feed refreshes every minute) |
| `autostart` | `true` | start with the web app |
| `confirm_window_hours` | `12` | how close to the day a forecast must have been seen to count |
| `multi_from_amendment` / `multi_amendment_after` | `true` / `06:00` | same-day amendment → Multi Day |
| `workbook_path` / `workbook_sheet` | `""` | workbook for CLI/auto updates; sheet auto-found |
| `auto_update_workbook` | `false` | update `workbook_path` after each collector run |
| `multi_rating_display` | `auto` | auto / highest / combined / review |
| `tfb_mode` | `auto` | auto / any / districts / count |
| `write_future_days` | `false` | also write preliminary future days |
| `backup_dir` / `backups_keep` | `<data>/fdr_backups` / `30` | workbook backups |

No credentials are needed (public feed).

## 8. Deployment

### Windows (local, against the workbook on your PC)

```powershell
git clone https://github.com/SirTophamMatt/PassiveMotintor.git
cd PassiveMotintor
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt          # includes tzdata (needed on Windows)
$env:UM_DATA_DIR = "C:\FDR\data"         # where the DB, log and backups live
python -m app.modules.fdr inspect --workbook "C:\FDR\FDR Daily Statistics Spreadsheet - 2026-27.xlsx"
python -m app.modules.fdr run --dry-run --workbook "C:\FDR\FDR Daily Statistics Spreadsheet - 2026-27.xlsx"
```

Task Scheduler (every 30 minutes; a run while the workbook is open in Excel
just reports "open in Excel" and the next run catches up):

```powershell
schtasks /Create /TN "FDR collection" /SC MINUTE /MO 30 /F /TR ^
 "cmd /c set UM_DATA_DIR=C:\FDR\data&& C:\path\PassiveMotintor\.venv\Scripts\python.exe -m app.modules.fdr run --workbook \"C:\FDR\FDR Daily Statistics Spreadsheet - 2026-27.xlsx\""
```

Set the task's "Start in" to the `PassiveMotintor` folder (Task Scheduler UI →
Actions → Edit).

### Server (Hostinger VPS, existing Docker deployment)

Nothing extra to deploy: the collector runs inside the existing `app`
container from boot and stores into `/data/unified_monitor.db`. Use the Intel
page to fill an uploaded copy of the workbook. To keep a workbook on the
server instead, put it in `./data/` and set
`fdr.workbook_path = "/data/FDR Daily Statistics Spreadsheet - 2026-27.xlsx"`
(+ `auto_update_workbook: true` if wanted). CLI inside the container:

```bash
docker compose exec app python -m app.modules.fdr status
docker compose exec app python -m app.modules.fdr run --workbook "/data/FDR Daily Statistics Spreadsheet - 2026-27.xlsx"
```

Host cron alternative (if the collector is disabled):
`*/30 * * * * cd /opt/watchdesk && docker compose exec -T app python -m app.modules.fdr run >> /var/log/fdr-cron.log 2>&1`

Concurrency: the CLI takes `fdr.lock`; SQLite runs in WAL with a 30 s busy
timeout, so the web app and a CLI run can share the DB. Only one writer should
own a given workbook file — don't point Task Scheduler on a PC and the server
at the same file on a synced drive.

### Backup and recovery

* **Workbook:** every write first copies the original to `fdr_backups/`
  (timestamped, last 30 kept). To restore, close Excel and copy a backup over
  the workbook. `fdr_workbook_updates` lists every cell changed and its
  previous value.
* **Database:** the web app snapshots `unified_monitor.db` to `backups/` on
  each boot (last 15). Back up `./data` on the VPS as for the rest of the app.
* **Never delete `unified_monitor.db`** — it is the only record of past
  ratings, since CFA keeps no archive.

## 9. Troubleshooting

| Symptom | Look at |
|---|---|
| Run failed, "no ratings recognised" | `fdr_debug_cfa-state.xml` next to the DB — the feed format probably changed; send it in with a bug report |
| Values stuck "preliminary" | collector not running near the day (`/health` → `fdr_running`, `fdr_last_error`) |
| "open in Excel" | close the workbook; the next scheduled run writes |
| Cells "left alone" | a person typed a different value; correct it or rerun with `--force` |
| A district shows ⚠ | the two sources disagree — check CFA, then add a correction and resolve the flag |
| Wrong column picked | `inspect`; set `fdr.workbook_sheet` / rename the header |

## 10. Limitations and things to validate

1. **Not yet verified against the live CFA feed** (blocked from the build
   environment). The parser works by content, so wording differences are
   tolerated, but the first live runs should be checked on the Intel page
   (Revision history shows the text exactly as published). Replace
   `tests/fixtures/cfa_tfbfdr_sample.xml` (constructed test data) with a saved
   copy of the real feed.
2. **The real workbook was not available.** The tests use a workbook built to
   the described layout (C/D/E/G–O/P, row 12, FY 2026-27). Run `inspect` on
   the real one and confirm: the sheet, the column mapping, the TFB convention
   and the dropdown spellings. Then set `tfb_mode` and `multi_rating_display`
   explicitly.
3. **Multi Day reporting convention** (highest vs combined vs blank) must be
   confirmed with whoever owns the statistics; default leaves the cell blank.
4. **Same-day amendment times** are when the tool noticed, to the nearest
   collection interval.
5. **No historical backfill from CFA** — only from this tool's own history.
6. District feeds: URL pattern confirmed for three districts only.
7. BoM is not used: no official BoM FDR/TFB endpoint could be verified from
   here, and inventing one was out of scope.

Future enhancements this is built for: severity-increase alerts (hook the
watchdog/notify webhooks to `fdr_forecasts` revisions), seasonal statistics
and district comparisons (`export` CSV is one row per date × district with
rank and season — ready for Power BI), Extreme/Catastrophic day counts, a
TFB-day dashboard, Op Summary auto-fill of the TFB/FDR slide.

## 11. Sample execution log

From `fetch --from-file` with the constructed test feed (dates 15–18 Oct
2026), against a workbook built to the described layout:

```
$ python -m app.modules.fdr fetch
Run 1: ok — 35 values read, 35 new, 0 amended, 0 unchanged.

$ python -m app.modules.fdr fetch            # same feed again
Run 2: ok — 35 values read, 0 new, 0 amended, 35 unchanged.

$ python -m app.modules.fdr fetch            # CFA amends Central to Extreme at 14:40
Run 3: ok — 35 values read, 0 new, 1 amended, 34 unchanged.

$ python -m app.modules.fdr fetch            # source unreachable
WARNING app.modules.fdr.scraper: FDR: cfa-state fetch failed: [Errno 2] No such file or directory: '/nonexistent.xml'
Run 4: failed — 0 values read, 0 new, 0 amended, 0 unchanged.
  ERROR cfa-state: fetch failed — [Errno 2] No such file or directory: '/nonexistent.xml'
(exit 1 — nothing stored was changed)

$ python -m app.modules.fdr update-excel --workbook "FDR Daily Statistics Spreadsheet - 2026-27.xlsx"
  Sheet 'Daily Stats', header row 11, 365 dated rows (2026-07-01 .. 2027-06-30)
  Date column E; TFB C (convention: any); Multi Day D; Comments P
  Districts: Mallee G, Wimmera H, Northern Country I, North Central J, North East K, East Gippsland L, West and South Gippsland M, Central N, South West O
  Present and preserved untouched: dropdown lists stored as Excel extensions
Wrote 11 cell(s); 0 left alone; 1 need review.
    G118 2026-10-15  None -> 'Extreme'  (Mallee rating (cfa-state))
    H118 2026-10-15  None -> 'Extreme'  (Wimmera rating (cfa-state))
    I118 2026-10-15  None -> 'High'  (Northern Country rating (cfa-state))
    J118 2026-10-15  None -> 'High'  (North Central rating (cfa-state))
    K118 2026-10-15  None -> 'Moderate'  (North East rating (cfa-state))
    L118 2026-10-15  None -> 'Moderate'  (East Gippsland rating (cfa-state))
    M118 2026-10-15  None -> 'Moderate'  (West and South Gippsland rating (cfa-state))
    O118 2026-10-15  None -> 'High'  (South West rating (cfa-state))
    D118 2026-10-15  None -> 'Yes'  (Multi Day (derived))
    C118 2026-10-15  None -> 'Yes'  (Total Fire Ban)
    P118 2026-10-15  None -> '[FDR] Central: High until 14:40, Extreme from 14:40 (amended on the day); rating left blank for: Central'  (Comments)
  REVIEW 2026-10-15: Central (N118): more than one rating — left blank for manual entry (the workbook has no matching combined value; ...)
  106 started day(s) have no stored data (01/07/2026 .. 14/10/2026) and are left blank — CFA publishes no archive, so only days collected by this tool (or corrected by hand) can be filled
Backup of the original: fdr_backups/FDR Daily Statistics Spreadsheet - 2026-27.20261015_144005.xlsx
(exit 2 — written, one item for a person)

$ python -m app.modules.fdr update-excel --workbook ...   # again
Wrote 0 cell(s); 0 left alone; 1 need review.

$ python -m app.modules.fdr show --date 2026-10-15
2026-10-15 (today) TFB=YES ['Mallee', 'Wimmera'] Multi=YES (derived)
  Mallee                     Extreme        confirmed    TFB=YES rev=1
  ...
  Central                    High/Extreme   confirmed    TFB=NO rev=2 High until 14:40, Extreme from 14:40 (amended on the day)
  South West                 High           confirmed    TFB=NO rev=1
```

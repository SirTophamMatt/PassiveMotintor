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
| CFA per-district feeds — `…/rssfeed/<slug>-firedistrict_rss.xml` | Yes, same family | One district each | Optional cross-check (off; all 9 URLs confirmed live) |
| BoM | Fire weather products exist, but no FDR/TFB source was verified | — | Not used |
| VicEmergency | Incidents/warnings GeoJSON (already collected by the Fire module); no FDR/TFB in it | — | Not used |

**Verified against the live feed (15 Oct 2026)** — a copy is committed as
`tests/fixtures/cfa_tfbfdr_live_20261015.xml` (+ the Central district feed):

* **5 items**: today + 4 days, titled `Thursday, 15 October 2026`.
* Each item: a statewide sentence (`Today, Thu, 15 Oct 2026 is not currently a
  day of Total Fire Ban.`), then one TFB line per district
  (`Central: NO - RESTRICTIONS MAY APPLY`), then `Fire Danger Ratings` /
  `Bureau of Meteorology forecast issued at: Thursday, 15 October 2026 05:30
  AM`, then `District: RATING` lines (`MODERATE`, `NO RATING`, …).
* **The 5th day has no "forecast issued at" line and is all `NO RATING`** — a
  placeholder before the Bureau has forecast it. It is recognised and NOT
  stored; it is collected once it carries an issue time.
* **The BoM issue time is the real forecast time** and is stored as
  `issue_ts`; an amendment is timed by it (falling back to when the tool saw
  it).
* District feeds (`<slug>-firedistrict_rss.xml`) carry one district, the TFB
  sentence only, and a legend line "Displays when Total Fire Ban in force"
  that appears on EVERY item — it is ignored, never read as a ban.
* Not yet seen live: a **Total Fire Ban day** (wording assumed
  `District: YES - TOTAL FIRE BAN IN FORCE`; anything other than a clear YES/NO
  is UNKNOWN and flagged) and a **multi-rating value** (none published).
* **CFA terms:** the feeds are for personal, non-commercial use, must be used
  as provided, and a website showing them should link back to CFA (the page
  does). Catalogue licence metadata disagrees (CC-BY 3.0 vs 4.0); CFA's own
  terms are the stricter, so they are what this tool follows.

Gaps that matter:

* **No archive.** CFA publishes today + 3 days only. Past ratings cannot be
  fetched, so **backfill can only re-apply what this tool already stored**
  (or what someone enters as a correction). Nothing is reconstructed.
* **Issue time.** The channel `pubDate` is just when CFA regenerated the
  feed (every minute); the forecast's own time is the "forecast issued at"
  line. Revisions are detected by **content**; both the issue time and "when
  we saw it" are recorded.
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
2. **An amendment during the day** — this is what the workbook's own note on
   Multi Day asks ("YES/NO - did the FDR change during the day"). The rating
   current at midnight differs from one issued after `multi_amendment_after`
   (06:00); a revision issued before 06:00 is the day's forecast being
   finalised, not a second rating. Times are the BoM "forecast issued at"
   time (else when the tool first saw it).

**The convention (confirmed 15 Oct 2026):** the rating cell records the **most
recent** rating, and Comments records what it was before and **when it
changed**: `[FDR] Central: High → Extreme at 13:30 (changed during the day)`
(several changes chain: `… → Extreme at 13:30 → Catastrophic at 15:10`).
Multi Day is written `YES` as soon as a change is confirmed — it cannot
un-happen — and `NO` only once the day is over with no change.

`multi_rating_display` = `latest` (default). Two ratings published at the
same moment for one district (not seen live) have no "latest": the workbook's
own combined dropdown value is used if it has one, otherwise the cell is left
for a person and Comments says why. Other settings: `highest`, `combined`,
`auto`, `review`.

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

### The real workbook (FDR Daily Statistics Spreadsheet - 2026-27.xlsx)

Checked on the copy supplied 15 Oct 2026 (`python -m app.modules.fdr inspect`):

* `Sheet1`; a summary table in rows 2–8 (`=COUNTIF(G12:INDEX(G:G,…), C3)` per
  rating × district) that also names every district — skipped because it has
  no Date column; the data header is **row 11**; dates 1 Jul 2026 – 30 Jun
  2027 in E12:E376.
* Columns: C `TFB declared (Yes/No)` · D `Multi Day` (note: "did the FDR change
  during the day") · E `Date` · G Mallee · H Wimmera · I South West · J Northern
  Country · K North Central · L Central · M North East · N West and South
  Gippsland · O East Gippsland · P Comments. (Not CFA's order — mapped by
  header.)
* Dropdowns live on the `List` sheet as Excel-extension validations:
  ratings `Pick Rating…. / No Rating / Moderate / High / Extreme /
  Catastrophic`, TFB and Multi Day `YES / NO`. **`Pick Rating….` is the
  prompt pre-filled in days not yet entered — treated as empty.**
* TFB column = **"is a Total Fire Ban declared?"** — `YES` if any district has
  one (`tfb_mode` `any`, confirmed 15 Oct 2026).
* Rows to 15 Oct were entered by hand. With the live feed of 15 Oct the tool
  planned **zero** changes — every value already matched. Simulating the next
  morning it wrote D118 (`NO`), C119 and G119:O119 into the prompts, changing
  only `sheet1.xml` and `workbook.xml`; recalculated in LibreOffice, the
  summary counts rose by exactly those nine ratings.
* A rating that changes during the day: latest rating in the cell, the change
  and its time in Comments (§4).
* **Days still to come** are written as CFA's current forecast with every value
  suffixed `(forecast)` — `Moderate (forecast)`, `NO (forecast)` — plus
  `[FDR] Forecast (BoM issued 15/10 05:30) — future day, may change` in
  Comments. The sheet's SEARCH-based colouring still colours them, but the
  summary's exact-match COUNTIFs do **not** count them (verified: totals
  unchanged after a forced recalculation), so the season statistics only ever
  contain days that happened. They follow CFA's updates, are not logged in
  `fdr_workbook_updates`, and are replaced by the confirmed values (logged)
  when the day starts. CFA's placeholder fifth day gets nothing. A value a
  person typed into a future row is left alone.

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

* Days that have started: confirmed values only; unknown stays blank.
* Days still to come: the current forecast, marked `(forecast)`, not logged,
  replaced when the day starts (`forecast_days`, `forecast_suffix`).
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
| `multi_rating_display` | `latest` | latest / auto / highest / combined / review |
| `tfb_mode` | `any` | any (YES if any district) / districts / count / auto |
| `forecast_days` / `forecast_suffix` | `true` / ` (forecast)` | write future days as marked forecasts |
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

1. **Verified against the live feed and the real workbook (15 Oct 2026)**, but
   no Total Fire Ban day or multi-rating value has been seen live yet; their
   wording is assumed and anything unclear is flagged, not guessed.
2. **Cells already filled by hand that differ from CFA** are never overwritten
   (flagged instead) — correct them, or rerun with `--force` deliberately.
3. **Forecast values sit outside the dropdown list** (`Moderate (forecast)`):
   Excel accepts them (validation only checks typed entries) but "Circle
   Invalid Data" would circle them — intended, they are not final values.
4. **Same-day amendment times** use the BoM "forecast issued at" time when the
   feed gives one, else when the tool noticed (to the nearest collection
   interval).
5. **No historical backfill from CFA** — only from this tool's own history.
6. District feeds: all nine URLs confirmed live (15 Oct 2026); off by default
   only because they repeat the statewide feed at nine times the requests.
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

# Deploying Passive Monitor on a Hostinger VPS

This runs the always-on web build behind HTTPS with an admin login. The
dashboards (Overview / Flood / Power) are public and read-only; Start/Stop,
Settings, Import, event tags and export sit behind the admin password.

## What you need

- A **Hostinger VPS** (KVM 2 — 2 vCPU / 8 GB — is comfortable if you run the
  power scraper's Chrome; a 1–2 GB plan is fine for flood-only).
- A **domain** (or subdomain) with an `A` record pointing at the VPS IP.
- Docker + Docker Compose on the VPS (Hostinger's "Ubuntu 22.04 with Docker"
  template ships them; otherwise `curl -fsSL https://get.docker.com | sh`).

## First deploy

```bash
# on the VPS, in a checkout of this repo's unified_monitor/ folder
cp .env.example .env
# edit .env: set a strong UM_ADMIN_PASSWORD and a random UM_SECRET_KEY
python3 -c "import secrets; print(secrets.token_hex(32))"   # paste into UM_SECRET_KEY

# put your domain in the Caddyfile (replace monitor.example.com)
nano Caddyfile

docker compose up -d --build
```

Caddy fetches a Let's Encrypt certificate on first request. Browse to
`https://your-domain` — the dashboards load immediately and **flood collection
auto-starts** (config `flood.autostart`).

## Turning on the power module

Power scraping needs working EM-COP credentials and a visible Chrome (provided
by Xvfb in the container). Once its `forbidden.seam` blocker is resolved:

1. Log in at `/admin` with your admin password.
2. Open **Settings**, enter EM-COP credentials, save.
3. On **Admin**, tick *Auto-start on server boot* for power (and *Start* it
   once to verify). Leave headless **off** — EM-COP drops headless sessions.

## Health checks

- `GET /health` returns JSON and HTTP 200 (or 503 if the DB is unreachable):
  `db_ok`, `flood_running`, `power_running`, `flood_last_heartbeat`, and the
  last collector errors. This distinguishes "web is up" from "still collecting".
- The container has a Docker healthcheck hitting `/health`; `docker compose ps`
  shows health, and `restart: unless-stopped` brings it back after a crash.
- **Add an external monitor** (UptimeRobot / Better Stack / Healthchecks.io)
  polling `https://your-domain/health` every 1–5 min for email/SMS alerts.

## Backups

Everything writable is on the mounted `./data` volume (`unified_monitor.db`,
`config.json`, `backups/`, log). The app also snapshots the DB into
`backups/` on each start (last 15 kept). Copy `./data` off-box periodically:

```bash
tar czf pm-backup-$(date +%F).tgz data/
```

## Updating

```bash
git pull
docker compose up -d --build
```

The DB and config persist in `./data`. Existing named flood events are migrated
into date-range **tags** automatically on first start of the new build, so past
incidents stay selectable on the Flood page.

## "No space left on device" during a build

Check the `load build context` line of the build: it should be a few MB. If it
is gigabytes, something from `./data` is being copied into the image — see the
comment at the top of `.dockerignore`. To recover disk after a failed build:

```bash
docker builder prune -af   # build cache, incl. the half-built layers
docker image prune -f      # superseded app images (each may hold a copy of the data)
df -h /
```

These never touch `./data` or Caddy's certificate volumes.

## Intel Tool password (Operational Summary)

The Operational Summary builder at `/intel/summary` is marked Official: Sensitive, so it
stays closed until the Intel Tool has a real password. Add to `.env`:

```bash
UM_INTEL_PASSWORD=something-strong
```

then `docker compose up -d`. Leaving it blank keeps the old default password for the fire
chart generator at `/intel`, and the summary page shows how to switch it on.

## Feedback to GitHub issues

Every report from the Feedback button can open an issue in the repository.

1. On GitHub: **Settings → Developer settings → Personal access tokens →
   Fine-grained tokens → Generate new token**. Resource owner: the repo's owner;
   **Only select repositories** → this repo; Repository permissions → **Issues:
   Read and write** (nothing else). Pick an expiry and note it — an expired
   token just means reports stop opening issues (they are still stored).
2. Add it to `.env`: `UM_GITHUB_TOKEN=github_pat_...` and run `docker compose up -d`.
3. Admin → Report delivery → **Check GitHub connection**. It reads the repo only
   (no test issue) and says whether the repository is PUBLIC.

The repository and the switches (issues on/off, include reporter name) are on the
Settings page. The reporter's email is never put in an issue; the report text is
shown in a code block so a public form cannot @-mention people or post links.
Each report stays in Admin → Feedback either way, with **Send to GitHub** to retry.

## Trying a branch before merging it

This runs the branch beside the live app on a **copy** of the database, with **no
collectors** (no second EM-COP login, no extra BoM load), reachable only through an SSH
tunnel. Nothing in the live `./data` is touched.

```bash
# 1. In the live checkout: put the branch in a separate folder
git fetch origin
git worktree add ../pm-test origin/<branch-name>

# 2. Snapshot the live database (SQLite online backup, safe while running)
docker compose exec app python -c "import sqlite3; s=sqlite3.connect('/data/unified_monitor.db'); d=sqlite3.connect('/data/test-copy.db'); s.backup(d); d.close()"
mkdir -p ../pm-test/data
sudo mv data/test-copy.db ../pm-test/data/unified_monitor.db

# 3. Build and run the branch on localhost:8060 with collectors off
cd ../pm-test
docker build -t pm-test .
docker run --rm -d --name pm-test -p 127.0.0.1:8060:8050 \
  -v "$PWD/data:/data" -e UM_DATA_DIR=/data \
  -e UM_INTEL_PASSWORD='a-test-password' -e UM_SECRET_KEY=test \
  pm-test python -c "from app.factory import create_app; from waitress import serve; serve(create_app(autostart=False).server, host='0.0.0.0', port=8050)"
```

From your own computer: `ssh -L 8060:127.0.0.1:8060 you@your-vps`, then open
<http://localhost:8060>. Logs: `docker logs -f pm-test`.

Clean up afterwards (the live app is unaffected throughout):

```bash
docker stop pm-test
cd <live checkout> && sudo rm -rf ../pm-test && git worktree prune
docker image rm pm-test
```

## Flood-only (smaller/cheaper) variant

If you don't need power yet, you can shrink the image: delete the Chrome/Xvfb
lines from the `Dockerfile` and change the `CMD` to run `python run_web.py`
directly (no `xvfb-run`). A 1 GB VPS handles this easily.

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

## Flood-only (smaller/cheaper) variant

If you don't need power yet, you can shrink the image: delete the Chrome/Xvfb
lines from the `Dockerfile` and change the `CMD` to run `python run_web.py`
directly (no `xvfb-run`). A 1 GB VPS handles this easily.

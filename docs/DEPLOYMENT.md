# Deployment

Perch runs on a single VPS: Docker Compose for the three long-running
processes, systemd for making sure Compose itself survives a reboot,
and a nightly cron-equivalent (systemd timer) for backups. This
replaces the macOS-laptop + LaunchAgent setup described in the main
README, which was fine for development but is a single point of
failure with no auto-restart across a machine restart and no offsite
backup.

This is Phase 4 of the Perch build. `tradebot/api/` (Phase 6) doesn't
exist yet — add it to `docker-compose.yml` as a fourth service, same
shape as the three below, once it does.

## What's here

| File | Purpose |
|---|---|
| `Dockerfile` | One image for all three processes (they share code/deps; only the command differs) |
| `requirements.txt` | Pinned direct dependencies for the image |
| `docker-compose.yml` | worker / bot / runner services, `restart: unless-stopped`, a heartbeat healthcheck on the runner |
| `systemd/perch.service` | Brings the Compose stack up on boot |
| `systemd/perch-backup.{service,timer}` | Nightly SQLite backup via `scripts/backup.sh` |
| `scripts/backup.sh` | `.backup`-based dump of `journal.db`/`users.db`/`universe.db`, gzipped, rotated after 14 days by default |

## First-time VPS setup

Sizing: this is a light workload (three Python processes, SQLite, no
Postgres) — a 2 vCPU / 4GB VPS is comfortable headroom for hundreds of
users; resize later if the web dashboard (Phase 6) or Postgres
migration changes that.

1. Provision a VPS (any provider), Ubuntu 22.04+ or Debian 12+.
2. Install Docker + Compose plugin and sqlite3 (the last is for the
   *host-side* backup script, not just inside the container):
   ```bash
   curl -fsSL https://get.docker.com | sh
   apt-get install -y sqlite3
   ```
3. Create a non-root deploy user, add it to the `docker` group.
4. Clone the repo to `/opt/perch` (or adjust the paths in the two
   systemd unit files if you use a different path).
5. Place `.env` at `/opt/perch/.env` (see **Secrets** below) — never
   committed, already covered by `.gitignore`.
6. `cp systemd/perch.service systemd/perch-backup.* /etc/systemd/system/`
   then:
   ```bash
   systemctl daemon-reload
   systemctl enable --now perch.service
   systemctl enable --now perch-backup.timer
   ```
7. Verify: `docker compose ps`, `docker compose logs -f runner`, and
   the existing `scripts/status.sh` checklist from the README (still
   valid — it reads `data/heartbeat.json` and `data/incidents.jsonl`,
   which are bind-mounted the same way whether the processes run bare
   or in containers).

## Secrets

Four required today (`tradebot/vendors/alpaca.py`,
`tradebot/alerts.py`, `tradebot/telegram_bot/client.py`):

- `ALPACA_KEY_ID`, `ALPACA_SECRET_KEY` — Alpaca market data credentials.
- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` — the bot's own token and the
  ops channel it posts summaries/heartbeats to.

Rotation: generate the new credential at the provider first, update
`.env` on the VPS, then `docker compose up -d` (recreates the affected
containers only; unaffected services are left running). There is no
in-place secret reload — a restart is required and is the intended
behavior, same as it is on bare metal today.

Do not put secrets in `docker-compose.yml` itself (`env_file: .env`
keeps them out of both the compose file and `docker compose config`
output going to shell history).

## Backups

`scripts/backup.sh` runs nightly at 03:00 UTC via
`perch-backup.timer`, and can be run by hand any time:
```bash
BACKUP_DIR=/opt/perch/backups scripts/backup.sh
```
It writes gzip'd, timestamped copies of the three SQLite databases to
`$BACKUP_DIR` (`./backups` by default) and deletes anything older than
`RETAIN_DAYS` (14 by default). It does **not** copy off-box on its own
— point `BACKUP_DIR` at a mounted remote volume, or add an rsync/rclone
line once a specific destination (S3, Backblaze, another host) is
chosen; deliberately not hard-coded to one vendor here.

**Restore:**
```bash
systemctl stop perch.service         # stop the containers writing to these files
gunzip -c backups/journal_<stamp>.db.gz > data/journal.db
gunzip -c backups/users_<stamp>.db.gz > data/users.db
gunzip -c backups/universe_<stamp>.db.gz > data/universe.db
systemctl start perch.service
```

## Updating a deployed version

```bash
cd /opt/perch
git pull
docker compose up -d --build
```
`restart: unless-stopped` plus `depends_on` means `bot`/`runner` don't
need to be stopped by hand — Compose recreates whichever service's
image actually changed.

## Known gap

There is currently no automated TLS-terminating reverse proxy in this
compose file — not needed yet, because nothing here serves HTTP.
Add one (e.g. Caddy or nginx, as its own compose service) when the
Phase 6 internal API and web dashboard exist and need a public,
TLS-covered endpoint.

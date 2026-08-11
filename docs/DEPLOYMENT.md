# Deployment

Perch runs on a single VPS: Docker Compose for the long-running
processes, systemd for making sure Compose itself survives a reboot,
and a nightly cron-equivalent (systemd timer) for backups. This
replaces the macOS-laptop + LaunchAgent setup described in the main
README, which was fine for development but is a single point of
failure with no auto-restart across a machine restart and no offsite
backup.

## What's here

| File | Purpose |
|---|---|
| `Dockerfile` | One image for worker/bot/runner/api (they share code/deps; only the command differs) |
| `requirements.txt` | Pinned direct dependencies for the image |
| `docker-compose.yml` | worker / bot / runner / api / caddy services, `restart: unless-stopped`, a heartbeat healthcheck on the runner |
| `Caddyfile` | Reverse proxy + automatic TLS for `api.perchmarkets.com`, proxying to the `api` service |
| `systemd/perch.service` | Brings the Compose stack up on boot |
| `systemd/perch-backup.{service,timer}` | Nightly SQLite backup via `scripts/backup.sh` |
| `scripts/backup.sh` | `.backup`-based dump of `journal.db`/`users.db`/`universe.db`, gzipped, rotated after 14 days by default |

## First-time VPS setup

Sizing: this is a light workload (a handful of Python processes, SQLite,
no Postgres) — a 2 vCPU / 4GB VPS is comfortable headroom for hundreds
of users; resize later if a Postgres migration changes that.

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
6. **DNS, before the stack comes up**: point `api.perchmarkets.com` at
   the VPS's IP (an A record in Cloudflare, the same place
   perchmarkets.com's other DNS already lives). Set this record to
   **DNS only** (grey cloud, not proxied) — Caddy needs to complete its
   own ACME (Let's Encrypt) challenge and terminate TLS itself; routing
   it through Cloudflare's proxy first would fight that. `app.` (the
   dashboard, once it's deployed) is separate — that one *should* stay
   proxied through Cloudflare, since it's a static site on Cloudflare
   Pages, not something this VPS serves.
7. `cp systemd/perch.service systemd/perch-backup.* /etc/systemd/system/`
   then:
   ```bash
   systemctl daemon-reload
   systemctl enable --now perch.service
   systemctl enable --now perch-backup.timer
   ```
8. Verify: `docker compose ps`, `docker compose logs -f runner`,
   `docker compose logs caddy` (should show a successful certificate
   issuance for `api.perchmarkets.com`), `curl https://api.perchmarkets.com/healthz`
   (`{"ok": true}`), and the existing `scripts/status.sh` checklist from
   the README (still valid — it reads `data/heartbeat.json` and
   `data/incidents.jsonl`, which are bind-mounted the same way whether
   the processes run bare or in containers).

## Secrets

Four required today (`tradebot/vendors/alpaca.py`,
`tradebot/alerts.py`, `tradebot/telegram_bot/client.py`):

- `ALPACA_KEY_ID`, `ALPACA_SECRET_KEY` — Alpaca market data credentials.
- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` — the bot's own token and the
  ops channel it posts summaries/heartbeats to.

Two more, needed once the web dashboard's magic-link login goes live
(`tradebot/email_sender.py`) — until both are set, magic links are
logged instead of emailed (`DevEmailSender`), which is fine for local
dev but not for a real user trying to sign in from perchmarkets.com:

- `RESEND_API_KEY` — from the Resend dashboard.
- `RESEND_FROM_EMAIL` — must be an address on a domain verified in
  Resend (e.g. `login@perchmarkets.com`); verification means adding the
  DNS records Resend gives you to perchmarkets.com's zone (in Cloudflare,
  since that's already where the marketing site's DNS lives) — a
  one-time manual step, not something any script here does.

Three more, for the `api` service (`tradebot/api/app.py`) — required
for a real deployment; the code falls back to insecure dev defaults if
they're unset, which is only fine for local development:

- `SESSION_SECRET_KEY` — signs the login session cookie. Generate one
  with `python3 -c "import secrets; print(secrets.token_hex(32))"` and
  never reuse the built-in dev default in production.
- `FRONTEND_URL` — where the magic-link email points people (its own
  confirmation screen, `web-app/src/components/VerifyMagicLink.jsx`,
  is what actually POSTs the token back to `/auth/magic-link/verify`
  once they press the button there — that route itself is a same-
  origin JSON POST, not something a plain emailed link can trigger on
  its own), and the primary origin the API's CORS headers allow. Set
  to `https://app.perchmarkets.com` once the dashboard is deployed
  there (Phase 6); until then, magic-link login has nowhere real to
  send people. See `DEV_CORS_ORIGIN` below for also trusting a local
  frontend dev server.
- `SESSION_COOKIE_DOMAIN` — set to `.perchmarkets.com` so the session
  cookie `api.perchmarkets.com` sets is also sent on requests from
  `app.perchmarkets.com`. Leave unset for local development (host-only
  cookie).

One more, local development only — never set in production:

- `DEV_CORS_ORIGIN` — an extra origin (e.g. `http://localhost:5173`)
  the API's CORS headers trust, on top of `FRONTEND_URL`. Unset means
  only the real frontend origin is ever trusted — a local dev origin
  used to be hardcoded in and trusted unconditionally, in production
  too; this is opt-in now.

Local dev over plain `http://localhost` needs one more override: the
session cookie is `Secure` by default (required in production, since
Caddy always terminates real TLS), and browsers won't store a `Secure`
cookie on a non-HTTPS origin other than `localhost`/`127.0.0.1`. Set
`SESSION_COOKIE_SECURE=0` when running `tradebot/api/app.py` locally
against a non-localhost hostname; leave it unset (defaults to on)
everywhere else.

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

The web dashboard itself (`web-app/`, a static Vite/React app talking
only to `api.perchmarkets.com`) doesn't exist yet — this covers the
backend (`tradebot/api/`) and its public, TLS-covered endpoint. Once
the dashboard is built, deploy it to Cloudflare Pages (same place the
marketing site already lives) at `app.perchmarkets.com`, and set
`FRONTEND_URL`/`SESSION_COOKIE_DOMAIN` above so the API will accept
requests from it.

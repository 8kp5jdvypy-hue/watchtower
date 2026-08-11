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
| `scripts/backup.sh` | `.backup`-based dump of `journal.db`/`users.db`/`universe.db`, gzipped, rotated after 14 days by default. Also ships `journal.db`/`users.db`/`.env` off-box, GPG-encrypted, once `/opt/perch/.backup-env` is configured — see Backups below |

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

Three more, for the `api` service (`tradebot/api/app.py`):

- `SESSION_SECRET_KEY` — signs the login session cookie. **Required —
  `create_app()` raises and the process refuses to start without it.**
  No insecure fallback: this used to default to a hardcoded dev key,
  which — in a public repo — meant a single missing env var was a full
  account-takeover away. Generate one with
  `python3 -c "import secrets; print(secrets.token_hex(32))"`.
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
`RETAIN_DAYS` (14 by default).

**These local backups are not enough on their own** — they live on the
same disk as the data they're backing up, so a VPS or disk failure
takes out both simultaneously. `scripts/backup.sh` also ships
`journal.db`, `users.db`, and `.env` off-box (universe.db is skipped —
cheaply rebuildable, see the script's own comment), GPG-encrypted
before upload, once the setup below is done. Opt-in: with neither of
the two env vars below set, off-box shipping is silently skipped and
nothing changes from today's local-only behavior.

### Off-box setup (one-time)

**Provider: DigitalOcean Spaces**, recommended — same provider as the
VPS already, no new vendor relationship or billing account, and it's
S3-compatible so `rclone` needs no DO-specific code. Any other
S3-compatible provider works the same way; swap the endpoint in
`rclone.conf` below.

1. **Create a Space** in the DigitalOcean control panel (Spaces Object
   Storage → Create), any region — doesn't need to match the droplet's
   region for a backup destination. Note the region slug (e.g. `nyc3`)
   and the Space's name.
2. **Generate Spaces access keys** (API → Spaces Keys → Generate New
   Key). Save the access key and secret key somewhere safe — this is
   the one time DigitalOcean shows the secret key.
3. **Install rclone on the VPS** (not in a container — `backup.sh` runs
   on the host, see `systemd/perch-backup.service`):
   ```bash
   curl https://rclone.org/install.sh | sudo bash
   ```
4. **Configure rclone** — `rclone config` and choose: `n` (new remote),
   name it `do-spaces`, type `s3`, provider `DigitalOcean`, paste the
   access key / secret key from step 2, endpoint
   `<region>.digitaloceanspaces.com` (e.g. `nyc3.digitaloceanspaces.com`),
   leave the rest at defaults. This writes
   `~/.config/rclone/rclone.conf` — if `perch-backup.service` runs as a
   different user than the one who ran `rclone config`, either run it
   as that user or set `RCLONE_CONFIG` to point at wherever it landed.
5. **Generate a backup encryption passphrase**, separate from every
   other credential in this project on purpose — leaking `.env` must
   not also hand over the key that decrypts the `.env` backups:
   ```bash
   python3 -c "import secrets; print(secrets.token_urlsafe(32))" > /opt/perch/.backup-passphrase
   chmod 600 /opt/perch/.backup-passphrase
   ```
   Store a copy of this passphrase somewhere outside the VPS too (a
   password manager) — losing it makes every off-box backup unreadable,
   same failure mode the off-box copy exists to prevent for the
   databases themselves.
6. **Point `perch-backup.service` at both**, via a small dedicated env
   file (kept separate from the main `.env` — this is backup
   *configuration*, not an application secret):
   ```bash
   cat > /opt/perch/.backup-env <<'EOF'
   RCLONE_REMOTE=do-spaces:perch-backups
   BACKUP_ENCRYPTION_PASSPHRASE_FILE=/opt/perch/.backup-passphrase
   EOF
   chmod 600 /opt/perch/.backup-env
   ```
   `perch-backup.service` already has `EnvironmentFile=-/opt/perch/.backup-env`
   (the leading `-` makes it optional, so the service still runs
   local-only backups if this file doesn't exist yet).
7. **Verify**: `systemctl start perch-backup.service` (runs immediately,
   doesn't wait for 03:00 UTC), then `journalctl -u perch-backup.service -n 20`
   — should show `shipped off-box to do-spaces:perch-backups: ...`, and
   `rclone ls do-spaces:perch-backups` should list the encrypted files.

### Restore

**Local (no off-box involved):**
```bash
systemctl stop perch.service         # stop the containers writing to these files
gunzip -c backups/journal_<stamp>.db.gz > data/journal.db
gunzip -c backups/users_<stamp>.db.gz > data/users.db
gunzip -c backups/universe_<stamp>.db.gz > data/universe.db
systemctl start perch.service
```

**Off-box (the actual disaster-recovery path — the one that matters if
the VPS itself is gone):**
```bash
mkdir -p /tmp/restore && cd /tmp/restore
rclone copy do-spaces:perch-backups/journal_<stamp>.db.gz.gpg . --config ~/.config/rclone/rclone.conf
rclone copy do-spaces:perch-backups/users_<stamp>.db.gz.gpg . --config ~/.config/rclone/rclone.conf
rclone copy do-spaces:perch-backups/env_<stamp>.gpg . --config ~/.config/rclone/rclone.conf

gpg --batch --yes --pinentry-mode loopback --passphrase-file /opt/perch/.backup-passphrase -d journal_<stamp>.db.gz.gpg > journal_<stamp>.db.gz
gpg --batch --yes --pinentry-mode loopback --passphrase-file /opt/perch/.backup-passphrase -d users_<stamp>.db.gz.gpg > users_<stamp>.db.gz
gpg --batch --yes --pinentry-mode loopback --passphrase-file /opt/perch/.backup-passphrase -d env_<stamp>.gpg > .env.restored

systemctl stop perch.service
gunzip -c journal_<stamp>.db.gz > /opt/perch/data/journal.db
gunzip -c users_<stamp>.db.gz > /opt/perch/data/users.db
cp .env.restored /opt/perch/.env   # review before overwriting a live .env — see below
systemctl start perch.service
rm -rf /tmp/restore                # don't leave decrypted secrets/data on disk
```
Restoring `.env` onto a VPS that still has a working `.env` is rarely
what you want (it would roll back credentials that may have since
rotated) — this path is really for rebuilding on a *new* VPS after the
old one is gone, where there's no existing `.env` to conflict with.

**Tested, 2026-08-12**: the SQLite backup/restore mechanics
(`.backup` → `gzip` → `gunzip`, verified via `PRAGMA integrity_check`
and an exact row-count match against a real, non-trivial database)
were run end-to-end and confirmed correct. The GPG encrypt/decrypt and
`rclone` upload/download legs use standard, well-documented commands
but have **not** been run against a real DigitalOcean Spaces bucket —
neither `gpg` nor `rclone` exist in the environment this was built in.
**Before trusting this for real disaster recovery, run the off-box
restore commands above once, for real**, against a test file, and
update this note with the date. A backup nobody has restored from is a
hope, not a backup — this note exists so that's never quietly assumed
true.

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

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
   proxied through Cloudflare, since it's a Workers static-assets
   Worker (`perch-dashboard`), not something this VPS serves.
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
GIT_SHA=$(git rev-parse --short HEAD) docker compose up -d --build
```
`GIT_SHA` is a plain shell variable for this one command, read by
Compose's `${GIT_SHA:-unknown}` build-arg substitution (see
`docker-compose.yml`) — it never touches the app's own `.env` file.
Baked into the image so `journal.code_version()` (every detection row's
"what code produced this" stamp) has a real value in-container instead
of falling back to `"unknown"` every time, which is what actually
happened in production before 2026-08-12 — the image never had `.git`
to shell out to. Skipping the `GIT_SHA=...` prefix still works, it just
silently reverts to that same `"unknown"`.

`restart: unless-stopped` plus `depends_on` means `bot`/`runner` don't
need to be stopped by hand — Compose recreates whichever service's
image actually changed.

## Frontend deploys (Cloudflare Workers) — two separate Workers, easy to conflate

Everything above this section is the VPS/backend only
(`tradebot/api/` at `api.perchmarkets.com`). The two frontends are
**separate Cloudflare Workers, on separate projects, with separate
deploy mechanisms** — confusing one for the other cost a real deploy
window (2026-08-12: 40 minutes spent because this wasn't written
down). Check the Cloudflare dashboard's project name, not just "did
something deploy," before trusting any frontend change is live:

| | `watchtower` | `perch-dashboard` |
|---|---|---|
| **Serves** | `perchmarkets.com` (marketing/landing, `web/`) | `app.perchmarkets.com` (the authenticated subscriber dashboard, `web-app/`) |
| **Deploy trigger** | Auto-builds from GitHub on push, but a build still needs a **manual promote** in the Cloudflare dashboard to go live | Manual only — `wrangler deploy` from a developer's machine, no GitHub connection at all |
| **This matters for** | Landing-page/marketing copy changes | **Every subscriber-facing dashboard change** — signal cards, copy fixes, badges, anything in `web-app/src/` |

**If you just shipped a fix to `web-app/` (the actual product surface
subscribers use), `watchtower` promoting is irrelevant to it and will
not make your change live — only a `perch-dashboard` wrangler deploy
does.**

### Deploying `perch-dashboard` (`app.perchmarkets.com`)

From `web-app/` (see that directory's own `README.md`, which already
had this written down — this section exists so it's also findable from
the ops doc, not just the frontend's):

```bash
npx wrangler login          # only if `npx wrangler whoami` shows not logged in
VITE_API_URL=https://api.perchmarkets.com npm run build
npx wrangler deploy
```

`VITE_API_URL` is baked into the JS bundle at build time, not read at
runtime — omitting it silently falls back to `http://localhost:8000`
(see `src/api.js`), which is a real footgun: the build still succeeds
and deploys fine, it just points the live dashboard at a local API
that doesn't exist from a subscriber's browser. Always set it
explicitly for a production build; never trust an unset default.

**Verify the deploy actually took** (Vite content-hashes asset
filenames, so this proves *which* build is live, not just that
something responded):
```bash
curl -s https://app.perchmarkets.com/ | grep -o 'assets/index-[A-Za-z0-9_-]*\.\(js\|css\)'
```
Compare the printed filenames against what your own `npm run build`
(with `VITE_API_URL` set, same as above) just produced locally in
`dist/assets/`. Match means the deploy landed; mismatch means it
didn't, regardless of what the terminal said.

### Frontend cache behavior (Workers static assets: no purge, ever)

Both frontends are Cloudflare **Workers with static assets** and no
Worker script (each `wrangler.toml` has only an `[assets]` block), and
that changes which caches are even in the request path. Verified
against Cloudflare's docs and against live behavior on 2026-08-15;
each claim below cites its source.

- **The zone HTTP cache is not in front of these responses.** Zone
  Cache Rules, cache-level settings, and Purge Everything configure the
  *zone's* cache, which Worker responses never pass through: "Workers
  Caching is your Worker's cache, not your zone's cache… None of the
  following applies: Cache Rules…"
  (developers.cloudflare.com/workers/cache/limitations/). A Bypass
  rule for these hostnames is inert — one exists in the zone (named
  `app HTML bypass`, created 2026-08-15 while chasing exactly this)
  and was verified live to have no effect. Safe to delete; harmless to
  keep in case either hostname ever points at a real cacheable origin.
- **The `cf-cache-status: HIT` on responses is the Workers assets
  platform's own cache** — `CF-Cache-Status` is a default header of
  static asset serving, and the docs note it isn't always accurate
  (developers.cloudflare.com/workers/static-assets/headers/). That
  cache is version-scoped and content-addressed: every deploy uploads
  a manifest mapping each path to a file hash, tracked per Worker
  version (…/workers/static-assets/direct-upload/), and requests
  resolve against the currently deployed version's manifest. A
  `wrangler deploy` therefore supersedes the cache atomically.
  **Proven live 2026-08-15**: a `_headers`-only change was deployed
  with zero purge, and within seconds the live asset responses carried
  the new header. There is no purge step in this pipeline, ever — a
  `HIT` on `/` is the normal steady state, not a staleness signal.
- **`_headers` governs browser caching only** (there is no
  configuration surface for the platform's edge cache — the complete
  `assets` config is directory/binding/run_worker_first/html_handling/
  not_found_handling). It still matters: `Cache-Control: no-cache` on
  the HTML entry points (`/` and `/index.html`, named explicitly — the
  SPA has no other real document URLs) forces an etag revalidation (a
  cheap 304) in browsers, so a browser can never pin an old
  `index.html`; `public, max-age=31536000, immutable` on `/assets/*`
  and `/fonts/*` lets browsers keep content-hashed files forever. Keep
  the rules mutually exclusive: when several `_headers` rules match
  one request, same-named headers CONCATENATE rather than override (a
  `/*` catch-all briefly shipped assets with "no-cache, public,
  max-age=31536000, immutable" stacked in one header).
- **The hash check above is the definitive deploy verification.** It
  proves which build is actually being served end-to-end, which
  subsumes every caching question — trust it over any cache header.

**What actually caused the historical "stale deploy" incidents** (two
deploys that appeared to serve the old `index.html` until a manual
Purge Everything): not the zone cache, which was never in the path.
The real culprits: (a) the old `/*` `_headers` rule stamped the
year-long `max-age` onto the HTML itself, so *browsers* held the
previous `index.html` — Purge Everything got the credit while
cache-bypassing re-checks (fresh curls, hard refreshes) did the work;
and (b) for `watchtower`, a pushed build that was never **promoted**
(see below) isn't live at all, which reads as "stale" from outside.
Both are closed by the current `_headers` files and the hash-check
habit.

### Deploying `watchtower` (`perchmarkets.com`)

Push to the branch its GitHub build watches, then **manually promote**
the resulting build in the Cloudflare dashboard (Workers & Pages →
`watchtower` → Deployments) — a merge alone does not make it live.

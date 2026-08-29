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
| `Dockerfile` | One image for worker/bot/runner/postmarket shadows/api (they share code/deps; only the command differs) |
| `requirements.txt` | Pinned direct dependencies for the image |
| `docker-compose.yml` | worker / bot / runner / postmarket shadows / default-off customer-readiness dry run / api / caddy services with restart policies and market-aware healthchecks |
| `docs/postmarket-discovery-daily-audit.md` | Immutable daily coverage, provenance, lifecycle, and conservation audit for market-wide discovery |
| `docs/postmarket-signal-quality-preflight.md` | Read-only exact-revision, database, backup, control, credential, and licensed-reference preflight before the complete shadow stack is deployed |
| `Caddyfile` | Reverse proxy + automatic TLS for `api.perchmarkets.com`, proxying to the `api` service |
| `systemd/perch.service` | Brings the Compose stack up on boot |
| `systemd/perch-backup.{service,timer}` | Nightly SQLite backup via `scripts/backup.sh` |
| `scripts/backup.sh` | Verified online snapshots of all five durable SQLite databases plus immutable postmarket audits/evidence, SHA-256 manifesting, 14-day rotation, and encrypted off-box shipping of irrebuildable state — see Backups below |
| `scripts/verify_backup.py` | Digest-check and isolated restore of one manifest-bound backup set; rejects missing databases, corrupt SQLite, and unsafe artifact archives |
| `scripts/fetch_cache.py`, `scripts/purge_and_backfill_runts.py`, and other `scripts/*.py` tools | Not in the image (see Dockerfile) and need the app's real deps — see "Running `scripts/` tools in-container" below for the one correct invocation |

The `postmarket-discovery` and `postmarket-external-context` health probes are
intentionally stricter than the basic postmarket-window probe. When their kill
switches are enabled, they require fresh heartbeats all day: finalized outcome,
census, context, lifecycle, rank, and provider-proof maintenance runs outside
the active window, while option expectation capture begins before the close.
Both probes require the running revision and observer identity to match.
Discovery also fails on any explicit subsystem `error`. A `degraded` evidence
result remains visible but does not trigger a restart loop; missing market data
is not the same as a dead worker.

The `postmarket-customer-dry-run` service is independently default-off and has
no delivery/outbox dependency. When explicitly enabled, its health probe
requires a fresh matching-revision heartbeat all day. Enabled startup also
requires exact policy and dry-run owner-authorization files; the discovery
heartbeat must be fresh and clean before any row is classified eligible.
Routine deployment must leave `POSTMARKET_CUSTOMER_DRY_RUN_ENABLED=0` until the
evidence campaign, policy, and dry-run authorization have been separately
reviewed. Even enabled mode records readiness decisions only and cannot contact
customers.

Before deploying the complete signal-quality stack, run the fail-closed
preflight in `docs/postmarket-signal-quality-preflight.md`. A safe shadow
verdict is distinct from full evidence-campaign readiness, and neither enables
customer delivery.

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

Optional shadow-evidence credentials:

- `MASSIVE_API_KEY` — candidate-level Massive REST price/reference context.
- `MASSIVE_S3_ACCESS_KEY_ID`, `MASSIVE_S3_SECRET_ACCESS_KEY` — dedicated
  Massive flat-file credentials for the next-day full-universe provider proof.
  These are distinct from the REST key and from ordinary AWS credentials. If
  absent, the proof reports `unconfigured`; it does not silently reuse Alpaca.

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
It writes gzip'd, timestamped online snapshots of `journal.db`, `users.db`,
`evaluations.db`, `postmarket_shadow.db`, and `universe.db` to `$BACKUP_DIR`
(`./backups` by default). All five are required; a missing one makes the job
fail loudly. Although the asset catalog inside `universe.db` is rebuildable,
its Stage-1 screening ticks and per-symbol decisions are not.

The job also archives `data/postmarket_audits/` and
`data/postmarket_evidence/` when present. Every SQLite snapshot passes
`PRAGMA quick_check`; the artifact archive is rejected if it contains a
symlink, special file, absolute path, traversal, or unexpected root. A
`manifest_<stamp>.sha256` binds every file in that timestamped set. Local
retention removes only recognized database, artifact, and manifest filenames
older than `RETAIN_DAYS` (14 by default).

**These local backups are not enough on their own** — they live on the
same disk as the data they're backing up, so a VPS or disk failure
takes out both simultaneously. `scripts/backup.sh` also ships
`journal.db`, `users.db`, `evaluations.db`, `postmarket_shadow.db`,
`universe.db`, the postmarket artifact archive, the set manifest, and `.env`
off-box, GPG-encrypted before upload, once the setup below is done. With
`RCLONE_REMOTE` unset, the job is
explicitly local-only. If a remote is configured but its encryption key is
missing, the job fails instead of silently downgrading to local-only custody.
The encrypted off-box manifest is generated from that exact remote payload.
The remote must include a non-empty path (`remote:bucket-or-prefix`); remote-root
retention is refused.

The isolated restore verifier remains backward-compatible with historical
off-box sets created before `universe.db` became mandatory. Those sets restore
the four databases they actually contain, but they are not valid inputs to the
current signal-quality campaign preflight because their Stage-1 screening
evidence is absent.

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

**Verify and stage a local backup set (no live files are replaced):**
```bash
python3 scripts/verify_backup.py \
  backups/manifest_<stamp>.sha256 \
  /tmp/perch-restore-<stamp>
```

The command verifies every manifest digest before creating the destination,
decompresses into an isolated staging directory, runs `PRAGMA quick_check` on
every restored database, safely extracts postmarket artifacts, and writes
`restore_report.json`. It refuses an existing destination and leaves live
`data/` untouched. Review the report and restored contents before any disaster
recovery cutover.

**Install a reviewed staged restore during an actual recovery:**

```bash
systemctl stop perch.service
install -m 0644 /tmp/perch-restore-<stamp>/data/journal.db data/journal.db
install -m 0644 /tmp/perch-restore-<stamp>/data/users.db data/users.db
install -m 0644 /tmp/perch-restore-<stamp>/data/evaluations.db data/evaluations.db
install -m 0644 /tmp/perch-restore-<stamp>/data/postmarket_shadow.db data/postmarket_shadow.db
install -m 0644 /tmp/perch-restore-<stamp>/data/universe.db data/universe.db
[[ ! -d /tmp/perch-restore-<stamp>/data/postmarket_audits ]] || \
  cp -a /tmp/perch-restore-<stamp>/data/postmarket_audits/. data/postmarket_audits/
[[ ! -d /tmp/perch-restore-<stamp>/data/postmarket_evidence ]] || \
  cp -a /tmp/perch-restore-<stamp>/data/postmarket_evidence/. data/postmarket_evidence/
systemctl start perch.service
```

Only use the installation block after resolving the exact timestamp and
reviewing the isolated restore. It is intentionally not automated by the
verifier.

**Off-box (the actual disaster-recovery path — the one that matters if
the VPS itself is gone):**
```bash
RESTORE_WORKDIR=$(mktemp -d /tmp/perch-offbox-restore.XXXXXX)
cd "$RESTORE_WORKDIR"
rclone copy do-spaces:perch-backups/journal_<stamp>.db.gz.gpg . --config ~/.config/rclone/rclone.conf
rclone copy do-spaces:perch-backups/users_<stamp>.db.gz.gpg . --config ~/.config/rclone/rclone.conf
rclone copy do-spaces:perch-backups/evaluations_<stamp>.db.gz.gpg . --config ~/.config/rclone/rclone.conf
rclone copy do-spaces:perch-backups/postmarket_shadow_<stamp>.db.gz.gpg . --config ~/.config/rclone/rclone.conf
rclone copy do-spaces:perch-backups/universe_<stamp>.db.gz.gpg . --config ~/.config/rclone/rclone.conf
rclone copy do-spaces:perch-backups/postmarket_artifacts_<stamp>.tar.gz.gpg . --config ~/.config/rclone/rclone.conf
rclone copy do-spaces:perch-backups/manifest_<stamp>.sha256.gpg . --config ~/.config/rclone/rclone.conf
rclone copy do-spaces:perch-backups/env_<stamp>.gpg . --config ~/.config/rclone/rclone.conf

gpg --batch --yes --pinentry-mode loopback --passphrase-file /opt/perch/.backup-passphrase -d journal_<stamp>.db.gz.gpg > journal_<stamp>.db.gz
gpg --batch --yes --pinentry-mode loopback --passphrase-file /opt/perch/.backup-passphrase -d users_<stamp>.db.gz.gpg > users_<stamp>.db.gz
gpg --batch --yes --pinentry-mode loopback --passphrase-file /opt/perch/.backup-passphrase -d evaluations_<stamp>.db.gz.gpg > evaluations_<stamp>.db.gz
gpg --batch --yes --pinentry-mode loopback --passphrase-file /opt/perch/.backup-passphrase -d postmarket_shadow_<stamp>.db.gz.gpg > postmarket_shadow_<stamp>.db.gz
gpg --batch --yes --pinentry-mode loopback --passphrase-file /opt/perch/.backup-passphrase -d universe_<stamp>.db.gz.gpg > universe_<stamp>.db.gz
gpg --batch --yes --pinentry-mode loopback --passphrase-file /opt/perch/.backup-passphrase -d postmarket_artifacts_<stamp>.tar.gz.gpg > postmarket_artifacts_<stamp>.tar.gz
gpg --batch --yes --pinentry-mode loopback --passphrase-file /opt/perch/.backup-passphrase -d manifest_<stamp>.sha256.gpg > manifest_<stamp>.sha256
gpg --batch --yes --pinentry-mode loopback --passphrase-file /opt/perch/.backup-passphrase -d env_<stamp>.gpg > .env.restored

python3 /opt/perch/scripts/verify_backup.py \
  manifest_<stamp>.sha256 \
  /tmp/perch-restore-<stamp>
```
Review the isolated restore report before installing data or `.env`. Do not
delete the staged evidence until recovery verification is complete.
Restoring `.env` onto a VPS that still has a working `.env` is rarely
what you want (it would roll back credentials that may have since
rotated) — this path is really for rebuilding on a *new* VPS after the
old one is gone, where there's no existing `.env` to conflict with.

The local online-snapshot, compression, digest, five-database restore,
`PRAGMA quick_check`, artifact recovery, and traversal/tamper rejection paths
are exercised end-to-end in the automated test suite. The GPG encrypt/decrypt
and `rclone` upload/download legs use standard commands and simulated integration
coverage but have **not** been run against a real DigitalOcean Spaces bucket.
**Before trusting this for real disaster recovery, run the off-box
restore commands above once, for real**, against a test file, and
update this note with the date. A backup nobody has restored from is a
hope, not a backup — this note exists so that's never quietly assumed
true.

## Updating a deployed version

Every source/image deployment goes through `scripts/deploy.sh`. The wrapper
requires the full 40-character revision and refuses a dirty checkout, a stale
or non-main revision, and an unreviewed rollback target.

For the one-time rollout of the wrapper itself, update the checkout without
touching the running containers, then invoke it:

```bash
cd /opt/perch
git fetch origin main
REVISION="$(git rev-parse origin/main)"
git checkout --detach "$REVISION"
scripts/deploy.sh "$REVISION"
```

For later releases the current deployed checkout already contains the wrapper:

```bash
cd /opt/perch
git fetch origin main
REVISION="$(git rev-parse origin/main)"
scripts/deploy.sh "$REVISION"
```

The wrapper takes a verified predeploy backup, checks out exactly that commit,
installs the repository's systemd units, binds the seven-character revision to
Compose's `GIT_SHA` build argument, waits for the full stack, verifies the
reported revision in every Python service, runs `PRAGMA quick_check` against
all five production databases, checks the public API, and takes a verified
postdeploy backup. Any failed gate exits nonzero and names the failed phase.

An explicit rollback uses the same complete gate and accepts only an ancestor
of current `origin/main`:

```bash
scripts/deploy.sh --rollback <full-40-character-ancestor-sha>
```

Do not run raw `docker compose up -d --build` for a source release. Compose's
fallback remains `GIT_SHA=unknown`, so bypassing the wrapper destroys revision
attribution. `systemd/perch.service` intentionally runs `docker compose up -d`
without `--build`: boot supervision restarts the exact images produced by the
last verified deployment and can never silently rebuild them as `unknown`.

## Running `scripts/` tools in-container

Neither of the two obvious ways to run a `scripts/*.py` tool on this box
works out of the box:

- **Bare host `python3`** doesn't have the app's dependencies —
  `requirements.txt` is only ever `pip install`ed inside the Docker
  build (see `Dockerfile`), never on the host itself. This is why a
  script fails with `ModuleNotFoundError: exchange_calendars` (or any
  other dependency) when run directly on the VPS.
- **`docker compose exec runner python3 scripts/foo.py`** fails
  differently — the image only `COPY`s `tradebot/` (see `Dockerfile`);
  `scripts/`, `docs/`, and `.git` are deliberately not in it. `exec`
  also can't fix this even with `-v`: it attaches to the *existing*,
  already-running container, whose mounts were fixed at `docker compose
  up` time — you cannot add a new bind mount to a container that's
  already running.

**The one correct invocation** — a fresh, one-off container built from
the same image/config as the `runner` service (so it has the real deps
and `.env`), with the host's `scripts/` bind-mounted in just for this
run:

```bash
cd /opt/perch
docker compose run --rm -v /opt/perch/scripts:/app/scripts runner \
  python3 scripts/<name>.py [args...]
```

`run` (not `exec`) is what makes the extra `-v` possible — it creates a
new container rather than attaching to the long-running restart-always
one, so `--rm` afterward leaves nothing behind and the live `runner`
process is never touched. Add `-e SOME_VAR=value` before `runner` to
override or add an env var for just that one run (`.env` still loads
normally via the service's own `env_file:`).

This is also how the July SIP backfill was originally done — the
invocation just wasn't written down anywhere until now (`docs/BACKLOG.md`
tracked that as a known gap since 2026-08-12). The same pattern works
for any current or future `scripts/` tool, not just the two below. Use
it as the one invocation path, not a per-script special case.

### Ship #1 (P5b) example: runt purge + SIP backfill

```bash
cd /opt/perch

# 1. Report which cached sessions actually fail the plausibility floor
#    (docs/open-awareness-proposals-2026-08.md, Proposal 5c) -- no files
#    touched yet.
docker compose run --rm -v /opt/perch/scripts:/app/scripts runner \
  python3 scripts/purge_and_backfill_runts.py

# 2. Delete exactly the files the report named.
docker compose run --rm -v /opt/perch/scripts:/app/scripts runner \
  python3 scripts/purge_and_backfill_runts.py --apply

# 3. Refetch under SIP -- also backfills each symbol up to 20 SIP
#    sessions total (fetch_cache.py's default --sessions-n), which is
#    what Proposal 1/2's baselines need.
docker compose run --rm -v /opt/perch/scripts:/app/scripts \
  -e DETECTOR_DATA_FEED=sip runner \
  python3 scripts/fetch_cache.py --sessions-n 20
```

Two standing warnings for any manual cache operation on this box
(the mechanism behind the 2026-08-12 incident is now fixed at the code
level, but these are still true operationally):

- **Never delete a current trading day's own intraday cache file before
  that day's `backfill_marks()` has run** at session close — it's the
  only source `backfill_marks()` reads to compute AFTER DETECTION
  outcomes for that session's detections.
- **`fetch_cache.py` cannot refetch the current day at all** — its
  session walk-back deliberately starts at `date.today() - 1` (today's
  cache is instead written by the live pipeline's own close-time
  fetch, `runner._cache_todays_intraday_bars`). Don't expect step 3
  above to touch today's file, on today or any day.

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

## Google Search Console (perchmarkets.com)

One-time setup (already done, recorded here so it isn't chat-only):

1. Go to search.google.com/search-console → **Add property**.
2. Choose the **Domain** property type and enter `perchmarkets.com` —
   a Domain property covers every subdomain, so `app.perchmarkets.com`
   is included automatically.
3. Google gives you a TXT record. Add it in **Cloudflare DNS** for the
   `perchmarkets.com` zone (Type: TXT, Name: `@`, Content: the
   `google-site-verification=...` string).
4. Back in Search Console, click **Verify**. DNS propagation can take
   a few minutes; retry if it fails on the first attempt.

After any landing deploy: **URL Inspection** →
`https://perchmarkets.com/` → **Request Indexing**, so Google picks up
the new build promptly rather than on its own crawl schedule.

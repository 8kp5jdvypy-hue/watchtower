# Operational resilience review

Read-only review: what happens when things fail, and who notices. Not a
code-correctness review — see the separate security review for that.
Findings ranked by (likelihood × severity). Each has a one-line proposed
fix and effort estimate; nothing here is implemented. The top 3 are
flagged as recommended next work once the SIP migration ships.

## Top 3 (recommended next work)

**Status (2026-08-12):** #1 is implemented — off-box backup shipping
(gpg-encrypted, rclone to DigitalOcean Spaces) plus a documented
restore procedure, branch `ops-offbox-backups`, pending review/merge.
#2 and #3 are queued as next-up work below (not implemented) — approved
to be *specified* now, not built yet.

### 1. Backups live on the same disk they're backing up

**Likelihood: medium (VPS/disk loss is uncommon but real on a single
droplet) · Severity: catastrophic (total, unrecoverable data loss)**

`scripts/backup.sh:27` defaults `BACKUP_DIR` to `$REPO_ROOT/backups` —
i.e. `/opt/perch/backups`, the same disk as `data/journal.db`,
`data/users.db`, `data/universe.db`. `docs/DEPLOYMENT.md:144-147` and
the script itself are explicit that off-box shipping is a deliberately
unimplemented manual step — no rsync/rclone/S3 code exists anywhere in
the repo. **If the VPS or its disk dies right now, the daily 03:00 UTC
backups die with it.** Everything is lost simultaneously: every
subscriber account, every trade journal entry, every Telegram user's
limits/watchlist/settings, and the entire journaled history
`SCANNER_PLAN.md`'s track-record numbers are computed from. `.env` is
also never backed up anywhere in-repo — a fresh VPS would need every
credential re-obtained from wherever (if anywhere) the operator
separately stored them.

**Proposed fix:** add one rsync/rclone/scp line to `backup.sh` (or a
second small script + cron/timer) shipping the gzip'd backups to a
second location — even the operator's own Mac via `scp` is a real
improvement over "same disk." Separately, back up `.env` to a password
manager or similar (not into the repo, not into the same on-box
`backups/` dir unencrypted).
**Effort: small** (a few lines in `backup.sh` + choosing a destination;
no application code changes).

### 2. A hung (not crashed) worker silently disables both alert delivery and the deadman switch at once

**Likelihood: medium (hangs — deadlocks, stuck network calls — are a
real failure mode distinct from crashes, and `worker.py` has no
timeout/liveness self-check) · Severity: high (total silent alerting
failure, potentially for an entire session or longer)**

The outbox worker (`tradebot.telegram_bot.worker`) is simultaneously
the *only* process that delivers Telegram messages and the *only*
process that pages on a stale runner heartbeat
(`worker.py:210-248`). It has **no `healthcheck:`** in
`docker-compose.yml` (only `runner` has one), so `restart:
unless-stopped` — which only reacts to process **exit**, not a hang —
does nothing for it. A hung worker means: no HIGH alerts reach
subscribers, no MEDIUM digest goes out, *and* the one mechanism that
would otherwise page the operator about the runner being stale is
itself the thing that's stuck. Nothing else in the stack would notice
until the operator manually checks Telegram or inspects the `outbox`
table.

**Proposed fix:** add a `healthcheck:` to the `worker` service —
e.g. touch a liveness timestamp file once per drain loop, healthcheck
reads its age (same pattern `runner`'s healthcheck already uses for
`heartbeat.json`). Pairs naturally with finding #3 below.
**Effort: small** (one small code change to touch a file periodically
in the worker's loop + a `healthcheck:` block mirroring the existing
`runner` one).

**Next-up spec (queued, not implemented):**
- `tradebot/telegram_bot/worker.py`: in the main drain loop, touch
  `data/worker_heartbeat.json` (mirroring `runner.py`'s
  `heartbeat.json` write) once per pass — a plain `{"ts": ...}` write
  is enough, no new dependency.
- `docker-compose.yml`: add a `healthcheck:` block to the `worker`
  service reading that file's age, same shape as `runner`'s existing
  one (`docker-compose.yml:53-66`) — same staleness threshold (900s)
  unless the worker's normal loop cadence argues for a different
  number.
- No change to the deadman-switch logic itself (`worker.py:210-248`)
  — this only gives Docker a way to *notice* the worker's own hang,
  which today it structurally cannot.

### 3. Docker's healthcheck has no autoheal — "unhealthy" is observability, not recovery

**Likelihood: medium-high (this is the exact case the healthcheck was
added for — a hang, not a crash) · Severity: high (the one automated
detection mechanism that exists doesn't actually recover anything)**

`runner`'s `healthcheck:` (`docker-compose.yml:53-66`) correctly
detects a stale heartbeat (>900s) and marks the container `unhealthy`
— but Compose does not restart a container for being unhealthy, only
for exiting. The compose file's own header comment (`docker-compose.yml:4-5`)
implies `restart: unless-stopped` **is** "this deployment's watchdog,"
which is true for crashes but not for the hang scenario the healthcheck
exists to catch. Today, an unhealthy-but-still-running `runner` just
sits there until a human runs `docker compose ps` and notices.

**Proposed fix:** add a small autoheal companion container (e.g. the
well-known `willfarrell/autoheal` image) that restarts any container
Docker marks unhealthy. One new service block in `docker-compose.yml`,
no application code changes. Combined with #2's new worker healthcheck,
this closes the loop for both known hang scenarios at once.
**Effort: small** (one new service block; no code changes beyond #2's).

**Next-up spec (queued, not implemented):**
- `docker-compose.yml`: add an `autoheal` service (`willfarrell/autoheal`
  image), mounted against `/var/run/docker.sock`, scoped via
  `AUTOHEAL_CONTAINER_LABEL` (or per-container `autoheal: "true"`
  labels on `runner` and, once #2 lands, `worker`) so it only restarts
  the two services with real healthchecks — not `bot`/`api`/`caddy`,
  which don't have one and shouldn't be auto-restarted on a false
  signal.
- Depends on #2 for the `worker` half of the benefit; the `runner`
  half is usable standalone today since `runner` already has a
  healthcheck.

## Everything else found, ranked

### 4. Runner failures outside RTH are never paged

**Likelihood: medium (a pre-open startup failure has real precedent —
recent history includes fixes for restart-loop and heartbeat-threshold
bugs) · Severity: medium-high (a full session's alerts silently missed,
discovered only the next morning)**

The heartbeat staleness page is gated to RTH only (`worker.py:218`,
`is_rth_fn`). A runner that fails to start before the open, or dies
overnight, pages nobody until the operator happens to check.
**Proposed fix:** add a narrower pre-open liveness check (e.g. "is the
runner container running and has it written a heartbeat by 9:35 ET") —
doesn't need the full RTH staleness machinery, just a once-daily check.
**Effort: medium** (new, fairly targeted logic; more than a one-liner
but well-scoped).

### 5. Backups are untested — restore has never been verified

**Likelihood: medium · Severity: medium-high (could discover a broken
restore exactly when it's needed most)**

`docs/DEPLOYMENT.md:149-156` documents restore as manual commands only;
no script, test, or CI job exercises it. Combined with finding #1, this
means the backup strategy's actual recovery capability is unverified
end to end.
**Proposed fix:** run the documented restore procedure once, by hand,
against a copy of real backup files, and note the date it was last
verified somewhere durable (e.g. `docs/PROGRAM-STATE.md`).
**Effort: small** (no code — one manual dry run).

### 6. `data/cache/` grows unboundedly, on a VPS with no documented disk size

**Likelihood: low-medium near-term (slow growth), rising over the life
of the deployment · Severity: medium (eventual disk-full failure, no
early warning)**

`fetch_cache.py` is purely additive — confirmed no deletion/pruning
logic anywhere in the file. `docs/DEPLOYMENT.md` documents CPU/RAM
sizing but never disk size. Nothing monitors disk usage.
**Proposed fix:** either a cheap disk-usage check added to `status.sh`/the
heartbeat page, or a retention policy in `fetch_cache.py` mirroring
`backup.sh`'s `RETAIN_DAYS` pattern.
**Effort: small–medium** (status.sh disk check is small; cache pruning
logic is a bit more).

### 7. Total Telegram outage has no alternate notification channel

**Likelihood: low (Telegram is generally reliable) · Severity: high
when it happens (fully silent — even the deadman switch has nowhere
else to send its page, `worker.py:243-247`)**

Structural, not a quick code fix — this system has exactly one
notification channel by design.
**Proposed fix (partial, cheap):** point a free external uptime monitor
(e.g. UptimeRobot or similar) at `/healthz` as a second, independent
channel that doesn't depend on this stack's own Telegram delivery
working. Doesn't cover "Telegram is down but the API is fine," but
covers "the whole stack is down" independently of Telegram.
**Effort: trivial** (operational setup, no code).

### 8. `status.sh`'s process checks are silently meaningless under Docker, and the deployment doc says otherwise

**Likelihood: medium (confusing next time someone runs it) · Severity:
low (misleading, not dangerous — heartbeat/incident reads still work)**

`status.sh`'s worker/bot/runner up/down lines read `data/*.pid`
(`scripts/status.sh:18-33`), written only by `scripts/start.sh`'s
bare-metal flow. Docker Compose invokes `python -m tradebot...` directly
as each container's command — no pidfiles are ever written under the
Docker deployment, so those lines would read "DOWN" regardless of real
status. `docs/DEPLOYMENT.md:60-63` asserts `status.sh` is "still valid"
on the VPS, true only for the heartbeat/incident parts.
**Proposed fix:** either branch `status.sh` to check `docker compose
ps` when running under the VPS deployment, or narrow the doc's claim to
say which parts of the checklist still apply.
**Effort: small.**

### 9. Total Alpaca outage alerts once per symbol per loop, undeduped

**Likelihood: low · Severity: low-medium (not silent, but noisy enough
to risk alert fatigue or muting)**

Confirmed the runner does **not** fail silently on a total data-provider
outage — `runner.py:1129-1140` catches and alerts per symbol per
~5-minute loop pass, unthrottled. 17 watchlist symbols means 17 alerts
every 5 minutes for the duration of an outage. Not a detection gap, but
worth a dedup pass so a real outage doesn't get muted by whoever's on
the receiving end.
**Proposed fix:** collapse to one alert per outage window (e.g. same
dedup/cooldown pattern already used for detector alerts) rather than
one per symbol.
**Effort: small.**

## What's confirmed working, not a gap

- The heartbeat deadman switch itself (`worker.py:210-248`) is real,
  correctly implemented, and pages within ~15-20 minutes of a genuine
  RTH staleness — assuming the worker is alive (see #2).
- Backup rotation (`RETAIN_DAYS=14`) and the timer schedule
  (`03:00 UTC`, `Persistent=true`) are both real and correctly wired,
  contingent on the timer actually being installed (unverifiable from
  the repo alone).
- `fetch_cache.py` is confirmed not automated anywhere (no cron/systemd/launchd
  reference in the repo) — a known, not a hidden, manual step.

## What can't be verified without server access

- Whether `perch-backup.timer` is actually installed and enabled on the
  real VPS.
- Whether backups are actually accumulating and rotating in practice.
- Real disk size and current `data/cache`/database sizes on the VPS
  (this review's on-disk measurements are from a local dev checkout,
  not production).
- Whether `TELEGRAM_CHAT_ID` and other required secrets are correctly
  set in the real `/opt/perch/.env`.
- Whether any external uptime monitor is already configured against
  `/healthz` outside this repo.

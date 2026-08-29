# State of the system

Fresh-eyes handoff doc, written 2026-08-12 from a cold read of the repo:
`docs/PROGRAM-STATE.md`, `docs/ROADMAP.md`, `docs/BACKLOG.md`,
`docs/full-code-review.md`, `docs/DEPLOYMENT.md`, and the last ~20
commits on `main`. Every claim below is either sourced to a specific
file/commit, or explicitly marked as inference/unverifiable — see
section 5 for the honest limits of a repo-only read.

**Refreshed 2026-08-15** (current HEAD: `bec981d`). The original was
written at `eaf7a67` and had fallen ~11 PRs behind. Two things changed
in kind, not just in detail:

- **All five CRITICAL findings are now resolved** (section 4), the last
  one — the cross-database alert-before-commit bug — by PR #36.
- **This document is no longer a repo-only read.** The 2026-08-15 sweep
  hit the live sites over HTTP and rebuilt both frontends locally to
  compare against what's deployed, so several items section 5 listed as
  structurally unknowable are now answered. What remains unknowable is
  narrower, and still marked as such.

---

## 1. What Perch is

Perch (repo name `watchtower`, product name `perchmarkets.com`) is a
market-scanning and alerting service: it watches a fixed watchlist of
US equities intraday, runs a set of statistical detectors (volume
spikes, relative-strength breaks, etc. — `tradebot/detectors.py`)
against live bar data, and when a detector fires it journals the
detection to a SQLite database and pushes a Telegram alert to
subscribers. Every alert is tracked forward against what the price
actually did afterward (+15/+30/+60 min and session close), and that
outcome history is surfaced back to users on an authenticated web
dashboard (`app.perchmarkets.com`) and, per the roadmap, will become a
public, unedited weekly track-record recap — the core positioning bet
(`docs/ROADMAP.md`) is "the honest one": no fabricated data, no
cherry-picked track record, real prices and real misses shown
side-by-side. It is explicitly not a trading terminal — read-only
views, no order placement (`web-app/README.md`).

---

## 2. Current live state

### What's verifiable from the repo alone

- **Current `main` HEAD**: `bec981d` (2026-08-15, PR #39). The last
  *code* changes are PR #37 (universe delist guard) and PR #36
  (cross-database alert atomicity); the last product feature is PR #31
  (Trade Journal).
- **Detector feed**: `tradebot/vendors/alpaca.py:44` resolves
  `DETECTOR_DATA_FEED` from an environment variable, defaulting to
  `"iex"` if unset. The repo does not commit `.env`, so which value is
  actually set on the VPS today is not verifiable from git — see
  §2 "SIP feed migration" and §5 below.
- **Two separate Cloudflare Workers exist in-repo**, confirmed by
  their own `wrangler.toml` files:
  - `watchtower` (root `wrangler.toml`, serves `web/dist`) — the
    marketing/waitlist site, intended for `perchmarkets.com`.
  - `perch-dashboard` (`web-app/wrangler.toml`) — the authenticated
    dashboard (Today/Watchlist/Signals/Performance), intended for
    `app.perchmarkets.com`, talking only to `api.perchmarkets.com`
    (`web-app/README.md`).
  - `docs/DEPLOYMENT.md`'s stale "dashboard doesn't exist yet" section
    is **fixed** — PR #22 merged 2026-08-14, and PR #35 later corrected
    the caching half of that same section (see below).
- **VPS deployment shape**: `docs/DEPLOYMENT.md` describes a single
  VPS running `worker`/`bot`/`runner`/`api`/`caddy` via Docker Compose,
  with `systemd` units keeping the stack up across reboots and a
  nightly backup timer. This is the documented *design*, verified
  against the actual `docker-compose.yml`, `Dockerfile`, and
  `systemd/*` files in the repo — it matches what's committed.
- **`GIT_SHA` build-arg fix landed** (`4bb2fb4`): the deployed Docker
  image now bakes `GIT_SHA` at build time so
  `journal.code_version()` (the per-detection "what code produced
  this" stamp) has a real value instead of `"unknown"`. Per the commit
  message, `"unknown"` is what actually happened in every production
  detection row before 2026-08-12.
- **Security posture on `main`**: `SESSION_SECRET_KEY` is required at
  startup with no insecure fallback (`2534139`, confirmed present —
  `create_app()` raises without it, per `docs/DEPLOYMENT.md:88-93`),
  and the magic-link verify endpoint no longer accepts a bare GET
  (`56eaa29`, CSRF fix).

### Verified live over HTTP, 2026-08-15

Not repo claims — these were measured:

- **Both frontends are serving current `main`.** Local builds of `web/`
  and `web-app/` (with `VITE_API_URL` set) produced exactly the asset
  hashes the live sites serve. No drift between deployed and source.
- **The frontends are Workers static-assets Workers, and the zone cache
  is not in their request path.** `cf-cache-status: HIT` on both
  surfaces comes from the Workers assets platform's own cache, which is
  version-scoped and content-addressed; a `wrangler deploy` supersedes
  it atomically. Proven live: a `_headers`-only deploy with zero purge
  flipped the served headers within seconds. **There is no purge step in
  this pipeline**, and a zone Cache Rule created against these hostnames
  is inert. See `DEPLOYMENT.md`'s "Frontend cache behavior".
- **The landing's favicon was uncrawlable** (a `data:` URI, which
  Googlebot-Image cannot fetch) — the reason search results showed a
  generic globe for `perchmarkets.com` while `app.` showed the mark.
  Fixed in PR #38; **that fix requires a manual Promote to go live**,
  since `watchtower` is Promote-only.

### What's asserted in-repo but not independently verifiable by this read

- `docs/PROGRAM-STATE.md` (dated 2026-08-11) states the live-quotes
  path is "live in production... confirmed live-verified against
  both [VPS and Worker], not inferred from git history" — this is a
  **claim written by whoever authored that doc**, not something this
  read re-verified (no SSH access, no live HTTP check performed).
  Treat it as high-confidence secondhand testimony from inside the
  repo, not a fact this document independently confirmed.
- `TODO.md` states the VPS is a DigitalOcean droplet at
  `67.207.83.138` running all 5 services, and that the dashboard is
  live at `app.perchmarkets.com`. Same caveat — this is the repo's own
  operator checklist, not something re-verified live.
- **SIP feed migration status**: `docs/BACKLOG.md`'s opening line
  ("compiled 2026-08-12, from the night of the SIP flip and the
  backfill incident") and `b813185`'s commit message ("the
  post-migration stats reset... means these aggregates are near-empty
  right now") both describe the *detector* feed as **already flipped
  to SIP in production**, on top of the *display* feed
  (`fetch_latest_quote`), which `docs/PROGRAM-STATE.md` says has been
  SIP for longer. But since `DETECTOR_DATA_FEED` is an env var never
  committed to the repo, this read cannot confirm the VPS's `.env`
  actually has `DETECTOR_DATA_FEED=sip` set right now — it can only
  confirm the **code supports the flip via one config value**
  (`docs/sip-migration-proposal.md`'s stated design goal) and that
  multiple committed docs/commits describe the flip as already having
  happened. Also worth noting: `docs/ROADMAP.md`'s "Now" section still
  lists "Week 2 (Aug 18-24) — SIP cutover" as upcoming, which reads in
  tension with BACKLOG.md's "night of the SIP flip" — the roadmap may
  simply be stale relative to the faster-than-planned actual cutover,
  but this read can't resolve which document is authoritative.

---

## 3. What changed recently (from commit history, not speculation)

### Since this document was first written (2026-08-13 → 08-15)

The original's list picks up below; these landed after it:

1. **Trade Journal** (PR #31) — schema, `/journal` API, dashboard UI,
   signal linking, tests. The largest product addition since the
   dashboard itself. Its `_detection_snapshot()` deliberately tolerates
   a missing journal row, citing the cross-DB atomicity finding — that
   tolerance is now belt-and-braces rather than load-bearing, since
   PR #36 closed the hole underneath it.
2. **August 2026 design review + hotfixes** (PRs #30, #33) — portal
   overlays, the sign-convention cluster, hero padding, trend labels.
3. **Caching contract, twice** (PRs #34, #35) — #34 made the `_headers`
   rules mutually exclusive so assets emit one `Cache-Control` instead
   of a stacked, self-contradicting pair; #35 then corrected the whole
   model after live investigation (zone Cache Rules are inert for
   Workers static assets; `_headers` governs browser caching only;
   deploys supersede the assets cache atomically; no purge, ever).
4. **Backup crash fix** (PR #28) — `backup.sh` used a bare `$HOME`,
   which is unset for a systemd unit with no `User=`, so the off-box
   shipping step crashed under `set -u`. Off-box backups were failing
   nightly until this landed.
5. **Heartbeat threshold** — `HEARTBEAT_STALE_SECONDS` raised from 5min
   to 15min. The old value equalled the runner's own bar cadence, i.e.
   zero margin; a validation run paged 12 times in 68 minutes with zero
   real incidents.
6. **CRITICAL #5 closed** (PR #36) — a Telegram alert could be durably
   enqueued before the detection row it references committed to
   `journal.db`, so a crash in that window delivered a real subscriber
   alert citing a `detection_id` that never existed (and broke the
   alert's own "I took this" button). Now routed through
   `_commit_then_send()`.
7. **Universe mass-delist guard** (PR #37) — an empty or truncated
   vendor fetch would have marked the entire scan universe delisted in
   one call; the delisting pass is now skipped, loudly, below a 50%
   plausibility floor.
8. **Landing favicon** (PR #38) — real crawlable square icon files
   replacing an uncrawlable `data:` URI. **Awaiting a Promote click.**

### The original 2026-08-12 reconstruction

Reconstructed from `git log` on `main`, most recent first:

1. **SIP feed migration executed** (context, not a single commit):
   `docs/sip-migration-proposal.md` and `docs/sip-decision-a-proposal.md`
   document the evidence-gathering (entitlement check, 83-session
   backtest, train/test threshold validation) behind moving the
   detector feed from IEX to SIP. PR #20 (`c81bcf5`, "Phase 3 bundle:
   data_feed/origin schema, stats filtering, RADAR labeling") landed
   the schema support: a `data_feed` column on `detections`, and
   `CURRENT_FEED_FILTER_SQL` (`tradebot/journal.py:52-54`) which
   excludes any detection row whose feed doesn't match the most
   recent one written — deliberately resetting the historical
   continuation-rate stats to near-zero rather than blending
   pre-/post-migration populations ("Decision B," documented inline
   in `journal.py:32-49`).

2. **A live incident**: at the 2026-08-12 16:00 ET session close,
   `backfill_marks()` wrote **0 marks for ~160 real detections**, with
   no error, no log line, and no alert — surfaced only because a
   subscriber noticed the dashboard stuck on placeholder "Pending"
   text (`0b03712`'s commit message).

3. **Same-night hotfix** (`0b03712`, PR #23,
   `backfill-marks-loud-hotfix`): made the failure *loud* rather than
   fixing its root cause yet — the live call site now captures
   `backfill_marks()`'s return value and alerts if marks written are
   implausibly low; `backfill_marks()` itself now distinguishes "no
   cached file" from "cached but nothing new"; `fetch_cache.py` now
   checks the real NYSE calendar instead of guessing "holiday?" for
   any zero-bar day. 12 new tests. Follow-up commits same night:
   `956525c` added the missing end-to-end test connecting both halves
   of that pipeline, and `b813185` separately fixed a sign-convention
   bug in `historical_performance()`'s `avg_return_pct` (found during
   the same overnight review, small and unrelated enough to ship
   independently).

4. **Structural fix** (`a92ec76`, PR #24,
   `backfill-structural-fix`): found and fixed the actual root cause —
   nothing in the live pipeline had ever written *today's own*
   intraday bars to the on-disk cache that `backfill_marks()` reads
   from (`scripts/fetch_cache.py`'s session walk-back structurally
   starts at `date.today() - 1`, so it can never cache "today").
   `run_live()` now fetches every symbol with a detection this session
   directly from the vendor and writes it to that cache path right
   before calling `backfill_marks()`, closing the gap for good. 18 new
   tests, including one that replays the exact starting conditions of
   the incident and proves marks now get written. Also baked `GIT_SHA`
   into the Docker image in the same window (`4bb2fb4`) so future
   incidents have an accurate "what code was running" stamp — the
   *previous* incident's own postmortem was hampered by
   `code_version()` returning `"unknown"`.

5. **Two quick corrections on the hotfix itself**, same night:
   `d7b2820` fixed the new cache-fetch alert to page only on *total*
   failure (0-of-N) rather than any single symbol's transient miss —
   caught during the author's own PR verification, not by an outside
   reviewer. `d802948` then made sure a large *partial* miss (e.g. 15
   of 33 symbols) still surfaces somewhere real (the heartbeat,
   unconditionally), since the commit message notes the existing
   "implausibly few marks" check isn't mathematically guaranteed to
   catch a partial failure.

6. **Overnight full-codebase review** (`5f1f370`, then compiled into
   `docs/BACKLOG.md` by `f64b3ba`, merged as PR #25): a read-only
   correctness review (702 tests green) surfaced 27 ranked findings —
   5 CRITICAL, 8 HIGH, 11 MEDIUM, 3 LOW — plus a 19-item "Silent
   Failure Inventory." Explicitly **not triaged yet** except for the
   two items already fixed same-night (the backfill-loudness chain,
   and the `historical_performance` sign bug). *(That was the state on
   2026-08-12. The findings were triaged by code re-read on 2026-08-15
   — see §4 for current statuses; all five CRITICALs are resolved.)*

Earlier same-window work still on `main` (2026-08-11, one day prior):
security fixes (`56eaa29` CSRF fix on magic-link verify, `2534139`
required-`SESSION_SECRET_KEY`), `docs/PROGRAM-STATE.md` and
`docs/ROADMAP.md` added (PRs #15, #18), copy fixes for dashboard
"Pending" outcomes and watchlist accuracy (PR #19), and five bugs
fixed from an earlier live-data dashboard review (PR #14).

---

## 4. What's open / needs fixing

Ranked by `docs/full-code-review.md`'s own severity plus
`docs/BACKLOG.md`'s operational list. Status is this read's own
assessment against current `main`, not a re-run of the review.

### CRITICAL (5 total, code-review numbering)

| # | Finding | Status |
|---|---|---|
| 1 | `run_live`'s `backfill_marks()` return value discarded | **Fixed** — `0b03712` captures it and alerts. |
| 2 | Missing cache file silently becomes `[]`, not an error | **Fixed** — `0b03712`/`a92ec76` make this loud and, per #3 below, prevent the missing-file case outright on the live path. |
| 3 | `fetch_cache.py`'s silent "no data (holiday?)" mislabels real fetch failures | **Fixed** — `0b03712` checks the real NYSE calendar. |
| 4 | No test for `backfill_marks()` with missing/empty cache dir | **Fixed** — `a92ec76`'s 18 new tests include the exact incident-shape case. |
| 5 | Telegram alert can be sent before its detection row commits to `journal.db` (cross-database ordering bug) | **CLOSED** (PR #36, 2026-08-15). `_commit_then_send()` commits journal.db before any send that could reference a detection, making "send without committing first" inexpressible in `process_new_bar` rather than a convention to remember. The exposure was narrower than this table originally implied — the trade path was already safe, since `record_contract_selection()` commits and flushes the pending INSERT with it — but the **NO TRADE** path was genuinely exposed, and NO TRADE is a common outcome. 6 tests; 4 fail against the pre-fix ordering. |

**All five CRITICALs are now resolved.** The former #3 remnant is also
closed: `scripts/fetch_cache.py` exits nonzero when a symbol ends with
`"gave up"` or a real-session empty fetch, while fully cached and holiday
no-ops remain successful. Cache writes use same-directory temporary files
and atomic replacement; regression tests cover partial-write cleanup.

Also worth stating plainly, since this document's own earlier revision
is the source of the confusion: **#1, #2 and #4 were already fixed by
the 2026-08-12/13 hotfix work while `BACKLOG.md` still described the
review as untriaged.** Only #5 was fixed after a sweep identified it as
the last one standing. Four of the five were closed before anyone
formally triaged the list.

### HIGH (8 total) — triaged 2026-08-15 by code re-read

- **#6** (empty vendor response mass-delists the universe) — the
  destructive half is **closed** (PR #37): the delisting pass is skipped
  below a 50% plausibility floor, with an ERROR naming both counts.
- **#7** (`historical_performance` sign convention) — **DONE** (PR #23),
  with regression tests asserting the sibling functions agree in sign.
- **#13** (`run_live`/`run_replay` coverage) — **DONE**: direct loop tests
  inject one symbol-evaluation failure in live and replay modes, prove every
  later symbol in that same pass is still evaluated, and assert the failure
  is retained in `HeartbeatStats.errors`.
- **#11** (partial cache file treated as complete) — **DONE**: atomic
  same-filesystem replacement and crash-path tests prevent a truncated final
  file from becoming the idempotency marker.
- **#12** (contract-forward vendor failure hidden as absence) — **DONE**:
  chain and day-range provider failures reach the per-contract logger;
  successful absent-contract/no-trade results remain non-fabricated absences.
- **#9** (ambiguous missing outcome marks) — **DONE**: an append-only
  per-checkpoint resolution ledger and bounded calendar-derived pre-backfill
  states now distinguish pending, waiting, unavailable, not reached, and
  delayed outcomes through the API and signal-detail UI.
- **#8, #10** — **still open**, each confirmed present in current `main`.

### MEDIUM (11) / LOW (3)

Not reviewed by a human yet per BACKLOG.md. The review's former
"Priority 2" time-alignment defect is now closed: `rvol_spike` uses
DST-aware RTH time slots and `relative_strength_break` uses exact timestamp
joins. A silently dropped mid-session bar therefore cannot shift every
subsequent baseline/proxy comparison; missing required timestamps abstain.
Dedicated detector regressions preserve both former false-signal cases.

### Operational backlog (`docs/BACKLOG.md`)

Statuses re-verified 2026-08-15:

| Item | Status |
|---|---|
| PR #22 — document the two Cloudflare Workers | **MERGED** 2026-08-14. |
| Off-box backups (GPG + rclone to DigitalOcean Spaces) | **MERGED** as PR #26. Remnant: the GPG/rclone restore legs have still never been run against a real bucket — `DEPLOYMENT.md` says so itself. A backup nobody has restored from is a hope, not a backup. |
| `backup.sh` crashing under systemd (unset `$HOME`) | **MERGED** as PR #28 — off-box backups were failing nightly before this. |
| Operational resilience review (deadman gap, autoheal) | **Still unmerged**, no PR. Branch `ops-resilience-review`. |
| `broad_scan` honesty proposal (labeling, stats exclusion) | **Still unmerged**, doc only. **Needs a decision, not a session.** |
| `deploy.sh` wrapper for the `GIT_SHA=...` invocation | **Open** — the convention was already forgotten once. |
| RFAMU-type thin-symbol policy | **Open, undecided** — a human decision, not code. |
| Near-close detection copy | **Open, not implemented.** |
| First-session observation report (against a real, cleanly-closed live SIP session) | **Still owed, not delivered.** |
| DEPLOYMENT.md gaps | **Partially closed** — the two-Workers split, the `GIT_SHA` line and the caching contract are now written (#22, #24, #35). The in-container `fetch_cache` invocation, the "never clear today's cache before backfill runs" warning, and the two-local-checkouts note remain unwritten. |
| Landing favicon (PR #38) | **Merged, not live** — needs a manual Promote in the Cloudflare dashboard, then a Search Console re-crawl request. |

---

## 5. Known gaps in this read's own knowledge

Explicit, not implied:

- **Still no VPS access.** Nothing here ran `docker compose ps` or hit
  `api.perchmarkets.com`. Concretely still unknown:
  - Whether `DETECTOR_DATA_FEED=sip` is actually set in the VPS's
    `.env` today (the code defaults to `iex` if unset — see §2).
  - Whether all 5 Compose services are currently `Up`, and which image
    `GIT_SHA` they were last built from.
- ~~Whether the two Cloudflare Workers are serving the latest built
  assets~~ — **answered 2026-08-15**: both serve exactly the asset
  hashes a local build of current `main` produces. See §2's live-verified
  block. (The favicon fix in PR #38 is the one known exception — merged
  but not yet promoted.)
- ~~Root cause of finding #5~~ — **answered**: traced through every
  write between the detection INSERT and the final commit. The trade
  path had incidental mitigation (`record_contract_selection()` commits,
  flushing the pending INSERT); the NO TRADE path had none. Fixed in
  PR #36.
- **Which document is authoritative where PROGRAM-STATE.md/BACKLOG.md
  and ROADMAP.md disagree on SIP-cutover timing** (§2) — this read
  flags the tension but cannot resolve it without asking whoever
  updates the roadmap.
- **No test suite or build was run** as part of producing this
  document — the "702 passed" figure is `full-code-review.md`'s own
  claim from its run, not reproduced here.
- **PR review status** (approvals, CI results, review comments) on the
  open/unmerged branches listed in §4 was not checked — only their
  existence as unmerged branches was confirmed via `git branch -a`.

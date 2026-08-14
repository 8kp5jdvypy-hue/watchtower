# State of the system

Fresh-eyes handoff doc, written 2026-08-12 from a cold read of the repo:
`docs/PROGRAM-STATE.md`, `docs/ROADMAP.md`, `docs/BACKLOG.md`,
`docs/full-code-review.md`, `docs/DEPLOYMENT.md`, and the last ~20
commits on `main` (current HEAD: `eaf7a67`, 2026-08-12, "Merge pull
request #25 from docs-review-and-backlog"). Every claim below is
either sourced to a specific file/commit, or explicitly marked as
inference/unverifiable — see section 5 for the honest limits of a
repo-only read.

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

- **Current `main` HEAD**: `eaf7a67796a44288d9702f471f2756b98391f09c`
  (2026-08-12), a docs-only merge (adds `BACKLOG.md` and
  `full-code-review.md`). The last *code* change on `main` is
  `d3d373e` (PR #24, "backfill-structural-fix").
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
  - `docs/DEPLOYMENT.md` on `main` still describes the dashboard as
    **not built yet** ("Known gap" section, line 179) — this is
    **stale**. The fix for this (PR #22,
    `docs-two-workers-clarification`) exists as an open, unmerged PR
    per `docs/BACKLOG.md` — confirmed still open via `git branch -a`.
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
   and the `historical_performance` sign bug).

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
| 5 | Telegram alert can be sent before its detection row commits to `journal.db` (cross-database ordering bug) | **Open, explicitly deferred.** `docs/BACKLOG.md` names this by hand as "explicitly queued for triage, not touched tonight on purpose." **This is the one CRITICAL still live** — a SIGKILL/OOM between the alert send and the journal commit currently can deliver an alert referencing a detection ID that then never exists. Flagging as genuinely risky: it's a data-integrity bug on the money/trust path (an alert a subscriber acts on could reference a row that silently vanishes), not just a UX gap. |

### HIGH (8 total) — all appear unreviewed/untriaged by a human per BACKLOG.md

None of findings #6–#13 (empty-vendor-response mass delisting, tier-performance's missing `news_driven` filter, frontend pending-state ambiguity, an admin-halt test that can't distinguish `chat_id` from `user_id`, non-atomic cache-file writes, a swallowed exception in options-chain fetch one layer below its own handler, and zero test coverage on the highest-blast-radius per-symbol error-isolation loop in `run_live`/`run_replay`) show any sign of having been touched since the review landed. `docs/BACKLOG.md` says explicitly: "Everything else in the review is unreviewed by a human; CRITICALs should come first" — meaning even finding #5 above hasn't been formally triaged yet, just named and consciously deferred.

Worth flagging as risky specifically: **#13** (zero test coverage on `run_live`/`run_replay`'s per-symbol exception isolation) — this is the mechanism that keeps one bad symbol from taking down the scan for every watched ticker, and it currently has no regression protection at all.

### MEDIUM (11) / LOW (3)

Not reviewed by a human yet per BACKLOG.md. Two categories worth a
maintainer's attention sooner rather than later, per the review's own
"Priority 2" section (separate from the ranked list): `rvol_spike` and
`relative_strength_break` key their historical baselines by **list
position**, not time-of-day — a silently dropped mid-session bar
permanently misaligns every subsequent comparison for the rest of that
session, with no error. Statistical, not a crash, but exactly the kind
of bug that wouldn't announce itself.

### Operational backlog (`docs/BACKLOG.md`)

| Item | Status |
|---|---|
| PR #22 — document the two Cloudflare Workers | **Open, unmerged** (confirmed via `git branch -a`: `docs-two-workers-clarification` exists as a branch, not merged into `main`). |
| Off-box backups (GPG + rclone to DigitalOcean Spaces) | **Not merged** — branch `ops-offbox-backups` (and a `-rebased` variant) exists but, per BACKLOG.md, no PR opened yet. Marked "top priority per standing instruction." |
| Operational resilience review (deadman gap, autoheal) | **Not merged** — branch `ops-resilience-review` exists, no PR yet. |
| `broad_scan` honesty proposal (labeling, stats exclusion) | **Doc only, no code** — branch `broad-scan-honesty-proposal` exists. |
| `deploy.sh` wrapper for the `GIT_SHA=... docker compose up -d --build` invocation | **Open** — BACKLOG.md notes the manual invocation "already was forgotten once tonight." |
| RFAMU-type thin-symbol policy (a symbol can fire a detection but be too thin for the vendor to backfill) | **Open, undecided** — confirmed real during the incident repair (1 of 33 symbols), not yet a policy decision either way. |
| Near-close detection copy ("Resolves after session close" shown for a detection that can never resolve) | **Open, not implemented.** |
| First-session observation report (signal-rate/volume-multiple/error-log checklist against a real, cleanly-closed live SIP session) | **Owed, not delivered** — BACKLOG.md is explicit that tonight's manual repair proved the fix works mechanically but is not a substitute for this. |
| DEPLOYMENT.md gaps (in-container `fetch_cache` invocation, the "never clear today's cache before backfill runs" warning, the two-local-checkouts note) | **Open**, listed item-by-item in BACKLOG.md. |

---

## 5. Known gaps in this read's own knowledge

Explicit, not implied:

- **No live access.** This read never connected to the VPS
  (`67.207.83.138` per `TODO.md`), never ran `docker compose ps`, and
  never hit `api.perchmarkets.com`, `app.perchmarkets.com`, or
  `perchmarkets.com` over HTTP. Every claim about what's *actually
  running right now* is sourced to what the repo's own docs/commits
  *say* is running, not independent confirmation. Concretely unknown:
  - Whether `DETECTOR_DATA_FEED=sip` is actually set in the VPS's
    `.env` today (the code defaults to `iex` if unset — see §2).
  - Whether all 5 Compose services are currently `Up`, and which
    image `GIT_SHA` they were last built from — i.e., whether the VPS
    is actually running `main`'s current HEAD or something a few
    commits behind it.
  - Whether the two Cloudflare Workers (`watchtower`,
    `perch-dashboard`) are serving the latest built assets, or are a
    few `npm run build && wrangler deploy` cycles behind their source
    branches.
- **Root cause of finding #5 (cross-DB alert-before-commit bug)** is
  understood mechanically from the review but this read did not trace
  every call site to confirm no other mitigating logic exists
  elsewhere in the codebase.
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

# Backlog

Compiled 2026-08-12, from the night of the SIP flip and the backfill
incident. Verified against actual repo/PR state before writing this
down, not from memory — see each item's status.

**Corrected 2026-08-15** against a full completion sweep (repo, live
sites, and code re-read finding by finding). Several items below were
already done when this file still called them open — those are marked
inline rather than deleted, so the record shows what was actually true
versus what this file claimed.

## Open PRs needing review/merge

- ~~**[#22](https://github.com/8kp5jdvypy-hue/watchtower/pull/22) —
  Document the two separate Cloudflare Workers.**~~ **MERGED**
  2026-08-14. `DEPLOYMENT.md` now carries the real `watchtower` vs
  `perch-dashboard` split.
- **[#32](https://github.com/8kp5jdvypy-hue/watchtower/pull/32) —
  design hotfixes.** **CLOSED, superseded.** Its fixes shipped via #33
  and #34; its one remaining unique piece (a `signalHistory` test file
  that nothing ever executed) was salvaged in #40. Note that #32's
  branch carries a *different* `signalHistory.js` — an
  `atrFollowThroughLabel` export that never merged, and `FLAT_BAND_ATR`
  at 0.25 where main has 0.05 — so nothing else on it can be
  cherry-picked without failing against main.

## PRs not yet opened (branches pushed, sitting unreviewed)

- ~~**`ops-offbox-backups`**~~ — **MERGED** as
  [#26](https://github.com/8kp5jdvypy-hue/watchtower/pull/26)
  (2026-08-12). Off-box GPG+rclone shipping and the restore procedure
  are on main. The restore path's GPG/rclone legs are still untested
  against a real bucket — see `DEPLOYMENT.md`'s own note, which is the
  live remnant of this item.
- **`ops-resilience-review`** — still unmerged, no PR. The doc (backups,
  hung-worker deadman gap, autoheal) with next-up specs for findings
  #2/#3. Content still applies; open it as a docs PR.
- **`broad-scan-honesty-proposal`** — still unmerged, no PR. Doc only,
  no code. **Needs a decision, not a session**: adopt the proposal or
  drop the branch.

## DEPLOYMENT.md — still missing (confirmed against current main)

Now merged and no longer missing: the two-Workers split (#22), GIT_SHA's
deploy-command line (#24), the frontend caching contract (#35, which
also corrected it — zone Cache Rules are inert for Workers static
assets, `_headers` governs browser caching only, deploys supersede the
assets cache atomically, and no purge step exists), and — as of Ship #1
(P5b+5c, PR #49), 2026-08-17, after the VPS 5b run hit exactly the two
failure modes this item predicted (host has no deps, image has no
`scripts/`) — the whole "how do you run a `scripts/` tool on this box"
question: DEPLOYMENT.md's new "Running `scripts/` tools in-container"
section has the one canonical invocation (`docker compose run --rm -v
/opt/perch/scripts:/app/scripts runner python3 scripts/<name>.py`), the
never-clear-current-day-cache-before-backfill_marks() warning, and the
fetch_cache.py-can't-refetch-today note, all in one place instead of
scattered or unwritten. Still not written anywhere:
- Phase 0 cache archives happen **on the VPS**, not the local Mac
  checkout (the real one tonight: `data/cache-archive/iex-2026-08-11`,
  20M, made on the VPS before the cache clear).
- Two local Mac checkouts exist: `~/projects/watchtower` (canonical,
  used for tonight's `wrangler` deploy) and `~/watchtower-deploy`
  (stale, source of an earlier out-of-date deploy). Doc should name the
  canonical one; recommend deleting the stale clone.

## Copy fixes

- **Near-close detections**: a detection fired close enough to session
  end that +15/+30/+60 offsets fall after hours shows "Resolves after
  session close" — same text as a genuinely pending row, but this one
  can *never* resolve. Should read something like "n/a — detected near
  close." Not yet implemented.
- Post-close future-tense text and the general Pending-copy fix already
  shipped (PR #19, merged).

## First-session observation report

Owed, not yet delivered: the read-only signal-rate / volume-multiple /
error-log checklist against the actual first live SIP session, per the
original migration runbook. Tonight's manual repair proved the
structural fix works mechanically; it isn't a substitute for that
checklist once a real live SIP session closes cleanly on its own.

## Ops follow-ups from tonight

1. **DONE — `deploy.sh` wrapper.** Source deployments now require one full-SHA
   command that verifies main ancestry, backups, service revisions, Compose
   health, SQLite integrity, and public health. The systemd boot unit cannot
   rebuild with `GIT_SHA=unknown`.
2. **RFAMU-type thin-symbol policy** — a symbol can detect (fire a
   cluster) but be too thin for the vendor to return intraday bars for
   at backfill time (confirmed real tonight: RFAMU, 1 of 33 symbols in
   the repair run, vendor returned no bars). Not a bug — the alert
   chain correctly surfaced it as a named, specific, honest gap. Open
   question: screen such symbols out of detection entirely, or accept
   occasional unresolvable marks as a real, bounded cost. Undecided.

## Bucketing inconsistency: `monthly_recap`/`personal_stats` use UTC, not ET (found 2026-08-13)

Not one of the 27 findings in `docs/full-code-review.md` — surfaced
during architecture recon for the Trade Journal feature. Every
session/day-boundary decision elsewhere in the codebase converts to ET
(`America/New_York`) first: `ET = ZoneInfo("America/New_York")` in both
`tradebot/journal.py` and `tradebot/runner.py`, and `session_date_fn` in
`runner.py` (`now.astimezone(ET).date()`) is the canonical "what day is
this" function, explicitly reused by the subscriber-alert hook and the
medium-digest fanout, with `/signals/today` in `tradebot/api/app.py`
carrying a comment calling out exactly this risk ("not the server's own
local/UTC date, which would be wrong ... around the ET midnight
rollover").

`monthly_recap()` and `personal_stats()` in `tradebot/telegram_bot/db.py`
do not follow that convention: they bucket closed trades by
`closed_at.year`/`closed_at.month` taken directly from a
`datetime.fromisoformat()` parse of the ISO-UTC-stored `closed_at`
string, with no ET conversion. A trade closed in the evening ET (e.g.
8pm ET = past midnight UTC) can land in the wrong UTC calendar
day/month relative to every other ET-bucketed view in the app.

Not the Trade Journal's job to fix — flagging so it isn't silently
inherited by whatever extends `user_trades` next, and doesn't get lost.
Low severity today (no UI currently exposes `monthly_recap` output
prominently), but worth closing before any feature buckets trades by
day/week/month at user-facing granularity.

## Code review triage (`docs/full-code-review.md`)

27 ranked findings (5 CRITICAL, 8 HIGH, 11 MEDIUM, 3 LOW) plus a Silent
Failure Inventory (19 paths), from the overnight full-codebase review.

### CRITICALs — all five resolved, verified in code 2026-08-15

Verified by re-reading the code finding by finding, not by trusting this
file's own earlier "not triaged yet". Note the sequence honestly: four
of the five were already fixed by the 2026-08-12/13 hotfix work while
this document still described them as untriaged; only #5 was fixed
after the sweep identified it as the last one standing.

| # | Finding | Status |
|---|---|---|
| 1 | `run_live`'s `backfill_marks()` return value discarded | **DONE** (PRs #23/#24) — `_alert_if_backfill_implausible` captures, logs and pages; 4 tests |
| 2 | Missing cache file silently becomes `[]` | **DONE** (PRs #23/#24) — per-symbol ERROR plus the close-time `_cache_todays_intraday_bars` structural fix; incident-replay test |
| 3 | `fetch_cache.py` fetch failures are print-only | **PARTIALLY DONE** — see the remnant below |
| 4 | No test for `backfill_marks()` with a missing cache dir | **DONE** (PR #24) — covers the exact incident shape end to end |
| 5 | A Telegram alert can reference a `detection_id` before it commits | **DONE** ([#36](https://github.com/8kp5jdvypy-hue/watchtower/pull/36)) — `_commit_then_send()`; 6 tests, 4 of which fail against the pre-fix ordering |

**The C3 remnant (still open, small).** The production path is covered:
`run_live` fetches today's bars at close and
`_alert_if_cache_fetch_failed` pages on a total failure. But
`scripts/fetch_cache.py` itself still **exits 0** when a symbol ends its
run with `"gave up"` — only `AlpacaCredentialsError` produces a non-zero
exit. A manual or cron invocation therefore still fails silently, which
is exactly finding #3's original complaint, just narrowed to the
out-of-band callers. Fix: exit non-zero (or alert) when any symbol
finishes with `"gave up"`, and treat a `WATCHLIST` symbol missing
today's session file as a hard failure.

### HIGH / MEDIUM / LOW — corrected statuses

Also verified in code during the same sweep, since this file previously
called the whole set untriaged:

- **#7 (`historical_performance` sign convention)** — **DONE** (PR #23),
  with regression tests asserting the sibling functions agree.
- **#13 (`run_live`/`run_replay` coverage)** — **DONE**: direct live and
  replay regressions prove one symbol failure cannot stop later symbols and
  require the failure in heartbeat errors.
- **#22 (Watchlist error discarded)** — **DONE**: both watchlist and
  `fetchToday` failures are surfaced; unknown signal state renders checking/
  unavailable and never the false calm `quiet` label.
- **#9, #11, #12, #14–#17, #19, #20, #24** — **DONE**; see the corresponding
  resolution evidence and regression tests in `full-code-review.md`.
- **#8** is **DONE**: independently tested and merged in PR #134.
- **#6, #10, #18, #21, #23, #25–#27 remain open.** #6's mass-delist
  half is guarded ([#37](https://github.com/8kp5jdvypy-hue/watchtower/pull/37)),
  which closes its destructive path but not the whole finding.
- The **Priority 2** `rvol_spike`/`relative_strength_break` timestamp
  alignment defect is **DONE**: both detectors use exact DST-aware timestamp
  alignment, with missing-bar regressions preventing list-position drift.

## Small items found during the 2026-08-15 sweep

- **`_commit_then_send()` covers `process_new_bar` only.** The other
  `alerter.send()` sites (digests, heartbeats, recaps, session
  open/close) don't reference a `detection_id`, so none of them can
  dangle one today — but that's a property of what those call sites
  currently render, not an invariant anything enforces. If a future
  alert starts citing a detection, it must route through
  `_commit_then_send()` too.
- **The landing has no `robots.txt` and no `sitemap.xml`.** Both
  currently resolve to the SPA shell at HTTP 200 (via
  `not_found_handling=single-page-application`). Neither blocks
  crawling — an absent robots.txt means nothing is disallowed — so this
  is not why the favicon was missing from search results (that was an
  uncrawlable `data:` URI, fixed in
  [#38](https://github.com/8kp5jdvypy-hue/watchtower/pull/38)). A real
  sitemap would still help Search Console discover and re-crawl pages.
- **The dashboard's own favicon** is non-square (190×178) and has no
  `.ico`. Google renders it today, so this is cosmetic consistency with
  the landing's new set, not a defect.
- **Branch/worktree hygiene**: ~35 fully-merged local branches and
  several stale worktrees are prunable. The only branches with unmerged
  content are `ops-resilience-review` and `broad-scan-honesty-proposal`
  (both listed above).

## `RefreshResult.delisted == ()` is ambiguous (from finding #6's fix)

`refresh_universe()` now refuses to delist on an implausibly small
vendor fetch (PR #37), but returns `delisted=()` for both "nothing
needed delisting" and "refused to delist" — the same two-states-one-
output shape findings #2/#14 flag elsewhere. The ERROR log distinguishes
them; a caller can't. Add a `delist_skipped` field to `RefreshResult`
when finding #14's API-status work lands, so both get done under one
consistent "make the status machine-readable" decision rather than
piecemeal.

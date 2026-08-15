# Backlog

Compiled 2026-08-12, from the night of the SIP flip and the backfill
incident. Verified against actual repo/PR state before writing this
down, not from memory — see each item's status.

## Open PRs needing review/merge

- **[#22](https://github.com/8kp5jdvypy-hue/watchtower/pull/22) —
  Document the two separate Cloudflare Workers.** Open, not merged.
  Rewrites `DEPLOYMENT.md`'s stale "web dashboard doesn't exist yet"
  section with the real `watchtower` vs `perch-dashboard` split. Should
  land before the items below extend that same section further.

## PRs not yet opened (branches pushed, sitting unreviewed)

- **`ops-offbox-backups`** — off-box backup shipping (GPG + rclone to
  DigitalOcean Spaces), documented restore procedure. Top priority per
  standing instruction: open first.
- **`ops-resilience-review`** — the operational resilience review doc
  (backups, hung-worker deadman gap, autoheal), with next-up specs for
  findings #2/#3 already written in.
- **`broad-scan-honesty-proposal`** — the broad_scan labeling/stats-
  exclusion proposal (doc only, no code yet).

## DEPLOYMENT.md — still missing (confirmed against current main)

PR #22 covers the two-Workers split; GIT_SHA's deploy-command line is
already merged (via #24). Not yet written anywhere:
- In-container `fetch_cache` invocation — `docker compose run --rm -v
  /opt/perch/scripts:/app/scripts -e DETECTOR_DATA_FEED=sip runner
  python3 scripts/fetch_cache.py` (host has no Python deps; the image
  doesn't include `scripts/`, hence the mount).
- Explicit warning: never clear current-day intraday cache files before
  that day's `backfill_marks()` has run — the mechanism behind the
  2026-08-12 incident (now fixed at the code level in PR #24, but the
  operational warning for anyone doing a manual cache operation isn't
  written down anywhere).
- `fetch_cache.py` structurally cannot refetch the current day (session
  walk-back starts at `date.today() - 1`) — worth stating plainly so a
  future manual repair doesn't get attempted the wrong way.
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

1. **`deploy.sh` wrapper** so `GIT_SHA=$(git rev-parse --short HEAD)
   docker compose up -d --build` isn't a rememberable-but-forgettable
   convention — it already was forgotten once tonight. A wrapper script
   (or Makefile target) makes the correct invocation the only one
   available.
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
Not triaged yet. Two findings already known and explicitly deferred by
name during tonight's hotfix work:
- Cross-database atomicity bug (a Telegram alert can reference a
  `detection_id` before that row commits to `journal.db`) — HIGH,
  explicitly queued for triage, not touched tonight on purpose.
- `historical_performance()`'s sign-convention bug — already fixed
  (part of PR #23).

Everything else in the review is unreviewed by a human; CRITICALs
should come first.

## `RefreshResult.delisted == ()` is ambiguous (from finding #6's fix)

`refresh_universe()` now refuses to delist on an implausibly small
vendor fetch (PR #37), but returns `delisted=()` for both "nothing
needed delisting" and "refused to delist" — the same two-states-one-
output shape findings #2/#14 flag elsewhere. The ERROR log distinguishes
them; a caller can't. Add a `delist_skipped` field to `RefreshResult`
when finding #14's API-status work lands, so both get done under one
consistent "make the status machine-readable" decision rather than
piecemeal.

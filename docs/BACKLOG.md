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

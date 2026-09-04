# Watchtower

A market scanner watching SPY, QQQ, GOOGL, TSLA, BE, IONQ for notable
intraday conditions and pushing alerts to Telegram. It never places
orders and has no broker write access.

## Rules

- Detectors and signal functions are PURE: data in, `Detection | None`
  out. No I/O, no network, no clock reads, no globals.
- `Bar.ts` is the timestamp of the bar's OPEN, in UTC. A 5-minute bar
  stamped 14:30 is not knowable until 14:35. Never make a decision
  timestamped before the bar it used has closed.
- All market data access goes through the `MarketData` protocol. No
  vendor SDK imports outside its own adapter module.
- All thresholds are expressed in ATR units, never percentages.
- Anchors are computed once per session and frozen. Never recomputed per
  bar.
- Every detection is journaled before any alert is sent, including
  sub-threshold ones.
- Live alerting is opt-in via `--live`. Default is log-only.
- Python 3.11+, type hints, dataclasses over dicts. Boring code
  preferred.
- Run pytest before claiming a task is complete. Report failures; do not
  describe a task as done while tests are red.

## Workflow

- Before changing behavior, read `docs/PROGRAM-STATE.md`,
  `docs/STATE-OF-THE-SYSTEM.md`, `docs/ROADMAP.md`, and the relevant
  architecture or deployment document. Treat conflicts as a decision to
  resolve, not permission to choose the most convenient version.
- Inspect the current branch, worktree state, and in-progress work before
  editing. One task owns one branch/worktree; record its disposition and
  remove it after merge only when it contains no unique or uncommitted work.
- Do not deploy, change production configuration, rotate credentials, alter
  data-provider entitlements, or perform destructive production actions
  without explicit approval.
- Use `scripts/verify.sh` before claiming repository-wide completion. A
  surface-specific change may run the narrower relevant commands during
  iteration, but the final handoff must name everything that was and was not
  verified.
- Releases must identify the exact revision, post-deploy checks, and rollback
  path. Passing local tests is not evidence that production is current.

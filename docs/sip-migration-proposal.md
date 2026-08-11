# SIP feed migration proposal

**Status: proposal only. Zero code changes included in this document or
its branch.** Nothing here is implemented until each phase below is
explicitly approved — recalibration in particular is called out as its
**own, separate approval**, not something this proposal pre-authorizes.

## Summary

Move the detector-facing market-data feed in `tradebot/vendors/alpaca.py`
— `fetch_daily_bars`, `fetch_intraday_bars`, `fetch_daily_bars_bulk`,
all currently hardcoded to `DataFeed.IEX` — to SIP (the full
consolidated tape), so the scanner evaluates against the same
real-volume, real-spread data any other market participant sees,
instead of IEX's single-venue slice.

This is a proposal to **gather the evidence and build the tooling** to
do that migration safely — not a request to flip the feed today. The
actual cutover (Phase 3 below) is gated on Phase 1's backtest results
and, if needed, a separate recalibration approval.

## What this explicitly does NOT touch

- **The SIP display path is out of scope, unconditionally.**
  `fetch_latest_quote`/`fetch_latest_quotes` in `vendors/alpaca.py`
  already run on SIP today, feeding the dashboard's `/quotes` endpoint
  (price display only). Per `docs/PROGRAM-STATE.md`'s guardrail, this
  migration's config change must not fold that display path into
  whatever setting drives the detector feed — they answer different
  questions (what a human sees right now vs. what the model evaluated),
  and unifying them would just be two unrelated feeds sharing one
  on/off switch for no reason. This proposal only concerns the three
  detector-facing calls above; the display path's SIP usage doesn't
  change and isn't "further evidence" that the detector feed is ready.
- **No threshold or tier recalibration is included here.** `TIER_HIGH`
  (3.8) and `TIER_MEDIUM` (1.9) — and every other ATR/ratio-based
  threshold in `tradebot/detectors.py` — stay exactly as they are
  through this entire proposal. If Phase 1's backtest evidence shows
  they'd need to change under SIP, that evidence gets reported and
  specific new values get proposed as a **separate, later approval**,
  following the same train/test-split discipline `SCANNER_PLAN.md`
  already uses for every other calibration decision in this project
  (see its "Best hours" and tier-calibration sections) — not bundled
  into or rushed by this migration.
- **No change to the options/contract-selection feed**
  (`OptionsFeed.INDICATIVE` in `fetch_option_chain`) — untouched,
  unrelated to this proposal.

## Why the feed matters here specifically

`rvol_spike` (`tradebot/detectors.py`) compares live cumulative volume
against `avg_cum_volume_by_bar`, a per-bar-index baseline built entirely
from IEX-cached replay history (`build_anchors()`, fed by
`scripts/fetch_cache.py`'s cached bars). Live-measured, real numbers
already on record in `vendors/alpaca.py`'s own docstrings: SIP volume
runs 20-42x IEX's on this watchlist, same session, same RTH window (SPY
26x, NVDA 20x, TSLA 42x). Flipping the feed without addressing that
mismatch would make `rvol_spike` fire on almost every bar — not a
subtle miscalibration, a broken detector. Every other detector
(`level_break`, `range_expansion`, `vwap_break`, `round_number_break`,
`gap`) is priced in ATR units off OHLC, not volume, so the exposure is
narrower than "the whole scanner breaks," but all of them still see
different prices/ranges under SIP vs. IEX (SIP includes venues IEX
doesn't) and none have been validated against that difference yet. That
validation is exactly what Phase 1 produces.

## Phase 0 — archive the IEX cache

Before any SIP data collection begins, snapshot the current
`data/cache/{symbol}/` tree (per-symbol `daily.csv` and
`intraday_{date}.csv` files written by `scripts/fetch_cache.py`) to an
archive location — e.g. `data/cache-archive/iex-{cutover-date}/`, or
off-box alongside the existing SQLite backup destination
(`docs/DEPLOYMENT.md`'s `scripts/backup.sh` / `BACKUP_DIR`, if one is
already configured). **Copy, never move or delete** — `ReplayMarketData`
and every backtest in this proposal keep reading the live `data/cache/`
path, so the archive is a safety copy, not a relocation. This preserves
the ability to reproduce every historical detection (`out/replay_detections.csv`,
`data/journal.db`) and every number already published in
`SCANNER_PLAN.md` (the 2026-08-05 `gap()` recalibration, the
confirmation-delay test, the coin-flip HIGH-tier significance check)
against the exact IEX data they were originally computed from, indefinitely,
regardless of what the live cache becomes later.

## Phase 1 — build SIP replay capability, gather backtest evidence

**Nothing here touches the live scanner.** This is entirely a replay/
backtest exercise, following the existing pattern:

1. Add a SIP-feed variant of the cache-fetch path — either a
   `--feed sip` flag on `scripts/fetch_cache.py` or a parallel script —
   that pulls the same daily + 5-minute bar shapes `ReplayMarketData`
   already expects, into a **separate** directory (e.g.
   `data/cache-sip/{symbol}/`), for the same historical session window
   already cached under IEX. This is new tooling, not a change to the
   three hardcoded `DataFeed.IEX` call sites in `vendors/alpaca.py` —
   those stay IEX until Phase 3.
2. Run `scripts/replay.py` (or `python -m tradebot.runner --replay-date`)
   twice over the same session set — once against `data/cache/` (IEX,
   existing), once against `data/cache-sip/` (new) — into two separate
   journal databases, the same A/B pattern `scripts/compare_replay.py`
   already uses for calibration changes (its own docstring: "the tool
   used to calibrate new detectors/thresholds... against real data
   before trusting a default"). Two files, not one shared DB, for the
   same reason that script already documents: re-running the same
   session under a second data source into one DB would collide or
   silently overwrite on `cluster_id()`.
3. Diff the two runs and report, per detector kind and per tier:
   - **Signal counts** — how many clusters fire under SIP vs. IEX,
     broken out the same way `tier_performance()`/`kind_performance()`
     already group results (by tier, by kind).
   - **Volume-multiple distribution** — not just the 3 anecdotal SPY/
     NVDA/TSLA data points already on record, but the actual SIP:IEX
     cumulative-volume ratio per symbol, across the full 17-symbol
     watchlist, over the whole backtest window — mean, median, and
     range per symbol, since a single "20-42x" summary could be hiding
     real per-symbol variation that matters for a baseline that's
     currently built per-symbol.
4. Report that evidence plainly — real counts and real distributions,
   not a recommendation dressed as a finding. If the evidence shows
   `rvol_spike` (or any other detector) would need different thresholds
   under SIP, that goes back as a **separate proposal with specific
   proposed values**, following the train/test-split methodology
   `SCANNER_PLAN.md` already holds every other threshold decision to —
   fit on one half of the backtest window, validate on the other,
   exactly like the existing "Best hours" and confirmation-delay
   write-ups in that doc. This proposal does not pre-approve any
   resulting values; it only commits to producing the evidence honestly.

## Phase 2 — decide on recalibration (separate approval, may not be needed at all)

Gated entirely on Phase 1's output. Three possible outcomes, all
legitimate:

- Evidence shows thresholds hold up fine under SIP (unlikely for
  `rvol_spike` given the volume mismatch, plausible for the ATR-priced
  detectors) — no recalibration needed, proceed to Phase 3 as-is.
- Evidence shows specific thresholds need new values — a separate,
  explicit proposal with those values and the train/test evidence
  behind them, requiring its own approval before Phase 3.
- Evidence is ambiguous or the backtest window is too thin to trust
  (same "n=5 in some buckets" caution `SCANNER_PLAN.md` already applies
  elsewhere) — extend the backtest window rather than guess.

No live cutover happens while this phase is open.

## Phase 3 — live cutover

Only once Phase 1 (and Phase 2, if triggered) are resolved:

1. Flip the three `feed=DataFeed.IEX` literals in `vendors/alpaca.py`
   (`fetch_daily_bars`, `fetch_intraday_bars`, `fetch_daily_bars_bulk`)
   to `DataFeed.SIP`, together, in one change — not one call site at a
   time, per the existing docstrings' own instruction on this ("flip
   together... only after a real recalibration pass").
2. **Record the cutover timestamp** — both as a code-level marker (e.g.
   a dated entry in `docs/PROGRAM-STATE.md`, matching this doc's own
   "point-in-time facts" convention) and, if useful for later analysis,
   a sentinel written to `data/journal.db` or a `data/cache/` marker
   file — so any future query into detector behavior can cleanly split
   "before" from "after" without relying on git-log archaeology.
3. Deploy through the existing path (`docs/DEPLOYMENT.md`'s
   `git pull && docker compose up -d --build`, recreating `runner` and
   `worker`), during a low-stakes window (e.g. a weekend, before the
   next session open) so the first live SIP-fed session is being
   watched, not discovered after the fact.

## Rollback plan

Because this is a three-line feed-literal change with no schema or
journal-format impact (`Bar`/`Detection`/`DailyAnchors` shapes are
identical regardless of which feed populated them), rollback is
**revert the commit, redeploy** — the same `docker compose up -d --build`
path as any other deploy, same-day. Two things make this safe:

- `data/journal.db` is append-only and never needs correcting — a
  session scanned under SIP is journaled as real data either way; it
  doesn't need to be "undone," only future sessions need to go back to
  IEX. `write_cluster`'s upsert-by-identity behavior means a rolled-back
  redeploy re-scanning the same session would only affect that one
  session's rows, not history.
- The existing kill switches (`data/HALT`, per-user `/halt`,
  `WATCHTOWER_KILL_SWITCH`) already stop alerting immediately if the
  post-cutover session looks wrong in a way that can't wait for a
  redeploy — no new stop mechanism needed for this migration
  specifically.

**Rollback trigger criteria** (to define concretely before Phase 3, not
left to judgment in the moment): a maximum acceptable deviation in
daily HIGH-tier alert count vs. the Phase 1 backtest's predicted range,
checked against the first N live sessions post-cutover. Exact N and the
acceptable band are worth pinning down as part of Phase 2's sign-off,
once real backtest numbers exist to anchor them against.

## Open questions

- Does the current Alpaca account entitlement (Algo Trader Plus, per
  `vendors/alpaca.py`'s docstring — the same entitlement the SIP
  display path already runs under per `docs/PROGRAM-STATE.md`) cover
  SIP historical-bar requests at the volume Phase 1's backtest would
  need, or only real-time SIP quotes? Worth confirming with Alpaca
  directly before Phase 1 tooling work starts, since it changes whether
  Phase 1 needs its own entitlement/cost conversation first.
- How large a backtest window does Phase 1 need to trust the volume-
  multiple distribution and signal-count comparison? `SCANNER_PLAN.md`'s
  own "Best hours" section flags n=5-per-bucket as too thin to trust —
  worth sizing this deliberately rather than reusing whatever's already
  cached by default (20 sessions per `fetch_cache.py`'s current
  `--sessions-n` default).
- `broad_scan.py`'s own RVOL_THRESHOLD check (Stage 1 screening across
  the full active universe, not just the fixed watchlist) has the
  identical IEX-baseline dependency per its own docstring — same
  question, likely same answer, but worth an explicit line item in
  Phase 1's scope rather than assuming it's covered by the watchlist
  backtest.

## What approval unlocks at each stage

- Approving this document: unlocks Phase 0 (archive) and Phase 1
  (SIP replay tooling + backtest evidence-gathering) only. No live
  feed change.
- Phase 1's output, once delivered: a decision point on Phase 2 —
  recalibrate or don't, as its own approval.
- Phase 2 resolved (either "no recalibration needed" or a separately
  approved set of new values): unlocks Phase 3, the actual live cutover.

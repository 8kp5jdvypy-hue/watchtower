# SIP feed migration proposal

**Status: proposal only. Zero code changes included in this document or
its branch.** Nothing here is implemented until each phase below is
explicitly approved — recalibration in particular is called out as its
**own, separate approval**, not something this proposal pre-authorizes.

**Revision note:** this revision replaces the original "flip three
hardcoded literals" cutover mechanism with a single config value (see
"Feed-site audit" and Phase 3 below), adds a stats-treatment decision
point, a session-boundary cutover runbook, and reports the result of an
empirical entitlement check that's since been run.

## Summary

Move the detector-facing market-data feed in `tradebot/vendors/alpaca.py`
— currently three separate hardcoded `feed=DataFeed.IEX` call sites —
to SIP (the full consolidated tape) via **one config value**, so the
scanner evaluates against the same real-volume, real-spread data any
other market participant sees, instead of IEX's single-venue slice, and
so both cutover and rollback are a config flip + restart, never a code
edit.

This is a proposal to **gather the evidence and build the tooling** to
do that migration safely — not a request to flip the feed today. The
actual cutover (Phase 3 below) is gated on Phase 1's backtest results
and, if needed, a separate recalibration approval.

## Entitlement check: settled empirically

Before any Phase 1 tooling work, ran the minimal test this proposal
originally left as an open question: can the current Alpaca account
(Algo Trader Plus, per `vendors/alpaca.py`'s docstring) pull
**historical** SIP bars, not just the real-time SIP quotes the
dashboard's `/quotes` path already proves work?

Two live, read-only calls against the real account, today:

| Call | Result |
|---|---|
| `StockBarsRequest`, `SPY`, `TimeFrame.Day`, `feed=DataFeed.SIP`, 5-day lookback | **Success** — 3 daily bars returned (most recent: 2026-08-11, close 770.03, volume 22,743,110) |
| `StockBarsRequest`, `SPY`, `TimeFrame.Minute`, `feed=DataFeed.SIP`, full session 2026-08-11 | **Success** — 611 one-minute bars returned, 08:00 UTC through 19:07 UTC |

**Settled: yes, the current entitlement covers historical SIP bars, both
daily and intraday/minute-level — not just real-time quotes.** Phase 1
needs no separate entitlement or cost conversation with Alpaca before
starting. (This removes what was previously the first open question in
this document.)

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
  detector-facing calls audited below; the display path's SIP usage
  doesn't change and isn't "further evidence" that the detector feed is
  ready.
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
  unrelated to this proposal (see the audit below for why this is a
  different feed *type*, not just a different value of the same one).

## Feed-site audit (reconciled)

Exhaustive repo-wide search (`grep -rn "feed=" tradebot/ scripts/`) finds
**six** `feed=` call sites total, **all six in one file**,
`tradebot/vendors/alpaca.py` — nowhere else in the codebase constructs
its own feed selection:

| # | Function | Line | Feed | In scope for this migration? |
|---|---|---|---|---|
| 1 | `fetch_daily_bars` | 117 | `DataFeed.IEX` | **Yes** — detector-facing |
| 2 | `fetch_intraday_bars` | 141 | `DataFeed.IEX` | **Yes** — detector-facing |
| 3 | `fetch_daily_bars_bulk` | 288 | `DataFeed.IEX` | **Yes** — detector-facing |
| 4 | `fetch_latest_quote` | 159 | `DataFeed.SIP` | No — dashboard display, already SIP, out of scope per the guardrail above |
| 5 | `fetch_latest_quotes` | 183 | `DataFeed.SIP` | No — same as #4, the batched form |
| 6 | `fetch_option_chain` | 225 | `OptionsFeed.INDICATIVE` | No — a different feed *enum entirely* (`OptionsFeed`, not `DataFeed`); options-chain data, unrelated to equity bars/quotes |

**Reconciling the 3-vs-~4 count**: this migration's actual target is 3
sites (#1-3, all `DataFeed.IEX`, all detector-facing). If an earlier
pass counted ~4, the most likely explanation is counting site #6
(`OptionsFeed.INDICATIVE`) as a fourth "feed-shaped" location worth
tracking — it does say `feed=` — even though it's a different Alpaca
feed enum for a different data type, not a `DataFeed.IEX`/`DataFeed.SIP`
choice at all. This document treats it as explicitly out of scope
(table row #6), not silently uncounted.

**Direct answers to the two required questions:**

- **`scripts/fetch_cache.py`** (`scripts/fetch_cache.py:32`):
  `from tradebot.vendors.alpaca import AlpacaCredentialsError,
  fetch_daily_bars, fetch_intraday_bars` — it calls the vendor module's
  functions directly and has no `DataFeed` import and no feed logic of
  its own anywhere in the file. **It fetches through the vendor
  module's feed configuration and therefore follows
  `DETECTOR_DATA_FEED` automatically** — no separate site to track or
  flip.
- **`tradebot/broad_scan.py`**: has no `DataFeed` import and no Alpaca
  import at all, anywhere in the file. `build_snapshots_from_daily_bars()`
  takes already-fetched `bars_by_symbol` as a plain argument — the
  actual fetch is `vendors.alpaca.fetch_daily_bars_bulk` (site #3
  above), called by `tradebot/runner.py`'s orchestration and handed to
  `broad_scan.py`. **Same conclusion: it has no feed setting of its
  own and automatically follows `DETECTOR_DATA_FEED`** through site #3
  — see the last open question below for what this means for Phase 1's
  backtest scope.

## One config value, not three literals

Original design (superseded by this revision): edit the three
`DataFeed.IEX` literals directly, as a code change, redeploying to both
cut over and roll back. Replaced with a single environment variable —
matching how every other environment-dependent choice in this codebase
already works (`SESSION_COOKIE_SECURE`, `DEV_CORS_ORIGIN`, etc.):

```python
# tradebot/vendors/alpaca.py, near the top, alongside the other module
# constants (e.g. BULK_FETCH_CHUNK_SIZE) -- read once at import time.
_raw = os.environ.get("DETECTOR_DATA_FEED", "iex").strip().lower()
if _raw not in ("iex", "sip"):
    raise ValueError(f"DETECTOR_DATA_FEED must be 'iex' or 'sip', got {_raw!r}")
DETECTOR_DATA_FEED = DataFeed.SIP if _raw == "sip" else DataFeed.IEX
```

Sites #1-3 above change from `feed=DataFeed.IEX` to `feed=DETECTOR_DATA_FEED`.
Sites #4-6 are untouched literals, exactly as today — this variable has
no effect on them, by construction (they never reference it). One name
throughout this document, for both the env var and the module constant
it produces: `DETECTOR_DATA_FEED`.

**Why this matters for cutover and rollback specifically**: since
`docker-compose.yml`'s services already read `env_file: .env`, changing
`DETECTOR_DATA_FEED` in `.env` and running `docker compose up -d` (no
`--build` needed — no image content changed, only the env file Compose
already tracks) is sufficient to take effect on next container
recreate. Cutover and rollback both become **the same one-line config
edit + restart**, symmetric, no git revert, no rebuild, no redeploy
pipeline — see the revised Phase 3 and Rollback sections below.

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
backtest exercise, following the existing pattern, now unblocked by the
entitlement check above.

**Prerequisite: ship the `DETECTOR_DATA_FEED` config-value code now,**
not in Phase 3 — Phase 1's own tooling (step 1 below) needs
`vendors/alpaca.py` to already recognize the env var. This is the
"One config value, not three literals" change above and nothing else:
still defaults to `"iex"`, a no-op for current live behavior, low-risk
by construction. Approving this document covers shipping this small,
inert code change alongside Phase 0's archive step — it's what makes
Phase 1 possible, not a live feed change. Phase 3 (below) then only
needs the `data_feed` column and whichever Decision point B option is
chosen, plus the actual flip.

### Why "rebuild the anchors" isn't a separate step — and why that's exactly the risk

`avg_cum_volume_by_bar` is not a stored artifact anywhere — `build_anchors()`
(`tradebot/detectors.py:115-162`) computes it **fresh, in memory, on
every call**, purely from whatever `historical_session_bars` it's
handed. Both the replay path and — critically — the **live** path
source those bars the same way: `tradebot/runner.py`'s
`history_by_symbol` (built at `runner.py:771` in replay and `runner.py:1014`
in live, identical logic in both) calls `full_session_rth_bars()`
(`runner.py:165-169`), which reads from `CACHE_DIR` — a hardcoded
module constant (`runner.py:91` and, separately, `scripts/replay.py:43`),
both pointing at `data/cache/`. **Even in live mode, the historical
baseline never comes from a fresh vendor call — it always comes from
whatever's sitting in `data/cache/` on disk.** Only *today's* live bars
(via `LiveMarketData`) are fetched fresh through `DETECTOR_DATA_FEED`.

This means "rebuild the anchors" has one, and only one, real mechanism:
**refresh the contents of the cache directory `CACHE_DIR` points at**
— there's no separate anchor-rebuild script or artifact to run. And it
means the exact failure mode this migration exists to prevent
(SIP-scale live volume compared against an IEX-scale baseline) doesn't
just risk showing up in Phase 3 — it's the literal, mechanical default
outcome of flipping `DETECTOR_DATA_FEED` alone, live or in a backtest,
without also refreshing `data/cache/`'s contents. Phase 1 has to
produce backtest evidence that's actually SIP-baselined, not
SIP-current-bar-against-IEX-baseline, or the whole exercise measures
the wrong thing.

### Steps

1. **Build a separate SIP cache**, without touching the live `.env` or
   `data/cache/` at all: `scripts/fetch_cache.py` already accepts
   `--cache-dir` (`scripts/fetch_cache.py:97`, pre-existing, no code
   change needed there) and — per the feed-site audit above — already
   fetches through `vendors.alpaca.fetch_daily_bars`/`fetch_intraday_bars`,
   which honor `DETECTOR_DATA_FEED` once this section's own prerequisite
   has shipped. A one-off invocation with a process-local env override:
   ```
   DETECTOR_DATA_FEED=sip python3 scripts/fetch_cache.py --cache-dir data/cache-sip
   ```
   populates `data/cache-sip/{symbol}/` with SIP bars for the same
   historical session window already cached under IEX — the live
   scanner's own `DETECTOR_DATA_FEED` (still `iex`) and `data/cache/`
   are untouched by this.
2. **Point replay tooling at the SIP cache for the SIP-side run.**
   `CACHE_DIR` is currently a hardcoded constant, not a parameter, in
   both `tradebot/runner.py:91` and `scripts/replay.py:43` — this needs
   a small addition (a `--cache-dir` override on `scripts/replay.py`
   and the replay code path in `tradebot/runner.py`, mirroring the flag
   `fetch_cache.py` already has) as part of Phase 1's tooling work. If
   that addition is deferred, the fallback is the same pattern
   `scripts/compare_replay.py`'s own docstring already documents for
   A/B comparisons — swap `data/cache/`'s contents out and back in
   between runs — but an explicit `--cache-dir` flag is the cleaner,
   safer version of the same idea and worth the small implementation
   cost.
3. Run `scripts/replay.py` (or `python -m tradebot.runner --replay-date`)
   twice over the same session set — once against `data/cache/` (IEX,
   existing, untouched), once against `data/cache-sip/` (new, from step
   1, read via step 2's `--cache-dir`) — into two separate journal
   databases, the same A/B pattern `scripts/compare_replay.py` already
   uses for calibration changes (its own docstring: "the tool used to
   calibrate new detectors/thresholds... against real data before
   trusting a default"). Two files, not one shared DB, for the same
   reason that script already documents: re-running the same session
   under a second data source into one DB would collide or silently
   overwrite on `cluster_id()`. Because of the anchor mechanism above,
   the SIP-side run's anchors are correctly SIP-baselined by
   construction — `build_anchors()` only ever sees what `--cache-dir`
   pointed it at.
4. Diff the two runs and report, per detector kind and per tier:
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
5. Report that evidence plainly — real counts and real distributions,
   not a recommendation dressed as a finding. If the evidence shows
   `rvol_spike` (or any other detector) would need different thresholds
   under SIP, that goes back as a **separate proposal with specific
   proposed values**, following the train/test-split methodology
   `SCANNER_PLAN.md` already holds every other threshold decision to —
   fit on one half of the backtest window, validate on the other,
   exactly like the existing "Best hours" and confirmation-delay
   write-ups in that doc. This proposal does not pre-approve any
   resulting values; it only commits to producing the evidence honestly.

## Phase 2 — two decision points (both separate approvals)

Gated entirely on Phase 1's output.

### Decision point A: recalibration

- Evidence shows thresholds hold up fine under SIP (unlikely for
  `rvol_spike` given the volume mismatch, plausible for the ATR-priced
  detectors) — no recalibration needed, proceed to Phase 3 as-is.
- Evidence shows specific thresholds need new values — a separate,
  explicit proposal with those values and the train/test evidence
  behind them, requiring its own approval before Phase 3.
- Evidence is ambiguous or the backtest window is too thin to trust
  (same "n=5 in some buckets" caution `SCANNER_PLAN.md` already applies
  elsewhere) — extend the backtest window rather than guess.

### Decision point B: how the stats functions treat pre- vs post-cutover history

**This is new in this revision — not addressed before.** None of
`historical_performance()`, `tier_performance()`, or `kind_performance()`
(`tradebot/journal.py`) currently have any notion of "which feed
produced this row." All three query the full journal unconditionally
and blend everything into one continuation-rate/avg-return number, with
only `MIN_HISTORY_SAMPLE = 5` as a floor. Left alone, the very first
SIP-fed session would start blending a handful of SIP rows into
`/performance`, `/start`'s live significance check, and every per-kind
card in the web dashboard's Performance tab — silently mixing two
different measurement regimes (different prices, different volume
baselines) into numbers that already carry real weight (`SCANNER_PLAN.md`'s
own n=466 HIGH-tier coin-flip verdict is exactly this kind of stat).

A complication specific to this revision's config-flip design: the
existing `detections.code_version` column (a git short-hash, written by
`journal.code_version()`) does **not** change at cutover under the
config-value mechanism above, since cutover is an env var flip, not a
code change — so `code_version` can't be used to tell IEX-era rows from
SIP-era rows after the fact. **Implementing either option below
requires adding an explicit `data_feed` column to `detections`**,
written from `DETECTOR_DATA_FEED`'s value at journal-write time — a
schema addition, not something either option can skip.

**Option 1 — post-cutover-only (recommended).** Once `data_feed`
exists, filter all three stats functions to the currently-live feed
value (`WHERE data_feed = 'sip'` once SIP is live), discarding
pre-cutover IEX rows from these three functions' queries entirely
(the raw rows stay in the journal — nothing is deleted, only excluded
from these specific aggregates). Combined with the existing
`MIN_HISTORY_SAMPLE` floor, this is the same "never report a stat built
on too few points" discipline the project already applies everywhere
else, just triggered by a feed change instead of a brand-new detector.
  - *Pro*: never blends two measurement regimes into one number — the
    exact failure mode `SCANNER_PLAN.md`'s "never fabricate a stat"
    ethos exists to prevent.
  - *Con*: `/performance`, `/start`'s significance check, and the
    per-kind Performance-tab cards go quiet (report `None`/"not enough
    data yet") for a real stretch of time post-cutover — the existing
    n=466 HIGH-tier verdict effectively resets to n=0 for "is this
    real" purposes, and a lot of hard-won IEX sample size stops
    counting toward these specific numbers, permanently.

**Option 2 — segmented eras.** Keep both eras, report them as two
explicitly labeled segments (e.g. "IEX era, n=X, through
{cutover-date}" and "SIP era, n=Y, since {cutover-date}") side by side,
rather than blending or discarding either.
  - *Pro*: no history thrown away; honest about two regimes existing
    instead of picking one silently.
  - *Con*: meaningfully more implementation surface — not just the
    three `journal.py` functions, but every consumer that renders their
    output (`render_morning_briefing`, `/start`'s onboarding text,
    `/performance`'s Telegram reply, the web dashboard's Performance.jsx
    per-kind cards) needs a two-segment layout, plus a copy decision on
    how to explain "two eras" in one Telegram message without it
    reading as hedging. Doesn't resolve cleanly if the feed is ever
    flipped a second time (three-way segmentation, and so on).

**This is explicitly the operator's decision, not resolved by this
proposal.** Recommendation is Option 1, matching the project's existing
small-sample discipline, but Option 2 is a legitimate choice if
preserving IEX-era sample size matters more than the quiet period.
Whichever is chosen becomes part of Phase 3's implementation scope
(the `data_feed` column is needed either way).

No live cutover happens while Phase 2 (either decision point) is open.

## Phase 3 — live cutover

**Status (2026-08-12): both of Phase 2's decision points are now
resolved.** Decision A (`docs/sip-decision-a-proposal.md`): keep
current thresholds as-is — the evidence for re-deriving them (options
ii/iii) failed the same train/test consistency check
`SCANNER_PLAN.md` already requires, so **no `TIER_HIGH`/`TIER_MEDIUM`/
detector `atr_units` values change as part of this cutover.** Decision
B: Option 1 (post-cutover-only), as already recommended above.

Step 1 below has grown since it was first scoped — building it
surfaced a second, related gap (`docs/broad-scan-honesty-proposal.md`):
broad_scan-promoted ("screening") symbols were journaled
indistinguishably from real watchlist hits everywhere a subscriber
sees them, including the same three stats functions Decision B already
needed to touch. Both fixes share the same schema migration and the
same three `journal.py` queries, so they shipped together rather than
as two separate migrations touching the same code twice. The bundle
described below (branch `sip-phase3-bundle`) is complete, tested (700
tests passing), and awaiting your review — **not merged, not
deployed.**

1. Ship, as one change set:
   - the `data_feed` column (Decision B) — the `DETECTOR_DATA_FEED`
     config-value code itself already shipped back in Phase 1's
     prerequisite, still defaulting to `"iex"`;
   - the `origin` column (`'watchlist'`/`'screening'`,
     `docs/broad-scan-honesty-proposal.md` finding (a)/(b)) —
     resolved once per tick in `run_live`'s `scan_symbols` loop from
     `symbol in WATCHLIST`, threaded through `process_new_bar()` into
     `journal.write_cluster()`, same frozen-at-write-time discipline as
     `symbol_class`;
   - `historical_performance()`/`tier_performance()`/`kind_performance()`
     filtering to the current feed AND to `origin = 'watchlist'`
     (`journal.CURRENT_FEED_FILTER_SQL`) — "current feed" is read from
     the journal's own most recent row, not the live config value, so
     every pre-migration row (`data_feed IS NULL`) and every
     screening-origin row is excluded from these three functions by
     construction, no separate backfill required for this part;
   - the plain-text "· RADAR" tag on `render_high_alert`/`render_digest`
     for screening-origin clusters (SCANNER_PLAN.md's "exactly one
     emoji, the tier marker" rule ruled out an emoji badge) and the
     matching dashboard badge (`/signals/*` endpoints now return
     `origin`; `SignalCard`/`SignalDetail` render a neutral "RADAR" pill).

   This step is a normal code change/deploy, same as any other, and
   the `DETECTOR_DATA_FEED` piece specifically is still a no-op for
   live behavior until the env var is actually set to `"sip"` — the
   `origin`/screening-labeling piece, however, is live-behavior-visible
   as soon as this deploys (broad_scan-promoted alerts get tagged
   immediately, independent of the feed cutover timing).
2. **Flip at a session boundary, not mid-session**, in this order —
   `tradebot.runner` already "runs once per trading day and exits at
   the close" (`README.md`), so do this between one session's close and
   the next session's open:
   a. **Refresh the live cache first.** Same anchor mechanism explained
      in Phase 1: `data/cache/` is what `build_anchors()` actually reads
      (`runner.py:91`/`:771`/`:1014`, `full_session_rth_bars()` at
      `runner.py:165-169`), live mode included — it is never a fresh
      vendor call. Overwrite it in place with SIP data, on the VPS,
      after confirming Phase 0's archive copy is safe:
      ```
      DETECTOR_DATA_FEED=sip python3 scripts/fetch_cache.py
      ```
      (no `--cache-dir` override this time — this intentionally targets
      the live `data/cache/` default.) Skipping this step is exactly
      the failure mode this migration exists to prevent: the first
      live SIP session would evaluate fresh SIP volume against a
      baseline still built from IEX-cached history.
   b. **Only then** set `DETECTOR_DATA_FEED=sip` in `.env` and
      `docker compose up -d` (no rebuild needed) — so the moment SIP
      data starts flowing live, `data/cache/` is already SIP-consistent
      and no single session's bars or baseline are ever a mix of IEX
      and SIP. Never flip while `runner` is actively scanning.
3. **Record the cutover timestamp** — a dated entry in
   `docs/PROGRAM-STATE.md` (matching this doc's own "point-in-time
   facts" convention), which combined with the `data_feed` column is
   enough for any future query to cleanly split "before" from "after"
   without git-log archaeology.

### First-session observation checklist (observe and report only)

For the first live session after cutover — **no threshold changes
happen based on this checklist, regardless of what it shows**. One
session is not the train/test evidence Phase 1/2's discipline requires;
this is a sanity check for "did anything actually break," not a second
round of calibration:

- **Signal rate** — HIGH/MEDIUM/log-tier counts for the session,
  compared against the range Phase 1's backtest predicted for a session
  like this one. Wildly outside that range (not just "different") is a
  signal something's actually broken, not just noisier.
- **Volume-multiple distribution, live vs. backtest** — spot-check a
  handful of watchlist symbols' actual live SIP cumulative volume
  against what the IEX-baseline `avg_cum_volume_by_bar` anchors
  expected, to see in practice, on the first real day, how far live
  reality tracked Phase 1's backtest numbers.
- **Error logs** — `docker compose logs runner` / `data/runner_live.log`
  for exceptions, especially anywhere touching the three vendor call
  sites or `rvol_spike` specifically.
- If something looks genuinely broken rather than merely different,
  that's what the kill switches and the rollback below are for — not an
  ad-hoc threshold tweak in the moment.

## Rollback plan

Because cutover is now a config value, not a code edit, rollback is the
same operation in reverse: set `DETECTOR_DATA_FEED=iex` (or delete the
line — `"iex"` is the default) in `.env`, `docker compose up -d` (no
rebuild), same-day, no git revert needed. Two things make this safe:

- `data/journal.db` is append-only and never needs correcting — a
  session scanned under SIP is journaled as real data either way; it
  doesn't need to be "undone," only future sessions need to go back to
  IEX. `write_cluster`'s upsert-by-identity behavior means a rolled-back
  session's rows aren't touched by anything after it.
- The existing kill switches (`data/HALT`, per-user `/halt`,
  `WATCHTOWER_KILL_SWITCH`) already stop alerting immediately if a
  post-cutover session looks wrong in a way that can't wait even for a
  same-day config rollback — no new stop mechanism needed for this
  migration specifically.

**Rollback trigger criteria** (to define concretely before Phase 3, not
left to judgment in the moment): a maximum acceptable deviation in
daily HIGH-tier alert count vs. the Phase 1 backtest's predicted range,
checked against the first N live sessions post-cutover. Exact N and the
acceptable band are worth pinning down as part of Phase 2's sign-off,
once real backtest numbers exist to anchor them against.

## Open questions

- ~~Does the current Alpaca account entitlement cover SIP historical-bar
  requests?~~ **Settled above — yes**, both daily and intraday.
- How large a backtest window does Phase 1 need to trust the volume-
  multiple distribution and signal-count comparison? `SCANNER_PLAN.md`'s
  own "Best hours" section flags n=5-per-bucket as too thin to trust —
  worth sizing this deliberately rather than reusing whatever's already
  cached by default (20 sessions per `fetch_cache.py`'s current
  `--sessions-n` default).
- `broad_scan.py`'s own RVOL_THRESHOLD check (Stage 1 screening across
  the full active universe, not just the fixed watchlist) shares the
  identical IEX-baseline dependency, per its own docstring — the
  feed-site audit above confirms it has no separate feed site to flip
  (it consumes `fetch_daily_bars_bulk`'s output directly), so it
  automatically follows whatever `DETECTOR_DATA_FEED` is set to. Still
  worth an explicit line item in Phase 1's backtest scope (the broader
  universe, not just the 17-symbol watchlist) rather than assuming
  watchlist-only backtest evidence covers it.

## What approval unlocks at each stage

- Approving this document: unlocks Phase 0 (archive), the small inert
  `DETECTOR_DATA_FEED` config-value code (still defaults to `"iex"`,
  no live behavior change), and Phase 1 (SIP replay tooling + backtest
  evidence-gathering). No live feed change, no schema change.
- Phase 1's output, once delivered: two decision points on Phase 2 —
  recalibration (A) and stats treatment (B) — each its own approval.
- Both resolved: unlocks Phase 3 — ship the `data_feed`/`origin` schema
  and stats/labeling changes (branch `sip-phase3-bundle`, built and
  tested, awaiting review), then the actual session-boundary cache
  refresh + flip of `DETECTOR_DATA_FEED` to `"sip"`, each still gated
  on explicit go-ahead.
- Separately, low priority, after Phase 3's code ships: a one-off
  offline script (`scripts/backfill_detection_origin.py`, not yet
  written) reconstructing `origin` for rows journaled before this
  migration, via each row's `code_version` cross-referenced against
  `WATCHLIST`'s git history at that commit — see
  `docs/broad-scan-honesty-proposal.md` finding (d) for the method and
  its caveats. Report-only (no journal writes) until its own approval.

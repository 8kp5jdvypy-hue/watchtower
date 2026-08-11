# SIP Phase 1 backtest report

Delivers `docs/sip-migration-proposal.md`'s Phase 1: SIP replay tooling
built, IEX-vs-SIP backtest run for the watchlist, a separate
universe-wide volume-multiple line item, and a backtest-window
recommendation. **This is evidence only — no recalibration decision
(Decision A) or live cutover is made or implied here.**

## Methodology

- **Watchlist backtest**: 23 sessions (2026-06-29 through 2026-08-10,
  minus a few dates present in one cache but not the other — see
  "Window size" below), all 17 watchlist symbols, replayed twice via
  `scripts/replay.py`'s new `--cache-dir`/`--db-path` flags — once
  against the existing IEX cache, once against a freshly built SIP
  cache (`DETECTOR_DATA_FEED=sip python3 scripts/fetch_cache.py
  --cache-dir data/cache-sip`) — into two separate journal DBs.
  **Critically: each run's historical baseline (`avg_cum_volume_by_bar`)
  was built from the SAME cache directory as that run's live
  evaluation bars** — i.e. this measures the migration's target end
  state (SIP-current vs. SIP-baseline, both properly rebuilt), not the
  dangerous mixed state (SIP-current vs. stale-IEX-baseline) the
  proposal's Phase 3 ordering exists to prevent. Both are useful
  numbers; this report is the former.
- **Universe volume-multiple line item**: full active universe (13,026
  symbols, `tradebot.universe.active_symbols()`), daily bars only (no
  intraday, no detector replay — broad_scan's own screen is
  daily-bar-only, see `screen_snapshot()`), 30-day lookback, both feeds,
  via `fetch_daily_bars_bulk` (chunked, ~9 bulk calls per feed).

## Watchlist: signal counts by kind and tier

**Cluster-level tier totals** (one row per detection cluster,
regardless of how many kinds fired together):

| Tier | IEX | SIP | Δ |
|---|---|---|---|
| high | 65 | 67 | +3.1% |
| medium | 740 | 624 | -15.7% |
| log | 2569 | 2542 | -1.1% |

**Per-kind instance counts** (a cluster with multiple kinds counts once
per kind — this is why these totals exceed the cluster-level counts
above):

| Kind | IEX | SIP | Δ |
|---|---|---|---|
| vwap_break | 1652 | 1626 | -1.6% |
| level_break | 1085 | 1068 | -1.6% |
| range_expansion | 721 | 653 | -9.4% |
| round_number_break | 305 | 234 | -23.3% |
| gap | 76 | 72 | -5.3% |
| **rvol_spike** | **16** | **6** | **-62.5%** |

**The one detector this migration is specifically about moved in the
opposite direction from the anecdotal concern.** The proposal's stated
risk was "SIP volume runs 20-42x IEX's, so `rvol_spike` would fire on
almost everything" if flipped without a baseline rebuild. With both
sides properly rebuilt (this test's setup), `rvol_spike` actually fired
*less* under SIP, not more. This is evidence the specific failure mode
is avoidable **as long as the baseline is genuinely rebuilt before
cutover** — it is not evidence that SIP is safe if that step is
skipped; skipping it is expected to reproduce something close to the
originally-feared explosion, since a SIP-current bar would then be
compared against the old, much-smaller IEX-scale baseline.

**Caveat on `rvol_spike` specifically**: n=16 (IEX) / n=6 (SIP) is a
very thin sample — see "Window size" below. This direction is real
evidence, not noise (both counts are small, but the drop is consistent
with the mechanism: a properly-rebuilt SIP baseline is itself much
larger, so the same live volume clears it less often), but it is not
enough to certify SIP-safety for `rvol_spike` on its own. Nothing about
tier/threshold values changes based on this — that stays Decision A's
call, later, with more evidence if this direction holds up.

## Watchlist: volume-multiple distribution (real, not anecdotal)

Per symbol, SIP:IEX cumulative-volume ratio across the same 23
sessions (391 symbol-sessions total):

| Symbol | Mean | Median | Min | Max |
|---|---|---|---|---|
| QQQ | 71.99 | 71.51 | 55.37 | 97.73 |
| USO | 81.59 | 79.45 | 47.82 | 131.38 |
| TSLA | 40.54 | 38.25 | 30.05 | 55.89 |
| AMD | 41.20 | 40.91 | 22.34 | 56.54 |
| PLTR | 37.23 | 37.64 | 27.21 | 48.85 |
| COIN | 36.03 | 33.10 | 26.60 | 49.37 |
| SPY | 32.05 | 31.14 | 26.88 | 42.68 |
| IONQ | 30.66 | 29.10 | 19.44 | 52.40 |
| BE | 30.01 | 29.69 | 22.60 | 39.84 |
| AAPL | 29.16 | 28.29 | 23.21 | 41.33 |
| NVDA | 28.83 | 27.31 | 19.78 | 40.69 |
| GOOGL | 27.85 | 27.94 | 21.71 | 34.64 |
| MSFT | 27.75 | 25.60 | 21.51 | 45.61 |
| META | 24.99 | 25.06 | 18.00 | 37.57 |
| SMCI | 23.76 | 22.81 | 17.21 | 40.73 |
| AMZN | 21.71 | 21.73 | 15.57 | 27.22 |
| IWM | 19.94 | 17.76 | 11.75 | 29.52 |
| **All** | **35.61** | **29.97** | **11.75** | **131.38** |

Confirms and refines the previously-recorded anecdotal figures (SPY
26x, NVDA 20x, TSLA 42x — all within range of what a full 23-session
sample shows for those symbols) and reveals **real per-symbol
variation the single-session anecdote couldn't show**: QQQ and USO run
70-80x on average, IWM runs closer to 18-20x — a 4x spread across the
watchlist that a single global multiplier would flatten.

## broad_scan universe: volume-multiple distribution (its own line item, as requested)

13,026 active universe symbols, daily bars, 30-day lookback. 12,829
returned IEX data, 13,023 returned SIP data (SIP's broader
venue coverage means it sees a few symbols IEX doesn't trade at all).

**Two findings, not one:**

1. **Among symbol-days where IEX shows ANY volume** (184,571
   symbol-days compared): mean ratio 75.22x, median 25.51x — broadly
   consistent with the watchlist's own median (~30x), with a much
   fatter tail (p95 = 237x, p99 = 702x, max = 174,678x on presumably a
   near-zero-IEX-volume micro-cap) than the watchlist shows. No
   symbol-day had SIP volume lower than IEX (structurally expected —
   SIP is the consolidated tape, IEX is one venue within it).

2. **90,622 symbol-days (32.9% of all universe symbol-days compared)
   showed literally zero IEX volume while having real SIP volume** —
   excluded from the ratio calculation above (undefined/infinite
   ratio), but this is arguably the single most important number in
   this report for the universe specifically: for roughly a third of
   symbol-days across the active universe, **an IEX-only volume screen
   sees no trading activity at all** where real trading is happening.
   `broad_scan.py`'s `RVOL_THRESHOLD` check would be structurally blind
   to these, not just miscalibrated, under the current IEX-only feed —
   a materially different (and worse) problem than "the multiplier is
   large," and one that recalibrating thresholds alone can't fix,
   since `rvol = volume / avg_volume` is undefined/zero on both sides
   when the baseline itself is zero.

This is exactly the kind of finding the proposal's "own line item"
instruction was meant to surface — the universe's problem shape is
different from the watchlist's, not just a bigger version of it.

## Window size: recommendation

**This backtest used 23 sessions, not the intended 30** — the SIP
fetch walked back 30 trading days from "yesterday" (2026-08-10), but 7
of those 30 didn't have an IEX counterpart already cached (3 very
recent dates newer than the existing IEX cache's last date, 4 dates in
an early-July gap in the existing IEX cache). 23 was the actual
intersection used for a clean apples-to-apples comparison.

**Recommendation: widen to 60-90 sessions before this evidence is used
to inform Decision A**, specifically because of `rvol_spike`'s small
sample size here (n=16 IEX / n=6 SIP, and **zero** HIGH-tier
`rvol_spike` detections on either side in this window — nowhere near
enough to say anything about whether SIP changes `rvol_spike`'s
HIGH-tier behavior specifically). At this window's ~4% per-symbol-session
hit rate for `rvol_spike` (16/391), even 60-90 sessions would only
produce on the order of 40-60 `rvol_spike` detections total — still
thin by `SCANNER_PLAN.md`'s own standard (it flagged n=5-per-bucket as
too thin to trust for a 7-bucket split), but a real improvement over
this pass, and probably the practical ceiling before the "gather more
evidence" cost stops being worth it for a first recalibration read.
**A HIGH-tier-specific verdict for `rvol_spike` would need a much
larger window still** (likely several hundred sessions, given HIGH-tier
`rvol_spike` events are rarer than medium-tier ones) — worth flagging
now rather than discovering after a 90-session run still comes up
empty at HIGH tier specifically.

This recommendation is about evidence quality for a *future* Decision
A discussion — it does not change anything about what's authorized now
(Phase 0/1 only, per the standing approval).

## Artifacts

All gitignored (`data/`, `out/` — nothing here is committed):

- `data/cache-sip/` — the built SIP cache, 17 symbols × 30 sessions
- `data/cache-iex-subset/`, `data/cache-sip-subset/` — the 23-session
  matched intersection actually replayed
- `data/journal_iex.db`, `data/journal_sip.db` — full replay output,
  queryable directly for anything beyond what's summarized here
- `out/replay_iex.csv`, `out/replay_sip.csv` — per-cluster CSV dumps
- `data/_universe_iex_bars.pkl`, `data/_universe_sip_bars.pkl` — raw
  universe daily-bar volumes, both feeds (large; not meant to be
  committed, kept for anyone who wants to re-derive a different cut of
  the universe distribution without re-fetching)

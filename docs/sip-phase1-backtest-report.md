# SIP Phase 1 backtest report

Delivers `docs/sip-migration-proposal.md`'s Phase 1: SIP replay tooling
built, IEX-vs-SIP backtest run for the watchlist, a separate
universe-wide volume-multiple line item, and a backtest-window
recommendation. **This is evidence only — no recalibration decision
(Decision A) or live cutover is made or implied here.**

**Extension update (2026-08-12)**: the original 23-session pass below
has been superseded by an 83-session rerun (widened per this doc's own
recommendation) and **surfaced a materially different, more
significant finding than the original pass did** — not just
`rvol_spike`, but a broad decline across every detector kind and every
tier under a properly-rebuilt SIP baseline, traced to SIP's wider
ATR. See "Watchlist: signal counts" below for the corrected numbers and
"What changed and why" for the mechanism. The universe section is
unaffected and unchanged.

## Methodology

- **Watchlist backtest**: 83 sessions (2026-04-01 through 2026-08-05 —
  the intersection of the existing 144-session IEX cache and a
  freshly-built 90-session SIP cache; see "Window size" below for why
  83, not 90), all 17 watchlist symbols, replayed twice via
  `scripts/replay.py`'s `--cache-dir`/`--db-path` flags — once against
  the existing IEX cache, once against the SIP cache
  (`DETECTOR_DATA_FEED=sip python3 scripts/fetch_cache.py --cache-dir
  data/cache-sip`) — into two separate journal DBs, each run's session
  count and cache path printed and confirmed before trusting its
  output. **Critically: each run's historical baseline
  (`avg_cum_volume_by_bar`) was built from the SAME cache directory as
  that run's live evaluation bars** — i.e. this measures the
  migration's target end state (SIP-current vs. SIP-baseline, both
  properly rebuilt), not the dangerous mixed state (SIP-current vs.
  stale-IEX-baseline) the proposal's Phase 3 ordering exists to
  prevent.
- **Universe volume-multiple line item**: full active universe (13,026
  symbols, `tradebot.universe.active_symbols()`), daily bars only (no
  intraday, no detector replay — broad_scan's own screen is
  daily-bar-only, see `screen_snapshot()`), 30-day lookback, both feeds,
  via `fetch_daily_bars_bulk` (chunked, ~9 bulk calls per feed).
  Unaffected by the extension — not rerun, numbers below are unchanged
  from the original pass.

## Watchlist: signal counts by kind and tier

**Cluster-level tier totals** (one row per detection cluster,
regardless of how many kinds fired together):

| Tier | IEX (83 sessions) | SIP (83 sessions) | Δ |
|---|---|---|---|
| high | 313 | 207 | -33.9% |
| medium | 2689 | 1355 | -49.6% |
| log | 9361 | 5722 | -38.9% |

**Per-kind instance counts:**

| Kind | IEX | SIP | Δ |
|---|---|---|---|
| vwap_break | 6021 | 3641 | -39.5% |
| level_break | 4245 | 2544 | -40.1% |
| range_expansion | 2752 | 1494 | -45.7% |
| round_number_break | 1023 | 578 | -43.5% |
| gap | 223 | 124 | -44.4% |
| rvol_spike | 24 | 5 | -79.2% |

**Every detector kind fires meaningfully less often under a
properly-rebuilt SIP baseline — mean clusters/day drops from 148.95 to
87.76, a 41% reduction.** This is a broader and more consequential
finding than the original 23-session pass surfaced (that pass showed
`rvol_spike` alone moving, by a small and thin-sample amount). With 83
sessions, the pattern is unambiguous and consistent across every kind,
including the ATR-priced ones the proposal's own risk analysis assumed
were largely insulated from the feed choice ("narrower than 'the whole
scanner breaks'" — see `vendors/alpaca.py`'s docstrings). That
assumption doesn't hold up under this data.

### What changed and why: SIP's wider ATR, not just volume

The proposal's risk analysis was entirely about `rvol_spike`'s
volume-baseline mismatch. This backtest found a second, distinct
mechanism affecting every ATR-thresholded detector
(`level_break`, `range_expansion`, `vwap_break`, `round_number_break`,
`gap` all price moves against `atr_units * ATR`):

**SIP's consolidated tape produces a wider high-low range per bar than
IEX alone — more venues contributing prints means more extreme highs
and lows — which inflates ATR(14).** Measured directly: SPY's average
`atr14` across these 83 sessions is **0.7246 under IEX vs. 0.8776 under
SIP — 21% wider**. Since these detectors fire when a price move exceeds
`atr_units × ATR`, a wider ATR denominator means the *same* absolute
price move clears the bar less often. This isn't a bug in either feed —
both ATRs are "correct" for what each feed actually saw — but it means
**the migration's threshold-sensitivity isn't confined to `rvol_spike`
and volume**. Any recalibration proposal (Decision A) that only
re-examines `rvol_spike`'s volume threshold would miss this.

**`rvol_spike` specifically**: 24 (IEX) → 5 (SIP), and HIGH-tier
`rvol_spike` went from 3 → 0. Same direction as the original pass, now
on a real sample. The proposal's feared failure mode (SIP volume
against a stale IEX baseline exploding `rvol_spike`'s firing rate)
remains avoidable as long as the baseline is genuinely rebuilt first —
this data doesn't change that conclusion, it just confirms it on a
much larger sample and reveals the ATR effect as an additional,
separate consideration alongside it.

## Watchlist: volume-multiple distribution

Per symbol, SIP:IEX cumulative-volume ratio across the 83 sessions
(1,411 symbol-sessions total):

| Symbol | Mean | Median | Min | Max |
|---|---|---|---|---|
| USO | 102.15 | 94.61 | 41.95 | 266.25 |
| TSLA | 59.50 | 58.21 | 28.20 | 106.03 |
| QQQ | 61.52 | 59.36 | 41.67 | 97.73 |
| AMD | 50.20 | 49.08 | 22.34 | 84.13 |
| PLTR | 42.40 | 40.93 | 21.02 | 80.43 |
| COIN | 42.28 | 41.76 | 22.87 | 70.91 |
| IONQ | 37.45 | 36.82 | 19.44 | 91.38 |
| MSFT | 36.55 | 35.70 | 21.51 | 62.35 |
| SPY | 35.25 | 33.44 | 24.94 | 63.47 |
| NVDA | 34.20 | 33.62 | 19.78 | 60.13 |
| AAPL | 33.83 | 32.02 | 18.34 | 67.27 |
| BE | 33.08 | 31.92 | 21.14 | 50.00 |
| GOOGL | 30.39 | 28.71 | 20.56 | 66.48 |
| META | 31.21 | 30.63 | 18.00 | 55.41 |
| SMCI | 25.58 | 25.02 | 17.21 | 40.73 |
| AMZN | 23.91 | 22.65 | 15.37 | 46.58 |
| IWM | 23.61 | 23.18 | 11.75 | 48.74 |
| **All** | **41.36** | **35.07** | **11.75** | **266.25** |

Consistent with the original 23-session pass (median 35x here vs. 30x
there) and with the previously-recorded anecdotal figures, with a
wider observed tail now that there's more data (max 266x vs. 131x
before) — the more sessions sampled, the more extreme single-session
outliers show up, as expected.

## broad_scan universe: volume-multiple distribution (its own line item, as requested)

Unchanged from the original pass — not rerun, since it doesn't depend
on the watchlist session-count issue above (this uses daily bars over
a fixed 30-day lookback, not the intraday replay window).

13,026 active universe symbols, daily bars, 30-day lookback. 12,829
returned IEX data, 13,023 returned SIP data (SIP's broader
venue coverage means it sees a few symbols IEX doesn't trade at all).

**Two findings, not one:**

1. **Among symbol-days where IEX shows ANY volume** (184,571
   symbol-days compared): mean ratio 75.22x, median 25.51x — broadly
   consistent with the watchlist's own median, with a much fatter tail
   (p95 = 237x, p99 = 702x, max = 174,678x on presumably a
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

## Window size: what was actually achieved, and why

**83 sessions, not 90** — `scripts/fetch_cache.py`'s
`MAX_LOOKBACK_DAYS` safety cap (60 candidate days) stopped the first
extension attempt at 40 sessions; overriding that cap for this one-off
evidence-gathering run (not a change to the committed script) reached
90. Of those 90, 83 overlapped with the existing 144-session IEX cache
(2026-01-02 through 2026-08-05) — the other 7 were dates newer than the
IEX cache's last cached date, excluded from the matched comparison for
a clean apples-to-apples run.

**This resolves the original pass's main caveat.** `rvol_spike` now has
n=24 (IEX) / n=5 (SIP) — better than the original n=16/n=6, though
HIGH-tier `rvol_spike` specifically is still thin (3 → 0). The other
five kinds now have samples in the hundreds to thousands per side,
comfortably past `SCANNER_PLAN.md`'s own n=5-per-bucket caution for
everything except `rvol_spike`'s HIGH tier and `gap`'s HIGH tier (8 →
4). **A HIGH-tier-specific verdict for `rvol_spike` would still need a
much larger window** — this pass doesn't resolve that, it just narrows
what's still open going into Decision A.

## Artifacts

All gitignored (`data/`, `out/` — nothing here is committed):

- `data/cache-sip/` — the built SIP cache, 17 symbols × 90 sessions
- `data/cache-iex-subset90/`, `data/cache-sip-subset90/` — the
  83-session matched intersection actually replayed
- `data/journal_iex90.db`, `data/journal_sip90.db` — full replay
  output for this extended pass, queryable directly for anything
  beyond what's summarized here
- `out/replay_iex90.csv`, `out/replay_sip90.csv` — per-cluster CSV
  dumps
- `data/_universe_iex_bars.pkl`, `data/_universe_sip_bars.pkl` — raw
  universe daily-bar volumes, both feeds (large; not meant to be
  committed, kept for anyone who wants to re-derive a different cut of
  the universe distribution without re-fetching)

The original 23-session artifacts (`data/cache-iex-subset/`,
`data/cache-sip-subset/`, `data/journal_iex.db`, `data/journal_sip.db`)
are superseded but left in place, not deleted.

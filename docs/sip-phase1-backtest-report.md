# SIP Phase 1 backtest report

Delivers `docs/sip-migration-proposal.md`'s Phase 1: SIP replay tooling
built, IEX-vs-SIP backtest run for the watchlist, a separate
universe-wide volume-multiple line item, and a backtest-window
recommendation. **This is evidence only — no recalibration decision
(Decision A) or live cutover is made or implied here.**

**Correction (2026-08-12, second pass):** the first 83-session update
to this report (below, in "What changed and why") reported a broad,
~41% decline across every detector kind and tier under SIP, explained
by a 21%-wider ATR(14). **That finding was substantially wrong** — an
artifact of a second, unrelated mistake in how the SIP cache was
built, caught while preparing the Decision A analysis, before it was
used for anything. See "The second bug" below for the full account and
corrected numbers. The real effect is real but much smaller: roughly a
4% overall decline, concentrated in `round_number_break` and
`rvol_spike`, with most other kinds nearly flat. The universe section
was never affected by either bug and remains valid throughout.

## The second bug: SIP's daily-bar cache was too shallow for ATR14

While preparing the Decision A threshold analysis, a sanity check
(counting distinct sessions with any detection in each journal)
surfaced: `journal_iex90.db` had detections on all 83 replayed
sessions, but `journal_sip90.db` had detections on only **51** — every
session before 2026-05-18 was completely empty, then a hard cutover
to a normal-looking count.

Root cause: `scripts/fetch_cache.py`'s daily-bar fetch
(`ensure_daily()`, `--daily-n`, default 60) is idempotent — it skips
fetching if `daily.csv` already exists — and every SIP cache-extension
pass in the earlier session only ever touched the *intraday* lookback
(`MAX_LOOKBACK_DAYS`), never `--daily-n`. So `data/cache-sip/*/daily.csv`
was left at its very first, default fetch: 60 daily bars, starting
2026-05-15. `ATR(14)` needs the trailing 14 daily bars, i.e. 15 total
(`tradebot/detectors.py`'s `atr()`), and `ReplayMarketData.daily_bars()`
only returns bars dated *strictly before* the replayed session — so for
every session before daily history had accumulated 15 prior bars (in
practice, before 2026-05-18), `atr()` returned `None` and **every
ATR-thresholded detector silently declined to fire, for all 17 symbols,
for 32 of the 83 sessions.** The IEX cache never hit this because its
`daily.csv` already held ~300 bars (built up over the project's real
history, not a single one-shot fetch), so it never needed extending.

This means the "83-session" SIP numbers reported below in the original
pass were really an average over 51 real sessions and 32 structurally-
empty ones — which mechanically drags every mean/rate down and
manufactures the appearance of a much larger decline than is real,
independent of any genuine SIP-vs-IEX effect.

**Fix**: deleted the undersized `daily.csv` files, refetched with
`--daily-n 150` (150 daily bars per symbol, back to 2026-01-06 —
comfortably past the 15-bar minimum for the earliest replayed session,
2026-04-01), copied the corrected files into the matched-intersection
cache used for replay, and reran the SIP replay in the foreground.
Verified before trusting the output: `SELECT DISTINCT session FROM
detections` now returns all 83 sessions on the SIP side, matching IEX.

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
  output (and, after the second bug above, confirmed again by checking
  that every expected session actually produced detections, not just
  that the header line looked right).
- **Universe volume-multiple line item**: full active universe (13,026
  symbols, `tradebot.universe.active_symbols()`), daily bars only (no
  intraday, no detector replay — broad_scan's own screen is
  daily-bar-only, see `screen_snapshot()`), 30-day lookback, both feeds,
  via `fetch_daily_bars_bulk` (chunked, ~9 bulk calls per feed). Never
  touched by either bug above — this doesn't use the replay/ATR path at
  all — numbers below are unchanged from every prior pass.

## Watchlist: signal counts by kind and tier

**Cluster-level tier totals** (one row per detection cluster,
regardless of how many kinds fired together):

| Tier | IEX (83 sessions) | SIP (83 sessions, corrected) | Δ |
|---|---|---|---|
| high | 313 | 296 | -5.4% |
| medium | 2689 | 2333 | -13.2% |
| log | 9361 | 9213 | -1.6% |

Mean clusters/day: **148.95 (IEX) → 142.67 (SIP), a 4.2% decline** —
not the 41% originally (and wrongly) reported.

**Per-kind instance counts:**

| Kind | IEX | SIP (corrected) | Δ |
|---|---|---|---|
| vwap_break | 6021 | 5893 | -2.1% |
| level_break | 4245 | 4152 | -2.2% |
| range_expansion | 2752 | 2516 | -8.6% |
| round_number_break | 1023 | 840 | -17.9% |
| gap | 223 | 216 | -3.1% |
| rvol_spike | 24 | 12 | -50.0% |

**The real picture is much narrower than the first (buggy) pass
suggested.** `vwap_break`, `level_break`, and `gap` are nearly flat
(2-3%) — the ATR-widening mechanism described below is real but small
at the current sample size, not the dominant, broad effect originally
reported. `range_expansion` shows a moderate decline (-8.6%).
`round_number_break` shows the largest ATR-driven decline (-17.9%) —
worth a closer look on its own if Decision A moves forward, since it's
now the clearest outlier among the ATR-thresholded kinds.
`rvol_spike` remains the single largest proportional decline (-50%,
though at n=24→12, this was already known to be a thin sample even
before the daily-cache bug, and is now down to n=12 — too thin to
treat as more than directionally suggestive).

### What changed and why: SIP's wider ATR is real, but modest

SPY's average `atr14` across the corrected 83 sessions: **0.7246
(IEX) vs. 0.7799 (SIP) — 7.6% wider**, not the 21% originally reported
(that number was itself computed from the corrupted SIP journal, so it
inherited the same bug). A ~7.6% wider ATR denominator is consistent
with the modest declines seen above in the ATR-thresholded kinds
(`level_break`, `range_expansion`, `vwap_break`, `round_number_break`,
`gap`, all gated on `atr_units * ATR`) — real, but not the kind of
effect that would justify a sweeping recalibration on its own.

**`rvol_spike` specifically**: 24 (IEX) → 12 (SIP corrected) — still
the standout decline, and this one was never affected by the daily-bar
bug (it depends on `avg_cum_volume_by_bar`, a separate baseline, not
`atr14`). The proposal's original concern — a volume-baseline mismatch
specific to `rvol_spike` — remains the best-supported finding in this
whole report, now on a smaller but still real sample.

## Watchlist: volume-multiple distribution

Per symbol, SIP:IEX cumulative-volume ratio across the 83 sessions
(1,411 symbol-sessions total). Unaffected by the daily-bar bug (this
is computed directly from intraday cache volume columns, not from the
replay/ATR path):

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

Unaffected by either bug above — this uses daily bars over a fixed
30-day lookback and never touches the replay/ATR path at all.

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
intraday-extension attempt at 40 sessions; overriding that cap for
this one-off evidence-gathering run (not a change to the committed
script) reached 90. Of those 90, 83 overlapped with the existing
144-session IEX cache (2026-01-02 through 2026-08-05) — the other 7
were dates newer than the IEX cache's last cached date, excluded from
the matched comparison for a clean apples-to-apples run.

**This resolves the original pass's main caveat, on the corrected
data.** `rvol_spike` now has n=24 (IEX) / n=12 (SIP) — better than the
very first n=16/n=6 pass, though still thin, especially at HIGH tier.
The other five kinds now have samples in the hundreds to thousands per
side, comfortably past `SCANNER_PLAN.md`'s own n=5-per-bucket caution.
**A HIGH-tier-specific verdict for `rvol_spike` would still need a much
larger window** — this pass doesn't resolve that, it just narrows what
was already known to be thin.

## Artifacts

All gitignored (`data/`, `out/` — nothing here is committed):

- `data/cache-sip/` — the built SIP cache, 17 symbols × 90 sessions,
  `daily.csv` now 150 bars deep per symbol (was 60 — see "The second
  bug" above)
- `data/cache-iex-subset90/`, `data/cache-sip-subset90/` — the
  83-session matched intersection actually replayed
- `data/journal_iex90.db`, `data/journal_sip90.db` — full replay
  output for this extended pass (SIP side rebuilt after the daily-bar
  fix), queryable directly for anything beyond what's summarized here
- `out/replay_iex90.csv`, `out/replay_sip90.csv` — per-cluster CSV
  dumps
- `data/_universe_iex_bars.pkl`, `data/_universe_sip_bars.pkl` — raw
  universe daily-bar volumes, both feeds (large; not meant to be
  committed, kept for anyone who wants to re-derive a different cut of
  the universe distribution without re-fetching)

The original 23-session artifacts (`data/cache-iex-subset/`,
`data/cache-sip-subset/`, `data/journal_iex.db`, `data/journal_sip.db`)
are superseded but left in place, not deleted.

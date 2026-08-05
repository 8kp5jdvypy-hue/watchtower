# Watchtower — Scanner Plan

## Purpose

Detect notable intraday conditions on a fixed watchlist and push alerts
to Telegram. Read-only: never places orders, no broker write access. See
CLAUDE.md for the engineering rules this project follows.

## Watchlist

SPY, QQQ, GOOGL, TSLA, BE, IONQ

## Architecture

1. `tradebot/detectors.py` — pure detection functions: `Bar`,
   `DailyAnchors`, `build_anchors()`, `level_break`, `rvol_spike`,
   `range_expansion`, `gap`, `score_cluster()`, `tier_for_score()`.
2. `tradebot/marketdata.py` — the `MarketData` protocol and
   `ReplayMarketData` (backtest/replay, cursor-gated).
3. `tradebot/vendors/alpaca.py` — the only file that imports the Alpaca
   SDK. Fetches bars behind the `MarketData` protocol.
4. `scripts/fetch_cache.py` — caches daily + 5-min (premarket + RTH) bars
   per symbol to `data/cache/{symbol}/`.
5. `scripts/replay.py` — walks cached sessions bar by bar, evaluates all
   detectors, clusters same-bar detections, writes
   `out/replay_detections.csv` and journals every cluster.
6. `tradebot/journal.py` — SQLite (`data/journal.db`): `detections` (every
   cluster, including sub-threshold) and `marks` (forward prices at
   +15/+30/+60min).
7. `tradebot/alerts.py` — `format_alert()`, `TelegramAlerter` /
   `ConsoleAlerter`, `AlertBudget` (daily cap, per-detector cooldown,
   hourly medium digest, EOD log summary).
8. `tradebot/costs.py` — `breakeven_move()` for the ATM option contract,
   shown in every alert.
9. `tradebot/runner.py` — the live 5-minute loop.

## Tiers

Detections are scored in ATR units (or a ratio, for `rvol_spike`) and
bucketed by `tier_for_score()`:

- `TIER_HIGH = 3.0`, `TIER_MEDIUM = 1.5` (calibrated 2026-08-05 from a
  20-session replay across the full watchlist — see
  `out/replay_detections.csv` and `detectors.py`'s comment for the
  derivation). Targets ~2-5 high-tier clusters/day across the watchlist.

## Alert format

Plain text with emojis, not HTML/Markdown — renders cleanly in both
Telegram and ConsoleAlerter's stdout with no parse_mode or escaping
needed. One cluster per message for high tier; medium tier is batched
into an hourly digest; log tier only appears in the end-of-day summary,
not as individual messages.

```
{tier_emoji} {TIER} — {symbol} {trend_emoji}
{kinds}

{headlines}

📊 Score: {score:.2f} ATR
💵 Close: ${close:.2f}  (ATR14: {atr14})
⚖️ Breakeven (60m): {breakeven}
📐 Range: ${opening_range_low:.2f}-${opening_range_high:.2f}  |  Prior close: ${prior_close:.2f}
💹 Quote: ${bid:.2f} / ${ask:.2f}  (last ${last:.2f})

🕐 {ts_et} ET
🆔 {id} · v{code_version}
```

- `{tier_emoji}` — 🔴 HIGH, 🟡 MEDIUM, ⚪ LOG.
- `{trend_emoji}` — 📈 up, 📉 down (close vs. `prior_close`).
- `{TIER}` — uppercase: `HIGH`, `MEDIUM`, or `LOG`.
- `{kinds}` — the cluster's detector kinds, comma-and-space joined (e.g.
  `gap, rvol_spike`).
- `{headlines}` — the constituent detections' headlines, semicolon
  joined, as already stored in the journal.
- `{atr14}` — two decimals, or `n/a` if unavailable.
- `{breakeven}` — from `costs.breakeven_move()` for a 60-minute hold of
  the nearest-ATM contract, shown as `X.XX% (Y.YY ATR)`, or
  `no tradable contract` if the chain is unavailable or the ATM contract
  fails the liquidity filter (spread > 12% of mid, or open interest <
  500) — never a guessed delta.
- `{ts_et}` — `YYYY-MM-DD HH:MM` in US/Eastern.
- `{id}` — the journal's detection id.
- `{code_version}` — the short git hash the cluster was journaled under.

Digests, log summaries, system notices (halt, staleness, cap reached,
errors), and the heartbeat all follow the same emoji-plus-plain-text
convention — see `tradebot/runner.py`'s message builders.

## Non-goals

No order placement. No broker write access. Live alerting is opt-in
(`--live`); default is log-only (`ConsoleAlerter`).

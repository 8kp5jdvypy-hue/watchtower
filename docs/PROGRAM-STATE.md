# Program state

Point-in-time facts about what's actually built, deployed, and off-limits
to change without a deliberate decision — the things a plan (a migration,
a redesign) needs to check against before assuming otherwise. Not a
running log; update entries in place as reality changes, don't append a
history here (that's what git log is for).

## Live-quotes display path: built and deployed

**Status as of 2026-08-11: live in production**, not just merged to
`main`. The dashboard's real-time price display —
`GET /quotes` (`tradebot/api/app.py`), `fetch_quotes()`/
`fetch_latest_quotes()` (`tradebot/marketdata.py`,
`tradebot/vendors/alpaca.py`), and the polling `useQuotes` hook
(`web-app/src/hooks/useQuotes.js`) — is running on the production VPS
(`api.perchmarkets.com`) and the production Cloudflare Worker
(`app.perchmarkets.com`), confirmed live-verified against both, not
inferred from the git history.

This path runs **under the Alpaca beta approval** on the account backing
`ALPACA_KEY_ID`/`ALPACA_SECRET_KEY`. Any change to what market data this
path is allowed to request (a different feed, a different entitlement
tier, a different provider) is a question for that approval, not just a
code change.

## Guardrail: the SIP display path must not be unified into the detector feed

`tradebot/vendors/alpaca.py` deliberately runs **two different feeds**
for two different purposes, and this split is intentional, not
leftover inconsistency to "clean up":

- `fetch_daily_bars`, `fetch_intraday_bars`, `fetch_daily_bars_bulk` —
  **IEX**. This is what every detector actually evaluates
  (`tradebot/detectors.py`), and `rvol_spike`'s volume baseline
  (`avg_cum_volume_by_bar`, built from IEX-cached replay history via
  `scripts/fetch_cache.py`) is calibrated against IEX's volume, which
  live-measurement showed runs 20-42x lower than SIP's on this
  watchlist (real numbers: SPY 26x, NVDA 20x, TSLA 42x, same session,
  same RTH window — see the docstrings in `vendors/alpaca.py`). Flipping
  these to SIP without a real recalibration pass would make
  `rvol_spike` fire on almost everything.
- `fetch_latest_quote`, `fetch_latest_quotes` — **SIP**. Feeds the
  dashboard's `/quotes` endpoint only (price display, no detector or
  baseline dependency) — pure upside from the tighter, more complete
  consolidated-tape quote, with none of the above recalibration
  blocker.

**During the upcoming data-provider migration: do not fold the SIP
display path into whatever config drives the detector feed.** They are
answering different questions (what does a human see right now, vs. what
did the model actually decide against) and have different calibration
requirements. If the detector feed itself moves to SIP or another
provider, that move has to carry its own baseline recalibration — the
display path already being on SIP is not evidence that the detector feed
is ready to follow, and unifying the two into one feed setting would
silently reintroduce the exact volume-baseline mismatch this split
exists to avoid.

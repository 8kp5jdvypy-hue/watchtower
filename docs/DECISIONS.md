# Decision Log

Perch/Watchtower decisions that are not derivable from the code. One
entry per decision, newest first. Longer decision records keep their
own file (e.g. `tiingo-licensing-decision-2026-09.md`,
`sip-decision-a-proposal.md`) and get a pointer here.

Statuses: **approved**, **proposed**, **superseded**.

## 2026-09-17 — Market prices come from free official sources, not SocialCrawl

- **Status:** approved (owner, 2026-09-17); implemented locally
  2026-09-18 on branch `feature/price-feed-free-sources`, not deployed.
  Perch remains on hold; nothing here restarts the VPS stack.
- **Decision:** the bot's price feed (`tradebot/pricefeed.py`) uses
  Coinbase Exchange public API as the crypto primary and Kraken public
  API as the crypto fallback; Alpaca Market Data on the **IEX** feed as
  the US-equity primary and Finnhub free tier as an optional equity
  fallback that is inert until the owner creates a key and sets
  `FINNHUB_API_KEY`. Ordered fallback, 5-minute TTL cache, stale values
  served only when flagged `stale=True`, and nothing fabricated when
  every source is down.
- **Excluded:** SocialCrawl `/v1/finance/quote` (5 credits/call ≈
  $1,080/month at 10 instruments every 5 minutes for free data);
  Binance.com (geo-blocked from US IPs; Binance.US acceptable but fewer
  pairs, not needed); Yahoo Finance scraping (unofficial, ToS risk).
  Detectors never read this feed — bars still come only through the
  `MarketData` protocol.
- **Kept paid:** SocialCrawl `/v1/polymarket/research` for prediction
  markets, hourly, ~20 markets ≈ $6/day. Out of scope for this module;
  the SocialCrawl key does not enter this repository or the VPS `.env`
  by this change.
- **Consumers (2026-09-18):** the Telegram bot's `/status` prints one
  line per asset class (existing command, no BotFather change needed)
  and the API exposes `/prices` for the web app. The dashboard's
  `/quotes` (SIP bid/ask) is unchanged. No `/price` command was added
  because the command registry must match BotFather exactly and the
  bot refuses to start on drift -- that is the owner's call.
- **Why:** run rate for prices stays at $0 with 100×+ rate-limit
  headroom (`docs/price-feed-cost-note-2026-09.md`), and IEX keeps the
  feed independent of the Algo Trader Plus subscription.
- **Accounts:** no new accounts or keys were created. Alpaca keys
  already exist in the VPS `.env`. Finnhub is the owner's call.

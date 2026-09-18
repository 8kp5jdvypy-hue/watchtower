# Decision Log

Perch/Watchtower decisions that are not derivable from the code. One
entry per decision, newest first. Longer decision records keep their
own file (e.g. `tiingo-licensing-decision-2026-09.md`,
`sip-decision-a-proposal.md`) and get a pointer here.

Statuses: **approved**, **proposed**, **superseded**.

## 2026-09-18 — Alpaca paid plan cancelled; every Alpaca call is now free-tier IEX

- **Status:** approved (owner cancelled Algo Trader Plus 2026-09-18);
  code change on branch `fix/quotes-on-free-alpaca-plan`.
- **Decision:** Perch runs on Alpaca's free market-data plan. The
  quote-display path (`fetch_latest_quote(s)`: dashboard `/quotes`,
  postmarket discovery/external-context shadows) moves from hardcoded
  SIP to `QUOTE_DATA_FEED` (default `iex`), with `last` taken from the
  IEX last trade rather than the quote mid because IEX bid/ask can be
  dollars wide off-hours. `QUOTE_DATA_FEED=sip` restores the old
  behaviour if a SIP plan is ever active again. Detector bars were
  already IEX by default and are unchanged.
- **Consequence:** the SIP migration proposal (`docs/sip-migration-
  proposal.md`, Decision A) is moot until a SIP entitlement exists
  again. `DETECTOR_DATA_FEED=sip` must not be set anywhere.
- **Evidence:** live 2026-09-18 — SIP latest quote refused
  ("subscription does not permit querying recent SIP data"); IEX
  latest trade, IEX daily/intraday bars, and the indicative options
  chain all served.

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
  `/quotes` (SIP bid/ask) is unchanged. `/price [SYMBOL ...]` was
  added to the command registry (owner's call, 2026-09-18); the first
  start after merge must run with `--sync-commands` so BotFather
  matches, or the drift check refuses to start.
- **Why:** run rate for prices stays at $0 with 100×+ rate-limit
  headroom (`docs/price-feed-cost-note-2026-09.md`), and IEX keeps the
  feed independent of the Algo Trader Plus subscription.
- **Accounts:** no new accounts or keys were created. Alpaca keys
  already exist in the VPS `.env`. Finnhub is the owner's call.

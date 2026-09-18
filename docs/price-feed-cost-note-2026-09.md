# Price feed cost note — September 2026

**Date:** 2026-09-18
**Scope:** `tradebot/pricefeed.py` and its vendor adapters
(`vendors/coinbase.py`, `vendors/kraken.py`, `vendors/finnhub.py`,
`vendors/alpaca.fetch_latest_trade_prices`). Decision record:
`docs/DECISIONS.md` (2026-09-17).

## Bottom line

The price feed's incremental run rate is **$0/month** on every source
it uses, at the configured cadence, with headroom of two to three
orders of magnitude on every rate limit. The only paid data path Perch
is deliberately keeping is Polymarket research through SocialCrawl,
and that is not part of this module.

## Cadence assumption

`DEFAULT_INSTRUMENTS` = 9 instruments (BTC, ETH, SOL; SPY, QQQ, GOOGL,
TSLA, BE, IONQ). `DEFAULT_TTL` = 5 minutes, so at most **288
refreshes/day** per instrument, and only when something asks
(`PriceFeed.get()` is pull-based; an idle bot makes zero calls).

## Per-source request budget vs. limit

| Source | Role | Requests per refresh | Per day (worst) | Published free limit | Utilisation | $/month |
|---|---|---|---|---|---|---|
| Coinbase Exchange public `/products/{id}/ticker` | crypto primary | 1 per crypto instrument (3) | 864 | 10 req/s per IP | 0.1% | $0 |
| Kraken public `/0/public/Ticker` | crypto fallback | 1 batched call | 288 (only when Coinbase fails; 0 in normal operation) | ~1 req/s sustained | 0.3% | $0 |
| Alpaca Market Data, IEX feed, latest trade | equity primary | 1 batched call | 288 | 200 req/min (free tier) | 0.1% | $0 |
| Finnhub `/quote` | equity fallback, **optional**, inert until `FINNHUB_API_KEY` is set | 1 per equity instrument (6) | 1,728 (only when Alpaca fails) | 60 req/min | 2% | $0 |

Live check 2026-09-18 16:17 UTC from the dev Mac: one `feed.get()`
served 9/9 in 1.46 s using 2 source batches (Coinbase, Alpaca); Kraken
0 calls; the immediate second `get()` was a cache hit (0 calls, <1 ms).
Coinbase and Kraken BTC last trades differed by 0.02%.

## Alpaca plan note

**Updated 2026-09-18:** the owner cancelled the Algo Trader Plus
subscription ($99/month) the same day. Perch's Alpaca cost is now
**$0** and every Alpaca call it makes is on the free plan:

- `fetch_latest_trade_prices` (this module) — IEX, unaffected.
- Detector bars — IEX by default (`DETECTOR_DATA_FEED` unset);
  **`DETECTOR_DATA_FEED=sip` must not be set on the VPS** or the
  scanner's own fetches will be refused.
- Dashboard `/quotes` and the two postmarket shadow quote fetches —
  were hardcoded SIP and were refused live ("subscription does not
  permit querying recent SIP data"); moved to `QUOTE_DATA_FEED`
  (default `iex`, `last` = IEX last trade) the same day.
- Options chain — indicative feed, live-verified working (642 SPY
  contracts).

`docs/sip-migration-proposal.md` and `docs/sip-decision-a-proposal.md`
describe a SIP era that no longer exists; they are kept as history.

## What was avoided

SocialCrawl `/v1/finance/quote` at 5 credits/call, the owner's
2026-09-17 figure: 10 instruments × 288 refreshes × 30 days × 5
credits = 432,000 credits ≈ **$1,080/month** for numbers Coinbase and
Alpaca return for $0. The 9-instrument list above would be ≈ $972/month
on that route. `tests/test_pricefeed.py::test_socialcrawl_is_not_a_source`
pins this.

## Outside this module (for the whole-picture number)

- **Polymarket research via SocialCrawl `/v1/polymarket/research`**, 5
  credits/call, ~20 markets refreshed hourly: 480 calls/day × 5 =
  2,400 credits/day ≈ **$6/day ≈ $180/month**. This is the one scraper
  use the 2026-09-17 decision keeps. It is not implemented in Perch by
  this change, and the SocialCrawl key stays where it is (the
  FirstPounce project's gitignored `.secrets/`); wiring it into Perch
  would need the owner to decide where that key lives on the VPS.
  Worth knowing, not a change to the decision: Polymarket's own public
  REST (gamma-api / clob) serves raw market prices keyless at $0, if
  raw prices rather than research summaries turn out to be what the
  bot needs.
- **Droplet** `ubuntu-s-1vcpu-2gb-nyc1`: unchanged by this note; it is
  powered on and billing while the stack is on hold (see
  `docs/DEPLOYMENT.md`).

## If the cadence or list grows

Cost stays $0 until a *rate limit* binds, not a price. The first to
bind is Finnhub's 60/min in fallback mode at ~50 equities on a 5-minute
TTL; Coinbase's 10/s would take ~3,000 crypto instruments; Alpaca's
batched call is flat in instrument count up to `BULK_FETCH_CHUNK_SIZE`
(1,500). Shortening the TTL below ~30 s is the only realistic way to
approach any limit with today's list.

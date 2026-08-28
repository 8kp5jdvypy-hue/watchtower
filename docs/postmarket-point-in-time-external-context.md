# Postmarket point-in-time external context

`tradebot/postmarket_external_context.py` records external facts as observations,
not timeless symbol attributes. Every append-only row identifies its candidate,
fact kind, effective/source time, observation time, provider, feed, endpoint,
canonical payload digest, revision, run, attempt, status, and error code.

## Available version 1 facts

- For known after-hours earnings, a pre-close worker captures the nearest
  complete call/put strike during the final ten minutes of RTH. Only this
  pre-close snapshot may populate `OPTIONS_EXPECTED_MOVE`; it stores the
  indicative ATM straddle as a percentage of the contemporaneous quote midpoint,
  contract IDs, option/stock timestamps, expiry, open interest, and the semantic
  `pre_event_expected_move_baseline`.
- A chain fetched after a candidate appears is instead stored as
  `CURRENT_OPTION_MARKET_CONTEXT`, with the semantic
  `post_detection_current_context_not_pre_event_expectation`. It can describe the
  remaining option market but can never be used retroactively as the event's
  expected-move baseline. Unscheduled symbols without a pre-close snapshot get
  an explicit `NO_PRE_EVENT_OPTION_SNAPSHOT` reason.
- `NEWS` stores only attributable metadata for provider-symbol-tagged Alpaca/
  Benzinga items knowable by observation time. Future items and unrelated symbols
  are excluded. A symbol tag is explicitly not treated as proof of causality.
- `INDEPENDENT_PRICE_COMPARISON` uses Massive's separate stock-aggregate
  endpoint when `MASSIVE_API_KEY` (or legacy `POLYGON_API_KEY`) is present.
  It compares unadjusted RTH-close and candidate five-minute bars at exact
  timestamps, never forward-fills a missing aggregate, and marks disagreement
  when either close differs by more than 50 basis points or direction differs.
  The result is post-detection reconciliation, not a signal input.
- `SECTOR_CLASSIFICATION` and `FUNDAMENTALS` use that provider's dated ticker
  reference response when configured. SEC SIC is stored as industry
  classification, never mislabeled as GICS sector or sector-ETF mapping. Market
  cap and outstanding-share fields are stored; float remains unavailable. These
  observations are not replay-safe rank inputs because a requested historical
  date is not proof that every underlying filing fact was public then.
- `HALT_STATE` comes from Nasdaq Trader's official trade-halt RSS feed. The
  worker requests halts begun or resumed on the session at most once per batch,
  distinguishes a successful no-match from failure, and never infers a halt
  from missing bars.

Alpaca SIP versus Alpaca IEX is never called independent-provider agreement.
Float, GICS/ETF mapping, carry-forward halts begun on earlier sessions, and
filing-availability-safe historical fundamentals remain explicit gaps.

## Operational isolation

The enrichment worker wakes before the official exchange close to capture known
scheduled-catalyst option expectations, then enriches detected candidates after
close. It runs in a separate Compose service so an option/news call
cannot stall the one-minute discovery loop. It has its own default-off
`POSTMARKET_EXTERNAL_CONTEXT_ENABLED` kill switch, market-aware health probe,
atomic heartbeat, ten-candidate batch, and three-attempt failure cap. It remains
shadow-only and imports no alert, Telegram, outbox, broker, or order path.

The official Alpaca option-chain endpoint exposes latest quote/trade/Greeks and
identifies `indicative` separately from official `opra`; the news endpoint is a
provider metadata source. These sources enrich evidence but do not satisfy the
program's required second-provider price reconciliation.

Massive documents extended-hours custom aggregates and omits intervals without
qualifying trades; Perch preserves that absence. Its ticker-overview endpoint
supports a requested date, but Perch does not equate that date with public-
availability time for every filing-derived field. Nasdaq Trader documents that
its halt feed covers Nasdaq and other exchange-listed securities and refreshes
once per minute; the adapter respects that cadence.

Source contracts:

- [Massive custom stock bars](https://massive.com/docs/rest/stocks/aggregates/custom-bars)
- [Massive ticker overview](https://www.massive.com/docs/rest/stocks/tickers/ticker-overview)
- [Nasdaq Trader halt RSS specification](https://nasdaqtrader.com/Trader.aspx?id=TradeHaltRSS)
- [Nasdaq Trader halt fields and codes](https://nasdaqtrader.com/Trader.aspx?id=TradeHaltCodes)

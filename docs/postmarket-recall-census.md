# Postmarket full-universe recall census

The live market-wide observer combines bounded Alpaca top-mover/most-active
lists with a deterministic full-universe sweep. The bounded lists provide fast
attention to conspicuous names; the sweep partitions the active universe into
five disjoint shards and covers one shard per minute, so a complete cycle
matches the five-minute completed-bar cadence. The recall census independently
snapshots the active universe after the postmarket window and evaluates every
symbol from completed five-minute SIP bars.

## Bounded full-market execution

The census runs only after 20:05 ET. It requests the final RTH bar plus the
postmarket window, not a full trading day, and processes sorted 500-symbol
chunks so the 2 GB production host never materializes the whole market's bar
history at once. One finalized session is attempted per idle cycle. A degraded
provider attempt may retry up to three times; every attempt is append-only.

The universe snapshot is stored with its symbol count, SHA-256 digest, request
count, feed/provider/timeframe/revision, and evaluator thresholds. Symbols
absent from a successful bulk response are `NO_DATA_RETURNED`; a failed chunk is
`FETCH_ERROR`. Neither is silently classified as a non-opportunity.

## Recall labels

For every symbol with data, the census evaluates each knowable completed-bar
instant through the same pure postmarket evaluator used live. This preserves a
reaction that qualified and later reversed. Qualifications are compared as
symbol/direction pairs with the append-only Stage-1 candidate ledger.

False-negative reasons distinguish:

- `NOT_OBSERVED_BY_LIVE_DISCOVERY`: the symbol never appeared in any bounded or
  sweep observation; and
- `RETURNED_NOT_CONFIRMED`: Stage 1 saw the symbol, but the live evaluator never
  recorded the qualifying direction.

The ledger records eligible pairs, true positives, false negatives, false
positives, recall, first eligible timestamps, live detection delays, unavailable
symbols, and conservation invariants. Missing data does not create a false
positive or false negative.

## Immutable primary and second-provider reports

Each attempt publishes:

```text
data/postmarket_audits/postmarket_recall_census_<session>_v<attempt>.json
```

The existing encrypted backup includes these reports and the census tables in
`postmarket_shadow.db`. Heartbeat fields expose the current attempt and latest
verdict.

The same-night census is independent of the bounded screen but uses the same
Alpaca SIP bar provider as the live evaluator. Its immutable report therefore
continues to carry `PROVIDER_COMPARISON_NOT_CONFIGURED`; later evidence never
rewrites that historical fact.

When dedicated `MASSIVE_S3_ACCESS_KEY_ID` and
`MASSIVE_S3_SECRET_ACCESS_KEY` credentials are configured, a separate next-day
proof downloads one finalized `us_stocks_sip/minute_aggs_v1` object after a
two-hour publication safety lag. It streams the gzip CSV, keeps only the frozen
census universe and final-RTH/postmarket window, and resamples observed
one-minute aggregates into exact five-minute buckets without filling empty
intervals. It then replays both providers over the same universe and thresholds.

The append-only proof stores object key, ETag, last-modified time, byte count,
selected-row SHA-256, source row counts, provider/feed/dataset identity,
comparable-symbol coverage, primary and independent eligible pairs, agreement,
independent Stage-1 recall, exact timestamp overlap, exact-bar close divergences,
per-symbol differences, revision, run, attempt, errors, and invariants. Reports are
written as:

```text
data/postmarket_audits/postmarket_recall_provider_<session>_v<attempt>.json
```

Technical evidence floors are locked before live results: at least 99%
comparable coverage, 95% eligible-pair agreement, 95% independent recall, and
at least 95% exact timestamp overlap, with no exact-bar close divergence above
50 basis points. Undefined recall or timestamp overlap is explicitly ineligible.
A late, missing, malformed, unauthorized, misdated, or wrongly keyed object
creates a durable degraded attempt; it never becomes an empty market. The
flat-file adapter uses Massive's documented full-market SIP minute aggregates,
available approximately the following day, rather than thousands of per-symbol
REST calls.

The census is shadow-only. It cannot change candidates, thresholds, ranks,
alerts, or orders.

Source contract: [Massive stock minute aggregate flat files](https://massive.com/docs/flat-files/stocks/minute-aggregates)
and [Massive flat-file S3 setup](https://massive.com/docs/flat-files/quickstart).

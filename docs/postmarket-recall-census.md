# Postmarket full-universe recall census

The live market-wide observer begins with bounded Alpaca top-mover and
most-active lists. Those lists are useful discovery inputs, not proof that
Perch watched every active symbol. The recall census independently snapshots
the active universe after the postmarket window and evaluates every symbol from
completed five-minute SIP bars.

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

- `NOT_RETURNED_BY_BOUNDED_SCREEN`: the symbol never appeared in any Stage-1
  observation; and
- `RETURNED_NOT_CONFIRMED`: Stage 1 saw the symbol, but the live evaluator never
  recorded the qualifying direction.

The ledger records eligible pairs, true positives, false negatives, false
positives, recall, first eligible timestamps, live detection delays, unavailable
symbols, and conservation invariants. Missing data does not create a false
positive or false negative.

## Immutable reports and current limitation

Each attempt publishes:

```text
data/postmarket_audits/postmarket_recall_census_<session>_v<attempt>.json
```

The existing encrypted backup includes these reports and the census tables in
`postmarket_shadow.db`. Heartbeat fields expose the current attempt and latest
verdict.

The census is independent of the bounded screen but currently uses the same
Alpaca SIP bar provider as the live evaluator. Reports therefore carry
`PROVIDER_COMPARISON_NOT_CONFIGURED` and remain ineligible as final empirical
evidence until a genuinely independent comparison source is configured. This
limitation is explicit; same-provider agreement is not mislabeled as external
confirmation.

The census is shadow-only. It cannot change candidates, thresholds, ranks,
alerts, or orders.

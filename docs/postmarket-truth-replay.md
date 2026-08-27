# Postmarket truth set and offline replay

The versioned postmarket truth set locks observer behavior to reviewable
inputs and expected outcomes. The replay imports the same pure evaluator as
the shadow service, but it has no network, journal, alert, Telegram, or broker
dependency. It cannot send or trade.

## Evidence cohorts

- `tuning`: production-shaped CRM, CRWD, and OKTA examples that influenced the
  v1 observer. They are regression fixtures, not independent evidence.
- `contract_holdout`: synthetic cases fixed after v1 thresholds shipped. They
  test deterministic behavior for persistent moves, fades, quiet reports,
  liquidity, latency, early closes, malformed/stale/missing bars, duplicate and
  out-of-order data, and provider failure.
- `empirical_holdout`: independently labeled real sessions not used to tune the
  observer. This cohort is intentionally empty today. The CLI fails closed if
  it is requested before real cases exist.

Perfect contract-holdout precision or recall means the code satisfies its
declared synthetic contract. It is not production precision, profitability,
or the 95% empirical-recall evidence required to activate customer alerts.

## Run it

```bash
python -m tradebot.postmarket_replay --cohort contract_holdout
python -m tradebot.postmarket_replay --cohort tuning --compact
```

Exit status is `0` when all selected cases match, `1` when the truth contract
has failures, and `2` when the truth file or configuration is invalid. Reports
include both the locked truth thresholds and current evaluator thresholds, so
threshold drift is visible and changes behavior rather than silently rewriting
history.

## Current evidence boundary

The 2026-08-26 live shadow session produced 27 invariant-clean ticks, zero
errors, and an average tick latency of 1.047 seconds. It recovered the known
CRM, CRWD, and OKTA reactions plus HPQ and LTRX. This is one clean live session
of the required ten. It proves the observer ran and preserved its funnel; it
does not yet establish independent recall, precision, or alert readiness.

Customer postmarket alerts remain disabled. Adding empirical cases must retain
raw provenance, use labels fixed independently of observer output, keep symbols
disjoint from tuning, and receive review as a versioned truth-set change.

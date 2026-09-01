# Final-RTH momentum truth replay

This offline replay protects the final-30-minute RTH handoff contract from
silent threshold, timestamp, freshness, persistence, and data-quality
regressions. It imports no live vendor, persistence, delivery, broker, or order
path.

Run the scored synthetic contract holdout with:

```bash
python -m tradebot.rth_momentum_replay --compact
```

The versioned truth file is `truth/rth_momentum_v1.json`. It contains three
strictly separated cohorts:

- `tuning` contains named incidents used to shape or repair the design. GPRO is
  here and nowhere else. Its bars are production-shaped synthetic evidence
  derived from the user-supplied screenshot; they are not proof of what the
  production database observed.
- `contract_holdout` contains symbol-disjoint synthetic positives and
  negatives. It covers upside, downside, the exact 8% boundary, an XNYS early
  close, quiet and illiquid moves, one-bar motion, gaps, staleness, zero volume,
  missing baselines/bars, malformed OHLC, duplicate timestamps, naive
  timestamps, and out-of-order provider evidence.
- `empirical_holdout` is intentionally empty. It may be populated only with a
  locked, independently labeled live manifest. Running that cohort currently
  fails closed.

The report records the truth threshold snapshot and the current implementation
thresholds separately. Any threshold or momentum-version drift makes the
contract incompatible and the command exits nonzero, even if the particular
fixtures happen to retain the same outcomes. The baseline is the former system
with no final-RTH handoff lane, whose recall for positive handoff cases is zero
by construction.

A perfect contract replay proves only deterministic behavior for these cases.
It does not establish live recall, precision, latency, profitability, full-
market coverage, independent-provider agreement, or customer readiness. Those
claims require clean live sessions, the daily missed-mover census, independent
labels, locked empirical holdout evidence, and owner review.

# Daily full-market RTH missed-mover census

The final-RTH observer combines bounded fast admission with a deterministic
full-universe sweep. Neither lane may certify its own recall. This census runs
after the full postmarket evidence window and measures the remaining blind spot
over the canonical active universe.

It is an audit, not a signal generator. It cannot create candidates, change
thresholds, send notifications, or reach a broker/order path.

## Truth scope

The census fetches finalized Alpaca SIP daily bars in sorted 500-symbol chunks.
For each symbol it requires:

- exactly one valid daily bar for the XNYS session;
- one valid earlier daily close;
- canonical symbol identity and positive, internally consistent OHLCV; and
- at least $1,000,000 of close-times-volume daily notional.

A `MAJOR_CLOSE_MOVER` is at least 8% up or down at the finalized close versus
the earlier close. Daily highs and lows that crossed 8% but did not retain the
move are recorded separately as `EXCURSION_ONLY` review cases. They do not count
as fast-lane false negatives because daily OHLCV cannot establish exact
intraday timing, persistence, or tradability at the excursion.

This scope is deliberately narrower than “all intraday opportunities.” Full
five-minute replay and an independently sourced holdout remain required for
signal-quality claims.

## Miss attribution

Every major-close symbol/direction is compared with the append-only final-RTH
candidate ledger. A miss receives one deterministic reason:

- `RTH_LANE_NOT_RUNNING`: the session has no final-RTH ticks;
- `NOT_SELECTED_BY_BOUNDED_RTH_LANE`: a historical bounded-only lane ran but
  never evaluated the symbol;
- `NOT_OBSERVED_BY_FULL_UNIVERSE_RTH_SWEEP`: the deterministic sweep was active
  but no observation exists, which is a coverage/invariant failure; or
- `SELECTED_NOT_QUALIFIED:<outcomes>`: the symbol was evaluated and the exact
  live rejection outcomes are retained.

The census reports major-close pairs, caught pairs, missed pairs, close recall,
excursion-only symbols, data-unavailable symbols, fast-lane coverage counts,
provider/feed/revision, universe digest, request chunks, and errors. Provider
chunk failure and successful omission are different data states.

## Evidence and limitations

Runs and per-symbol events are append-only in `postmarket_shadow.db`. Each
attempt writes an immutable report:

```text
data/postmarket_audits/rth_missed_mover_census_<session>_v<attempt>.json
```

The report is operationally complete only when the full universe is conserved
without fetch/evaluation failures. It remains ineligible as independent quality
evidence while live and census bars both come from Alpaca; the report records
`PROVIDER_COMPARISON_NOT_CONFIGURED` rather than hiding that dependence.

This census tells Perch what it failed to see. Replay/holdout review decides
whether a miss was a desirable opportunity and whether any threshold should
change.

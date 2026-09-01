# Postmarket deterministic evidence rank

The postmarket rank orders current shadow candidates by stored evidence. It is
not confidence, probability, expected return, profitability, advice, or a
delivery decision. The exact semantic label stored with every run is
`heuristic_evidence_ordering_not_probability`.

## Version 2 decomposition

Version 2 retains version 1's maximum raw component score of 100; no component
weight or outcome threshold changed:

| Component | Maximum | Evidence |
|---|---:|---|
| Volatility-normalized move | 20 | Candidate move in prior-session ATR units; capped at 5 ATR |
| SPY-relative directional excess | 15 | Excess percentage points in the candidate direction |
| RTH dollar liquidity | 15 | Log-scaled from $1M through $1B |
| Postmarket notional | 10 | Log-scaled from $100K through $100M |
| SIP quote spread | 15 | Linear from 15 at 0 bps to 0 at 300 bps |
| SIP quoted depth | 5 | Linear through $1M displayed bid/ask notional |
| Verified catalyst | 10 | Verified ledger fact; unexplained receives zero |
| Lifecycle | 10 | Strengthening 10, confirmed 8, requalified 7, fading 2 |

Named penalties are separately stored for degraded context, wide spreads, thin
RTH liquidity, unexplained catalysts, fading, requalification after failure,
missing ATR/benchmark evidence, and stale observations. Final evidence score is
`clamp(raw components + penalties, 0, 100)`. `evidence_coverage_pct` measures
how much of the versioned 100-point evidence surface was available; unavailable
inputs are not treated as zero-valued favorable facts.

## Hard rankability gates

Ordinal rank requires all of the following:

- a completed, non-degraded context row;
- that exact context row is no more than 420 seconds old and is bound to the
  same lifecycle observation sequence being ranked;
- technical data-confidence status `HIGH` or `MEDIUM`;
- lifecycle state `CONFIRMED`, `STRENGTHENING`, or `REQUALIFIED`;
- a completed-bar lifecycle observation no more than 420 seconds old;
- an available, active, tradable asset fact;
- an available SIP quote no more than 420 seconds old, not from the future, and
  a spread no wider than 300 bps.

Every failure is stored in `exclusion_reasons_json`. Unrankable candidates keep
their evidence decomposition for analysis but receive no ordinal rank.

## Append-only reproducibility

`postmarket_rank_runs` stores rank version, semantic as-of time, code/run ID,
input digest, input/rankable counts, component weights, thresholds, and the
canonical SHA-256 rank-contract identity. The contract covers the exact
component formulas, penalties, hard gates, context version, and deterministic
tie-break. The input digest also includes that contract, so a semantic change
cannot reuse an older run even if its market evidence is identical.
`postmarket_candidate_ranks` stores the exact context ID, lifecycle transition
ID, completed-bar observation sequence, named components, named penalties,
exclusions, explanation, score, coverage, and ordinal.

The digest contains each candidate's source IDs, context-to-lifecycle binding,
technical data-confidence status, and independent observation/context/quote
freshness states. Identical evidence inside the same freshness states is
idempotent. A new context, transition, completed bar, or any fresh-to-stale
boundary creates a new immutable snapshot. Ties resolve deterministically by
evidence score, coverage, symbol, then candidate ID.

Legacy rows migrate with a NULL contract and remain historical only. They are
never rewritten or accepted by empirical qualification. A fresh attributable
snapshot is required before a session can enter a locked experiment.

The observer heartbeat separates current rankability from historical session
capability. It exposes the newest run, inputs, rankable and unrankable counts,
top five, named current exclusion counts, session run/rankable-run counts, peak
rankable count, first/latest rankable timestamps, and the latest historically
rankable snapshot. A post-close stale or `CLOSED` snapshot therefore cannot hide
whether ranking worked during the active window, while its historical top is
explicitly labeled rather than presented as current. The heartbeat retains the
non-probability semantic label. This remains shadow-only and cannot send alerts
or place trades.

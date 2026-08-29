# Perch signal-quality program

## Objective

Turn Perch from a reliable reaction detector into a trustworthy market-
intelligence system whose coverage, timeliness, tradability, ranking, and
forward outcomes are independently measurable. More alerts are not success.
Success means fewer unexplained misses and a calibrated separation between
historical observation, current qualification, and forward-looking confidence.

All new capabilities remain shadow-only until the evidence gate passes. No
quality score may be described as confidence, probability, profitability, or
advice unless its calibration supports that exact claim.

## Acceptance contract

The program is complete only when all of the following are true:

1. Every postmarket candidate has append-only, provenance-bound outcome marks
   at +5, +15, +30, and +60 minutes, the postmarket close, next-session open,
   and next-session close. Missing and unavailable observations remain explicit.
2. Maximum favorable/adverse excursion and time-to-MFE are measured from the
   first knowable candidate bar without look-ahead.
3. Every discovery cycle records scheduled lag and screen, bar-fetch,
   evaluation, persistence, and total latency so a tick gap names its slow stage.
4. A separate after-the-fact full-universe census measures Stage-1 false
   negatives instead of assuming bounded vendor rankings equal full coverage.
5. Versioned features cover volatility-adjusted movement, market/sector-relative
   strength, liquidity and spread, catalyst provenance, data confidence,
   persistence shape, and reversal risk.
6. Candidate lifecycle transitions are append-only and distinguish newly
   qualifying, confirmed, strengthening, fading, dequalified, and closed states.
7. The quality rank is deterministic, versioned, decomposable into named
   components, and reproducible from stored evidence.
8. Threshold and rank changes use walk-forward development data and a blinded,
   independently labeled empirical holdout. Tuning examples never count as
   independent evidence.
9. The evidence package satisfies at least the existing floors: ten clean
   sessions, zero dirty sessions in the locked range, at least 95% recall,
   worst-case detection latency no greater than 330 seconds, zero ambiguous
   labels or direction mismatches, and passing control artifacts. Owner-declared
   precision and sample-size floors must be fixed before viewing holdout results.
10. Customer delivery requires a separately tested state-transition router,
    kill switch, rollback, stale/degraded presentation, and explicit owner
    approval. No shadow milestone silently enables alerts.

## Ordered workstreams

### 1. Outcome truth

- Append-only candidate mark events with deterministic idempotency keys.
- Availability state and provider/feed/timeframe/revision provenance.
- Directional returns, MFE, MAE, time-to-MFE, and cohort quality reports.
- Next-session resolution through the actual exchange calendar.

Implementation: `tradebot/postmarket_quality.py` owns provider-free outcome
semantics; `tradebot/postmarket_quality_backfill.py` performs bounded candidate-
only fetch orchestration and immutable report publication. Operational details
are in `docs/postmarket-outcome-quality.md`.

### 2. Timing truth

- Scheduled-versus-actual cycle lag and missed-cycle count.
- Per-stage screen, bar-fetch, evaluation, persistence, and total latency.
- Heartbeat and audit attribution for every excessive gap.

Implementation: `postmarket_discovery_timing` is committed atomically with
each discovery tick, the service runs on an exchange-close-anchored grid, and
discovery audit version 3 reconciles schedule, stage, persistence, and total
timing. See `docs/postmarket-timing-truth.md`.

### 3. Coverage truth

- Independent full-universe postmarket census after the window closes.
- Stage-1 recall, false-negative symbols, detection delay, and reason codes.
- Provider disagreement and unavailable-universe accounting.

Implementation: `tradebot/postmarket_recall_census.py` snapshots and replays
the active universe in bounded chunks, writes append-only per-symbol evidence,
and publishes immutable miss reports. See `docs/postmarket-recall-census.md`.
Independent provider comparison remains explicitly unconfigured.

### 4. Context and tradability

- ATR/expected-move normalization and SPY/sector-relative movement.
- Spread, quoted depth, dollar volume, float, market cap, halt, and bar-quality
  evidence with explicit unavailable states.
- Earnings, guidance, filing, news, regulatory, analyst, and unexplained catalyst
  categories with source timestamps and confidence boundaries.

### 5. Lifecycle and rank

- Append-only state transitions derived from completed bars only.
- An interpretable rank whose components and penalties are stored with the
  candidate. Historical qualification never masquerades as current actionability.

### 6. Empirical qualification

- Blinded labeling, walk-forward evaluation, cohort metrics, and miss review.
- At least the locked technical floors above; larger samples remain preferable.
- Shadow deployment, daily immutable audits, backups, restore drills, and final
  owner review before any customer delivery decision.

## Anti-goals

- Do not tune thresholds against one exciting session.
- Do not optimize alert count, raw move size, or backtest return in isolation.
- Do not fabricate prices for missing bars or silently forward-fill stale data.
- Do not average away poor small-cap, low-liquidity, or provider-failure cohorts.
- Do not introduce a black-box model before the outcome and recall ledgers exist.

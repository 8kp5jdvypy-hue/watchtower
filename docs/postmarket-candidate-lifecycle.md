# Postmarket candidate lifecycle

A candidate is a historical qualification, not a permanent current signal.
After Stage 1 creates a market-wide candidate, Perch now tracks that symbol
directly through the end of the postmarket window even if the provider's
bounded mover/active lists stop returning it.

## Append-only states

`postmarket_candidate_lifecycle` stores state transitions:

- `NEWLY_QUALIFYING`: the first completed-bar qualification; actionability is
  `WATCH` until a later completed bar confirms it;
- `CONFIRMED`: qualification persisted on a later completed bar;
- `STRENGTHENING`: absolute move expanded at least 1 percentage point beyond
  the recorded peak;
- `FADING`: the move remained technically qualified but retraced at least 2
  percentage points from the peak;
- `DEQUALIFIED`: completed bars no longer satisfy the move, persistence,
  notional, stability, or original-direction condition;
- `REQUALIFIED`: qualification returned after dequalification; and
- `CLOSED`: the exchange-calendar postmarket window and final-bar grace ended.

Every transition stores previous state, actionability, semantic transition
time, physical recording time, evidence-bar timestamp, evaluator outcome and
reason, movement/notional, feed/provider/timeframe, revision, and run ID.
Tables reject updates and deletes.

Fetch errors, stale data, malformed bars, or missing baselines never create a
`DEQUALIFIED` transition. They remain operational errors while the last proven
state stays unchanged.

## Freshness without noisy state churn

`postmarket_candidate_lifecycle_observations` stores every distinct completed
candidate bar evaluated after admission, even if it causes no state change.
This keeps freshness and current movement auditable without inventing repeated
`CONFIRMED` transitions on every one-minute poll. Re-evaluating the same
completed bar is idempotent.

The heartbeat exposes the number of tracked/fetched candidates, new completed-
bar observations, transitions by state, errors, latency, and latest-session
state counts. Latest transition plus latest completed-bar observation—not the
original candidate row—defines current shadow actionability.

## Safety boundary

Lifecycle runs only in the market-wide shadow observer. It does not change the
original candidate, rank, threshold, delivery, or trading path. `CLOSED` is
terminal for a candidate/version. A future customer router must consume tested
state transitions and freshness rules rather than treating an old candidate as
actionable.

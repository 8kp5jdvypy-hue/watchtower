# Final-RTH momentum to postmarket handoff shadow

This subsystem closes one specific visibility gap: a symbol that becomes an
exceptional mover during the final 30 minutes of the regular session must not
disappear merely because the regular-session and postmarket observers have
different clocks and candidate stores.

It is evidence-only. It has no alert, customer, Telegram, broker, or order
dependency. Its thresholds are an initial shadow contract, not a calibrated
claim of signal quality or profitability.

## Coverage contract

For every real XNYS session, including early closes, the discovery supervisor
wakes at `close - 30 minutes` and schedules one tick per minute through the
exchange close, inclusive. That is 31 expected scheduled identities. Each tick
unions:

- the bounded Alpaca SIP mover/activity screen;
- symbols scheduled to report earnings after that session's close; and
- one deterministic shard of the complete active universe.

Five shards cover the full active universe at the five-minute bar cadence while
the provider screen remains the low-latency fast lane. Sweep-only symbols use a
bounded final-window fetch; provider leaders and scheduled reporters retain the
full-session fetch. The Stage-1 archive and after-the-fact close census remain
independent coverage truth surfaces rather than assuming the live sweep worked.
If the bounded screen fails validation, that error is stored and makes the tick
operationally dirty, but the attributable universe shard still evaluates. A
screen endpoint outage therefore cannot silently erase the independent live
coverage lane.

The shared market-wide screen validator requires the exact endpoint set,
provider/feed agreement, bounded ranks, finite metrics, canonical symbol
identity, source timestamps, and freshness. Empty or malformed responses fail
loudly before candidate persistence.

## Qualification contract

A symbol qualifies only when all of the following are true:

- the baseline is an earlier valid daily close, never the current session;
- two contiguous completed, nonzero-volume five-minute RTH bars persist in the
  same direction;
- the latest completed bar is at least 8% from that prior close;
- cumulative RTH notional is at least $1,000,000;
- bars are valid, ordered, unique, contiguous, and no more than 420 seconds
  old; and
- provider/feed/timeframe provenance is Alpaca/SIP/5Min.

Every selected symbol receives exactly one observation per tick. Missing
intraday bars, missing daily baselines, stale data, gaps, invalid values, and
evaluation errors are distinct outcomes. Successful provider omission is not
mislabelled as a transport failure.

## Append-only handoff lifecycle

The subsystem writes four append-only tables in `postmarket_shadow.db`:

- `rth_momentum_ticks` for schedule, conservation, provenance, and per-stage
  timing;
- `rth_momentum_observations` for every selected-symbol decision;
- `rth_momentum_candidates` for deduplicated qualifications; and
- `rth_postmarket_handoffs` for immutable lifecycle transitions.

Every new RTH candidate first receives `RTH_QUALIFIED`. A same-symbol,
same-direction postmarket candidate adds `POSTMARKET_QUALIFIED`. If the full
postmarket evidence window ends without that qualification, reconciliation adds
`POSTMARKET_NOT_QUALIFIED`. Transitions are added; history is never rewritten.

## Daily evidence gate

Five minutes after the exchange close, the read-only audit can publish
`rth_momentum_audit_<session>_v2.json`. A session is evidence-eligible only when
all 31 scheduled ticks are present and:

- selection equals evaluation on every tick;
- every sweep tick has the expected universe digest, shard identity, attributed
  observations, unique universe positions, and complete five-tick cycles;
- no invariant, evaluation error, or missed cycle exists;
- schedule lag and total processing latency stay within the declared bounds;
- code/feed/provider provenance is internally consistent;
- every candidate has its initial handoff identity; and
- no orphan, identity mismatch, invalid link, or conflicting terminal state
  exists.

The report is created atomically, made read-only, and never overwritten. Audit
exceptions are explicit discovery-health failures. A clean operational report
only proves this contract ran correctly; it does not satisfy empirical recall,
precision, independent-provider, holdout, or customer-readiness gates.

## Replay boundary

`docs/rth-momentum-replay.md` defines the versioned offline regression and
contract-holdout suite for this lane. The GPRO incident is tuning-only and
cannot score the thresholds it motivated. Synthetic holdout success remains
separate from the intentionally empty, independently labeled empirical
holdout. No replay result authorizes customer delivery.

# Postmarket rank empirical qualification

Perch's evidence score is a deterministic ordering heuristic, not a confidence
or profit forecast. `tradebot/postmarket_empirical.py` tests whether a fixed
rank version and a fixed selection rule improve signal selection on independent
labels without allowing the holdout to become another tuning set.

## Leakage controls

Every experiment is append-only and locks these facts before the first holdout
session closes:

- development sessions and strictly later, disjoint holdout sessions;
- rank version, minimum evidence score, and optional maximum ordinal rank;
- label method, owner precision floor, 95% or higher recall floor, and minimum
  definitive/positive sample counts.

Labels are appended by an API that does not read candidate or rank tables. Each
label records its independent artifact digest and acquisition time, reviewer,
method, reason, rationale, eligibility instant, and direction. Revisions are
append-only. Holdout labels become permanently frozen when the owner records a
one-way unblinding event; holdout evaluation is impossible before that event.

## Evaluation semantics

The baseline selects every first-discovered session/symbol. The candidate rule
uses the first rankable snapshot for the locked rank version, so a later score
cannot look backward and improve an earlier decision. It applies the prelocked
score and ordinal gates, then reports per-session and aggregate confusion
metrics, precision, recall, direction mismatches, duplicate candidate rows, and
precision/recall deltas from baseline.

Missing samples, ambiguous labels, direction mismatches, or failure to meet the
locked precision/recall floors are named blockers. Reports and source tables are
append-only and idempotent for the exact evidence digest.

Passing this rank experiment does not satisfy the production evidence gate by
itself. Ten clean sessions, full-universe recall census, independent-provider
agreement, latency floors, controls, and explicit owner review remain required.

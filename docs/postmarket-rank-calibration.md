# Postmarket rank calibration

The live `evidence_score` remains a deterministic, decomposable ordering
heuristic. It is not a probability. `tradebot.postmarket_calibration` is the
only path that may produce a calibrated observed-quality estimate, and it
fails closed unless the complete leakage-resistant sequence below succeeds.

## Evidence boundary

1. Lock the rank experiment after development sessions and before holdout.
2. Import independent, rank-blind development labels.
3. Before the first holdout session opens, fit one monotonic isotonic-PAV
   mapping. The mapping binds the exact latest label revisions, first rankable
   score rows, policy, code revision, and model with SHA-256.
4. Once fitted, development labels are frozen by a database trigger. A second
   model for the same experiment is rejected.
5. Import independent holdout labels while rank output remains sealed.
6. Explicitly freeze the holdout inventory and unblind once.
7. Evaluate the already-frozen mapping. No refit or threshold selection is
   permitted after unblinding.

The estimate means only: observed probability that the independently labeled
postmarket reaction was eligible in the same direction, conditional on the
candidate having a first rankable score. It is not expected return,
profitability, advice, or a customer-delivery authorization.

## Fail-closed qualification

A holdout calibration claim is invalid when any locked sample floor is missed,
labels are ambiguous, rank evidence is missing, any populated reliability bin
lacks its minimum support, the Brier-score ceiling is exceeded, or expected
calibration error exceeds its ceiling. Development evaluation always includes
`HOLDOUT_VALIDATION_REQUIRED`, even if in-sample fit is perfect.

Each reliability bin reports its frozen score interval, predicted quality,
holdout labels and positives, observed quality, absolute error, and a 95%
Wilson interval. This keeps the model interpretable and makes small-sample
uncertainty visible.

## Operator sequence

Use `scripts/postmarket_empirical.py` for `lock`, `import-labels`,
`fit-calibration`, `inventory`, `unblind`, and `evaluate-calibration`. Every
policy value is explicit on `fit-calibration`. The command must run before the
first holdout open and writes no customer or delivery state.

Calibration artifacts are immutable JSON in `data/postmarket_audits` by
default. Their filenames start with `postmarket_rank_calibration_`, so the
existing postmarket artifact archive and encrypted off-box backup include
them. A passing artifact is evidence for a later readiness gate only; it never
enables alerts.

# Postmarket rank calibration

The live `evidence_score` remains a deterministic, decomposable ordering
heuristic. It is not a probability. `tradebot.postmarket_calibration` is the
only path that may produce a calibrated observed-quality estimate, and it
fails closed unless the complete leakage-resistant sequence below succeeds.

## Evidence boundary

1. Lock the rank experiment after development sessions and before holdout.
2. Import independent, rank-blind development labels.
3. Before the first holdout session opens, fit one monotonic isotonic-PAV
   mapping. The mapping binds the exact locked rank contract, latest label
   revisions, first rankable score rows and their run input digests, policy,
   code revision, and model with SHA-256.
4. Once fitted, development labels are frozen by a database trigger. A second
   model for the same experiment is rejected.
5. Import independent holdout labels while rank output remains sealed.
6. Explicitly freeze the holdout inventory and unblind once.
7. Evaluate the already-frozen mapping. No refit or threshold selection is
   permitted after unblinding.

The database rejects holdout-label inserts after unblinding even when a caller
bypasses the Python writer. Fit and evaluation timestamps cannot predate their
latest label/rank evidence or the explicit unblind event. Reusing an identical
input inventory under a different code revision is rejected instead of being
silently reattributed.

The estimate means only: observed probability that the independently labeled
postmarket reaction was eligible in the same direction, conditional on the
candidate having a first rankable score. It is not expected return,
profitability, advice, or a customer-delivery authorization.

## Fail-closed qualification

A holdout calibration claim is invalid when any locked sample floor is missed,
labels are ambiguous, rank evidence is missing, any populated reliability bin
lacks its minimum support, the Brier-score ceiling is exceeded, or expected
calibration error exceeds its ceiling. Any same-version rank run carrying a
different or missing contract is also a named blocker; fitting refuses such a
development set, and projection refuses a rank run that does not match the
frozen model's contract. Development evaluation always includes
`HOLDOUT_VALIDATION_REQUIRED`, even if in-sample fit is perfect.

Each reliability bin reports its frozen score interval, predicted quality,
holdout labels and positives, observed quality, absolute error, and a 95%
Wilson interval. This keeps the model interpretable and makes small-sample
uncertainty visible.

The immutable artifact embeds the prospectively frozen model—not just its
digest. The aggregate readiness gate independently recomputes the model hash,
development-label conservation, monotonic segments, training Brier/ECE,
Wilson intervals, holdout Brier/ECE, and each reliability-bin prediction. A
report therefore cannot substitute post-hoc probabilities chosen after seeing
the holdout outcomes.

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

# Signal-quality program status

`tradebot.postmarket_program_status` is the read-only, fail-closed progress
ledger for Perch's postmarket signal-quality program. It answers the operator's
"what now?" question from stored evidence instead of process health, memory, or
an optimistic checklist.

It is an inventory and reproducibility check, not a new acceptance gate. The
existing campaign, discovery, empirical, calibration, and customer dry-run
contracts remain authoritative. This ledger cannot call a provider, tune a
threshold, create or select evidence, send a message, or enable delivery.
`customer_delivery_enabled` is always `false`.

## Run it

Inside the production checkout:

```bash
docker compose exec -T postmarket-discovery \
  python3 -m tradebot.postmarket_program_status
```

The command uses the deployed `data/postmarket_shadow.db`,
`data/postmarket_audits`, and `data/postmarket_evidence` paths by default. It
prints one JSON report. Exit status `0` means only that the complete evidence
inventory is eligible for a separate owner review. Exit status `1` means the
program is incomplete or the ledger could not validate its evidence.

For an offline restore drill or test fixture, override all paths explicitly:

```bash
python3 -m tradebot.postmarket_program_status \
  --database /restore/postmarket_shadow.db \
  --audit-dir /restore/postmarket_audits \
  --evidence-dir /restore/postmarket_evidence
```

## Milestones

The report reconciles these ordered requirements:

1. SQLite integrity.
2. Ten clean, evidence-eligible discovery sessions.
3. Append-only market-wide outcome-quality reports for those clean sessions.
4. Full-universe recall censuses for those clean sessions.
5. Independent-provider proofs for those clean sessions.
6. A prospectively locked empirical experiment.
7. Non-empty context, lifecycle, and decomposed-rank evidence.
8. Enough independently produced, rank-blind holdout labels for the exact
   locked experiment floor.
9. A passing frozen empirical holdout.
10. A passing frozen calibration holdout.
11. Independent customer-case reviews.
12. A reproducible customer-delivery review gate.

Counts are intersected with the clean-session set where applicable. Evidence
from a dirty or unrelated session does not improve progress.
The experiment milestone validates the complete canonical contract rather than
counting rows: its XNYS development/holdout split, lock timestamp, eligibility
and selection rules, sample and metric floors, current rank version/contract,
and manifest SHA-256 must all reproduce. Labels, empirical holdout reports, and
calibration reports count only when they share that same valid experiment.

The context/lifecycle/rank milestone reports per-feature status distributions,
rankable-rank counts, and qualified-lifecycle counts in addition to its exact
coherent-chain count. This makes a missing required input (for example, a
licensed sector reference) visible without treating populated tables as proof.
Customer reviews are scoped to the exact SHA-256 of a locked campaign and count
distinct blinded cases and symbols against that campaign's locked floors. A
single review, duplicate reviews of one case, or reviews from another campaign
cannot complete the milestone. The final gate still reconstructs and validates
every review payload before customer-delivery review can become eligible.

## Fail-closed rules

- Malformed JSON, symlinks, conflicting same-version reports, unreadable
  SQLite, and unknown evidence identities produce an
  `EVIDENCE_LEDGER_VALIDATION` error.
- A file that merely claims the final verdict does not count. The ledger finds
  every exact SHA-256-bound input and reruns the existing customer dry-run gate;
  the recomputed report must equal the stored report.
- A gate with customer delivery already enabled is rejected.
- Evidence validation and database-integrity errors take priority in
  `next_action`. If context rows exist but none form a coherent feature,
  lifecycle, and rank chain, that dependency is surfaced before session-count
  collection; otherwise the first unresolved milestone supplies the action.

The output is safe to archive with the normal evidence backup, but a status
report is never a substitute for the immutable artifacts it inventories.

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
also expects the exact provider qualification at
`data/postmarket_evidence/provider-qualification/qualification.json` and the
prospectively locked campaign at
`data/postmarket_evidence/discovery-campaign.json`. It prints one JSON report.
Exit status `0` means only that the complete evidence inventory is eligible
for a separate owner review. Exit status `1` means the program is incomplete
or the ledger could not validate its evidence.

For an offline restore drill or test fixture, override all paths explicitly:

```bash
python3 -m tradebot.postmarket_program_status \
  --database /restore/postmarket_shadow.db \
  --audit-dir /restore/postmarket_audits \
  --evidence-dir /restore/postmarket_evidence \
  --provider-qualification /restore/qualification.json \
  --discovery-campaign /restore/discovery-campaign.json
```

## Milestones

The report reconciles these ordered requirements:

1. SQLite integrity.
2. A coherent context, lifecycle, and decomposed-rank development-evidence
   chain.
3. A prospectively locked empirical experiment.
4. An operator-approved qualification manifest for the exact implemented
   independent provider and dataset.
5. A prospectively locked discovery campaign whose exact session inventory is
   the experiment holdout and whose experiment, rank, provider, dataset, and
   empirical floors all match.
6. Ten clean, evidence-eligible discovery sessions from that locked campaign.
7. Append-only market-wide outcome-quality reports for those clean sessions.
8. Full-universe recall censuses for those clean sessions.
9. Independent-provider proofs for those clean sessions, bound to the exact
   qualification manifest, provider, and dataset.
10. Enough independently produced, rank-blind holdout labels for the exact
   locked experiment floor.
11. A passing frozen empirical holdout.
12. A passing frozen calibration holdout.
13. Independent customer-case reviews.
14. A reproducible customer-delivery review gate.

Session-based counts are first restricted to the prospectively locked campaign
inventory and then intersected with the clean-session set where applicable.
Evidence from a dirty, historical, or otherwise unrelated session does not
improve progress. A syntactically valid but unrelated qualification digest also
does not count as an independent-provider proof.
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
  `next_action`. Remaining milestones follow causal order: development evidence,
  experiment lock, provider qualification, and campaign lock are surfaced
  before session-count collection. If context rows exist but none form a
  coherent feature, lifecycle, and rank chain, that dependency is surfaced
  first.

The output is safe to archive with the normal evidence backup, but a status
report is never a substitute for the immutable artifacts it inventories.

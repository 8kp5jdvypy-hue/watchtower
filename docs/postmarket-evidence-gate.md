# Postmarket aggregate evidence gate

The aggregate gate answers one narrow question: has a locked body of shadow
evidence met its declared technical acceptance policy closely enough to be
reviewed by the owner? Its only verdicts are `NOT_READY` and
`ELIGIBLE_FOR_OWNER_REVIEW`. It cannot activate alerts, fetch data, read a live
database, send messages, place orders, or write files.

## Evidence custody

The input manifest conforms to
`truth/postmarket_evidence_set_v1.schema.json`. Runtime validation is stricter
than JSON Schema and requires:

- a complete inventory of every XNYS session in the declared date range;
- one relative, SHA-256-pinned daily report per session;
- exact report/session agreement and internally conserved confusion metrics;
- attributable audit and observer revisions;
- successful catalyst-ledger evidence;
- declared allowed feeds and market-data providers;
- SHA-256-pinned failure-injection, kill-switch, and rollback artifacts;
- a manifest locked only after the coverage window and control work finish.

Paths cannot escape the manifest directory. Any missing file, changed byte,
duplicate session/control, malformed ratio, contradictory clean/eligible flag,
or incomplete provenance is a configuration error rather than a failed metric.
Each control artifact conforms to
`truth/postmarket_control_evidence_v1.schema.json`; its summary status must
agree with every internal check. A present-but-failed control remains valid
evidence of failure and keeps the verdict `NOT_READY`. A control claiming pass
while containing a failed check invalidates the package.
Control revisions must belong to the audit/observer evidence era; a passing
exercise from unrelated old code cannot satisfy the gate.

## Aggregate policy

The manifest cannot weaken these program floors:

- at least ten clean, fully inventoried sessions;
- zero dirty sessions inside the locked range;
- recall of at least 95%;
- worst-case detection latency no greater than 330 seconds;
- zero ambiguous labels and direction mismatches;
- all three control artifacts present.

The owner-reviewed manifest additionally declares minimum precision and minimum
definitive/positive sample sizes. Those values are printed in the report; the
tool does not hide a permissive policy. Feed/provider changes outside the
declared allowed evidence era fail the gate instead of being averaged together.

Precision and recall are recomputed from aggregate confusion counts, not
averaged from the best sessions. Mean latency is weighted by true positives;
the gate uses the worst session latency for its pass/fail decision.

## Workflow

1. The production observer writes its immutable operational daily report.
2. A blinded reviewer locks the independent empirical manifest and generates a
   reviewed daily report with `tradebot.postmarket_audit`.
3. Run `tradebot.postmarket_controls` as documented in
   `docs/postmarket-control-evidence.md`. Its failure-injection, kill-switch,
   and rollback exercises produce reviewable artifacts stamped with their
   tested revisions.
4. The evidence-set manifest lists relative artifact paths and SHA-256 values.
5. Run the gate offline:

```bash
python -m tradebot.postmarket_evidence_gate evidence/postmarket_set_v1.json
```

Exit `0` means eligible for owner review, `1` means the declared metric/control
policy is not met, and `2` means the evidence package itself is invalid.

## Current production position

The immutable 2026-08-26 report is operationally dirty because observation
started late and covered 10.8% of the required window. It has no independent
empirical manifest. Therefore the current aggregate position remains
`NOT_READY`, with 0/10 clean sessions. This tool does not reinterpret or remove
that evidence.

# Postmarket aggregate evidence gate

The aggregate gate answers one narrow question: has a locked body of shadow
evidence met its declared technical acceptance policy closely enough to be
reviewed by the owner? Its only verdicts are `NOT_READY` and
`ELIGIBLE_FOR_OWNER_REVIEW`. It cannot activate alerts, fetch data, read a live
database, send messages, place orders, or write files.

## Evidence custody

Evidence custody has two immutable layers. Before the first covered session
opens, an operator creates a prospective campaign conforming to
`truth/postmarket_evidence_campaign_v1.schema.json`. It fixes the exact XNYS
date range, metric floors, permitted feeds/providers, and permitted audit and
observer schema/code revisions. After the final covered session, the aggregate
manifest conforms to `truth/postmarket_evidence_set_v2.schema.json` and
SHA-256-pins that campaign. Runtime validation is stricter than JSON Schema and
requires:

- a complete inventory of every XNYS session in the declared date range;
- one relative, SHA-256-pinned daily report per session;
- exact report/session agreement and internally conserved confusion metrics;
- attributable audit and observer revisions;
- successful catalyst-ledger evidence;
- declared allowed feeds and market-data providers;
- only the audit/observer schema and code revisions declared prospectively;
- SHA-256-pinned failure-injection, kill-switch, and rollback artifacts;
- a campaign locked before the first session opens and a final manifest locked
  only after the coverage window and control work finish.

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

The campaign file is fsynced under a temporary same-directory name, atomically
linked without replacement, directory-fsynced, and left at mode `0444`. The
tool refuses to replace an existing path or expose a torn final file. File permissions are not an
independent timestamp authority, so the campaign must also enter the nightly
encrypted off-box backup before coverage starts. A campaign created late,
covering fewer sessions than its own clean-session floor, changed after
digesting, or inconsistent with the final policy is invalid—not merely a failed
metric. If a material observer/audit revision changes during coverage, start a
new prospective campaign rather than selecting a more favorable report later.

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

1. Before the first covered XNYS session, lock the campaign. Use the deployed
   audit/observer schema and code revisions, then allow the nightly encrypted
   off-box backup to capture the file:

```bash
docker compose run --rm postmarket-discovery \
  python -m tradebot.postmarket_evidence_campaign \
  data/postmarket_evidence/campaigns/campaign-2026-09-01.json \
  --campaign-id campaign-2026-09-01 \
  --coverage-start 2026-09-01 --coverage-end 2026-09-15 \
  --min-clean-sessions 10 --min-definitive-labels 20 \
  --min-positive-labels 10 --min-recall 0.95 --min-precision 0.90 \
  --max-detection-latency-seconds 330 \
  --allowed-data-feed sip --allowed-market-data-provider alpaca \
  --allowed-audit-version 3 --allowed-observer-version 1 \
  --allowed-audit-code-version REVISION \
  --allowed-observer-code-version REVISION

systemctl start perch-backup.service
systemctl show perch-backup.service \
  -p Result -p ExecMainStatus -p ExecMainExitTimestamp
```

Do not start coverage unless the backup reports `Result=success`, exit status
`0`, and its log confirms the new campaign artifact shipped off-box.

2. The production observer writes its immutable operational daily report.
3. A blinded reviewer locks the independent empirical manifest and generates a
   reviewed daily report with `tradebot.postmarket_audit`.
4. Run `tradebot.postmarket_controls` as documented in
   `docs/postmarket-control-evidence.md`. Its failure-injection, kill-switch,
   and rollback exercises produce reviewable artifacts stamped with their
   tested revisions.
5. The v2 evidence-set manifest lists the campaign, session, and control
   artifacts by relative path and SHA-256.
6. Run the gate offline:

```bash
python -m tradebot.postmarket_evidence_gate evidence/postmarket_set_v2.json
```

Exit `0` means eligible for owner review, `1` means the declared metric/control
policy is not met, and `2` means the evidence package itself is invalid.

## Current production position

Historical reports that predate a valid prospective campaign remain useful for
engineering diagnostics but cannot qualify an evidence campaign. Production
therefore remains `NOT_READY` until a prospectively registered range accumulates
the complete clean inventory, independent labels, provider evidence, and
controls. This tool does not reinterpret or remove earlier evidence.

# Market-wide discovery aggregate evidence gate

This gate is the final offline readiness check for the market-wide postmarket
discovery shadow. It consumes immutable evidence; it does not query market
data, open a database, send an alert, place an order, or alter production
state. A pass means only `ELIGIBLE_FOR_OWNER_REVIEW`. It never enables customer
delivery.

## Lock the campaign first

Before the first covered XNYS session opens, create one immutable campaign with
`python -m tradebot.postmarket_discovery_evidence_campaign`. The campaign pins:

- the exact XNYS date range;
- the blinded empirical experiment ID, manifest SHA-256, and rank version;
- minimum clean-session, label, precision, recall, provider-comparison,
  coverage, and latency floors;
- every allowed feed, provider, dataset, audit/discovery version, and code
  revision; and
- fail-closed requirements for complete inventories, zero dirty sessions,
  unavailable symbols, price disagreements, ambiguous labels, direction
  mismatches, and duplicate candidates.

The writer rejects fewer than ten sessions, floors weaker than the durable
acceptance contract, a lock timestamp at or after the first session open, a
symlink output, or replacement of an existing campaign. Store its printed
SHA-256 with the campaign record.

## Build the evidence set

After every covered session and its next-day independent proof are complete,
create a locked evidence-set manifest conforming to
`truth/postmarket_discovery_evidence_set_v1.schema.json`. Paths are relative to
the manifest and every artifact is SHA-256 pinned. The inventory must contain:

- exactly one discovery audit for every expected XNYS session;
- exactly one primary full-universe recall census for every session;
- exactly one independent-provider proof for every session;
- one unblinded, passed empirical holdout artifact covering the exact campaign
  sessions; and
- exactly four passing controls: discovery failure injection, discovery kill
  switch, discovery delivery isolation, and rollback runbook.

The primary census is intentionally accepted only in the reconciled state
`operational_complete=true`, `evidence_eligible=false`, with the sole issue
`PROVIDER_COMPARISON_NOT_CONFIGURED`. The separate provider report must then
prove the same census identity and universe using a genuinely different
provider. This prevents a same-provider comparison or a falsely self-declared
eligible primary report from satisfying independence.

## Seal the explicit package

Use `tradebot.postmarket_discovery_evidence_set` to publish the final manifest.
Every artifact is named explicitly as `SESSION=PATH` or `KIND=PATH`; the writer
does not glob, select a latest version, or choose a favorable report. All paths
must remain beneath the output manifest's directory.

```bash
python -m tradebot.postmarket_discovery_evidence_set \
  data/postmarket_evidence/campaign-1/evidence-set.json \
  --evidence-set-version campaign-1-final \
  --campaign data/postmarket_evidence/campaign-1/campaign.json \
  --discovery-audit \
    2026-09-01=data/postmarket_evidence/campaign-1/audits/discovery-2026-09-01.json \
  --recall-census \
    2026-09-01=data/postmarket_evidence/campaign-1/census/recall-2026-09-01.json \
  --provider-proof \
    2026-09-01=data/postmarket_evidence/campaign-1/provider/provider-2026-09-01.json \
  --empirical-artifact data/postmarket_evidence/campaign-1/empirical/holdout.json \
  --control \
    discovery_failure_injection=data/postmarket_evidence/campaign-1/controls/failure.json \
  --control \
    discovery_kill_switch=data/postmarket_evidence/campaign-1/controls/kill-switch.json \
  --control \
    discovery_delivery_isolation=data/postmarket_evidence/campaign-1/controls/isolation.json \
  --control \
    rollback_runbook=data/postmarket_evidence/campaign-1/controls/rollback.json
```

Repeat each session argument for every locked XNYS session. The sealer requires
the exact campaign inventory and all four unique controls, computes digests,
runs the complete aggregate gate from a temporary manifest, and publishes a
read-only final file only if the verdict is `ELIGIBLE_FOR_OWNER_REVIEW`. It
refuses overwrite, symlink, traversal, outside-tree, missing-session, and
not-ready packages. A failure leaves no final or temporary manifest behind.

## Re-evaluate

Run:

```bash
python -m tradebot.postmarket_discovery_evidence_gate path/to/evidence-set.json
```

Exit codes are deliberately distinct:

- `0`: every locked check passed; eligible for explicit owner review only;
- `1`: the package is valid but one or more readiness checks failed;
- `2`: the manifest or a pinned artifact is malformed, inconsistent, missing,
  causally impossible, or digest-invalid.

The standalone evaluator remains useful for independently rechecking the sealed
bytes. Its JSON output includes every observed value, required value, pass/fail check,
aggregate metric, revision/provider inventory, and artifact digest. Preserve
the campaign, evidence set, gate output, and referenced artifacts in the
encrypted off-box backup before owner review.

## Fail-closed boundaries

The evaluator recomputes count identities, recalls, provider coverage,
eligible-pair agreement, bar overlap, empirical confusion-matrix totals, and
per-session empirical aggregates. It also enforces provider object causality,
exact session inventories, exact campaign policy equality, campaign and
experiment digests, and revision allowlists. Missing data is never converted
to a pass.

Passing this gate is necessary but not sufficient for customer delivery. The
delivery router, its own kill switch, degraded-state presentation, rollback,
and explicit owner approval remain separate release conditions.

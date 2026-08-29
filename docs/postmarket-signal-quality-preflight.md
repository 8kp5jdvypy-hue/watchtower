# Signal-quality shadow deployment preflight

`scripts/postmarket_signal_quality_preflight.py` is a read-only, fail-closed
check performed after the final revision is checked out and its control
artifacts are generated, but before images are rebuilt or services restarted.
It never changes `.env`, deploys Compose, calls a provider, enables an alert, or
prints secret values.

The report separates two claims:

- `safe_to_deploy_shadow` proves the checkout, live databases, recent backup,
  off-box configuration, disk headroom, enabled scheduled/discovery shadows,
  and exact-revision controls are sound enough for a shadow-only deployment.
- `evidence_campaign_ready` additionally proves the external-context switch,
  Massive REST and dedicated flat-file credentials, and an operator-reviewed
  licensed reference manifest are present. It is false if any campaign input is
  unavailable even when basic shadow deployment is safe.

Neither verdict authorizes customer alerts.

## Prerequisites

1. Fetch and check out the exact merged `origin/main` revision with a clean
   worktree.
2. Run a fresh backup and verify the systemd job succeeded and shipped off-box.
   The preflight independently verifies the explicit local manifest and all of
   its payload digests, but it cannot prove a remote upload from local bytes
   alone; retain the successful service log as that separate evidence.
3. Generate the three discovery control artifacts at the exact checkout.
4. Generate the cross-cutting controls at the same checkout using the previous
   deployed revision as the rollback target. The preflight consumes its
   `rollback_runbook.json`; the other cross-cutting artifacts remain useful
   evidence but do not substitute for the three discovery controls.
5. Obtain and review the licensed reference manifest. Omitting it leaves
   campaign readiness false without making a shadow deployment unsafe.

## Run

```bash
REVISION=$(git rev-parse HEAD)
ROLLBACK_REVISION=<previous-deployed-git-sha>
CONTROL_ROOT="data/postmarket_evidence/${REVISION:0:7}/predeploy-controls"

python -m tradebot.postmarket_discovery_controls \
  --revision "$REVISION" \
  --output-dir "$CONTROL_ROOT/discovery"

python -m tradebot.postmarket_controls \
  --revision "$REVISION" \
  --rollback-revision "$ROLLBACK_REVISION" \
  --output-dir "$CONTROL_ROOT/cross-cutting"

python3 scripts/postmarket_signal_quality_preflight.py \
  --repo-root /opt/perch \
  --expected-revision "$REVISION" \
  --env-file /opt/perch/.env \
  --backup-env-file /opt/perch/.backup-env \
  --data-dir /opt/perch/data \
  --backup-manifest /opt/perch/backups/manifest_<UTC-stamp>.sha256 \
  --control \
    discovery_failure_injection="$CONTROL_ROOT/discovery/discovery_failure_injection.json" \
  --control \
    discovery_kill_switch="$CONTROL_ROOT/discovery/discovery_kill_switch.json" \
  --control \
    discovery_delivery_isolation="$CONTROL_ROOT/discovery/discovery_delivery_isolation.json" \
  --control \
    rollback_runbook="$CONTROL_ROOT/cross-cutting/rollback_runbook.json" \
  --reference-manifest /secure/provider/reference-<date>.json
```

The backup must be no more than two hours old by default, and the data volume
must have at least 1 GiB free. Both bounds can be made stricter through the CLI;
weakening them for a failing deployment is not evidence.

Exit `0` means the full evidence campaign is ready. Exit `1` means the command
produced a valid report but at least one deployment or campaign check failed.
Exit `2` means invocation or top-level input was structurally invalid. Always
inspect every check; do not treat a safe shadow verdict as campaign readiness.

## What is verified

- `HEAD` equals the requested revision and `origin/main`, with no tracked or
  untracked worktree changes;
- `.env` contains non-placeholder Alpaca credentials and explicitly enabled
  scheduled plus market-wide shadow switches;
- full campaign mode also has the isolated external-context switch, Massive
  REST key, and dedicated Massive S3 key pair;
- `.backup-env` names a non-root remote path and a present passphrase file;
- `PRAGMA quick_check` passes read-only for all five live databases;
- the explicit backup manifest is recent, every digest matches, all five
  signal-quality databases (including the irrebuildable Stage-1 evidence in
  `universe.db`) are present, and the safe
  artifact archive contains both audit and evidence roots;
- free disk meets the fixed minimum;
- exactly four required controls pass at the exact checked-out revision; and
- the licensed manifest has valid temporal causality, schema, provider,
  dataset, license reference, sector benchmark mapping, and row invariants.

Only configuration presence is reported for secrets. Their values never enter
the result, logs, or evidence JSON.

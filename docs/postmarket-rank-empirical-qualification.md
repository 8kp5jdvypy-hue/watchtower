# Postmarket rank empirical qualification

Perch's evidence score is a deterministic ordering heuristic, not a confidence
or profit forecast. `tradebot/postmarket_empirical.py` tests whether a fixed
rank version and a fixed selection rule improve signal selection on independent
labels without allowing the holdout to become another tuning set.

## Leakage controls

Every experiment is append-only and locks these facts before the first holdout
session opens:

- development sessions and strictly later, disjoint holdout sessions;
- the independent eligibility definition: absolute move, cumulative-notional,
  and persistence-bar floors;
- rank version, the exact canonical rank-contract SHA-256, minimum evidence
  score, and optional maximum ordinal rank;
- label method, owner precision floor, 95% or higher recall floor, and minimum
  definitive/positive sample counts.

Labels are appended by an API that does not read candidate or rank tables. Each
label records its independent artifact digest and acquisition time, reviewer,
method, reason, rationale, eligibility instant, and direction. Revisions are
append-only. Holdout labels become permanently frozen when the owner records a
one-way unblinding event; holdout evaluation is impossible before that event.

The operator imports a strict `truth/postmarket_empirical_manifest_v1.schema.json`
document. It must declare that review was blinded, reproduce the experiment's
exact eligibility rule, identify digest-bound evidence artifacts, and include
the observed persistence count for every label. Eligible and ineligible labels
are rejected when their evidence contradicts that rule. Multi-provider review
requires at least two distinct providers. The raw manifest digest binds every
imported label to the reviewed artifact set; imports are atomic and identical
bytes are idempotent.

## Evaluation semantics

The baseline selects every first-discovered session/symbol. The candidate rule
uses the first rankable snapshot for the locked rank version **and contract**,
so a later score cannot look backward and improve an earlier decision. The
input digest includes every first-rankable rank/run ID, source input digest,
score, ordinal, and timestamp—not merely the symbols that passed selection. It
applies the prelocked score and ordinal gates, then reports aggregate confusion
metrics, precision, recall, direction mismatches, duplicate candidate rows, and
precision/recall deltas from baseline.

Missing samples, ambiguous labels, direction mismatches, mixed/unattributed
rank contracts, or failure to meet the locked precision/recall floors are named
blockers. Reports and source tables are append-only and idempotent for the exact
evidence digest. Legacy experiments without a contract digest fail closed and
must be replaced rather than backfilled.

Passing this rank experiment does not satisfy the production evidence gate by
itself. Ten clean sessions, full-universe recall census, independent-provider
agreement, latency floors, controls, and explicit owner review remain required.

## Operator workflow

The reviewer must not inspect Perch candidates, ranks, or observer output while
creating label manifests. Keep manifests in a restricted host directory and
mount it read-only into the one-off tool container.

Lock the experiment after development ends but before the first holdout opens:

```bash
RANK_CONTRACT=$(docker compose run --rm runner python3 -c \
  'from tradebot.postmarket_rank import rank_contract_sha256; print(rank_contract_sha256())')

docker compose run --rm -v /opt/perch/scripts:/app/scripts runner \
  python3 scripts/postmarket_empirical.py lock \
  --experiment-id rank-v2-exp-1 --created-by owner --rank-version 2 \
  --rank-contract-sha256 "$RANK_CONTRACT" \
  --label-method multi_provider_reconciliation \
  --development-session 2026-08-27 --holdout-session 2026-08-31 \
  --eligibility-move-pct 8 --eligibility-min-notional 250000 \
  --eligibility-persistence-bars 2 --minimum-evidence-score 60 \
  --maximum-ordinal-rank 10 --min-precision 0.90 --min-recall 0.95 \
  --min-definitive-labels 100 --min-positive-labels 30
```

Import a completed blinded review:

```bash
docker compose run --rm \
  -v /opt/perch/scripts:/app/scripts \
  -v /opt/perch/review-manifests:/app/review-manifests:ro runner \
  python3 scripts/postmarket_empirical.py import-labels \
  --experiment-id rank-v2-exp-1 \
  /app/review-manifests/postmarket-2026-08-27-v1.json
```

Development evaluation can run at any time. Holdout remains sealed. When all
holdout labels are final, preview and review its exact digest, then pass that
same digest to the irreversible unblind command:

```bash
docker compose run --rm -v /opt/perch/scripts:/app/scripts runner \
  python3 scripts/postmarket_empirical.py inventory \
  --experiment-id rank-v2-exp-1

docker compose run --rm -v /opt/perch/scripts:/app/scripts runner \
  python3 scripts/postmarket_empirical.py unblind \
  --experiment-id rank-v2-exp-1 --unblinded-by owner \
  --reason 'independent holdout review complete' \
  --expected-inventory-sha256 <digest-from-inventory>

docker compose run --rm -v /opt/perch/scripts:/app/scripts runner \
  python3 scripts/postmarket_empirical.py evaluate \
  --experiment-id rank-v2-exp-1 --split holdout
```

Unblinding freezes holdout labels permanently. A stale or mistyped inventory
digest fails without changing state.

Evaluation also exports the exact persisted run to
`data/postmarket_audits/postmarket_rank_empirical_<experiment>_holdout_<digest>_v1.json`
(the experiment component is a stable hash, not unsanitized operator text).
The envelope conforms to
`truth/postmarket_rank_empirical_artifact_v1.schema.json`, binds the evaluated
time and Git revision to the stored input/report digests and the experiment's
locked manifest digest, is published by
atomic no-replace link, and is included in the encrypted off-box artifact
backup. An unknown revision, mismatched stored digest, identity conflict, or
holdout report without an explicit unblind event prevents export. This artifact
is the empirical input to the market-wide aggregate readiness gate; console
output alone is not qualification evidence.

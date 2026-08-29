# Postmarket customer-delivery readiness foundation

This foundation defines when a future customer-alert router may enter a dry
run. It does not enqueue, render, send, trade, write production state, add a
Compose service, or enable a switch. Passing means only
`ELIGIBLE_FOR_DRY_RUN`.

## Intended use and failure costs

The intended customer condition is an independently qualified, exceptional
postmarket move that remains current, liquid enough to inspect, and supported
by completed-bar evidence. The customer action is to investigate it; the
message is market intelligence, not advice or a trade instruction.

A stale, directionally wrong, degraded, duplicated, or falsely authorized
alert has greater expected harm than a missed opportunity. The policy therefore
fails closed on uncertainty and treats the absence of an alert as safer than
silently relaxing evidence, freshness, or authorization requirements.

## Exact readiness boundary

`tradebot.postmarket_delivery_readiness` is a pure decision function. It
requires all of the following:

- an independently enabled dry-run switch and disengaged dedicated readiness
  kill switch (both function arguments default to the safe state);
- `clean` operational status;
- a time-bounded manual owner authorization bound to the exact policy,
  evidence-set digest, evidence-gate digest, and router revision;
- an executing router revision that exactly matches the policy revision;
- a later-bar lifecycle state allowed by the locked policy, with
  `QUALIFIED` actionability;
- a complete rank run, no hard exclusions, the exact locked rank version,
  score/ordinal/coverage floors, an allowed evidence revision, and a fresh
  completed bar;
- an allowed provider and feed; and
- no future-dated transition or evidence.

The deterministic idempotency key binds release, policy, candidate,
transition, and rank run. It is necessary but not sufficient for delivery: a
future router still needs an append-only routing ledger to enforce the key
transactionally.

`tradebot.postmarket_delivery_dry_run.py` supplies that offline ledger. It
stores every distinct suppression state, permits a formerly suppressed item to
become eligible when its conditions genuinely change, and uses a partial
unique index to record at most one eligible dry-run decision for an
idempotency key. Exact duplicate suppression states are also idempotent. The
ledger is append-only and stores the policy and authorization digests, exact
candidate/transition/rank IDs, controls, runtime revision, reason codes, and
presentation. It still has no customer-delivery dependency.

## Default-off supervised dry run

`tradebot.postmarket_delivery_dry_run_shadow.py` is an independently
supervised Compose service with its own heartbeat and health probe. Its
`POSTMARKET_CUSTOMER_DRY_RUN_ENABLED` switch defaults to `0`. Disabled mode
does not load an authorization or query evidence. Enabled mode starts only
with strict regular-file policy and authorization contracts, and it operates
only during the XNYS postmarket window.

Each cycle reads the latest exact rank snapshot for the current session and
joins its persisted lifecycle transition and completed-bar observation IDs. A
separate read of the discovery heartbeat allows `clean` operation only for a
fresh, enabled, error-free `ok` cycle with current lifecycle/context evidence,
a complete rank, and an allowed evidence revision. Anything else is recorded
as a degraded suppression. The service never imports a market-data provider,
Telegram, the outbox, a broker, or order code.

Every active-window cycle is also stored append-only in
`postmarket_delivery_dry_run_ticks` on an exchange-close-anchored one-minute
grid. A tick records scheduled/started/completed timestamps, scheduled lag,
total latency, exact rank run, input/eligible/suppressed/deduplicated counts,
operational status and reasons, provenance digests, runtime revision, and a
conservation invariant. `postmarket_delivery_dry_run_tick_decisions` binds the
tick atomically to its exact route rows. Each link must resolve to the same
session, rank run, policy, authorization, and runtime revision. Exact reruns
are idempotent; conflicting evidence for an existing scheduled slot fails
rather than replacing the first record.

After the complete exchange-calendar session and final-bar processing grace,
`tradebot.postmarket_delivery_dry_run_audit` independently reconstructs the
close-anchored schedule from the append-only database. It does not trust the
service heartbeat. The immutable daily report reconciles every expected slot,
tick/decision conservation, exact route links, orphan routes, eligible
identity uniqueness, policy/authorization/runtime drift, rank availability,
degraded cycles, invariants, scheduled lag, and processing latency. Any gap or
inconsistency makes both `operational_clean` and
`session_evidence_eligible` false. Reports conform to
`truth/postmarket_customer_dry_run_audit_v1.schema.json` and are written
exclusively under `data/postmarket_audits` without replacement.

Before any session may count, an owner-approved dry-run campaign must be
locked with `tradebot.postmarket_customer_dry_run_campaign`. The immutable
contract names every expected XNYS session and binds the exact delivery
policy, owner authorization, release, rank/router/audit versions, four control
artifacts, operational limits, case-count floors, and independent-review
requirements. It must be created before the first covered session opens, and
the authorization must remain valid through the final session audit. The
campaign requires at least ten clean sessions, twenty eligible decisions,
twenty independently reviewed cases across at least ten symbols, at least 90%
review approval, exact schedule coverage, and zero critical or ledger-control
failures. Locking a campaign records requirements only; it does not create an
authorization or enable the default-off supervisor.

The two expected contract paths are
`data/postmarket_customer_delivery_policy.json` and
`data/postmarket_customer_delivery_authorization.json`; either can be changed
only explicitly through its corresponding environment path. Creating these
files or enabling the switch is an owner operation and is not performed by a
passing evidence gate or a routine deployment.

## Immutable control evidence

`tradebot.postmarket_delivery_dry_run_controls` runs four offline exercises:

- failure injection for missing authorization, stale bars, degraded discovery,
  revision mismatch, and transactional eligible-decision deduplication;
- the independent default-off switch, disabled health behavior, and safe
  default policy arguments;
- static delivery/provider/order isolation across every readiness module and
  the Compose service; and
- the exact rollback runbook below.

The suite uses deterministic fixtures and in-memory SQLite. It refuses a dirty
worktree, a revision other than checked-out `HEAD`, a failed exercise, or an
existing output directory. All four artifacts are written as one atomic,
read-only set conforming to
`truth/postmarket_customer_dry_run_control_evidence_v1.schema.json`.

Run it only from the exact clean revision being reviewed:

```bash
REVISION=$(git rev-parse --short HEAD)
docker compose run --rm postmarket-customer-dry-run \
  python -m tradebot.postmarket_delivery_dry_run_controls \
  --revision "$REVISION" \
  --output-dir "data/postmarket_evidence/$REVISION/customer-dry-run-controls"
```

### Rollback runbook

Customer delivery remains disabled throughout this dry run. To contain the
readiness observer itself:

1. Using the operator-approved secret editor, set exactly one line in
   `/opt/perch/.env` to `POSTMARKET_CUSTOMER_DRY_RUN_ENABLED=0`, then verify the
   file contains exactly that one key and value. Do not print other secrets.
2. Recreate only the isolated service with an explicit safe override:

   ```bash
   cd /opt/perch
   POSTMARKET_CUSTOMER_DRY_RUN_ENABLED=0 GIT_SHA=$(git rev-parse --short HEAD) \
     docker compose up -d --no-deps --force-recreate postmarket-customer-dry-run
   ```

3. Verify the container environment reports the switch as `0`, the service is
   healthy, and `data/postmarket_delivery_dry_run_heartbeat.json` reports
   `enabled=false` and `status=disabled`.
4. Verify `sqlite3 -readonly data/postmarket_shadow.db "PRAGMA quick_check;"`
   returns `ok` and archive the incident evidence. Never delete or rewrite
   `postmarket_delivery_dry_runs`; its suppressed and eligible dry-run rows are
   append-only evidence.

Rollback never restarts the runner, discovery, worker, bot, or API and never
changes a customer-delivery switch because no customer-delivery path exists.

Presentation is explicit: `ACTIONABLE`, `STALE`, `DEGRADED`, or `CLOSED`.
Suppressed decisions retain every reason code. The evidence score remains a
heuristic ordering score; it is never presented as confidence, probability,
profitability, or advice.

## Manual authorization is not automatic approval

The JSON contracts in `truth/postmarket_customer_delivery_policy_v1.schema.json`
and `truth/postmarket_customer_delivery_authorization_v1.schema.json` document
the exact owner record a future release process must validate. Passing the
aggregate evidence gate cannot create this record and cannot flip a runtime
switch. The acknowledgement explicitly says the record approves readiness
review only and does not enable or send alerts. The field is deliberately named
`dry_run_readiness_approved`; a future customer-delivery activation must be a
different, separately controlled owner action.

## Still required before customer delivery

Customer delivery remains intentionally unimplemented. The supervised router
is evidence-only: it has no users/outbox/provider/alert/order/network path and
can produce only `ELIGIBLE_FOR_DRY_RUN` or `SUPPRESSED` ledger rows. Its
default-off switch, failure injection, rollback, isolation controls, cycle
timing, and daily audits must first accumulate the prospectively required
clean sessions. A later customer-delivery implementation still requires a
separate design, review, policy, owner activation, and release gate; no dry-run
result can enable it.

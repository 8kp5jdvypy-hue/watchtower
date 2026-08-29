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

This foundation intentionally leaves delivery unimplemented. A later,
separately reviewed change must provide an isolated router service, immutable
decision/delivery ledgers, transactional deduplication, customer eligibility
and quiet-hours behavior, stale/degraded rendering, a dedicated kill-switch
control, failure injection, rollback evidence, and explicit owner activation.
Those controls must be proven against the evidence-qualified revision before
any customer-alert readiness claim.

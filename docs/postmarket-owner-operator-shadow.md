# Owner-only postmarket opportunity shadow

The postmarket discovery service deliberately cannot deliver anything. That
is the correct boundary for market-wide evidence collection, but it also meant
an internally detected move could remain invisible to the operator until a
manual database query. The `postmarket-operator` service closes that visibility
gap without authorizing customer alerts.

It is a separate, default-off bridge. It reads qualified discovery candidates
from `data/postmarket_shadow.db` in SQLite read-only mode and can append an
outbox row for exactly one explicitly configured administrator chat. The
existing worker remains the only process that contacts Telegram.

## Admission contract

The bridge does not create or re-evaluate candidates. An owner notification is
eligible only when the upstream append-only discovery ledger already contains
a candidate that passed completed five-minute bars, two-bar persistence, the
8% move threshold, $100,000 postmarket notional, ordering/contiguity/volume/
freshness guards, and the RTH-close baseline contract.

The bridge additionally requires:

- a finite positive RTH close, latest price, volume, and notional;
- non-future detection and evidence timestamps;
- nonempty provider, feed, and timeframe provenance;
- a candidate age no greater than 15 minutes; and
- an exact `users.chat_id` row with `is_admin=1`.

The message identifies itself as owner-only shadow intelligence, includes the
persisted provider/feed/timeframe and available lifecycle/rank/catalyst
context, labels material risk flags, says it is not advice, and states that no
order was placed. It is intelligence visibility, not a recommendation or an
execution path.

Each candidate uses the deterministic identity
`postmarket-operator:v1:candidate:<candidate_id>`. The existing unique
`(alert_id, chat_id)` outbox constraint makes restarts and retries idempotent.
The bridge enqueues at most five new candidates per 15-second cycle; previously
delivered candidates do not consume that limit.

## Isolation and switches

Two explicit environment values are required to arm the bridge:

```dotenv
POSTMARKET_OPERATOR_ALERTS_ENABLED=1
POSTMARKET_OPERATOR_CHAT_ID=<the administrator chat_id already in users.db>
```

Compose defaults `POSTMARKET_OPERATOR_ALERTS_ENABLED` to `0` and the chat ID to
blank. Disabled mode opens neither database and is healthy without a heartbeat
history. Invalid switch values, a blank/zero/noninteger chat ID, or a chat that
does not resolve to exactly one administrator fail closed.

The service has no customer/subscriber/watchlist fan-out, broker, or order
dependency. Its only service dependency is the outbox worker. Discovery keeps
its original no-delivery import boundary.

## Exact-revision control evidence

Before enabling a new revision, run the offline control suite from a clean
checkout at exact `HEAD`:

```bash
REVISION=$(git rev-parse --short=7 HEAD)
OUTPUT_DIR="data/postmarket_evidence/${REVISION}/operator-controls"

python -m tradebot.postmarket_operator_controls \
  --revision "$REVISION" \
  --output-dir "$OUTPUT_DIR"
```

It creates three immutable, SHA-256-addressable artifacts conforming to
`truth/postmarket_operator_control_evidence_v1.schema.json`:

- `operator_failure_injection`: rejects a non-admin destination, ignores stale
  and future evidence, enqueues one fresh candidate, proves retry idempotency,
  verifies message provenance/disclosures, and checks both temporary databases;
- `operator_kill_switch`: proves explicit parsing, default-off Compose wiring,
  disabled health without databases, and that the disabled branch executes
  before database connection;
- `operator_owner_isolation`: proves the exact admin predicate, single explicit
  recipient, read-only shadow connection, worker-only dependency, and absence
  of broker/order imports.

The suite is offline and refuses dirty or mismatched revisions. Passing it
does not prove live Telegram delivery, candidate quality, complete-market
recall, or customer readiness.

## Enable and verify

1. Preserve a verified local and encrypted off-box backup.
2. Deploy the exact reviewed revision with `scripts/deploy.sh` while leaving the
   operator switch off.
3. Run and review the three exact-revision control artifacts; confirm they are
   included in the next off-box artifact backup.
4. Confirm the chosen chat exists once and is an administrator without printing
   tokens or unrelated user data:

   ```bash
   sqlite3 -readonly data/users.db \
     "SELECT COUNT(*) FROM users WHERE chat_id=<id> AND is_admin=1;"
   ```

   The result must be exactly `1`.
5. Set both environment values and recreate only the bridge:

   ```bash
   GIT_SHA=$(git rev-parse --short HEAD) \
     docker compose up -d --no-deps --force-recreate postmarket-operator
   ```

6. Verify its exact revision, health, heartbeat, logs, and outbox destination.
   During a live postmarket candidate, verify one pending/delivered deterministic
   row and no row for any other chat.

The first live session remains a shadow validation session. Do not infer signal
quality from message delivery success.

## Immediate rollback

Set `POSTMARKET_OPERATOR_ALERTS_ENABLED=0` and recreate only
`postmarket-operator`. Confirm its heartbeat reports `disabled`. Already queued
rows are durable; if a bad release queued an unintended row, pause the worker
before any manual outbox intervention and preserve the database/evidence first.
Do not delete or rewrite discovery, lifecycle, rank, or audit evidence.

Code rollback uses the repository's exact ancestor-only deployment runbook.
Operator delivery remains independently disabled during rollback.

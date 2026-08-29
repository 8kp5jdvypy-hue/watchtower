# Market-wide discovery operational control evidence

`tradebot.postmarket_discovery_controls` runs three offline exercises against
the distinct market-wide discovery service. It does not reuse the scheduled-
earnings observer's controls because that would falsely certify a different
selection, persistence, service, and kill-switch path.

The command publishes three revision-bound JSON artifacts conforming to
`truth/postmarket_discovery_control_evidence_v1.schema.json`:

- `discovery_failure_injection`;
- `discovery_kill_switch`; and
- `discovery_delivery_isolation`.

The harness uses deterministic fixtures, temporary SQLite storage, and
checked-in source/configuration only. It cannot fetch live market data, deliver
an alert, place an order, restart a service, change configuration, or open a
production database. Passing evidence never authorizes customer delivery.

## Failure injection

The exercise sends one valid bounded Alpaca/SIP screen row through the real
selection, evaluation, and append-only persistence path while omitting its
bulk-bar response. It verifies that the symbol becomes an attributable
`FETCH_ERROR`, that screen/universe/evaluation counts conserve exactly, and
that no candidate is inserted.

It then injects both a stale screener timestamp and a screener outage. The stale
screen must fail before the bar fetch, both failures must occur before any new
tick or observation is committed, and the temporary database must still pass
`PRAGMA quick_check`.

## Independent kill switch

The exercise verifies every documented true and false spelling, rejects an
ambiguous value, and evaluates the disabled supervisor without a heartbeat. It
also binds checks to SHA-256 digests of Docker Compose, the discovery observer,
and its health probe. The Compose service must use
`POSTMARKET_DISCOVERY_ENABLED` with a `0` fallback, never the scheduled
observer's switch, and the disabled branch must run before database setup.

## Delivery isolation

The exercise parses the observer and persistence modules as Python ASTs and
rejects imports of alert, Telegram, outbox, broker, or order modules. It also
rejects known delivery/order call sites, requires the Compose command to run
only `tradebot.postmarket_discovery_shadow`, requires no bot/worker dependency,
and confirms the service writes local shadow evidence.

This proves code/configuration isolation at the tested revision. It is not a
network sandbox or an approval to add a delivery router. Any future router must
be a separate service with separate state-transition, noise, stale-data,
kill-switch, rollback, and owner-approval evidence.

## Run the suite

Run from a clean checkout after the control code is committed. The revision
must resolve to the checkout's exact `HEAD`; the suite refuses false attribution
to an older or unrelated commit.

```bash
REVISION=$(git rev-parse --short=7 HEAD)
OUTPUT_DIR="data/postmarket_evidence/${REVISION}/discovery-controls"

python -m tradebot.postmarket_discovery_controls \
  --revision "$REVISION" \
  --output-dir "$OUTPUT_DIR"
```

Exit `0` means all three exercises passed and all three artifacts were
published as read-only files. Exit `2` means a revision, control, I/O, or
destination check failed. The suite publishes the directory only after every
exercise and write succeeds; it never replaces an existing evidence set.

The stdout inventory records each path, SHA-256, revision, and completion time.
The future market-wide aggregate readiness manifest must pin those exact bytes.
Review every internal check before locking that manifest.

## Custody and limits

- Keep the exact three-file inventory together under its revision directory.
- Preserve failed results as evidence; never edit failure into success.
- Regenerate under a new revision after any observer, persistence, health,
  Compose, or control-harness change.
- The nightly encrypted off-box backup already includes
  `data/postmarket_evidence` recursively.
- These artifacts do not prove live provider entitlement, complete-session
  coverage, second-provider agreement, empirical precision/recall, production
  deployment, or rollback. Those remain separate readiness requirements.
- The existing cross-cutting rollback rehearsal remains useful but cannot
  substitute for these discovery-specific artifacts.

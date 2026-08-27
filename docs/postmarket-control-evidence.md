# Postmarket operational control evidence

`tradebot.postmarket_controls` runs the three operational exercises required by
the aggregate postmarket evidence gate. It produces immutable JSON artifacts
that conform to `truth/postmarket_control_evidence_v1.schema.json`.

The harness is offline and intentionally incapable of fetching market data,
sending messages, placing orders, editing configuration, restarting services,
or opening a production database. All SQLite work happens in temporary files.
A passing run is evidence that the tested software controls behave correctly
at the recorded revision. It is not approval to enable customer alerts.

## What the exercises prove

### Failure injection

The harness sends provider outage, missing RTH baseline, malformed OHLC, and
single-spike reversal cases through the real postmarket evaluator. It then
persists one complete control tick through the real append-only shadow writer
and verifies:

- every scheduled symbol has one attributable outcome;
- the provider outage is recorded as `FETCH_ERROR`;
- malformed and missing inputs are rejected explicitly;
- persistence failure cannot become a candidate;
- no candidate row leaks from any injected failure; and
- the temporary database passes `PRAGMA quick_check`.

### Kill switch

The harness verifies all documented on/off spellings, rejects ambiguous
configuration, exercises the disabled health state with no heartbeat, confirms
Docker Compose remains default-off, and checks that the observer source imports
no alert, Telegram, order, or broker delivery module. The artifact records the
SHA-256 of both reviewed source files.

This is a deterministic software-control exercise, not an assertion that a
production container was restarted. A production restart is unnecessary for
this shadow-only gate and remains a separate operator action if ever required.

### Rollback runbook

The harness requires both the tested revision and an exact rollback revision to
resolve as Git commits, and requires the rollback target to be an ancestor of
the tested revision. It writes a real shadow tick, takes a SQLite online backup,
mutates the source after the backup, restores into a separate database, and
verifies:

- source, backup, and restored `PRAGMA quick_check` results;
- point-in-time row-count restoration;
- preservation of every append-only trigger; and
- byte-identical backup and restored database files.

The exercise never replaces a live database or changes the checked-out code.

## Running the suite

Run it from a clean checkout of the revision that produced the shadow evidence.
The declared tested revision must resolve to the checkout's exact `HEAD`; the
tool refuses to attribute newer code to an older evidence revision. Use a
reviewed ancestor as the rollback target:

```bash
REVISION=$(git rev-parse --short=7 HEAD)
ROLLBACK_REVISION=$(git rev-parse --short=7 HEAD^)
OUTPUT_DIR="evidence/postmarket-controls/${REVISION}"

python -m tradebot.postmarket_controls \
  --revision "$REVISION" \
  --rollback-revision "$ROLLBACK_REVISION" \
  --output-dir "$OUTPUT_DIR"
```

The command exits `0` only when all three exercises pass and all artifacts are
written. It exits `2` on invalid revision, failed control, I/O error, or an
existing destination. It never overwrites an artifact.

The JSON inventory printed to stdout contains each artifact's path, revision,
completion timestamp, and SHA-256. Copy those values into the locked aggregate
evidence manifest. Do not edit the artifacts after creation; any changed byte
will fail the aggregate digest check.

## Evidence custody

- Keep the three files together under a revision-specific directory.
- Review every internal check and its observed evidence before manifest lock.
- Do not reuse controls from a revision absent from the daily audit/observer
  evidence era; the aggregate gate rejects unrelated control revisions.
- A failed artifact is useful evidence and must not be rewritten as passed.
- Regenerate a new directory after any observer, persistence, health, Compose,
  or control-harness change.

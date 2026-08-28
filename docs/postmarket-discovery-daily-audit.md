# Market-wide postmarket discovery daily audit

The discovery audit converts append-only market-wide shadow evidence into one
immutable, failure-explicit report after the complete postmarket processing
window ends. It is independent from the scheduled-earnings audit because the
two observers have different universe and provenance contracts.

Reports are written to:

```text
data/postmarket_audits/postmarket_discovery_audit_<session>_v2.json
```

Version 2 preserves any immutable version 1 report and publishes a separate
corrected report; historical evidence is never rewritten.

That directory is already included in the encrypted off-box backup and
isolated restore drill. Existing files are never overwritten. The audit opens
the SQLite ledger read-only and has no provider, alert, Telegram, outbox,
broker, or order-routing dependency.

## Session verdict

The expected observation window begins at the actual XNYS session close,
including early closes, and ends at 8:00 PM ET. Publication waits an additional
five minutes so the final 8:00 PM bar has had time to complete; that processing
grace is not misrepresented as missing observer coverage. A session is
ineligible when coverage starts late, ends early, contains excessive tick gaps,
or crosses a holiday/non-session boundary incorrectly. Partial sessions can
therefore be preserved as useful calibration without being described as a
complete qualification session.

The operational audit also rejects:

- duplicate, out-of-order, or negative-duration ticks;
- stale, future, missing, or malformed provider source timestamps;
- incomplete or drifting endpoint, top-N, feed, provider, timeframe, scope,
  revision, universe, threshold, or discovery-version provenance;
- underfilled top-N source scope;
- malformed ranks or screen evidence that disagrees with the tick snapshot;
- broken active-universe, screen, fetch, evaluation, candidate, or error-count
  conservation;
- unexplained missing bulk responses, fetch errors, or excessive latency; and
- candidate ledger timestamps, directions, counts, or metadata that do not
  reconcile to qualifying observations.

Each report includes outcome/source counts, scheduled-earnings overlap,
candidate lifecycles, and non-candidate near misses. Expected non-qualifying
outcomes such as no after-hours trade, a sparse close, or a bar gap remain
visible without being misrepresented as operational failures.

## Automatic operation

The discovery service checks for missing completed reports only while idle or
disabled. The latest immutable verdict is exposed in
`data/postmarket_discovery_heartbeat.json`. Audit failure is itself visible as
`audit_status=error`; it does not silently disappear.

Manual read-only inspection remains available:

```bash
python -m tradebot.postmarket_discovery_audit \
  --db data/postmarket_shadow.db \
  --session 2026-08-27 \
  --audit-code-version "$(git rev-parse --short HEAD)"
```

The command exits nonzero when the operational report is not clean.

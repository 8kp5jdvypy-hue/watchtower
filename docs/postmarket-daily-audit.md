# Postmarket daily audit and empirical evidence

The daily audit turns append-only shadow observations into a failure-explicit
session report. It cannot fetch market data, write the shadow database, send an
alert, contact Telegram, or place an order. After the complete postmarket
window ends, the existing shadow service atomically writes one immutable JSON
report to `data/postmarket_audits/` and exposes audit status in its heartbeat.

## What counts as an operationally clean session

A process being alive is insufficient. A clean session requires all of the
following:

- observation begins within 90 seconds of the actual XNYS close;
- observation continues through 20:05 ET, including final-bar grace;
- no inter-tick gap exceeds 150 seconds;
- scheduled, evaluated, and stored symbol counts conserve on every tick;
- the successful market-wide earnings-ingestion ledger reconciles exactly to
  the symbols observed by the shadow service;
- no fetch error, invariant failure, malformed count, or provenance mismatch;
- one stable observer version, revision, feed, provider, timeframe, catalyst
  source, and threshold snapshot for the session;
- processing latency remains at or below 30 seconds;
- the deduplicated candidate ledger reconciles to recorded new candidates.

The window uses the exchange calendar, so holidays, daylight-saving changes,
and early closes are not interpreted using fixed UTC hours.

This definition corrects the initial 2026-08-26 evidence statement. That run
started at approximately 19:38 ET, about 218 minutes after the regular close.
Its 27 ticks were internally consistent, but the session covered only about
11% of the required window. It is therefore a healthy partial observation and
does **not** count toward the ten clean-session activation gate. The honest
current count is 0/10 complete sessions.

## Empirical labels are a separate evidence source

Operational reports do not manufacture ground truth from observer output.
Activation-quality scoring requires a locked manifest conforming to
`truth/postmarket_empirical_manifest_v1.schema.json` and the stricter runtime
loader. A valid manifest must:

- be created after the full session window ends;
- identify the labeler and a blinded label method;
- declare the move, notional, and persistence policy;
- record provider, feed, endpoint, acquisition time, and SHA-256 for every raw
  evidence artifact;
- label every scheduled symbol exactly once;
- remain blinded to the observer's candidate output while labels are fixed;
- use at least two providers when claiming multi-provider reconciliation.

The tool can validate these declarations and artifact digests syntactically;
it cannot prove that a human reviewer was genuinely blinded. That remains a
review and custody control. A single-provider blind review is independent of
observer output, but it is not evidence of provider agreement.

## Run and interpret

```bash
python -m tradebot.postmarket_audit \
  --db data/postmarket_shadow.db \
  --journal data/journal.db \
  --audit-code-version "$GIT_SHA" \
  --session 2026-08-27 \
  --compact

python -m tradebot.postmarket_audit \
  --db data/postmarket_shadow.db \
  --journal data/journal.db \
  --audit-code-version "$GIT_SHA" \
  --session 2026-08-27 \
  --manifest evidence/postmarket_2026-08-27_v1.json
```

Without a manifest, exit `0` means operationally clean and empirical status is
`NOT_PROVIDED`. Omitting `--journal` leaves an explicit warning and makes the
report ineligible for activation evidence because upstream catalyst admission
was not independently reconciled. A missing/unknown audit code revision also
prevents activation evidence, because the scoring logic must be attributable.
With a manifest, exit `0` requires complete
catalyst-ledger conservation plus operational cleanliness and complete,
direction-consistent empirical results with no false positive or false
negative. Exit `1` means an evidence gate failed; exit `2` means the database,
manifest, or command configuration was invalid.

`session_evidence_eligible=true` means only that this one session may enter the
aggregate evidence set. It never means the product is ready for alerts. The
separate program gate still requires ten complete sessions, aggregate recall,
latency/noise controls, failure injection, a kill switch, and owner approval.

Automatic operational reports include the audit-schema version in their
filename and never overwrite an existing session/version file.
Reviewed empirical reports remain explicit CLI artifacts so a running observer
cannot label or silently rewrite its own performance.

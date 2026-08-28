# Postmarket outcome quality

Perch records forward outcome truth for both scheduled-earnings candidates and
market-wide discovery candidates. This subsystem is observational: it has no
alert, Telegram, broker, order, or customer-delivery dependency.

## Checkpoints and semantics

Each candidate receives append-only events for +5, +15, +30, and +60 minutes,
postmarket close, next-session open, and next-session close. The baseline is the
first knowable candidate bar close. Marks use completed five-minute SIP bars and
the XNYS calendar. They include directional return, MFE, MAE, time-to-MFE, bar
count, target distance, and full provider/feed/timeframe/revision provenance.

An absent symbol in a provider bulk response is a fetch error, not a zero return
and not `NO_BAR`. The checkpoint stays unresolved and is retried. A response that
explicitly contains the symbol with no qualifying completed bars can produce an
explicit `NO_BAR` event after the relevant session is final.

## Automatic maintenance

The enabled market-wide shadow service plans only unresolved, finalized
candidate checkpoints. Requests are deduplicated by symbol and session, so one
bounded bulk request can serve both candidate streams. Same-session outcomes are
finalized after 20:05 ET. Next-session open and close marks become eligible five
minutes after their calendar targets. Exact replays do not add rows.

Heartbeat keys expose maintenance state:

- `quality_backfill_status`
- `quality_candidates_planned`
- `quality_marks_written`
- `quality_unresolved_checkpoints`
- `quality_fetch_errors`
- `quality_fetch_error_details`
- `quality_reports_written`
- `quality_latency_ms`
- `latest_quality_reports`

`degraded` means at least one due checkpoint remained unresolved or a provider
response failed. `error` includes a bounded exception summary. Neither state is
silently treated as clean evidence.

## Immutable daily reports

Once every checkpoint for a stream/session has an event, the service writes:

```text
data/postmarket_audits/postmarket_quality_<stream>_<session>_v<N>.json
```

Reports fail closed below the fixed 20-candidate sample floor or when any mark is
unavailable. Re-running unchanged evidence writes nothing. A later provider
correction appends new mark events and, if report semantics change, creates a
new report version instead of rewriting history. The existing nightly artifact
backup includes these files because it archives `data/postmarket_audits/`.

## Safety boundary

Outcome maintenance does not change candidate qualification, thresholds,
ranking, lifecycle, or delivery. It supplies the empirical labels those future
systems must use. Customer alerts remain separately gated.

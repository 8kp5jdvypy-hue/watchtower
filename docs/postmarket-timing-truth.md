# Postmarket timing truth

Market-wide discovery stores one append-only timing row for every persisted
tick. The timing ledger is separate from candidate observations so a delayed or
missed cycle cannot be hidden by otherwise valid market data.

## Schedule contract

The one-minute schedule is anchored to the actual XNYS session close. For each
tick Perch records the scheduled timestamp, actual start, scheduled lag, and
number of skipped schedule slots. The service sleeps to the next anchored slot
after processing instead of sleeping a fixed minute and accumulating drift.
Early-close sessions use their real exchange close.

Startup delay is explicit: if the first tick begins ten minutes after close,
the first timing row records ten missed cycles. Later gaps are counted from the
previous persisted schedule slot. A failed cycle leaves no partial row; the
next successful cycle accounts for the skipped slot.

## Stage measurements

Each tick records non-negative processing durations for:

- provider screen;
- local selection and provenance validation;
- bounded-screen plus full-universe-shard bar fetch;
- completed-bar evaluation; and
- total screen-to-evaluation processing.

The tick also records the count, average, and maximum logical persistence span
seen in its evaluations. This is market-time confirmation delay (for example,
300 seconds across two consecutive five-minute bars), not CPU time.

Timing rows and their update/delete triggers are created by the normal shadow
database connection. They are committed atomically with the tick, observations,
and any new candidates.

## Audit behavior

Discovery audit version 4 requires a one-to-one timing/tick mapping and checks
schedule arithmetic, missed-cycle counts, stage sums, total latency,
persistence summaries, timestamp agreement, and deterministic five-tick sweep
shard assignment. Missing timing, excessive lag, missed cycles, malformed shard
metadata, or inconsistent measurements block a clean session. When a tick gap
exists, the report names the slowest stage on the preceding tick when that
evidence is available.

These measurements are operational evidence only. They do not change candidate
thresholds, ranking, alerts, or trading behavior.

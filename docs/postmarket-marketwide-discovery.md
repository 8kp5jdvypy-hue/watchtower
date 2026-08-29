# Market-wide postmarket discovery shadow

The earnings observer answers a catalyst-specific question: how did every
scheduled after-hours reporter react? The market-wide discovery shadow answers
a different question: which tradable US stocks became exceptional after the
close, including symbols without a known earnings catalyst?

It is a default-off, alert-incapable service. It cannot import or call Telegram,
the outbox, a broker, or an order path. Its only durable output is append-only
evidence in `data/postmarket_shadow.db`, which is already included in local and
encrypted off-box backups.

## Two-lane discovery, one strict evaluator

The fast lane uses Alpaca's real-time SIP stock screeners at their maximum
documented bound of 50 rows per side/list:

- market gainers;
- market losers;
- most active by volume; and
- most active by trade count.

Alpaca performs the market-wide ranking. Perch records the requested bound,
endpoint set, provider timestamps, returned rows, unique symbols, active-universe
count and excluded symbols. A top-N response is never represented as a full
per-symbol census.

The coverage lane sorts the canonical active universe, SHA-256 binds that exact
snapshot, divides it into five deterministic disjoint shards, and requests one
shard per exchange-close-anchored minute. A complete five-tick cycle therefore
covers every active symbol once at the same cadence as completed five-minute
bars. With roughly 13,100 active symbols, a normal shard is about 2,620 symbols;
the implementation refuses a shard above the explicit 4,000-symbol safety
bound. The vendor adapter further divides each shard into bounded 1,500-symbol
requests and fetches only the final RTH bar plus the elapsed postmarket window.

The service unions both lanes, deduplicates overlap, annotates scheduled
earnings, and invokes the existing strict reaction evaluator. A provider
screener percentage or sweep membership is never enough to create a candidate.
Qualification still requires:

- the actual exchange-calendar RTH closing bar;
- two consecutive completed five-minute postmarket bars;
- an 8% same-direction move from the RTH close;
- at least $100,000 cumulative postmarket notional;
- fresh, ordered, unique, contiguous, nonzero-volume, valid OHLC bars; and
- no unstable close/last-print divergence.

Missing bulk-bar responses become explicit `FETCH_ERROR` observations. A sweep
request failure cannot fabricate a candidate and does not erase valid results
from the bounded lane. Every discovered symbol has exactly one evaluation.
Version-2 tick evidence stores provider counts separately from sweep universe
digest, schedule, shard index/count/size, overlap, and per-symbol universe
position, with tick-level conservation invariants.

This closes the structural top-200 recall hole; it does not by itself prove
complete live coverage. Provider omissions, latency, restarts, and missed cycles
remain measurable failure modes. The independent same-night full-universe
recall census remains mandatory.

## Enablement

`POSTMARKET_DISCOVERY_ENABLED=0` is the default and is an independent kill
switch from `POSTMARKET_SHADOW_ENABLED`. Building or deploying the service does
not start market-data polling unless an operator explicitly sets it to `1` and
recreates that service.

The initial release is shadow evidence only. Customer delivery requires a
separate approval after complete-session observation, replay calibration,
false-positive/false-negative review, latency/error review, and a tested routing
kill switch. The existing earnings evidence gate is not reused or reset by this
separate discovery stream.

Discovery-specific offline failure, kill-switch, and delivery-isolation
exercises are documented in
`docs/postmarket-discovery-control-evidence.md`. They certify only those named
controls at an exact revision. They do not replace the market-wide census,
independent-provider proof, empirical holdout, complete clean sessions, or the
aggregate discovery readiness gate documented in
`docs/postmarket-discovery-evidence-gate.md`.

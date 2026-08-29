# Market-wide postmarket discovery shadow

The earnings observer answers a catalyst-specific question: how did every
scheduled after-hours reporter react? The market-wide discovery shadow answers
a different question: which tradable US stocks became exceptional after the
close, including symbols without a known earnings catalyst?

It is a default-off, alert-incapable service. It cannot import or call Telegram,
the outbox, a broker, or an order path. Its only durable output is append-only
evidence in `data/postmarket_shadow.db`, which is already included in local and
encrypted off-box backups.

## Two-stage coverage

Stage 1 uses Alpaca's real-time SIP stock screeners at their maximum documented
bound of 50 rows per side/list:

- market gainers;
- market losers;
- most active by volume; and
- most active by trade count.

Alpaca performs the market-wide ranking. Perch records the requested bound,
endpoint set, provider timestamps, returned rows, unique symbols, active-universe
count, excluded symbols, and the aggregate active-universe count not returned by
the bounded provider screen. A top-N response is never represented as a full
per-symbol census.

Stage 2 deduplicates that union, annotates scheduled earnings overlap, bulk-fetches
one SIP five-minute session snapshot, and invokes the existing strict reaction
evaluator. A provider screener percentage is never enough to create a candidate.
Qualification still requires:

- the actual exchange-calendar RTH closing bar;
- two consecutive completed five-minute postmarket bars;
- an 8% same-direction move from the RTH close;
- at least $100,000 cumulative postmarket notional;
- fresh, ordered, unique, contiguous, nonzero-volume, valid OHLC bars; and
- no unstable close/last-print divergence.

Missing bulk-bar responses become explicit `FETCH_ERROR` observations. Every
discovered symbol must have exactly one evaluation, and tick-level conservation
invariants are persisted with the source evidence.

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

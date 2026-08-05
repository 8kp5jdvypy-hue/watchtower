# Watchtower

A market scanner watching SPY, QQQ, GOOGL, TSLA, BE, IONQ for notable
intraday conditions and pushing alerts to Telegram. It never places
orders and has no broker write access.

## Rules

- Detectors and signal functions are PURE: data in, `Detection | None`
  out. No I/O, no network, no clock reads, no globals.
- `Bar.ts` is the timestamp of the bar's OPEN, in UTC. A 5-minute bar
  stamped 14:30 is not knowable until 14:35. Never make a decision
  timestamped before the bar it used has closed.
- All market data access goes through the `MarketData` protocol. No
  vendor SDK imports outside its own adapter module.
- All thresholds are expressed in ATR units, never percentages.
- Anchors are computed once per session and frozen. Never recomputed per
  bar.
- Every detection is journaled before any alert is sent, including
  sub-threshold ones.
- Live alerting is opt-in via `--live`. Default is log-only.
- Python 3.11+, type hints, dataclasses over dicts. Boring code
  preferred.
- Run pytest before claiming a task is complete. Report failures; do not
  describe a task as done while tests are red.

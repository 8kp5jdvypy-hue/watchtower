# broad_scan honesty fix: proposal

Proposal only — no code changes. Responds to the earlier read-only
trace of broad_scan's Stage 1 output, which found that symbols
promoted into the detector suite by the daily screen
(`tradebot/broad_scan.py`) are, once merged, indistinguishable from
fixed-watchlist symbols everywhere downstream: Telegram alerts, the
dashboard, and the performance-stats endpoints. `--broad-scan` is
live-by-default in production, so this isn't a hypothetical edge case
— it's the default subscriber experience today.

## Root cause, in one sentence

**No detection record, at any layer, carries a field recording whether
its symbol was in the fixed watchlist or promoted in by broad_scan —
the only place this fact is even knowable is a merge line in
`runner.py`, and it's discarded immediately after.**

Specifically: `scan_symbols = WATCHLIST + [s for s in dynamic_symbols
if s not in WATCHLIST]` (`tradebot/runner.py:1068`) is the single point
in the whole pipeline where watchlist-vs-promoted is known. From there,
`symbol` alone flows into `process_new_bar` (`runner.py:1114`) with no
accompanying origin flag, into the in-memory `Detection`
(`tradebot/detectors.py:84-90`) and `Cluster`
(`tradebot/alerts.py:26-44`) objects — neither has an origin field —
and finally into the `detections` table
(`tradebot/journal.py:42-67`, full column list: `id, ts_utc, session,
symbol, kinds, headlines, score, tier, close, atr14, trend,
context_json, code_version, alerted, suppress_reason, no_trade,
news_driven, primary_kind, symbol_class, event_kind, event_severity,
suppress_category, lifecycle_state, related_detection_id`) — no origin
column exists. (`symbol_class` looks tempting but is options-liquidity
class, unrelated — `tradebot/config.py:19-25`.)

This means every proposal below except (c) requires the same first
step: **add an origin field and thread it through.** (c) is copy-only
and doesn't depend on it.

## (a) Distinct labeling wherever a subscriber sees a promoted symbol

**Schema change (shared prerequisite):** add `origin` (or similar) to
the `detections` table — `"watchlist"` / `"screening"` — set once at
write time from the `runner.py:1068` merge fact, threaded through
`Cluster` construction (`runner.py:368-382`) and
`write_cluster()` (`journal.py:192-241`).

**Telegram:** `render_high_alert`
(`tradebot/rendering/templates.py:148-188`) builds its headline purely
from `cluster.tier`/`cluster.symbol`/`cluster.trend`
(line 172: `f"{tier_emoji} {cluster.tier.upper()} · {symbol} ·
{bias}"`). No badge primitive exists yet in `fields.py` (only numeric
formatters: `money, pct, rate, atr, qty, ratio, ts, dash`). Proposed:
a one-line addition to the message when `cluster.origin == "screening"`
— e.g. a `📡 Radar` tag near the tier badge, plus a single explainer
line the first time a subscriber sees one in a session (or always, if
that reads better in practice): *"Radar: this symbol isn't on your
core watchlist — Perch's daily screen flagged it as active today."*
Exact copy/placement is a design call, not specified here.

**Dashboard:** needs the new field surfaced through `_recent_signals`
(`tradebot/api/app.py:404-429`, currently selects `id, ts_utc, session,
symbol, kinds, headlines, score, tier, trend, alerted, primary_kind,
context_json, close` — no origin) and both consumers,
`/signals/feed` and `/signals/today` (`app.py:431-445`). Frontend side:
a small "Radar" pill on the signal card, same explainer copy as
Telegram for consistency, one component (likely
`web-app/src/components/SignalCard.jsx`, not directly inspected here
but the natural shared render point for both Feed and Today).

**Effort: medium.** The schema/threading work is the bulk of it (touches
`runner.py`, `alerts.py`, `journal.py`, `api/app.py`); the actual
badge rendering on each surface is small once the field exists.

## (b) Exclude screening-symbol detections from the performance stats

Confirmed today: **none of `historical_performance()`,
`tier_performance()`, or `kind_performance()` filter by symbol or
origin at all.**

- `historical_performance(conn, kind, trend, exclude_id, lookback=20,
  offset_min=30)` — `journal.py:358-365`; WHERE clause
  (`journal.py:385-396`): `d.primary_kind = ? AND d.trend = ? AND
  d.id != ? AND (d.news_driven IS NULL OR d.news_driven = 0)`.
- `tier_performance(conn, offset_min=30)` — `journal.py:427-441`; no
  WHERE clause beyond the `marks` join.
- `kind_performance(conn, offset_min=30)` — `journal.py:474+`; same
  no-symbol-filter pattern.

Proposed: once (a)'s `origin` column exists, append `AND d.origin =
'watchlist'` to all three queries (or an equivalent `NOT IN
('screening')` if origin values expand later). This is a small,
mechanical change *once the column exists* — the real work is (a)'s
schema/threading step, which this shares.

**Why this should land alongside Decision B, not before or separately:**
Decision B (from `docs/sip-migration-proposal.md`, pending merge via
PR #17) already plans to touch these exact same three functions to add
post-cutover-only filtering for the SIP migration. Landing both filters
in the same change avoids two separate migrations touching the same
WHERE clauses in different commits, and avoids a window where stats
are correct on data-feed grounds but still contaminated by
screening-origin symbols (or vice versa). Recommend one combined PR,
scoped to `journal.py`'s three performance functions, implementing
both filters together.

**Effort: small, contingent on (a).** The query changes themselves are
one line each; the dependency is what makes this "land alongside
Decision B" rather than "do immediately."

## (c) Corrected copy for Today.jsx and Feed.jsx

No dependency on (a)/(b) — pure copy fix, can land independently and
immediately.

- `web-app/src/components/Feed.jsx:29`:
  `<h1>Recent activity, across the whole watchlist.</h1>` — literally
  claims watchlist-only scope, which has been inaccurate since
  `--broad-scan` went live-by-default.
  **Proposed:** `<h1>Recent activity, across your watchlist and
  today's radar picks.</h1>` (or similar — exact wording is a product
  call, not specified here; the point is dropping the false
  "watchlist-only" claim).

- `web-app/src/components/Today.jsx:124`:
  `<p>Perch is watching SPY, QQQ, GOOGL, TSLA, BE, IONQ right now. The
  moment something unusual happens, a card like the ones below will
  show up here.</p>` — hardcodes 6 of the current 17 WATCHLIST symbols
  (stale even on watchlist-only grounds, since WATCHLIST has grown
  since this copy was written) and omits broad_scan entirely.
  **Proposed:** drop the hardcoded symbol list (it will always drift
  out of sync with `config.WATCHLIST`) in favor of something like
  *"Perch is watching your full watchlist, plus scanning the market
  for anything else worth flagging. The moment something unusual
  happens, a card like the ones below will show up here."* If a
  concrete count is wanted, it should come from `/watchlist`
  (`tradebot/api/app.py:369-375`, already returns `config.WATCHLIST`)
  rather than being hardcoded again.

**Effort: trivial.** Two copy edits, no backend dependency, ships
independently of everything else in this proposal — recommend doing
this one first regardless of what else gets approved.

## (d) Retroactive identification of already-journaled screening rows

**Partially possible, not a clean query.** `WATCHLIST` is a plain,
unversioned list (`tradebot/config.py:3-7`) that has changed three
times in git history (`22d5f57`, `a2d96de` — the 6→17 symbol
expansion, `3c401fe` — unrelated). There's no snapshot of "WATCHLIST as
of detection X's write time" stored anywhere in the DB directly.

The bridge that makes it *partially* reconstructable: every detection
row already stores `code_version` (`journal.py:55`, the git short-hash
at write time, set via `code_version()` at `journal.py:170-180`). In
principle, `git show <code_version>:tradebot/config.py`'s `WATCHLIST`
for each distinct `code_version` present in the table, cross-referenced
against each row's `symbol`, would classify old rows without needing a
schema migration for history.

**Two caveats that keep this "partial," not "solved":**
1. This assumes the deployed process at write time exactly matched the
   named commit in the repo — not verifiable from the DB alone, though
   plausible given the deploy flow (`git pull && docker compose up -d
   --build`) leaves little room for drift.
2. It's an offline reconstruction script against the DB + git history,
   not a query `historical_performance()` etc. can run live — so even
   after building it, (b)'s live filter still needs (a)'s real column
   for anything written going forward. The retroactive script would be
   a one-time backfill of that same column for historical rows, not a
   permanent parallel mechanism.

**Proposed, if retroactive cleanup is wanted:** a one-off script,
`scripts/backfill_detection_origin.py` (not written — this is a
proposal), that for each distinct `code_version` in `detections`,
checks out that commit's `config.WATCHLIST`, and sets the new `origin`
column accordingly for all rows with that `code_version`. Rows from
before `WATCHLIST` existed as a concept, or from a `code_version` not
present in git history (e.g. uncommitted local runs), would need a
documented fallback (skip, or mark `"unknown"`) rather than a silent
guess.

**Effort: small-medium**, and entirely optional — (b)'s stats fix
works going forward without it; this only matters if you want the
`historical_performance()` family to also exclude *already-journaled*
screening detections rather than just future ones.

## Summary of what's independent vs. sequenced

- **(c)** — do anytime, no dependency, trivial effort.
- **(a)** — the schema/threading prerequisite for both (b) and, if
  wanted, (d). Medium effort, the biggest single piece of work here.
- **(b)** — small once (a) exists; recommend bundling with Decision B's
  `data_feed` filtering in one PR touching the same three functions.
- **(d)** — optional, small-medium, only relevant if historical rows
  need retroactive correction rather than just going clean from here
  forward.

Nothing in this document has been implemented.

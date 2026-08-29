# Full Code Review — Watchtower

Read-only, full-codebase correctness review. No code or config was changed
as part of this review. Scope: `tradebot/` (detectors, runner, journal,
broad_scan, marketdata, alerts, rendering, telegram_bot, api, vendors),
`scripts/`, `web-app/src`, `web/src`, and the test suite. `pytest` was run
and is green (702 passed) — the findings below are about what the suite
doesn't cover or verify, not about currently-failing tests.

## Context: tonight's incident

`backfill_marks()` (`tradebot/journal.py:343`) wrote 0 marks for ~160
detections at today's 16:00 session close, with no error, no log entry, no
alert. This review found the exact mechanism and traced it one layer
further upstream than expected — see the Silent Failure Inventory below,
items 1–3. That chain (missing-cache → empty list → zero writes →
uncaptured return value → ambiguous API response → frontend shows stale
"Pending" forever) turned out to be one instance of a repeated pattern in
this codebase: several other jobs have the identical shape and are equally
unmonitored today.

---

## Ranked findings

### CRITICAL

**1. `tradebot/runner.py:1164` — live-mode `backfill_marks()` return value is discarded entirely**
`backfill_marks(conn, session_date)` is called as a bare statement in
`run_live` (the actual production path). The replay-mode call site
(`runner.py:863`) at least does `print(f"backfilled {marks_written} ...")`;
the live path doesn't even do that — there is no code path, in production,
that could ever have surfaced tonight's "wrote 0 of ~160" state.
*Fix: capture the return value, log it, and emit a metric/alert if
`written == 0` while `len(detections for session) > 0`.*

**2. `tradebot/journal.py:327-333` + `tradebot/marketdata.py:99-101` — missing cache file silently becomes an empty bar list, not an error**
`_read_bars()` does `if not path.exists(): return []`. `_all_bars_for_session`
builds a `ReplayMarketData` on top of that, so a missing/wrong `cache_dir`
or an unfetched symbol/session produces `bars = []` with zero exceptions.
`backfill_marks` (`journal.py:371-386`) then loops over that empty list:
every offset's `_price_at_or_after` returns `None` (skipped, "session
ended before reaching it" — the same code path as a genuinely-missing
future offset), and `if bars:` is `False` so even the close mark never
writes. A total infrastructure failure (missing file) and a benign
"nothing to do yet" condition produce byte-identical output.
*Fix: have `_read_bars` (or a wrapper used specifically by `backfill_marks`)
distinguish "file exists and is empty/short" from "file does not exist at
all" and raise or log loudly on the latter — a missing cache file for a
session that's already closed is never legitimate.*

**3. `scripts/fetch_cache.py:56-89` (`ensure_sessions`) — likely the actual upstream root cause, and it's already silent by design**
When Alpaca returns no bars for a candidate day, this silently records
`"no data (holiday?)"`; when it can't satisfy the requested session count
within `MAX_LOOKBACK_DAYS`, it appends a "gave up" string. Both statuses
only ever reach `print()` in `main()` — never a log file, exception, or
alert. If this script is what's supposed to keep
`data/cache/{symbol}/intraday_{date}.csv` populated and it failed silently
today (credential expiry, vendor outage, run-before-close timing), the
result is exactly the missing file that produced finding #2 above — one
layer further upstream than the mechanism already root-caused.
*Fix: make `ensure_sessions`/`main` exit non-zero (or alert) when any
symbol ends today's run with `"gave up"`, and treat a `WATCHLIST` symbol
missing today's session file as a hard failure, not a print line.*

**4. `tests/test_journal.py:199-247` — `backfill_marks()` has no test for a missing/empty cache directory, i.e. no test covers tonight's actual failure shape**
Both existing tests write real CSVs into `cache_dir` before calling
`backfill_marks`. Neither asserts what happens when the directory or the
symbol's file is absent. Given `_read_bars` "fails safe" to `[]`
(finding #2), a regression that made this path *fabricate* a price instead
of silently no-op'ing (a much worse outcome) would go completely
undetected.
*Fix: add `test_backfill_marks_missing_cache_dir_writes_zero_marks_and_signals_it`
that exercises this exact path and asserts on whatever loud signal fix #2
adds, not just `written == 0`.*

**5. `tradebot/runner.py:311-524` — a Telegram alert can be sent and durably delivered before the detection row it references is ever committed to `journal.db`**
`journal_write_cluster()` inserts the detection row (line 311) on `conn`
(journal.db), but `conn.commit()` doesn't happen until line 524. On the
NO-TRADE path (no `record_contract_selection()`, which is the only thing
that commits early — see `journal.py:714-748`), `alerter.send(...)` at line
473 writes to a **separate** connection (`users.db`'s outbox,
`telegram_bot/outbox.py:133`) which commits immediately and can deliver to
Telegram within seconds — well before journal.db's transaction closes. A
SIGKILL/OOM/power-loss between line 473 and line 524 rolls back the entire
`journal.db` transaction: the detection row disappears, but the user
already received a real alert referencing its (now nonexistent)
`detection_id`. This directly violates CLAUDE.md's "every detection is
journaled before any alert is sent" and breaks every later lookup
(`historical_performance`, `backfill_marks`, `/took`) against that id.
*Fix: commit the detection INSERT (and `alerted` flag update) to
journal.db immediately after `journal_write_cluster()`, before any
`alerter.send()` call — mirroring what `record_contract_selection()`
already does on the trade path.*

### HIGH

**6. `tradebot/universe.py:70-135` (`refresh_universe`) + `tradebot/vendors/alpaca.py:318-354` (`fetch_us_equity_assets`) — an empty/partial vendor response silently delists the entire scan universe**
`fetch_us_equity_assets()` returns whatever `client.get_all_assets(...)`
gives back with no non-empty/sanity check. `refresh_universe` treats any
symbol previously `is_active=1` but absent from the fresh fetch as
delisted (lines 122-128) — if the fetch legitimately succeeds (200 OK) but
returns zero or a truncated set of rows (rate limit, pagination bug,
transient vendor issue), every real symbol gets marked `is_active=0` in
one call, with no error and no size sanity-check.
*Fix: refuse to reconcile (raise or no-op with a loud log) if
`len(fetched)` is drastically smaller than `len(existing active)`, e.g.
below some percentage floor.*

**7. `tradebot/journal.py:452,464` (`historical_performance`) — `avg_return_pct` sign convention disagrees with its sibling functions across the dashboard**
`kind_performance`, `tier_performance`, and `hour_performance` all flip the
sign of the raw return for a "down" trend (`r if trend == "up" else -r`,
e.g. `journal.py:502,575,649`) before averaging, so a positive number
always means "continued in the predicted direction." `historical_performance`
does not — it averages the raw signed `(price-close)/close` directly. Same
underlying rows, opposite sign convention, shown side by side: a 5-sample
down-trend setup that continued down 4/5 times reports
`avg_return_pct = -2.1%` from `historical_performance` (used by
`/signals/<id>`) and `+2.1%` from `kind_performance` (used by
`/performance`) for what a reader would reasonably assume is the same
number.
*Fix: sign-flip `historical_performance`'s `returns` the same way its
siblings do, and add a regression test asserting all four functions agree
in sign on a shared fixture.*

**8. RESOLVED — one technical-performance population contract**

`historical_performance`, `kind_performance`, `tier_performance`, and
`hour_performance` now all use current-feed, watchlist-origin,
non-news-driven detections with real outcome marks. Tier/hour results retain
`excluded_news_driven` counts so the exclusion is visible rather than a
silent denominator change; the Performance UI and hour report display it.
Regression fixtures mix old-feed, screening-origin, and news-driven rows and
prove none can contaminate the clean technical sample.

**9. RESOLVED — `web-app/src/components/SignalDetail.jsx` no longer infers every missing mark as "pending"**
`afterDetectionRows()`/`pendingResolutionLabel()` always renders either a
countdown or a calm "Resolves after session close" — indefinitely, with no
upper bound and no explicit status field. This is exactly tonight's
incident on the frontend side: nothing here (or in the API response —
see finding #14 below) can distinguish "hasn't happened yet" from "the
backfill ran and silently wrote nothing."
*Resolution: `/signals/<id>` now exposes total per-checkpoint outcome states
and an aggregate resolution status. The component renders separate pending,
close-batch waiting, not-reached, data-unavailable, and delayed copy; legacy
servers still fall back to real mark rows. Missing resolution after the
bounded post-close grace period is degraded rather than calm indefinitely.*

**10. `tests/telegram_bot/test_handlers.py:37,769-781` — the admin-halt test collapses three conceptually different IDs onto the same literal `999`, the exact shape of the project's known chat_id incident**
The shared `_app()` fixture sets `admin_ids=frozenset({999})`; the halt
test then uses `telegram_user_id=999` and `chat_id=999` for the same user.
`handlers.handle_halt` correctly checks `ctx.user.is_admin` (resolved via
`user_id`), not `ctx.chat_id in ctx.app.admin_ids` — but this test cannot
tell the two implementations apart, since both pass when all three values
are identical. It's the only test exercising the admin-halt path.
*Fix: give the admin fixture a `chat_id` different from both
`telegram_user_id` and the `admin_ids` value, so a future swap of chat_id
for user_id in the admin check would actually fail this test.*

**11. RESOLVED — `scripts/fetch_cache.py` atomic cache publication**
CSV files are written directly to their final path (no temp file + atomic
rename). If the process is killed mid-write, the partial file remains;
every subsequent run sees `path.exists()` and reports "skipped (exists)" —
it is never re-fetched or re-validated. `_read_bars` (`marketdata.py:99`)
has no row-count/checksum/trailing-newline check, so it parses whatever
bytes are on disk and silently returns a bar list with a truncated final
row's numeric field parsed as if valid, not empty and not raising.
The fetcher now writes through a same-directory temporary file and publishes
with `os.replace()` only after the shared CSV writer completes. It also exits
nonzero on exhausted/real-session-empty acquisition. Regression tests prove a
failed partial write leaves neither a final file nor a temporary artifact.

**12. RESOLVED — contract-forward vendor failure attribution**

`_forward_mid` now lets chain fetch/auth/network exceptions reach
`backfill_pending_contract_mids`' per-selection logger; only a successful
chain missing a required leg returns `None`. `fetch_option_day_range` follows
the same contract: API/transport failures propagate to the day-range backfill
logger, while a successful response with no trade bars returns `None`.
Regression tests prove both attribution and sibling-contract isolation.

**13. RESOLVED — `run_replay`/`run_live` per-symbol exception isolation**

Direct tests now execute both real loops, inject a failure from one symbol's
`process_new_bar` call, prove every later symbol in the same pass is still
evaluated, and assert the exception is retained in `HeartbeatStats.errors`.
This pins the highest-blast-radius coverage boundary against a misplaced
`raise`, `return`, or broken handler.

### MEDIUM

**14. RESOLVED — `signal_detail` makes outcome resolution explicit even when `marks` is empty**
The endpoint's own comment acknowledges marks are "empty until [backfill]
... never fabricated for an interval that hasn't been reached yet," but
there's no field distinguishing those two empty states, which is the root
cause of finding #9 above being possible on the frontend at all.
*Resolution: the journal records an append-only event for every requested
checkpoint on every close-batch attempt. SQLite constraints enforce valid
final states, price consistency, attempt uniqueness, and no update/delete.
The endpoint returns `outcomes` with status/reason/resolution time plus an
aggregate `outcome_status`; historical real-price marks remain compatible.*

**15. `tradebot/metrics.py:46-57` (`increment`) — a corrupted counters file is silently reset to `{}`, discarding all historical counts, then overwritten non-atomically**
`except (json.JSONDecodeError, OSError): data = {}` treats any corruption
as "start fresh," and the subsequent `path.write_text(...)` is a direct
write with no temp+rename — so a crash mid-write can itself produce the
corruption that gets silently "recovered" (erased) on the next call. This
file is one of the few places this codebase looks at "how often is X
happening," including several of the failure counters cited elsewhere in
this review (`dedup_check_failed`, `validator_rejection`, etc.) — a
silent reset erases exactly the evidence needed to notice a failure spike.
*Fix: write via temp file + `os.replace()`; on decode failure, log loudly
and back up the corrupt file instead of silently discarding it.*

**16. `tradebot/status_page.py:69-76` (`_missed_alerts_by_rule`) — the public status page is blind to most of the failure counters the codebase already writes**
Only `metrics.json` keys prefixed `validator_rejection` are surfaced.
`dedup_check_failed`, `event_window_downgrade`, `event_window_suppression`,
`duplicate_suppression`, and `data_health_suppression` are all recorded
(see `runner.py:300,304,353,372,398,409`) but never displayed — the one
page meant to catch exactly this class of problem has an undetected blind
spot for most of what it could show.
*Fix: surface all `*_suppression`/`*_failed` metric families on the status
page, not just `validator_rejection`.*

**17. `tradebot/journal.py:149-157` (`_add_column_if_missing`) — unconditionally treats any `sqlite3.OperationalError` from `ALTER TABLE` as "column already exists"**
If the real cause is a full disk, a corrupted DB, or a permissions issue,
it's silently misdiagnosed as benign and swallowed — runs on every
`connect()` call, so a corrupted `journal.db` could present as a clean
startup.
*Fix: match on the specific "duplicate column name" error message/code
rather than the whole exception class.*

**18. `tradebot/runner.py:242-259` (`TelegramHaltChecker.check()`) — a network blip while polling for `/halt` is indistinguishable from "not halted," silently dropping an admin's halt command**
Documented as intentional fail-open, but it means an operator who sends
`/halt` during a transient Telegram API error gets no feedback and no
retry — they may believe the bot is halted when it is still running.
*Fix: distinguish "confirmed not halted" from "couldn't check" and alert
loudly (not just fail open) on repeated check failures.*

**19. `web-app/src/api.js:23` — a 200 response with an unparseable JSON body silently becomes `null`, indistinguishable from "still loading"**
`await response.json().catch(() => null)` guards this on the success path
too, not just the error path. Downstream, every consumer (`Today.jsx`,
`Feed.jsx`, `SignalDetail.jsx`, `Watchlist.jsx`, `Performance.jsx`) gates
content strictly on `data &&`, so a malformed-but-200 response renders
identically to "hasn't fetched yet" rather than surfacing an error.
*Fix: only swallow the parse failure on non-ok responses; on `response.ok`
with a bad body, throw/set an explicit error state.*

**20. `web-app/src/App.jsx:60-61` — any `/me` failure (network blip, 500, timeout) is treated as "signed out"**
`api.me().then(setAccount).catch(() => setAccount(null))` renders the
`Login` screen for every failure mode, not just an actual invalid session.
An active user hitting a transient backend hiccup gets silently logged out
with no retry affordance.
*Fix: distinguish auth failure (401) from transport failure; only clear
the session on the former.*

**21. `tradebot/journal.py:219-223,267-288` (`cluster_id`/`write_cluster`) — the detection identity key is a 64-bit-truncated SHA-256 with a silent `ON CONFLICT ... DO UPDATE`**
A genuine collision between two different `(symbol, session, ts_utc,
kinds)` tuples would silently overwrite one detection's row with another's
— no secondary check. Low probability at current volume, but it's a real
gap relative to every other identity key in this codebase (e.g. outbox's
`UNIQUE(alert_id, chat_id)` is a true business key, not a hash).
*Fix: widen the truncation or add a collision check (verify the existing
row's non-hash fields match before allowing the upsert to proceed).*

**22. `web-app/src/components/Watchlist.jsx:19-21` — the signals-feed fetch's error is discarded, making "fetch failed" look identical to "no active signals"**
`const { data: today } = useApiData(fetchToday)` never destructures
`error`; a failed `/signals/today` while `/watchlist` succeeds renders the
calm "quiet" badge for every symbol with no indication anything failed.
*Fix: destructure and surface `error` from the hook; render a distinct
error state instead of the default "quiet" badge.*

**23. `tests/telegram_bot/test_handlers.py:64-70` (`_ctx()`) — every one of ~80 handler tests hardcodes `chat_id == user_id`, structurally**
No test in this file ever constructs a context where `chat_id` differs
from the acting user's `telegram_user_id` (e.g. a group-chat scenario).
Current handler code doesn't read `ctx.chat_id` for per-user identity
today (verified), so there's no live bug — but the fixture design means a
future handler that accidentally swapped in `ctx.chat_id` for a per-user
DB lookup would pass all ~80 tests in this file silently, which is exactly
the shape of the project's known past incident.
*Fix: parametrize `_ctx()` (or add one dedicated test) with a `chat_id`
distinct from `user_id`/`telegram_user_id` to give at least one test in the
file the power to catch an identity mixup.*

**24. `web-app/src/hooks/useQuotes.js:37-39` — a sustained quote-fetch outage is invisible to the user indefinitely**
`.catch(() => { if (!cancelled && !hasLoaded) setQuotes({}) })` discards
the error after the first successful load; stale quotes stay on screen
with no "as of" indicator. Reads as a deliberate choice for transient
blips per the file's own comments, but nothing distinguishes a 5-second
hiccup from an hours-long outage.
*Fix: track time-since-last-successful-quote and surface a staleness
indicator past some threshold.*

### LOW

**25. `tradebot/analytics.py:63-72` (`_full_session_bars`) — a near-exact duplicate of `journal._all_bars_for_session`, inheriting the same silent-empty-list behavior, and its callers are dead code**
The module docstring admits this is "duplicated here rather than imported
since that helper is private to journal.py." Worse, `backfill_five_minute_marks`
and `backfill_next_day_marks` (the only callers of this duplicated helper)
are never invoked anywhere outside `analytics.py` and its own tests — the
+5m/+1day analytics were built but never wired into `runner.py` or
`scripts/`.
*Fix: either export `_all_bars_for_session` from `journal.py` for reuse, or
delete the unused analytics backfill functions until something calls
them.*

**26. `tradebot/config.py:3-7` — `WATCHLIST` (17 symbols) has drifted from `CLAUDE.md`'s description of the project ("watching SPY, QQQ, GOOGL, TSLA, BE, IONQ")**
The watchlist has grown to include NVDA, AAPL, AMD, META, AMZN, MSFT,
COIN, PLTR, SMCI, IWM, USO per `detectors.py`'s calibration comments, but
the top-level project description was never updated.
*Fix: update `CLAUDE.md`'s one-line description (out of scope for this
review to edit directly).*

**27. `web-app/src/hooks/useMarketClock.js:26-29` — ET "now" is derived via a locale-string round-trip through `Date`, not `Intl.DateTimeFormat` parts**
`new Date(now.toLocaleString('en-US', { timeZone: 'America/New_York' }))`
reparses a locale-formatted string with the platform `Date` constructor,
whose non-ISO parsing is implementation-defined, unlike
`useLiveStatus.js`'s own `Intl.DateTimeFormat`-based approach in the same
codebase. No confirmed DST failure today, but it's a known-brittle pattern
for exactly this calculation.
*Fix: reuse the `Intl.DateTimeFormat`-parts approach already present in
`useLiveStatus.js` instead of the locale-string round-trip.*

---

## Silent Failure Inventory (Priority 1)

Every path found with the shape "a failure produces silence instead of
noise," in one place for reference. Items already detailed above are
cross-referenced rather than repeated in full.

| # | Location | Shape | Detail |
|---|----------|-------|--------|
| 1 | `tradebot/runner.py:1164` | Return value never captured | See finding #1 |
| 2 | `tradebot/journal.py:327-333`, `tradebot/marketdata.py:99-101` | Missing file → silent `[]` | See finding #2 |
| 3 | `scripts/fetch_cache.py:56-89` | Fetch failure → print only | See finding #3 |
| 4 | `tradebot/universe.py:70-135`, `vendors/alpaca.py:318-354` | Empty fetch → mass "found nothing = succeeded" | See finding #6 |
| 5 | `tradebot/runner.py` (`_forward_mid`) | **Resolved:** provider failures propagate to the per-selection logger | See finding #12 |
| 6 | `tradebot/runner.py` + `vendors/alpaca.py` (`fetch_option_day_range`) | **Resolved:** API failure propagates/logs; successful no-bars remains `None` | Provider outage no longer masquerades as "no trades" |
| 7 | `tradebot/status_page.py:69-76` | Monitoring layer itself has a blind spot | See finding #16 |
| 8 | `tradebot/runner.py:843-853`, `:1144-1154` | Secondary `except Exception: pass` around the alert-of-a-failure | If the alert itself fails (outbox locked/corrupt), that's fully swallowed with zero logging |
| 9 | `tradebot/journal.py:149-157` (`_add_column_if_missing`) | Overbroad exception match | See finding #17 |
| 10 | `tradebot/runner.py:242-259` (`TelegramHaltChecker.check()`) | Network error → fail-open as "not halted" | See finding #18 |
| 11 | `tradebot/metrics.py:46-57` | Corrupted state silently reset to empty | See finding #15 |
| 12 | `tradebot/analytics.py:56-108` | Dead backfill jobs (never called) | See finding #25 — not "silent failure" so much as "silent non-existence" |
| 13 | `web-app/src/components/SignalDetail.jsx:33-61` | Frontend infers "pending" from absence, no ceiling | See finding #9 |
| 14 | `tradebot/api/app.py:498-517` | API response ambiguous between two very different states | See finding #14 |
| 15 | `web-app/src/api.js:23` | 200 + bad body → silent `null`, looks like "loading" | See finding #19 |
| 16 | `web-app/src/App.jsx:60-61` | Any `/me` failure → treated as signed-out | See finding #20 |
| 17 | `web-app/src/components/Watchlist.jsx:19-21` | Fetch error discarded, looks like "no signals" | See finding #22 |
| 18 | `web-app/src/hooks/useQuotes.js:37-39` | Sustained outage invisible after first successful load | See finding #24 |
| 19 | `tradebot/funnel_events.py:57-65`, `tradebot/client_errors.py:38-51` | Public endpoints intentionally "silently ignore" bad input | **Not a bug** — explicitly documented anti-enumeration discipline; flagged here only so it isn't mistaken for an oversight during any future audit |

**Systemic observation:** the project's only two active "is anything
broken" mechanisms are (a) the heartbeat-staleness deadman's switch in
`telegram_bot/worker.py`, which watches whether the scan *loop* is alive,
and (b) manual, on-demand reads of `metrics.json`/`client_errors`/
`status_page.py`. Neither one checks *data completeness* of a
once-per-session job like `backfill_marks`, `backfill_contract_day_ranges`,
or `refresh_universe` — a scanner that's current on heartbeats can still be
silently writing zero rows in any of these jobs, which is precisely what
happened tonight. The durable fix is a job-level "wrote N of M expected
rows" check with its own alert, not another special case bolted onto the
heartbeat watcher.

---

## Priority 2 — Money-path correctness

Detectors, anchors, and timezone handling are unusually careful: real
`zoneinfo`-based ET conversion throughout (no fixed-offset DST bug found),
explicit lookahead guards in `evaluate_bar`, and anchors correctly computed
once and frozen. The bugs that exist are statistical/alignment bugs rather
than crashes or lookahead violations:

- Finding #7 (sign convention) and #8 (missing news_driven filter) above.
- **Resolved after this review:** `rvol_spike` keys its historical and
  current cumulative-volume lookups by DST-aware RTH wall-clock slot, while
  `relative_strength_break` joins symbol and proxy bars by exact timestamp.
  Missing/duplicate required timestamps fail closed. Regression tests retain
  both former false-signal constructions so positional alignment cannot
  silently return.

## Priority 3 — Data integrity

Covered above (findings #5, #11, #21, plus the following context):
neither `journal.py:connect()` nor `telegram_bot/db.py:connect()` sets
`PRAGMA journal_mode=WAL`/`synchronous` — each individual connection is
crash-atomic under SQLite's default rollback journal, but nothing
coordinates the two separate databases (`journal.db`/`users.db`), which is
the structural reason finding #5's cross-database ordering bug is possible
at all. The outbox's at-least-once delivery (a `SIGKILL` between a
successful Telegram send and `mark_delivered` can cause a duplicate send)
is already explicitly documented and accepted in
`telegram_bot/outbox.py:18-26` — confirmed real, not a new finding, not
worth fixing given the tradeoff already reasoned about in the docstring.

## Priority 4 — Test suite honesty

No `assert True`-shaped or tautological tests were found anywhere in the
suite, and no test was found mocking the exact thing it claims to test.
The suite is generally strong — `test_marketdata.py`'s lookahead-guard
tests, `test_dedup.py`, and `test_db.py`'s deliberate group-vs-private
`chat_id` divergence tests (`test_db.py:30-53`, using `-987654321` vs a
positive `telegram_user_id`) are good examples of tests actively designed
to catch an identity mixup. The gaps that do exist are findings #4, #10,
#13, and #23 above — all four share the same root cause: fixtures/tests
built for the happy path, none constructed to prove the failure path
degrades safely.

## Priority 5 — Frontend correctness

Covered above (findings #9, #19, #20, #22, #24, #27).  `web/src` (the
public marketing site) had no correctness bugs worth flagging — cleanup on
unmount, `matchMedia` listener teardown, and closure handling all looked
correct in `HeroScene.jsx`, `useThrottledInvalidate.js`, and `usePrefs.js`.

## Priority 6 — Everything else

- Finding #25 (duplicated helper + dead analytics backfill jobs).
- Finding #26 (CLAUDE.md watchlist description drift).
- `tradebot/rendering/` (templates.py, fields.py) is clean: pure functions,
  explicit `None`-vs-`0` handling via the `dash()` helper, real
  `zoneinfo`-based ET formatting. No findings.

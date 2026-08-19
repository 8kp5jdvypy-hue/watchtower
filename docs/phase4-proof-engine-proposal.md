# Phase 4 — the proof engine: proposal

Written 2026-08-18, before any code. Covers Part A (public track-record
page) and Part B (weekly recap generator). Both are read-only over
`marks`/`detections`/`outbox` — zero detection-engine changes, and the
live-watch clock running on Ship #2 (P3) is untouched.

Orientation read: `docs/STATE-OF-THE-SYSTEM.md`,
`docs/open-awareness-proposals-2026-08.md` (DECISIONS + Ship log),
`docs/design-elevation-2026-08/consistency-map-rerun.md`.

---

## A finding that changes the design, before anything else

`tradebot.telegram_bot.performance.track_record()` /
`weekly_recap()` — the functions this proposal was told to reuse —
compute their sample over **every HIGH-tier detection**, not just the
ones actually alerted. `_signed_returns()`'s query is `WHERE d.tier = ?`
only; `alerted=1` is checked separately, just for the `total_alerts`
display count, never for which rows feed hit-rate/avg-return.

Confirmed against local data: `track_record()` reports
`sample_size=467` off `total_alerts=12`. The other 455 are real HIGH
detections that were journaled (CLAUDE.md: journal before alert) but
never actually pushed — budget cap, a suppression, or (locally) a
replay run that never sent anything at all.

That's the right population for the **internal** question these
functions were built for ("is our detection edge real," /performance,
the Telegram weekly recap) — but it is exactly wrong for a page whose
whole premise is "every row here is something a real subscriber really
received." Shipping it as-is would mean a skeptic cross-checking
against their own Telegram history finds *more* rows in our stats than
they ever got — a real, if unintentional, integrity gap on the one
page where that can't happen.

**Fix, not a fork:** add `alerted_only: bool = False` to
`_signed_returns()`, threaded through `track_record()` and
`weekly_recap()`. Default `False` preserves every existing caller
(`/performance`, the Telegram weekly recap) byte-for-byte. Part A and
Part B are the only two callers that ever pass `True`. One query, one
sign convention, two populations — the same discipline
`CURRENT_FEED_FILTER_SQL` and the planned `coverage_era` column already
use for the same reason (never let two measured populations silently
answer different questions).

---

## PART A — the public track-record page

### Wireframe, top to bottom

```
┌─────────────────────────────────────────────────────────┐
│ PERCH · TRACK RECORD                                     │
│ Every HIGH-tier alert sent since [first-alert date].     │
│ Graded automatically at close. Nothing is edited or      │
│ removed.                                          [date] │
├─────────────────────────────────────────────────────────┤
│ METHODOLOGY (one paragraph, plain English)                │
│ "Perch scans SPY, QQQ, GOOGL... [watchlist]. When a       │
│ detector fires above a fixed threshold, HIGH-tier alerts  │
│ go out immediately... 'Continuation' means the price kept │
│ moving the alert's direction 30 minutes later, in ATR     │
│ units (a volatility-normalized measure, not raw %)..."    │
├─────────────────────────────────────────────────────────┤
│ [only if coverage_era has >1 value, post-Ship-3/4]        │
│ ⓘ Aug 2026: open-hour coverage began. Alerts before this  │
│   date structurally couldn't fire 09:40–10:45 ET.         │
│   Stats below are shown separately by era, never blended. │
├─────────────────────────────────────────────────────────┤
│ THE HONEST AGGREGATE          [current era, or all-time   │
│                                 if only one era exists]    │
│   Win rate       49.5%  (n=12)         <- or "not enough  │
│   Avg win        +X.XX%                    data to report │
│   Avg loss       -X.XX%                    yet" below     │
│   Sample size    12 alerts                  MIN_HISTORY_  │
│                                              SAMPLE (5)    │
├─────────────────────────────────────────────────────────┤
│ EVERY ALERT                    [Filter: symbol ▾] [tier]  │
│ (filters narrow only — default = unfiltered, every row)   │
│                                                             │
│ SENT (ET)      SYMBOL  DIRECTION  HEADLINE          +30m   │
│ 2026-08-05     MSFT    ↓ BEARISH  "MSFT bar range    -0.3% │
│ 16:05:03                          2.9x ATR..."             │
│ 2026-08-05     QQQ     ↓ BEARISH  "QQQ bar range     +0.1% │
│ 16:00:41                          5.8x ATR..."             │
│ ...                                                        │
│ (append-only order, newest first; wins and losses same     │
│  row style, same weight — no color, tabular figures)       │
├─────────────────────────────────────────────────────────┤
│ FOOTER: link to /performance methodology detail (none      │
│ required — everything above is already self-contained)     │
└─────────────────────────────────────────────────────────┘
```

**Empty/thin state (today's real state — 12 alerts):** no padding, no
"coming soon" placeholder rows. Aggregate block prints
`"Not enough data to report a win rate yet (n=12, need 5+ per slice
shown — see Methodology)."` where relevant slices fall under
`MIN_HISTORY_SAMPLE`, and the honest total ("12 alerts. Every one
graded nightly. Unedited.") stands on its own as the whole pitch,
per the brief.

### Route & serving decision

**Client-side fetch from a new public JSON endpoint on the existing
`api.perchmarkets.com` Flask app, rendered by a new static page in
`web/` (the landing Vite project), deployed through the existing
landing pipeline.** Not a scheduled static-regeneration build.

Reasoning, weighed against the two-Workers architecture in
`DEPLOYMENT.md`:

- `watchtower` (the landing) is a pure static-assets Worker with no
  server logic — Vite output, `wrangler deploy`, Promote-gated. It has
  no mechanism to run a query or render server-side today, and adding
  one would be new infrastructure this proposal doesn't need.
- `api.perchmarkets.com` already runs as a plain Flask/gunicorn process
  on the VPS (not a Worker) and already holds open connections to both
  `journal.db` and `users.db` in the same process
  (`tradebot/api/app.py:create_app()`) — exactly what a query joining
  `detections` and `outbox` needs. Adding one new unauthenticated route
  there is a few lines on infrastructure that's already deployed,
  already monitored, already has a working update path (`git pull` +
  `docker compose up -d --build`, no Promote gate).
- The alternative — bake stats into the HTML at build time, on a
  schedule — needs a new scheduled job, a new deploy credential on
  either the VPS or in CI, and a way to trigger a Cloudflare deploy
  that bypasses (or repeatedly clicks) the landing's manual Promote
  step. That's real new moving parts for a page that just needs to
  show current numbers. The dashboard (`app.perchmarkets.com`) already
  proves the "static shell, live client-side fetch from
  `api.perchmarkets.com`" pattern works in this exact stack — this
  reuses it instead of inventing a second one.

**New route:** `GET /public/track-record` on the existing Flask app.
Unauthenticated, read-only, no cookies read or required — so rather
than extending the credentialed CORS allowlist (`app.frontend_url` +
`DEV_CORS_ORIGIN`, currently scoped to `app.perchmarkets.com` for
cookie-bearing requests), this one route sets
`Access-Control-Allow-Origin: *` directly and skips the credentials
header entirely — it's public data, nothing to restrict, and doing
that inline keeps the credentialed-origins allowlist meaning exactly
one thing (who gets a session cookie honored) instead of also meaning
"who can read public stats."

**New page:** `web/record.html` + `web/src/record-main.jsx`, a second
Vite entry point (`build.rollupOptions.input`, same mechanism
`vite.config.js` already documents for the vendor-chunk split) —
same repo, same deploy, same Promote step as the rest of the landing.
No client-side router needed or added; this is a second static page,
not a route inside the single-page hero experience.

**Named tradeoff, not silently decided:** a client-fetched page has a
brief blank/loading moment before data appears, and a crawler that
doesn't execute JS sees an empty shell. For a trust page meant to be
found and shared, that's a real cost. Proposed for v1 anyway, because
the alternative (build-time prerendering) is a clean *later* addition
(fetch the same JSON at build time, inject as initial HTML, hydrate
client-side same as today) that doesn't require deciding now — and
because "simpler wins" was the explicit brief. Flagging it here so the
choice is visible, not because it's free.

### Exact queries, cited

1. **Aggregate stats** — `track_record(conn, tier="high",
   offset_min=30, alerted_only=True)` (the fix above). Win rate, avg
   return, sample size, significance — all already computed correctly
   once `alerted_only` exists; no new stats math.
2. **Row listing** — new function, `public_alert_history(journal_conn,
   users_conn, tier="high", offset_min=30, limit=None) ->
   list[PublicAlertRow]`, proposed home
   `tradebot/telegram_bot/performance.py` (beside `track_record`, same
   module, same "one source of truth" reasoning). Reuses
   `_signed_returns`'s sign convention exactly (calls it internally
   with `alerted_only=True`) rather than recomputing `up`/`down` sign
   logic a second time. Per row: `symbol`, `headline` (the primary
   detection's own sentence — the exact rationale line
   `render_high_alert` put in the sent message, recovered from
   `detections.headlines`/`kinds`/`primary_kind` by position; not the
   plain-English dashboard card copy, which is a client-side-only
   rendering that was never sent to anyone — see the 2026-08-19
   session), `trend`, `sent_at` (see
   below), `return_pct` (signed, from the existing `_signed_returns`
   shape), `origin` (`watchlist`/`screening`, same field the dashboard
   already surfaces — RADAR badge convention carries over unchanged).
3. **`sent_at` — the verifiable timestamp.** `SELECT
   MIN(delivered_at) FROM outbox WHERE alert_id = ? AND status =
   'delivered'`, batched as one `WHERE alert_id IN (...)` query against
   `users_conn`, not N+1. `alert_id = detections.id` always (confirmed:
   both the ops-channel send in `_commit_then_send` and the subscriber
   fan-out in `delivery.make_subscriber_hook` enqueue under
   `cluster.id` — see `tradebot/telegram_bot/delivery.py:57`).
   `MIN()` because a HIGH alert enqueues one ops-channel row plus one
   row per eligible subscriber, all under the same `alert_id`,
   `UNIQUE(alert_id, chat_id)` — earliest real delivery across any of
   them is "when this was actually sent." A detection with `alerted=1`
   but no `delivered` outbox row yet (in-flight, or a non-Telegram test
   run) is excluded from the page, not shown with a fake or missing
   time — it appears the moment delivery confirms, which is itself the
   append-only property requirement #1 asks for.
4. **Feed/origin scoping.** Aggregate stats use
   `CURRENT_FEED_FILTER_SQL` (`d.data_feed = <latest>`, `origin =
   'watchlist'`) — same population `historical_performance()` already
   uses, so the page's headline number can never quietly diverge from
   what `/performance` or a HIGH alert's own "Similar Setups" line
   claims. The **row table does not apply this filter** — every real
   sent alert appears (watchlist and screening, any feed era), each
   labeled, because hiding a screening-origin or pre-migration row from
   the list itself would be exactly the cherry-picking requirement #5
   forbids. Screening rows get the same RADAR label the dashboard
   already uses. Pre-SIP-migration rows (`data_feed IS NULL` or an
   older value) are visually unremarkable today — there is currently
   only one era in the data — and become the first real use of
   requirement #3's annotation once Ship #3/#4 (P1/P2) lands
   `coverage_era`.

### Requirement #3 — the era annotation, honestly sequenced

`coverage_era` does not exist yet — it ships with P1/P2 (Ship #3/#4),
which is currently blocked behind Ship #2's 2–3-session live-watch
clock (`docs/open-awareness-proposals-2026-08.md` Sequencing table).
**This page can and should ship now** — the annotation is written to
render conditionally (`if len(set(era for era in ...)) > 1: show the
banner`), so today, with one era in the data, it silently doesn't
render. The day P1/P2 lands and `coverage_era` starts getting written,
the banner activates with no further page change — the design is ready
for it, not blocked on it, and not built ahead of data that doesn't
exist yet.

### Design tokens

Landing tokens only (`web/src/index.css`), per the consistency map:
`--step--1…--step-2` for the type scale, `--radius-control` /
card-radius conventions, the existing mono/tabular-figure treatment
already used for prices elsewhere on the landing (`AlertCard.jsx`).
**No color for win/loss** — every other "no gamification" signal in
this brief (no new colors, machined figures, same typography for wins
and losses) points at sign (`+`/`−`) and monospace alignment doing the
work color would otherwise do, not a new green/red pair. The landing's
existing `--red` is deliberately a different *meaning* (a coverage-chip
state) from the app's `--down` per the consistency map's own "same hex,
two documented meanings" note — reusing it for "loss" would be a third
meaning on the same token, exactly the kind of drift that review closed
out. If a design reviewer wants a subtle loss treatment later, that's a
one-line addition on top of this, not a blocker now.

---

## PART B — the weekly recap generator

### Shape

`scripts/generate_weekly_recap.py`, argparse (`--week-start
YYYY-MM-DD`, `--format markdown|html|both`, `--db-path`/`--users-db-path`
overrides, `--out-dir`), following the documented in-container
invocation (`DEPLOYMENT.md`'s "Running scripts/ tools in-container").
Pure rendering logic lives in `tradebot/rendering/recap.py` (new
module, mirrors `templates.py`'s "pure, data in, string out" discipline
— see that file's own module docstring), so it's unit-testable without
a subprocess and importable from wherever posting eventually gets
automated. The script is a thin CLI shell: connect, call
`weekly_recap(..., alerted_only=True)` for aggregates and
`public_alert_history(..., since=week_start, until=week_end)` for rows
(same Part-A function, `since`/`until` params added the same way
`_signed_returns` already supports them), pass both to
`render_recap_markdown()` / `render_recap_html()`, write files.

**Deterministic/idempotent:** every input is a closed `[week_start,
week_end)` query against data that only gets appended to (marks are
written once, at their fixed offset, never edited) — same week, same
`--db-path`, same output, byte for byte. No wall-clock reads inside the
renderer (the "generated" timestamp, if shown at all, is `week_end`,
not `datetime.now()`).

### Sample — real current data, week of 2026-07-27 to 2026-08-03

Generated against local `data/journal.db` with
`weekly_recap(conn, "2026-07-27", "2026-08-03", tier="high",
offset_min=30)` (real numbers; `alerted_only` doesn't yet exist so
this is today's un-fixed population — recomputing after the fix lands
is part of the build, not a proposal-stage requirement):

> `sample_size=14, hit_rate=0.857, avg_return_pct=0.741,
> significance.is_significant=True, total_alerts=1`

Honest caveat carried into the sample below: **locally, zero of the 12
`alerted=1` HIGH detections have a matching `outbox.delivered_at` row**
— every one is `run_replay`/dev-test provenance, never a real Telegram
send. `sent_at` in the mock-up below is therefore illustrative
(detection `ts_utc`, labeled as such), not a real production number.
The build step for Part B includes pulling one real week from the VPS
(mirroring every other ship's "replay locally, verify live" process in
this doc set) before this ships for real.

```markdown
**Perch — week of Jul 27–Aug 2**

1 HIGH-tier alert sent this week.

MSFT — bearish — Jul 30, 16:05 ET
"MSFT bar range 3.04 is 4.4x ATR(14)=0.69; MSFT broke below VWAP
(489.74), 2.58 ATR; MSFT crossed below round number 490.00, 2.87 ATR
past it" — +30m: [not enough same-week marks to grade standalone;
rolled into the running total below, same as every week]

Running total (all HIGH alerts, all-time): n=14, hit rate 85.7%,
avg move +0.74% (+30m). Statistically better than a coin flip
(z=2.67) — still an early sample; see /record for the full,
continuously-updated history.

Sent, graded, unedited. — perchmarkets.com/record
```

HTML fragment: same content, `<table>` for the (currently
single-row) alert list matching the page's row shape 1:1 — same
function produces both, so the two can never drift into disagreement
about what happened this week.

### Voice rules, baked into the templates (not left to the caller)

- Zero emoji — the landing/Telegram convention is "exactly one, the
  tier marker"; a recap has no single alert to anchor one emoji to, so
  it gets none, matching `SCANNER_PLAN.md`'s stated non-goal of
  decoration.
- No superlatives generated from the data ("best week," "crushed it")
  — the template only ever states the number and its z-score
  significance verdict, the same restraint `render_weekly_recap`
  already enforces for the Telegram version.
- A losing week renders through the exact same function as a winning
  one — no `if hit_rate > 0.5` branch that changes structure, only the
  number changes (this is already `render_weekly_recap`'s design;
  Part B's new renderer inherits it, doesn't reinvent it).
- Sub-`MIN_HISTORY_SAMPLE` weeks state that plainly ("not enough
  tracked alerts this week (n=X) for a real hit rate") rather than
  omitting the section — same line `render_weekly_recap` already
  prints, reused verbatim.

---

## Open questions for review (not blocking, but real decisions)

1. **Screening-origin rows in the aggregate.** Proposed: excluded from
   the headline number (matches `historical_performance()`), shown in
   the row list (matches requirement #5). Confirm, or decide the
   dashboard's own convention should change instead — this is the same
   unresolved question `docs/BACKLOG.md`'s "broad_scan honesty
   proposal" already flags as "needs a decision, not a session."
2. **`offset_min` for the headline number.** `track_record`'s existing
   default is 30. The page could instead lead with `+60m` or session
   close (`CLOSE_MARK_OFFSET_MIN`) as the "did it actually work"
   number — a product call, not a data one. Proposed: keep 30m,
   matching every existing surface (`/performance`, the Telegram
   recap, HIGH alert cards) so the number a subscriber has already
   seen a dozen times is the same one on the public page.
3. **CORS wildcard on the new endpoint** — confirmed intentional
   (public, no cookies, no auth) rather than an oversight; flagging
   once for sign-off since every other route in `app.py` is
   credential-scoped.

---

## Build plan once approved

One PR per part, tests included, no deploys without the owner (landing
leg is Promote-only regardless). Order: the `alerted_only` fix +
`public_alert_history()` + tests first (both parts depend on it) →
Part A (API route + page) → Part B (script + templates), each with its
own replay-style verification (real local data first, a real VPS week
before the recap is trusted for actual posting).

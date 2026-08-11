# Cleanup backlog

Quality/simplification/efficiency findings from the code review of the
web-dashboard live-data batch (76 commits, `HEAD..origin/main` as of
2026-08-11: primary_kind/context_summary, per-kind performance stats,
live SIP quotes, trust indicators, and the surrounding frontend polish).
None of these are correctness bugs -- see the review report for the five
that were (fixed separately, one commit each). Not fixed here on
purpose; pick up individually whenever there's room for cleanup, not as
a batch.

## 1. `kind_performance()` duplicates `tier_performance()`'s stats loop

**File:** `tradebot/journal.py:474`

`kind_performance()` re-implements the same ~15-line "group returns by
key, gate on `MIN_HISTORY_SAMPLE`, compute `continuation_rate`/
`avg_return_pct`/`median_return_pct`" logic that already lives in
`tier_performance()` (line 427). A future fix to that math (e.g. the
direction-sign convention) has to be found and applied in both places,
and the next grouping dimension (by hour? by symbol?) will likely copy
it a third time. Worth factoring a shared helper parameterized on the
grouping key once there's a third caller, or proactively if it's cheap.

## 2. `usePolling` reimplements `useApiData`'s fetch/loading/error machinery

**File:** `web-app/src/hooks/usePolling.js:13`

`useApiData.js` guards against out-of-order responses with a boolean
`cancelled` flag; `usePolling.js` re-derives the same data/error/loading
tracking with a different idiom (an object-identity `latestTokenRef`
token) instead of building on `useApiData`. Two independently-maintained
implementations of "fetch and track loading/error" now exist in the
project -- a fix to the race-guard logic in one is easy to forget in the
other.

## 3. Hero's 1s clock tick does a full timezone round-trip every tick

**File:** `web/src/components/Hero.jsx:34`

Every second, `tick()` builds `new Date(now.toLocaleString('en-US',
{timeZone: 'America/New_York'}))` and recomputes `sessionState()`
purely to derive a session label (PRE-MARKET/MARKET OPEN/AFTER
HOURS/MARKET CLOSED) that changes at most 4 times a day. Only the
digital clock display actually needs 1s granularity -- 3,600
unnecessary locale-parses per hour on the marketing site's hero for a
label that's static almost all the time. Compute the session label on a
much coarser interval (or only when the minute changes) and keep the 1s
tick just for the visible clock digits.

## 4. `_context_summary`/`_HEADLINE_CONTEXT_FIELDS` put domain logic in the API layer

**File:** `tradebot/api/app.py:103`

`app.py`'s own module docstring says every endpoint "calls into existing
`tradebot.journal`... functions rather than reimplementing anything" --
i.e. this file is meant to be thin HTTP plumbing. `_context_summary()`
instead adds new domain logic (per-kind field extraction/renaming from
`context_json`) directly in the API layer. A second consumer of the same
summary (a future mobile client, a Telegram-side headline) would have to
duplicate this or import Flask-adjacent code to reuse it. Consider
moving it into `tradebot/journal.py` alongside the other
detection-shaping helpers.

## 5. The same 44px touch-target fix is copy-pasted across three CSS files

**Files:** `web/src/components/AlertCard.css:128`, `AlertReveal.css`,
`MarketCoverage.css`

Each of the three files independently adds `min-height: 44px` for touch
devices, each with its own near-identical rationale comment. A future
change to the touch-target constant (or a decision to make it a shared
`--touch-target` custom property) requires editing all three in
lockstep, and the next new tappable element is likely to get a fourth ad
hoc copy. Worth a shared custom property or a single utility class once
touched again for any other reason.

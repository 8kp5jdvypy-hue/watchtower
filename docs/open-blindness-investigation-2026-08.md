# Blind at the Open — investigation, 2026-08

Why the scanner misses premarket movers and goes quiet during the first 75
minutes of the session — the actual mechanics, traced through code and
journal data, with each cause tagged and cited. Investigation was
read-only; no code was changed. This is the evidence base cited by
`docs/open-awareness-proposals-2026-08.md`.

2026-08-17 · repo @ main `bec981d` · local journal.db (replay-era, sessions
2026-01-02 → 2026-08-10, 22,906 detections) · **live-verified 2026-08-17
against the VPS journal, SIP era, sessions 2026-08-12 → 2026-08-17** ·
`DETECTOR_DATA_FEED=sip` confirmed.

Cause tags: `data-availability` / `deliberate-guard` / `baseline-warmup` /
`detector-design`.

---

## Verdict

**The blindness is structural, not a bug in any one detector.** Premarket
bars are fetched from Alpaca every poll and then discarded by a clock
filter before any detector sees them; five of the seven detectors are
mathematically unable to fire until **10:45 ET** because their ATR baseline
is built only from same-session RTH bars; and the one detector aimed at
overnight moves (`gap`) gets a single 5-minute window at 09:35, scores
against a denominator that makes HIGH nearly unreachable, and routes 94% of
its detections into an hourly digest or the end-of-day summary. A stock
that gapped +7% premarket is then treated, by every baseline in the system,
as calmly trading at a normal — merely elevated — level.

Seven months of journaled detections confirm it: the 09:45–10:45 hour
produced **29 detections total**; a typical later hour produces **~3,700**.

**Live-verified 2026-08-17 — the production journal is starker than the
replay data.** In four live SIP sessions the 09:45–10:45 buckets contain
**zero detections** (not merely few); `gap` does fire at 09:35 (detection
works; routing is what fails); the 25% "extreme gap" guard suppressed two
genuine movers at **+48.8%** and **+70.3%** — of the four HIGH detections
at the open, three were killed by data-integrity guards and one alerted;
the earnings blackout is confirmed unwired (`event_windows` empty); and
`rvol_spike`'s live silence was investigated and **resolved as
non-structural** (cause 8: baseline verified healthy at 0.56–0.86× on a
live session — no qualifying 3× volume day has occurred yet). In practice
`gap` remains the only detector that has produced open-hour signal in
production, but by circumstance, not defect.

---

## Ranked causes

### 1. Premarket data is ingested, then thrown away — `data-availability`

`fetch_intraday_bars()` requests the **full UTC calendar day** of 5-minute
bars — its own docstring says it "covers premarket, RTH, and anything else
the feed reports" (tradebot/vendors/alpaca.py:144–166). But the runner
reads bars only through `session_bars()` (tradebot/runner.py:1285), which
filters to 09:30–16:00 ET via `_is_rth`
(tradebot/marketdata.py:139–141, 259–263). A `premarket_bars()` accessor
exists on both data classes (marketdata.py:88–90, 206–208, 265–269) but
has **zero callers on the detection path** — its only consumers are
mark-backfill (journal.py:347) and analytics (analytics.py:70).

Proof the data exists: the local cache, written from the same fetch, holds
premarket bars — `data/cache/SPY/intraday_2026-08-05.csv` starts at 08:30
ET (12:30Z) and runs to 16:15 ET. That was IEX, whose premarket is thin
(first print: 100 shares); on SIP, coverage starts at 04:00 ET. Nothing
between 04:00 and 09:30 can ever produce a detection.

**Intent:** half-deliberate. The premarket/RTH split is designed (protocol
methods, docstrings), but no detector was ever wired to the premarket
side — scaffolding never connected, not a documented decision to ignore
premarket.

### 2. The ATR warm-up wall: 5 of 7 detectors cannot fire before 10:45 ET — `baseline-warmup`

`atr()` needs **15 bars** (period 14 + 1; tradebot/detectors.py:97–107)
and is computed over **this session's RTH bars only** — never prior-day
bars, which sit unused in the daily cache. Fifteen 5-minute bars means the
earliest possible ATR exists at the 10:45 ET bar close. Every ATR-gated
detector is silent until then: `level_break` (detectors.py:182–183),
`vwap_break` (:323–325), `round_number_break` (:373–375),
`relative_strength_break` (:472–474), and `range_expansion`, which needs
16 bars → 10:50 (:275–277).

Empirically exact: across 22,906 journaled detections over ~7 months, the
earliest detection *ever* recorded for each of those five kinds is the
10:45 bucket. The most active hour of the trading day is covered by two
detectors: `gap` (one shot, 09:35) and `rvol_spike` (from 09:40,
watchlist-only). **Live-verified (VPS checks #1–2):** in four production
SIP sessions the earliest detection per kind is 10:45 (`vwap_break`,
`round_number_break`, `relative_strength_break`) and 10:50 (`level_break`,
`range_expansion`), and the 09:45–10:45 window holds **zero** detections —
the wall is not a replay artifact.

**Intent:** accidental emergent gap. CLAUDE.md mandates ATR *units*, not
an ATR *window*; no doc argues for session-only ATR at the open. Prior-day
ATR is already fetched (runner.py:1306) and used by `gap` — just not by
the other five.

### 3. The gap detector: one 5-minute shot, a harsh denominator, and digest routing — `detector-design`

`gap()` fires only when `len(bars) == 1` (tradebot/detectors.py:419) — the
09:30 bar, knowable at 09:35. Its score is
`|open − prior_close| / (prior_high − prior_low)` (:422–426), so a +7% gap
over a prior day with a 2.5% range scores ≈ 2.8 — **MEDIUM** (HIGH needs
≥ 3.8, detectors.py:38, i.e. a gap nearly 4× the prior day's entire
range). MEDIUM is queued into the hourly digest and released at the next
clock-hour boundary (tradebot/alerts.py:216–218, 239–251) — a 09:35 gap
detection reaches subscribers around **10:00**. In the local journal, gap
clusters split **1,001 LOG / 341 MEDIUM / 85 HIGH**: 70% surface only in
the end-of-day summary, 24% wait for a digest, 6% qualify for an immediate
push.

**Live-verified (VPS check #3):** `gap` DOES fire in production, earliest
at 09:35 — the poll-phase race (loop ticks every ~300s with arbitrary
phase, runner.py:1351–1352) resolves benignly: a tick does observe the
one-bar state in time. The blindness from this cause is therefore entirely
in the scoring and routing above — the detection happens; it just doesn't
reach anyone at the open.

**Intent:** the one-shot design and denominator are deliberate (documented
bug-fix history in detectors.py:403–417); the consequence — the only
premarket-aware signal being structurally unable to page anyone
promptly — looks unexamined.

### 4. After the gap, every baseline says "calm" — `detector-design`

Walk the +7% gapper past 10:45, when detectors finally unlock — none can
see the move that already happened:

- **`level_break`** requires a *fresh* crossing: if the previous bar was
  already beyond `prior_high`/`swing_high`, it declines
  (detectors.py:202–204). A gapped stock opened beyond those levels, so
  the breakout that happened overnight can never fire — the levels read as
  "already broken".
- **`vwap_break`**'s VWAP accumulates from the RTH open (:295–308) — it is
  anchored *at the gapped price*. Deviation from the gap is zero by
  construction.
- **`relative_strength_break`** measures returns "since this session's
  open" (:483–490) — the 7% is excluded from the comparison.
- **`rvol_spike`** is the one detector that can catch gap follow-through
  (earnings volume usually clears 3× cumulative baseline by 09:40) — but
  it compares RTH volume only, needs cached per-symbol history, and in
  practice fired just **39 times before 10:40 in seven months**.

**Intent:** each fresh-crossing check is individually deliberate
(anti-refire). Their composite — "a move completed before 09:30 is
invisible to every baseline" — is the accidental system-level property the
owner observed.

### 5. Off-watchlist gappers arrive late and detection-crippled — `detector-design` / `data-availability`

The watchlist is 17 fixed symbols (tradebot/config.py:3). An arbitrary
earnings gapper only enters through the broad scan — enabled in production
(docker-compose.yml:55) — which runs every **30 minutes** (runner.py:1079),
promotes at most 25 symbols (:1086), and screens *daily* bars: pre-open
there is no "today" bar yet, so the pre-open pass screens yesterday's data
(broad_scan.py:99–123). Once promoted mid-session, the symbol is maximally
handicapped: its bars arrive N-at-once so the `len(bars)==1` state never
exists → **no gap detection**; it has no cached history →
**`rvol_spike` can never fire** (explicit comment, runner.py:1315–1321);
and its gapped levels are "already broken" per cause 4. Only *new* moves
made after promotion are detectable.

**Intent:** the promotion cap and cadence are deliberate ("higher
coverage, NOT higher alert volume", runner.py:1080–1086); the promoted
symbols' inability to express the very signal that got them promoted is
accidental.

### 6. The earnings blackout would suppress the rest — but appears unwired — `deliberate-guard`

By explicit design, the session that prices in an earnings report is a
whole-session **suppress** window and the day before is **downgrade**
(tradebot/events.py:270–293; enforcement runner.py:406–412, 431–437).
Documented rationale: "the technical read isn't trustworthy here";
continuation stats don't transfer to event-driven moves (events.py:1–31).
So even a HIGH-scoring earnings gapper is silenced *by policy* on exactly
the days the owner watches for it. However: `refresh_earnings_events()`
(events.py:316) and `refresh_edgar_events()` (:241) have **no callers
anywhere in the codebase** outside tests. **Live-verified (VPS check
#4):** the production `event_windows` table is empty — the blackout
machinery is confirmed inert.

**Intent:** the suppression is deliberate and documented; the
non-ingestion is accidental. Interaction note for the fix conversation:
wiring up earnings ingestion *without* revisiting severity would make
earnings-day silence worse, on purpose. *(Resolved by the owner's
2026-08-17 decision in the proposals doc: earnings = context, never
suppression.)*

### 7. Extreme movers (>25%) are guarded out entirely — `deliberate-guard`

The data-integrity guard rejects any alert whose quote is more than **25%**
away from prior close (tradebot/guard.py:30, 127–131,
`extreme_prior_close_gap`). Built as a bad-data tripwire, it also fires on
genuine +30% biotech/squeeze moves — every alert on such a symbol is
suppressed all session as "data integrity".

**Live-verified (VPS check #5), and worse than originally ranked:** in the
first live week alone, two real movers — **+48.8%** ($0.87→$1.295) and
**+70.3%** ($2.93→$4.99) — had their alerts suppressed by this guard.
These are precisely the "already moved" stocks this investigation is
about: the system detected them, then discarded its own detections as
implausible data. Not a minor edge case; an active weekly silencer of the
biggest movers the broad scan surfaces.

**Intent:** deliberate guard, accidental reach — with live confirmation
that the reach includes exactly the events the product exists to catch.

### 8. rvol_spike has never fired in production — `data-availability` / `baseline-warmup` — RESOLVED non-structural

Across all live SIP sessions, `rvol_spike` does not appear in the per-kind
detection list at all — zero firings at any time of day, versus regular
firings in replay. Three hypotheses were chased and resolved the same day:

1. **Empty cache intersection → no baseline: falsified.** VPS cache
   counts: all 17 watchlist symbols hold 23–24 `intraday_*.csv` files; the
   aligned-date intersection (runner.py:158–163) is **22 sessions**; no
   minimum-history threshold exists anywhere.
2. **IEX-era baseline → sessions start above 3× → the fresh-crossing guard
   (detectors.py:250–252) suppresses forever: falsified.** The VPS volume
   audit showed the July history was backfilled 2026-08-12 01:19 at
   healthy SIP scale (SPY 34–70M/day, no era cliff). What the audit did
   find: **two runt cache files** — 2026-08-11 (~1.0M, written 01:35
   mid-backfill) and 2026-08-12 (~1.03M, written 22:25 mid-session), each
   30–40× under real volume. Only 08-11 survives the intersection. The
   arithmetic clears it: one runt among 22 sessions drags the mean
   baseline ~4.4% *low*, which *inflates* the live ratio to ~1.05× — a
   distortion toward **more** firing, not less.
3. **Non-structural (confirmed):** the in-container ratio replay (SPY,
   session 2026-08-13, 20 history sessions, runt included) printed ratios
   of **0.56–0.86×** across all 78 bars: the baseline is the correct
   scale, the detector saw exactly what it should have, and the session
   never approached the 3× threshold. Combined with the base rate
   (replay-era rvol fired in only 8 of the last 24 sessions, so zero-in-4
   is ~20% likely by chance), the live silence is quietness, not
   breakage — rvol will fire on the next genuine 3× volume day.

**Status:** demoted from a cause to an observation. Two residual cleanup
items survive it (both in the proposals doc, P5): the runt cache files
(08-11, 08-12), and the fact that nothing validates a cache file's
plausibility (volume scale, bar count) before averaging it into every
future session's baseline — one bad write on an incident night silently
becomes part of the measuring stick.

---

## The session clock

What can mathematically fire, minute by minute, for a watchlist symbol on
a normal day. There is no explicit market-open gate or warm-up timer
anywhere — the runner's loop starts at process boot and only checks
`loop_start >= close_ts` (runner.py:1259–1262). All gating below is
emergent from data shape.

| Time (ET) | State |
|---|---|
| 04:00–09:29 | **Zero detection capability.** Premarket bars stream from the vendor and are discarded by `_is_rth`. `session_bars()` returns empty → `continue` (runner.py:1285–1287). Broad scan runs pre-open but screens yesterday's daily bars. |
| 09:35 | **`gap` only.** Its single eligible evaluation (`len(bars)==1`). Anchors are built here and frozen: opening range = first bar only (runner.py:1310–1322, detectors.py:63–64). |
| 09:40 | **+ `rvol_spike`** — needs 2 bars plus a cached cumulative-volume baseline (detectors.py:237–243); watchlist symbols only. `gap` is now permanently out for the day. |
| 09:40–10:44 | **The silent hour.** Only `rvol_spike` can fire (39 firings before 10:40 in 7 replay months); in production it hasn't fired yet — verified healthy but waiting on a genuine 3× volume day (cause 8) — so live, this window has been completely empty (VPS check #1: zero detections, four sessions). |
| 10:45 | **+ `level_break`, `vwap_break`, `round_number_break`, `relative_strength_break`** — 15 session bars now exist; ATR(14) becomes computable. |
| 10:50 | **+ `range_expansion`** (16 bars). The full detector suite is finally online — 80 minutes into the session. |

## Detector baselines and warm-up

| Detector | Baseline it compares against | Min RTH bars | Earliest fire (ET) | At 09:35? |
|---|---|---|---|---|
| `gap` | Prior day close & range, from daily bars (detectors.py:422–426) | exactly 1 | 09:35 | CAN fire (only then) |
| `rvol_spike` | Avg cumulative RTH volume by bar index, from cached history sessions (detectors.py:140–148, 237–243) | 2 + history | 09:40 | locked |
| `level_break` | Prior high/low, opening range, 20-day swing; threshold in session-ATR units (:182–195) | 15 | 10:45 | locked |
| `vwap_break` | Session VWAP (accumulates from 09:30) ± session ATR (:311–335) | 15 | 10:45 | locked |
| `round_number_break` | Nearest round level ± session ATR (:364–389) | 15 | 10:45 | locked |
| `relative_strength_break` | Return since 09:30 open vs SPY's, normalized by session ATR (:472–495) | 15 | 10:45 | locked |
| `range_expansion` | Last bar's range vs trailing session ATR of prior bars (:266–284) | 16 | 10:50 | locked |

The "Earliest fire" column is confirmed empirically: min detection time
per kind over all 22,906 journaled detections matches this table exactly,
and the live VPS journal replicates it (10:45/10:50).

## Evidence: detections by time of day

Journaled detections per 15-minute ET bucket — all tiers, 145 sessions,
2026-01-02 → 2026-08-10, local journal.db (replay-era):

| ET bucket | count | high / medium / log |
|---|---|---|
| 09:30 | 1,439 | 89 / 349 / 1,001 |
| 09:45 | 17 | 2 / 15 / 0 |
| 10:00 | 8 | 1 / 7 / 0 |
| 10:15 | 2 | 0 / 2 / 0 |
| 10:30 | 2 | 0 / 2 / 0 |
| 10:45 | 1,345 | 5 / 53 / 1,287 |
| 11:00 | 1,267 | 4 / 80 / 1,183 |
| 11:15 | 1,104 | 4 / 86 / 1,014 |
| 11:30 | 1,076 | 7 / 83 / 986 |
| 11:45 | 980 | 11 / 88 / 881 |
| 12:00 | 996 | 7 / 95 / 894 |
| 12:15 | 943 | 5 / 111 / 827 |
| 12:30 | 920 | 16 / 157 / 747 |
| 12:45 | 915 | 20 / 161 / 734 |
| 13:00 | 866 | 18 / 178 / 670 |
| 13:15 | 877 | 24 / 188 / 665 |
| 13:30 | 880 | 48 / 202 / 630 |
| 13:45 | 800 | 7 / 174 / 619 |
| 14:00 | 932 | 49 / 257 / 626 |
| 14:15 | 875 | 21 / 238 / 616 |
| 14:30 | 940 | 31 / 243 / 666 |
| 14:45 | 738 | 9 / 178 / 551 |
| 15:00 | 1,048 | 47 / 369 / 632 |
| 15:15 | 870 | 42 / 226 / 602 |
| 15:30 | 973 | 14 / 297 / 662 |
| 15:45 | 1,454 | 104 / 693 / 657 |
| 16:00 | 639 | 23 / 367 / 249 |

The 09:30 bucket looks healthy but is a monoculture: of the 1,466
detections before 10:40, **1,427 are `gap`** (mostly one per symbol per
day at 09:35, 69% sub-threshold LOG) and 39 are `rvol_spike`. Market
volume and volatility peak in the first half hour; detections should
too — instead 09:45–10:45 runs at ~1% of a midday hour. **Live
confirmation:** the production journal's four SIP sessions show the same
shape with the silent hour fully empty — 15 detections at 09:30 (all
`gap`-led), nothing at all until 10:45, then 10–43 per bucket for the
rest of the day.

## Live verification results (2026-08-17, sessions 2026-08-12 → 2026-08-17)

| Check | Result | vs. local diagnosis |
|---|---|---|
| 1 · Histogram | 09:30 bucket: 15 detections (4 HIGH, 1 alerted). 09:45–10:45: **zero rows**. Midday buckets 10–43 each. | Matches — starker: the silent hour is literally empty live |
| 2 · Earliest per kind | gap 09:35 · vwap/round/rel-strength 10:45 · level_break & range_expansion 10:50 · **rvol_spike absent entirely** | Matches the wall; rvol silence investigated → resolved non-structural (cause 8) |
| 3 · Gap fires live? | Yes — 10 (08-12), 2 (08-13), 4 (08-17) | Resolves the poll-phase race benignly |
| 4 · event_windows | Empty | Confirms the earnings blackout is unwired (cause 6) |
| 5 · Open-hour suppressions | 15 pre-10:45 detections: 12 sent-or-log, 2 `extreme_prior_close_gap` (+48.8%: $0.87→$1.295; +70.3%: $2.93→$4.99), 1 `last_outside_session_range`. Staleness log hits: **0**. `DETECTOR_DATA_FEED=sip`. | Guard reach confirmed (cause 7); staleness ruled out as a contributor; 3 of the open's 4 HIGH detections were guard-killed |

The three suppressed symbols trade at $0.87–$4.99 — broad-scan promotions,
not watchlist names: the guard and quote-jitter checks bite hardest on
exactly the thin, fast movers Stage 1 exists to surface.

## Gate inventory

Every check between a bar arriving and an alert leaving, with thresholds.
None is an explicit "wait for the open" gate; the open-blindness is the sum
of the starred rows.

| Gate | Where | Threshold | Effect at the open |
|---|---|---|---|
| ★ RTH-only bar filter | marketdata.py:139–141, 259–263 | 09:30 ≤ t < 16:00 ET | Discards all premarket bars before detectors run |
| ★ ATR needs 15 bars | detectors.py:97–107 | period 14 + 1 | Locks 5 of 7 detectors until 10:45/10:50 |
| ★ rvol baseline coverage | detectors.py:241–243; runner.py:1315–1321 | history for bar idx & idx−1 | Watchlist-only; promoted symbols never fire it |
| Zero-volume (halt) skip | runner.py:121–125, 337–340 | volume == 0 | Benign; skips thin premature bars |
| Bar-gap skip | runner.py:128–145, 341–344 | > 5 min between opens | Skips the bar after a dropped one |
| Bar staleness | runner.py:97, 117–118, 1288–1300 | > 90 s after bar close | Suppresses the whole symbol that tick; live check #5: zero hits |
| Quote staleness (guard) | guard.py:29, 121–124 | > 60 s | Blocks send at alert time |
| Spread guard | guard.py:28, 104–105 | > 5% of mid | Can bite in thin 09:30 quotes |
| ★ Extreme-gap guard | guard.py:30, 127–131 | > 25% vs prior close | Silences genuine >25% movers all day (live-confirmed, cause 7) |
| ★ Earnings blackout | events.py:270–293; runner.py:431–437 | whole session, HIGH only | Designed to silence earnings days — confirmed unwired (cause 6) |
| Budget: HIGH cap / cooldown | alerts.py:179–180 | 8/day; 45 min per (symbol, kind) | Later-day effect, not open-specific |
| ★ MEDIUM digest cadence | alerts.py:239–251 | next clock-hour boundary | A 09:35 MEDIUM gap lands ~10:00 |
| LOG tier routing | alerts.py:212–214 | end-of-day only | 70% of gap detections end here |

## Notes

Also noted, non-blocking: the bare-metal (Mac) deployment's watchdog only
starts the runner inside market hours (scripts/watchdog.sh,
`is_market_open`), so local live sessions can start after 09:35 and skip
the gap window entirely; the VPS Docker deployment runs 24/7
(docker-compose.yml:50–59) and is unaffected. The gap-at-09:35 race is
resolved by live evidence (check #3), and the 90-second staleness gate is
ruled out as a contributor (check #5: zero "data is stale" log hits).
Cause 8 was chased through three hypotheses and resolved the same day:
empty cache intersection (falsified by cache counts), IEX-era baseline
(falsified by the volume audit), and finally confirmed non-structural by
the in-container ratio replay — every claim in this report is either
code-cited or live-verified. Investigation was read-only; no code was
changed.

**Follow-up:** the fix designs, replay evidence, and the owner's binding
decisions live in `docs/open-awareness-proposals-2026-08.md`.

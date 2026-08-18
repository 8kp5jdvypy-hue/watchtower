# Open-awareness proposals — 2026-08

Six proposals to eliminate the structural open-blindness documented in the
2026-08-17 investigation ("Blind at the Open"), which traced why the system
is blind to premarket moves and quiet 09:40–10:45 ET. **Binding philosophy:
precision stays paramount — we eliminate STRUCTURAL blindness, not
standards.** Builds ship ONE at a time, replay-validated, watched 2–3 live
sessions before the next. Proposals only; no engine code was changed in
producing this document.

All replay numbers below were produced 2026-08-17 by read-only simulation
against the local `data/cache` (145 sessions, 17 watchlist symbols, Jan–Aug
2026, IEX-era) and `data/journal.db` (22,906 detections, 118,824 graded
marks, 97% coverage). Simulations mirror each detector's exact predicate
using the project's own `atr`/`vwap`/scoring functions. The production build
of each proposal re-runs its validation through the real `run_replay`
pipeline before shipping. SIP-era revalidation is in every ship checklist.

Investigation context (live-verified 2026-08-17 on the VPS): the 09:45–10:45
window contains zero detections in production; earliest fire per ATR-gated
kind is 10:45/10:50; `gap` fires at 09:35 but 70% of gap detections are
LOG-tier and pushes are structurally unreachable; the 25% guard suppressed
+48.8% and +70.3% movers in one week; `event_windows` is empty (earnings
blackout unwired); rvol_spike's live silence was investigated and resolved
non-structural (healthy baseline, no 3× volume day yet).

---

## DECISIONS — 2026-08-17, owner

The three open decisions are made. Where this section differs from the
proposal text below, **this section binds**.

1. **Sequencing: APPROVED as recommended.** Hygiene first — build order is
   the Sequencing table exactly as written (P5b+5c → P3 → P2 → P1 → P4 →
   P6 phase 1 → P5a).
2. **P3 spread guard: OPTION B.** Extreme-mover carve-out up to a **15%**
   spread, with the spread printed on the card ("spread 11% — wide
   market"); silent above 15%. The trade path's honesty is unchanged —
   `select_contract()`'s NO TRADE labeling stands. Option A is rejected.
3. **P4 severity: AMENDED — earnings are context AND signal, never
   suppression** (owner's binding ruling; the proposal's original
   `downgrade` recommendation muted the signal and is superseded).
   - Earnings day **and** the day before: severity `context` — alerts
     deliver **on time, at full tier**, with earnings named on the card
     ("⚠ Earnings day — event-driven move; excluded from Similar Setups
     stats").
   - Macro release windows (FOMC/CPI/EIA minutes) keep `suppress` —
     genuinely broken tape, not an earnings question.
   - Stats purity is preserved by the existing `news_driven` exclusion,
     which was always the only legitimate reason for suppression.
   - Ingestion wiring proceeds exactly as proposed (P4's wire section).

---

## Proposal 1 — Warm-start baselines (causes 2 & 4) — READY TO BUILD

Give the five ATR-locked detectors a valid ATR-equivalent from the second
bar of the session (09:40 close) instead of 10:45, and make a gapped
stock's continuation detectable without weakening any fresh-crossing rule.

### Rejected by replay — iteration 1

The obvious design — roll ATR14 across the session boundary (prior session
tail + today) — was simulated first and FAILED: +3.09 HIGH/day and +10.87
MEDIUM/day added load in one hour, 48% +60m continuation (below the 50%
midday control). Mechanism: the prior afternoon's quiet tail ATR
understates normal opening volatility, so the open's routine violence reads
as signal ("bar range 12.5× ATR(14)=0.18"). Precision-paramount fails this
outright.

### Design (iteration 2): time-of-day TR profile

The ATR-equivalent for early bar k is the **historical mean session-to-date
true range at that same bar index**, averaged over the trailing 20 cached
sessions — the per-bar-index profile idiom `rvol_spike` already uses for
volume (`avg_cum_volume_by_bar`).

- `DailyAnchors` gains `expected_atr_by_bar: Mapping[int, float]`, built in
  `build_anchors()` from the `historical_session_bars` it already receives —
  no new plumbing, pure-function discipline intact.
- Each ATR-gated detector uses `atr(bars)` when available (bar ≥ 14 —
  current behavior, unchanged) and `expected_atr_by_bar[k]` before that.
  `range_expansion` compares against the profile at k−1. Thresholds
  (0.5 / 2.0 / 1.0 ATR-units) unchanged.
- **Fresh-crossing semantics unchanged** — the anti-refire rules are
  correct. Gap continuation is covered by what the profile unlocks:
  opening-range breaks at 09:40–09:45 ARE gap continuation (visible
  repeatedly in samples), plus VWAP/round-number/relative-strength breaks
  in the window. Additionally, `build_anchors()` records which levels the
  session **opened beyond** (`opened_beyond: tuple[str, ...]`) — journaled
  context and card copy ("opened above prior high, 20-day high"), no
  detector semantics change.
- VWAP stays session-anchored (definitionally correct).

### Replay evidence — 139 sessions, window 09:40–10:40 (currently zero detections live)

| Design | HIGH/day added | MEDIUM/day added | HIGH +60m cont. | median +60m | Verdict |
|---|---|---|---|---|---|
| Iteration 1 — cross-boundary ATR | +3.09 | +10.87 | 48% | −0.24 ATR | **rejected** |
| Iteration 2 — time-of-day TR profile | +0.37 | +3.06 | 51% | +0.30 ATR | **proposed** |
| Control: current-rule breaks 10:45–11:45 | — | — | 50% | −0.01 ATR | baseline |

5,169 window clusters over 139 sessions; 2,669 net-new signals (vs 4,536
that current rules eventually catch later the same session — those now
arrive up to an hour earlier). Max HIGH added on any single day: 4.
**Flag:** USO is 61% of window HIGHs (31/51) — its IEX-thin history
pollutes its profile (all 16 sub-60-bar local cache files are USO, 38–58
bars traded). Ex-USO added HIGH load is 0.14/day at n=20 — too small to
grade, which is the honest reading: for most symbols this mostly adds
earlier MEDIUM/LOG coverage, not a flood of pushes.

### Preconditions & guardrails

- Profiles are feed-scoped: build only from SIP-era files (same discipline
  as the rvol baseline). The VPS has ~5 SIP sessions — **backfill 20 SIP
  sessions via fetch_cache first** (one in-container run; Alpaca serves
  historical SIP). Ship with a minimum-profile floor of 8 sessions; below
  it, current behavior (silent until 10:45) — never a thin-profile guess.
- Requires Proposal 5's plausibility floor first, so runt files can never
  join a profile.
- Full tier recalibration before ship, per the documented ritual
  (detectors.py:15–37), with per-symbol HIGH share as a tracked metric;
  USO's share feeds the existing thin-symbol policy decision in BACKLOG.

### Acceptance criteria

Replay across all cached SIP sessions shows: added HIGH ≤ 0.5/day (ex
thin-symbol policy exclusions); window +60m continuation within 2 points of
the midday control; no symbol above 20% of window HIGHs post-calibration;
zero detections timestamped before 09:40. Build size: S–M.

---

## Proposal 2 — Gap-and-go (causes 1 & 3) — READY TO BUILD

A significant premarket mover gets a confirmation-gated push at 09:40 with
premarket context on the card; the existing gap detector's miscalibrated
score is fixed. The uncalled `premarket_bars()` finally gets its
detection-path caller.

### Significance threshold

A gap is a candidate when `|open − prior_close| ≥ 0.75 × daily ATR14`
(prior 20 daily bars — already fetched for anchors), **strong** at ≥ 1.0.
ATR units per CLAUDE.md, replacing the prior-day-range denominator. Replay:
0.74 events/day in the 0.75–1.0 band, 0.76/day at ≥ 1.0 across 17 symbols.

### Confirmation predicate — one shot, 09:40 close

Using bars 0–1 only:
- **held** — the 09:40 close retains ≥ 50% of the gap (sign-adjusted vs
  prior close);
- **real volume** — cumulative volume through bar 1 ≥ 1.5× the 20-session
  average at that bar index (rvol baseline machinery, reused).

Both true → `gap_confirmed` fires. Single-shot for precision; a gap that
confirms late is covered by Proposal 1's continuation detectors instead.

### Replay evidence — 124 sessions: the gate separates continuation

| Gap size (daily-ATR) | events/day | confirm rate | confirmed → close cont. | unconfirmed → close cont. |
|---|---|---|---|---|
| 0.5–0.75 (below threshold) | 2.13 | 24% | 51% (n=63) | 49% (n=201) |
| 0.75–1.0 | 0.74 | 43% | **62% (n=40)** | **40%, median −0.27 dATR (n=52)** |
| 1.0–1.5 | 0.43 | 66% | 57% +60m (n=35) | 61% (n=18 — noisy) |
| 1.5–2.5 | 0.19 | 79% | **63% (n=19)** | 40% (n=5) |
| 2.5+ | 0.14 | 100% | 59% (n=17) | — |

Below the 0.75 threshold the gate has no edge (51% vs 49%) — correctly
excluded. Pooled confirmed ≥ 0.75: **57% close-continuation (n=111)** — the
card's honest base-rate line. Would-have-fired cards include exactly the
shape the owner watched for: PLTR 2026-08-04 **+15.3%** (3.3 dATR, held,
6.6× volume → closed a further +2.0 dATR), MSFT +12.1% and AMZN +12.3% on
their earnings days (currently scored ~2 → digest), SMCI −26.9%. One honest
counterexample ships in the golden set: AAPL −8.5%, confirmed, reversed
−0.94 dATR.

### Scoring, routing, dedup

- **Gap re-score:** `gap`'s score becomes gap size in daily-ATR units
  (audit of the current denominator on the same 450 events: median score
  0.86, only 2% reach HIGH — the 3.8×-prior-range bar is unreachable for
  real gaps). Tier thresholds unchanged.
- **Routing:** the raw 09:35 `gap` detection never pushes — journal +
  digest only, at any tier. **All gap pushes go through `gap_confirmed`**
  at 09:40: confirmed AND ≥ 1.0 dATR → HIGH push (+0.57/day at backtest
  rates; knob to 1.25 → ~0.35/day); confirmed 0.75–1.0 → MEDIUM digest.
  Monster gaps lose nothing by waiting one bar — the 2.5+ bucket confirmed
  100% of the time.
- **Dedup:** `gap_confirmed` sets `related_detection_id` to its 09:35
  parent; the digest renderer skips a gap line whose confirmed child
  pushed. Budget cooldown keys the two kinds together per symbol so nothing
  double-pushes.
- **Premarket context, not gate:** `premarket_bars()` feeds the card
  (premarket cumulative volume, premarket high/low). Context only until 20
  SIP premarket sessions exist — replay proves IEX premarket is unusable as
  a gate (volumes in the low thousands; BE gapped +22.7% with zero IEX
  premarket prints). Gate promotion is a separate, evidence-gated flip
  later.
- **Unconfirmed resolution:** a candidate that fails the gate is journaled
  with `context.confirmation="failed: held 31%"` and rides the digest with
  that copy — visible, never silent.

### The card — three questions in order

```
⚡ PLTR gapped +15.3% overnight — 3.3× its daily range
WHAT: opened 168.10 vs prior close 145.80 · opened above prior high and 20-day high
REAL? confirmed 09:40 — holding 87% of the gap on 6.6× normal opening volume
      premarket: 1.3M shares traded 04:00–09:30, premarket high 168.40
BASE RATE: confirmed gaps ≥1 ATR continued into the close 57% of the time (n=111, backtest)
```

The base-rate line is labeled `backtest` until Proposal 6's live-era cohort
reaches n≥50, then switches source with the label — never an unlabeled
blend.

### Acceptance criteria

Golden-set replay (the 12 confirmed ≥1.0 dATR events + AAPL counterexample)
renders correct cards end-to-end; added HIGH ≤ 0.6/day over full-cache
replay; zero double-pushes in replay; confirmed-cohort close-continuation
≥ 55% reproduced. Build size: M.

---

## Proposal 3 — Extreme-mover persistence check (cause 7) — SHIPPED-PENDING-ACCEPTANCE

Replace the 25% hard silence (guard.py:127–131) with evidence-of-reality:
past 25%, the alert goes out if the move **persists across two consecutive
bars on real volume** — the data-integrity purpose survives, the week's
biggest movers stop being discarded.

### Predicate

When `|quote.last − prior_close| / prior_close > 25%`: require the last two
consecutive RTH bars to each have (a) non-zero volume, (b) closes beyond
the 25% line on the same side, and (c) closes within 10% of each other (a
real level, not a print error). Pass → alert proceeds, cluster tagged
`extreme_mover`, card prefixed **"EXTREME MOVER +48.8% vs prior close —
verified across 2 bars, 412k shares"**. Fail → suppress exactly as today,
plus the existing ERROR. All other guards (crossed quote, high<low,
session-range) unchanged.

### Owner decision: the spread guard

> **DECIDED 2026-08-17: Option B** — see the DECISIONS section.

Thin extreme movers often carry >5% spreads, so the spread guard
(guard.py:104–105) would still silence some.
**Option A** — keep it (honest limitation; some verified movers still
suppressed). **Option B (recommended)** — extreme-mover carve-out up to a
15% spread, spread printed on the card, trade path left to
`select_contract()`'s existing NO TRADE honesty.

### Validation — runs on the VPS (the movers' bars exist only there)

```bash
cd /opt/perch
docker compose exec -T runner python3 - <<'PY'
import sys, glob; sys.path.insert(0, "/app")
from tradebot.marketdata import _read_bars, _is_rth
# the two suppressed movers: find their symbols from the journal, then:
for path in glob.glob("/app/data/cache/*/intraday_2026-08-1*.csv"):
    sym = path.split("/")[4]
    bars = [b for b in _read_bars(__import__("pathlib").Path(path), sym) if _is_rth(b)]
    if not bars: continue
    # print bar-by-bar closes+volume around any >25% excursion vs first close
    base = bars[0].open
    hits = [(i,b) for i,b in enumerate(bars) if abs(b.close-base)/base > 0.25]
    if hits:
        i0 = max(0, hits[0][0]-1)
        print(f"== {sym} {path.split('_')[-1][:10]}")
        for i in range(i0, min(i0+6, len(bars))):
            b = bars[i]
            print(f"  bar{i:2d} {b.ts:%H:%M}Z close={b.close:<8g} vol={b.volume:,}")
PY
```

Expected outcome, stated in advance: both the +48.8% ($0.87→$1.295) and
+70.3% ($2.93→$4.99) movers show multi-bar persistence on real volume →
both would have produced tagged EXTREME MOVER alerts instead of
`data_integrity_failed` rows. If either shows single-print behavior, the
guard was right and this proposal's evidence section says so.

### Acceptance criteria

VPS replay of both movers produces the tagged card (or documents why not);
synthetic bad-print fixtures (single-bar spike, zero-volume spike, crossed
quotes) all still suppress; no change to sub-25% behavior. Build size: S.

---

## Proposal 4 — Wire the earnings blackout, re-severitied (cause 6) — OWNER DECISION

> **DECIDED 2026-08-17, AMENDED:** earnings day and day-before are
> `context` (full-tier, on-time, earnings named on the card) — not
> `downgrade` as recommended below. Macro windows keep `suppress`. See the
> DECISIONS section, which binds over this section's original text.

Recommendation as originally written: **wire it — do not remove it — but
flip the earnings-day severity from `suppress` to `downgrade`.**

- **Wire:** call `refresh_earnings_events()` (built, tested, currently
  uncalled) once per session in `run_live`'s open block, for the watchlist;
  failure is a loud ERROR + heartbeat line, never session-blocking. The
  pre-open card then actually lists earnings, and `has_earnings_before()`
  stops answering from an empty table.
- **Re-severity:** earnings day `suppress → downgrade`; day-before
  `downgrade → context`. Macro windows (FOMC/CPI/EIA release minutes) stay
  `suppress` — genuine untrustworthy-tape moments.
- **Why not remove:** the framework is the right home for macro suppression
  and the news-tagging the track record depends on. **Why not keep
  suppress:** Proposal 2 exists to alert on earnings gappers with context;
  whole-session silence on exactly those days contradicts the product's
  direction — and the documented rationale ("continuation stats don't
  transfer") is already preserved by the `news_driven` tag, which keeps
  event-driven detections out of Similar Setups. Stats purity survives
  without silence; the card gains an explicit "earnings day" line.
- **Alternative if purity is preferred:** keep `suppress` but exempt the
  `gap_confirmed` kind (definitionally news-aware). Weaker: it silences
  P1's continuation signals on the most-watched sessions of the quarter.

### Validation & acceptance

Seed real August earnings dates into a scratch DB and replay 2026-07-30/31
(MSFT +12.1%, AMZN +12.3%, AAPL −8.5% are in the cache): cards must show
the earnings tag, HIGHs route as MEDIUM (downgrade), nothing suppressed
outright; pre-open card lists the events; ingestion failure paths log
loudly. Build size: S.

---

## Proposal 5 — Off-watchlist reach + cache hygiene (cause 5 + hygiene) — 5b/5c SHIPPED 2026-08-17, 5a screening partly deferred

### 5a — Pre-open gap promotion (feasible now)

At 09:00 and 09:25 ET, one bulk `fetch_latest_quotes()` pass over the
active universe (~10 chunked calls) against prior closes from the daily
cache the broad scan already holds: `|Δ| ≥ 5%`, price ≥ $1, average-volume
floor → promote up to 10 symbols **before the open** (inside the existing
25 cap). Their `LiveMarketData` then exists from bar 0, so
`gap`/`gap_confirmed` can fire for them at 09:35–09:40 — needing only daily
bars, which promoted symbols have. Honest limits, stated: no rvol and no
P1 profile for them (no cached history — unchanged); coverage is the gap
path plus post-10:45 detectors. Also: first intraday broad scan moves to
09:45, then every 15 min until 10:30 (API cost: ~9 bulk requests/pass,
measured ~20s).

**Deferred:** premarket intraday bars for the whole universe (SIP premarket
bulk fetching + premarket profiles) — a research project; revisit after
P2's SIP premarket data matures on the watchlist.

### 5b — Runt purge (ops, one session)

Delete every watchlist `intraday_2026-08-11/12.csv` failing the floor
below, then refetch those dates in-container with
`DETECTOR_DATA_FEED=sip` (Alpaca serves the history; same
`fetch_intraday_bars` + `write_bars_csv` pair the close-time cacher uses).
Same run backfills 20 SIP sessions for P1/P2 baselines.

### 5c — Cache plausibility floor (calibrated)

A file is rejected — from baseline-building AND at close-time write — when
**RTH volume < 20% of the symbol's trailing 20-session median**, or
**RTH bar count < 50% of the calendar-expected count for that session**
(calendar-aware so early closes never false-trip). Every rejection: ERROR
log + heartbeat `data_gaps` line + metrics counter. Never silent.

**Floor calibration — all 2,448 local cache files:** volume floor trips 4
of 2,448 (0.16%) — all genuinely degenerate IEX-thin USO days that SHOULD
leave baselines. It catches the VPS runts (~2.5% of median volume) with an
8× margin. Bar-count floor set at 50% (not 75%) because all 16 sub-60-bar
local files are real IEX-thin USO sessions (38–58 bars traded, not early
closes) — on SIP this thinness disappears; tighten later with evidence.
This finding also explains P1's USO concentration.

### Acceptance criteria

Floor replay over the full local cache rejects only the 4 known
degenerates; runt refetch verified by re-running the volume audit
(08-11/08-12 land at normal scale); pre-open promotion validated on the VPS
by journal origin='screening' detections at 09:35–09:40. Build sizes: 5a M,
5b ops-only, 5c S.

---

## Proposal 6 — Outcome-informed alerts — PHASE 1 SMALL, phase 2 research (deferred)

The track record should make alerts smarter, not just prove honesty. Raw
material is real: 97% of 22,906 detections carry graded marks (118,824
rows), and cohort spreads are large:

| Cohort (kind · trend · tier) | n | +60m continuation |
|---|---|---|
| range_expansion · up · high | 103 | **63%** |
| level_break · up · high | 60 | 57% |
| gap · up · medium | 187 | 55% |
| level_break · down · high | 50 | **42%** |
| range_expansion · down · high | 83 | **36%** |

A 27-point spread between the best and worst HIGH cohorts is the difference
between a push worth interrupting someone for and one that isn't.

### Phase 1 — small, one build

- **Card annotation:** extend the existing Similar Setups machinery
  (`historical_performance()`, already on HIGH cards) to a plain-language
  cohort line — "setups like this continued 6 of 10 times (n=103)" — on
  HIGH cards and MEDIUM digest lines. Source labeling mandatory: `backtest`
  cohorts (graded replay journal) until the live SIP-era cohort reaches
  n≥50, then `live` — the Decision-B feed filter means live cohorts are
  near-empty today, and an unlabeled blend would break the product's core
  honesty rule.
- **Demotion rule (armed, dormant):** a cohort with **live-era** n≥50 and
  continuation <45% routes push→digest, journaled as
  `suppress_reason=cohort_demotion` and disclosed in the weekly recap. On
  backtest numbers, range_expansion·down·high (36%) would be the first
  demotion — but the rule deliberately waits for live samples rather than
  acting on IEX-era evidence.

### Phase 2 — research, explicitly deferred

Score integration (multiplicative cohort adjustments), context features
(gap size, time-of-session, news_driven interaction), calibration curves,
per-symbol priors. Needs months of SIP-era accumulation and a
backtest-vs-live divergence monitor before touching a live score.

### Acceptance criteria (phase 1)

Cohort lines render on replayed golden cards with correct n and era label;
cohorts under n=50 show no line (never a stat on too little data); demotion
fires only on live-era cohorts in tests. Build size: S–M.

---

## Sequencing

One build ships at a time, replay-validated, then 2–3 live sessions watched
before the next.

| # | Ship | Why this position | Status |
|---|---|---|---|
| 1 | **P5b+5c** — hygiene: floor, runt purge, 20-session SIP backfill | The substrate. P1's profiles and P2's volume gate need clean SIP history; smallest risk; pure win. | **SHIPPED 2026-08-17** (PR #49 code, PR #50 DEPLOYMENT.md follow-up). See Ship log below. |
| 2 | **P3** — extreme-mover persistence | Smallest detector-adjacent change, independent, immediate observed wins (this week's movers). | **SHIPPED-PENDING-ACCEPTANCE** (PR #52 + a notional-floor follow-up). Corrected 2026-08-17 — code merged/deployed is not the same as the acceptance gate clearing; see Ship log for the 3 open items. |
| 3 | **P2** — gap-and-go | The flagship gap fix; volume confirmation baseline SIP-clean from ship 1. | **Blocked on ship #2's acceptance gate closing** — does not start until all 3 open items below resolve. |
| 4 | **P1** — time-of-day profiles | Biggest surface area; profiles ready from ship 1's backfill; full recalibration ritual included. | Queued behind #3. |
| 5 | **P4** — earnings wiring | Pairs naturally once P2's cards can carry the earnings tag. | Queued behind #4. |
| 6 | **P6 phase 1** — cohort lines | After P1/P2 settle, so cohort definitions are stable before annotating with them. | Queued behind #5. |
| 7 | **P5a** — pre-open gap promotion | Extends P2's reach beyond the watchlist once gap-and-go has 2–3 clean live sessions. | Queued behind #6. |

### Ship log

**Ship #1 (P5b+5c), 2026-08-17 — merged, deployed, VPS-verified.**

- Code: PR #49 (`tradebot/marketdata.py` plausibility floor,
  `tradebot/runner.py` wiring at both required points,
  `scripts/purge_and_backfill_runts.py`). Local replay evidence in the
  PR body: floor re-verified against all 2,448 local cache files (3
  rejections, all IEX-thin USO days); `run_replay` before/after on
  2026-08-04 produced identical tier counts — additive hygiene, zero
  detection-behavior regression.
- Ops follow-up: PR #50 documented the in-container `scripts/`
  invocation (`docker compose run --rm -v
  /opt/perch/scripts:/app/scripts runner python3 scripts/<name>.py`) —
  the VPS run surfaced that this was never written down; now it is,
  permanently, in `docs/DEPLOYMENT.md`.
- VPS execution, 2026-08-17: purge report found **33 runt files**
  across **16 of 17 watchlist symbols × both 2026-08-11/12** (every
  symbol but AMZN, whose 08-12 file simply didn't exist yet — an
  unrelated pre-existing cache gap, not part of this incident; the
  subsequent backfill step filled it regardless). All 33 rejections
  were `implausible_volume`, all against realistic tens-of-millions
  trailing-median references per symbol — i.e. this was a **watchlist-
  wide** incident on those two dates, not the SPY-only shape the
  HANDOFF section's framing ("~1M vs ~40M SPY volume") implied. `--apply`
  deleted exactly those 33 files (1:1 with the report, nothing missed,
  nothing extra). The SIP backfill then re-fetched all 34 slots (33
  purged + AMZN's pre-existing gap) plus two unrelated pre-existing
  gaps (PLTR and USO, both missing 08-14) that `--sessions-n 20`'s
  normal walk-back closed in the same run — 0 errors across all 17
  symbols.
- Read as corroborating (not just circumstantial) evidence that the
  purged numbers really were degraded-feed reads rather than random
  bad prints: ~1M-scale RTH volume on SPY is within IEX's typical
  ~2–3% share of consolidated tape for a ~40M-share day — i.e. the
  "runt" numbers are the right *order of magnitude* for an IEX-only
  read, not an arbitrary low number.
- P1/P2 revalidation window's SIP-cleanliness: **confirmed 2026-08-17**
  — the file-mtime check (HANDOFF section) came back **empty**. Every
  cached intraday file across the whole watchlist postdates the 08-12
  01:19 SIP backfill; zero pre-flip IEX remnants in the trailing
  20-session window. Ships #3 (P2) and #4 (P1) are clear to consume it.

**Ship #2 (P3), 2026-08-17 — code merged/deployed; acceptance gate NOT yet cleared.**

Corrected same day: code landing is not the same as the acceptance gate
closing. The gate is literal — the two historically-suppressed movers,
matched by symbol+session, verifying (or documented why not) — and
that hadn't been checked. Three items are open; ship #3 (P2) does not
start until all three close.

- Code: PR #52. `tradebot/guard.py`'s `extreme_mover_evidence()` (past
  25%, verified by two consecutive real-volume bars closing within 10%
  of each other) replaces the flat suppression; Option B's widened 15%
  spread ceiling for a verified mover only, silent above it; sub-25%
  behavior structurally can't change (the carve-out is gated on
  evidence that can't exist below the line). `spread_pct_of_mid()`
  shared between the guard check and the rendered card so the two can
  never disagree. `extreme_mover`/`extreme_mover_gap_pct`/
  `extreme_mover_volume` journal columns, NULL unless verified. `run_replay`
  before/after on 2026-08-04: identical tier counts (no extreme movers
  in local cache — the expected null result).
- First VPS run, 2026-08-17: `scripts/verify_extreme_mover_evidence.py`
  (no args — swept the full cache tree, not narrowed to the two
  originally-named incident movers, which were never identified by
  symbol) found 43 real `>25%` sessions across the broad-scan/screening
  universe, 2026-08-12 through 08-17 (none on WATCHLIST): 42 verify,
  1 (DFSC, 08-13) correctly doesn't — a real single-bar spike (38.4%)
  reverting to 0.8% one bar later.
- **Open item 1 — the literal acceptance gate.** The 43-session sweep
  never identifies which (if any) of those sessions are the two rows
  the investigation actually named as suppressed. Query, run on the
  VPS:
  ```bash
  docker compose exec runner sqlite3 -header -column /app/data/journal.db \
    "SELECT symbol, session, ts_utc, suppress_reason FROM detections WHERE suppress_reason LIKE 'data_integrity_failed: extreme_prior_close_gap%' ORDER BY ts_utc;"
  ```
  Match the returned symbol+session pairs against the 43-session sweep
  output by hand; report whether the two real historically-suppressed
  rows verify.
- **Open item 2 — the volume check was too weak, now fixed.** Owner
  review of the raw sweep output found a $8.02-combined-notional
  verification (200+300 shares of a sub-penny name) — the doc's literal
  "non-zero volume" bar passed it. Fixed in a follow-up commit:
  `EXTREME_MOVER_MIN_NOTIONAL = $2,000`, a combined-dollar-notional
  floor across the two persistence bars. Calibrated against the real
  sweep data, not guessed: 9 of the 42 would-alert verifications sat
  under $1,900 combined notional (as low as $8.02); the next-lowest
  real one was $3,815 — a clean, wide gap the threshold sits in.
  Excludes exactly the 9 dubious ones, changes nothing for the other
  33. 2 new tests (`test_extreme_mover_evidence_none_below_the_notional_floor`,
  `test_extreme_mover_evidence_verified_at_a_real_notional`); full
  suite 830 passed. **Needs a second VPS run after this redeploys**,
  to confirm the same 9 sessions now correctly stop verifying and
  nothing else changes.
- **Open item 3 — a specific "bar32... 10x collapse... VERIFIED" claim,
  not yet reproduced.** Owner flagged a bar they read as a 10x single-bar
  collapse printing VERIFIED in the raw sweep output. Two independent
  programmatic scans of the full saved output (not eyeballing) found
  **zero** `VERIFIED` tags whose two paired bars diverge by more than
  the 10% tolerance anywhere in the file — including zero anything near
  10x. Every bar-to-bar ratio ≥5x found (3, all in AACBR 08-13) is
  correctly un-verified. Status: could not reproduce from the data in
  hand; needs the specific symbol/session/bar to investigate further,
  or stands resolved if it doesn't recur on the item-2 re-run.

---

## Track-record annotation — the 09:35-era discontinuity

Same discipline as the SIP flip (Decision B), applied to coverage:

- **Schema:** detections gain an explicit `coverage_era` column
  (`'rth-1045'` for everything to date, `'open-0935'` from the P1/P2
  ship) — explicit, not inferred from `code_version`, so queries and
  dashboards can scope on it forever.
- **Stats:** Similar Setups, cohort lines, and tier performance compute
  within-era, exactly as `CURRENT_FEED_FILTER_SQL` scopes by feed — never
  blending a population that structurally could not contain 09:40–10:45
  detections with one that can.
- **Public surfaces:** weekly recap and performance dashboard carry a dated
  marker: *"Detector coverage extended to the market open on <date>.
  Sessions before this date structurally excluded 09:40–10:45; statistics
  are era-scoped."* Nothing deleted, nothing blended.
- **Cards:** every base-rate line carries its n and era label (P2/P6).

---

## Remaining work (as of 2026-08-17 session end)

1. **Owner approvals/decisions**: sequencing sign-off; P3's spread-guard
   option (A or B); P4's suppress→downgrade re-severity. Everything else is
   blocked on these.
2. **Run P3's VPS validation command** (verbatim in Proposal 3 above) so the
   two suppressed movers' would-have-been cards are evidence, not
   prediction, before that build ships.
3. **SIP-era revalidation of P1/P2 numbers** after ship 1's backfill (20 SIP
   sessions via fetch_cache): the local calibration is IEX-era; each
   proposal's checklist requires re-running it on SIP data.

---

## HANDOFF — for a fresh session executing builds from this doc

- **Context docs:** the investigation ("Blind at the Open") is the causal
  ground truth — published artifact, 2026-08-17; this doc is its fix
  design. `docs/STATE-OF-THE-SYSTEM.md` and `docs/BACKLOG.md` for system
  shape and open items. CLAUDE.md rules are binding (pure detectors, ATR
  units, anchors frozen, journal-before-alert, pytest before done).
- **Key engine files:** `tradebot/detectors.py` (pure detectors, anchors,
  tiers — recalibration ritual documented at :15–37), `tradebot/runner.py`
  (live/replay pipeline; history build at ~:1226; close-time caching at
  ~:1356), `tradebot/marketdata.py` (RTH/premarket slicing),
  `tradebot/vendors/alpaca.py` (feed constant, fetch functions),
  `tradebot/alerts.py` (budget/digest), `tradebot/guard.py` (data-integrity
  guards), `tradebot/events.py` (blackout framework, unwired ingestion at
  :316), `tradebot/journal.py` (Decision-B feed filter at :32–54 — the
  pattern `coverage_era` must mirror).
- **Validation harness:** `run_replay` + `scripts/compare_replay.py` (two
  detector versions, same session, separate DBs — the intended A/B tool for
  every proposal here). Local cache: `data/cache/` (145 IEX-era sessions ×
  17 symbols + daily.csv each). Local journal: `data/journal.db` (22,906
  detections, marks table 97% coverage). The 2026-08-17 simulation scripts
  (iteration-1 rejection, TR-profile sim, gap-confirmation calibration,
  floor calibration) lived in a session scratch dir and are NOT in the
  repo — their methods and numbers are fully specified above; rebuild them
  as `run_replay`-based validations per proposal, which is required anyway
  before shipping.
- **Production facts (verified 2026-08-17, updated post-ship-1):** VPS at
  /opt/perch, Docker compose, runner runs `--live --broad-scan`,
  `DETECTOR_DATA_FEED=sip` since 2026-08-12; July files were SIP-
  backfilled 08-12 01:19. The 5b purge is **done** (see the Ship log
  above) — it turned out to be watchlist-wide (33 files, 16 of 17
  symbols × both 08-11/12), not the SPY-only pair this bullet used to
  say. `event_windows` table is empty (P4 wires it); rvol baseline
  verified healthy (0.56–0.86× on 08-13) — do not "fix" rvol, it isn't
  broken.
- **SIP-clean verification (owed, not yet run):** confirms the ~17-18
  pre-existing "skipped (exists)" files per symbol from ship #1's
  backfill are genuinely SIP-era (from the 08-12 01:19 backfill or
  later), not stale pre-flip IEX remnants sitting in the trailing
  20-session window P2/P1 will build on. File mtime is the only local
  signal available (the cache CSVs carry no feed column) — run on the
  VPS:
  ```bash
  find /opt/perch/data/cache -name 'intraday_*.csv' \
    ! -newermt '2026-08-12 01:19:00' -printf '%TY-%Tm-%Td %TH:%TM  %p\n' | sort
  ```
  Empty output = every cached intraday file across the whole watchlist
  was written at or after the SIP backfill — the window is clean, ships
  #3/#4 can proceed. Any line printed names a file that predates the
  flip and needs the same purge+backfill treatment ship #1 gave
  08-11/12 before P2/P1 build on it.
- **Process:** ship order per the Sequencing table; one build per PR;
  replay validation results in the PR body; owner watches 2–3 live sessions
  between ships; every proposal's acceptance criteria are the merge bar.
  The `coverage_era` stamp must land with whichever of P1/P2 ships first.

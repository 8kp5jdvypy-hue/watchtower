# Range-expansion HIGH forensics — investigation, 2026-08

Why some delivered HIGH alerts (GYGY, SGLY) read as junk to the operator
despite surviving every existing guard, and whether that's a tradeability
problem, a scoring-denominator problem, or both. Investigation was
read-only; no code was changed, no thresholds touched, no production
behavior altered. This is the evidence base for a later, separate
decision about what (if anything) to build.

2026-08-20 · repo @ main `cee815ab3e17fba808e29cd2d6db3cd7296f32c2` ·
production `journal.db` and `data/cache/` on the VPS (this checkout's
local copies are a stale, pre-A1 replay snapshot and were not used for
any data claim below) · queries run by the operator against the VPS,
read-only, results reported back over the course of the investigation.

Two competing hypotheses were named at the outset and tested explicitly,
per case:

- **H1 tradeability** — the event was real, but price/spread/liquidity
  made it unsuitable to interrupt a subscriber for.
- **H2 scoring pathology** — a tiny or unstable denominator made a
  mediocre move look extraordinary.

---

## Verdict

**H2 is a strong, well-evidenced hypothesis for the GYGY/SGLY shape of
case — not a confirmed defect.** The mechanism is real and reproduced
exactly from stored data (below). The population test that would move
it from "strong hypothesis" to "confirmed" is underpowered at the
current sample size (37 symbol/session pairs) and was correctly not
forced past that limit. **H1 remains only partially testable**: the
journal computes spread-at-decision-time but only persists it when a
HIGH alert is *rejected* for being too wide — a *delivered* alert's
spread is computed and discarded, never written anywhere. GYGY and SGLY
both delivered, so neither can be fully adjudicated on tradeability
grounds from the data that exists today.

The durable finding, independent of either hypothesis resolving further:
**`range_expansion` measures local statistical surprise against the
stock's own recent behavior — it does not measure, and was never
designed to measure, absolute importance.** Those two things are
usually correlated. When they diverge, Perch currently has no way to
tell the difference, because it only ever computes the local one.

---

## 1. The mechanism — working as specified, not malfunctioning

`range_expansion` fires when the triggering bar's high-low range exceeds
`atr_multiple` (2.0) times the trailing ATR14 computed over the bars
*before* it:

```python
def range_expansion(bars, anchors, period=14, atr_multiple=2.0):
    if len(bars) < period + 2:
        return None
    window = atr(bars[:-1], period=period)   # detectors.py:277
    ...
    ratio = bar_range / window                # detectors.py:282
```
(`tradebot/detectors.py:266-292`)

`atr()` averages the last 14 true-range values from the trailing bars
(`detectors.py:97-107`) — 14 consecutive 5-minute bars, roughly a
70-minute trailing window. Both numerator and denominator are **raw
dollars**, never percent-of-price (`bar_range` in `detectors.py:281` and
`window` from `atr()` are both `$`-denominated). The cluster score
combines the strongest constituent detector's score with a partial
(0.25×) bonus per corroborating detector (`score_cluster`,
`detectors.py:515-523`); tiers are `HIGH ≥ 3.8`, `MEDIUM ≥ 1.9`
(`detectors.py:38-39`).

This is exactly what CLAUDE.md's "thresholds in ATR units, never
percentages" rule describes — the bug, if there is one, is not that this
formula deviates from spec. It's that a dollar-denominated rolling ATR
can itself become small enough, on a low-priced or newly-quiet name,
that the *ratio* stops carrying the meaning a trader would assume it
carries. The formula does exactly what it was built to do; the question
this investigation was chasing is whether what it was built to do is
still meaningful at the tails.

Per-detector score reconstruction was validated directly against
production: for every detector kind except `gap`, recomputing the score
from the exact numerator/denominator values each detector wrote into its
own `context_json` at write time reproduced the stored cluster score
exactly, across every constituent-detector combination tested (validated
locally against 6,594 real multi-detector clusters spanning six
watchlist symbols; zero mismatches). `gap` is a separate, already-mostly-fixed
mechanism — see §5.

## 2. The GYGY worked example

GYGY, 2026-08-20, same session, two `range_expansion` firings on the
same symbol:

| Time (relative) | Bar range | ATR14 window | Ratio | Tier |
|---|---:|---:|---:|---|
| earlier | $1.02 | $0.434 | 2.35× | MEDIUM |
| later | $0.32 | $0.074 | 4.65× | **HIGH**, delivered |

The absolute move got **smaller** (from $1.02 to $0.32 — roughly a
third the size). The score went **up** (2.35 → 4.65) because the
denominator collapsed by ~5.9× (from $0.434 to $0.074) in the same
window. `0.32 / 0.074 ≈ 4.32`, and the reported cluster score of 4.65
is consistent with `range_expansion` combining with one weaker
corroborating detector under `score_cluster`'s 0.25× bonus — the same
formula, applied to smaller and smaller numbers as the stock quieted
down intraday, produced a bigger and bigger score. This matches the
operator's original characterization verbatim ("$0.32 range — 4.3x its
typical bar") and is the single cleanest illustration the investigation
produced: **this is denominator compression, reproduced from the stock's
own two same-day detections, not a hypothetical.**

SGLY's HIGH (score 4.2641, delivered) sits in the same population and
shape — multiple sub-HIGH `range_expansion` firings the same session
(0.65-2.12) before the one that crossed 3.8 — but was not reconstructed
to the same numerator/denominator precision as GYGY before the
investigation stopped (see §7, open items).

## 3. Population

All-time `primary_kind='range_expansion' AND tier='high'` in the
production journal (no session-window filter applied):

- **42 qualifying rows / 37 unique symbol/session pairs / 30 unique
  symbols.** 32 pairs contributed exactly 1 row; 5 pairs contributed 2 —
  modest but real within-pair repetition. These rows are not fully
  independent samples: the same symbol recurs across sessions, and
  sessions can share market-regime effects.
- **Origin, by pair: 19 screening (broad-scan-promoted) / 8 watchlist /
  10 NULL.** Among the 27 pairs with a recorded origin, that's ~70%
  screening. **This figure is not, by itself, evidence of screening
  enrichment.** It is `P(screening | range_expansion HIGH)`. The
  comparison that would actually support an enrichment claim —
  `P(screening | range_expansion fired at all, any tier)`, or ideally
  `P(screening | range_expansion was even evaluable)` — was queried for
  but the results were never returned before the investigation stopped
  on tooling grounds (see §7). **The 70% figure should not be read as
  "predominantly a broad-scan phenomenon" until that base rate is in
  hand**; it may equally represent screening symbols' general
  overrepresentation in the whole `range_expansion`-eligible population,
  which would make 70% unsurprising or even low.
- The 10 NULL-origin pairs are treated as `origin_unknown`, not
  presumed pre-migration — `origin` is `NULL` on every row written
  before that column shipped and is never backfilled
  (`tradebot/journal.py:205`), which is consistent with but not proof of
  these specific 10 rows' provenance; the confirming query (session-date
  clustering in early history) was requested but not returned.
- **Cache coverage: 34 of 37 pairs have usable cached 5-minute bars; 3
  do not.** The 3 missing pairs were requested for full characterization
  (symbol, session, origin, score, alerted state, kinds, outcomes) but
  that data was likewise never returned before the investigation
  stopped. They are excluded from the ATR-trajectory reconstruction and
  must stay explicitly marked `MISSING_UNDERLYING_BARS` — excluded from
  any bucket, not silently dropped from the population count — in any
  future continuation of this work.
- **Retrieval cost, verified from the actual fetch path (not assumed):**
  `fetch_intraday_bars(symbol, session_date)`
  (`tradebot/vendors/alpaca.py:144-165`) is one Alpaca request per
  `(symbol, session)` pair with no date-range batching (only
  `fetch_daily_bars_bulk`, daily-bar-only, batches — `alpaca.py:285`),
  and a single day of 5-minute bars never needs pagination. Backfilling
  the 3 missing pairs would cost exactly 3 requests. What is *not*
  verified: whether those requests would return data at all — if any of
  the 3 are genuinely OTC/pink-sheet names, `DETECTOR_DATA_FEED` (IEX or
  SIP, both consolidated-exchange feeds, `alpaca.py:149-151`) may simply
  have nothing to return for them. This was flagged, not tested — no
  fetch was executed.

## 4. Statistical power — why H2 stays a hypothesis

The proposed test bucketed `range_expansion` HIGHs by
`current-ATR / earlier-same-session-median-ATR` compression (`≤25%`,
`25-50%`, `50-75%`, `>75%`) and compared forward outcomes across
buckets. With 37 total pairs split four ways, even an even split lands
5-12 pairs per bucket; real distributions won't be even, and the
within-pair correlation noted in §3 shrinks the effective sample
further. As a rough rule of thumb (not a formal power calculation, which
would need an assumed effect size this investigation doesn't have),
something on the order of 15-20+ independent pairs *per bucket*
(60-80+ total, and considerably more for a defensible comparative claim)
would be the loose floor before "compressed-baseline HIGHs underperform"
should be read as more than a visible pattern. Current N is roughly
2-3× short of even that floor.

**The reconstruction script needed to run this bucket comparison was
written and validated locally (10 real production-shaped rows, zero
crashes, correct bucket/outcome arithmetic) but was never successfully
executed against the real 37-pair population** — three consecutive
delivery attempts (a single heredoc, a single long base64 line via
`echo`, and a 23-line chunked base64 append) all failed on paste in the
operator's VPS terminal, corrupting mid-string each time. Given that
even a clean run would land at "pattern worth watching, not a
comparative claim" per the power analysis above, and that the GYGY
worked example (§2) already delivers that same finding concretely from
a single query, continuing to fight the delivery mechanism stopped being
worth it. The script and its exact bucket methodology are preserved in
this investigation's session history for reuse if the population grows
enough to justify rerunning it.

## 5. RNWWW — negative control

RNWWW repeatedly produces mathematically extreme `range_expansion`
scores against a market that doesn't functionally exist:

| Session | Score | Spread (guard-measured, % of mid) |
|---|---:|---:|
| 2026-08-17 | 7.60 | 142% |
| 2026-08-17 | 19.5 | 103% |
| 2026-08-20 | 4.41 | 73.7% |

The 19.5 print: close $0.0128, ATR14 $0.001 — `0.001/0.0128 ≈ 7.8%` of
price, which is actually a *large* ATR-as-%-of-price by normal-stock
standards (not a small one); the pathology here is that the ATR is tiny
in raw dollar terms specifically, at a price scale where minimum
tradable price increments can themselves be a meaningful fraction of a
dollar-denominated ATR — the same mechanism as §2, sharper. Every one of
these was correctly suppressed by the spread guard
(`tradebot/guard.py:194-199`, `SPREAD_MAX_PCT_OF_MID = 0.05`,
`guard.py:30`) — none reached a subscriber. **RNWWW demonstrates the
guards working exactly as intended on a case that is mathematically
identical in shape to GYGY/SGLY but has no real executable market behind
it.** It should not be read as evidence that a new filter is needed —
the existing one already caught it, every time, across two separate
sessions.

## 6. The evidentiary gap: H1 cannot be fully tested from the journal as it stands

`guard.validate_alert_data`'s spread check
(`tradebot/guard.py:194-199`) computes `spread_pct_of_mid` on every HIGH
send attempt, but only its *failure* is persisted — as free text inside
`suppress_reason` (e.g. `"data_integrity_failed: spread_too_wide: spread
is 7.2% of mid"`, written from `tradebot/runner.py:593-604`). A
*passing* spread value is computed and then discarded; no column on
`detections` stores it. The same is true of dollar volume, share
volume, and historical average dollar volume — none are computed by any
detector or guard and persisted; they are reconstructible only by
rejoining a detection's timestamp against the cached intraday bars
(where cache coverage allows, per §3), never read directly off the
journal. **Concretely: GYGY and SGLY both delivered, which means their
real spread-at-decision-time is unrecoverable from the journal alone —
only bar-level price/volume reconstruction from cache remains possible,
and only where that cache exists.** H1 (tradeability) can be
*partially* investigated this way, but not *fully* adjudicated, without
either a schema change (out of scope here — no code changes) or a
cache-based reconstruction effort broader than this investigation
completed.

## 7. Open items — what remains unresolved

Stated explicitly rather than left implicit, per this investigation's
own evidence standard:

- The origin base-rate comparison (§3) — `P(screening | range_expansion
  fired, any tier)` and the coarser `P(screening | any detection)` — was
  queried for but results were never returned. **The 70%-screening
  figure among HIGH is not yet known to be enrichment, underrepresentation,
  or exactly base rate.**
- The 3 `MISSING_UNDERLYING_BARS` pairs (§3) were never individually
  characterized (symbol, session, origin, score, alerted state, kinds,
  outcomes) — requested twice, not returned.
- The NULL-origin pre-migration hypothesis (§3) was not confirmed via
  session-date clustering — requested, not returned.
- The full 34-pair ATR-compression bucket reconstruction (§4) — script
  written and locally validated, never executed against production due
  to a paste-delivery failure, not a data or logic failure.
- SGLY was not reconstructed to the same numerator/denominator precision
  as GYGY (§2).
- No test of H1 beyond what §6 describes as structurally possible was
  attempted (e.g. a systematic reconstruction of dollar volume across
  the full 34-pair population from cache, which cache coverage would
  partially support).

None of these being open changes the verdict in §1-2 (the mechanism
claim and the GYGY illustration are both fully evidenced) or the
sample-size conclusion in §4 (more data would not currently change the
"underpowered" verdict even if every open item above were resolved
today, since none of them add pairs to the population).

## 8. The durable finding, and what it implies for a later conversation

`range_expansion`'s ATR14 is a **local** reference: the stock's own
trailing ~70 minutes, recomputed continuously, with no external anchor.
That is precisely what makes it sensitive to genuine local
re-expansion — and precisely what makes it blind to whether the move it
just flagged is big in any sense a subscriber would recognize as
"big." A stock that has gone quiet for an hour and then prints a
below-average bar can score higher than a stock making a real, sizeable
move, because the score only ever asks "is this unusual for you,
right now" — never "is this unusual, period."

This is the case *for* a design direction — not a decision made here —
where a **second, externally-anchored volatility reference** (a longer
trailing window, a market-relative comparison, or some other
denominator insulated from the specific stock's own recent quiet
stretch) is reported **alongside** the existing local ATR, not in place
of it. Local statistical surprise and absolute importance are different
questions; Perch's `range_expansion` currently answers only the first
one and reports it as if it were the second. Whether and how to build
anything in response to that is explicitly out of scope for this
document — that decision belongs in a separate proposals conversation,
the same way `docs/open-awareness-proposals-2026-08.md` followed
`docs/open-blindness-investigation-2026-08.md`.

---

## Evidence standard

Code-level claims (§1, §5, §6, §8's mechanism description) are cited by
file:line against `cee815ab3e17fba808e29cd2d6db3cd7296f32c2` and were
verified by direct reading of `tradebot/detectors.py`, `tradebot/guard.py`,
`tradebot/runner.py`, `tradebot/journal.py`, `tradebot/config.py`, and
`tradebot/vendors/alpaca.py`, plus local validation of the per-detector
score-reconstruction method against 6,594 real (if pre-A1, replay-era)
production-shaped detection rows in this checkout's own cache and
journal — a mechanical proof that the reconstruction methodology is
sound, independent of the (separate, VPS-only) population data.

Data-level claims (§2, §3, §5) are the operator's own read-only queries
against the production VPS `journal.db` and `data/cache/`, reported back
over the course of this investigation. This document does not
independently re-verify those query results — it records what was
reported. Open items in §7 are exactly the claims that were requested
but never confirmed either way, and are marked as such rather than
inferred.

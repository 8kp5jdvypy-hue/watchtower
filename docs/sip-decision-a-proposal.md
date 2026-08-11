# Decision A: threshold treatment for the SIP era

Proposal only — no code changes, no threshold changes shipped. Uses
the corrected 83-session n backtest
(`docs/sip-phase1-backtest-report.md`, 2026-08-12 second-pass
correction). Follows `SCANNER_PLAN.md`'s established train/test
discipline (fit on one half, validate on the other, then reversed;
reject anything that doesn't hold up in both directions) and its
significance convention (`tradebot.telegram_bot.performance
.significance_check`, |z| ≥ 1.96 for "distinguishable from chance").

**Deciding metric**: continuation rate at +30min (the project's own
default offset for track record — see `SCANNER_PLAN.md`'s "Time
horizons" section), technical setups only (`news_driven` excluded,
same discipline as `historical_performance()` — an event-driven move
doesn't share the technical mechanism a threshold is meant to price).
Sessions: the same 83-session, matched-intersection IEX/SIP pair
(2026-04-01 – 2026-08-05), split into a first half (41 sessions,
2026-04-01–2026-05-29) and second half (42 sessions, 2026-06-01–2026-08-05)
for train/test.

## Option (i): keep current thresholds (TIER_HIGH=3.8, TIER_MEDIUM=1.9)

**Projected alerts/day (SIP, full 83 sessions):** HIGH 3.57, MEDIUM
28.11, LOG 111.00 — vs. IEX's historical 3.77 / 32.40 / 112.78. A
4.2% overall decline, already reported in the corrected backtest.

**Continuation rate at +30min, full sample:**

| Tier | IEX | SIP |
|---|---|---|
| HIGH | 50.5% (n=212, z=+0.14, not significant) | 51.6% (n=184, z=+0.44, not significant) |
| MEDIUM | 47.0% (n=1863, z=-2.62, **significant**) | 46.0% (n=1574, z=-3.18, **significant**) |
| LOG | 49.7% (n=8599, z=-0.49, not significant) | 49.5% (n=8487, z=-0.97, not significant) |

**SIP's surviving signals are not worse than IEX's own historical
baseline — if anything, HIGH tier ticks up slightly (50.5%→51.6%).**
MEDIUM tier's continuation rate is significantly *below* 50% on
**both** feeds — a real, pre-existing finding, not a SIP effect (see
"Aside" below).

**Train/test consistency**: HIGH tier's result flips between halves
(45.1% train / 58.1% test in one direction, mirrored in the other) —
consistent with `SCANNER_PLAN.md`'s already-established "HIGH tier is
not a proven edge," not new information. MEDIUM tier's sub-50% pattern
is significant in the first half (44.5%, z=-3.26) but not the second
(47.9%, z=-1.13) — a real effect at full sample, but not perfectly
stable split by half, which is expected at reduced per-half n.

## Option (ii): re-derive thresholds to approximate the historical alert rate

Fit `T_high`/`T_medium` on SIP train-half data so its HIGH/day and
(HIGH+MEDIUM)/day match IEX's train-half historical rate; validate the
fitted, fixed thresholds on the SIP test half.

| | Direction A (fit on first half) | Direction B (fit on second half) |
|---|---|---|
| Fitted T_high | 3.580 | 3.837 |
| Fitted T_medium | 1.472 | 1.472 |
| Applied to test half → HIGH/day | 4.52 | 3.10 |
| Applied to test half → MEDIUM/day | 31.29 | 33.44 |
| Applied to test half → LOG/day | 105.88 | 107.15 |
| Test-half HIGH continuation | 53.2% (n=109, ns) | 46.1% (n=89, ns) |
| Test-half MEDIUM continuation | 50.9% (n=865, ns) | 44.6% (n=1026, **significant, below 50%**) |

**The two fitting directions disagree meaningfully on `T_high`** (3.58
vs. 3.84 — current default, 3.8, sits almost exactly at direction B's
fit and well above direction A's) **and disagree on whether the fitted
threshold actually helps**: direction A's fit moves MEDIUM's
continuation from significantly-below-chance to statistically neutral
on holdout; direction B's fit does not — MEDIUM stays significantly
bad. `T_medium` is the one stable number here (1.472 both directions),
but `T_high` — the more consequential one, since it gates the tier
that gets sent as an individual live alert — is not.

This is the same "both directions contradicted each other" signature
`SCANNER_PLAN.md`'s "Best hours" section already established as noise,
not a real effect, at comparable sample sizes. **Re-deriving
thresholds to hit a target rate does not reliably reproduce a stable
rate OR reliably improve quality out of sample** — it's better in one
direction, no better (and still significantly sub-50%) in the other.

## Option (iii): evidence-optimal re-derivation

Swept candidate score thresholds (0.6 to 3.8, step 0.2) against each
train half, checking whether continuation rate at +30min shows a real,
direction-consistent optimum anywhere in the range (full sweep table
in the analysis script, `docs/`-adjacent scratch output, not
committed — summary below):

- **First half (train)**: continuation stays in the 43-48% range
  across the *entire* threshold sweep, several points significantly
  below 50% (e.g. T≥1.0: 45.5%, z=-4.05), with no clear rise at higher
  thresholds — if anything, mid-range thresholds (T≥2.6: 47.8%) do
  marginally better than the current default (T≥3.8: 45.1%).
- **Second half (train, reversed)**: continuation is at or slightly
  above 50% across most of the range, rising toward the top
  (T≥3.6: 54.7%, T≥3.8: 58.1%) — the opposite shape from the first
  half, where higher thresholds did *not* help.

**No direction-consistent optimum exists.** The first half says higher
thresholds don't help (and the current default is one of the *worse*
points in its own sweep); the second half says higher thresholds help
the most right at the current default and above. Exactly the
contradiction pattern `SCANNER_PLAN.md` already treats as disqualifying.
**The evidence does not support a smarter cutoff than what's already
in production.**

## Aside, not part of this decision: MEDIUM tier's continuation rate

Flagging because it surfaced during this analysis and is real, not
because it's a SIP question: **MEDIUM tier's continuation rate is
significantly below 50% on both feeds** (IEX 47.0% z=-2.62, SIP 46.0%
z=-3.18, full 83-session sample). Since this holds on IEX too, it
predates and is independent of the SIP migration — this proposal
doesn't attempt to fix it (that's a MEDIUM-tier calibration question
in its own right, orthogonal to which feed is behind it), but it
shouldn't sit quietly in a Decision A doc that specifically went
looking at tier-level continuation. Worth its own look separately.

## Verdict: option (i), keep current thresholds

**The evidence favors keeping current thresholds as-is.** Not because
there's no effect — there is, a real ~4% decline, concentrated in
`round_number_break` and `rvol_spike` per the backtest report — but
because:

1. The decline is modest, not the dramatic drop the first (buggy)
   backtest pass suggested.
2. Both attempts to correct it — (ii) targeting a rate, (iii) targeting
   quality — fail the same train/test consistency check
   `SCANNER_PLAN.md` already holds every other calibration decision in
   this project to. Neither produces a threshold that reliably helps
   in both directions; picking either one over the status quo would be
   fitting noise, not signal.
3. The signals that do survive under SIP at current thresholds are not
   lower quality than IEX's own historical baseline — HIGH ticks up
   slightly, MEDIUM and LOG are within noise of IEX's already-known
   numbers.

**Fewer but better is close to the honest description of what SIP
does here, at HIGH tier specifically** (3.77→3.57/day, 50.5%→51.6%
continuation) — a real trade in the right direction, if a small one.
MEDIUM is fewer and very slightly worse (47.0%→46.0%, both already
sub-50% on IEX), which is closer to "fewer and about the same" than
"fewer and better," but neither move clears statistical significance
against IEX's own number, so "no meaningful change in quality, some
loss in volume" is the more defensible summary than either "better" or
"worse."

**Known gap this doesn't resolve**: `rvol_spike`'s volume-baseline
mismatch (n=24→12, per the backtest report) isn't addressed by any
option here — none of these are score-threshold fixes for a
volume-based detector's baseline problem. That was already flagged as
its own issue in `docs/sip-migration-proposal.md` and remains open,
independent of this verdict.

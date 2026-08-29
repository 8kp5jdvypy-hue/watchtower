# Options-tradeability delivery policy — investigation and proposal

**Status: proposal. No code written, no live path touched, no threshold
changed.** Read-only investigation. The only write from this work is this
file.

2026-08-29 · repo @ `origin/main` `48f0e59` · local checkout is a stale
pre-cutover snapshot and was **not** used for any population claim below.

## Owner decision this document implements

> Perch is built for options traders. A HIGH that an options trader cannot
> realistically act on must not be delivered as a HIGH.

Binding, and it supersedes the deferral in
`docs/range-expansion-forensics-2026-08.md` §8 ("Whether and how to build
anything in response to that is explicitly out of scope for this
document — that decision belongs in a separate proposals conversation").
This document is that conversation.

**Tradeability is a DELIVERY policy** applied after scoring and after the
data guard. Detection, scoring, and journaling stay complete and
unchanged. Nothing here is a detector change or a threshold change.

## Evidence convention

- **VERIFIED** — measured in this session against `origin/main` code or a
  real repo artifact, and stated with its file:line or query.
- **SOURCED** — a claim made by a repo document, quoted, not re-measured.
- **INFERRED** — my reasoning over VERIFIED/SOURCED facts, labelled.
- **PENDING** — requires production data I cannot reach. Not estimated.

---

# PART 0 — The blocker, stated first

**Sections 1, 2 and 3 of the brief (MEASURE / ANSWER / SIMULATE) cannot be
completed from this machine.** I am not estimating them.

**VERIFIED.** The only `journal.db` on this laptop
(`~/projects/watchtower/data/journal.db`, 15 MB, mtime 2026-08-17) covers
`2026-01-02 → 2026-08-10`, 22,906 detections. Queried read-only:

```
SELECT MIN(session), MAX(session), COUNT(*) FROM detections;
→ 2026-01-02 | 2026-08-10 | 22906

SELECT tier, COUNT(*) FROM detections WHERE session >= '2026-08-11' GROUP BY tier;
→ (no rows)

SELECT symbol FROM detections WHERE symbol IN ('GYGY','SGLY');
→ (no rows)
```

The SIP cutover is **2026-08-11**. This file ends the day before it. It
contains **zero** rows in the measurement window, and neither GYGY nor
SGLY appears anywhere in it. It also has **no `decision_events` table at
all** — it predates PR #74, so the contract-selection outcome the brief
asks for does not exist in it even in principle.

`.tables` on that file returns exactly:
`contract_selections  detections  event_windows  iv_history  marks`.

This matches the precedent set by the forensics investigation, which
recorded the same split: *"production `journal.db` and `data/cache/` on
the VPS (this checkout's local copies are a stale, pre-A1 replay
snapshot and were not used for any data claim below) · queries run by the
operator against the VPS, read-only, results reported back."* **[SOURCED]**

**→ The exact read-only query pack to run on the VPS is Part 6.** It is
validated (Part 6.4) — the previous investigation lost a round-trip to a
paste-delivery failure, so I built a scratch database from the real
schema and ran every query against it before writing them down.

Everything that does **not** depend on that data — what the code does
today, which buckets are even expressible, feature availability, the
prerequisite persistence change, the policy design, the rollback, the
acceptance gate — is complete below.

---

# PART 1 — Findings from the code (VERIFIED)

## 1.1 A HIGH with no tradable contract is delivered today

This is the defect the owner decision names, and it is unambiguous in the
code. In `runner.process_new_bar`, for a HIGH that passed the budget and
the guard:

```
runner.py:922   selection = select_contract(...)
runner.py:927   set_no_trade(conn, detection_id, not selection.is_tradable)
runner.py:932   _record_decision(stage="contract_selection",
                    decision="TRADABLE" if selection.is_tradable else "NO_TRADE", ...)
runner.py:973   else:  logger.info("NO TRADE: symbol=%s reason=%s ...")
runner.py:980   text = templates.render_high_alert(cluster, ..., selection, ...)
runner.py:986   _commit_then_send(conn, alerter, text, priority=outbox.PRIORITY_HIGH, ...)
runner.py:987   UPDATE detections SET alerted=1 WHERE id=?
```

**There is no branch between the NO_TRADE result and the send.** The
`selection` object is passed into the renderer, which prints
`"none tradable — no liquid strike"` (`templates.py:84-85`,
`NO_TRADE_LABELS` at `templates.py:67-71`), and the alert goes out at
`PRIORITY_HIGH` to the ops channel and every subscriber.

**[VERIFIED]** The full ordering, which is where a policy must insert:

```
detect → cluster → score → tier
  → dedup lookup                       (decision_events stage="dedup")
  → event-window routing               (stage="event_window_routing")
  → AlertBudget.evaluate()             (stage="alert_routing")   ← cap+cooldown reserved
  → guard.validate_alert_data()        (stage="data_guard", rejections only)
  → select_contract()                  (stage="contract_selection")  ← tradeability known HERE
  → render_high_alert() → _commit_then_send()                        ← delivery happens HERE
```

Tradeability is already computed **one step before** delivery. The policy
needs no new data fetch and no new vendor call on the live path — only a
branch between two adjacent lines. **[INFERRED, from the ordering above]**

## 1.2 Bucket (c) is empty by construction

The brief asks for the share of delivered HIGHs with *"a chain selected
but illiquid by a stated floor."* **That set cannot be non-empty under
current code**, and this reshapes the whole proposal.

`costs._passes_liquidity` (`costs.py:105-122`) is applied **during**
selection, not after it. A contract that fails any floor is never
selected; it is filtered out, and if nothing survives, selection returns
`NoTrade("no_liquid_strike", ...)`. The floors already in force:

| Floor | Value | `costs.py` |
|---|---|---|
| Minimum bid | `> 0.05` | `costs.py:106`, `MIN_BID:33` |
| Spread as % of mid | `≤ 10%` | `:112`, `MAX_SPREAD_PCT_OF_MID:30` |
| Absolute spread if mid < $3 | `≤ $0.15` | `:114`, `MAX_SPREAD_ABSOLUTE_UNDER_3:31` |
| Open interest | `≥ 500` deep / `≥ 250` thin | `:116`, `MIN_OPEN_INTEREST:29` |
| Day volume (when reported) | `≥ 100` | `:118`, `MIN_DAY_VOLUME:30` |

Symbol class comes from `config.liquidity_class` — `deep` for the ten
index-ETF/mega-cap names, `thin` for everything else (`config.py:19-26`).

**[VERIFIED]** Consequence: **the liquidity floors are not missing. They
are already enforced, and they already produce the correct answer — the
answer is simply ignored at delivery time.** The gap is not detection of
illiquidity; it is that an alert is sent anyway once illiquidity has been
detected.

This is the single most important finding in this document, because it
means the proposed policy is not "add liquidity checking" (new logic,
new failure modes, new calibration) but "honour the liquidity verdict the
system already reached" (a branch on an existing boolean).

## 1.3 There are exactly three NO_TRADE reasons

**[VERIFIED]** `grep -oE 'NoTrade\("[a-z_]+"' tradebot/costs.py`:

| Reason | Raised when | `costs.py` |
|---|---|---|
| `no_liquid_strike` | no listed expiry in the 7–14 DTE window; **or** no contract cleared the liquidity gate; **or** no liquid strike near 0.40–0.55 delta; **or** the selected contract is missing greeks | `:247,263,284,290` |
| `earnings_blackout` | earnings fall before the candidate expiry | `:254` |
| `breakeven_exceeds_typical_move` | breakeven in ATR exceeds the typical move for similar setups (n ≥ min sample) | `:305` |

Note that `no_liquid_strike` is **four distinct causes collapsed into one
reason code**, separated only by free-text `detail`. That matters for
bucketing (Part 2.3) and is addressed in the prerequisite PR (Part 4).

## 1.4 The guard's spread is the UNDERLYING's, and it is discarded on pass

**[VERIFIED]** `guard.validate_alert_data` computes
`spread_pct_of_mid(quote)` at `guard.py:194` and rejects above
`SPREAD_MAX_PCT_OF_MID = 0.05` (widened to
`EXTREME_MOVER_SPREAD_MAX_PCT_OF_MID = 0.15` for a verified extreme
mover). This is the **underlying equity quote's** spread — not any
option's.

On a rejection the value survives as free text inside `suppress_reason`
(`"data_integrity_failed: spread_too_wide: spread is 7.2% of mid"`). On a
pass it is **computed and discarded** — no column on `detections` stores
it.

This is the gap the brief calls the PR #62 §6 discard gap, and the
forensics document states its consequence directly: *"GYGY and SGLY both
delivered, which means their real spread-at-decision-time is
unrecoverable from the journal alone."* **[SOURCED —
`range-expansion-forensics-2026-08.md` §6]**

**Per-row honesty rule for the reconstruction:** for every delivered
HIGH, underlying spread-at-decision-time is `UNRECOVERABLE`. It must be
written as that string in the reconstruction, per row — never
back-estimated from cache bars and never left blank in a way a reader
could mistake for zero.

## 1.5 The selected contract's own liquidity is journaled nowhere

**[VERIFIED]** `contract_selections` (`journal.py:228-246`) stores:
`detection_id, symbol, right, strike, expiry, dte, delta, is_vertical,
short_strike, short_delta, entry_mid, entry_ts_utc, mid_15m, mid_30m,
mid_60m, mid_close` — plus additive `day_low`, `day_high`
(`journal.py:505-507`).

**No bid, no ask, no spread, no open interest, no day volume.**

The `decision_events` `detail_json` for `stage="contract_selection"`
(`runner.py:940-950`) carries `expiry, dte, similar_setups_sample,
insufficient_sample, no_trade_detail, is_vertical, strike, right` — also
**no liquidity fields**.

So the brief's request for *"selected contract's spread/OI/volume where
journaled"* has a definite answer: **nowhere, for any row, ever.** The
values existed in memory inside `_passes_liquidity` and were discarded.
This is why Part 4's prerequisite PR exists.

## 1.6 What decision_events does give us

**[VERIFIED]** `decision_events` (`journal.py:320-370`) is append-only
(DB triggers, not convention) and carries `stage, decision, reason,
detail_json, code_version, run_mode, run_id`. `run_mode` values are
`live` / `replay` / `unknown` (`journal.py:1558-1560`).

**Every reconstruction query must filter `run_mode='live'`.** The
schema's own docstring explains why: replaying a session appends rows
with the same `detection_id` (it is a hash of symbol/session/ts/kinds,
stable across runs) that "read exactly like the live ones from that day
… appended after them and therefore looking like the later, superseding
truth." An unfiltered query silently mixes replays into the population.

Close marks use the sentinel `offset_min = -1`
(`CLOSE_MARK_OFFSET_MIN`, `journal.py:64`), not a positive minute value.

---

# PART 2 — The one population measurement available locally

## 2.1 GYGY is not optionable at all

`universe.db` carries `assets.options_enabled`, populated from Alpaca's
asset catalog (`universe.py:51`). The local copy is a **2026-08-08**
snapshot — stale, and the production file is authoritative — but the
schema and the values for the two named symbols are real.

**[VERIFIED]** read-only against `~/projects/watchtower/data/universe.db`:

```
SELECT symbol, exchange, tradable, options_enabled, is_active
FROM assets WHERE symbol IN ('GYGY','SGLY');

GYGY | NASDAQ | 1 | 0 | 1
SGLY | NASDAQ | 1 | 1 | 1
```

**GYGY has `options_enabled = 0`.** It has no listed options chain. A HIGH
on GYGY is not "hard to trade" for an options trader — it is
**impossible** to trade as an option, and was impossible at the moment it
was delivered. It sits in bucket (a) by definition, with no liquidity
judgment required.

**SGLY has `options_enabled = 1`.** It *is* optionable, so whatever made
it read as junk is a different problem — a liquidity, breakeven, or
scoring-denominator problem, not an optionability one. The two symbols
the owner named land in **different buckets**, and a policy that catches
GYGY does not automatically catch SGLY.

**[INFERRED]** This is consistent with the forensics verdict, which found
H2 (scoring pathology) the stronger hypothesis for "the GYGY/SGLY shape
of case" while H1 (tradeability) stayed only partially testable. The two
hypotheses are not competing for the same rows: GYGY is now a **confirmed
H1 case on optionability grounds alone**, independent of spread data that
no longer exists.

## 2.2 More than half the scannable universe is not optionable

**[VERIFIED]** same file, same snapshot:

```
SELECT options_enabled, COUNT(*) FROM assets WHERE is_active=1 GROUP BY options_enabled;
0 | 6881
1 | 6145
```

**6,881 of 13,026 active symbols (52.8%) carry no options chain.**

**[VERIFIED]** `universe.active_symbols()` accepts `require_options`
(`universe.py:295-304`), and **no caller anywhere in `tradebot/` or
`scripts/` ever passes it** — confirmed by grep; the only hits are the
parameter's own definition and docstring. Stage 1 therefore screens, ranks
and promotes from the **full** set, non-optionable symbols included.

**[INFERRED]** Every promotion slot spent on a non-optionable symbol is a
slot that cannot produce an actionable HIGH for an options trader, and
the promotion limit is 25. The magnitude of the effect on delivered HIGHs
is **PENDING** — it depends on whether non-optionable names are
over- or under-represented among high scorers, which is exactly what
Q1/Q2 answer.

Universe composition is **out of scope** per the brief. Flagged as a
follow-up in Part 8.1 with this evidence attached.

## 2.3 What the four buckets can and cannot be

| Bucket | Determinable? | From |
|---|---|---|
| **(a)** no options chain | **Yes, cleanly** | `universe.assets.options_enabled = 0` |
| **(b)** chain exists, selection failed | **Yes** | `options_enabled = 1` **and** `decision_events.decision = 'NO_TRADE'` |
| **(c)** chain selected but illiquid | **Empty by construction** | Part 1.2 — an illiquid contract is never selected |
| **(d)** liquid contract selected | **Yes** | `decision_events.decision = 'TRADABLE'` |

**Honest limitation.** (a) and (b) are **not** separable from
`decision_events` alone: a symbol with no chain and a symbol whose chain
had no liquid strike both produce `no_liquid_strike`. Only
`options_enabled` separates them, which is why the query pack joins
`universe.db` across an `ATTACH`. If the production `universe.db` has
drifted since a given session (the catalog is refreshed in place; there
is no point-in-time history of `options_enabled`), that join is
**as-of-today, not as-of-decision-time** — a real limitation, recorded
here rather than glossed, and closed permanently by Part 4's stamp.

---

# PART 3 — Simulation design (numbers PENDING)

Cannot be run until Q1 returns. The design is fixed here so the analysis
is not invented after seeing the data.

## 3.1 Policies to simulate

| ID | Policy | Delivery gate |
|---|---|---|
| **P0** | Current | none — every HIGH that passes budget + guard is delivered |
| **P1** | Require chain exists | `options_enabled = 1` |
| **P2** | Require selection success | `contract_decision = 'TRADABLE'` |
| **P3a** | P2 + floor set A (loosen) | spread ≤ 12% of mid; OI ≥ 250 deep / 100 thin |
| **P3b** | P2 + floor set B (current) | spread ≤ 10% of mid; OI ≥ 500 / 250; vol ≥ 100 |
| **P3c** | P2 + floor set C (tighten) | spread ≤ 7% of mid; OI ≥ 1000 / 500; vol ≥ 250 |
| **P4** | Optionable-only universe | Stage 1 promotes only `options_enabled = 1` |

All floors are **scale-aware** — expressed as a percentage of mid, or as
contract counts per symbol liquidity class. No absolute-dollar ATR
threshold appears in any of them, per the brief and per CLAUDE.md's
ATR-units rule.

**P3a/P3c cannot be simulated from the journal.** They need per-contract
spread/OI/volume that Part 1.5 established is journaled nowhere. They
become simulable only **after** the Part 4 prerequisite PR has run for
long enough to accumulate a population. Reporting them as simulable
would be false. **P0, P1, P2 and P4 are simulable from Q1 today.**

## 3.2 Result table (one row per policy)

| Metric | Definition |
|---|---|
| HIGHs retained | n delivered under policy / n delivered under P0 |
| Alerts/day | retained ÷ distinct sessions in window |
| Technical hit rate | share where the underlying continued in `trend` at +30m, using `marks` |
| Contract hit rate | share where `mid_30m > entry_mid`, over rows having both |
| MFE / MAE | from `contract_selections.day_high`/`day_low` vs `entry_mid`, where present |
| Symbols excluded | named, not counted |
| **Actionable recall** | of the HIGHs the policy **excluded**, the share that would have been genuinely actionable. A miss counts **only if the symbol was inside the eligible set** — a non-optionable symbol excluded by P1 is **not** a miss, because it was never actionable for the audience this product serves |

Report `n` on every cell. Where a bucket has n < 10, label it
**underpowered** and draw no conclusion from it — the forensics
investigation correctly refused to force a verdict past a 37-pair sample,
and the same discipline applies here.

**[INFERRED]** Given the daily HIGH cap of 8 (`alerts.py:179`) and ~13
sessions from 2026-08-11 to 2026-08-29, the ceiling on the whole
population is ~104 delivered HIGHs, and the real number will be well
below that. **Several per-policy cells will be underpowered, and the
honest output of this exercise may be a direction plus a
measure-longer.** Saying so now prevents over-reading the table later.

---

# PART 4 — Feature availability, and the prerequisite PR

Per the standing rule, each input the policy needs, stated explicitly:

| Input | Status | Detail |
|---|---|---|
| Symbol has an options chain | **AVAILABLE** | `universe.assets.options_enabled`. Caveat: as-of-today, not as-of-decision (Part 2.3) |
| Contract-selection outcome | **AVAILABLE** | `decision_events` stage `contract_selection`, decision `TRADABLE`/`NO_TRADE` |
| NO_TRADE reason | **AVAILABLE** | same row, `reason`; four causes collapse into `no_liquid_strike` (Part 1.3) |
| Selected contract identity | **AVAILABLE** | `contract_selections` right/strike/expiry/dte/delta |
| Entry and forward option mids | **AVAILABLE** | `contract_selections.entry_mid`, `mid_15m/30m/60m/mid_close` |
| Option day range | **AVAILABLE** | `contract_selections.day_low/day_high` (additive, PR-era; older rows NULL) |
| Underlying marks | **AVAILABLE** | `marks` at 15/30/60 and `-1`; status via `mark_resolution_events` |
| Delivered / suppressed + why | **AVAILABLE** | `detections.alerted`, `suppress_reason`, `suppress_category`; `decision_events` `alert_routing` / `data_guard` |
| **Selected contract spread** | **UNAVAILABLE** | Computed in `_passes_liquidity`, never persisted (Part 1.5) |
| **Selected contract OI / day volume** | **UNAVAILABLE** | Same |
| **Underlying spread at decision, delivered rows** | **UNAVAILABLE** | Computed at `guard.py:194`, discarded on pass (Part 1.4). Recoverable only for rows **rejected** for spread, as free text |
| **Chain snapshot for delivered alerts** | **UNAVAILABLE** | Never persisted in any form |
| Underlying dollar volume | **UNAVAILABLE from journal** | Not computed by any detector or guard; reconstructible only by rejoining cached intraday bars, where cache exists **[SOURCED — forensics §6]** |
| Point-in-time optionability | **UNAVAILABLE** | `assets` is refreshed in place; no history |

## 4.1 Prerequisite PR (separate, additive, ships before the policy)

**Purpose:** make the tradeability decision auditable after the fact, so
the policy's effect can be measured rather than asserted, and so P3a/P3c
become simulable.

Follows the existing additive-column pattern
(`_add_column_if_missing`, `journal.py:394-405`), which is already used
for 20+ columns. **`context_json`'s shape is not touched** — the brief
forbids it and the additive-column pattern makes it unnecessary.

**Additive columns on `contract_selections`** (all nullable; old rows stay
NULL, which reads correctly as "not recorded then"):

```
entry_bid            REAL
entry_ask            REAL
entry_spread_pct     REAL     -- (ask-bid)/mid at selection
open_interest        INTEGER
day_volume           INTEGER  -- NULL when the vendor did not report it
```

**Additive columns on `detections`:**

```
underlying_spread_pct        REAL  -- guard.spread_pct_of_mid at decision, PASS included
options_enabled_at_decision  INTEGER  -- point-in-time stamp, closes Part 2.3's caveat
```

**Additive fields inside `decision_events.detail_json`** for
`stage="contract_selection"` — that column is already free-form JSON, so
adding keys is not a shape change to any typed structure:

```
no_trade_cause   -- splits no_liquid_strike's four causes: NO_EXPIRY
                 -- | NO_CONTRACT_PASSED_LIQUIDITY | NO_STRIKE_NEAR_DELTA
                 -- | MISSING_GREEKS
best_rejected    -- {spread_pct, open_interest, day_volume} of the
                 -- nearest-miss contract, so "how far off was it?" is
                 -- answerable instead of only "it failed"
```

`best_rejected` is what converts a future floor change from a guess into
a measurement: it says how many alerts a given floor set would have
gained or lost.

**Blast radius of the prerequisite PR: nil on delivery.** It only writes
columns. No decision consults them. It is safe to ship and let run while
the policy is still being decided — and it should be, because every
session it runs is a session of evidence the policy will need.

---

# PART 5 — Proposed policy

## 5.1 The proposal: P2, behind a default-off flag

**Deliver a HIGH only when contract selection produced a tradable
contract. Journal, label, and display every held HIGH. Never drop one
silently.**

```
OPTIONS_TRADEABILITY_POLICY = off | shadow | enforce      (default: off)
```

- **off** — current behaviour exactly. No new code path executes.
- **shadow** — evaluate the policy, journal the verdict, **deliver
  everything anyway**. This is how the blast radius gets measured on real
  traffic before anything is withheld.
- **enforce** — a HIGH whose `selection.is_tradable` is false is
  journaled, labelled, counted, and **not pushed**.

### Why P2 and not P1 or P3

- **Not P1 (chain exists).** Strictly weaker than P2 — every symbol P1
  excludes, P2 also excludes, because a symbol with no chain always
  yields `no_liquid_strike`. P1 would have held GYGY but not a
  chain-having symbol whose every strike was untradable. P2 dominates it
  at no extra cost.
- **Not P3 (new floors).** The floors in `costs.py` are **contract-
  selection thresholds**. Changing them is a threshold change, which the
  brief puts out of scope and which the standing rules require separate
  evidence and approval for. **v1 holds every floor in `costs.py`
  exactly as it is.** P3a/P3c are not simulable today anyway (Part 3.1).
- **P2 adds no new judgment.** It consumes a boolean the system already
  computes one line earlier. There is no new threshold, no new vendor
  call, no new failure mode, and nothing to calibrate — which is why it
  can ship on a much shorter evidence leash than a scoring change.

### Where it goes

One branch in `runner.process_new_bar`, between `select_contract()`
(`runner.py:922`) and `render_high_alert()` (`runner.py:980`). Detection,
scoring, journaling, `set_no_trade`, and the `contract_selection`
decision event **all still happen first and unchanged** — the policy is
strictly downstream of the complete record.

**Budget interaction, and it matters.** A held HIGH must
`budget.release_unsent(cluster, decision)` — the same mechanism PR #84
added after the 2026-08-26 incident where eight guard-rejected candidates
consumed all eight daily reservations and blocked a later valid HIGH
(`runner.py:1001`). Without this, holding untradeable HIGHs would
burn the daily cap on alerts nobody received, reproducing that exact bug.
**This is the single highest-risk detail in the change** and must have a
regression test that fails against an implementation missing it.

## 5.2 Blast radius

**PENDING — the honest answer.** The share of delivered HIGHs that would
be held is exactly what Q2 returns, and I decline to estimate it.

What is bounded today:

- **Lower bound, VERIFIED:** GYGY would have been held.
  `options_enabled = 0` guarantees `no_liquid_strike` guarantees
  `is_tradable = false`.
- **Upper bound, VERIFIED:** the policy can only ever hold HIGHs already
  carrying `detections.no_trade = 1`. That column has been written since
  before the cutover, so `SELECT COUNT(*) ... WHERE tier='high' AND
  no_trade=1 AND session>='2026-08-11'` is a **complete** upper bound and
  is the first line of Q2. If it is a large fraction of delivered HIGHs,
  that is itself the finding — it would mean Perch has been routinely
  interrupting subscribers with alerts its own contract logic had already
  judged untradeable.
- **Zero effect on MEDIUM, LOG, digests, recaps, or the journal.** The
  policy touches one branch on the HIGH delivery path only.

## 5.3 What a held HIGH looks like

**Never a silent drop.** Three surfaces, all additive:

**Journal.** The detection row is written in full, exactly as today —
same score, same tier `high`, same kinds, same headlines, same
`contract_selection` event. Then:

```
suppress_reason   = 'tradeability_held: <no_trade_reason>'
suppress_category = 'tradeability'          -- new SuppressionCategory member
alerted           = 0
```

`SuppressionCategory` (`alerts.py:131-147`) is explicitly designed for
this: *"the field a future consumer (dashboard, quality metrics) should
group/filter on instead of parsing suppress_reason's several different
free-text shapes."* Adding a member is additive and does not disturb
`handlers`' exact-string dependency on `suppress_reason='cooldown_active'`.

A `decision_events` row is appended: `stage="tradeability_policy"`,
`decision="HOLD"`, `reason=<no_trade_reason>`, with the policy mode and
version in `detail_json` — so the ledger says which policy version held
it and in which mode.

**Dashboard.** The signal still appears in Signals and in the detail
view, tier `HIGH`, with an explicit state — not hidden, not downgraded to
MEDIUM. Downgrading would be a lie about what the detector found;
withholding delivery is a claim about actionability. Proposed copy:

> **HIGH · not delivered — no tradable contract**
> Perch found this setup but could not find an options contract you could
> realistically trade at the time. It is kept here in full.
> Reason: no liquid strike.

**Telegram.** Nothing is pushed. The daily/weekly recap gains one line
naming the count and the symbols, so a subscriber can always see what was
held and go look. A policy that hides its own suppressions would
contradict the product's central positioning.

## 5.4 Rollback

- `OPTIONS_TRADEABILITY_POLICY=off` restores current behaviour exactly.
  It is an env var in `.env`, read at process start — a `docker compose
  restart runner` reverts it. No schema migration to undo, no data to
  backfill, no deploy required.
- The additive columns from Part 4 stay; they are inert without the
  policy.
- Held detections are already journaled in full, so a rollback loses
  nothing retrospectively — the record of what *would* have been held
  survives as `decision_events` rows.

## 5.5 Acceptance gate

**Shadow first, and the gate is a real gate.**

1. Ship the Part 4 prerequisite PR. Let it run ≥ 5 clean sessions.
2. Run the Part 6 query pack. Produce the Part 3 table with real `n`.
   **If the population is underpowered, say so and keep measuring — do
   not enforce on a thin sample.**
3. Deploy `shadow`. Require **≥ 10 clean sessions** in which:
   - every HIGH has a `tradeability_policy` decision event (conservation:
     no HIGH silently skips the policy);
   - the held set is reviewed symbol by symbol by the owner, and contains
     **no** symbol the owner judges was genuinely actionable;
   - `budget.release_unsent` behaviour is confirmed against the live
     journal — no session where held HIGHs consumed cap slots.
4. Only then `enforce`, and only with explicit owner approval — per the
   standing rule that engine-adjacent changes need evidence plus separate
   approval.
5. First enforced week: daily check that delivered-HIGH volume did not
   collapse below ~1/day. A policy that silences the product is a failure
   even if every individual hold was correct.

**Ten clean shadow sessions is the floor already used** by the
signal-quality program's evidence gate **[SOURCED —
`docs/signal-quality-program.md`, acceptance contract item 9]**, so this
is the house standard, not a number invented here.

---

# PART 6 — The read-only query pack for the VPS

**Run these on the production box. All read-only.** `sqlite3 -readonly`
plus `mode=ro` on the ATTACH; additionally, `decision_events`,
`mark_resolution_events` and the screening tables carry DB-level
append-only triggers, so a stray write would abort rather than corrupt.

Per `docs/BACKLOG.md`, the canonical way to run a repo tool on that box is
`docker compose run --rm -v /opt/perch/scripts:/app/scripts runner ...`,
but these need only `sqlite3` against the data volume, so they can run
directly on the host against `/opt/perch/data/`.

### 6.1 Q3 first — sanity and coverage (run this one first; it is cheap)

```bash
cd /opt/perch/data
sqlite3 -readonly journal.db <<'SQL'
.mode column
.headers on
SELECT 'window' AS metric, MIN(session) AS a, MAX(session) AS b, COUNT(*) AS n
FROM detections WHERE tier='high' AND session >= '2026-08-11'
UNION ALL SELECT 'sessions_with_high', NULL, NULL, COUNT(DISTINCT session)
FROM detections WHERE tier='high' AND session >= '2026-08-11'
UNION ALL SELECT 'high_delivered', NULL, NULL, COUNT(*)
FROM detections WHERE tier='high' AND session >= '2026-08-11' AND alerted=1
UNION ALL SELECT 'high_suppressed', NULL, NULL, COUNT(*)
FROM detections WHERE tier='high' AND session >= '2026-08-11' AND COALESCE(alerted,0)=0
UNION ALL SELECT 'high_no_trade_upper_bound', NULL, NULL, COUNT(*)
FROM detections WHERE tier='high' AND session >= '2026-08-11' AND no_trade=1
UNION ALL SELECT 'high_missing_contract_event', NULL, NULL, COUNT(*)
FROM detections d WHERE d.tier='high' AND d.session >= '2026-08-11'
  AND NOT EXISTS (SELECT 1 FROM decision_events e
                  WHERE e.detection_id=d.id AND e.stage='contract_selection' AND e.run_mode='live');

SELECT data_feed, origin, tier, COUNT(*) AS n
FROM detections WHERE session >= '2026-08-11'
GROUP BY data_feed, origin, tier ORDER BY data_feed, origin, tier;

SELECT stage, decision, COUNT(*) AS n
FROM decision_events WHERE run_mode='live'
GROUP BY stage, decision ORDER BY stage, n DESC;
SQL
```

`high_no_trade_upper_bound` is the complete upper bound on the policy's
blast radius (Part 5.2). `high_missing_contract_event` should be 0; a
non-zero value means some HIGHs never reached selection and needs
explaining before the rest is trusted.

### 6.2 Q2 — the bucket answer the brief asks for

```bash
cd /opt/perch/data
sqlite3 -readonly journal.db <<'SQL'
ATTACH DATABASE 'file:/opt/perch/data/universe.db?mode=ro' AS u;
.mode column
.headers on
WITH ce AS (
  SELECT detection_id, decision, reason,
         ROW_NUMBER() OVER (PARTITION BY detection_id ORDER BY seq DESC) AS rn
  FROM decision_events WHERE stage='contract_selection' AND run_mode='live'
)
SELECT
  CASE WHEN d.alerted=1 THEN 'delivered' ELSE 'not_delivered' END AS delivery,
  CASE
    WHEN a.symbol IS NULL         THEN 'z_unknown_symbol_not_in_universe'
    WHEN a.options_enabled = 0    THEN 'a_no_options_chain'
    WHEN ce.decision = 'NO_TRADE' THEN 'b_chain_exists_selection_failed'
    WHEN ce.decision = 'TRADABLE' THEN 'd_liquid_contract_selected'
    ELSE 'z_no_contract_selection_event'
  END AS bucket,
  COALESCE(ce.reason,'') AS no_trade_reason,
  COUNT(*) AS n,
  COUNT(DISTINCT d.symbol) AS n_symbols,
  GROUP_CONCAT(DISTINCT d.symbol) AS symbols
FROM detections d
LEFT JOIN ce ON ce.detection_id=d.id AND ce.rn=1
LEFT JOIN u.assets a ON a.symbol=d.symbol
WHERE d.tier='high' AND d.session >= '2026-08-11'
GROUP BY delivery, bucket, no_trade_reason
ORDER BY delivery, bucket, n DESC;
SQL
```

Bucket **(c)** is deliberately absent — Part 1.2 establishes it is empty
by construction, and a bucket that cannot be occupied should not appear
as an always-zero row pretending it was measured.

### 6.3 Q1 — the full row-level reconstruction (CSV; everything else derives from this)

```bash
cd /opt/perch/data
sqlite3 -readonly journal.db <<'SQL' > /tmp/high_reconstruction.csv
ATTACH DATABASE 'file:/opt/perch/data/universe.db?mode=ro' AS u;
.mode csv
.headers on
WITH ce AS (
  SELECT detection_id, decision, reason, detail_json,
         ROW_NUMBER() OVER (PARTITION BY detection_id ORDER BY seq DESC) AS rn
  FROM decision_events WHERE stage='contract_selection' AND run_mode='live'
),
ar AS (
  SELECT detection_id, decision,
         ROW_NUMBER() OVER (PARTITION BY detection_id ORDER BY seq DESC) AS rn
  FROM decision_events WHERE stage='alert_routing' AND run_mode='live'
),
dg AS (
  SELECT detection_id, reason,
         ROW_NUMBER() OVER (PARTITION BY detection_id ORDER BY seq DESC) AS rn
  FROM decision_events WHERE stage='data_guard' AND run_mode='live'
)
SELECT
  d.id, d.session, d.ts_utc, d.symbol, d.origin, d.data_feed,
  d.score, d.primary_kind, d.kinds, d.close, d.atr14, d.trend,
  d.alerted, d.suppress_reason, d.suppress_category,
  d.no_trade, d.news_driven, d.extreme_mover,
  a.options_enabled AS symbol_options_enabled,
  a.exchange        AS symbol_exchange,
  ar.decision       AS routing_decision,
  dg.reason         AS guard_reject_reason,
  ce.decision       AS contract_decision,
  ce.reason         AS contract_no_trade_reason,
  json_extract(ce.detail_json,'$.no_trade_detail')       AS no_trade_detail,
  json_extract(ce.detail_json,'$.dte')                   AS sel_dte,
  json_extract(ce.detail_json,'$.strike')                AS sel_strike,
  json_extract(ce.detail_json,'$.right')                 AS sel_right,
  json_extract(ce.detail_json,'$.is_vertical')           AS sel_is_vertical,
  json_extract(ce.detail_json,'$.similar_setups_sample') AS similar_n,
  json_extract(ce.detail_json,'$.insufficient_sample')   AS insufficient_sample,
  cs.entry_mid, cs.delta, cs.expiry,
  cs.mid_15m, cs.mid_30m, cs.mid_60m, cs.mid_close,
  cs.day_low, cs.day_high,
  m15.price AS px_15, m30.price AS px_30, m60.price AS px_60, mcl.price AS px_close
FROM detections d
LEFT JOIN ce ON ce.detection_id=d.id AND ce.rn=1
LEFT JOIN ar ON ar.detection_id=d.id AND ar.rn=1
LEFT JOIN dg ON dg.detection_id=d.id AND dg.rn=1
LEFT JOIN contract_selections cs ON cs.detection_id=d.id
LEFT JOIN marks m15 ON m15.detection_id=d.id AND m15.offset_min=15
LEFT JOIN marks m30 ON m30.detection_id=d.id AND m30.offset_min=30
LEFT JOIN marks m60 ON m60.detection_id=d.id AND m60.offset_min=60
LEFT JOIN marks mcl ON mcl.detection_id=d.id AND mcl.offset_min=-1
LEFT JOIN u.assets a ON a.symbol=d.symbol
WHERE d.tier='high' AND d.session >= '2026-08-11'
ORDER BY d.session, d.ts_utc;
SQL
wc -l /tmp/high_reconstruction.csv
```

**Two columns are deliberately absent because they do not exist**
(Part 1.4/1.5), and must be read as `UNRECOVERABLE` for every delivered
row rather than as missing data: the underlying spread at decision time,
and the selected contract's spread/OI/volume. For rows **rejected** for
spread, the value is inside `guard_reject_reason` as free text.

`offset_min = -1` is the session-close sentinel, not a typo.

### 6.4 Validation status of this pack

**[VERIFIED]** Every query above was executed before being written down.
I built scratch `journal.db` and `universe.db` from the real schema by
calling `tradebot.journal.connect()` and `tradebot.universe.connect()` on
`origin/main`, inserted a synthetic GYGY-shaped HIGH, and ran all three.
Q1 returned its 43 columns with the cross-database `ATTACH`, the
`json_extract` calls, the `marks` pivot and the `ROW_NUMBER()`
latest-event dedup all resolving. Q2 correctly classified the synthetic
row as `delivered / a_no_options_chain / no_liquid_strike`.

This is not a claim about the production data — only that the SQL parses
and the joins resolve against the real schema, so a round-trip is not
lost to a syntax error the way the previous investigation lost one.

---

# PART 7 — What the public track record should lead with

**Stated, not changed** — the brief explicitly scopes the change out.

Once tradeability gates delivery, **contract outcomes become the primary
population** and the underlying's continuation rate becomes a secondary,
explanatory statistic. The track record should then lead with:

> Of the alerts Perch delivered, this is what the **contract it named**
> actually did — entry mid to +30 minutes and to the close, every alert,
> unedited.

Two honesty obligations that follow, and both must be designed before the
page changes:

1. **The held set must be published too**, with its count and reasons.
   A track record computed only over delivered alerts, after a policy
   that removes the least tradeable ones, is a **survivorship-filtered
   record**. Publishing "we held N HIGHs as untradeable" alongside it is
   what keeps the "unedited, misses included" claim true.
2. **The population changes on the enforce date**, exactly as the SIP
   cutover changed it. `journal.py`'s `CURRENT_FEED_FILTER_SQL` already
   sets the precedent — Decision B deliberately reset the stats rather
   than blending pre- and post-migration populations. The same call has
   to be made here, and it is a separate decision.

Not implemented in this proposal.

---

# PART 8 — Out of scope, and follow-ups

Untouched per the brief: detector thresholds, the scoring formula,
watchlist/universe composition, the postmarket program, gap-and-go.

## 8.1 Follow-up: non-optionable symbols in the promotion set

**Flagged with evidence, not proposed.** Part 2.2: 52.8% of the active
universe is non-optionable (n = 6,881 / 13,026, 2026-08-08 snapshot), and
`require_options` exists but is never passed by any caller. Stage 1
spends promotion slots — capped at 25 — on symbols that cannot produce an
actionable options alert.

P4 in the simulation quantifies what that costs in delivered HIGHs. If
the answer is material, the fix is a one-argument change at the
`active_symbols()` call site, but it is a **universe-composition change**
and therefore needs its own proposal and approval.

Note the interaction: **P2 and P4 are complements, not alternatives.** P2
stops the bad alert reaching the subscriber; P4 stops the wasted
promotion slot upstream. P2 alone leaves Stage 2 evaluating symbols whose
HIGHs can only ever be held.

## 8.2 Follow-up: `no_liquid_strike` conflates four causes

Part 1.3. Addressed by the prerequisite PR's `no_trade_cause` field, but
worth naming separately: today "this stock has no options" and "this
stock's options are too wide" are the same reason code, and they warrant
different product copy and different upstream fixes.

---

# PART 9 — What I need from you

1. **Run Q3, then Q2, then Q1** (Part 6). Q3 and Q2 are small; paste
   their output. Q1 writes a CSV — send the file, or paste it if it is
   small (the population ceiling is ~104 rows, Part 3.2).
2. With that I will complete brief §1 (row-level reconstruction, with
   `UNRECOVERABLE` marked per row), §2 (the four buckets with symbols
   named), and §3 (the policy table for P0/P1/P2/P4 with honest `n`), and
   revise Part 5.2's blast radius from PENDING to measured.

**Decision you can make now, without the data:** whether to ship the
Part 4 prerequisite PR. It is additive-only, has nil delivery blast
radius, and every session it runs is a session of evidence the policy
needs — including the data that makes P3a/P3c simulable at all. Holding
it back until the numbers arrive costs sessions that cannot be recovered
retrospectively.

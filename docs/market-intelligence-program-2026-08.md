# Market-wide intelligence program — 2026-08-26

Status: active implementation program. This document ranks the gaps; it
does not turn unfinished research into live alert behavior.

## Objective

Perch should observe every active US equity it is entitled to observe,
identify scheduled and unscheduled opportunity contexts across relevant
sessions, explain every admission/rejection, and publish only signals that
have passed replay and live-shadow evidence gates.

"Watch the market" means measurable funnel coverage, not a claim that
every instrument receives the expensive Stage 2 detector suite on every
tick. "Opportunity" means an observed, provenance-backed setup; it does
not imply a trade recommendation or guaranteed fill.

## Triggering miss: 2026-08-26 earnings reactions

Production evidence for CRWD, CRM, and OKTA established one causal chain:

```
active/tradable universe
  -> no market-wide catalyst admission
  -> not promoted into the 25-symbol Stage 2 set
  -> no Stage 2 evaluation session
  -> no detection
  -> no alert
```

This was not an Alpaca/SIP outage. Full postmarket bars were available for
all three. It was not budget, cooldown, suppression, or delivery: no
detection existed to route. The regular-session runner also terminates at
the XNYS close, so it could not observe the reaction after 16:00 ET even
if the symbols had been promoted earlier.

## Ranked gap register

### P0 — coverage truth

1. **Scheduled catalyst ingestion is unwired and failure-ambiguous.** The
   earnings adapter existed, but no production caller populated
   `event_windows`; its undocumented provider adapter returned `[]` for
   both a legitimate empty day and every transport/shape failure.
2. **No extended-hours observation process exists.** The vendor fetch
   contains premarket/postmarket bars, while the live detector loop reads
   RTH-only bars and exits at the close.
3. **Stage 1 has no catalyst dimension.** It ranks regular-session daily
   price/volume anomalies and caps promotion at 25. A known after-close
   reporter can be quiet by that invariant and never reach Stage 2.
4. **No end-to-end coverage SLO exists.** A healthy heartbeat proves that
   a loop is alive, not that scheduled events, symbols, bars, evaluations,
   or alerts conserved across the funnel.

### P1 — signal truth and data integrity

5. The time-of-day alignment bug remains open in `rvol_spike` and
   `relative_strength_break`: a missing bar can shift positional
   baselines for the rest of a session.
6. Manual/cron cache acquisition can still finish with `gave up` while
   exiting zero, and final-path CSV writes are not atomic.
7. Contract-forward outcome fetches still conflate vendor failure with a
   genuinely absent contract in some paths.
8. Similar-setup/tier/hour populations still need one consistent
   `news_driven` exclusion contract and explicit coverage-era scoping.
9. The per-symbol live exception-isolation loop needs a direct regression
   test proving one bad symbol cannot stop the rest of the market pass.

### P1 — product trust

10. Missing outcome marks remain ambiguous in the API/UI after their
    expected resolution time.
11. Quote staleness can remain invisible after the first successful UI
    fetch; several frontend fetch errors still render as quiet/loading or
    signed-out states.
12. Public/system status exposes only a subset of already-recorded failure
    families.

### P2 — operations and durability

13. A real off-box restore drill is still owed.
14. Deployment should be one exact-revision wrapper, not a command pattern
    an operator must reconstruct.
15. Metrics writes need atomic replace and corruption preservation.
16. Universe/screening retention needs a separately reviewed archival job;
    observability must not be deleted in the change that first records it.

## Target architecture

```
active asset catalog + provider entitlements
  -> coverage ledger (expected / fetched / eligible / stale / missing)
  -> catalyst ledger (scheduled events + provenance + ingestion status)
  -> session observers
       premarket       04:00–09:30 ET
       regular         09:30–16:00 ET
       postmarket      16:00–20:00 ET
  -> candidate admission
       market anomaly | scheduled catalyst | verified news reaction
  -> Stage 2 evaluation / extended-hours reaction evaluation
  -> data-quality, liquidity, freshness, duplicate, and budget guards
  -> journal-before-alert routing
  -> outcomes + miss attribution + public honesty surfaces
```

The extended-hours observer is a separate service and state machine, not
an extension of the regular-session runner's clock. It first runs in
shadow mode and writes observations only. Options-market availability is
shown honestly; an after-hours equity reaction is not automatically
called an executable options trade.

## Release sequence

Each slice is independently reversible and gets its own PR and acceptance
evidence.

1. **Market-wide catalyst ledger.** Use the active universe, distinguish a
   successful empty provider response from failure, persist provider/run/
   revision/count provenance, apply the binding earnings=`context` policy,
   and bound the pre-open digest. No new customer alert type.
2. **Extended-hours data contract.** Add explicit pre/postmarket accessors,
   session/early-close tests, freshness/gap/zero-volume/single-print guards,
   and coverage conservation metrics.
3. **Postmarket shadow observer.** Scheduled-catalyst admission first;
   measure reaction from the official RTH close on completed SIP bars.
   Persist candidates, rejections, latency, and data age. No sends.
4. **Versioned truth set and replay.** Include CRWD/CRM/OKTA as tuning
   examples plus held moves, fades, negative reactions, quiet reports,
   single bad prints, stale/missing bars, early closes, and provider
   failure. Keep a final holdout set separate from tuning fixtures.
5. **Explanation enrichment.** Attach official issuer/SEC result links and
   timestamps after price detection; never delay the initial observation
   waiting for prose enrichment.
6. **Whole-market unscheduled extended-hours discovery.** Add a bulk/
   streaming anomaly pass only after the scheduled-catalyst observer proves
   its data and noise controls.
7. **Alert activation decision.** Requires explicit owner approval after
   the shadow gate below. Rollback is one configuration kill switch.
8. **Remaining ranked reliability/trust work.** Close P1/P2 items above in
   small PRs while live-shadow evidence accumulates.

## Implementation status

- Slice 1, market-wide catalyst ledger: merged and deployed at `b39e4b5`.
  The 2026-08-26 production ingestion matched 48 of 48 provider events to
  the 13,091-symbol active universe, created 96 context windows, and an
  immediate manual rerun proved idempotent (one successful run remained
  one). CRM, CRWD, and OKTA all have structured after-hours ledger facts.
- Slices 2–3, extended-hours contract and postmarket observer: merged and
  deployed at `6cc0a95`. The new process
  is default-off (`POSTMARKET_SHADOW_ENABLED=0`), imports no delivery path,
  writes a separate append-only shadow database, uses one SIP snapshot per
  symbol/tick, observes actual XNYS closes including early-close sessions,
  evaluates completed bars only, and records an explicit outcome for every
  scheduled symbol including per-symbol fetch failures. Its first live session
  recorded 27 invariant-clean ticks, zero errors, 1.047-second average tick
  latency, and five deduplicated candidates: CRM, CRWD, HPQ, LTRX, and OKTA.
  A coverage audit later established that this observer began around 19:38 ET,
  covered only about 11% of the required close-through-20:05 window, and must
  not count as one of the ten clean sessions. The complete-session count is 0.
- Slice 4, versioned truth set and replay: merged at `1f9d879`. The
  production-shaped CRM/CRWD/OKTA tuning cases are isolated from 18 synthetic
  adversarial contract-holdout cases. The empirical holdout is intentionally
  empty until independently labeled live cases are available; synthetic
  precision/recall does not count toward the activation gate.
- Daily operational audit and empirical scoring: in review. Full-window,
  conservation, error, revision, provenance, latency, and candidate-ledger
  invariants produce an immutable daily report. Empirical scoring requires a
  separately locked, blinded, artifact-digested manifest and fails closed on
  missing symbols, policy drift, ambiguity, misses, false positives, or
  direction disagreement.
- Extended-hours customer alerts remain unimplemented and unauthorized.
  The shadow observer may remain enabled to collect evidence. Customer routing
  must remain absent/off until the acceptance gate below is satisfied and the
  owner explicitly approves a separate delivery release.

## Shadow acceptance gate

Before any extended-hours customer alert is armed:

- at least 10 clean live shadow sessions;
- at least 95% recall for scheduled, liquid reactions reaching 8% on the
  independently labeled empirical holdout set;
- observation latency no greater than one completed bar plus processing;
- zero stale, zero-volume, or single-print alerts;
- every candidate and rejection has symbol/session/provider/feed/revision/
  baseline/freshness provenance;
- bounded duplicate rate with an explicit per-symbol/event key;
- failure injection proves provider outage, missing bars, malformed data,
  and persistence failure are loud and do not fabricate opportunity;
- a tested kill switch and exact rollback revision are recorded.

Precision, latency, and recall are reported together. No threshold is
changed merely to make one missed screenshot pass.

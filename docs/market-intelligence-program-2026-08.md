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

5. **Resolved:** `rvol_spike` now keys both historical and current
   cumulative-volume baselines by DST-aware RTH wall-clock slot, and
   `relative_strength_break` joins symbol and proxy bars by exact timestamp.
   Missing or duplicate required proxy timestamps fail closed. Regression
   tests cover both former false-signal paths after a silently omitted bar.
6. **Resolved:** manual/cron cache acquisition returns nonzero when any
   symbol exhausts its session search or a real-session fetch is empty, while
   legitimate cached/holiday no-ops still return zero. CSVs are written to a
   same-directory temporary file and atomically replaced only after a complete
   write; failure tests prove no partial final path remains.
7. **Resolved:** contract-chain fetch exceptions now propagate to the
   per-selection backfill logger while a successful chain missing the target
   contract remains an honest absence. Option day-range API failures likewise
   propagate and log, while a successful response with no trade bars remains
   distinguishable as no range. Sibling contracts continue backfilling.
8. **Resolved:** similar-setup, kind, tier, and hour statistics now share
   one clean-technical population: current-feed, watchlist-origin,
   non-news-driven detections with real marks. Tier/hour results expose the
   number of otherwise-eligible news-driven rows excluded, and regressions
   prevent feed-era, screening-origin, or event-driven contamination.
9. **Resolved:** direct live and replay loop regressions inject a failure in
   one symbol's evaluation, prove every later symbol in the same market pass
   still reaches evaluation, and require the failure to appear in
   `HeartbeatStats.errors`.

### P1 — product trust

10. **Resolved:** every regular-session checkpoint now has an explicit
    resolution state. The append-only ledger distinguishes available prices,
    targets the session could not reach, and failed data resolution without
    fabricating marks. Before the close batch, the API derives bounded
    pending/waiting states; after its grace period, a missing ledger event is
    explicitly delayed/degraded. Signal detail renders those states instead
    of treating every absent mark as indefinitely pending.
11. **Resolved:** quote responses carry provider-error, stale-cache, missing,
    cache-age, and checked-at evidence. The UI preserves last-known prices
    during a transient failure but labels reconnecting/delayed/partial/
    unavailable states with the last successful update. Malformed successful
    API bodies fail explicitly, only a real 401 means signed out, and a failed
    watchlist signal fetch can no longer render every symbol as quietly clean.
12. **Resolved:** public status retains the separately defined
    data-integrity missed-alert count and also surfaces every recorded
    rejection, suppression, downgrade, and `*_failed` operational family.
    Symbol-labelled counters aggregate by family to keep the public artifact
    bounded, and the page explicitly says overlapping counter increments are
    not a deduplicated incident total.

### P2 — operations and durability

13. A real off-box restore drill is still owed.
14. **Resolved:** source/image deployment is one mandatory full-SHA wrapper.
    It refuses dirty/stale revisions, binds `GIT_SHA`, requires verified pre-
    and postdeploy backups, waits for Compose health, verifies every Python
    service revision, checks all five SQLite databases and the public API, and
    supports only explicit ancestor rollback. Boot supervision no longer
    rebuilds images with the `unknown` fallback.
15. **Resolved:** metrics writes use a flushed, fsynced same-directory
    temporary file and atomic replace. Invalid JSON or a non-object root is
    logged loudly and copied to a collision-safe sibling before counters
    restart; failure to preserve or read the original aborts the increment,
    and failed publication leaves the previous file intact.
16. Universe/screening retention needs a separately reviewed archival job;
    observability must not be deleted in the change that first records it.
    The prerequisite custody gap is closed: `universe.db` is now required in
    local and encrypted off-box backup sets because its Stage-1 screening
    evidence is not rebuildable from the asset catalog.

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
- Daily operational audit and empirical scoring: merged and deployed at
  `b7d58f2`. Full-window,
  conservation, error, revision, provenance, latency, and candidate-ledger
  invariants produce an immutable daily report. Empirical scoring requires a
  separately locked, blinded, artifact-digested manifest and fails closed on
  missing symbols, policy drift, ambiguity, misses, false positives, or
  direction disagreement.
- Aggregate evidence gate: merged at `987c3c3`. A locked, complete XNYS-session inventory
  pins daily and control artifacts by SHA-256, recomputes aggregate confusion
  and latency metrics, rejects mixed feed/provider eras, and emits only
  `NOT_READY` or `ELIGIBLE_FOR_OWNER_REVIEW`. It has no activation path.
- Operational control evidence: merged at `17afffd`. An offline harness
  exercises real
  failure-conservation, default-off kill-switch, delivery-isolation, Git
  rollback-ancestry, SQLite online-backup, point-in-time restore, integrity,
  and append-only invariants. It writes immutable, SHA-256-addressable control
  artifacts and refuses partial or overwrite-prone evidence sets.
- Complete evidence custody: merged and production-restored through the
  encrypted off-box path at `6261801`. Nightly backups cover all durable
  decision/evaluation/shadow databases plus immutable postmarket audits and
  controls, bind each set with SHA-256, ship irrebuildable state encrypted
  off-box, and support a traversal-safe isolated restore with SQLite checks.
- Slice 6, whole-market unscheduled discovery: implemented as a separate,
  default-off shadow service pending review and live provider-contract
  calibration. Alpaca's bounded real-time SIP mover/activity screens provide a
  fast lane. A second deterministic lane SHA-256 binds and partitions the active
  universe into five disjoint shards, evaluating one shard each minute so a
  complete cycle covers every symbol at the five-minute bar cadence. Both lanes
  reuse the same strict evaluator. Provider bounds/timestamps/metrics, sweep
  identity/position, active-universe conservation, missing bars, evaluations,
  and candidates are append-only in the already-backed-up shadow database. This
  slice has no delivery dependency and does not alter or reset the scheduled-
  earnings evidence program.
- Slice 7, discovery evidence audit: implemented as a read-only immutable
  session report pending review. It distinguishes partial calibration from a
  complete close-through-8:05 PM ET evidence window; reconciles provider
  freshness, top-N scope, ranks, universe/fetch/evaluation/candidate counts,
  revision and threshold provenance; and preserves candidate plus near-miss
  lifecycles in the already-backed-up audit directory.
- Public status failure-family disclosure: implemented behind the existing
  static status-page generation path. All current runner failure/suppression
  counter families are selected by stable family semantics rather than a
  validator-only allowlist, while neutral throughput counters remain absent.
- Metrics durability: implemented with same-directory atomic publication,
  corrupt-byte preservation, fail-closed unreadable/backup behavior, and
  crash-path regressions that prove the previous counter file survives a
  simulated replacement failure.
- Schema-migration error integrity: implemented so only the exact duplicate
  for the requested column is benign. Disk, lock, readonly, malformed-schema,
  and unrelated migration failures now stop startup instead of presenting a
  partially migrated journal as healthy.
- Exact-revision deployment: implemented as `scripts/deploy.sh`, with the
  systemd boot unit changed to start existing verified images rather than
  rebuild. Black-box tests cover the complete happy path plus malformed SHA,
  dirty tree, stale main, backup, container revision, SQLite, and rollback
  failures.
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

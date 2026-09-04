# Perch signal-quality program

## Objective

Turn Perch from a reliable reaction detector into a trustworthy market-
intelligence system whose coverage, timeliness, tradability, ranking, and
forward outcomes are independently measurable. More alerts are not success.
Success means fewer unexplained misses and a calibrated separation between
historical observation, current qualification, and forward-looking confidence.

All new capabilities remain shadow-only until the evidence gate passes. No
quality score may be described as confidence, probability, profitability, or
advice unless its calibration supports that exact claim.

## Acceptance contract

The program is complete only when all of the following are true:

1. Every postmarket candidate has append-only, provenance-bound outcome marks
   at +5, +15, +30, and +60 minutes, the postmarket close, next-session open,
   and next-session close. Missing and unavailable observations remain explicit.
2. Maximum favorable/adverse excursion and time-to-MFE are measured from the
   first knowable candidate bar without look-ahead.
3. Every discovery cycle records scheduled lag and screen, bar-fetch,
   evaluation, persistence, and total latency so a tick gap names its slow stage.
4. A deterministic live full-universe sweep supplements bounded vendor rankings,
   while a separate after-the-fact census still measures discovery false
   negatives rather than assuming either live lane achieved complete coverage.
5. Versioned features cover volatility-adjusted movement, market/sector-relative
   strength, liquidity and spread, catalyst provenance, data confidence,
   persistence shape, and reversal risk.
6. Candidate lifecycle transitions are append-only and distinguish newly
   qualifying, confirmed, strengthening, fading, dequalified, and closed states.
7. The quality rank is deterministic, versioned, decomposable into named
   components, and reproducible from stored evidence.
8. Threshold and rank changes use walk-forward development data and a blinded,
   independently labeled empirical holdout. Tuning examples never count as
   independent evidence.
9. Before the first evaluation session opens, a prospective campaign locks the
   exact range, technical/owner floors, feeds/providers, and eligible
   audit/observer schema and code revisions. The final evidence package
   SHA-256-pins that campaign and satisfies at least the existing floors: ten clean
   sessions, zero dirty sessions in the locked range, at least 95% recall,
   worst-case detection latency no greater than 330 seconds, zero ambiguous
   labels or direction mismatches, and passing control artifacts. Owner-declared
   precision and sample-size floors must be fixed before viewing holdout results.
10. Customer delivery requires a separately tested state-transition router,
    kill switch, rollback, stale/degraded presentation, and explicit owner
    approval. No shadow milestone silently enables alerts.

## Ordered workstreams

### 1. Outcome truth

- Append-only candidate mark events with deterministic idempotency keys.
- Availability state and provider/feed/timeframe/revision provenance.
- Directional returns, MFE, MAE, time-to-MFE, and cohort quality reports.
- Next-session resolution through the actual exchange calendar.

Implementation: `tradebot/postmarket_quality.py` owns provider-free outcome
semantics; `tradebot/postmarket_quality_backfill.py` performs bounded candidate-
only fetch orchestration and immutable report publication. Operational details
are in `docs/postmarket-outcome-quality.md`.

### 2. Timing truth

- Scheduled-versus-actual cycle lag and missed-cycle count.
- Per-stage screen, bar-fetch, evaluation, persistence, and total latency.
- Heartbeat and audit attribution for every excessive gap.

Implementation: `postmarket_discovery_timing` is committed atomically with
each discovery tick, the service runs on an exchange-close-anchored grid, and
discovery audit version 4 reconciles schedule, stage, persistence, total timing,
and deterministic full-universe sweep shards. See
`docs/postmarket-timing-truth.md`.

### 3. Coverage truth

- Independent full-universe postmarket census after the window closes.
- Stage-1 recall, false-negative symbols, detection delay, and reason codes.
- Provider disagreement and unavailable-universe accounting.

Implementation: the live observer covers one of five deterministic universe
shards per minute in addition to the bounded screen; then
`tradebot/postmarket_recall_census.py` independently snapshots and replays the
active universe in bounded chunks, writes append-only per-symbol evidence, and
publishes immutable miss reports. See `docs/postmarket-recall-census.md`.
Candidate-level Massive comparison is implemented in the isolated external-
context worker. A separate next-day Massive SIP flat-file proof replays the
full frozen census universe, records independent recall/provider agreement and
exact-bar divergence, and publishes immutable reports. Production credentials,
entitlement validation, and clean live proofs remain gates.

### 4. Context and tradability

- ATR/expected-move normalization and SPY/sector-relative movement.
- Spread, quoted depth, dollar volume, float, market cap, halt, and bar-quality
  evidence with explicit unavailable states.
- Earnings, guidance, filing, news, regulatory, analyst, and unexplained catalyst
  categories with source timestamps and confidence boundaries.

Implementation: `tradebot/postmarket_context.py` writes bounded, append-only
candidate context attempts covering prior-session ATR, SPY-relative movement,
SIP quote spread/depth, RTH dollar liquidity, asset eligibility, completed-bar
quality, and verified catalyst-ledger facts. Context version 2 also computes
sector-relative movement when a locked licensed manifest row and its mapped
ETF's completed bar were both knowable at detection, preserving the manifest
ID, digest, and observation time. It refreshes append-only for each completed
lifecycle bar and records a named technical data-confidence inventory. Every
source carries point-in-time and provider/feed provenance. Missing licensed
mapping, float shares, guidance, regulatory, and analyst inputs stay explicitly
unavailable.
See `docs/postmarket-context-tradability.md`.

Point-in-time external evidence: `tradebot/postmarket_external_context.py`
adds attributable option-implied-move and news observations plus exact-timestamp
Massive price reconciliation, dated SIC/reference context, Nasdaq Trader halt
evidence, and acceptance-bounded SEC filing SIC/XBRL facts in a separately
supervised, default-off worker. SEC facts join accessions to submission
acceptance and apply a 15-minute safety lag. They remain outside rank pending
holdout evidence. Missing credentials, bars, float shares, and sector-ETF
mappings remain explicit rather than inferred. See
`docs/postmarket-point-in-time-external-context.md`.

Licensed reference evidence: `tradebot/postmarket_reference_manifest.py`
strictly ingests operator-reviewed provider manifests for true sector
classification, sector-benchmark mapping, and optional float shares. The
append-only digest-bound contract enforces publication/observation causality
and never scrapes or infers the data. Context may materialize these facts as
shadow evidence, but they remain outside rank pending holdout evidence. See
`docs/postmarket-licensed-reference-manifest.md`.

### 5. Lifecycle and rank

- Append-only state transitions derived from completed bars only.
- An interpretable rank whose components and penalties are stored with the
  candidate. Historical qualification never masquerades as current actionability.

Lifecycle implementation: `tradebot/postmarket_lifecycle.py` directly tracks
every admitted market-wide candidate through the window, even after Stage 1
stops returning it. It stores distinct completed-bar observations plus
`NEWLY_QUALIFYING`, `CONFIRMED`, `STRENGTHENING`, `FADING`, `DEQUALIFIED`,
`REQUALIFIED`, and `CLOSED` transitions. See
`docs/postmarket-candidate-lifecycle.md`.

Rank implementation: `tradebot/postmarket_rank.py` writes immutable rank runs
and per-candidate decompositions bound to exact context, transition, and
completed-bar observation IDs. Version 2 retains version 1's named 100-point
weights while adding hard context/lifecycle binding and independent
context/quote freshness and technical data-confidence exclusions. It has
explicit penalties and hard exclusions, deterministic tie-breaking,
freshness-sensitive idempotency, a stored non-probability semantic label, and a
canonical contract digest covering formulas, weights, gates, upstream context
version, and ordering. Legacy unbound ranks remain historical only.
See `docs/postmarket-quality-rank.md`.

### 6. Empirical qualification

- Blinded labeling, walk-forward evaluation, cohort metrics, and miss review.
- At least the locked technical floors above; larger samples remain preferable.
- Shadow deployment, daily immutable audits, backups, restore drills, and final
  owner review before any customer delivery decision.

Rank qualification implementation: `tradebot/postmarket_empirical.py` locks
development/holdout sessions, the ground-truth eligibility definition,
selection rules, exact rank-contract digest, owner precision/sample floors, and
the technical recall floor before holdout results. Evaluation rejects mixed or
legacy-unbound rank contracts and binds every first-rankable source row into its
input digest. Strict digest-bound review manifests atomically append
rank-blind labels, and a digest-confirmed one-way unblind record freezes the
holdout before baseline-versus-rank evaluation. See
`docs/postmarket-rank-empirical-qualification.md`.

Aggregate campaign implementation: `tradebot/postmarket_evidence_campaign.py`
exclusively creates the immutable pre-session campaign, while
`tradebot/postmarket_evidence_gate.py` requires its digest and exact policy in
the final v2 evidence set. This prevents choosing a favorable date range,
threshold, provider, or report revision after outcomes are visible. See
`docs/postmarket-evidence-gate.md`.

Market-wide empirical reports are exported from their append-only database row
as immutable, digest-bound artifacts by
`tradebot.postmarket_empirical.export_empirical_report`. The export refuses
unknown revisions, digest/identity drift, and sealed holdout results, and enters
the same encrypted off-box audit archive used by the operational evidence.

Market-wide control implementation:
`tradebot/postmarket_discovery_controls.py` independently exercises the
discovery service's failure containment, default-off kill switch, and absence
of alert/order delivery dependencies. Its immutable artifacts have a separate
schema and cannot be replaced by the scheduled-earnings observer's controls.
See `docs/postmarket-discovery-control-evidence.md`.

Market-wide aggregate gate implementation:
`tradebot/postmarket_discovery_evidence_campaign.py` prospectively locks the
session range, empirical identity, exact canonical rank-contract digest,
policy floors, providers, datasets, and eligible revisions.
`tradebot/postmarket_discovery_evidence_gate.py` then
requires exact clean-session inventories, reconciled full-universe censuses,
separate independent-provider proofs, the matching empirical holdout, and all
four operational controls. The explicit-only
`tradebot/postmarket_discovery_evidence_set.py` sealer refuses post-hoc artifact
selection and publishes only a gate-passing immutable package. A pass is only
eligible for owner review and cannot enable delivery. See
`docs/postmarket-discovery-evidence-gate.md`. Prospective live evidence and
owner review remain unfinished gates.

Deployment preflight: `scripts/postmarket_signal_quality_preflight.py` verifies
the exact clean `origin/main` revision, live SQLite integrity, recent
digest-valid backup of all five signal-quality databases (including Stage-1
screening evidence in `universe.db`), evidence custody, disk headroom, shadow
switches,
independent-provider credential presence, exact-revision controls, and the
licensed reference contract without printing secrets or changing production.
It distinguishes safe shadow deployment from full evidence-campaign readiness.
See `docs/postmarket-signal-quality-preflight.md`.

Program progress ledger: `tradebot.postmarket_program_status` provides one
read-only, fail-closed status report across database integrity, ten clean
sessions, append-only outcome quality, full-universe censuses, independent
provider proofs, the locked empirical experiment, blinded labels, empirical
and calibration holdouts, independent customer reviews, and the final customer
review gate. It recomputes a claimed final gate from its exact digest-bound
inputs rather than trusting the verdict string. It never enables delivery and
returns a nonzero status until the complete inventory is eligible for a
separate owner review. See `docs/postmarket-program-status.md`.

Stage-1 retention prerequisite: `tradebot/screening_archive.py` publishes
deterministic SHA-256-addressed archives for every completed screening session,
with nightly catch-up before encrypted backup and full creation/restore
verification. It deletes nothing; pruning remains separately gated on real
archive and restore evidence. See `docs/screening-evidence-archive.md`.

Customer-readiness dry run:
`tradebot/postmarket_delivery_readiness.py` provides a pure, default-off,
evidence-bound policy decision for the isolated dry-run router. Policy v2
requires exact owner authorization, lifecycle/rank freshness, a candidate-level
projection through the exact frozen holdout-qualified calibrator, calibrated
observed-quality and coverage floors, clean operations, allowed provenance, and
a disengaged independent kill switch. It imports no provider, outbox, alert, or
trading path and cannot send anything. See
`docs/postmarket-customer-delivery-readiness.md`.

The companion `tradebot/postmarket_delivery_dry_run.py` ledger atomically
records distinct suppressed/eligible decision states and enforces at most one
eligible row for a deterministic idempotency key. It remains offline and has
no production outbox or customer-delivery dependency.

The default-off supervisor and its independent immutable control suite live in
`tradebot/postmarket_delivery_dry_run_shadow.py` and
`tradebot/postmarket_delivery_dry_run_controls.py`. The controls inject stale,
degraded, unauthorized, and revision-mismatched cases; prove database-level
eligible-decision deduplication, delivery/provider isolation, the kill switch,
and rollback; and refuse false attribution to dirty or mismatched code. These
artifacts establish dry-run operability only and cannot authorize customer
delivery.

The dry-run supervisor persists exchange-close-anchored cycle truth in
append-only tick and tick-to-decision tables. Scheduled lag, total latency,
conservation, operational reasons, exact rank provenance, and cross-release
link invariants make clean-session coverage independently auditable instead of
inferring it from a process heartbeat.

Every eligible route also has an append-only calibration link. Daily audit v2
reproduces that link through the exact projection, frozen model, passing
canonical holdout report, rank row, and candidate. Blinded review case v3,
campaign v3, preflight, and the final aggregate gate all bind the same model
identity. Missing, stale, substituted, or unattributed calibration evidence
fails closed. Customer delivery remains unimplemented and disabled; prospective
clean sessions, independent case review, owner activation, and a separate
delivery release gate remain required.

## Anti-goals

- Do not tune thresholds against one exciting session.
- Do not optimize alert count, raw move size, or backtest return in isolation.
- Do not fabricate prices for missing bars or silently forward-fill stale data.
- Do not average away poor small-cap, low-liquidity, or provider-failure cohorts.
- Do not introduce a black-box model before the outcome and recall ledgers exist.

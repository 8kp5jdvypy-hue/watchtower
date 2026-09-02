# Tiingo licensing decision — September 2026

**Date:** 2026-09-01  
**Status:** Approved direction; subscription and integration not yet activated  
**Owner/approver:** Perch Markets operator

## Decision

Perch may use Tiingo only as an independent, backend **EOD evaluation** source.
The intended starting plan, if that narrower evaluation is approved, is the
monthly Internal Commercial plan at $50/month. Tiingo-derived customer output
must remain derived-only: alerts, rankings, factor scores, evaluation metrics,
model signals, and other non-reconstructable summaries. Perch will not expose
raw Tiingo quotes, price tables, price charts, or downloadable raw records.

Tiingo does **not** satisfy Perch's immutable bulk full-universe intraday recall
proof. Its documented per-symbol intraday source is not the next-day bulk
object with immutable provenance required by that contract. Tiingo therefore
cannot complete `INDEPENDENT_PROVIDER_PROOFS`, cannot make a discovery session
evidence-eligible, and must not be represented as the provider that unlocks the
ten-session campaign.

The $250/month Standard Startup Redistribution plan, the $100/month
Fundamentals add-on, and an annual commitment are deferred until evidence from
the monthly evaluation shows that they solve a measured product requirement.

## Context and rationale

Perch needs independent sources for two distinct jobs: EOD outcome/reference
evaluation and immutable intraday full-universe recall proof. Tiingo Sales
confirmed in an email received 2026-09-01 that Perch's described derived-only
SaaS model qualifies for the Internal Commercial plan. That plan is materially
less expensive than raw-data redistribution and can support the first job. It
does not satisfy the second job's bulk-object, postmarket-window, or immutable
provenance requirements.

This source is an evaluation and evidence layer, not an automatic improvement
to live detection. The existing real-time pipeline remains responsible for
timely signals until a separately licensed, measured, and qualified live-feed
change is approved.

## Provider clarification retained as evidence

Tiingo Sales stated that the Internal Commercial plan:

- costs $50/month or $499/year and covers up to two raw-feed backend users;
- permits customer-facing derived alerts, rankings, factor scores, model
  signals, and recall metrics without counting external SaaS users as seats;
- permits backend storage and caching for validation, backtesting, and recall
  benchmarking while the subscription is active, with no fixed cache TTL;
- requires deletion of stored raw Tiingo data promptly after cancellation;
- permits permanent retention of non-reconstructable derived outputs that
  cannot recreate or substitute for the underlying Tiingo service;
- includes historical EOD OHLCV, corporate actions, security-master
  identifiers, active/delisted status, permanent identifiers, and basic shares
  outstanding;
- does not include free float or point-in-time historical security-master
  revisions; and
- requires standard Tiingo attribution when a data source is cited.

Tiingo also stated that sector and industry classifications require the
Fundamentals add-on, quoted at an additional $100/month for a startup under
five full-time employees. Raw customer-facing prices, tables, or charts require
the $250/month redistribution plan and a bilateral agreement. The provider's
published Terms of Service govern the standard subscription and supersede the
email as the legal instrument.

The operator's screenshots of the Tiingo Sales reply are the source evidence.
The outbound inquiry was sent through Resend with message ID
`977f529a-fba1-492c-826a-9b6c16797fa7`.

## Alternatives considered

- **$250/month redistribution immediately:** rejected for now because Perch
  does not need to expose Tiingo raw prices and should not pay for unused
  display rights.
- **$499/year Internal Commercial plan:** deferred until the first month proves
  coverage, reliability, and integration value.
- **Fundamentals add-on:** deferred because it does not provide free float or
  historical point-in-time security-master revisions. It could supply licensed
  current sector/industry classification for sector-relative shadow context,
  but it still would not satisfy the intraday full-universe recall proof.
- **Treat Tiingo as the recall-proof provider:** rejected because it lacks the
  required immutable next-day bulk postmarket object and the shipped provider
  registry therefore fails it closed for that purpose.
- **No recall-proof-capable independent provider:** not acceptable for the
  evidence campaign because it leaves full-universe recall dependent on the
  same source being evaluated. Shadow collection may continue, but those
  sessions cannot count toward customer readiness.

## Protected implementation boundaries

1. Tiingo credentials and raw responses remain server-side and outside logs,
   APIs, customer exports, and browser payloads.
2. Tiingo facts must be provenance-tagged and isolated from Alpaca display and
   detector-feed contracts.
3. Raw Tiingo records require a deletion mechanism tied to subscription
   cancellation; eligible derived evaluation evidence may remain.
4. Provider data cannot enter production ranking logic until it passes a
   locked replay, walk-forward evaluation, and holdout gate.
5. The subscription invoice, applicable Terms snapshot, this clarification,
   and a licensed-reference manifest must be archived before enabling the
   evidence campaign.

## Consequences and next steps

- Complete the production discovery-audit memory verification before adding a
  new provider workload.
- Subscribe monthly only after the VPS is stable.
- Do not purchase Tiingo on the assumption that it unlocks the full campaign.
- If the operator approves the narrower EOD evaluation, implement an EOD-only
  Tiingo adapter and a raw-data deletion inventory.
- If the Fundamentals add-on is approved for sector context, generate and
  archive its license/reference manifest before enabling external context.
- Separately obtain and qualify a recall-proof-capable provider with completed
  postmarket minute bars, full-universe coverage, and immutable bulk-object
  provenance before locking the ten-session evidence campaign.
- Compare any activated Tiingo evaluation against the primary pipeline for at
  least 30 calendar days, then decide whether to continue, upgrade, or remove
  the integration.

## Review triggers

Review and supersede this decision before any of the following:

- displaying Tiingo raw prices, charts, tables, or downloadable records;
- changing Tiingo plans, adding real-time or Fundamentals data, or committing
  annually;
- changing the published Tiingo Terms of Service;
- ending the subscription or changing raw-data retention behavior; or
- using Tiingo-derived fields as production ranking inputs.

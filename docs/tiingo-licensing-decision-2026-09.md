# Tiingo licensing decision — September 2026

**Date:** 2026-09-01  
**Status:** Approved direction; subscription and integration not yet activated  
**Owner/approver:** Perch Markets operator

## Decision

Perch will use Tiingo only as an independent, backend reference source for a
30-day signal-quality evaluation. The intended starting plan is the monthly
Internal Commercial plan at $50/month. Tiingo-derived customer output must
remain derived-only: alerts, rankings, factor scores, recall metrics, model
signals, and other non-reconstructable summaries. Perch will not expose raw
Tiingo quotes, price tables, price charts, or downloadable raw records.

The $250/month Standard Startup Redistribution plan, the $100/month
Fundamentals add-on, and an annual commitment are deferred until evidence from
the monthly evaluation shows that they solve a measured product requirement.

## Context and rationale

Perch needs an independent source to measure full-universe recall, validate
candidate and ranking outcomes, normalize corporate actions, and distinguish
pipeline misses from primary-feed limitations. Tiingo Sales confirmed in an
email received 2026-09-01 that Perch's described derived-only SaaS model
qualifies for the Internal Commercial plan. That plan is materially less
expensive than raw-data redistribution and is sufficient for the immediate
evaluation purpose.

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
  historical point-in-time security-master revisions, and sector data is not
  required to begin independent EOD recall validation.
- **No independent provider:** rejected because it leaves recall and outcome
  quality dependent on the same source being evaluated.

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
- Implement an EOD-only Tiingo adapter and a raw-data deletion inventory.
- Generate and archive a license/reference manifest before enabling the
  external-context campaign.
- Compare Tiingo against the primary pipeline for at least 30 calendar days,
  then decide whether to continue, upgrade, or remove the integration.

## Review triggers

Review and supersede this decision before any of the following:

- displaying Tiingo raw prices, charts, tables, or downloadable records;
- changing Tiingo plans, adding real-time or Fundamentals data, or committing
  annually;
- changing the published Tiingo Terms of Service;
- ending the subscription or changing raw-data retention behavior; or
- using Tiingo-derived fields as production ranking inputs.

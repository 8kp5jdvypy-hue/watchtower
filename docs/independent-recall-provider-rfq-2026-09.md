# Independent recall-provider RFQ — September 2026

**Status:** Draft only; not sent and not purchasing authority

**Owner/approver:** Perch Markets operator

**Purpose:** Qualify a second source for immutable full-universe postmarket
recall evidence. This is not a request for raw-data redistribution rights.

## Required evidence contract

A provider is usable for Perch's ten-session evidence campaign only when the
provider and licensed dataset satisfy every capability below:

1. Completed one-minute bars for the full Reg NMS US-equity universe.
2. Coverage through the completed 4:00–8:00 PM America/New_York postmarket
   window, including inactive/delisted symbols when applicable to the session.
3. One next-day bulk snapshot whose identity can be retained: dataset, object
   key, object version or ETag, last-modified time, byte count, and a digest of
   the exact rows Perch used.
4. A documented availability time after which the object is complete. Late
   corrections and replacement/version semantics must be explicit.
5. Commercial rights for internal validation in a customer-facing derived-only
   SaaS product. Perch will not redistribute the provider's raw records.
6. Permission to retain non-reconstructable derived evidence permanently:
   recall counts, disagreement classes, aggregate metrics, run identities,
   digests, rankings, model weights, and evaluation reports.
7. Written raw-data caching, retention, and post-cancellation deletion terms.
8. Startup pricing for a company with fewer than five employees and, if
   available, a bounded evaluation period or representative sample object.

Point-in-time sector classification is useful but optional for this particular
contract. If offered, the quote must separately identify its classification
system, effective/publication timestamps, symbol-history semantics, license,
and price. Sector data does not substitute for the eight recall requirements.

## Provider email draft

**Subject:** Startup licensing request — immutable US-equity postmarket bulk data

Hello Sales/Data Licensing,

Perch Markets is an early-stage market-intelligence company with fewer than
five employees. Website: https://perchmarkets.com

We are evaluating an independent US-equity reference source for internal
signal-quality validation. Our customer product remains derived-only: alerts,
rankings, recall statistics, and model outputs. We do not intend to expose or
redistribute your raw quotes, bars, tables, charts, or downloadable records.

We need written confirmation and pricing for a dataset with all of the
following properties:

- completed one-minute bars for the full Reg NMS US-equity universe;
- coverage through the completed 4:00–8:00 PM New York postmarket window;
- delivery as a next-day bulk snapshot with durable object identity or version,
  ETag, last-modified time, byte count, and documented correction semantics;
- commercial internal-validation rights for a customer-facing derived-only
  SaaS product;
- permission to retain non-reconstructable derived evidence permanently; and
- explicit raw-data retention and deletion terms.

Please also confirm the dataset's normal availability time, history depth,
inactive/delisted-symbol coverage, rate or egress limits, and startup pricing
for a company with fewer than five employees. If you offer point-in-time sector
classification, please quote it separately and describe its effective-date and
symbol-history semantics.

We would appreciate a representative sample object or a short evaluation path
so we can verify completeness, timestamps, object provenance, and correction
handling before purchasing. We can share our exact machine-readable acceptance
contract if useful.

Thank you,

Perch Markets

https://perchmarkets.com

## Operator acceptance checklist

No credential is installed and no campaign is locked until the operator has
archived the quote or agreement and recorded each item below.

| Item | Required proof | State |
|---|---|---|
| Commercial internal-validation rights | Executed agreement or governing terms plus provider clarification | Pending |
| Derived-output retention | Written permission and non-reconstructability boundary | Pending |
| Raw-data lifecycle | Cache/retention/deletion obligations | Pending |
| Completed intraday bars | Schema and sample object inspection | Pending |
| Full-universe snapshot | Sample symbol inventory reconciled to the session universe | Pending |
| Postmarket coverage | Sample contains completed bars through 8:00 PM ET | Pending |
| Immutable object provenance | Stable key/version/ETag/last-modified/bytes available | Pending |
| Correction semantics | Written version/replacement schedule and replay behavior | Pending |
| Production qualification | Adapter test, bounded production dry run, and operator sign-off | Pending |
| Startup price | Written monthly/evaluation quote and cancellation terms | Pending |

## Fail-closed rollout

1. Archive the legal grant, invoice/quote, terms snapshot, dataset
   documentation, and sample-object identity off-box.
2. Configure credentials only on the server; never print or commit them.
3. Run the provider adapter against a bounded historical session and reproduce
   its selected-row digest before enabling scheduling.
4. Keep external context, customer dry-run, operator alerts, and all customer
   delivery off.
5. Import any separately licensed sector manifest and prove a coherent
   feature/lifecycle/rank chain.
6. Prospectively lock the empirical experiment and evidence campaign before
   the first covered session opens. Never backdate a campaign.
7. Count a session only after its clean discovery audit, outcome-quality
   report, full-universe census, independent-provider proof, and all required
   immutable identities pass.

Until every acceptance row is proven, the provider remains unqualified and
shadow health must not be described as customer-readiness evidence.

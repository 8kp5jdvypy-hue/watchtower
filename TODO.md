# Perch — operator to-do

Checklist of things **only you can do** (sudo prompts, GitHub auth,
account signups, Telegram account). Everything else is already wired up.
Tick a box with `[x]` as you go.

---

## Done — the laptop-era checklist

These no longer apply now that the bot runs on a VPS instead of this
Mac (2026-08-10):

- [x] Push the code to GitHub — `main` is the default branch, fully
  pushed and up to date.
- [x] Move off the laptop onto a VPS — DigitalOcean droplet at
  `67.207.83.138`, all 5 services (`worker`/`bot`/`runner`/`api`/`caddy`)
  running via `docker compose`, `perch.service`/`perch-backup.timer`
  enabled so it survives a reboot.
- [x] Point `api.perchmarkets.com` at the VPS — DNS-only record in
  Cloudflare, Caddy has a real Let's Encrypt certificate.
- ~~Schedule the pre-open wake~~ / ~~keep the Mac on AC power~~ / ~~morning
  health check on the laptop~~ — moot, nothing runs on this laptop
  anymore. (`scripts/status.sh` still works if you ever want to check
  this machine specifically, but it's no longer the live host.)

## Done — web dashboard (2026-08-10)

- [x] **Deploy `web-app/`** to its own Cloudflare Workers project
  (`perch-dashboard`) — live at `app.perchmarkets.com`.
- [x] **Resend domain verified**, `RESEND_API_KEY` / `RESEND_FROM_EMAIL`
  set on the server. Confirmed working end-to-end: magic-link email
  sent, received, clicked, logged into the real dashboard.

The full surface is live: `perchmarkets.com` (marketing), `app.perchmarkets.com`
(dashboard), `api.perchmarkets.com` (backend), plus the Telegram bot —
all pointing at the one VPS.

## Before the social-media launch

- [ ] **DM the bot `/start` from your own Telegram account** (not a
  channel) and walk the full onboarding once, as a user would see it.

- [ ] **Document trades honestly**, including the losers — the whole
  design (status page, weekly recap, coin-flip disclosure) depends on the
  public record matching reality.

## Ongoing, on the server now (not this laptop)

- [ ] Occasional health check: `ssh root@67.207.83.138`, then
  `cd /opt/perch && docker compose ps` — want all 5 services `Up`.

## Active — signal-quality program

The durable acceptance contract and ordered workstreams live in
`docs/signal-quality-program.md`. This remains shadow-only until its empirical
and operational gates pass.

- [ ] Append-only postmarket outcome marks and quality reports.
  - [x] Provider-free mark schema, completed-bar semantics, MFE/MAE, and tests.
  - [x] Bounded automatic backfill, explicit fetch failures, and immutable reports.
  - [ ] Deploy in shadow, verify live marks/reports, and accumulate eligible samples.
- [ ] Per-stage latency and missed-cycle attribution.
  - [x] Append-only schedule/stage timing ledger and drift-free anchored polling.
  - [x] Persistence-span timing, heartbeat fields, and audit-v3 reconciliation.
  - [ ] Deploy in shadow and verify a complete clean timing session.
- [ ] Independent full-universe recall census and miss report.
  - [x] Bounded full-universe completed-bar replay and append-only attempts.
  - [x] Stage-1 false-negative reasons, detection delay, recall, and miss reports.
  - [x] Add next-day full-universe Massive SIP flat-file comparison with
    independent recall, eligible-pair agreement, and exact-bar divergence.
  - [ ] Configure Massive S3 credentials and verify live census load/entitlement.
- [ ] Volatility, relative-strength, tradability, catalyst, and confidence features.
  - [x] Append-only ATR, SPY-relative, quote/depth, liquidity, asset, and catalyst evidence.
  - [x] Explicit unavailable states and bounded retryable enrichment orchestration.
  - [x] Add pre-close implied-move, news, exact second-provider candidate bars,
    dated SIC/market-cap/share context, and official halt evidence.
  - [x] Add acceptance-bounded SEC filing SIC and XBRL fundamentals with a
    conservative dissemination lag and exact accession provenance.
  - [x] Add a strict append-only contract for licensed point-in-time float
    shares and true sector/ETF mapping; do not infer either from SEC
    public-float value or SIC.
  - [ ] Contract with/configure an authorized provider, ingest live manifests,
    and verify candidate bindings in shadow.
- [ ] Candidate lifecycle transitions and interpretable versioned rank.
  - [x] Direct off-screen tracking and append-only completed-bar observations.
  - [x] Versioned qualifying, confirmed, strengthening, fading, dequalified,
    requalified, and closed transitions with explicit actionability.
  - [x] Deterministic decomposable evidence rank and unavailable-data penalties.
  - [ ] Empirically tune or replace heuristic weights only on walk-forward data;
    never present version 1 as probability, confidence, or expected return.
- [x] Append-only rank-blind labels, locked walk-forward splits/rules, explicit
  holdout unblinding, and baseline-versus-rank empirical metrics.
  - [x] Lock the numeric eligibility definition, atomically ingest strict
    digest-bound blinded review manifests, and require inventory-digest
    confirmation before irreversible holdout unblinding.
  - [x] Export the exact persisted empirical run as an immutable,
    revision-attributed, digest-bound artifact covered by restore tests.
- [x] Prospectively lock aggregate evidence dates, policy floors,
  feeds/providers, and eligible audit/observer revisions before the first
  covered session; require its digest in the final evidence package.
- [ ] Accumulate independently reviewed holdout labels, complete provider
  comparison, satisfy the aggregate evidence gate, and obtain owner review.
- [x] Add append-only point-in-time option-implied-move/news evidence in an
  isolated default-off enrichment worker.
- [x] Implement isolated Massive candidate-price/reference and Nasdaq Trader halt
  adapters; never substitute same-provider feed comparison.
- [ ] Configure the licensed Massive REST key in production and validate
  candidate-level entitlement/agreement in shadow.
- [ ] Configure separate Massive S3 flat-file credentials and accumulate clean
  next-day full-universe provider-proof reports.

## Nice to have / later

- [ ] Ask me to wire `status.sh` (or an equivalent on the server) into a
  monitor that pings you if the stack goes down.

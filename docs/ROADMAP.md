# Perch Markets Roadmap

## Strategy (governs all prioritization)

- Positioning: premium niche product — win on price and trust, not
  volume. "The honest one" — every feature and marketing decision must
  be congruent with published, unedited track-record transparency.
- Pricing thesis for Q4: anchor real tier at $75-150/mo; annual billing
  with real discount from day one. No lifetime deals, no broker
  affiliate revenue, no discount-code influencer marketing — ever.
  These are standing rules, same weight as "no fabricated data."
- Growth engine: the journal is the content factory. Weekly public
  recap (all alerts + outcomes, unedited, misses included) posted on a
  schedule; public track-record page as the destination. Free tier =
  delayed recap; paid = real-time.
- Future ceiling (do not build yet, do not foreclose): API access to
  the signal feed for prop shops / small funds at premium pricing.
  Mention in the Alpaca scoping conversation — it affects licensing.
- No options/crypto expansion beyond the existing design doc until the
  equity SIP-era record is proven.

## Now (Aug, week of 11th) — migration + honesty

- [ ] Off-box backups with tested restore (ops #1) — before everything
- [ ] broad_scan honesty fix: proposal, then implementation (labeling,
      stats exclusion designed alongside Decision B filtering, copy
      fixes)
- [ ] Extended 60-90 session A/B backtest → Decision A evidence
- [ ] Merge open branches per agreed sequence
- [ ] HUMAN: VPS hardening (non-root deploy user, key-only SSH)

## Week 2 (Aug 18-24) — SIP cutover

- [ ] Phase 3: config + data_feed column, session-boundary flip,
      first-session observation checklist, pinned rollback criteria
- [ ] Ops #2/#3 fixes (deadman gap, autoheal)
- [ ] HUMAN: subscriber DMs (what do you act on / ignore / pay more
      for)

## Weeks 3-4 (late Aug - early Sep) — comprehension + retention core

- [ ] Beginner-clarity brief (v4)
- [ ] Alert-aftermath recap feature: daily/weekly outcomes from the
      journal — the retention engine AND the weekly public content
      source
- [ ] Cleanup backlog (#6-10)
- [ ] HUMAN: dashboard walkthrough recording; Alpaca scoping email
      (include the future API-access question)

## September — retention + revenue mechanics

- [ ] Engine investigation: MEDIUM tier continuation significantly
      <50% on both feeds (Decision A proposal, n=83) — investigate
      what MEDIUM measures and whether it should be re-derived,
      reframed as non-directional, or merged into LOG. Evidence-first,
      no changes without approval. Separate proposal required.
- [ ] Alert preferences: digest / quiet hours / HIGH-only (churn
      defense)
- [ ] Weekly market-recap note (semi-automated)
- [ ] Premium unification brief (v7)
- [ ] Watchlist expansion review from SIP-era broad_scan hit history
- [ ] Pricing tiers drafted per the strategy above + Alpaca answer +
      subscriber DM findings
- [ ] HUMAN: Alpaca broker-account arrangement conversation (deadline)

## October — pre-launch

- [ ] Public track-record page (needs ~6 weeks SIP-era data): every
      alert, unedited, with small-sample tags — marketing centerpiece
- [ ] Weekly public recap posting begins (X + communities, scheduled)
- [ ] Landing 10/10 brief (v3) remaining items + launch copy
- [ ] Full copy/compliance sweep vs no-advice-language guardrail
- [ ] Ops re-check before traffic ramp
- [ ] HUMAN: securities lawyer hour — terms, disclaimers, track-record
      page claims. Non-negotiable before Q4-scale billing.

## Q4 — launch

- [ ] Tiers live (premium anchor per strategy), annual billing,
      broker-account data arrangement executed
- [ ] Marketing ramp centered on the track-record page
- [ ] Post-launch queue: options/crypto build-out (v6), new detectors,
      API-access product exploration

## Standing rules

- Engine/threshold changes: evidence + separate approval only
- Archives, never deletes; proposals before code; docs live in the
  repo
- Sequencing gate: no September retention work until the SIP flip has
  survived its first full week

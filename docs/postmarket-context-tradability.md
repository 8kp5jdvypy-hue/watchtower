# Postmarket context and tradability evidence

Candidate qualification remains a completed-bar price/volume decision. Context
enrichment is an append-only shadow step after qualification; it cannot change
the candidate, rank it, send it, or place an order.

## Evidence captured

Each candidate receives a versioned row in `postmarket_candidate_context`:

- trailing 14-session ATR, ATR as a percent of the RTH close, and move size in
  ATR units, using only daily bars before the event session;
- SPY's move at the candidate's first knowable instant, raw relative movement,
  and excess movement in the candidate direction;
- when a locked licensed point-in-time reference row was already knowable at
  detection, the mapped Select Sector ETF's completed-bar move and the
  candidate's sector-relative move, bound to the manifest ID, SHA-256 digest,
  and source observation time;
- point-in-time SIP NBBO timestamp, temporal distance from detection, spread in
  basis points, bid/ask size, and quoted-depth notional;
- session RTH share volume and typical-price dollar volume, plus the candidate's
  cumulative postmarket notional;
- asset-catalog observation time, exchange, active/tradable state, options
  eligibility, and overnight eligibility;
- verified scheduled-earnings, SEC-filing, and macro ledger facts, including
  their source and ingestion timestamp; or `UNEXPLAINED` when none exists; and
- the completed-bar quality gate that the candidate already passed.

Bars retain provider, feed, and timeframe provenance. Quotes separately retain
their provider, SIP feed, and timestamp. A quote more than 180 seconds from the
candidate detection is stored as `TEMPORALLY_MISMATCHED`; its spread is
historical evidence about the fetched quote, not claimed as the signal-time
spread.

## Missing-data boundaries

Perch does not ship with or infer a sector/float dataset. When an operator has
ingested a provider-authorized manifest under the strict contract in
`docs/postmarket-licensed-reference-manifest.md`, context version 2 can use its
pre-detection sector mapping and optional float fact. Without that manifest,
or without a completed mapped-ETF bar knowable at detection, those features
remain explicitly unavailable. Market cap, halt state, options-implied expected
move, guidance, general news, regulatory actions, and analyst actions likewise
remain explicit `UNAVAILABLE` or `UNCONFIGURED` states. Price movement is never
used to invent a catalyst or sector.

`status=complete` means the enrichment attempt completed without an operational
fetch failure; it does not mean every desired feature was available. Named
status fields and `issues_json` carry feature completeness. Provider failures or
missing required bar/benchmark/quote/asset responses produce append-only
`degraded` attempts and may retry up to three times.

The service processes at most 100 pending candidates per pass. It bulk-fetches
only candidate daily bars, candidate/SPY intraday bars, any mapped sector ETF
named by an eligible licensed row, and candidate quotes. The latest-session
heartbeat summary exposes missing, complete, and degraded context counts and
per-feature availability, including sector-relative coverage.

## Safety and future use

This ledger is evidence for empirical qualification. Rank version 1 does not
consume the sector-relative or float fields; adding them to a score requires a
locked walk-forward/holdout result and a separate reviewed version change.
Ranking must penalize or reject unusable critical inputs and may not reinterpret
`UNAVAILABLE` as zero or favorable. Fundamentals, halt-state, news, and
implied-move sources still require separate adapters, provenance tests,
point-in-time replay rules, and owner approval before their statuses can become
available.

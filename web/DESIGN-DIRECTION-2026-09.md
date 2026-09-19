# Perch web + dashboard redesign — direction record (2026-09-18)

**Status:** approved direction (owner delegated the choice: "creative, unique,
match the mobile app, new logo"). Supersedes the dark-terminal landing that
shipped in August. The iOS app's real tokens (`perch-mobile-mvp/src/theme.ts`)
are the shared system: near-black ground, one cyan, one amber, Space Grotesk
display, JetBrains Mono utility. `DESIGN.md` in the app repo (field paper /
amber / serif) was an earlier essay the app did not ship; its one idea we keep
is the **observation rail**.

## Assignment

- **Subject:** Perch — a market scanner that watches a fixed list of US
  equities on 5-minute bars, notices unusual intraday behaviour, explains the
  evidence in plain language, and publishes its own track record including
  losing streaks. It never places orders.
- **Audience:** self-directed US equity investors and active traders who are
  tired of being told what to buy.
- **Page job (site):** make one person believe Perch notices things worth a
  closer look and is honest about how often it's wrong — then sign in.
- **Page job (dashboard):** show today's observations and their evidence
  faster than any chart app, without pretending to be a terminal.
- **Brand character, concretely:** a field instrument, not a trading desk.
  Quiet, exact, a little dry. Attention is amber; it is never profit-green.
  Says "not a mockup" and means it.
- **Unique material:** the real journal (public `/public/track-record`: 82
  alerts, 47.8% hit rate, longest losing streak 5 — published), the 5-minute
  bar, ATR units, session phases from the exchange calendar, the scanner's
  heartbeat, real app screens.
- **Constraints:** React + Vite on Cloudflare Workers static assets; no
  gated fonts (Space Grotesk + JetBrains Mono self-hosted, already there);
  reduced-motion edition must be complete; no scroll-jacking; keyboard
  complete; LCP without WebGL.

## What was studied

Seven Awwwards SOTD winners with jury scores (from the FirstPounce study,
`marketplace-deal-intelligence/docs/research/awwwards-references-2026-09-18.md`):
Navigate 7.93, Igloo 7.92 (Site of the Year 2024), Oryzo 7.86, MindMarket
7.85, Jeton 7.55, Kriss 7.45, Ctrl 7.28. Added: **Cash App Brand
Guidelines** — SOTD 17 May 2025, 7.6 / 6.94 / 7.56 / 7.67 / **7.4**. Plus the
ten fintech brand sites most cited for design in 2026: Stripe, Wise,
Robinhood, Cash App, Coinbase, Monzo, Nubank, Klarna, Mercury, Plaid.

What the winners share, and what each one costs:

1. **Two colours.** Every SOTD is one ground + one accent. Perch: `#05070A`
   + `#34E2FF`; amber `#FFB454` only for "closer look".
2. **One signature interaction, repeated with discipline.** Navigate's
   pause-loops, MindMarket's card stack, Oryzo's reveals.
3. **The product is demonstrated.** Jeton's real screens; Mercury's ungated
   sandbox. Perch has something better: real alerts with real outcomes.
4. **Scroll is choreography.** Igloo's single continuous scroll.
5. **Numbers as proof.** Jeton "1M+". Perch's numbers include the losses —
   nobody else on the list does that.
6. **Type carries it.** Ctrl, Igloo, Oryzo; Plaid's mono captions; Coinbase's
   transit-signage layouts.
7. **Usability is every winner's weakest score (6.9–7.5).** A product site
   wins there: fast, accessible, real CTAs, reduced-motion done properly.
8. **A loader and a footer moment**, small.

## Three directions

### A — The Watch Log (chosen)
*Thesis:* the site is the instrument. One continuous scroll structured as a
trading session — premarket, open, close, after hours — with a **session
rail** down the page: a vertical time scale that advances with scroll and
carries the real alerts from the public journal pinned at their real times.
The hero is a sentence and a live status line, not a chart. Data is the hero
the way Navigate does it, but the data is Perch's own record.

- Colours: Ground `#05070A`, Surface `#10141B`, Line `#1C222C`, Ink
  `#EEF2F6`, Cyan `#34E2FF`, Amber `#FFB454`. Muted `#8B95A3`.
- Type: Space Grotesk 600 display at poster sizes; system sans body;
  JetBrains Mono for times, symbols, ATR units, status.
- Rhythm: 8px base; sections keyed to session phases, not "features".
- Material: the rail's ruled ticks; hairlines; real app screens; no
  gradients, no glass, no blobs.
- Motion: one orchestrated sequence — the rail — driven by ScrollTrigger;
  pause-loop hovers on pinned alerts; status line ticks. Nothing else fades
  in "because".
- **Signature moment:** the rail passes the open at 09:30, and the day's
  real alerts light up one by one as you scroll through the session, each
  with its recorded 30-minute outcome — including the misses.
- Risk: data-first can feel cold. Protection: the hero leads with a
  plain sentence; every number has a label; the reduced-motion edition shows
  the whole rail at rest.

### B — Field Notes
Type-first editorial (Ctrl/Igloo). Big statements, a rewriting headline
fed by real alert headlines, the app in a morphing phone. Strong, but it is
a style any research newsletter could wear — fails the specificity test.

### C — The Perch (current)
Spatial WebGL market field, ticker tapes, terminal chrome. It is what
ships today and it is what the app's own design notes reject as "another
financial dashboard built around oversized metrics." Not distinctive; costs
LCP.

**Decision: A.** It is the only direction that can't be un-Perched: swap the
name and the copy and the rail's alerts still say 09:31 ET, 5.3× ATR, MSFT,
down, missed. B and C survive the substitution.

## The mark

Per the FirstPounce logo study (`docs/BRAND-STUDY.md`): the letter is the
mark; one device that means something; one colour + black; must hold at
60 px; the icon is never the wordmark. The placeholder falcon fails the
first and last of those.

**P on the rail.** A bold geometric **P** whose stem is the observation
rail — faint ruled ticks up its left edge — and one **cyan flag** under the
bowl: the observation Perch noticed. At 16 px the ticks disappear and the P
+ flag holds. The flag is the same element as the live-status dot and the
"closer look" marker in the app, so the mark and the product share one
gesture. States: idle, scanning (ticks breathe), signal (flag pulses).

Wordmark: **PERCH** in Space Grotesk 600, +0.18 em tracking — already the
app's header. Lockup: mark, then wordmark. App icon: the mark alone on
`#05070A`. The same SVG geometry ships to `web/`, `web-app/`, and
`perch-mobile-mvp/src/components/PerchMark.tsx` (separate PR there).

## Protected after this pass

- The session rail and its pinned real alerts (site).
- The mark geometry and the cyan flag device.
- Copy claims: only numbers the public API returns; "not a mockup" stays
  true or comes out.

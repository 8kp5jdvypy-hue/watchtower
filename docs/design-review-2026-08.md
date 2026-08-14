# Design review — August 2026

**Scope:** perchmarkets.com (landing) and app.perchmarkets.com (dashboard, all tabs), as deployed and live on 2026-08-13. Report only — no code changes.

**Standard:** the landing page's premium level is the bar. Restraint executed perfectly, machined not decorated, nothing default-looking, one elevation system, numbers treated as the emotional center.

**Captures:** 97 screenshots under `docs/design-review-2026-08/captures/` (`landing/` 57 files, `app/` 40 files), at 1440×900 desktop and 390×844 mobile (2× DPR), including boot frames, full scroll sweeps, hover and keyboard-focus states, auth flow, every dashboard tab, the signal-detail modal, empty/quiet states, unlinked variants, and a reduced-motion pass. Findings cite capture filenames.

---

## 0. Live-vs-repo check (stale-deploy history)

**Both live surfaces are byte-identical to current main. No stale deploy.**

- app.perchmarkets.com's JS/CSS bundles hash-match `web-app/dist`, which postdates the last `web-app/src` commit.
- A fresh `vite build` of `web/` on main produces hashes byte-identical to the bundles perchmarkets.com serves.
- The Journal feature is confirmed absent from the live app bundle (the one "journal" string in it is Performance-tab copy), consistent with it living only on `worktree-trade-journal`.

Everything in this report therefore applies equally to the live sites and to main.

### Methodology caveats (read before trusting a screenshot)

- **Dashboard captures use the live deployed bundle with API responses served locally** from real rows in the dev `journal.db` (session 2026-08-05, 20 HIGH/MEDIUM detections, timestamps remapped to capture time; aggregates recomputed with replicas of `journal.py`'s queries). No SSH identity exists on this machine for the prod VPS, so a real signed-in session wasn't possible. Pixels, CSS, and components are exactly what production serves; the *numbers* are real-but-relocated journal data.
- Auth screens are the real live app against the real API (the login 401 path); only the magic-link request/verify POSTs were intercepted so no real emails, rate-limit hits, or funnel events were generated. No live CTAs were clicked anywhere, for the same reason.
- Captures ran ~23:51 ET, so session chrome shows `AFTER HOURS` and timestamps read "5h ago" — fixture artifact, not a bug.
- In mobile **fullPage** captures (e.g. `app-390-performance.png`) the fixed bottom nav paints mid-page. That is a screenshot artifact of fixed-position elements, not a real defect.
- The small ring at dead-center of some landing captures is the custom cursor parked at the headless browser's default mouse position — harness artifact.

---

## 1. Consistency map — the "same company" test

Where the two surfaces agree, they agree *deeply*: the dashboard's `index.css` opens with a comment that the palette "has to match exactly," and it does — `--bg/--bg-1/--bg-2/--line/--ink/--ink-dim/--ink-faint/--cyan/--cyan-dim/--amber` are identical hex-for-hex. The app even tokenizes the landing's easing curve (`--ease-perch: cubic-bezier(0.2,0.8,0.2,1)`) so its transitions match by construction. The card surface gradient on the landing's demo AlertCard and the app's real SignalCard is the same `linear-gradient(165deg, rgba(16,20,27,.9), rgba(10,13,18,.95))`. The mono-uppercase-eyebrow-with-pulsing-dot pattern, the amber "Small sample" pill, the quiet-cyan-means-Perch-noticed rule, and the elevation language (soft black drop + faint cyan rim on emphasis) are all shared. A visitor moving from landing to app unambiguously stays in the same product. **The divergences below are the residue, not the rule.**

| Dimension | Landing (`web/`) | Dashboard (`web-app/`) | Verdict |
|---|---|---|---|
| Color tokens | Full set | Same set, + `--up`/`--down` (documented as functional, not brand) | ✅ Pass |
| Reds | `--red #ff3b4e` (MarketCoverage "unusual" chips) | `--down #fb7185` (price direction, errors) | ⚠️ Two unreconciled reds |
| Type scale | Fluid modular scale `--step--1…--step-6` (clamp), body `--step-0` | **No scale tokens.** Fixed sizes per component; body 0.95rem; h1 1.5rem with inline-style overrides to 1.1rem | ⚠️ Biggest structural gap |
| Display face | Space Grotesk w/ variable weight 640 in hero, 600 elsewhere | Space Grotesk 600 | ✅ Close enough |
| Buttons | Primary: gradient cyan magnetic button, mono uppercase, radius 2px. Ghost: 1px line border | Primary: flat cyan, display face sentence-case, radius 6px (Login/Verify). Chips: cyan-outline radius 5px ("View signal", Retry). Neutral: line-border mono radius 5–6px (Sign out, Close, Got it, dismiss) | ⚠️ Two-and-a-half button languages |
| Corner radii | 2, 3, 4, 6, 8, 999 | 4, 5, 6, 8, 10, 12, 16, 999 | ⚠️ No shared radius scale |
| Cards | AlertCard: 165deg gradient, 1px line, cyan rim on hover | SignalCard: same gradient, same rim language, + is-high left accent bar | ✅ Deliberate mirror — keep |
| Lists | n/a | Watchlist: hairline-divided flush rows. Activity: each row its own `.card` | ⚠️ Two list grammars one tab apart |
| Layout grid | Edge-padded `.wrap` max 1240px | `.view` max 720px centered, but topbar/tab chrome left-anchored full-width | ⚠️ Content column floats between left-anchored chrome |
| Motion | Hardcoded values (same curve) | Tokenized durations + easing | ✅ Compatible; app is ahead |
| Eyebrow / section-label pattern | mono uppercase + pulsing dot | identical | ✅ Pass |
| Empty/quiet states | n/a | PerchMark + plain-spoken copy | ✅ Excellent, on-brand |
| Focus states | Custom cyan `:focus-visible` + skip link | Same outline; modal focus trap | ✅ Pass |
| Reduced motion | Per-section narrative fallbacks | Global kill + per-component opt-outs | ✅ Pass |
| Voice | "We watch. You decide." plain-honesty register | Same register everywhere incl. errors | ✅ Pass |

**Page-by-page:** Landing hero/manifesto/alert-sequence/pricing/footer — internally consistent, sets the bar. Auth screens (`app-1440-auth-login.png`, `-login-sent.png`, `-verify-confirm.png`, `-verify-error.png`, `-loading-shell.png`) — pass the same-company test cleanly; the ambient ticker field ties them to the landing's market-field motif. Today/Signals — pass (the card IS the landing's promised card). Watchlist vs Activity — fail each other (list grammars). Performance — passes visually but is where the missing type system shows (inline-styled h1s). Settings — passes; quietest page, appropriately so.

---

## 2. Findings

Effort: **S** < ~1h · **M** ~half-day · **L** multi-day. Every finding names its capture file(s).

### HIGH — undermines premium feel or trust

**H1. Mobile hero content is flush against the left viewport edge, with clipped elements.**
- Page/element: landing 390 — hero content block.
- Screenshot: `landing/landing-390-hero.png`.
- What's wrong: the h1, subhead, and SIGN UP button all touch x=0 with zero padding; the eyebrow's diamond marker is half-clipped off the left edge; the clock ("18:31:18") runs to the right edge; the hero kestrel is cropped by the right edge. `.wrap` should provide ~20px of edge padding at 390 (`--edge` clamps to 1.25rem), so something — most plausibly a GSAP x-tween on `.hero-fade`/`.hero-line-i` that doesn't resolve at mobile widths, or the boot-timeline's from-state — is shifting `.hero-content` left. This is the first screen a phone visitor sees, and it reads as broken.
- Fix: reproduce in a real mobile viewport, find the offsetting transform, and clear it (`clearProps` on timeline complete or a mobile-guarded tween). Verify the eyebrow marker and clock sit inside the edge padding afterwards.
- Effort: **S–M** (fix is small; finding the offending tween is the work).

**H2. App chrome paints (and stays clickable) on top of the open signal-detail modal.**
- Page/element: dashboard — SignalDetail overlay vs topbar/tabs/mobile nav.
- Screenshots: `app/app-1440-signal-detail.png` (topbar + tab row fully bright and undimmed above the overlay), `app/app-390-signal-detail.png` (bottom tab bar rendered over the sheet's lower edge).
- What's wrong: `.view` sets `position: relative; z-index: 1`, creating a stacking context. The overlay (`position: fixed; z-index: 50`) renders *inside* `.view`, so its effective stacking position is `.view`'s 1 — beneath `.tabs` (z-index 2) and `.mobile-nav` (z-index 10). The backdrop never dims the chrome, the mobile sheet is partially covered by the nav, and tab buttons remain clickable under an open modal (tapping one swaps the view behind the dialog). For the app's flagship moment — "card transitions into the intelligence view" — the frame is visibly wrong.
- Fix: render SignalDetail through a portal to `document.body`, or drop the `z-index: 1` from `.view` and re-verify AmbientField layering. One of the two; portal is the robust one.
- Effort: **S**.

**H3. Giant pinned headlines bleed through the fixed nav's translucent bar at desktop.**
- Page/element: landing 1440 — `.site-nav.is-scrolled` over pinned sections.
- Screenshots: `landing/landing-1440-footer.png` ("BE THERE" behind the nav), `landing/landing-1440-pricing-hover.png` ("beta." behind the nav), `landing/landing-1440-scroll-16.png` ("Free while").
- What's wrong: `rgba(5,7,10,0.75)` + 10px blur is not enough against `--step-6` display type in near-white; the headline reads through the bar and collides with the nav links, which reads as a z-index bug rather than translucency. It happens exactly at the pages' biggest, most-photographed moments (pricing, final CTA).
- Fix: raise scrolled-nav backdrop to ~0.88–0.92 opacity (or add a short top-down gradient scrim under it) and/or bump blur. Keep the transparent-at-top state as is.
- Effort: **S**.

**H4. Trend arrow and change % sit side-by-side in opposing colors with no explanation.**
- Page/element: dashboard — SignalCard symbol row, every card.
- Screenshots: `app/app-1440-today.png` (SPY red ▼ next to green +0.87%), `app/app-1440-signals.png` (BE green ▲ next to −9.14%; PLTR red ▼ next to +10.98%).
- What's wrong: the code is deliberate and internally documented — symbol+arrow are colored by the signal's directional call, the % by raw price movement since detection — but nothing on the card says so. A red down-arrow touching a green "+3.05%" is read by any market-literate user as a rendering bug, on the exact row where the product's numbers are supposed to be the emotional center. It's the most repeated element in the app, so the confusion compounds per card.
- Fix (pick one): suffix the figure ("+0.87% since alert" in `--ink-faint` mono), or move the change into the footer next to the timestamp with that label, or neutralize the % color and let the sign speak. Keep arrow semantics as they are.
- Effort: **S** for the label; **M** if the row is recomposed.

**H5. The flagship modal shows the same statistic with two opposite signs.**
- Page/element: dashboard — SignalDetail "Historical stats" line.
- Screenshots: `app/app-1440-signal-detail.png`, `app/app-390-signal-detail.png` ("avg follow-through **0.07%** (≈−0.15× ATR)").
- What's wrong: the % averages raw returns weighted by each entry's price; the ATR figure normalizes per-entry by ATR — with mixed samples they can genuinely disagree in sign. Mathematically defensible, visually indistinguishable from a bug, in the panel whose entire purpose is "trust our numbers." CLAUDE.md's own convention is ATR units over percentages.
- Fix: decide which representation leads (per project rules: ATR) and derive the other for display from the same per-entry series so signs always agree — or show only one. If both stay, a sign disagreement should suppress the parenthetical rather than print a contradiction.
- Effort: **M** (display-rule change in `signalHistory` interpretation + the API/fixture path that mirrors it).

### MEDIUM — real polish, visible payoff

**M1. The app has no type scale; headings are improvised per view.**
- Screenshots: `app/app-1440-performance.png`; code: `web-app/src/components/Performance.jsx:58,82` (`<h1 style={{ marginTop: '1.75rem', fontSize: '1.1rem' }}>`), `Views.css` h1 1.5rem.
- What's wrong: the landing's `--step-*` fluid scale never crossed over. Performance uses three `h1`s per page, two shrunk by inline styles — a semantics problem (multiple h1s, wrong outline for AT) and the reason section headings ("By signal type", "HIGH-tier track record") don't sit on any rhythm. Every future view will improvise the same way until a scale exists.
- Fix: port a trimmed `--step-*` set into `web-app/src/index.css`, add a `.view h2`/`.view-section-title` style, demote the inline-styled h1s to h2s. Effort: **S**.

**M2. Button vocabulary differs between surfaces (and within the app).**
- Screenshots: `landing/landing-1440-hero.png` (gradient mono-uppercase SIGN UP, radius 2) vs `app/app-1440-auth-login.png` ("Send magic link", flat cyan, display face, radius 6) vs "View signal" outline chips vs "Sign out"/"Close"/"Got it" neutral chips.
- What's wrong: each individual button is fine; collectively there is no primary/secondary/tertiary spec, and the radii wander (2/5/6/10/12/16). The marketing site promises one machine; the app's controls are from a slightly different machine.
- Fix: write the three-tier button spec once (primary = cyan fill, secondary = cyan outline, tertiary = line outline; one radius token, one type treatment) and sweep both apps to it. The landing's gradient magnetic CTA can stay a landing-only hero flourish, documented as such. Effort: **M**.

**M3. Two list grammars one tab apart.**
- Screenshots: `app/app-1440-watchlist.png` (hairline-divided flush rows) vs `app/app-1440-activity.png` (one bordered card per trade).
- What's wrong: same data shape (symbol left, number right), two treatments. Watchlist's rows are the more machined of the two.
- Fix: converge Activity onto the `wl-row` grammar (or extract a shared `.data-row`). Effort: **S–M**.

**M4. Watchlist price column doesn't align as a column.**
- Screenshot: `app/app-1440-watchlist.png`.
- What's wrong: prices right-align against variable-width badges (`SIGNAL` pill vs `QUIET` text), so the figures land ragged — $776.45 / $724.76 / $331.28 end at different x positions. For a product whose numbers are the emotional center, tabular figures should stack into a true column.
- Fix: fixed-width badge slot (or `grid-template-columns: 1fr auto max-content` with a min width on the badge cell). Effort: **S**.

**M5. Feed reads templated: eyebrow and headline repeat the same fact 19 times.**
- Screenshot: `app/app-1440-signals.png`.
- What's wrong: `RANGE EXPANSION` eyebrow + "Trading in an unusually wide range" headline is the same pair on nearly every card; the plain-English headline adds nothing over the eyebrow while the genuinely informative sentence (the raw engine line with the numbers) sits demoted below it. En masse it reads mail-merge, not intelligence.
- Fix: make `cardHeadline()` incorporate the card's own numbers ("Range 2.04 — 6.2× its typical bar") or suppress the plain headline when it merely restates the eyebrow, promoting the raw line. Effort: **M**.

**M6. Near-duplicate detections render as two separate cards.**
- Screenshot: `app/app-1440-signals.png` (two MSFT HIGH cards, same minute, same headline, one with an extra kind tag).
- What's wrong: real journal rows, faithfully rendered — but to a subscriber it looks like a glitchy double-fire. The feed is the product's résumé.
- Fix: collapse same-symbol/same-window clusters into one card with combined kind tags (presentation-layer grouping; the journal stays untouched). Effort: **M**.

**M7. Desktop dashboard: centered content column vs left-anchored chrome.**
- Screenshots: `app/app-1440-today.png`, `app/app-1440-watchlist.png`.
- What's wrong: `.view` centers at 720px while the active-tab underline sits at the far left of a 1440 window — the eye has to travel ~350px from the selected tab to the content it selected, across a dead gutter. Defensible as a reading-width choice, but it makes the page feel like two ungoverned layers.
- Fix options (pick one deliberately): left-align the view column under the tabs with a max-width; or center the tab row over the column; or give the gutter a job (the ambient field already tries). Effort: **M**.

**M8. Mobile modal "After detection" table loses its shape.**
- Screenshot: `app/app-390-signal-detail.png`.
- What's wrong: `sd-mark-row` wraps label and value onto separate stacked lines for every row (long "Resolves after session close" values), so the two-column table degrades into an eight-line list — the machined feel evaporates exactly where the marks data should impress.
- Fix: shorten the pending-value copy on narrow viewports ("After close"), or right-align the wrapped value line (`margin-left: auto`), or tabularize with a smaller size. Effort: **S**.

**M9. Tabs are ARIA tabs in name only.**
- Code: `web-app/src/App.jsx:135-147` (`role="tablist"`, `role="tab"`, `aria-selected`, but no `aria-controls`, no `tabpanel`, no arrow-key handling).
- What's wrong: half-implemented pattern signals "tabs" to AT and then doesn't behave like them (arrow keys do nothing; each tab is a separate Tab stop).
- Fix: either complete the pattern (ids + `aria-controls` + roving tabindex + arrow keys) or simplify honestly to a nav of buttons with `aria-current`, which the mobile nav already does correctly. Effort: **S–M**.

**M10. Desktop "How a signal forms" pin has a near-empty beat.**
- Screenshot: `landing/landing-1440-scroll-04.png`.
- What's wrong: mid-pin scroll positions show the stage almost blank between the field beat and the context beat — a stalled moment in an otherwise continuously-alive sequence. (Scroll-scrub sampling makes this a real user-visible state, not a capture artifact.)
- Fix: overlap the outgoing/incoming beat tweens so the stage never drops below one visible layer. Effort: **M** (timeline tuning, test at several scroll speeds).

**M11. Unlinked/empty views drop their h1 while Today keeps it.**
- Screenshots: `app/app-1440-activity-unlinked.png` (eyebrow → straight to quiet-state; "Your own trade log." gone) vs `app/app-1440-today-empty.png` (heading retained above the quiet-state).
- What's wrong: inconsistent page anatomy between sibling states; the unlinked Activity page looks momentarily broken (eyebrow floating alone).
- Fix: keep the h1 + subtitle in the unlinked/empty branches. Effort: **S**.

### LOW — nitpicks

**L1. "▶ TECHNICAL DETAIL" vs "▶ TECHNICAL DETAILS".** Both collapsed disclosures in one modal, singular then plural (`app-1440-signal-detail.png`; `SignalDetail.jsx:281,330`). Pick one. **S**

**L2. Two reds, never reconciled.** Landing `--red #ff3b4e` (coverage chips) vs app `--down #fb7185`. Same argument the code already makes for dropping violet applies. **S**

**L3. Two blacks on the landing.** Sections alternate `--bg` (#05070a) and raw `#000` (manifesto, pricing, final CTA, footer). If the deepening is intentional rhythm, tokenize it (`--bg-deep`) and say so; if not, unify. **S**

**L4. Mobile scroll-cue reads as a stray "|".** `landing-390-hero.png`: the animated `.hero-scroll-line` renders inline after "SEE HOW PERCH WORKS" as what looks like a typo'd pipe character at rest. Hide it on touch or move it under the text. **S**

**L5. Settings "PRICING COMING SOON." chip masquerades as a disabled button.** `app-1440-settings.png`. It's a `.settings-pricing-note` div; restyle toward the dashed "coming soon" treatment used above it so it doesn't invite a tap. **S**

**L6. Performance stat grid orphan.** `app-1440-performance.png`: four kind tiles + one orphan ("Round Number") alone on row two. Cosmetic; a `minmax` tweak or 3-column cap would balance it. **S**

**L7. `.trend-up`/`.trend-down` defined twice** (Views.css and SignalCard.css). Harmless duplication; consolidate. **S**

**L8. `site/` is a dead legacy directory** (4 static HTML prototypes incl. `simple-v1-neon.html`) referenced by no deploy config. Archive or delete so it can never be deployed/indexed by accident. **S**

**L9. Marketing screenshot drift risk.** The ProductInterface embed's alt text describes "an AMD volume spike" card; keep the embedded dashboard image (and its alt) in lockstep with the shipped card design whenever SignalCard changes — it's the one place the landing *shows* the product. **S** (process note)

**L10. Footer signal glyph nearly invisible.** `landing-1440-footer.png`: the `.ft-glyph` renders as a faint dot row at 0.7 opacity — at rest it reads as smudge rather than the noise→signal mark. Either let it play its entrance at footer-reveal or raise its resting contrast. **S**

---

## 3. Top 10 — ranked by movement toward premium

1. **H2 modal stacking fix.** The single most "looks broken" moment in the product, in its hero interaction, on both breakpoints. One portal. (S)
2. **H1 mobile hero edge clipping.** First screen, every phone visitor, clipped glyphs. Nothing premium survives a cropped logo mark. (S–M)
3. **H4 change-% labeling.** Repeated on every card; converts the app's most-seen row from "is this a bug?" to "this is precise." (S)
4. **H3 nav bleed-through.** Fixes the three worst desktop stills of the landing page — pricing and final CTA included. (S)
5. **H5 sign-contradicting stat.** One line of display logic guards the trust story the whole modal exists to tell. (M)
6. **M1 app type scale.** Cheap structurally, and it's the root cause of every "why is this heading this size?" moment; unblocks consistent future views (Journal is about to ship a whole new tab). (S)
7. **M5 + M6 feed de-templating.** The feed is the daily-use surface; varied headlines + cluster collapse turn 20 near-identical cards into something that reads like judgment. (M)
8. **M2 button spec.** The largest remaining same-company gap between surfaces. (M)
9. **M4 + M3 number columns and one list grammar.** "Numbers as the emotional center" is currently truest on Performance and least true on Watchlist/Activity; this closes it. (S–M)
10. **M8 mobile marks table.** The modal's data payoff should survive 390px; small fix, visible daily. (S)

Deliberately *not* in the top 10: M7 (layout centering) — it's the biggest visual change with the least certain payoff; decide the grid philosophy first, ideally alongside the Journal tab's layout needs.

---

## 4. What's already excellent — do not touch

- **The boot sequence.** Also contractually protected by BRAND.md §4 ("Do not touch"). It's the right length, the right restraint, and the fade-out's `pointer-events: none` detail is exactly the kind of invisible correctness the rest of the site keeps delivering.
- **The hero moment.** `THE MARKET MOVES. / PERCH NOTICES.` with the cyan em glow, variable-weight 640 display, kestrel + vignette. This is the bar the rest of the review measures against. (Fix H1's mobile offset; change nothing about the design.)
- **The token discipline and the CSS comments.** Both stylesheets read like a design system's rationale docs — WCAG measurements inline, cross-surface reconciliation notes ("violet had no equivalent in the app's palette, so it's dropped rather than reconciled"), touch-target math. This is rare. Whatever process produced it, keep it.
- **The reduced-motion storytelling fallbacks.** AlertSequence, Manifesto, and MarketField don't just kill animation — they re-lay the narrative out so reduced-motion users still get the payoff ("YOU DECIDE." frozen at its resting frame). Best-in-class; never regress this to a blanket `animation: none`.
- **The quiet states.** "Watching the market. … That's not a bug — it's Perch deciding there's nothing worth interrupting you for." (`app-1440-today-empty.png`). Empty states as brand statements. The unlinked-Telegram states share the quality.
- **The honesty pattern.** Amber "Small sample" pills, "Not yet" under *Statistically significant?*, "Excludes N news-driven signals," negative medians shown at full size (`app-1440-performance.png`). This is the product's moat rendered in UI; protect it from any future instinct to make the numbers look better.
- **The signal-card system.** is-high left accent + restrained glow, capped entrance stagger, the once-per-arrival bloom, whole-card-as-button with real focus treatment. The card is the promise the landing makes, kept.
- **Auth flow.** Login → sent → confirm → error is calm, consistent, and passwordless-as-advantage framing lands ("No passwords. Just your email, every time."). The verify-error state (`app-1440-auth-verify-error.png`) fails gracefully with a next step.
- **Focus and keyboard basics.** Custom cyan `:focus-visible` everywhere, skip link, modal focus trap with restore, `Escape` handling, `sr-only` utilities. (M9's tablist completeness is the one gap.)
- **The falcon mark's *architecture*** — state hooks (`idle/scanning/signal/confirmed/alert`) with documented restraint rules. BRAND.md says the art itself is a placeholder awaiting professional redraw; nothing in this review dings the artwork, and the state system is ready for the real one.

---

*Capture scripts and fixture builder used for this review are reproducible; the dashboard fixture data derives from `data/journal.db` session 2026-08-05 via SQL replicas of `journal.py`'s aggregate queries (MIN_HISTORY_SAMPLE=5, offset 30m).*

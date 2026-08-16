# Consistency map — re-run, August 2026

**Scope:** re-audit of the exact divergences documented in
`docs/design-review-2026-08.md` §1 ("the same-company test"), after the
design elevation program (Phases 1–4, branch `worktree-design-elevation`).
Method: token-level diff of both `index.css` files, code inspection, and
the committed capture sets (`phase1/` … `phase4/`).

## The review's table, re-scored

| Dimension | Review verdict (Aug 13) | Now | Evidence |
|---|---|---|---|
| Color tokens | ✅ Pass | ✅ Pass | Full token diff: every shared token identical hex-for-hex. |
| Reds | ⚠️ Two unreconciled reds (`--red #ff3b4e` vs `--down #fb7185`) | ✅ **Closed** | Landing `--red` now carries `#fb7185` — one red family, two documented meanings (coverage-chip state vs price direction). Contrast improved (≈5.8:1 → ≈7.5:1 on `--bg`). |
| Type scale | ⚠️ Biggest structural gap — no scale in app | ✅ **Closed** | `--step--1…--step-2` in `web-app/src/index.css`, identical clamp recipes to the landing's. Page h1s, quiet h2s, auth/error titles ride it; Performance's inline-styled h1s demoted onto `.view-section-title`. Landing-only `--step-3…6` remain by design (hero display sizes a dashboard never sets). |
| Display face | ✅ Close enough | ✅ Pass | Unchanged. |
| Buttons | ⚠️ Two-and-a-half button languages | ✅ **Closed** | One three-tier spec (solid / cyan-tinted / line-border + danger), documented in `web-app/src/index.css`, swept app-wide on `--radius-control`. Solid CTAs speak the landing's mono-uppercase language; the magnetic gradient CTA is documented in `MagneticButton.css` as the landing-only hero flourish. |
| Corner radii | ⚠️ No shared radius scale | ✅ **Closed (controls)** | Every app control on `--radius-control: 6px`. Surface radii (cards 8, panels 12, sheets 16, pills 999, calendar cells 4) are a deliberate second scale, documented with the token. The landing CTA's 2px stays as the documented flourish. |
| Cards | ✅ Deliberate mirror | ✅ Pass | Gradient recipe untouched; signal modal now shares the journal/AlertReveal inner-catchlight, so overlay surfaces are one material. |
| Lists | ⚠️ Two list grammars one tab apart | ✅ **Closed** | One `.data-row` hairline grammar (Views.css); Watchlist rides it with a fixed status slot (prices are a true column, review M4), Activity converged onto it. |
| Layout grid | ⚠️ Centered column vs left chrome (M7) | ◼ **Accepted divergence** | Declined by the owner 2026-08-16: current grid philosophy stands; revisit only if it bugs in use. |
| Motion | ✅ Compatible; app ahead (tokens) | ✅ Pass | Unchanged; app motion tokens intact, journal-standard durations hold (nothing added over 300ms). |
| Eyebrow / section-label | ✅ Pass | ✅ Pass | Now also one tracking (0.14em) for every section label, per the journal elevation's alignment. |
| Empty/quiet states | ✅ Excellent | ✅ Pass | And structurally consistent: unlinked/empty branches keep the page h1 + subtitle (review M11). |
| Focus states | ✅ Pass | ✅ Pass | Plus honest tab semantics: `nav` + `aria-current` replaces half-ARIA tabs (M9). |
| Reduced motion | ✅ Pass | ✅ Pass | Re-verified: `phase4/rm-*` captures — the landing's narrative relayouts (AlertSequence static context+payoff, manifesto, footer) and the app's global kill all render correctly. M10's fix explicitly leaves the reduced-motion path untouched. |
| Voice | ✅ Pass | ✅ Pass | L1's DETAIL/DETAILS unified; the M5 headlines keep the plain-honesty register with real numbers. |

## Findings ledger (review §2)

- **HIGH:** H1, H2, H4, H5 closed by the pre-program hotfixes
  (`31eb54d`, `3942a6f`); H3 closed in Phase 2. **All closed.**
- **MEDIUM:** M1–M4 (Phase 1), M5+M6, M8 (Phase 2), M9, M10, M11
  (Phase 3) — **closed**. M7 — **declined by owner** (see above).
- **LOW:** L1–L8, L10 closed (L7 in Phase 1, the rest Phase 3).
  **L9** (ProductInterface embed regeneration) — **deliberately a ship
  step**: its documented process requires real journal rows through the
  real API, so it runs with the coordinated deploy, once pixels are
  final.

## Deliberate residue (the differences that are decisions)

- Landing-only tokens: `--bg-deep` (the act-break black, review L3),
  `--edge`, `--step-3…6` (display sizes), the magnetic CTA at radius 2.
- App-only tokens: `--up`/`--down` (functional price direction — the
  landing deliberately tells its story without green/red), motion
  duration tokens, `--radius-control`.
- `--red` (landing) and `--down` (app) share one hex with two names,
  because they mean different things on each surface; the names document
  that.

*Mobile QA (390) and reduced-motion capture sets: `phase4/`. The full
tab/section sets: `phase3/`.*

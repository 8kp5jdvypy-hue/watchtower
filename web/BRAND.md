# Perch brand mark — architecture reference

**Status: the current mark is a placeholder.** `FALCON_PATHS` in
`src/components/PerchMark.jsx` is deliberately simple geometric artwork,
not final brand design. This document exists so that when a
professionally designed mark is ready, swapping it in touches as little
code as possible.

**Do not redesign, regenerate, or AI-invent new bird artwork based on
this document.** Its job is architecture, not art direction. Brand
design and engineering are intentionally separated — see the task this
was written for.

---

## 1. Where the current mark is used

Every usage ultimately draws from one source of truth: `FALCON_PATHS`
in `src/components/PerchMark.jsx`, a set of four SVG polygon point
lists (far wing, near wing, body, tail) plus one accent line. Three
different rendering technologies consume it:

| # | Location | File | Renders via |
|---|---|---|---|
| 1 | Desktop + mobile nav brand link | `src/components/Nav.jsx` | `<PerchMark>` component |
| 2 | Footer brand row | `src/components/Footer.jsx` | `<PerchMark>` component |
| 3 | Final CTA (pre-waitlist) | `src/components/FinalCta.jsx` | `<PerchMark>` component |
| 4 | Alert phone-notification mockup | `src/components/AlertReveal.jsx` | `<PerchMark>` component |
| 5 | **Opening animation** — the boot-sequence signal dot the mark locks into | `src/components/BootSequence.jsx` | `<PerchMarkGlyph>` inside its own `<svg>` (needs a GSAP ref) |
| 6 | Mid-page "dive" moment | `src/components/MarketField.jsx` | `<PerchMarkGlyph>` inside its own `<svg>` (needs a rotation transform) |
| 7 | Hero WebGL texture | `src/scenes/kestrelTexture.js` | Canvas 2D → rasterized into a Three.js texture — **not SVG at all** |
| 8 | Favicon | `index.html` | Hand-encoded inline `data:image/svg+xml` URI — **fully disconnected from the JS source of truth**, since static HTML can't import it |

Everything in rows 1–6 is React, and after this pass all six share the
exact same polygon markup via one of two exports from `PerchMark.jsx`
— nothing hand-copies the `<polygon>` elements anymore. Rows 7 and 8 are
the two genuine exceptions (see §6).

## 2. The component API

```jsx
import PerchMark, { PerchMarkGlyph, FALCON_PATHS, PERCH_MARK_VIEWBOX, PERCH_MARK_STATES } from './components/PerchMark'

// Standalone icon -- the common case.
<PerchMark size={20} variant="ink" accent state="idle" />
```

| Prop | Values | Notes |
|---|---|---|
| `size` | any number (px) | Pure SVG + `viewBox`, so any size renders crisp — 16px favicon through a hypothetical 1024px app icon all work with no separate logic. |
| `variant` | `'ink'` (default, near-white) · `'cyan'` · `'dark'` (near-black) | Maps directly to the three planned pre-baked file variants — see `public/brand/README.md`. |
| `accent` | boolean, default `true` | The single thin cyan leading-edge line. Auto-suppressed when `variant="cyan"` (redundant against an already-cyan fill). |
| `state` | `'idle'` (default) · `'scanning'` · `'signal'` · `'confirmed'` · `'alert'` | See §5. No call site passes anything but the default today. |

For contexts that need their own outer `<svg>` (an animation ref, a
rotation transform), import `PerchMarkGlyph` directly — it's the bare
`<g>` of polygons with no wrapper, exactly what `PerchMark` itself
renders inside its `<svg>`:

```jsx
<svg ref={myGsapTarget} viewBox={PERCH_MARK_VIEWBOX}>
  <PerchMarkGlyph fill="currentColor" accent={false} />
</svg>
```

`fill` on `PerchMarkGlyph` defaults to `'currentColor'`. Pass
`fill={null}` to omit the attribute entirely when an ancestor sets
`fill`/`color` in CSS and should control it instead (this is what
`MarketField.jsx`'s dive kestrel does — its color/rim-stroke are set on
the outer `<svg>` in `MarketField.css`, not per-call).

## 3. Sizes this needs to work at

All already served by the `size` prop with no separate logic per size:

favicon (16px, 32px) · mobile nav (20px) · desktop nav (20px) · footer
(20px) · final CTA (22px) · hero (WebGL texture, not this component —
see §6) · alert phone-notification (16px) · loading state · social
avatar · a future native app icon.

The two that fall outside the React app entirely — the favicon `<link>`
tag and any future social-avatar/app-icon export — need real static
files once they exist; see `public/brand/README.md`.

## 4. The opening animation

**Do not touch this.** It is out of scope for brand-mark work.

The mark's entry point into the opening sequence is
`src/components/BootSequence.jsx`, specifically the `<svg className="boot-dot" ref={dotRef}>` element (around line 118). This is the exact
element that:

1. Scattered points converge and pull into.
2. Ignites as the single cyan "signal" moment.
3. Sits beside "PERCH" as the wordmark resolves.
4. Flies into the nav's own live-indicator dot as the overlay dissolves.

When the final mark replaces `FALCON_PATHS`, this is the one location
where the new artwork needs to survive being scaled, translated, and
composited as a single flat-colored glyph (via `fill="currentColor"`)
across that whole sequence — a professionally designed mark that works
as a single-color silhouette at small sizes will drop in here with no
choreography changes needed.

## 5. State architecture (for future animation)

`PerchMark` accepts a `state` prop (`PERCH_MARK_STATES` in
`PerchMark.jsx`), threaded through to `data-state` and a
`pm-state-{state}` class on the root `<svg>`. CSS hooks for all five
states already exist in `PerchMark.css` and are verified working —
**but nothing in the app passes anything but the default (`idle`)
today.** This is available architecture, not a new animation that's
live anywhere yet.

Intended motion philosophy, restrained by design (cyan means "Perch
noticed," never a permanent glow):

| State | Meaning | Current (placeholder) treatment |
|---|---|---|
| `idle` | Quiet, at rest | No filter — today's unchanged baseline |
| `scanning` | Perch's attention is forming | Faint cyan `drop-shadow` |
| `signal` | Something is emerging | Slightly stronger cyan `drop-shadow` |
| `confirmed` | Arrived | One brief `drop-shadow` bloom keyframe, then settles (respects `prefers-reduced-motion`) |
| `alert` | The mark is the visual anchor | Strongest — still restrained — cyan `drop-shadow` |

These are placeholder treatments sized for today's flat-polygon mark.
The final artwork may call for different treatment (e.g. an actual
professionally-drawn mark might support a stroke-based "pulse" the
current silhouette can't). The point of this prop existing now is that
*wiring* a future signal-aware placement (the alert experience, a
loading indicator) to `<PerchMark state={...}>` requires zero changes
to any call site's surrounding layout — only which literal string gets
passed in.

## 6. Known exceptions — not centralized, and why

**`src/scenes/kestrelTexture.js`** (the hero's WebGL mesh) draws
`FALCON_PATHS` with Canvas 2D `moveTo`/`lineTo`/`fill()` calls, then
hands the rasterized result to Three.js as a texture. This can't share
`PerchMarkGlyph` (that's SVG JSX; this is imperative canvas drawing) —
it's a fundamentally different rendering path, required because the
hero mesh needs a GPU texture, not a DOM node. When the final mark
arrives, this file's `drawPolygon()` calls need their own update (or,
better: refactor it to draw a loaded SVG/image onto the canvas instead
of hand-drawn path commands — a reasonable follow-up, not done here to
avoid touching the hero's current behavior in a pass that isn't about
the hero).

**`index.html`'s favicon** *was* a hand-encoded inline
`data:image/svg+xml` URI. That has been replaced with real files, for a
reason beyond tidiness: a `data:` URI is not a URL Googlebot-Image can
crawl, so Google showed a generic globe for `perchmarkets.com` in search
results while `app.perchmarkets.com` — whose icon is a real file —
showed the mark. Google's requirements are a square (1:1) icon at a
crawlable, stable URL
([docs](https://developers.google.com/search/docs/appearance/favicon-in-search));
the mark's natural 190×178 viewBox is not square, so the generated
assets extend the dark background 6px top and bottom to reach 190×190
without touching the artwork.

The set now lives at `public/brand/` (`perch-mark.svg`, `icon-48/96/144/
192/512.png`, `apple-touch-icon.png`) plus `public/favicon.ico` and
`public/site.webmanifest` at the root, and is generated by
`web/scripts/gen_favicons.py` — **re-run that script when the mark
changes**, don't hand-edit the binaries. The polygon data in that script
is still a deliberate duplicate of `FALCON_PATHS` (static HTML and a
standalone generator can't `import` a JS module), which is the same
permanent exception described above — now in one scripted place rather
than hand-encoded into an HTML attribute.

## 7. How to swap in the final mark

Once a designer delivers real files into `public/brand/` (see that
directory's own README for the expected filenames):

1. **`src/components/PerchMark.jsx`** — this is the only file that
   should need a real code change. Two reasonable approaches:
   - Replace `FALCON_PATHS`'s point data with the new artwork's
     coordinates (keeps everything — including `PerchMarkGlyph`,
     `BootSequence`, `MarketField` — working with zero further edits),
     **if** the new mark is still expressible as flat polygons.
   - Or switch `PerchMark`/`PerchMarkGlyph` to render an inline `<svg>`
     loaded from `public/brand/perch-mark.svg` (e.g. via `<use>` or an
     SVG-as-component import), if the new artwork needs paths/curves
     polygons can't express. `BootSequence.jsx` and `MarketField.jsx`
     would need no changes either way, since they only ever consume
     `PerchMarkGlyph`/`PERCH_MARK_VIEWBOX`, not the raw path data.
2. **`index.html`** — nothing to do for the favicon; it already points
   at real files. Instead update `MARK_POLYGONS`/`MARK_ACCENT` in
   `web/scripts/gen_favicons.py` to the new artwork and re-run it, which
   regenerates every icon at every size from one source.
3. **`src/scenes/kestrelTexture.js`** — update to match (see §6).
4. Everything else (`Nav`, `Footer`, `FinalCta`, `AlertReveal`) needs
   **no changes** — they already consume the shared component.

## 8. Explicit constraints for whoever touches this next

- No 3D bird, no realistic feather simulation, no animated wings, no
  cartoon bird, no particle bird, no generative/AI-morphing bird. The
  final mark is a simple, professionally designed, recognizable symbol
  — engineering (this document) exists to integrate it well, not to
  compensate for weak artwork with elaborate effects.
- Keep it SVG wherever possible; avoid canvas/WebGL for the mark itself
  outside the one hero-texture exception in §6, which exists for a
  real technical reason (GPU texture), not convenience.
- The state system (§5) should stay restrained if it's ever wired up
  live — cyan means "Perch noticed," not a permanent glow.

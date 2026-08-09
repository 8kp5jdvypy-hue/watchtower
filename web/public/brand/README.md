# /public/brand — future Perch mark assets

This directory is prepared for the final, professionally designed Perch
mark. **Nothing has been placed here yet** — the current site still runs
entirely on the placeholder geometric mark defined in
`src/components/PerchMark.jsx` (`FALCON_PATHS`). See `../../BRAND.md` for
the full audit of where that placeholder is used today and exactly what
changes when real files land here.

## Expected files

| File | Color | Used for |
|---|---|---|
| `perch-mark.svg` | `currentColor` (no hardcoded fill) | Inline/embedded SVG contexts where CSS should control color — the same role `<PerchMark>` plays today. |
| `perch-mark-white.svg` | Fixed near-white | Contexts that can't use `currentColor`: `<img>` tags, dark-background exports. Maps to today's `variant="ink"`. |
| `perch-mark-cyan.svg` | Fixed electric cyan | Signal-color contexts. Maps to today's `variant="cyan"`. |
| `perch-mark-dark.svg` | Fixed near-black | Light-background contexts. Maps to today's `variant="dark"`. |

Why both a `currentColor` master *and* three pre-baked variants: an
inline `<svg>` can inherit page CSS color, but a raster `<img src="...">`
or a `<link rel="icon">` reference cannot — those need the color already
baked into the file.

## Likely additions once a designer is engaged (not required now)

- `favicon-16.png`, `favicon-32.png` — pre-rendered raster fallbacks for
  browsers/contexts that don't support SVG favicons.
- `social-avatar.png` (1080×1080 or similar) — for profile pictures
  (Instagram, etc.), outside the website entirely.
- `app-icon.png` (1024×1024) — for a future native/App Store icon.

None of these are needed for the website itself to function; they're
listed here so the full scope is in one place when a designer asks
"what sizes do you need."

## Swapping in the real mark

Once `perch-mark.svg` (and friends) exist here, see **"How to swap in
the final mark"** in `../../BRAND.md` for the short, specific list of
code edits required. It is deliberately small — this directory and
`BRAND.md` exist so that list stays small.

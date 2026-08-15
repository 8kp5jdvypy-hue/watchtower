#!/usr/bin/env python3
"""Generate the landing site's favicon set from the Perch mark.

Run:  python3 web/scripts/gen_favicons.py

Why this exists rather than committed-by-hand binaries: every PNG/ICO in
public/brand/ is derived art, and a blob with no provenance is a blob
nobody can safely regenerate. The polygon data below is the SAME mark as
FALCON_PATHS in src/components/PerchMark.jsx and the (previously inline,
data:-URI) favicon in index.html -- see BRAND.md section 6, which calls
that duplication out as a permanent, deliberate exception since static
HTML can't import a JS module. When the final designer mark lands, update
MARK_* here and re-run; BRAND.md section 7 has the full swap procedure.

Squaring: the mark's natural viewBox is 190x178, which is NOT square, and
Google requires "a square (1:1 aspect ratio)" favicon for search results
(developers.google.com/search/docs/appearance/favicon-in-search). The
artwork itself is untouched -- only the dark background is extended by
6px top and bottom to reach 190x190, keeping the mark optically centered
exactly where it already sat.

No SVG rasterizer (rsvg/ImageMagick/Inkscape) is assumed to be installed,
so the polygons are drawn directly with PIL at each output size, 8x
supersampled and downsampled with LANCZOS for clean antialiased edges.
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

# --- the mark, in its own coordinate space -------------------------------

INK = "#05070a"  # background
BONE = "#eef2f6"  # the falcon
CYAN = "#34e2ff"  # the wing-line accent

# Natural (non-square) artwork box, kept for reference: -72 -82 190 178.
# Square box, same center, background-only extension:
VIEW_X, VIEW_Y, VIEW_SIZE = -72, -88, 190

MARK_POLYGONS = [
    # (points, opacity)
    ([(-8, -38), (-62, -72), (-18, -18)], 0.82),
    ([(10, -22), (108, -46), (30, 26), (4, -4)], 1.0),
    ([(46, -64), (14, -16), (-32, 58), (-2, -30)], 1.0),
    ([(-32, 58), (-58, 86), (-20, 72)], 1.0),
]
MARK_ACCENT = ((10, -22), (108, -46))
MARK_ACCENT_WIDTH = 5

SUPERSAMPLE = 8

OUT_DIR = Path(__file__).resolve().parent.parent / "public"
BRAND_DIR = OUT_DIR / "brand"

PNG_SIZES = [48, 96, 144, 192, 512]
APPLE_TOUCH_SIZE = 180
ICO_SIZES = [16, 32, 48]


def _hex_to_rgb(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))


def render(size: int) -> Image.Image:
    """The mark at `size` x `size` pixels, opaque, antialiased."""
    scale = (size * SUPERSAMPLE) / VIEW_SIZE
    canvas = size * SUPERSAMPLE

    def to_px(point):
        x, y = point
        return ((x - VIEW_X) * scale, (y - VIEW_Y) * scale)

    img = Image.new("RGBA", (canvas, canvas), _hex_to_rgb(INK) + (255,))

    for points, opacity in MARK_POLYGONS:
        layer = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
        ImageDraw.Draw(layer).polygon(
            [to_px(p) for p in points], fill=_hex_to_rgb(BONE) + (round(opacity * 255),)
        )
        img = Image.alpha_composite(img, layer)

    # The accent line, with the round caps the SVG specifies (PIL has no
    # linecap, so the caps are drawn as circles at each endpoint).
    accent = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
    draw = ImageDraw.Draw(accent)
    start, end = to_px(MARK_ACCENT[0]), to_px(MARK_ACCENT[1])
    stroke_px = MARK_ACCENT_WIDTH * scale
    draw.line([start, end], fill=_hex_to_rgb(CYAN) + (255,), width=round(stroke_px))
    radius = stroke_px / 2
    for cx, cy in (start, end):
        draw.ellipse(
            [cx - radius, cy - radius, cx + radius, cy + radius], fill=_hex_to_rgb(CYAN) + (255,)
        )
    img = Image.alpha_composite(img, accent)

    return img.resize((size, size), Image.LANCZOS).convert("RGB")


def svg_source() -> str:
    """The same mark as a square SVG -- what index.html points at for any
    browser that prefers vector, and what BRAND.md section 7 expects to
    find at public/brand/perch-mark.svg."""
    polys = []
    for points, opacity in MARK_POLYGONS:
        pts = " ".join(f"{x},{y}" for x, y in points)
        attr = f" opacity='{opacity}'" if opacity != 1.0 else ""
        polys.append(f"<polygon{attr} points='{pts}'/>")
    (ax, ay), (bx, by) = MARK_ACCENT
    return (
        f"<svg xmlns='http://www.w3.org/2000/svg' "
        f"viewBox='{VIEW_X} {VIEW_Y} {VIEW_SIZE} {VIEW_SIZE}'>"
        f"<rect x='{VIEW_X}' y='{VIEW_Y}' width='{VIEW_SIZE}' height='{VIEW_SIZE}' fill='{INK}'/>"
        f"<g fill='{BONE}'>{''.join(polys)}</g>"
        f"<polyline points='{ax},{ay} {bx},{by}' fill='none' stroke='{CYAN}' "
        f"stroke-width='{MARK_ACCENT_WIDTH}' stroke-linecap='round'/>"
        f"</svg>\n"
    )


def main() -> None:
    BRAND_DIR.mkdir(parents=True, exist_ok=True)

    (BRAND_DIR / "perch-mark.svg").write_text(svg_source(), encoding="utf-8")
    print(f"wrote {BRAND_DIR / 'perch-mark.svg'}")

    for size in PNG_SIZES:
        path = BRAND_DIR / f"icon-{size}.png"
        render(size).save(path, format="PNG", optimize=True)
        print(f"wrote {path} ({size}x{size})")

    apple = BRAND_DIR / "apple-touch-icon.png"
    render(APPLE_TOUCH_SIZE).save(apple, format="PNG", optimize=True)
    print(f"wrote {apple} ({APPLE_TOUCH_SIZE}x{APPLE_TOUCH_SIZE})")

    # favicon.ico lives at the ROOT, not in brand/: browsers and Google
    # both probe /favicon.ico directly when no <link> resolves, and this
    # site's not_found_handling=single-page-application would otherwise
    # answer that probe with the HTML shell at 200.
    ico = OUT_DIR / "favicon.ico"
    render(max(ICO_SIZES)).save(ico, format="ICO", sizes=[(s, s) for s in ICO_SIZES])
    print(f"wrote {ico} ({', '.join(f'{s}x{s}' for s in ICO_SIZES)})")


if __name__ == "__main__":
    main()

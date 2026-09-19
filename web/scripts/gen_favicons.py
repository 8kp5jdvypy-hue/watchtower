"""Generate the brand raster set from the Perch mark ("P on the rail").

Run:  python3 web/scripts/gen_favicons.py

Every PNG/ICO in public/brand/ is derived art; this script is its
provenance. The geometry below IS src/components/PerchMark.jsx's
MARK_PATHS, redrawn with PIL primitives (no SVG rasterizer is assumed on
the machine) at 8x supersample and LANCZOS-downsampled. The mark's
viewBox is a 100x100 square, so no squaring pass is needed anymore.

Also writes the four SVG variants public/brand/README.md describes
(currentColor master + white / cyan / dark), straight from MARK_PATHS,
and copies the icon set into web-app/public/brand so the dashboard's
favicon is the same file.
"""
from __future__ import annotations

import shutil
from pathlib import Path

from PIL import Image, ImageDraw

BG = "#05070a"
INK = "#eef2f6"
CYAN = "#34e2ff"
DARK = "#05070a"

# --- MARK_PATHS, as PIL primitives (viewBox 0 0 100 100) -----------------
STEM = (24, 10, 38, 92)          # x0, y0, x1, y1; radius 3
BOWL_RECT = (27, 10, 52, 54)     # the bowl's straight part
BOWL_CIRCLE = (52, 32, 22)       # cx, cy, r (outer)
COUNTER_RECT = (38, 22, 50, 42)
COUNTER_CIRCLE = (50, 32, 10)
FLAG = (38, 62, 62, 70)          # right end rounded, radius 4
TICKS_Y = [22, 34, 46, 58, 70, 82]
TICK_X = (14, 20)

# Paths matching PerchMark.jsx exactly, for the SVG variants.
LETTER_D = (
    "M27 10 H52 A22 22 0 0 1 52 54 H38 V89 A3 3 0 0 1 35 92 H27 A3 3 0 0 1 24 89 V13 A3 3 0 0 1 27 10 Z "
    "M38 22 V42 H50 A10 10 0 0 0 50 22 Z"
)
FLAG_D = "M38 62 H62 A4 4 0 0 1 62 70 H38 Z"

SUPERSAMPLE = 8
WEB = Path(__file__).resolve().parent.parent
BRAND_DIR = WEB / "public" / "brand"
DASH_BRAND_DIR = WEB.parent / "web-app" / "public" / "brand"
PNG_SIZES = [48, 96, 144, 192, 512]
APPLE_TOUCH_SIZE = 180


def _hex_rgba(color: str, alpha: float = 1.0) -> tuple[int, int, int, int]:
    c = color.lstrip("#")
    return (int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16), int(round(255 * alpha)))


def draw_mark(size: int, *, ink: str, bg: str | None, flag: str, ticks: bool, pad: float = 0.0) -> Image.Image:
    """Render the mark at `size` px. `pad` is a fraction of size reserved
    around the mark (app tiles want breathing room; favicons don't)."""
    ss = size * SUPERSAMPLE
    img = Image.new("RGBA", (ss, ss), _hex_rgba(bg) if bg else (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    inner = ss * (1 - 2 * pad)
    off = ss * pad
    k = inner / 100.0

    def X(v): return off + v * k
    def box(x0, y0, x1, y1): return (X(x0), X(y0), X(x1), X(y1))
    def circle(cx, cy, r): return (X(cx - r), X(cy - r), X(cx + r), X(cy + r))

    ink_rgba = _hex_rgba(ink)
    if ticks:
        tick = _hex_rgba(ink, 0.38)
        for y in TICKS_Y:
            d.line([(X(TICK_X[0]), X(y)), (X(TICK_X[1]), X(y))], fill=tick, width=max(1, int(2 * k)), joint="curve")
            d.ellipse(circle(TICK_X[0], y, 1), fill=tick)
            d.ellipse(circle(TICK_X[1], y, 1), fill=tick)
    d.rounded_rectangle(box(*STEM), radius=3 * k, fill=ink_rgba)
    d.rectangle(box(*BOWL_RECT), fill=ink_rgba)
    d.ellipse(circle(*BOWL_CIRCLE), fill=ink_rgba)
    hole = _hex_rgba(bg) if bg else (0, 0, 0, 0)
    d.rectangle(box(*COUNTER_RECT), fill=hole)
    d.ellipse(circle(*COUNTER_CIRCLE), fill=hole)
    x0, y0, x1, y1 = FLAG
    flag_rgba = _hex_rgba(flag)
    d.rectangle(box(x0, y0, x1 - 4, y1), fill=flag_rgba)
    d.rounded_rectangle(box(x1 - 8, y0, x1, y1), radius=4 * k, fill=flag_rgba)
    return img.resize((size, size), Image.LANCZOS)


def svg(fill: str) -> str:
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
        f'<g stroke="{fill}" stroke-width="2" stroke-linecap="round" opacity="0.38">'
        + "".join(f'<line x1="14" y1="{y}" x2="20" y2="{y}"/>' for y in TICKS_Y)
        + "</g>"
        f'<path d="{LETTER_D}" fill="{fill}" fill-rule="evenodd"/>'
        f'<path d="{FLAG_D}" fill="{CYAN if fill != CYAN else fill}"/>'
        "</svg>\n"
    )


def main() -> None:
    BRAND_DIR.mkdir(parents=True, exist_ok=True)
    # SVG variants
    (BRAND_DIR / "perch-mark.svg").write_text(svg("currentColor"))
    (BRAND_DIR / "perch-mark-white.svg").write_text(svg(INK))
    (BRAND_DIR / "perch-mark-cyan.svg").write_text(svg(CYAN))
    (BRAND_DIR / "perch-mark-dark.svg").write_text(svg(DARK))
    # PNG icons on the ground colour (favicon contexts: no padding, ticks from 96px)
    for size in PNG_SIZES:
        draw_mark(size, ink=INK, bg=BG, flag=CYAN, ticks=size >= 96, pad=0.06).save(BRAND_DIR / f"icon-{size}.png")
    # Apple touch icon: iOS masks its own corners; more breathing room like an app tile
    draw_mark(APPLE_TOUCH_SIZE, ink=INK, bg=BG, flag=CYAN, ticks=True, pad=0.16).save(BRAND_DIR / "apple-touch-icon.png")
    # favicon.ico: 16/32/48 from one master
    master = draw_mark(48, ink=INK, bg=BG, flag=CYAN, ticks=False, pad=0.04)
    master.save(WEB / "public" / "favicon.ico", sizes=[(16, 16), (32, 32), (48, 48)])
    # Dashboard gets the identical icon files
    DASH_BRAND_DIR.mkdir(parents=True, exist_ok=True)
    for name in ["perch-mark.svg", "apple-touch-icon.png", *[f"icon-{s}.png" for s in PNG_SIZES]]:
        shutil.copy(BRAND_DIR / name, DASH_BRAND_DIR / name)
    shutil.copy(WEB / "public" / "favicon.ico", WEB.parent / "web-app" / "public" / "favicon.ico")
    print("wrote brand set to", BRAND_DIR, "and", DASH_BRAND_DIR)


if __name__ == "__main__":
    main()

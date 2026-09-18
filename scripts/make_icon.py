"""Generate the app icon and favicon.

The art is a literal 16x16 pixel map. 16 divides every size macOS asks for
(16/32/64/128/256/512/1024), so every export is an exact integer scale and the
pixels stay hard-edged instead of turning to mush at small sizes.

Two speech bubbles with dot eyes: the product is two people talking, and the
colours are the same ones the captions use, so the icon says which app this is
before you read the name.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"

SPK0 = (253, 238, 0, 255)         # #FDEE00
SPK1 = (147, 197, 114, 255)     # #93C572
EYE = (20, 28, 32, 255)
BG = (13, 18, 21, 255)           # the app's own ground

ART = [
    "................",
    ".....GGGGGGGGG..",
    "....GGGGGGGGGGG.",
    "....GGGkGGGkGGG.",
    "....GGGGGGGGGGG.",
    "....GGGGGGGGGGG.",
    ".....GGGGGGGGG..",
    "........GG......",
    "..YYYYYYYYY.....",
    ".YYYYYYYYYYY....",
    ".YYkYYYkYYYY....",
    ".YYYYYYYYYYY....",
    ".YYYYYYYYYYY....",
    "..YYYYYYYYY.....",
    "....YY..........",
    "................",
]
COLORS = {"G": SPK1, "Y": SPK0, "k": EYE}


def art(scale: int = 1) -> Image.Image:
    im = Image.new("RGBA", (16, 16), (0, 0, 0, 0))
    px = im.load()
    for y, row in enumerate(ART):
        for x, ch in enumerate(row):
            if ch in COLORS:
                px[x, y] = COLORS[ch]
    if scale > 1:
        im = im.resize((16 * scale, 16 * scale), Image.NEAREST)
    return im


def tile(size: int, margin_ratio: float = 0.16) -> Image.Image:
    """The mark on the app's dark squircle, at an exact integer pixel scale."""
    inner = size - 2 * round(size * margin_ratio)
    scale = max(1, inner // 16)
    mark = art(scale)

    im = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    r = round(size * 0.225)
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=r, fill=BG)

    # No grid overlay: the hard pixel edges already read as dot-matrix, and a
    # faint dot lattice just looked like dirt on the tile at 128px and up.
    off = ((size - mark.width) // 2, (size - mark.height) // 2)
    im.alpha_composite(mark, off)
    return im


# The list placeholder, in the same 16x16 language as the icon: a film frame
# with sprocket holes. It stands in before a thumbnail arrives and stays put if
# one never does, so a missing poster never shows as a broken image.
PLACEHOLDER = [
    "................",
    ".##############.",
    ".#.#........#.#.",
    ".#............#.",
    ".#...##.......#.",
    ".#.#.####...#.#.",
    ".#...######.#.#.",
    ".#...######...#.",
    ".#.#.####...#.#.",
    ".#...##.....#.#.",
    ".#............#.",
    ".#.#........#.#.",
    ".##############.",
    "................",
    "................",
    "................",
]
DIM = (78, 92, 100, 255)          # --dimmer, so it recedes on the panel


def placeholder(scale: int = 4) -> Image.Image:
    im = Image.new("RGBA", (16, 16), (0, 0, 0, 0))
    px = im.load()
    for y, row in enumerate(PLACEHOLDER):
        for x, ch in enumerate(row):
            if ch == "#":
                px[x, y] = DIM
    return im.resize((16 * scale, 16 * scale), Image.NEAREST)


def main():
    ASSETS.mkdir(exist_ok=True)
    placeholder(4).save(ROOT / "web" / "placeholder.png")
    art(64).save(ASSETS / "mark.png")              # flat mark, no tile
    tile(1024).save(ASSETS / "icon-1024.png")
    art(4).save(ROOT / "web" / "favicon.png")      # 64px, crisp in a tab

    iconset = ASSETS / "app.iconset"
    if iconset.exists():
        for f in iconset.iterdir():
            f.unlink()
    iconset.mkdir(exist_ok=True)
    for size in (16, 32, 128, 256, 512):
        tile(size).save(iconset / f"icon_{size}x{size}.png")
        tile(size * 2).save(iconset / f"icon_{size}x{size}@2x.png")

    try:
        subprocess.run(["iconutil", "-c", "icns", str(iconset),
                        "-o", str(ASSETS / "app.icns")], check=True,
                       capture_output=True)
        print(f"寫入 {ASSETS/'app.icns'}")
    except Exception as e:
        print(f"iconutil 失敗：{e}", file=sys.stderr)
    print(f"寫入 {ASSETS/'icon-1024.png'}、web/favicon.png 與 web/placeholder.png")


if __name__ == "__main__":
    main()

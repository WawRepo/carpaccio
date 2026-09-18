#!/usr/bin/env python3
"""Build the web icons from docs/images/icon.png (and a web-sized README logo).

    python3 scripts/make_icons.py        # needs Pillow

Writes into src/carpaccio/static/ — the only directory the app serves, and the
one the auth middleware leaves open (a browser asks for the icon before it has
logged in). The originals in docs/images/ are source art and are not shipped.

The background is made transparent by FLOOD-FILLING FROM THE CORNERS, not by
turning every near-white pixel transparent: the fish's own white highlights
are not connected to the edge, so they survive. A global threshold would punch
holes in the fish.
"""

from pathlib import Path

from PIL import Image, ImageChops, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "docs" / "images"
STATIC = ROOT / "src" / "carpaccio" / "static"


def transparent_background(img: Image.Image, thresh: int = 40) -> Image.Image:
    img = img.convert("RGBA")
    w, h = img.size
    for corner in [(0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)]:
        ImageDraw.floodfill(img, corner, (255, 255, 255, 0), thresh=thresh)
    return img


def square(img: Image.Image, pad_ratio: float = 0.06) -> Image.Image:
    box = img.getbbox()  # bbox of non-transparent pixels
    img = img.crop(box)
    side = int(max(img.size) * (1 + 2 * pad_ratio))
    out = Image.new("RGBA", (side, side), (255, 255, 255, 0))
    out.paste(img, ((side - img.width) // 2, (side - img.height) // 2), img)
    return out


def main() -> None:
    fish = square(transparent_background(Image.open(SRC / "icon.png")))

    fish.save(STATIC / "favicon.ico", sizes=[(16, 16), (32, 32), (48, 48)])
    fish.resize((192, 192), Image.LANCZOS).save(STATIC / "icon-192.png", optimize=True)
    # iOS renders transparency as black on the home screen: flatten onto white.
    touch = Image.new("RGBA", fish.size, (255, 255, 255, 255))
    touch.alpha_composite(fish)
    touch.convert("RGB").resize((180, 180), Image.LANCZOS).save(
        STATIC / "apple-touch-icon.png", optimize=True
    )

    # README logo: trim the white margin, keep it light enough for a README.
    logo = Image.open(SRC / "logo.png").convert("RGB")
    bg = Image.new("RGB", logo.size, (255, 255, 255))
    box = ImageChops.difference(logo, bg).point(lambda v: 255 if v > 24 else 0).getbbox()
    m = 28  # keep a margin: a tight crop clips the tagline's descenders
    logo = logo.crop(
        (
            max(box[0] - m, 0),
            max(box[1] - m, 0),
            min(box[2] + m, logo.width),
            min(box[3] + m, logo.height),
        )
    )
    logo.thumbnail((640, 640), Image.LANCZOS)
    # Pixel art has few real colours: a 64-colour palette is visually
    # identical here and several times smaller than RGB.
    logo.quantize(colors=64, method=Image.Quantize.MEDIANCUT).save(
        SRC / "logo-readme.png", optimize=True
    )

    for f in ["favicon.ico", "icon-192.png", "apple-touch-icon.png"]:
        print(f"{f:<22} {(STATIC / f).stat().st_size:>7} bytes")
    print(f"{'logo-readme.png':<22} {(SRC / 'logo-readme.png').stat().st_size:>7} bytes")


if __name__ == "__main__":
    main()

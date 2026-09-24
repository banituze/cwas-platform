"""Builds static/img/flags.webp: every flag in static/flags drawn once, at twice its on-screen size, in one small image.

The country picker beside phone fields (templates/partials/phone.html, static/js/phone.js) paints each flag from this
single sprite. The sprite arrives with the page, so the whole list is already on screen the moment the picker opens,
instead of waiting on hundreds of separate SVG downloads.

Layout: flags sit in alphabetical order of their file names, COLS to a row, each in a CELL_W x CELL_H cell (a 22 x 16
CSS pixel box at 2x). services.phone_countries() derives each country's cell from the same order, and the stylesheet
(.flag in static/css/input.css) scales the sprite to SHEET_W x SHEET_H CSS pixels, so the three must agree.

Build-time only; the running app needs neither package. Run it again whenever a flag is added or changed:

    pip install cairosvg pillow
    python3 tools/flag_sprite.py
"""
import io
import os
import sys

import cairosvg
from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FLAGS = os.path.join(ROOT, "static", "flags")
OUT = os.path.join(ROOT, "static", "img", "flags.webp")
COLS, ROWS = 16, 16          # 256 cells: room for every ISO 3166 region
CELL_W, CELL_H = 44, 32      # 2x of the 22 x 16 box the flag is shown in


def draw(path):
    """One flag scaled to cover its cell (the same crop as object-fit: cover), centred."""
    # Render 1 px taller than the cell (4:3 flags into an 11:8 box), then trim the extra half pixel top and bottom.
    png = cairosvg.svg2png(url=path, output_width=CELL_W, output_height=CELL_H + 1)
    img = Image.open(io.BytesIO(png)).convert("RGBA")
    top = (img.height - CELL_H) // 2
    return img.crop((0, top, CELL_W, top + CELL_H))


def main():
    names = sorted(f for f in os.listdir(FLAGS) if f.endswith(".svg"))
    if len(names) > COLS * ROWS:
        sys.exit(f"{len(names)} flags do not fit a {COLS} x {ROWS} sheet; raise ROWS here, in services.py and in .flag")
    sheet = Image.new("RGBA", (COLS * CELL_W, ROWS * CELL_H), (0, 0, 0, 0))
    for i, name in enumerate(names):
        sheet.paste(draw(os.path.join(FLAGS, name)), ((i % COLS) * CELL_W, (i // COLS) * CELL_H))
    # Lossless keeps every flag's edges crisp; flat colours compress well, so the sheet stays small.
    sheet.save(OUT, "WEBP", lossless=True, quality=100, method=6)
    print(f"{len(names)} flags -> {os.path.relpath(OUT, ROOT)} ({os.path.getsize(OUT) // 1024} KB)")


if __name__ == "__main__":
    main()

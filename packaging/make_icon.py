#!/usr/bin/env python3
"""Generate the application icon: a minimal routing / patch-bay glyph.

Run:  python packaging/make_icon.py

Produces (committed to the repo):
    ferrodac/assets/app.png   256x256 window / taskbar icon
    packaging/app.ico         multi-size Windows icon for PyInstaller
"""

from __future__ import annotations

import os

from PIL import Image, ImageDraw

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
ASSETS = os.path.join(ROOT, "ferrodac", "assets")

# Palette matches the app's dark theme.
PANEL = (23, 28, 38, 255)      # #171c26
BORDER = (44, 55, 74, 255)     # #2c374a
ACCENT = (79, 195, 247, 255)   # #4fc3f7  (sources)
SINK = (105, 219, 124, 255)    # #69db7c  (sink)
WARM = (255, 138, 101, 255)    # #ff8a65  (a second source)

S = 1024            # supersample, downscaled for crisp anti-aliasing
OUT = 256


def quad(p0, p1, p2, n=80):
    pts = []
    for i in range(n + 1):
        t = i / n
        u = 1 - t
        pts.append((u * u * p0[0] + 2 * u * t * p1[0] + t * t * p2[0],
                    u * u * p0[1] + 2 * u * t * p1[1] + t * t * p2[1]))
    return pts


def dot(d, c, r, color):
    d.ellipse([c[0] - r, c[1] - r, c[0] + r, c[1] + r], fill=color)


def render() -> Image.Image:
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # rounded panel
    pad, rad = 70, 190
    d.rounded_rectangle([pad, pad, S - pad, S - pad], radius=rad,
                        fill=PANEL, outline=BORDER, width=18)

    # two sources (left) routed into one sink (right) — the patch bay
    src_a = (300, 360)
    src_b = (300, 664)
    sink = (724, 512)
    for src, col, ctrl in ((src_a, ACCENT, (560, 360)),
                           (src_b, WARM, (560, 664))):
        d.line(quad(src, ctrl, sink), fill=col, width=34, joint="curve")
    # nodes on top of the wires
    dot(d, src_a, 78, ACCENT)
    dot(d, src_b, 78, WARM)
    dot(d, sink, 96, PANEL)          # ring: dark core …
    d.ellipse([sink[0] - 96, sink[1] - 96, sink[0] + 96, sink[1] + 96],
              outline=SINK, width=30)

    return img.resize((OUT, OUT), Image.LANCZOS)


def main() -> None:
    os.makedirs(ASSETS, exist_ok=True)
    icon = render()
    icon.save(os.path.join(ASSETS, "app.png"))
    icon.save(os.path.join(ROOT, "packaging", "app.ico"),
              sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print("wrote ferrodac/assets/app.png and packaging/app.ico")


if __name__ == "__main__":
    main()

"""Draw the app icon (readiness ring on a dark rounded square, dashboard colours) as an .iconset."""
import sys
from pathlib import Path

from PIL import Image, ImageDraw

BG_TOP, BG_BOTTOM = (31, 33, 38), (13, 14, 17)  # --surface-2 -> --bg (dark theme)
TRACK, ACCENT = (38, 40, 46), (91, 155, 255)      # --track, --accent (dark theme)


def draw(size: int) -> Image.Image:
    s = 4  # supersample, then downscale for smooth edges
    n = size * s
    img = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    # Apple's grid: the rounded square is ~80% of the canvas, corner radius ~22% of it.
    inset, side = round(n * 0.1), round(n * 0.8)
    grad = Image.new("RGBA", (n, n))
    gd = ImageDraw.Draw(grad)
    for y in range(n):
        t = y / (n - 1)
        gd.line([(0, y), (n, y)], fill=tuple(round(a + (b - a) * t) for a, b in zip(BG_TOP, BG_BOTTOM)) + (255,))
    mask = Image.new("L", (n, n), 0)
    ImageDraw.Draw(mask).rounded_rectangle([inset, inset, inset + side, inset + side],
                                           radius=round(side * 0.225), fill=255)
    img.paste(grad, (0, 0), mask)

    d = ImageDraw.Draw(img)
    r, w = side * 0.30, max(round(side * 0.085), 2 * s)
    c = n / 2
    box = [c - r, c - r, c + r, c + r]
    d.ellipse(box, outline=TRACK, width=w)
    start, end = -90, -90 + 0.78 * 360  # ~78% filled, from 12 o'clock
    d.arc(box, start, end, fill=ACCENT, width=w)
    # round caps
    import math
    for ang in (start, end):
        a = math.radians(ang)
        mr = r - w / 2
        x, y = c + mr * math.cos(a), c + mr * math.sin(a)
        d.ellipse([x - w / 2, y - w / 2, x + w / 2, y + w / 2], fill=ACCENT)
    return img.resize((size, size), Image.LANCZOS)


if __name__ == "__main__":
    out = Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)
    for base in (16, 32, 128, 256, 512):
        draw(base).save(out / f"icon_{base}x{base}.png")
        draw(base * 2).save(out / f"icon_{base}x{base}@2x.png")

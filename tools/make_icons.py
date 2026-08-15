"""Generate the app icons.

The manifest previously pointed at a flaticon CDN URL — a third-party
dependency for the app's own icon, which fails at exactly the moment offline
mode matters. These are generated locally instead, with no image library, so
they can be regenerated from source at any time:

    python tools/make_icons.py

The mark is the app in one glyph: two bearing lines crossing at a fix.
"""
import math
import struct
import zlib
from pathlib import Path

OUT_DIR = Path(__file__).resolve().parent.parent / "static"
SUPERSAMPLE = 4  # rendered at 4x and box-filtered down, for clean edges

BACKGROUND = (27, 44, 49)     # --header-bg
LINE = (46, 204, 113)         # the project's green
FIX = (255, 255, 255)
OBSERVER = (150, 214, 180)


def _blend(base, colour, alpha):
    return tuple(round(b + (c - b) * alpha) for b, c in zip(base, colour))


def _draw_disc(pixels, size, cx, cy, radius, colour):
    for y in range(max(0, int(cy - radius) - 1), min(size, int(cy + radius) + 2)):
        for x in range(max(0, int(cx - radius) - 1), min(size, int(cx + radius) + 2)):
            if math.hypot(x + 0.5 - cx, y + 0.5 - cy) <= radius:
                pixels[y][x] = colour


def _draw_line(pixels, size, x1, y1, x2, y2, width, colour):
    length = math.hypot(x2 - x1, y2 - y1)
    if length == 0:
        return
    steps = int(length * 2)
    for step in range(steps + 1):
        t = step / steps
        _draw_disc(pixels, size, x1 + (x2 - x1) * t, y1 + (y2 - y1) * t, width / 2, colour)


def render(size):
    """Render the mark at ``size`` pixels, returning rows of RGB tuples."""
    work = size * SUPERSAMPLE
    pixels = [[BACKGROUND for _ in range(work)] for _ in range(work)]

    unit = work / 100.0
    # Two observers low and wide, bearing lines crossing at the fix above them.
    observers = [(20 * unit, 78 * unit), (80 * unit, 78 * unit)]
    fix = (50 * unit, 36 * unit)

    for ox, oy in observers:
        # Extend past the intersection so the lines read as bearings, not arrows.
        dx, dy = fix[0] - ox, fix[1] - oy
        _draw_line(pixels, work, ox, oy, ox + dx * 1.45, oy + dy * 1.45, 3.4 * unit, LINE)

    for ox, oy in observers:
        _draw_disc(pixels, work, ox, oy, 5.5 * unit, OBSERVER)

    _draw_disc(pixels, work, fix[0], fix[1], 11 * unit, BACKGROUND)
    _draw_disc(pixels, work, fix[0], fix[1], 8 * unit, FIX)

    # Box-filter down to the requested size.
    out = []
    for y in range(size):
        row = []
        for x in range(size):
            r = g = b = 0
            for sy in range(SUPERSAMPLE):
                for sx in range(SUPERSAMPLE):
                    pr, pg, pb = pixels[y * SUPERSAMPLE + sy][x * SUPERSAMPLE + sx]
                    r += pr
                    g += pg
                    b += pb
            count = SUPERSAMPLE * SUPERSAMPLE
            row.append((r // count, g // count, b // count))
        out.append(row)
    return out


def write_png(path, rows):
    size = len(rows)
    raw = bytearray()
    for row in rows:
        raw.append(0)  # filter type 0 (None)
        for r, g, b in row:
            raw += bytes((r, g, b))

    def chunk(tag, data):
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + chunk(b"IEND", b"")
    )
    path.write_bytes(png)
    return len(png)


if __name__ == "__main__":
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for size in (192, 512):
        target = OUT_DIR / f"icon-{size}.png"
        written = write_png(target, render(size))
        print(f"wrote {target} ({written:,} bytes)")

"""Regenerate logo.ico from the logo.svg brand geometry.

Brand mark: indigo rounded tile (#6366F1 -> #4338CA gradient), white signal
wave (M20 50 Q40 20 60 50 T80 50 in the svg's 100x100 viewBox) with a soft
glow, and a white node dot at (60, 50). Rendered at 1024 px and downscaled
into 16/24/32/48/64/128/256 frames.

Usage: python packaging/make_logo.py   (writes logo.ico at the repo root)
"""
import os

from PIL import Image, ImageDraw, ImageFilter

S = 1024  # master render, downscaled into the .ico frames
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logo.ico")


def clipped(layer, mask):
    return Image.composite(layer, Image.new("RGBA", layer.size, (0, 0, 0, 0)), mask)


def stamp_stroke(layer, pts, width, fill):
    # dense overlapping circles: smooth stroke with no joint artifacts
    d = ImageDraw.Draw(layer)
    r = width / 2
    for x, y in pts:
        d.ellipse([x - r, y - r, x + r, y + r], fill=fill)


def quad(p0, c, p1, n=240, k=1.0):
    pts = []
    for i in range(n + 1):
        t = i / n
        pts.append((((1 - t) ** 2 * p0[0] + 2 * (1 - t) * t * c[0] + t ** 2 * p1[0]) * k,
                    ((1 - t) ** 2 * p0[1] + 2 * (1 - t) * t * c[1] + t ** 2 * p1[1]) * k))
    return pts


# brand tile: vertical indigo gradient
top, bot = (99, 102, 241), (67, 56, 202)
grad = Image.new("RGBA", (S, S))
gd = ImageDraw.Draw(grad)
for y in range(S):
    t = y / (S - 1)
    gd.line([(0, y), (S, y)],
            fill=tuple(round(top[i] + (bot[i] - top[i]) * t) for i in range(3)) + (255,))

mask = Image.new("L", (S, S), 0)
ImageDraw.Draw(mask).rounded_rectangle([0, 0, S - 1, S - 1], radius=round(S * 0.20), fill=255)

img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
img.paste(grad, (0, 0), mask)

# soft top highlight for depth
sheen = Image.new("RGBA", (S, S), (0, 0, 0, 0))
ImageDraw.Draw(sheen).ellipse([S * 0.10, -S * 0.55, S * 0.90, S * 0.30], fill=(255, 255, 255, 26))
sheen = sheen.filter(ImageFilter.GaussianBlur(S * 0.06))
img = Image.alpha_composite(img, clipped(sheen, mask))

# wave geometry shared with logo.svg; node dot sits at the curve's middle joint
k = S / 100.0
wave = quad((20, 50), (40, 20), (60, 50), k=k) + quad((60, 50), (80, 80), (80, 50), k=k)[1:]
node = (60 * k, 50 * k)
dot_r = 8 * k

glow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
stamp_stroke(glow, wave, 6 * k * 2.1, (255, 255, 255, 130))
ImageDraw.Draw(glow).ellipse(
    [node[0] - dot_r * 1.6, node[1] - dot_r * 1.6, node[0] + dot_r * 1.6, node[1] + dot_r * 1.6],
    fill=(255, 255, 255, 150))
glow = glow.filter(ImageFilter.GaussianBlur(S * 0.020))
img = Image.alpha_composite(img, clipped(glow, mask))

fg = Image.new("RGBA", (S, S), (0, 0, 0, 0))
stamp_stroke(fg, wave, 6 * k, (255, 255, 255, 255))
ImageDraw.Draw(fg).ellipse(
    [node[0] - dot_r, node[1] - dot_r, node[0] + dot_r, node[1] + dot_r],
    fill=(255, 255, 255, 255))
img = Image.alpha_composite(img, clipped(fg, mask))

img.save(OUT, format="ICO", sizes=[(s, s) for s in (16, 24, 32, 48, 64, 128, 256)])
print("wrote", OUT, os.path.getsize(OUT), "bytes")

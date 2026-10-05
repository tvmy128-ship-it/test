"""Pillow/numpy drawing used by the image mocks: colour words from prompts, blocky character sheets, flat shapes
with real anti-aliased alpha, and the mask painter. Pure functions of their arguments (deterministic)."""
from __future__ import annotations

import io
import random
import re
from typing import Any

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

RGB = tuple[int, int, int]

COLOR_WORDS: dict[str, RGB] = {
    "red": (200, 45, 50), "crimson": (170, 25, 50), "scarlet": (225, 50, 40), "maroon": (110, 25, 40), "orange": (235, 130, 40),
    "amber": (240, 170, 40), "yellow": (240, 205, 60), "gold": (215, 175, 55), "lime": (150, 205, 60), "green": (60, 160, 80),
    "emerald": (30, 150, 100), "olive": (120, 130, 50), "teal": (30, 150, 150), "turquoise": (60, 200, 190), "cyan": (70, 200, 225),
    "aqua": (90, 210, 220), "sky": (110, 180, 230), "blue": (50, 90, 200), "navy": (30, 45, 110), "indigo": (75, 60, 160),
    "purple": (120, 70, 170), "violet": (140, 90, 200), "lavender": (180, 160, 220), "magenta": (200, 50, 160), "pink": (235, 140, 175),
    "rose": (225, 100, 130), "coral": (240, 120, 100), "salmon": (245, 140, 120), "peach": (250, 190, 150), "brown": (120, 80, 50),
    "chocolate": (95, 60, 40), "tan": (200, 165, 120), "beige": (220, 200, 160), "cream": (245, 235, 205), "white": (245, 245, 245),
    "ivory": (250, 245, 225), "grey": (140, 140, 145), "gray": (140, 140, 145), "silver": (190, 195, 200), "charcoal": (60, 62, 70),
    "black": (35, 35, 40), "mint": (150, 220, 180), "burgundy": (115, 30, 55), "khaki": (190, 175, 110), "denim": (70, 100, 150),
}
SKIN_TONES: tuple[RGB, ...] = ((255, 224, 196), (240, 200, 160), (222, 170, 130), (190, 130, 95), (150, 100, 70), (110, 75, 55))
BACKGROUND: RGB = (242, 242, 242)
_WORD = re.compile(r"[a-z]+")


def colors_from_text(text: str, limit: int = 6) -> list[RGB]:
    """Colours named in ``text`` (``COLOR_WORDS``), in order of first appearance, without repeats."""
    out: list[RGB] = []
    seen: set[str] = set()
    for w in _WORD.findall(text.lower()):
        if w in COLOR_WORDS and w not in seen:
            seen.add(w)
            out.append(COLOR_WORDS[w])
            if len(out) >= limit:
                break
    return out


def fallback_colors(rng: random.Random, k: int = 3) -> list[RGB]:
    names = sorted(COLOR_WORDS)
    return [COLOR_WORDS[rng.choice(names)] for _ in range(k)]


def palette_for(text: str, rng: random.Random, k: int = 3) -> list[RGB]:
    cols = colors_from_text(text)
    while len(cols) < k:
        cols.append(fallback_colors(rng, 1)[0])
    return cols


def darker(c: RGB, f: float = 0.6) -> RGB:
    return (int(c[0] * f), int(c[1] * f), int(c[2] * f))


def lighter(c: RGB, f: float = 0.35) -> RGB:
    return (int(c[0] + (255 - c[0]) * f), int(c[1] + (255 - c[1]) * f), int(c[2] + (255 - c[2]) * f))


def shade(c: RGB, delta: int) -> RGB:
    return (max(0, min(255, c[0] + delta)), max(0, min(255, c[1] + delta)), max(0, min(255, c[2] + delta)))


def to_png(im: Image.Image) -> bytes:
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


# --------------------------------------------------------------------------------------------------------------
# Blocky character concept sheets
# --------------------------------------------------------------------------------------------------------------

def _figure(d: ImageDraw.ImageDraw, cx: int, top: int, height: int, pal: dict[str, RGB], *, back: bool, line: int) -> None:
    """One blocky figure (head, torso, two arms, two legs) centred on ``cx``."""
    u = height / 10.0                                  # a stud-ish unit: head 2u, torso 3.5u, legs 3.5u ... = 9u + hair
    head_w, head_h = int(2.2 * u), int(2.0 * u)
    torso_w, torso_h = int(3.2 * u), int(3.6 * u)
    arm_w, arm_h = int(1.1 * u), int(3.6 * u)
    leg_w, leg_h = int(1.55 * u), int(3.4 * u)
    y = top
    outline = darker(pal["pants"], 0.35)

    def box(x0, y0, x1, y1, fill):
        d.rectangle([x0, y0, x1, y1], fill=fill, outline=outline, width=line)

    # head + hair + face
    hx0, hy0 = cx - head_w // 2, y
    box(hx0, hy0, hx0 + head_w, hy0 + head_h, pal["skin"])
    if back:
        box(hx0, hy0, hx0 + head_w, hy0 + head_h, pal["hair"])
    else:
        box(hx0, hy0, hx0 + head_w, hy0 + int(head_h * 0.38), pal["hair"])
        ex = int(head_w * 0.2)
        ey = hy0 + int(head_h * 0.55)
        ew = max(2, int(head_w * 0.12))
        for sx in (hx0 + ex, hx0 + head_w - ex - ew):
            d.rectangle([sx, ey, sx + ew, ey + int(ew * 1.6)], fill=(30, 30, 40))
        d.rectangle([cx - ew, hy0 + int(head_h * 0.82), cx + ew, hy0 + int(head_h * 0.82) + max(1, line)], fill=(120, 60, 60))
    y += head_h
    # arms (sleeve + skin) and torso
    tx0 = cx - torso_w // 2
    box(tx0, y, tx0 + torso_w, y + torso_h, pal["shirt"])
    for ax in (tx0 - arm_w, tx0 + torso_w):
        box(ax, y, ax + arm_w, y + int(arm_h * 0.55), pal["shirt"])
        box(ax, y + int(arm_h * 0.55), ax + arm_w, y + arm_h, pal["skin"])
    # a belt-like trim so the torso reads as clothing
    d.rectangle([tx0, y + torso_h - int(0.35 * u), tx0 + torso_w, y + torso_h], fill=pal["trim"])
    y += torso_h
    # legs and shoes
    for lx in (cx - leg_w, cx):
        box(lx, y, lx + leg_w, y + leg_h, pal["pants"])
        d.rectangle([lx, y + leg_h - int(0.6 * u), lx + leg_w, y + leg_h], fill=pal["shoes"], outline=outline, width=line)


def blocky_sheet(w: int, h: int, colors: list[RGB], rng: random.Random, *, skin: RGB | None = None) -> Image.Image:
    """A front and a back view of a blocky character on a flat light backdrop, coloured from ``colors``
    (shirt, pants, hair, trim ...). Wide canvases get two figures side by side, tall ones a single front view."""
    im = Image.new("RGB", (w, h), BACKGROUND)
    d = ImageDraw.Draw(im)
    pal = {"shirt": colors[0], "pants": colors[1 % len(colors)], "hair": colors[2 % len(colors)], "trim": colors[3 % len(colors)] if len(colors) > 3 else lighter(colors[0]),
           "skin": skin or rng.choice(SKIN_TONES), "shoes": darker(colors[1 % len(colors)], 0.45)}
    line = max(2, min(w, h) // 256)
    fh = int(h * 0.82)
    top = (h - fh) // 2
    if w >= int(h * 1.1):
        _figure(d, w // 4, top, fh, pal, back=False, line=line)
        _figure(d, 3 * w // 4, top, fh, pal, back=True, line=line)
    else:
        _figure(d, w // 2, top, fh, pal, back=False, line=line)
    return im


# --------------------------------------------------------------------------------------------------------------
# One centred flat shape with real alpha
# --------------------------------------------------------------------------------------------------------------

def _sdf(kind: str, x: np.ndarray, y: np.ndarray, r: float, rng: random.Random) -> np.ndarray:
    """Signed distance (pixels; negative inside) of a shape of radius ~r centred at the origin."""
    if kind == "circle":
        return np.hypot(x, y) - r
    if kind == "rounded_box":
        b = r * 0.8
        qx, qy = np.abs(x) - b + r * 0.25, np.abs(y) - b * 0.85 + r * 0.25
        return np.hypot(np.maximum(qx, 0), np.maximum(qy, 0)) + np.minimum(np.maximum(qx, qy), 0) - r * 0.25
    if kind == "diamond":
        return (np.abs(x) + np.abs(y)) / np.sqrt(2) - r * 0.65
    if kind == "hexagon":
        k = np.array([-0.866025404, 0.5, 0.577350269])
        px, py = np.abs(x), np.abs(y)
        dot = np.minimum(k[0] * px + k[1] * py, 0.0)
        px, py = px - 2.0 * dot * k[0], py - 2.0 * dot * k[1]
        px = px - np.clip(px, -k[2] * r * 0.9, k[2] * r * 0.9)
        py = py - r * 0.9
        return np.hypot(px, py) * np.sign(py)
    # "blob": two overlapping circles
    off = r * 0.35
    return np.minimum(np.hypot(x - off, y) - r * 0.62, np.hypot(x + off, y + r * 0.1) - r * 0.55)


def shape_rgba(w: int, h: int, color: RGB, rng: random.Random, *, margin: float = 0.14, outline: bool = True,
               kind: str | None = None) -> Image.Image:
    """One centred flat shape with an outline and a soft highlight on a fully transparent canvas. The alpha edge is
    anti-aliased (real alpha values between 0 and 255); nothing touches the canvas edge."""
    kind = kind or rng.choice(["circle", "rounded_box", "diamond", "hexagon", "blob"])
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    x, y = xx - (w - 1) / 2.0, yy - (h - 1) / 2.0
    r = min(w, h) * (0.5 - margin)
    sd = _sdf(kind, x, y, r, rng).astype(np.float32)
    alpha = np.clip(0.5 - sd, 0.0, 1.0)
    rgb = np.zeros((h, w, 3), np.float32)
    rgb[:] = color
    if outline:
        band = np.clip((sd + max(3.0, r * 0.05)) / 2.0, 0.0, 1.0)         # 0 inside the fill ... 1 at the very edge
        rgb = rgb * (1 - band[..., None]) + np.array(darker(color, 0.5), np.float32) * band[..., None]
    hi = np.clip(1.0 - np.hypot(x + r * 0.3, y + r * 0.35) / (r * 0.6), 0.0, 1.0) * 0.28 * (sd < -r * 0.08)
    rgb = rgb * (1 - hi[..., None]) + 255.0 * hi[..., None]
    out = np.dstack([np.clip(rgb, 0, 255), alpha * 255.0]).astype(np.uint8)
    return Image.fromarray(out, "RGBA")


def over_background(im: Image.Image, bg: RGB = BACKGROUND) -> Image.Image:
    base = Image.new("RGBA", im.size, (*bg, 255))
    base.alpha_composite(im.convert("RGBA"))
    return base.convert("RGB")


# --------------------------------------------------------------------------------------------------------------
# Edits
# --------------------------------------------------------------------------------------------------------------

def paint_masked(base: Image.Image, mask_png: bytes, color: RGB) -> Image.Image:
    """Image 1 with a flat ``color`` painted where the mask is editable (alpha 0); every other pixel is untouched."""
    arr = np.array(base.convert("RGBA"))
    m = np.asarray(Image.open(io.BytesIO(mask_png)).convert("RGBA"))[..., 3]
    editable = m == 0
    arr[editable] = (*color, 255)
    return Image.fromarray(arr, "RGBA")


def lightly_sharpened(base: Image.Image) -> Image.Image:
    """A copy with a mild unsharp mask (alpha untouched): what FINALIZE returns; passes the drift check."""
    rgba = base.convert("RGBA")
    rgb = rgba.convert("RGB").filter(ImageFilter.UnsharpMask(radius=1.0, percent=35, threshold=3))
    out = rgb.convert("RGBA")
    out.putalpha(rgba.getchannel("A"))
    return out


def decode(png: bytes) -> Image.Image:
    return Image.open(io.BytesIO(png))


def mean_color(png: bytes, *, ignore_near: RGB | None = BACKGROUND) -> RGB:
    """Average colour of the non-background, non-transparent pixels of an image (used to colour mock 3D models)."""
    arr = np.asarray(decode(png).convert("RGBA")).astype(np.int32)
    keep = arr[..., 3] > 40
    if ignore_near is not None:
        keep &= np.abs(arr[..., :3] - np.array(ignore_near)).sum(axis=-1) > 40
    if not keep.any():
        return (150, 150, 160)
    m = arr[keep][:, :3].mean(axis=0)
    return (int(m[0]), int(m[1]), int(m[2]))


def tokens_for_text(text: str) -> int:
    return max(1, -(-len(text) // 4))


def jsonable_summary(**kw: Any) -> dict[str, Any]:  # pragma: no cover - tiny helper
    return dict(kw)

"""MockRecraft: deterministic Recraft stand-in (APP_SPEC 7.8).

``generate`` returns simple valid SVGs: a full-size background rectangle in the sentinel colour (``controls.
background_color`` when given) and a few primitives with explicit ``fill`` attributes, no gradients, no CSS, no ``<use>``;
the shapes take the colours of ``controls.colors`` or the colours named in the prompt. ``credits`` and image ids are
returned like the real API. ``create_style`` returns a fixed UUID. ``vectorize`` traces an image into rectangles and
``remove_background`` makes the border-connected backdrop transparent.

Faults: ``429``/``rate_limit``, ``500``/``server``, ``401``/``auth``, ``402``/``billing``, ``moderation``, ``style_required``,
``timeout`` raise the real errors; ``hostile_svg`` returns an SVG full of banned constructs (for the sanitizer tests);
``png_instead_of_svg`` returns PNG bytes, which the shared ``ensure_svg`` check turns into the real validation error.
"""
from __future__ import annotations

import io
import random
from collections.abc import Callable
from typing import Any, Literal

import numpy as np
from PIL import Image

from duoskin.providers.base import CallCtx, CapabilityFlags, ProviderError, request_hash, seed_from_hash
from duoskin.providers.faults import FaultInjector
from duoskin.providers.mock import _draw as D
from duoskin.providers.mock._common import MockBase
from duoskin.providers.pricing import recraft_cost
from duoskin.providers.recraft import (
    StyleRegistry,
    VectorRequest,
    VectorResult,
    check_image_input,
    ensure_svg,
    style_kind_for_model,
    validate_request,
    validate_style_inputs,
)

MOCK_STYLE_ID = "5eed0000-0000-4000-8000-00000000d0c5"
SENTINEL_RGB = (255, 0, 255)

HOSTILE_SVG = b"""<?xml version="1.0"?>
<!DOCTYPE svg [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>
<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" viewBox="0 0 100 100" onload="alert(1)">
  <script>alert(document.cookie)</script>
  <style>@import url(http://evil.example/x.css); rect { fill: url(http://evil.example/p) }</style>
  <rect width="100" height="100" fill="#ff00ff"/>
  <image href="http://evil.example/track.png" width="10" height="10"/>
  <use xlink:href="file:///etc/passwd#x"/>
  <foreignObject width="50" height="50"><div xmlns="http://www.w3.org/1999/xhtml">hi</div></foreignObject>
  <circle cx="50" cy="50" r="20" fill="#336699" onclick="steal()"/>
  <text x="5" y="95">&xxe;</text>
</svg>
"""


def _hex(c: tuple[int, int, int]) -> str:
    return "#{:02x}{:02x}{:02x}".format(*c)


def make_svg(w: int, h: int, bg: tuple[int, int, int], colors: list[tuple[int, int, int]], rng: random.Random) -> bytes:
    """A valid, flat SVG: sentinel background, then ellipse, bezier path, polygon and rounded rect with explicit fills."""
    cx, cy = w / 2, h / 2
    r = min(w, h) * 0.28
    c = [colors[i % len(colors)] for i in range(4)]
    j = lambda v: round(v * rng.uniform(0.9, 1.1), 1)  # noqa: E731 - tiny local jitter helper
    parts = [
        f'<rect x="0" y="0" width="{w}" height="{h}" fill="{_hex(bg)}"/>',
        f'<ellipse cx="{cx:.1f}" cy="{cy:.1f}" rx="{j(r * 1.25)}" ry="{j(r)}" fill="{_hex(c[0])}"/>',
        f'<path d="M {cx - r:.1f} {cy:.1f} C {cx - r:.1f} {cy - j(r * 1.1)} {cx + r:.1f} {cy - j(r * 1.1)} {cx + r:.1f} {cy:.1f} '
        f'C {cx + r:.1f} {cy + j(r * 0.5)} {cx - r:.1f} {cy + j(r * 0.5)} {cx - r:.1f} {cy:.1f} Z" fill="{_hex(c[1])}"/>',
        f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{j(r * 0.45)}" fill="{_hex(c[2])}"/>',
        f'<polygon points="{cx:.1f},{cy - r * 0.2:.1f} {cx + r * 0.2:.1f},{cy + r * 0.2:.1f} {cx - r * 0.2:.1f},{cy + r * 0.2:.1f}" fill="{_hex(c[3])}"/>',
        f'<rect x="{cx - r * 0.5:.1f}" y="{cy + r * 0.7:.1f}" width="{r:.1f}" height="{r * 0.18:.1f}" rx="{r * 0.09:.1f}" fill="{_hex(c[2])}"/>',
    ]
    body = "\n  ".join(parts)
    return f'<?xml version="1.0" encoding="UTF-8"?>\n<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="{w}" height="{h}">\n  {body}\n</svg>\n'.encode()


def trace_rects(png: bytes, max_shapes: int | None = None) -> bytes:
    """Trace an image into row-merged rectangles with explicit fills (a crude vectoriser)."""
    im = Image.open(io.BytesIO(png)).convert("RGBA")
    w, h = im.size
    cells = 48
    while True:
        cw = max(1, -(-w // cells))
        ch = max(1, -(-h // cells))
        small = im.resize((max(1, w // cw), max(1, h // ch)), Image.NEAREST)
        arr = np.asarray(small)
        rects: list[str] = []
        for y in range(arr.shape[0]):
            x = 0
            while x < arr.shape[1]:
                px = arr[y, x]
                if px[3] < 40:
                    x += 1
                    continue
                q = tuple(int(v) // 32 * 32 + 16 for v in px[:3])
                x2 = x + 1
                while x2 < arr.shape[1] and arr[y, x2][3] >= 40 and tuple(int(v) // 32 * 32 + 16 for v in arr[y, x2][:3]) == q:
                    x2 += 1
                rects.append(f'<rect x="{x * cw}" y="{y * ch}" width="{(x2 - x) * cw}" height="{ch}" fill="{_hex(q)}"/>')  # type: ignore[arg-type]
                x = x2
        if max_shapes is None or len(rects) <= max_shapes or cells <= 4:
            break
        cells = max(4, cells // 2)
    rects = rects[: max_shapes or len(rects)]
    return (f'<?xml version="1.0" encoding="UTF-8"?>\n<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="{w}" height="{h}">\n  '
            + "\n  ".join(rects) + "\n</svg>\n").encode()


def cut_background(png: bytes, tolerance: int = 40) -> bytes:
    """RGBA copy where the backdrop (colour-near-corner pixels connected to the border) is transparent."""
    from scipy import ndimage
    im = Image.open(io.BytesIO(png)).convert("RGBA")
    arr = np.array(im)
    corner = np.median(np.array([arr[0, 0], arr[0, -1], arr[-1, 0], arr[-1, -1]])[:, :3], axis=0)
    near = np.abs(arr[..., :3].astype(int) - corner.astype(int)).sum(axis=-1) <= tolerance
    lab, _ = ndimage.label(near)
    border = set(np.unique(np.concatenate([lab[0], lab[-1], lab[:, 0], lab[:, -1]]))) - {0}
    bg = np.isin(lab, list(border))
    arr[bg, 3] = 0
    return D.to_png(Image.fromarray(arr, "RGBA"))


class MockRecraft(MockBase):
    """Deterministic Recraft provider."""

    name = "recraft"

    def __init__(self, *, faults: FaultInjector | None = None, cost_sink: Callable[[dict[str, Any]], None] | None = None,
                 flags: CapabilityFlags | None = None, registry: StyleRegistry | None = None) -> None:
        super().__init__(faults=faults, cost_sink=cost_sink, flags=flags)
        self.styles = registry or StyleRegistry()

    def __repr__(self) -> str:
        return "MockRecraft()"

    def generate(self, req: VectorRequest, ctx: CallCtx) -> VectorResult:
        validate_request(req, registry=self.styles)
        digest = request_hash({"op": "generate", "req": req})
        self.record("recraft", "images.generate", {"op": "generate", "req": req})
        ctx.tick()
        behaviour = self.fault("recraft", tag=req.tag)
        w, h = (int(v) for v in req.size.split("x"))
        pal = list(req.colors) or D.palette_for(req.prompt, random.Random(digest[:8]), 4)
        bg = req.background_rgb or SENTINEL_RGB
        svgs: list[bytes] = []
        for i in range(req.n):
            rng = random.Random(seed_from_hash(digest) + 104729 * i)
            colors = pal[i % len(pal):] + pal[: i % len(pal)] if len(pal) > 1 else pal
            raw = make_svg(w, h, bg, colors or pal, rng)
            if behaviour == "hostile_svg":
                raw = HOSTILE_SVG
            elif behaviour == "png_instead_of_svg":
                raw = D.to_png(Image.new("RGB", (64, 64), (200, 30, 30)))
            svgs.append(ensure_svg(raw))
        ids = [f"mock-img-{digest[:8]}-{i}" for i in range(req.n)]
        credits = 80.0 * req.n if "styles" not in req.model else 50.0 * req.n
        cost = self.record_cost(recraft_cost(req.model, operation=f"images.generate:{req.tag or 'vec'}", n=req.n, api_units=credits,
                                             request_id=f"mock-recraft-{digest[:12]}"))
        style_id = req.style_id
        body = {"prompt": req.prompt, "model": req.model, "size": req.size, "n": req.n, "nonce": req.nonce}
        return VectorResult(svgs=svgs, image_ids=ids, credits=credits, style_id=style_id, request_json=body, cost=cost,
                            request_id=f"mock-recraft-{digest[:12]}")

    def create_style(self, pngs: list[bytes], *, model: Literal["recraftv4_styles_vector", "recraftv4_styles"],
                     style: Literal["vector_illustration", "any"], ctx: CallCtx | None = None) -> str:
        validate_style_inputs(pngs, model, style)
        self.record("recraft", "styles.create", {"op": "create_style", "pngs": pngs, "model": model, "style": style})
        self.fault("recraft")
        self.record_cost(recraft_cost("style_create", operation="styles.create"))
        self.styles.register(style_kind_for_model(model), f"style:{MOCK_STYLE_ID}", MOCK_STYLE_ID)
        return MOCK_STYLE_ID

    def vectorize(self, png: bytes, *, max_num_shapes: int | None = None, ctx: CallCtx | None = None) -> bytes:
        check_image_input(png, "vectorize input")
        if max_num_shapes is not None and max_num_shapes < 1:
            raise ProviderError("recraft", "bad_request", "max_num_shapes must be >= 1", code="bad_shapes", billed="no")
        self.record("recraft", "images.vectorize", {"op": "vectorize", "png": png, "max": max_num_shapes})
        self.fault("recraft")
        self.record_cost(recraft_cost("vectorize", operation="images.vectorize"))
        return ensure_svg(trace_rects(png, max_num_shapes))

    def remove_background(self, png: bytes, *, ctx: CallCtx | None = None) -> bytes:
        check_image_input(png, "removeBackground input")
        self.record("recraft", "images.removeBackground", {"op": "remove_background", "png": png})
        self.fault("recraft")
        self.record_cost(recraft_cost("remove_background", operation="images.removeBackground"))
        return cut_background(png)

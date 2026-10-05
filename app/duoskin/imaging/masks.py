"""Masks, legal sizes and paste-back (PROMPT_BIBLE §2.6, FAILURE_MODES §5.4, GEN-01..GEN-03).

* ``make_mask`` builds the OpenAI edit mask: RGBA PNG at exactly Image 1's size, **alpha 0 = the model may change this area**.
* ``paste_back`` re-pastes the original outside the mask with a feather and *guarantees* that pixels farther than
  ``reach(feather)`` from the editable area are byte-identical to the original.
* ``pasteback_verify`` / ``check_paste`` implement the A_PASTE rule: the 4-8 px ring just outside the mask must not have shifted
  (mean CIEDE2000 <= 3) and the outside must be identical after the paste.
* ``LegalMap`` maps an asset onto a legal GPT Image canvas and back (``585x559`` is not a legal size).
"""
from __future__ import annotations

import io
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

from duoskin.checks import thresholds as TH
from duoskin.checks.model import CheckResult
from duoskin.checks.runner import build_result
from duoskin.imaging import palette as P

MAX_MASK_BYTES = 4 * 1024 * 1024


# ------------------------------------------------------------------ sizes
def valid_size(w: int, h: int) -> bool:
    """Legal GPT Image output size: both sides multiples of 16, ratio <= 3, 0.65-8.3 megapixels, long side < 3840."""
    return (w % 16 == 0 and h % 16 == 0 and max(w, h) / min(w, h) <= 3
            and 655_360 <= w * h <= 8_294_400 and max(w, h) < 3840)


@dataclass(frozen=True)
class LegalMap:
    """How an asset was placed on a legal canvas, so the repaired result can be mapped back (the "legal-size rule")."""

    orig_size: tuple[int, int]
    scale: int
    offset: tuple[int, int]
    canvas_size: tuple[int, int]

    def to_canvas(self, im: Image.Image, fill: tuple[int, int, int, int] = (242, 242, 242, 255)) -> Image.Image:
        """Upscale by the integer ``scale`` (nearest, so flat art stays flat) and pad onto the legal canvas."""
        if im.size != self.orig_size:
            raise ValueError(f"expected {self.orig_size}, got {im.size}")
        up = im.convert("RGBA").resize((im.width * self.scale, im.height * self.scale), Image.Resampling.NEAREST)
        canvas = Image.new("RGBA", self.canvas_size, fill)
        canvas.paste(up, self.offset)
        return canvas

    def to_orig(self, canvas: Image.Image) -> Image.Image:
        """Crop the asset area out of the canvas and area-downsample it back to the original size."""
        if canvas.size != self.canvas_size:
            raise ValueError(f"expected {self.canvas_size}, got {canvas.size}")
        x, y = self.offset
        crop = canvas.convert("RGBA").crop((x, y, x + self.orig_size[0] * self.scale, y + self.orig_size[1] * self.scale))
        if self.scale == 1:
            return crop
        return crop.convert("RGBa").resize(self.orig_size, Image.Resampling.BOX).convert("RGBA")

    def editable_to_canvas(self, editable: np.ndarray) -> np.ndarray:
        """Map a bool mask in asset coordinates to canvas coordinates."""
        up = np.kron(editable.astype(np.uint8), np.ones((self.scale, self.scale), np.uint8)).astype(bool)
        out = np.zeros((self.canvas_size[1], self.canvas_size[0]), bool)
        x, y = self.offset
        out[y:y + up.shape[0], x:x + up.shape[1]] = up
        return out


def legal_map(w: int, h: int) -> LegalMap:
    """Choose the smallest integer scale and a centred pad so that ``valid_size`` holds for the canvas."""
    for scale in range(1, 9):
        sw, sh = w * scale, h * scale
        cw = -(-sw // 16) * 16
        ch = -(-sh // 16) * 16
        # grow the short side if the aspect ratio would be above 3
        if max(cw, ch) / min(cw, ch) > 3:
            if cw > ch:
                ch = -(-math.ceil(cw / 3) // 16) * 16
            else:
                cw = -(-math.ceil(ch / 3) // 16) * 16
        if valid_size(cw, ch):
            return LegalMap((w, h), scale, ((cw - sw) // 2, (ch - sh) // 2), (cw, ch))
    raise ValueError(f"no legal canvas for {w}x{h}")


# ------------------------------------------------------------------ shapes to masks
def box_mask(shape: tuple[int, int], boxes: Iterable[Sequence[int]]) -> np.ndarray:
    """Bool mask ``(H, W)`` that is True inside every ``(x0, y0, x1, y1)`` box (x1, y1 exclusive)."""
    m = np.zeros(shape, bool)
    for x0, y0, x1, y1 in boxes:
        m[max(0, y0):max(0, y1), max(0, x0):max(0, x1)] = True
    return m


def polygon_mask(shape: tuple[int, int], points: Sequence[tuple[float, float]]) -> np.ndarray:
    im = Image.new("L", (shape[1], shape[0]), 0)
    ImageDraw.Draw(im).polygon([(float(x), float(y)) for x, y in points], fill=255)
    return np.asarray(im) > 0


def ellipse_mask(shape: tuple[int, int], box: Sequence[float]) -> np.ndarray:
    im = Image.new("L", (shape[1], shape[0]), 0)
    ImageDraw.Draw(im).ellipse([float(v) for v in box], fill=255)
    return np.asarray(im) > 0


# ------------------------------------------------------------------ the edit mask
def make_mask(editable: np.ndarray) -> bytes:
    """``HxW`` bool (True = the model may change it) to an RGBA PNG whose alpha is 0 where editable (OpenAI convention)."""
    if editable.ndim != 2:
        raise ValueError("editable must be a 2-D bool array")
    m = np.zeros((*editable.shape, 4), np.uint8)
    m[..., 3] = np.where(editable, 0, 255)
    buf = io.BytesIO()
    Image.fromarray(m, "RGBA").save(buf, "PNG")
    return buf.getvalue()


def editable_from_mask(mask_png: bytes | Image.Image) -> np.ndarray:
    """Inverse of ``make_mask``: True where alpha is 0. A mask without an alpha channel is rejected (GEN-02)."""
    im = Image.open(io.BytesIO(mask_png)) if isinstance(mask_png, (bytes, bytearray)) else mask_png
    if im.mode != "RGBA":
        raise ValueError(f"mask must be RGBA, got {im.mode}")
    return np.asarray(im)[..., 3] == 0


def validate_mask(mask_png: bytes, image_size: tuple[int, int]) -> list[str]:
    """Problems with an edit mask (empty list = fine): RGBA, same size as Image 1, alpha only 0 or 255, under 4 MB (GEN-02)."""
    problems: list[str] = []
    if len(mask_png) >= MAX_MASK_BYTES:
        problems.append("mask is 4 MB or larger")
    try:
        im = Image.open(io.BytesIO(mask_png))
        im.load()
    except Exception as e:  # noqa: BLE001
        return [f"mask is not a readable image: {type(e).__name__}"]
    if im.format != "PNG":
        problems.append(f"mask is {im.format}, not PNG")
    if im.mode != "RGBA":
        problems.append(f"mask mode is {im.mode}; an alpha channel is required")
        return problems
    if im.size != tuple(image_size):
        problems.append(f"mask size {im.size} differs from image size {tuple(image_size)}")
    a = np.asarray(im)[..., 3]
    if not np.isin(a, (0, 255)).all():
        problems.append("mask alpha must be exactly 0 or 255")
    return problems


def png(name: str, data: bytes) -> tuple[str, bytes, str]:
    """``(name, bytes, mime)`` tuple that the OpenAI SDK accepts as a file (never a bare ``BytesIO``)."""
    return (name, data, "image/png")


# ------------------------------------------------------------------ paste-back
def _reach(feather: int) -> int:
    """Pixels beyond this distance from the editable area get weight exactly 0 (a Gaussian is below 1/255 there)."""
    return 3 * int(feather) + 1


def feather_weights(editable: np.ndarray, feather: int = 4) -> Image.Image:
    """Soft 8-bit blend mask: 255 inside the editable area, a Gaussian ramp outside, and exactly 0 beyond ``reach(feather)``."""
    base = Image.fromarray(editable.astype(np.uint8) * 255, "L")
    if feather <= 0:
        return base
    soft = np.asarray(base.filter(ImageFilter.GaussianBlur(feather))).copy()
    far = ~P.dilate(editable, _reach(feather))
    soft[far] = 0
    return Image.fromarray(np.maximum(soft, np.asarray(base)), "L")


def paste_back(orig: Image.Image, out: Image.Image, editable: np.ndarray, feather: int = 4) -> Image.Image:
    """Paste the model's output over the original **only** inside the editable area (feathered), RGBA.

    Raises on a size difference: a drifted size is rejected, never resized (GEN-01).
    """
    if orig.size != out.size:
        raise ValueError("size drift: reject, never resize")
    w = feather_weights(editable, feather)
    return Image.composite(out.convert("RGBA"), orig.convert("RGBA"), w)


def _on_grey(im: Image.Image) -> np.ndarray:
    """RGB floats (0..1) of ``im`` composited on #808080 (RGB under alpha 0 is undefined, FM §5.4)."""
    base = Image.new("RGBA", im.size, (128, 128, 128, 255))
    return np.asarray(Image.alpha_composite(base, im.convert("RGBA")).convert("RGB"), dtype=np.float64)


def ring_delta_e(orig: Image.Image, raw_out: Image.Image, editable: np.ndarray, ring: Sequence[int] | None = None) -> float:
    """Mean CIEDE2000 between the original and the raw model output in the ring just outside the editable area."""
    r0, r1 = ring if ring is not None else TH.get("img.pasteback_ring_px")
    ring_mask = P.dilate(editable, int(r1)) & ~P.dilate(editable, int(r0))
    if not ring_mask.any():
        return 0.0
    lo = P.srgb_to_lab(_on_grey(orig)[ring_mask])
    lr = P.srgb_to_lab(_on_grey(raw_out)[ring_mask])
    return float(P.deltaE2000(lo, lr).mean())


def pasteback_verify(orig: Image.Image, raw_out: Image.Image, editable: np.ndarray, feather: int = 4,
                     ring: Sequence[int] | None = None, de_max: float | None = None) -> tuple[bool, bool, Image.Image]:
    """GEN-03. Returns ``(content_not_shifted, outside_identical, merged)``.

    ``content_not_shifted``: the ring just outside the mask is within ``de_max`` of the original (the model did not move the
    figure). ``outside_identical``: every pixel farther than ``reach(feather)`` from the mask equals the original.
    """
    if orig.size != raw_out.size:
        raise ValueError("GEN-01 size drift: reject, never resize before paste-back")
    lim = float(TH.get("img.pasteback_ring_de_max")) if de_max is None else de_max
    shift_ok = ring_delta_e(orig, raw_out, editable, ring) <= lim
    merged = paste_back(orig, raw_out, editable, feather)
    outside = ~P.dilate(editable, _reach(feather))
    same = bool((np.asarray(merged)[outside] == np.asarray(orig.convert("RGBA"))[outside]).all())
    return shift_ok, same, merged


def check_paste(orig: Image.Image, raw_out: Image.Image, editable: np.ndarray, *, feather: int = 4,
                subject_sha: str = "") -> CheckResult:
    """A_PASTE (CHK-A09, HARD): the ring did not shift and the outside is identical after the paste-back."""
    if orig.size != raw_out.size:
        return build_result("A_PASTE", passed=False, subject_sha=subject_sha, metric="size",
                            evidence=f"size drift {raw_out.size} vs {orig.size}", fix_hint="regenerate")
    de = ring_delta_e(orig, raw_out, editable)
    shift_ok, same, _ = pasteback_verify(orig, raw_out, editable, feather)
    ok = shift_ok and same
    why = []
    if not shift_ok:
        why.append(f"ring dE {de:.2f} above limit (content shifted)")
    if not same:
        why.append("pixels outside the mask changed")
    return build_result("A_PASTE", passed=ok, subject_sha=subject_sha, metric="ring_de2000_mean", value=de,
                        threshold=TH.describe("img.pasteback_ring_de_max", "<="),
                        evidence="; ".join(why) or f"ring dE {de:.2f}, outside identical", fix_hint="masked_edit")

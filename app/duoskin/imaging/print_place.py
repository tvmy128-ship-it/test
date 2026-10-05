"""Print placement for the clothing compositor (APP_SPEC §10.5 step 5, CLO-07/08/12/16).

Prints (the approved RGBA finals from I2/R2) are placed **by code** at region-relative anchors:

* the target is a recipe *print slot* (``recipe.print_slots``: absolute template boxes that are >= 5 px inside the region and
  entirely inside one row band, so no print is ever cut by the R15 split rows 170, 418/419 or 467);
* ``scale`` small/medium/large picks a fraction of the slot; the print keeps its aspect ratio and is never mirrored or
  rotated, and is rendered at 4x with a premultiplied-alpha resample, then box-downsampled with the whole garment;
* a print stays inside its target region unless the spec marks it ``wrap`` (CLO-16); a ``wrap`` print is centred on the
  right edge of its region and split by the adjacency map (the side strip of the part), each piece pasted un-mirrored.

``plan_prints`` is pure: the same inputs give the same ``Placement`` list and pixel pieces.
"""
from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from PIL import Image

from duoskin.roblox import template as T

SCALE = 4
SCALE_FRACTION = {"small": 0.40, "medium": 0.68, "large": 1.0}     # share of the slot's fitting size
MIN_PRINT_PX = 6.0
ALPHA_TRIM = 8                                                      # alpha <= this counts as empty when trimming


@dataclass
class Placement:
    """Where a code-placed print ended up (template px, inclusive box)."""
    part_id: str
    region: str
    box: tuple[int, int, int, int]
    wrap: bool
    scale: str


@dataclass
class PrintSpec:
    """One print to place: ``image`` is the approved RGBA final (uint8, straight alpha)."""
    part_id: str
    region: str
    scale: str
    image: np.ndarray
    wrap: bool = False
    anchor: tuple[float, float] = (0.0, 0.0)      # -1..1 offset of the print inside its slot (0 = centred)
    slot: str | None = None                        # preferred slot role


@dataclass
class PrintPiece:
    """Pixels to composite: straight-alpha RGBA uint8 at 4x, destined for canvas rows/cols (x4, y4) of ``region``."""
    part_id: str
    region: str
    x4: int
    y4: int
    rgba: np.ndarray
    wrap: bool

    @property
    def sha256(self) -> str:
        return hashlib.sha256(np.ascontiguousarray(self.rgba).tobytes()).hexdigest()


@dataclass
class PrintPlan:
    placements: list[Placement] = field(default_factory=list)
    pieces: list[PrintPiece] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def image_sha(img: np.ndarray) -> str:
    return hashlib.sha256(np.ascontiguousarray(img).tobytes() + str(img.shape).encode()).hexdigest()


def _trim(img: np.ndarray) -> np.ndarray | None:
    ys, xs = np.nonzero(img[..., 3] > ALPHA_TRIM)
    if len(ys) == 0:
        return None
    return img[ys.min():ys.max() + 1, xs.min():xs.max() + 1]


def resize_premult(img: np.ndarray, w: int, h: int) -> np.ndarray:
    """Resize straight-alpha RGBA uint8 to (w, h) in premultiplied space (no dark fringes) and return straight RGBA uint8."""
    f = img.astype(np.float32) / 255.0
    a = f[..., 3:4]
    pm = np.concatenate([f[..., :3] * a, a], axis=2)
    chans = []
    for c in range(4):
        im = Image.fromarray(np.ascontiguousarray(pm[..., c]), "F")
        chans.append(np.asarray(im.resize((w, h), Image.Resampling.LANCZOS), dtype=np.float32))
    out = np.stack(chans, axis=2)
    out = np.clip(out, 0.0, 1.0)
    alpha = out[..., 3:4]
    rgb = np.where(alpha > 1e-6, out[..., :3] / np.maximum(alpha, 1e-6), 0.0)
    return np.rint(np.concatenate([np.clip(rgb, 0, 1), alpha], axis=2) * 255.0).astype(np.uint8)


def default_slot(region: str, pants: bool = False) -> tuple[int, int, int, int]:
    """A safe box for a region its recipe has no slot for: 5 px inside the edges, inside the tallest row band, and (Pants leg
    faces) below the possibly hidden rows 355-377."""
    x0, y0, x1, y1 = T.REGIONS[region]
    inset = T.BEVEL_INSET_PX
    bands = list(T.bands_of(region))
    if pants and T.PART_OF[region] in T.LIMB_PARTS and T.FACE_OF[region] in ("f", "b", "l", "r"):
        lo = T.HIDDEN_LEG_ROWS[1] + 1
        bands = [(max(a, lo), b) for a, b in bands if b >= lo]
    by0, by1 = max(bands, key=lambda b: b[1] - b[0])
    return (x0 + inset, max(y0 + inset, by0), x1 - inset, min(y1 - inset, by1))


def _strip_pieces(part: str, p_lo: float, p_hi: float) -> list[tuple[str, float, float, float]]:
    """Split a side-strip interval [p_lo, p_hi) by region: ``(region, local_x_lo, local_x_hi, src_offset)``.

    Handles the B -> R wrap around the part (strip length L)."""
    offs: dict[str, float] = {}
    pos = 0.0
    for r in T.SIDE_CYCLE[part]:
        offs[r] = pos
        pos += T.SIZE[r][0]
    length = pos
    out: list[tuple[str, float, float, float]] = []
    for r, o in offs.items():
        w = T.SIZE[r][0]
        for shift in (-length, 0.0, length):
            lo, hi = max(p_lo + shift, o), min(p_hi + shift, o + w)
            if hi - lo > 1e-9:
                out.append((r, lo - o, hi - o, lo - shift - p_lo))
    return out


def plan_prints(specs: list[PrintSpec], slots: list[Any], *, pants: bool = False) -> PrintPlan:
    """Place every print. ``slots`` are the recipe's ``PrintSlot`` objects (``region``, ``role``, ``box``)."""
    plan = PrintPlan()
    for spec in specs:
        if spec.scale not in SCALE_FRACTION:
            raise ValueError(f"print scale must be one of {sorted(SCALE_FRACTION)}, got {spec.scale!r}")
        if spec.region not in T.REGIONS:
            raise ValueError(f"unknown print region {spec.region!r}")
        img = spec.image
        if img.ndim != 3 or img.shape[2] != 4 or img.dtype != np.uint8:
            raise ValueError("print image must be RGBA uint8")
        tr = _trim(img)
        if tr is None:
            plan.warnings.append(f"{spec.part_id}: print is fully transparent; skipped")
            continue
        slot_box = _pick_slot(spec, slots, plan, pants)
        sx0, sy0, sx1, sy1 = slot_box
        sw, sh = sx1 - sx0 + 1, sy1 - sy0 + 1
        ah, aw = tr.shape[:2]
        f = SCALE_FRACTION[spec.scale]
        k = min(sw / aw, sh / ah) * f
        tw, th = max(aw * k, MIN_PRINT_PX), max(ah * k, MIN_PRINT_PX)
        tw, th = min(tw, float(sw)), min(th, float(sh))
        ax, ay = spec.anchor
        cx = sx0 + sw / 2.0 + max(-1.0, min(1.0, ax)) * max(sw - tw, 0.0) / 2.0
        cy = sy0 + sh / 2.0 + max(-1.0, min(1.0, ay)) * max(sh - th, 0.0) / 2.0
        w4, h4 = max(int(round(tw * SCALE)), SCALE), max(int(round(th * SCALE)), SCALE)
        rgba4 = resize_premult(tr, w4, h4)
        x4 = int(round((cx - w4 / SCALE / 2.0) * SCALE))
        y4 = int(round((cy - h4 / SCALE / 2.0) * SCALE))
        rx0, ry0, rx1, ry1 = T.REGIONS[spec.region]
        if spec.wrap:
            _add_wrap(plan, spec, rgba4, x4, y4)
            continue
        # keep the print inside the region (and the slot); the slot already respects bevels and split rows
        x4 = max(x4, sx0 * SCALE)
        y4 = max(y4, sy0 * SCALE)
        x4 = min(x4, (sx1 + 1) * SCALE - w4)
        y4 = min(y4, (sy1 + 1) * SCALE - h4)
        box = (x4 // SCALE, y4 // SCALE, -(-(x4 + w4) // SCALE) - 1, -(-(y4 + h4) // SCALE) - 1)
        if not (rx0 <= box[0] and box[2] <= rx1 and ry0 <= box[1] and box[3] <= ry1):
            raise AssertionError(f"CLO-16: print {spec.part_id} box {box} leaves region {spec.region}")
        plan.placements.append(Placement(spec.part_id, spec.region, box, False, spec.scale))
        plan.pieces.append(PrintPiece(spec.part_id, spec.region, x4 - rx0 * SCALE, y4 - ry0 * SCALE, rgba4, False))
    return plan


def _pick_slot(spec: PrintSpec, slots: list[Any], plan: PrintPlan, pants: bool) -> tuple[int, int, int, int]:
    mine = [s for s in slots if s.region == spec.region]
    if spec.slot:
        pick = [s for s in mine if s.role == spec.slot]
        mine = pick or mine
    if mine:
        return tuple(mine[0].box)  # type: ignore[return-value]
    plan.warnings.append(f"{spec.part_id}: recipe has no print slot on {spec.region}; used a safe default box")
    return default_slot(spec.region, pants)


def _add_wrap(plan: PrintPlan, spec: PrintSpec, rgba4: np.ndarray, x4: int, y4: int) -> None:
    """Centre the print on the right edge of its region and split it along the side strip of the part."""
    part = T.PART_OF[spec.region]
    if T.FACE_OF[spec.region] not in ("f", "b", "l", "r"):
        raise ValueError("a wrap print must target a side face (f, b, l or r)")
    offs: dict[str, float] = {}
    pos = 0.0
    for r in T.SIDE_CYCLE[part]:
        offs[r] = pos
        pos += T.SIZE[r][0]
    h4, w4 = rgba4.shape[:2]
    edge = offs[spec.region] + T.SIZE[spec.region][0]
    width = w4 / SCALE
    p_lo = edge - width / 2.0
    p_hi = p_lo + width
    for region, lo, hi, src in _strip_pieces(part, p_lo, p_hi):
        c0 = int(round(src * SCALE))
        c1 = min(w4, c0 + int(round((hi - lo) * SCALE)))
        if c1 <= c0:
            continue
        piece = rgba4[:, c0:c1]
        rx0, ry0, rx1, ry1 = T.REGIONS[region]
        lx4 = int(round(lo * SCALE))
        ly4 = y4 - ry0 * SCALE
        if ly4 < 0 or ly4 + h4 > (ry1 - ry0 + 1) * SCALE:
            raise AssertionError(f"wrap print {spec.part_id} leaves the rows of {region}")
        box = (rx0 + lx4 // SCALE, y4 // SCALE, rx0 + -(-(lx4 + piece.shape[1]) // SCALE) - 1, -(-(y4 + h4) // SCALE) - 1)
        plan.placements.append(Placement(spec.part_id, region, box, True, spec.scale))
        plan.pieces.append(PrintPiece(spec.part_id, region, lx4, ly4, piece, True))


def check_placements(placements: list[Placement]) -> list[str]:
    """Problems with already-planned placements (empty = fine): a non-wrap print must lie inside its region (CLO-16), at least
    5 px inside the bevel, and inside one row band (CLO-07/08)."""
    bad: list[str] = []
    for p in placements:
        x0, y0, x1, y1 = T.REGIONS[p.region]
        bx0, by0, bx1, by1 = p.box
        if not p.wrap and not (x0 <= bx0 and bx1 <= x1 and y0 <= by0 and by1 <= y1):
            bad.append(f"{p.part_id}: box {p.box} leaves {p.region}")
        if T.band_of_rows(p.region, by0, by1) is None:
            bad.append(f"{p.part_id}: rows {by0}-{by1} cross a split row of {p.region}")
        inset = T.BEVEL_INSET_PX
        if not p.wrap and not (bx0 >= x0 + inset and by0 >= y0 + inset and bx1 <= x1 - inset and by1 <= y1 - inset):
            bad.append(f"{p.part_id}: box {p.box} is within {inset}px of the {p.region} edge")
    return bad


def composite_pieces(rgb: np.ndarray, alpha: np.ndarray, label: np.ndarray, pieces: list[PrintPiece],
                     print_label: int = 3) -> None:
    """Composite print pieces over the 4x canvas in place, only where the garment already covers (alpha > 0): a print never
    creates garment out of bare skin. ``print_label`` is written where the print alpha is at least half."""
    for p in pieces:
        rx0, ry0 = T.REGIONS[p.region][0] * SCALE, T.REGIONS[p.region][1] * SCALE
        h, w = p.rgba.shape[:2]
        ys, xs = slice(ry0 + p.y4, ry0 + p.y4 + h), slice(rx0 + p.x4, rx0 + p.x4 + w)
        src_a = p.rgba[..., 3:4].astype(np.float32) / 255.0
        cov = (alpha[ys, xs] > 0)[..., None]
        dst = rgb[ys, xs].astype(np.float32)
        out = p.rgba[..., :3].astype(np.float32) * src_a + dst * (1.0 - src_a)
        rgb[ys, xs] = np.where(cov, np.rint(out), dst).astype(np.uint8)
        sel = (p.rgba[..., 3] >= 128) & cov[..., 0]
        label[ys, xs][sel] = print_label


def bbox_of_pieces(pieces: list[PrintPiece]) -> list[tuple[str, tuple[int, int, int, int]]]:
    """Per piece, the region and its 1x inclusive box (for tests)."""
    out = []
    for p in pieces:
        rx0, ry0 = T.REGIONS[p.region][:2]
        h, w = p.rgba.shape[:2]
        out.append((p.region, (rx0 + p.x4 // SCALE, ry0 + p.y4 // SCALE, rx0 + math.ceil((p.x4 + w) / SCALE) - 1,
                               ry0 + math.ceil((p.y4 + h) / SCALE) - 1)))
    return out

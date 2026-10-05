"""Clothing compositor: layered Roblox classic Shirt/Pants templates from JSON recipes (APP_SPEC §10.5, bible §6).

``compose(GarmentRequest)`` is a **pure function** of its inputs: the same recipe, colours, fabric tile, fold set, prints
and kit pieces always give byte-identical PNGs and the same ``layer_stack_hash``. It starts from a fully transparent
canvas (CLO-18), authors at 4x (2340x2236) and runs a fixed stage order (CLO-15)::

    blocks  -> fabric -> folds -> details -> prints -> kit -> finish
    colour blocks,  tile gradient-    multiply/screen   seams, stitches,   placed by code     shoes, legwear,    box downscale (premultiplied),
    cuffs, collar   mapped to colours overlay           trims              (slots, no mirror)  bracelets, gloves  alpha binarise, palette snap,
                                                                                                                  gap fill + open-side bleed

``compose_template(kind, char, palette, prints, fabric, folds, kits)`` is the spec's entry point: it reads a
``Character``-like object (attribute or dict access; the pydantic spec model is not imported here) and calls ``compose``.

Label map classes (uint8 585x559): 0 transparent/skin, 1 fabric, 2 secondary block, 3 print, 4 trim, 5 bracelet/glove,
6 shoes, 7 legwear.
"""
from __future__ import annotations

import hashlib
import io
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import numpy as np

from duoskin.checks.model import CheckResult
from duoskin.imaging import fabric as FAB
from duoskin.imaging import folds as FOLD
from duoskin.imaging import print_place as PP
from duoskin.imaging import recipes as RCP
from duoskin.imaging import shoes_painted as KIT
from duoskin.imaging.palette import deltaE2000, hex_to_rgb, lab_to_srgb, srgb_to_lab
from duoskin.imaging.print_place import Placement, PrintSpec
from duoskin.models.common import canonical_json, sha256_of
from duoskin.roblox import template as T

Kind = Literal["shirt", "pants"]
SCALE = 4
STAGE_ORDER = ("blocks", "fabric", "folds", "details", "prints", "kit", "finish")
LABEL_FABRIC, LABEL_SECOND, LABEL_PRINT, LABEL_TRIM, LABEL_BRACELET, LABEL_SHOES, LABEL_LEGWEAR = 1, 2, 3, 4, 5, 6, 7
DEFAULT_LABEL = {"base": LABEL_FABRIC, "second": LABEL_SECOND, "inner": LABEL_SECOND, "trim": LABEL_TRIM}
RGB = tuple[int, int, int]
H4, W4 = T.HEIGHT * SCALE, T.WIDTH * SCALE


class ComposeError(ValueError):
    """The request cannot be composed (unknown colour reference, recipe/template mismatch, kit rule violated)."""


# --------------------------------------------------------------------------------------------------------------------
# request / result
# --------------------------------------------------------------------------------------------------------------------
@dataclass
class GarmentRequest:
    """Everything ``compose`` needs, as plain data.

    ``colours`` maps roles to RGB: ``base`` (required), ``second``, ``trim``, ``inner``, and for kit pieces ``shoe_base``,
    ``shoe_sole``, ``shoe_accent``, ``legwear``, ``bracelet``, ``glove``. Missing roles fall back (second/trim from base,
    shoe_base from base, shoe_accent and bracelet and glove from trim, legwear from second)."""

    kind: Kind
    recipe: RCP.Recipe
    colours: dict[str, RGB]
    attrs: dict[str, str] = field(default_factory=dict)         # cut attributes: sleeve, hem, neckline, front, block_layout, leg, waist
    params: dict[str, float] = field(default_factory=dict)      # numeric recipe overrides, e.g. crop_hem
    fabric: FAB.FabricTile | None = None
    folds: FOLD.FoldSet | None = None
    prints: list[PrintSpec] = field(default_factory=list)
    shoe_style: str | None = None                               # pants only
    legwear: str = "bare"                                       # pants only
    arm_extras: list[str] = field(default_factory=list)         # shirt only
    palette: list[RGB] = field(default_factory=list)            # snap interiors to these (empty = no snap)
    snap_de: float = 12.0
    skin: RGB = (214, 170, 140)                                 # preview only; never painted into the file
    kit_dirs: list[Path] = field(default_factory=list)


@dataclass
class ComposeResult:
    png: bytes                          # 585x559 RGBA 8-bit PNG, colour chunks stripped
    label_map: np.ndarray               # uint8 585x559: 0 transparent/skin, 1 fabric, 2 secondary, 3 print, 4 trim, 5 bracelet/glove, 6 shoes, 7 legwear
    layers: dict[str, bytes]            # per-stage PNGs at 4x (debug; only with ``with_layers=True``)
    placements: list[Placement]
    flat_front: bytes
    flat_back: bytes
    preview_boxes: bytes
    # ---- additive fields (not in the APP_SPEC sketch; all defaulted) ----
    stack: list[dict[str, Any]] = field(default_factory=list)   # the executed layer stack (what the hash covers)
    layer_stack_hash: str = ""                                  # CHK-B07 golden hash (descriptors + input identities)
    output_sha: str = ""                                        # sha256 of the PNG bytes
    base_layer_png: bytes = b""                                 # 585x559 base fabric layer (fabric on the base colour) for CLO-04
    meta: dict[str, Any] = field(default_factory=dict)          # recipe id, attrs, resolved vars, hem rows, fold label ...
    warnings: list[str] = field(default_factory=list)
    checks: list[CheckResult] = field(default_factory=list)     # validate_template results on this output


# --------------------------------------------------------------------------------------------------------------------
# colours
# --------------------------------------------------------------------------------------------------------------------
def _shift_lightness(rgb: RGB, dL: float) -> RGB:
    lab = srgb_to_lab(np.array(rgb, dtype=np.float64))
    lab = np.array([min(100.0, max(0.0, lab[0] + dL)), lab[1], lab[2]])
    out = lab_to_srgb(lab)
    return tuple(int(round(v)) for v in out)  # type: ignore[return-value]


class ColourBook:
    """Role -> RGB with the documented fallbacks; relative colours (``{"role": .., "dL": ..}``) shift CIELAB lightness."""

    def __init__(self, colours: Mapping[str, RGB]):
        if "base" not in colours:
            raise ComposeError("a garment needs a 'base' colour")
        self._c: dict[str, RGB] = {k: tuple(int(x) for x in v) for k, v in colours.items()}  # type: ignore[misc]
        base = self._c["base"]
        lum = float(srgb_to_lab(np.array(base, dtype=np.float64))[0])
        flip = 14.0 if lum < 62 else -14.0
        self._fallback: dict[str, Any] = {
            "second": lambda: _shift_lightness(base, flip),
            "trim": lambda: _shift_lightness(base, -18.0 if lum > 40 else 18.0),
            "inner": lambda: self.get("second"),
            "shoe_base": lambda: base,
            "shoe_sole": lambda: (232, 232, 232),
            "shoe_accent": lambda: self.get("trim"),
            "legwear": lambda: self.get("second"),
            "bracelet": lambda: self.get("trim"),
            "glove": lambda: self.get("trim"),
        }

    def get(self, role: str) -> RGB:
        if role in self._c:
            return self._c[role]
        if role in self._fallback:
            self._c[role] = self._fallback[role]()
            return self._c[role]
        raise ComposeError(f"unknown colour role {role!r}")

    def resolve(self, spec: Any) -> RGB:
        if isinstance(spec, str):
            return hex_to_rgb(spec) if spec.startswith("#") else self.get(spec)
        if isinstance(spec, dict):
            if "hex" in spec:
                base = hex_to_rgb(spec["hex"])
            elif "role" in spec:
                base = self.get(spec["role"])
            else:
                raise ComposeError(f"colour spec needs a role or hex: {spec}")
            d = float(spec.get("dL", 0.0))
            return _shift_lightness(base, d) if d else base
        raise ComposeError(f"bad colour spec {spec!r}")

    def default_label(self, spec: Any) -> int:
        if isinstance(spec, dict):
            spec = spec.get("role", "")
        return DEFAULT_LABEL.get(spec, LABEL_FABRIC) if isinstance(spec, str) else LABEL_FABRIC


# --------------------------------------------------------------------------------------------------------------------
# the 4x canvas and layer operations
# --------------------------------------------------------------------------------------------------------------------
class Canvas:
    """Straight-colour 4x canvas: ``alpha`` is 0/255 coverage (anti-aliasing comes from the final box downscale)."""

    def __init__(self) -> None:
        self.rgb = np.zeros((H4, W4, 3), dtype=np.uint8)
        self.alpha = np.zeros((H4, W4), dtype=np.uint8)
        self.label = np.zeros((H4, W4), dtype=np.uint8)
        self.cid = np.zeros((H4, W4), dtype=np.uint8)           # colour-table index used by the fabric stage (0 = no fabric)
        self.cols: list[RGB] = [(0, 0, 0)]

    def register(self, rgb: RGB) -> int:
        try:
            return self.cols.index(rgb, 1)
        except ValueError:
            if len(self.cols) >= 255:
                raise ComposeError("more than 254 distinct block colours") from None
            self.cols.append(rgb)
            return len(self.cols) - 1

    def snapshot_png(self) -> bytes:
        arr = np.dstack([self.rgb, self.alpha])
        return T.encode_png_rgba8(arr)


def _union(masks: list[RCP.Mask]) -> RCP.Mask | None:
    if not masks:
        return None
    if len(masks) == 1:
        return masks[0]
    x0, y0 = min(m.x0 for m in masks), min(m.y0 for m in masks)
    x1, y1 = max(m.x1 for m in masks), max(m.y1 for m in masks)
    arr = np.zeros((y1 - y0, x1 - x0), dtype=bool)
    for m in masks:
        arr[m.y0 - y0:m.y1 - y0, m.x0 - x0:m.x1 - x0] |= m.arr
    return RCP.Mask(arr, x0, y0)


def _layer_masks(rl: RCP.ResolvedLayer, named: dict[str, RCP.Mask]) -> list[RCP.Mask]:
    """The layer's mask(s) on the 4x canvas, clipped to the layer's regions (one per region for local frames)."""
    regions = RCP.select_regions(rl.regions)
    out: list[RCP.Mask] = []
    if rl.frame == "abs":
        base = RCP.rasterize(rl.shapes, named=named)
        if base is None:
            return []
        if rl.minus:
            base = RCP.subtract(base, RCP.rasterize(rl.minus, named=named))
        m = RCP.mask_and_regions(base, regions)
        return [m] if m is not None else []
    for region in regions:
        x0, y0 = T.REGIONS[region][:2]
        if rl.frame == "strip":
            if T.FACE_OF[region] not in ("f", "b", "l", "r"):
                continue
            dx, dy = float(x0) - T.strip_layout(T.PART_OF[region])[0][region], 0.0
        else:
            dx, dy = float(x0), (float(y0) if rl.frame == "local" else 0.0)
        base = RCP.rasterize(rl.shapes, dx=dx, dy=dy, named=named)
        if base is None:
            continue
        if rl.minus:
            base = RCP.subtract(base, RCP.rasterize(rl.minus, dx=dx, dy=dy, named=named))
        m = RCP.mask_and_regions(base, [region])
        if m is not None:
            out.append(m)
    return out


def _rows_of(mask: RCP.Mask) -> tuple[int, int]:
    rows = np.nonzero(mask.arr.any(axis=1))[0]
    return (mask.y0 + int(rows.min())) // SCALE, (mask.y0 + int(rows.max())) // SCALE


def _apply_paint(cv: Canvas, m: RCP.Mask, rl: RCP.ResolvedLayer, rgb: RGB | None, label: int | None, cid: int) -> None:
    sl = (slice(m.y0, m.y1), slice(m.x0, m.x1))
    mk = m.arr
    if rl.coverage == "erase":
        cv.alpha[sl][mk] = 0
        cv.label[sl][mk] = 0
        cv.cid[sl][mk] = 0
        return
    if rl.coverage == "within":
        mk = mk & (cv.alpha[sl] > 0)
    elif rl.coverage == "under":
        mk = mk & (cv.alpha[sl] == 0)
    if not mk.any():
        return
    assert rgb is not None
    cv.rgb[sl][mk] = rgb
    cv.alpha[sl][mk] = 255
    if label is not None:
        cv.label[sl][mk] = label
    cv.cid[sl][mk] = cid


def _bell(t: np.ndarray) -> np.ndarray:
    return np.sin(np.pi * np.clip(t, 0.0, 1.0)) ** 2


def _apply_shade(cv: Canvas, m: RCP.Mask, rl: RCP.ResolvedLayer) -> None:
    sl = (slice(m.y0, m.y1), slice(m.x0, m.x1))
    mk = m.arr & (cv.alpha[sl] > 0)
    if not mk.any() or rl.dL == 0:
        return
    if rl.stage == "blocks":                         # shade a colour block: new table colours so the fabric stage keeps it
        if rl.ramp:
            raise ComposeError(f"{rl.id}: ramp shading is only allowed after the fabric stage")
        cid_sl = cv.cid[sl]
        for old in np.unique(cid_sl[mk]):
            sel = mk & (cid_sl == old)
            if old == 0:
                rgb = cv.rgb[sl][sel]
                packed = np.unique(rgb, axis=0)
                for p in packed:
                    s2 = sel & np.all(cv.rgb[sl] == p, axis=2)
                    cv.rgb[sl][s2] = _shift_lightness(tuple(int(x) for x in p), rl.dL)  # type: ignore[arg-type]
                continue
            new = _shift_lightness(cv.cols[int(old)], rl.dL)
            cv.rgb[sl][sel] = new
            cv.cid[sl][sel] = cv.register(new)
        return
    yy, xx = np.nonzero(mk)
    px = cv.rgb[sl][yy, xx]
    packed = (px[:, 0].astype(np.uint32) << 16) | (px[:, 1].astype(np.uint32) << 8) | px[:, 2].astype(np.uint32)
    uniq, inv = np.unique(packed, return_inverse=True)
    ur = np.stack([(uniq >> 16) & 255, (uniq >> 8) & 255, uniq & 255], axis=1).astype(np.float64)
    lab = srgb_to_lab(ur)
    if rl.ramp:
        r = rl.ramp
        row = (m.y0 + yy + 0.5) / SCALE
        t = (row - r["from"]) / max(r["to"] - r["from"], 1e-9)
        f = _bell(t) if r.get("profile", "bell") == "bell" else np.clip(t, 0, 1)
        dL = rl.dL * f
        lab_px = lab[inv.reshape(-1)].copy()
        lab_px[:, 0] = np.clip(lab_px[:, 0] + dL, 0.0, 100.0)
        out = np.rint(lab_to_srgb(lab_px)).astype(np.uint8)
    else:
        lab2 = lab.copy()
        lab2[:, 0] = np.clip(lab2[:, 0] + rl.dL, 0.0, 100.0)
        out_u = np.rint(lab_to_srgb(lab2)).astype(np.uint8)
        out = out_u[inv.reshape(-1)]
    cv.rgb[sl][yy, xx] = out


def run_layer(cv: Canvas, rl: RCP.ResolvedLayer, book: ColourBook, named: dict[str, RCP.Mask]) -> dict[str, Any]:
    """Execute one resolved layer on the canvas and return its stack entry."""
    masks = _layer_masks(rl, named)
    u = _union(masks)
    entry = rl.descriptor()
    entry["mask_px"] = int(sum(int(m.arr.sum()) for m in masks))
    if u is not None:
        named[rl.id] = u
        if rl.assert_rule:
            lo, hi = _rows_of(u)
            msg = KIT.check_rule(rl.assert_rule, lo, hi)
            if msg:
                raise ComposeError(f"CLO-07 {rl.id}: {msg}")
    colour = book.resolve(rl.colour) if rl.op == "paint" and rl.coverage != "erase" else None
    if colour is not None:
        entry["rgb"] = list(colour)
    label = rl.label if rl.label is not None else (book.default_label(rl.colour) if rl.coverage in ("add", "under") else None)
    cid = cv.register(colour) if (colour is not None and rl.fabric and rl.stage == "blocks") else 0
    for m in masks:
        if rl.op == "shade":
            _apply_shade(cv, m, rl)
        else:
            _apply_paint(cv, m, rl, colour, label, cid)
    return entry


# --------------------------------------------------------------------------------------------------------------------
# fabric and folds stages
# --------------------------------------------------------------------------------------------------------------------
_FAB_CACHE: dict[tuple[str, str], np.ndarray] = {}


def fabric_index(ft: FAB.FabricTile, region: str) -> np.ndarray:
    """uint8 (h4, w4) fabric signal of ``region`` quantised to 0..255 (128 = mean), cached and read-only."""
    key = (ft.sha256, region)
    arr = _FAB_CACHE.get(key)
    if arr is None:
        v = FAB.fabric_values(ft, region, SCALE)
        arr = np.rint((v + 1.0) * 127.5).clip(0, 255).astype(np.uint8)
        arr.setflags(write=False)
        if len(_FAB_CACHE) > 80:
            _FAB_CACHE.clear()
        _FAB_CACHE[key] = arr
    return arr


def _fabric_lut(colour: RGB, amplitude: float) -> np.ndarray:
    """(256, 3) uint8: the block colour with its lightness swung by ``amplitude`` L* peak to peak across the tile range."""
    lab = srgb_to_lab(np.array(colour, dtype=np.float64))
    s = (np.arange(256) / 127.5) - 1.0
    labs = np.tile(lab, (256, 1))
    labs[:, 0] = np.clip(lab[0] + s * amplitude / 2.0, 0.0, 100.0)
    return np.rint(lab_to_srgb(labs)).astype(np.uint8)


def apply_fabric(cv: Canvas, ft: FAB.FabricTile) -> dict[str, np.ndarray]:
    """Gradient-map the tile onto every fabric block pixel (cid > 0). Returns the per-region fabric index arrays."""
    luts = {c: _fabric_lut(col, ft.amplitude_dL) for c, col in enumerate(cv.cols) if c > 0}
    out: dict[str, np.ndarray] = {}
    for region in T.REGION_ORDER:
        rs, cs = T.region_slices_scaled(region, SCALE)
        cid = cv.cid[rs, cs]
        if not cid.any():
            continue
        idx = fabric_index(ft, region)
        out[region] = idx
        rgb = cv.rgb[rs, cs]
        for c in np.unique(cid):
            if c == 0:
                continue
            sel = cid == c
            rgb[sel] = luts[int(c)][idx[sel]]
    return out


def apply_folds(cv: Canvas, folds: FOLD.FoldSet) -> int:
    """Multiply/screen the fold overlay, clamped to the garment (covered pixels only). Returns the regions touched."""
    touched = 0
    for region in T.REGION_ORDER:
        rs, cs = T.region_slices_scaled(region, SCALE)
        cov = cv.alpha[rs, cs] > 0
        if not cov.any():
            continue
        d = folds.delta(region, cov.shape)
        if not d.any():
            continue
        touched += 1
        rgb = cv.rgb[rs, cs].astype(np.float32) / 255.0
        mul = np.where(d < 0, 1.0 + 2.0 * d, 1.0)[..., None]
        scr = np.where(d > 0, 2.0 * d, 0.0)[..., None]
        out = 1.0 - (1.0 - rgb * mul) * (1.0 - scr)
        res = np.rint(np.clip(out, 0.0, 1.0) * 255.0).astype(np.uint8)
        view = cv.rgb[rs, cs]
        view[cov] = res[cov]
    return touched


def base_fabric_layer(ft: FAB.FabricTile, colour: RGB) -> np.ndarray:
    """(559, 585, 4) uint8: the fabric tile gradient-mapped to ``colour`` on EVERY region (the CLO-04 base fabric layer)."""
    lut = _fabric_lut(colour, ft.amplitude_dL)
    img = T.blank_template()
    for region in T.REGION_ORDER:
        idx = fabric_index(ft, region).astype(np.float32)
        h4, w4 = idx.shape
        mean = idx.reshape(h4 // SCALE, SCALE, w4 // SCALE, SCALE).mean(axis=(1, 3))
        rgb = lut[np.rint(mean).astype(np.uint8)]
        c = T.crop(img, region)
        c[..., :3] = rgb
        c[..., 3] = 255
    return img


# --------------------------------------------------------------------------------------------------------------------
# finishing: downscale, alpha policy, palette snap, edges
# --------------------------------------------------------------------------------------------------------------------
def downscale(cv: Canvas) -> tuple[np.ndarray, np.ndarray]:
    """Box-filter the 4x canvas to 585x559 in PREMULTIPLIED alpha, region by region (every region box is a multiple of 4 px at
    4x, so this equals a whole-canvas box filter). Garment alpha is then binarised at 128 (CLO-10 rung-1 auto-fix): a pixel is
    garment when at least half of its 16 sub-pixels are. Returns (RGBA uint8, label map uint8)."""
    img = T.blank_template()
    labels = np.zeros((T.HEIGHT, T.WIDTH), dtype=np.uint8)
    for region in T.REGION_ORDER:
        rs, cs = T.region_slices_scaled(region, SCALE)
        a4 = cv.alpha[rs, cs] > 0
        if not a4.any():
            continue
        h, w = a4.shape[0] // SCALE, a4.shape[1] // SCALE
        af = a4.astype(np.float32)
        prem = cv.rgb[rs, cs].astype(np.float32) * af[..., None]
        sum_p = prem.reshape(h, SCALE, w, SCALE, 3).sum(axis=(1, 3))
        sum_a = af.reshape(h, SCALE, w, SCALE).sum(axis=(1, 3))
        keep = sum_a >= (SCALE * SCALE) / 2.0
        rgb = np.where(keep[..., None], sum_p / np.maximum(sum_a, 1.0)[..., None], 0.0)
        c = T.crop(img, region)
        c[..., :3] = np.rint(rgb).astype(np.uint8)
        c[..., 3] = np.where(keep, 255, 0)
        lab4 = cv.label[rs, cs]
        counts = np.stack([((lab4 == k) & a4).reshape(h, SCALE, w, SCALE).sum(axis=(1, 3)) for k in range(8)], axis=0)
        counts[0] = 0
        lab1 = np.where(keep, np.argmax(counts, axis=0) + 0, 0)
        lab1 = np.where(keep & (counts.max(axis=0) == 0), LABEL_FABRIC, lab1)
        T.crop(labels, region)[...] = lab1.astype(np.uint8)
    return img, labels


def alpha_bleed_colour(img: np.ndarray) -> np.ndarray:
    """Give every fully transparent pixel INSIDE a region the colour of its nearest garment pixel in that region (alpha stays
    0), so bilinear filtering in 3D never pulls black into garment edges. Regions with no garment stay (0, 0, 0)."""
    from scipy import ndimage as ndi

    out = img.copy()
    for region in T.REGION_ORDER:
        c = T.crop(out, region)
        opaque = c[..., 3] > 0
        if not opaque.any() or opaque.all():
            continue
        _, (iy, ix) = ndi.distance_transform_edt(~opaque, return_indices=True)
        trans = ~opaque
        c[..., :3][trans] = c[..., :3][iy[trans], ix[trans]]
    return out


def snap_palette_interior(img: np.ndarray, palette: list[RGB], max_de: float = 12.0, keep_lightness: bool = True) -> tuple[np.ndarray, dict[str, Any]]:
    """Snap interior garment pixels (opaque, all 8 neighbours opaque, so anti-aliased and cut edges stay untouched) whose colour is
    within ``max_de`` (CIEDE2000) of a palette colour to that colour's hue and chroma. With ``keep_lightness`` the pixel's own
    L* is kept, so the weave and the folds survive; without it the pixel becomes the palette colour exactly."""
    from scipy import ndimage as ndi

    out = img.copy()
    stats = {"snapped_px": 0, "interior_px": 0, "max_de": 0.0}
    if not palette:
        return out, stats
    opaque = out[..., 3] == 255
    interior = ndi.binary_erosion(opaque, structure=np.ones((3, 3), bool), border_value=0) & (T.region_label_map() > 0)
    if not interior.any():
        return out, stats
    rgb = out[..., :3][interior]
    packed = (rgb[:, 0].astype(np.uint32) << 16) | (rgb[:, 1].astype(np.uint32) << 8) | rgb[:, 2].astype(np.uint32)
    uniq, inv = np.unique(packed, return_inverse=True)
    ur = np.stack([(uniq >> 16) & 255, (uniq >> 8) & 255, uniq & 255], axis=1).astype(np.float64)
    lab_u = srgb_to_lab(ur)
    pal = np.array(palette, dtype=np.float64)
    lab_p = srgb_to_lab(pal)
    d = deltaE2000(lab_u[:, None, :], lab_p[None, :, :])
    j = np.argmin(d, axis=1)
    dmin = d[np.arange(len(j)), j]
    take = dmin <= max_de
    new_lab = lab_u.copy()
    new_lab[take, 1:] = lab_p[j[take], 1:]
    if not keep_lightness:
        new_lab[take, 0] = lab_p[j[take], 0]
    new_rgb = np.rint(lab_to_srgb(new_lab)).astype(np.uint8)
    exact = take & (dmin < 0.5)
    new_rgb[exact] = np.array(palette, dtype=np.uint8)[j[exact]]          # an (almost) palette colour becomes exactly it
    out_rgb = new_rgb[inv.reshape(-1)]
    out[..., :3][interior] = out_rgb
    stats.update(snapped_px=int((take[inv.reshape(-1)]).sum()), interior_px=int(interior.sum()),
                 max_de=float(dmin[take].max()) if take.any() else 0.0)
    return out, stats


# --------------------------------------------------------------------------------------------------------------------
# the stack hash (CLO-15 / CHK-B07)
# --------------------------------------------------------------------------------------------------------------------
def layer_stack_hash(stack: list[dict[str, Any]]) -> str:
    """sha256 of the ordered layer stack: every executed layer's resolved descriptor plus the identity (sha) of each input
    (fabric tile, fold set, prints, palette). Deterministic across platforms because masks are described, not hashed."""
    return sha256_of(stack)


def assert_stage_order(stack: list[dict[str, Any]]) -> None:
    """CLO-15 ASSERT: stages never run out of order (folds over prints, blocks over prints, shoes under the fabric)."""
    last = -1
    for e in stack:
        i = STAGE_ORDER.index(e["stage"])
        if i < last:
            raise ComposeError(f"CLO-15: stage {e['stage']!r} ran after {STAGE_ORDER[last]!r}")
        last = i


# --------------------------------------------------------------------------------------------------------------------
# compose
# --------------------------------------------------------------------------------------------------------------------
def compose(req: GarmentRequest, *, with_layers: bool = False, run_checks: bool = True, previews: bool = True) -> ComposeResult:
    """Compose one classic template (Shirt or Pants). Pure: no clock, no randomness, no I/O besides reading the kit JSON.

    ``with_layers``: also return the per-stage 4x PNGs (debug). ``run_checks``: run ``validate_template`` on the output. ``previews``:
    render the flat front/back tiles and the 3D box preview (``b""`` when False; they never influence the file)."""
    recipe = req.recipe
    if recipe.template != req.kind:
        raise ComposeError(f"recipe {recipe.recipe_id} is a {recipe.template} recipe, not {req.kind}")
    attrs = recipe.attrs_with_defaults(req.attrs)
    problems = recipe.validate_attrs(attrs)
    if problems:
        raise ComposeError("; ".join(problems))
    env = RCP.resolve_vars(recipe, attrs, req.params)
    book = ColourBook(req.colours)
    ft = req.fabric or FAB.resolve_fabric("jersey_plain")
    folds = req.folds or FOLD.resolve_folds(recipe.fold_set)
    rlayers = RCP.resolve_layers(recipe, attrs, env)
    kit_resolved = RCP.resolve_layer_list(
        KIT.kit_layers(req.kind, shoe_style=req.shoe_style, legwear=req.legwear, arm_extras=req.arm_extras,
                       extra_dirs=req.kit_dirs or None), attrs, env)
    cv = Canvas()
    named: dict[str, RCP.Mask] = {}
    stack: list[dict[str, Any]] = []
    layers_png: dict[str, bytes] = {}
    warnings: list[str] = []

    def snap(name: str) -> None:
        if with_layers:
            layers_png[name] = cv.snapshot_png()

    # 1. colour blocks
    for rl in (x for x in rlayers if x.stage == "blocks"):
        stack.append({"stage": "blocks", **run_layer(cv, rl, book, named)})
    snap("blocks")
    # 2. fabric
    base_rgb = book.get("base")
    apply_fabric(cv, ft)
    stack.append({"stage": "fabric", "tile": ft.sha256, "fabric_id": ft.fabric_id, "amplitude_dL": ft.amplitude_dL,
                  "px_per_repeat": ft.px_per_repeat})
    snap("fabric")
    # 3. folds
    apply_folds(cv, folds)
    stack.append({"stage": "folds", "set": folds.set_id, "origin": folds.origin, "sha": folds.sha256})
    snap("folds")
    # 4. seams, stitches, trims
    for rl in (x for x in rlayers if x.stage == "details"):
        stack.append({"stage": "details", **run_layer(cv, rl, book, named)})
    snap("details")
    # 5. prints
    plan = PP.plan_prints(list(req.prints), list(recipe.print_slots), pants=req.kind == "pants")
    warnings.extend(plan.warnings)
    bad = PP.check_placements(plan.placements)
    warnings.extend(f"soft: {b}" for b in bad if "within" in b or "cross" in b)
    PP.composite_pieces(cv.rgb, cv.alpha, cv.label, plan.pieces, LABEL_PRINT)
    stack.append({"stage": "prints", "pieces": [{"part": p.part_id, "region": p.region, "x4": p.x4, "y4": p.y4,
                                                  "sha": p.sha256, "wrap": p.wrap} for p in plan.pieces]})
    snap("prints")
    # 6. painted kit pieces
    for rl in kit_resolved:
        stack.append({"stage": "kit", **run_layer(cv, rl, book, named)})
    snap("kit")
    assert_stage_order(stack)
    # 7. finish
    img, labels = downscale(cv)
    img = alpha_bleed_colour(img)
    if req.palette:
        img, snap_stats = snap_palette_interior(img, [tuple(c) for c in req.palette], req.snap_de)
    else:
        snap_stats = {"snapped_px": 0, "interior_px": 0, "max_de": 0.0}
    img = T.finish_edges(img, T.DEFAULT_BLEED_PX)
    labels[img[..., 3] == 0] = 0
    stack.append({"stage": "finish", "alpha": "binary_128", "palette": sorted(map(list, req.palette)), "snap_de": req.snap_de,
                  "keep_lightness": True, "bleed_px": T.DEFAULT_BLEED_PX})
    assert_stage_order(stack)
    png = _write_png(img)
    base_layer = T.encode_png_rgba8(base_fabric_layer(ft, base_rgb))
    stack_hash = layer_stack_hash(stack)
    meta = {"recipe_id": recipe.recipe_id, "recipe_sha": recipe.sha256, "kind": req.kind, "attrs": attrs,
            "vars": {k: float(v) for k, v in env.items()}, "fold_label": folds.label, "fabric_id": ft.fabric_id,
            "snap": snap_stats, "print_slots": [s.model_dump() for s in recipe.print_slots],
            "hem_rows": hem_rows(env), "colours": {k: list(v) for k, v in sorted(book._c.items())}}
    if folds.origin == "procedural":
        warnings.append("procedural folds")
    front = back = box_png = b""
    if previews:
        front, back = T.render_flat_preview(png if req.kind == "shirt" else None, png if req.kind == "pants" else None, skin=req.skin)
        box = T.render_box_preview(img if req.kind == "shirt" else None, img if req.kind == "pants" else None, skin=req.skin)
        box_png = T.encode_png_rgba8(box)
    res = ComposeResult(png=png, label_map=labels, layers=layers_png, placements=plan.placements, flat_front=front,
                        flat_back=back, preview_boxes=box_png, stack=stack, layer_stack_hash=stack_hash,
                        output_sha=hashlib.sha256(png).hexdigest(), base_layer_png=base_layer, meta=meta, warnings=warnings)
    if run_checks:
        from duoskin.roblox import validators as V

        res.checks = V.validate_template(png, req.kind, labels, meta, base_layer=base_layer, placements=plan.placements,
                                         stack=stack, plan=None)
    return res


def hem_rows(env: Mapping[str, float]) -> list[int]:
    """Template rows where the recipe cuts the garment on purpose (torso hem, sleeve end, leg hem): alpha edges there are by
    design and exempt from the split-row warning."""
    rows = []
    for k in ("torso_end", "sleeve_end", "leg_hem"):
        if k in env:
            rows.append(int(round(env[k])))
    if "waist_top" in env:                                   # the pants waist edge is the boundary waist_top-1 | waist_top
        rows.append(int(round(env["waist_top"])) - 1)
    return sorted(set(rows))


def _write_png(img: np.ndarray) -> bytes:
    try:
        from duoskin.imaging import files

        data = files.save_png_rgba8(img)
    except ImportError:                                   # pragma: no cover - imaging.files is another track's module
        data = T.write_template(img)
    facts = T.inspect_png(data)
    if not (facts.is_rgba8 and (facts.width, facts.height) == T.SIZE_WH and not facts.colour_chunks):
        raise AssertionError(f"CLO-02: composed PNG is not a clean 585x559 RGBA8 file: {facts}")
    return data


# --------------------------------------------------------------------------------------------------------------------
# the spec's entry point
# --------------------------------------------------------------------------------------------------------------------
def _get(obj: Any, name: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _hex_lookup(palette: Any) -> dict[str, str]:
    out: dict[str, str] = {}
    items = palette.values() if isinstance(palette, Mapping) else (palette or [])
    for c in items:
        cid, hx = _get(c, "id"), _get(c, "hex")
        if cid and hx:
            out[str(cid)] = str(hx)
    return out


def _image_of(asset: Any) -> np.ndarray:
    """RGBA uint8 from an asset-like: ndarray, PIL image, PNG bytes, a path, or an object with ``png``/``data``/``bytes``/``path``."""
    from PIL import Image

    if isinstance(asset, np.ndarray):
        arr = asset
        if arr.ndim == 3 and arr.shape[2] == 4 and arr.dtype == np.uint8:
            return arr
        return np.array(Image.fromarray(arr).convert("RGBA"), dtype=np.uint8)
    if isinstance(asset, Image.Image):
        return np.array(asset.convert("RGBA"), dtype=np.uint8)
    if isinstance(asset, (bytes, bytearray)):
        with Image.open(io.BytesIO(bytes(asset))) as im:
            return np.array(im.convert("RGBA"), dtype=np.uint8)
    if isinstance(asset, (str, Path)):
        with Image.open(asset) as im:
            return np.array(im.convert("RGBA"), dtype=np.uint8)
    for attr in ("png", "data", "bytes", "content"):
        v = _get(asset, attr)
        if isinstance(v, (bytes, bytearray)):
            return _image_of(bytes(v))
    p = _get(asset, "path")
    if p:
        return _image_of(Path(p))
    raise ComposeError(f"cannot read an image from {type(asset).__name__}")


def _find_asset(prints: Mapping[str, Any] | None, suffix: str) -> Any:
    for k, v in (prints or {}).items():
        if str(k).endswith(suffix):
            return v
    return None


def request_from_character(kind: Kind, char: Any, palette: Any, prints: Mapping[str, Any] | None = None,
                           fabric: FAB.FabricTile | str | None = None, folds: FOLD.FoldSet | None = None,
                           kits: Any = None, *, skin: RGB | None = None) -> GarmentRequest:
    """Build a ``GarmentRequest`` from a ``Character``-like object (``char.top`` for a shirt, ``char.bottom`` for pants)."""
    part = _get(char, "top" if kind == "shirt" else "bottom")
    if part is None:
        raise ComposeError(f"character has no {'top' if kind == 'shirt' else 'bottom'}")
    hexes = _hex_lookup(palette)

    def colour(ref: Any, role: str, required: bool = False) -> RGB | None:
        if ref in (None, "", "none"):
            if required:
                raise ComposeError(f"{role} colour reference is missing")
            return None
        if str(ref).startswith("#"):
            return hex_to_rgb(str(ref))
        if str(ref) not in hexes:
            raise ComposeError(f"{role}: colour reference {ref!r} is not in the palette")
        return hex_to_rgb(hexes[str(ref)])

    colours: dict[str, RGB] = {"base": colour(_get(part, "base_ref"), "base", True)}  # type: ignore[dict-item]
    for role, attr in (("second", "second_ref"), ("trim", "trim_ref")):
        c = colour(_get(part, attr), role)
        if c is not None:
            colours[role] = c
    recipe_dirs = list(_get(kits, "recipe_dirs", []) or [])
    recipe = RCP.load_recipe(str(_get(part, "recipe_id")), [Path(d) for d in recipe_dirs] or None)
    attr_names = ("sleeve", "hem", "neckline", "front", "block_layout", "leg", "waist")
    attrs = {a: str(_get(part, a)) for a in attr_names if _get(part, a) not in (None, "")}
    inner = _get(part, "inner_recipe_id")
    if inner not in (None, "", "none"):
        attrs["inner"] = str(inner)
    fabric_id = _get(part, "fabric_id")
    fabric_dirs = [Path(d) for d in (_get(kits, "fabric_dirs", []) or [])]
    ft = fabric if isinstance(fabric, FAB.FabricTile) else FAB.resolve_fabric(fabric if isinstance(fabric, str) else fabric_id, fabric_dirs)
    fold_dirs = [Path(d) for d in (_get(kits, "fold_dirs", []) or [])]
    fs = folds if folds is not None else FOLD.resolve_folds(recipe.fold_set, fold_dirs)
    specs: list[PrintSpec] = []
    tag = "top" if kind == "shirt" else "bottom"
    for i, p in enumerate(_get(part, "prints", []) or []):
        asset = _find_asset(prints, f"print.{tag}.{i}")
        if asset is None:
            continue
        specs.append(PrintSpec(f"print.{tag}.{i}", str(_get(p, "region")), str(_get(p, "scale", "medium")), _image_of(asset),
                               bool(_get(p, "wrap", False))))
    req = GarmentRequest(kind=kind, recipe=recipe, colours=colours, attrs=attrs, fabric=ft, folds=fs, prints=specs,
                         palette=[hex_to_rgb(h) for h in hexes.values()], kit_dirs=[Path(d) for d in (_get(kits, "kit_dirs", []) or [])])
    if skin is not None:
        req.skin = skin
    if kind == "shirt":
        req.arm_extras = [str(e) for e in (_get(part, "arm_extras", []) or [])]
        for role in ("bracelet", "glove"):
            colours.setdefault(role, colours.get("trim", colours["base"]))
    else:
        shoes = _get(part, "shoes")
        sid = _get(shoes, "style_id")
        req.shoe_style = None if sid in (None, "", "none") else str(sid)
        for role, attr in (("shoe_base", "base_ref"), ("shoe_sole", "sole_ref"), ("shoe_accent", "accent_ref")):
            c = colour(_get(shoes, attr), role)
            if c is not None:
                colours[role] = c
        req.legwear = str(_get(part, "legwear", "bare") or "bare")
        c = colour(_get(part, "legwear_ref"), "legwear")
        if c is not None:
            colours["legwear"] = c
        motif = _get(shoes, "motif")
        art = _find_asset(prints, "print.shoes.0")
        if motif not in (None, "", "none") and art is not None:
            img = _image_of(art)
            for region in ("rlimb_r", "llimb_l"):                      # the OUTER face of each shoe; the art is never mirrored
                req.prints.append(PrintSpec(f"print.shoes.0.{region}", region, "small", img, slot="shoe"))
    return req


def compose_template(kind: Kind, char: Any, palette: Any, prints: Mapping[str, Any] | None = None,
                     fabric: FAB.FabricTile | str | None = None, folds: FOLD.FoldSet | None = None, kits: Any = None,
                     *, with_layers: bool = False) -> ComposeResult:
    """APP_SPEC §10.5 entry point: compose the ``kind`` template of ``char`` (a ``Character``-like object).

    ``palette``: list of ``Colour``-likes (``id``, ``hex``); ``prints``: ``{part_id: asset}`` where an asset is PNG bytes, an
    ndarray, a PIL image, a path or an object with ``.png``/``.data``/``.path`` (keys ending in ``print.top.N``,
    ``print.bottom.N`` or ``print.shoes.0`` are matched); ``fabric``/``folds``: tile/set objects, or None to use the
    spec's fabric id and the recipe's fold set (procedural fallback); ``kits``: an object/dict with optional
    ``recipe_dirs``, ``fabric_dirs``, ``fold_dirs``, ``kit_dirs`` lists."""
    req = request_from_character(kind, char, palette, prints, fabric, folds, kits)
    return compose(req, with_layers=with_layers)


def canonical_request_json(req: GarmentRequest) -> bytes:
    """Stable JSON of a request's cache-relevant parts (recipe sha, attrs, colours, fabric, folds, prints, kit selection)."""
    return canonical_json({
        "kind": req.kind, "recipe": req.recipe.sha256, "attrs": req.attrs, "params": req.params,
        "colours": {k: list(v) for k, v in sorted(req.colours.items())},
        "fabric": req.fabric.sha256 if req.fabric else None, "folds": req.folds.sha256 if req.folds else None,
        "prints": [[p.part_id, p.region, p.scale, p.wrap, PP.image_sha(p.image)] for p in req.prints],
        "shoe": req.shoe_style, "legwear": req.legwear, "extras": req.arm_extras, "palette": [list(c) for c in req.palette]})

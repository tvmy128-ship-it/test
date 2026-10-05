"""Gate A: free code checks on one 2D asset (PROMPT_BIBLE §7.1, FAILURE_MODES §3.7 and §5.4).

Code measures, the VLM answers yes/no (FAILURE_MODES §0.3). Every function here returns a ``CheckResult`` (or facts) and reads its
numbers from ``checks/thresholds.py``. A check that cannot run is wrapped by ``checks.runner.run_check`` into ``ran=False``.

Rule ids (``checks.json``):
``A_ALPHA`` (real transparency, painted checkerboard FFT, haze) · ``A_HALO`` · ``A_COMPONENTS`` · ``A_MARGIN`` · ``A_PALETTE`` ·
``A_SINGLE_COLOUR`` · ``A_STROKE`` (at the placed size) · ``A_SYMMETRY`` · ``A_HIGHLIGHT`` · ``A_GUIDE_LEFT`` · ``A_SIL_GUIDE`` ·
``A_DRIFT`` · ``A_SWATCH`` · ``A_LEAK`` · ``A_VIEWS`` · ``A_BADGE``. Also the code clean-up of bible §2.5 (``cleanup_alpha_asset``)
and the canonical framing (``canonical_frame``).
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

import numpy as np
from PIL import Image

from duoskin.checks import thresholds as TH
from duoskin.checks.model import CheckResult
from duoskin.checks.runner import build_result
from duoskin.imaging import palette as P


# ------------------------------------------------------------------ small geometry helpers
def alpha_min() -> int:
    """Alpha at or above which a pixel is visible (``img.alpha_binarize``, 128)."""
    return int(TH.get("img.alpha_binarize"))


def visible(alpha: np.ndarray) -> np.ndarray:
    """Bool mask of the visible pixels of an alpha channel."""
    return alpha >= alpha_min()


def _connectivity(connectivity: int | None) -> int:
    return int(TH.get("img.connectivity")) if connectivity is None else connectivity


def alpha_of(im: Image.Image) -> np.ndarray:
    """``(H, W) uint8`` alpha of an RGBA image (a RGB image is fully opaque)."""
    return np.asarray(im.convert("RGBA"))[..., 3]


def bbox_of(mask: np.ndarray) -> tuple[int, int, int, int] | None:
    """``(x0, y0, x1, y1)`` with exclusive upper bounds, or ``None`` for an empty mask."""
    ys, xs = np.nonzero(mask)
    if ys.size == 0:
        return None
    return int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1


def silhouette_iou(a: np.ndarray, b: np.ndarray) -> float:
    """Intersection over union of two bool masks of the same shape (two empty masks give 1.0)."""
    if a.shape != b.shape:
        raise ValueError(f"shape mismatch {a.shape} vs {b.shape}")
    u = np.logical_or(a, b).sum()
    return 1.0 if u == 0 else float(np.logical_and(a, b).sum() / u)


def label_components(mask: np.ndarray, connectivity: int | None = None) -> tuple[np.ndarray, int]:
    from scipy import ndimage as ndi

    k = int(TH.get("img.morph_kernel"))
    st = np.ones((k, k), bool) if _connectivity(connectivity) == int(TH.get("img.connectivity")) else None
    lab, n = ndi.label(mask, structure=st)
    return lab, int(n)


def component_areas(mask: np.ndarray, connectivity: int | None = None) -> list[int]:
    lab, n = label_components(mask, connectivity)
    return [] if n == 0 else [int(c) for c in np.bincount(lab.ravel())[1:]]


# ------------------------------------------------------------------ A_ALPHA
def checker_peak_ratio(im: Image.Image, *, block: int | None = None, k_min: int | None = None, max_blocks: int | None = None) -> tuple[float, str]:
    """Strongest two-dimensional periodic peak in the fully opaque parts of ``im`` (a painted checkerboard).

    For each fully opaque ``block x block`` tile the luminance is Hann-windowed and FFT'd. Bins on the axes and below ``k_min``
    (the low frequencies of ordinary art) are ignored, so stripes and large shapes do not count. A checkerboard puts most of the
    remaining energy in two diagonal peaks ``(k, +-k)`` and none on the axes, so the share of energy in the two strongest peaks
    (3x3 neighbourhoods) must be on the diagonal to count. Returns ``(ratio, evidence)``; ``ratio`` is 0 when no tile qualifies.
    """
    blk = int(TH.get("img.checker_block_px")) if block is None else block
    kmin = int(TH.get("img.checker_k_min")) if k_min is None else k_min
    arr = np.asarray(im.convert("RGBA"), dtype=np.float64)
    h, w = arr.shape[:2]
    b = min(blk, h, w)
    smallest = int(TH.get("img.checker_min_block_px"))
    if b < smallest:
        return 0.0, "image too small for the FFT test"
    lum = np.asarray(im.convert("L"), dtype=np.float64)
    opaque = arr[..., 3] >= 255
    cap = int(TH.get("img.checker_max_blocks")) if max_blocks is None else max_blocks
    best, note, seen = 0.0, "", False
    size = b
    while size >= smallest:                       # a pure patch of the pattern may only exist in smaller tiles (a subject covers the rest)
        r, n, s_ = _checker_scan(lum, opaque, size, kmin, cap)
        seen = seen or s_
        if r > best:
            best, note = r, n
        size //= 2
    if not best:
        note = "no two-dimensional periodic peak" if seen else "no fully opaque tile with contrast"
    return float(best), note


def _checker_scan(lum: np.ndarray, opaque: np.ndarray, b: int, kmin: int, max_blocks: int) -> tuple[float, str, bool]:
    """Scan fully opaque ``b x b`` tiles; returns ``(best ratio, evidence, whether any tile had contrast)``."""
    h, w = lum.shape
    step = max(1, b // 2)
    ys = sorted({*range(0, h - b + 1, step), h - b})
    xs = sorted({*range(0, w - b + 1, step), w - b})
    integral = np.pad(np.cumsum(np.cumsum(opaque.astype(np.float64), axis=0), axis=1), ((1, 0), (1, 0)))
    need = float(TH.get("img.checker_opaque_frac")) * b * b
    origins = [(y, x) for y in ys for x in xs
               if integral[y + b, x + b] - integral[y, x + b] - integral[y + b, x] + integral[y, x] >= need]
    if len(origins) > max_blocks:
        stride = len(origins) / max_blocks
        origins = [origins[int(i * stride)] for i in range(max_blocks)]
    win = np.outer(np.hanning(b), np.hanning(b))
    fy_all = np.fft.fftfreq(b) * b
    ky = np.abs(fy_all)[:, None]
    kx = (np.arange(b // 2 + 1))[None, :]
    valid = (ky >= kmin) & (kx >= kmin)
    best = 0.0
    note = ""
    seen = False
    min_std = float(TH.get("img.checker_min_std"))
    diag_tol = float(TH.get("img.checker_diag_tol"))
    axis_frac = float(TH.get("img.checker_axis_energy"))
    for y, x in origins:
        t = lum[y:y + b, x:x + b]
        if t.std() < min_std:
            continue
        seen = True
        spec = np.abs(np.fft.rfft2((t - t.mean()) * win)) ** 2
        spec[0, 0] = 0.0
        total = float(spec.sum())
        if total <= 0:
            continue
        work = np.where(valid, spec, 0.0)
        taken = 0.0
        diag_ok = False
        for i in range(2):
            iy, ix = np.unravel_index(np.argmax(work), work.shape)
            fy = round(fy_all[iy])
            y0, y1, x0, x1 = max(0, iy - 1), min(b, iy + 2), max(0, ix - 1), min(work.shape[1], ix + 2)
            e = float(spec[y0:y1, x0:x1].sum())
            taken += e
            work[y0:y1, x0:x1] = 0.0
            if i == 0:
                # a checkerboard has its fundamental on the diagonal and (almost) nothing on the axes at that frequency
                e_ax = max(float(spec[:3, max(0, ix - 1):ix + 2].sum()), float(spec[max(0, abs(fy) - 1):abs(fy) + 2, :2].sum()))
                diag_ok = abs(ix - abs(fy)) <= max(1.0, diag_tol * ix) and e_ax < axis_frac * e
        ratio = taken / total if diag_ok else 0.0
        if ratio > best:
            best = ratio
            note = f"tile at ({x},{y}) size {b}: two diagonal peaks hold {ratio:.2f} of the spectrum"
    return best, note, seen


def alpha_facts(im: Image.Image, frame: float | None = None) -> dict:
    """Measured facts for IMG-01..03 (FAILURE_MODES §5.4 plus a few more). The image must already be RGBA (IMG-07)."""
    if im.mode != "RGBA":
        raise AssertionError(f"IMG-07: expected RGBA, got {im.mode}")
    fr = float(TH.get("img.border_frame")) if frame is None else frame
    a = np.asarray(im)[..., 3]
    h, w = a.shape
    fy, fx = max(1, int(h * fr)), max(1, int(w * fr))
    border = np.concatenate([a[:fy].ravel(), a[-fy:].ravel(), a[:, :fx].ravel(), a[:, -fx:].ravel()])
    fg = visible(a)
    bb = bbox_of(fg)
    if bb is None:
        return {"empty": True, "clear_share": float((a == 0).mean()), "border_clear": bool((border == 0).all())}
    x0, y0, x1, y1 = bb
    bbox_area = (x1 - x0) * (y1 - y0)
    return {
        "empty": False,
        "bbox": bb,
        "clear_share": float((a == 0).mean()),
        "border_clear": bool((border == 0).all()),
        "border_max_alpha": int(max(a[0].max(), a[-1].max(), a[:, 0].max(), a[:, -1].max())),
        "haze_share": float(((a > 0) & (a < 255)).sum() / bbox_area),
        "margin_min": float(min(y0 / h, 1 - y1 / h, x0 / w, 1 - x1 / w)),
    }


def check_alpha(im: Image.Image, *, subject_sha: str = "", allow_checker: bool = False) -> CheckResult:
    """A_ALPHA (CHK-A02, HARD): real transparency.

    Fails on: not RGBA; an empty subject; a clear (alpha 0) share below ``img.clear_share_min``; an outer 2% frame that is not fully
    clear; a painted checkerboard (two-dimensional FFT peak in the opaque area); semi-transparent haze above ``img.haze_share_max``
    of the subject bbox. ``allow_checker`` is for prints whose motif is itself a checkerboard.
    """
    if im.mode != "RGBA":
        return build_result("A_ALPHA", passed=False, subject_sha=subject_sha, metric="mode", evidence=f"mode {im.mode}, RGBA required",
                            fix_hint="change_technique")
    f = alpha_facts(im)
    if f.get("empty"):
        return build_result("A_ALPHA", passed=False, subject_sha=subject_sha, metric="empty", evidence="no visible pixels",
                            fix_hint="regenerate")
    problems: list[str] = []
    hint = "none"
    clear_min = float(TH.get("img.clear_share_min"))
    haze_max = float(TH.get("img.haze_share_max"))
    chk_max = float(TH.get("img.checker_peak_ratio"))
    if f["clear_share"] < clear_min:
        problems.append(f"clear share {f['clear_share']:.2f} < {clear_min}")
        hint = "change_technique"
    if not f["border_clear"]:
        problems.append("outer frame is not fully transparent")
        hint = "change_technique"
    ratio, note = (0.0, "skipped") if allow_checker else checker_peak_ratio(im)
    if ratio >= chk_max:
        problems.append(f"painted checkerboard ({note})")
        hint = "change_technique"
    if f["haze_share"] > haze_max:
        problems.append(f"haze {f['haze_share']:.3f} > {haze_max}")
        hint = hint if hint != "none" else "code_alpha_cleanup"
    if problems:
        metric = "checker_peak_ratio" if ratio >= chk_max else ("haze_share" if f["haze_share"] > haze_max else "clear_share")
        value = {"checker_peak_ratio": ratio, "haze_share": f["haze_share"], "clear_share": f["clear_share"]}[metric]
        thr = {"checker_peak_ratio": TH.describe("img.checker_peak_ratio", "<"), "haze_share": TH.describe("img.haze_share_max", "<="),
               "clear_share": TH.describe("img.clear_share_min", ">=")}[metric]
        return build_result("A_ALPHA", passed=False, subject_sha=subject_sha, metric=metric, value=value, threshold=thr,
                            evidence="; ".join(problems), fix_hint=hint)  # type: ignore[arg-type]
    return build_result("A_ALPHA", passed=True, subject_sha=subject_sha, metric="clear_share", value=f["clear_share"],
                        threshold=TH.describe("img.clear_share_min", ">="),
                        evidence=f"clear {f['clear_share']:.2f}, haze {f['haze_share']:.4f}, checker {ratio:.3f}")


def halo_de(im: Image.Image, ring_px: int | None = None) -> tuple[float, float]:
    """Mean CIEDE2000 of the 1..``ring_px`` px ring outside the alpha>=128 contour, composited on black and on white, against the
    plain background. Returns ``(on_black, on_white)``; 0 for a clean cut-out."""
    arr = np.asarray(im.convert("RGBA"), dtype=np.float64)
    a = arr[..., 3] / 255.0
    fg = visible(arr[..., 3])
    ring = P.dilate(fg, int(TH.get("img.halo_ring_px")) if ring_px is None else ring_px) & ~fg
    if not ring.any():
        return 0.0, 0.0
    out = []
    for bg in (0.0, 255.0):
        comp = arr[..., :3] * a[..., None] + bg * (1 - a[..., None])
        lab = P.srgb_to_lab(comp[ring])
        ref = P.srgb_to_lab(np.full((int(ring.sum()), comp.shape[-1]), bg))
        out.append(float(P.deltaE2000(lab, ref).mean()))
    return out[0], out[1]


def check_halo(im: Image.Image, *, subject_sha: str = "") -> CheckResult:
    """A_HALO (IMG-02, HARD): the ring just outside the cut-out contour is as clean as the background on black and on white."""
    worst = max(halo_de(im))
    lim = float(TH.get("img.halo_de_max"))
    return build_result("A_HALO", passed=worst <= lim, subject_sha=subject_sha, metric="halo_de2000", value=worst,
                        threshold=TH.describe("img.halo_de_max", "<="), evidence=f"ring dE {worst:.2f} (worst of black/white)",
                        fix_hint="code_alpha_cleanup")


def gate_a_alpha(im: Image.Image, *, subject_sha: str = "", allow_checker: bool = False) -> list[CheckResult]:
    """CHK-A02 as the pipeline runs it: ``[A_ALPHA, A_HALO]``."""
    return [check_alpha(im, subject_sha=subject_sha, allow_checker=allow_checker), check_halo(im, subject_sha=subject_sha)]


# ------------------------------------------------------------------ A_COMPONENTS, A_MARGIN
def count_components(mask: np.ndarray, *, min_area_frac: float | None = None, connectivity: int | None = None) -> tuple[int, list[int]]:
    """Connected components (8-connectivity) larger than ``img.component_min_area`` of the mask bbox. Returns ``(count, areas)``."""
    frac = float(TH.get("img.component_min_area")) if min_area_frac is None else min_area_frac
    bb = bbox_of(mask)
    if bb is None:
        return 0, []
    bbox_area = (bb[2] - bb[0]) * (bb[3] - bb[1])
    areas = [a for a in component_areas(mask, connectivity) if a >= frac * bbox_area]
    return len(areas), sorted(areas, reverse=True)


def check_components(im_or_mask: Image.Image | np.ndarray, expected: int | tuple[int, int], *, subject_sha: str = "",
                     connectivity: int | None = None) -> CheckResult:
    """A_COMPONENTS (CHK-A04, HARD): pieces of alpha>=128 (or of a non-background mask) equal the count expected for the asset type.

    ``expected`` is an exact count or an inclusive ``(min, max)`` (``mouth_open`` 1..3). Pieces under 0.2% of the bbox are ignored.
    """
    mask = im_or_mask if isinstance(im_or_mask, np.ndarray) else visible(alpha_of(im_or_mask))
    n, areas = count_components(mask.astype(bool), connectivity=connectivity)
    lo, hi = (expected, expected) if isinstance(expected, int) else expected
    ok = lo <= n <= hi
    want = f"{lo}" if lo == hi else f"{lo}..{hi}"
    return build_result("A_COMPONENTS", passed=ok, subject_sha=subject_sha, metric="components", value=float(n),
                        threshold=f"== {want} (img.component_min_area {TH.get('img.component_min_area')}, DES)",
                        evidence=f"{n} piece(s), areas {areas[:6]}", fix_hint="code_alpha_cleanup" if n > hi else "regenerate")


def check_margin(im: Image.Image, *, subject_sha: str = "", min_margin: float | None = None) -> CheckResult:
    """A_MARGIN (CHK-A03, HARD): the alpha bbox keeps >= 6% on every side and the image border has alpha 0 (nothing cropped)."""
    lim = float(TH.get("img.margin_min")) if min_margin is None else min_margin
    if im.mode != "RGBA":
        return build_result("A_MARGIN", passed=False, subject_sha=subject_sha, metric="mode", evidence=f"mode {im.mode}")
    f = alpha_facts(im)
    if f.get("empty"):
        return build_result("A_MARGIN", passed=False, subject_sha=subject_sha, metric="empty", evidence="no visible pixels")
    a = alpha_of(im)
    border_max = int(max(a[0].max(), a[-1].max(), a[:, 0].max(), a[:, -1].max()))
    ok = f["margin_min"] >= lim and border_max == 0
    return build_result("A_MARGIN", passed=ok, subject_sha=subject_sha, metric="margin_min", value=f["margin_min"],
                        threshold=TH.describe("img.margin_min", ">="),
                        evidence=f"smallest margin {f['margin_min']:.3f}, border alpha max {border_max}", fix_hint="code_recrop")


def canonical_frame(im: Image.Image, size: tuple[int, int] | None = None, *, fill: float | tuple[float, float] | None = None,
                    margin: float | None = None, resample: int = Image.Resampling.LANCZOS) -> Image.Image:
    """Re-pad the subject (alpha bbox) onto a transparent canvas: the "canonical framing" after A_MARGIN.

    * ``fill=0.825`` (or a ``(lo, hi)`` pair, its midpoint is used): scale the subject, aspect preserved, so its long side is that
      share of the canvas long side (3D inputs: 80-85% of 2048 px). Resampling is premultiplied.
    * ``margin=0.10``: do not scale unless needed; centre the subject and make sure each side keeps this margin.
    The subject is centred. ``size`` defaults to the input size.
    """
    rgba = im.convert("RGBA")
    bb = bbox_of(alpha_of(rgba) >= 1)
    cw, ch = size or rgba.size
    if bb is None:
        return Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
    crop = rgba.crop(bb)
    sw, sh = crop.size
    if fill is not None:
        f = sum(fill) / 2 if isinstance(fill, tuple) else fill
        target = f * max(cw, ch)
        scale = target / max(sw, sh)
    else:
        m = float(TH.get("img.margin_min")) if margin is None else margin
        avail = min(cw * (1 - 2 * m), ch * (1 - 2 * m))
        scale = min(1.0, avail / max(sw, sh))
    nw, nh = max(1, round(sw * scale)), max(1, round(sh * scale))
    if (nw, nh) != (sw, sh):
        crop = crop.convert("RGBa").resize((nw, nh), resample).convert("RGBA")
    canvas = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
    canvas.paste(crop, ((cw - nw) // 2, (ch - nh) // 2))
    return canvas


# ------------------------------------------------------------------ A_PALETTE
def check_palette(im: Image.Image, allowed_hex: Sequence[str], *, subject_sha: str = "", mask: np.ndarray | None = None,
                  exclude_hex: Sequence[str] = ()) -> CheckResult:
    """A_PALETTE (CHK-A05, HARD, class consistency).

    Interiors are eroded by 1 px and clustered in CIELAB. Every cluster of at least 2% must be within CIEDE2000 12 of an allowed
    colour, the number of such clusters is at most the allowed count + 1, and a large cluster (>= 5%) farther than 15 rejects the
    asset (the auto-fix, ``snap_to_palette``, cannot rescue it).
    """
    facts = P.palette_facts(im, allowed_hex, mask=mask, exclude_hex=exclude_hex)
    if not facts.clusters:
        return build_result("A_PALETTE", passed=False, subject_sha=subject_sha, metric="clusters", evidence="no interior pixels",
                            fix_hint="regenerate")
    de_max = float(TH.get("img.palette_de_max"))
    reject = float(TH.get("img.palette_large_reject_de"))
    problems = []
    if facts.worst_de > de_max:
        bad = [f"{c.hex}({d:.1f})" for c, (_, d) in zip(facts.clusters, facts.nearest, strict=True)
               if c.share >= float(TH.get("img.palette_min_cluster")) and d > de_max]
        problems.append(f"clusters off palette: {', '.join(bad)}")
    if facts.counted > facts.allowed + 1:
        problems.append(f"{facts.counted} colours for {facts.allowed} allowed")
    hint = "none"
    if problems:
        hint = "regenerate" if facts.worst_large_de > reject else "code_palette_snap"
    return build_result("A_PALETTE", passed=not problems, subject_sha=subject_sha, metric="palette_de2000_max", value=facts.worst_de,
                        threshold=TH.describe("img.palette_de_max", "<="),
                        evidence="; ".join(problems) or f"{facts.counted} colour(s), worst dE {facts.worst_de:.1f}", fix_hint=hint)  # type: ignore[arg-type]


# ------------------------------------------------------------------ A_SINGLE_COLOUR
def distinct_interior_colours(im: Image.Image, tol: int | None = None) -> tuple[list[tuple[int, int, int]], int]:
    """Distinct opaque interior colours (RGB within ``tol`` per channel count as the same), and the number of anti-aliased edge
    pixels whose RGB is not within ``tol`` of one of them. Interior = alpha 255 eroded by 1 px (alpha >= 200 for hair-thin lines)."""
    arr = np.asarray(im.convert("RGBA"))
    a = arr[..., 3]
    tol = int(TH.get("img.single_colour_tol")) if tol is None else tol
    interior = P.erode(a == 255, 1)
    if interior.sum() < int(TH.get("img.min_interior_px")):
        interior = a >= int(TH.get("img.hairline_alpha"))
    px = arr[interior][:, :3].astype(int)
    colours: list[tuple[int, int, int]] = []
    if len(px):
        uniq, counts = np.unique(px, axis=0, return_counts=True)
        for i in np.argsort(-counts):
            c = tuple(int(v) for v in uniq[i])
            if not any(int(np.abs(np.array(c) - np.array(d)).max()) <= tol for d in colours):
                colours.append(c)  # type: ignore[arg-type]
    edge = (a > 0) & ~interior & (a < 255)
    wrong = 0
    if edge.any() and colours:
        ep = arr[edge][:, :3].astype(int)
        ref = np.array(colours[0])
        wrong = int((np.abs(ep - ref).max(axis=1) > tol).sum())
    return colours, wrong


def check_single_colour(im: Image.Image, *, expected_hex: str | None = None, subject_sha: str = "", tol: int | None = None) -> CheckResult:
    """A_SINGLE_COLOUR (CHK-A08 / FACE-01, HARD): a line feature has exactly one opaque interior colour, and its anti-aliased edge
    pixels keep that RGB (only alpha varies). ``expected_hex`` also pins the colour to the spec colour (dE <= 3)."""
    colours, wrong = distinct_interior_colours(im, tol)
    n_expected = int(TH.get("face.line_colours"))
    problems = []
    if len(colours) != n_expected:
        problems.append(f"{len(colours)} distinct interior colours")
    if wrong:
        problems.append(f"{wrong} edge pixels carry another RGB")
    if expected_hex and colours and P.de2000_rgb(colours[0], P.hex_to_rgb(expected_hex)) > float(TH.get("img.single_colour_de")):
        problems.append(f"colour {P.rgb_to_hex(colours[0])} is not the spec colour {expected_hex}")
    return build_result("A_SINGLE_COLOUR", passed=not problems, subject_sha=subject_sha, metric="distinct_colours",
                        value=float(len(colours)), threshold=TH.describe("face.line_colours", "=="),
                        evidence="; ".join(problems) or "one interior colour", fix_hint="code_palette_snap")


# ------------------------------------------------------------------ A_STROKE
def _opening(mask: np.ndarray, k: int) -> np.ndarray:
    import cv2

    m = mask.astype(np.uint8)
    ker = np.ones((k, k), np.uint8)
    # anchors chosen so that even sizes are a true opening (union of the k x k squares inside the mask), not a shifted one
    er = cv2.erode(m, ker, anchor=(0, 0), borderType=cv2.BORDER_CONSTANT, borderValue=0)
    return cv2.dilate(er, ker, anchor=(k - 1, k - 1)).astype(bool)


def _trimmed_skeleton(mask: np.ndarray, tip_px: int) -> np.ndarray:
    from scipy import ndimage as ndi
    from skimage.morphology import skeletonize

    sk = skeletonize(mask)
    if not sk.any():
        return sk
    k = int(TH.get("img.morph_kernel"))
    nb = ndi.convolve(sk.astype(np.uint8), np.ones((k, k), np.uint8), mode="constant") - 1
    ends = sk & (nb <= 1)
    if tip_px > 0 and ends.any():
        near = ndi.distance_transform_edt(~ends) <= tip_px
        trimmed = sk & ~near
        if trimmed.any():
            return trimmed
    return sk


def min_stroke_px(mask: np.ndarray, *, tol: float | None = None, tip_px: int | None = None, cap: int | None = None) -> int:
    """Thinnest sustained stroke width of ``mask`` in whole pixels (square-opening scale), tapers and tips ignored.

    The largest ``k`` such that at most ``img.stroke_thin_tol`` (10%) of the trimmed skeleton lies outside the opening of the mask by
    a ``k x k`` square. ``cap`` is returned for shapes at least that thick or without any skeleton. Axis-aligned pixel semantics:
    a 1 px line gives 1, a 2 px line gives 2, a 45 degree line of 1 px Euclidean width gives 1.
    """
    t = float(TH.get("img.stroke_thin_tol")) if tol is None else tol
    tip = int(TH.get("img.stroke_tip_px")) if tip_px is None else tip_px
    cap = int(TH.get("img.stroke_cap_px")) if cap is None else cap
    if not mask.any():
        return cap
    sk = _trimmed_skeleton(mask, tip)
    n = int(sk.sum())
    if n == 0:
        return cap

    def thin_frac(k: int) -> float:
        return float((sk & ~_opening(mask, k)).sum()) / n

    lo, hi = 1, cap
    if thin_frac(hi) <= t:
        return cap
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if thin_frac(mid) <= t:
            lo = mid
        else:
            hi = mid
    return lo


def placed_alpha(im: Image.Image, scale: float) -> Image.Image:
    """The asset at its placed size: ``scale`` = placed pixels per image pixel (the compositor's region scale); area-downsampled in
    premultiplied alpha like the compositor does. ``scale >= 1`` leaves the image as it is (upscaling cannot add stroke width)."""
    if scale >= 1.0:
        return im.convert("RGBA")
    w, h = max(1, round(im.width * scale)), max(1, round(im.height * scale))
    return im.convert("RGBa").resize((w, h), Image.Resampling.BOX).convert("RGBA")


def check_stroke(im: Image.Image, *, placed_scale: float = 1.0, min_px: float | None = None, min_frac_of_bbox: float | None = None,
                 subject_sha: str = "", check_id: str = "A_STROKE") -> CheckResult:
    """A_STROKE (CHK-A08 / IMG-16, HARD): the thinnest stroke at the **placed** size is at least ``img.stroke_px_min_placed`` (2 px).

    ``placed_scale`` converts the working image to the placed (final texel) size first. ``min_frac_of_bbox`` instead gives a minimum
    relative to the subject bbox long side (3D inputs: 2%; badges: ``img.badge_outline_min`` of the width).
    """
    pl = placed_alpha(im, placed_scale)
    a = alpha_of(pl)
    mask = visible(a)
    bb = bbox_of(mask)
    if bb is None:
        return build_result(check_id, passed=False, subject_sha=subject_sha, metric="stroke_px", evidence="no visible pixels")
    if min_frac_of_bbox is not None:
        need = max(1.0, min_frac_of_bbox * max(bb[2] - bb[0], bb[3] - bb[1]))
        thr = f">= {min_frac_of_bbox:.3f} of the bbox ({need:.1f} px)"
    else:
        need = float(TH.get("img.stroke_px_min_placed")) if min_px is None else min_px
        thr = TH.describe("img.stroke_px_min_placed", ">=") if min_px is None else f">= {need} px"
    got = min_stroke_px(mask, cap=int(np.ceil(need)) + int(TH.get("img.stroke_cap_margin_px")))
    ok = got >= need
    return build_result(check_id, passed=ok, subject_sha=subject_sha, metric="stroke_px_placed", value=float(got), threshold=thr,
                        evidence=f"thinnest stroke {got} px at placed size {pl.size[0]}x{pl.size[1]} (need {need:.1f})",
                        fix_hint="regenerate")


def gradient_share(im: Image.Image, *, grad_de: float | None = None, flat_de: float | None = None) -> float:
    """Share of interior pixels that sit in a smooth colour ramp: a small but non-zero CIE76 step to both horizontal neighbours.

    Flat art has steps of 0 inside fills and large steps at edges, so its value is near 0; airbrushed gradients give a high value.
    """
    grad_de = float(TH.get("img.gradient_step_max")) if grad_de is None else grad_de
    flat_de = float(TH.get("img.gradient_step_min")) if flat_de is None else flat_de
    arr = np.asarray(im.convert("RGBA"))
    interior = P.erode(arr[..., 3] == 255, 1)
    if not interior.any():
        return 0.0
    lab = P.srgb_to_lab(arr[..., :3])
    dx = np.sqrt((np.diff(lab, axis=1) ** 2).sum(-1))
    dy = np.sqrt((np.diff(lab, axis=0) ** 2).sum(-1))
    stepx = np.zeros(interior.shape)
    stepy = np.zeros(interior.shape)
    stepx[:, 1:-1] = np.maximum(dx[:, :-1], dx[:, 1:])
    stepy[1:-1, :] = np.maximum(dy[:-1, :], dy[1:, :])
    step = np.maximum(stepx, stepy)
    smooth = (step >= flat_de) & (step <= grad_de)
    return float(smooth[interior].mean())


def check_flat_fills(im: Image.Image, *, subject_sha: str = "") -> CheckResult:
    """The HARD half of the old style profile (IMG-16, class buildability): the smooth-gradient pixel share on face lines and
    prints stays within ``img.gradient_share_max`` because palette snap needs flat fills. Reported as ``A_STROKE``'s sibling and
    run by the same step."""
    g = gradient_share(im)
    lim = float(TH.get("img.gradient_share_max"))
    return build_result("A_STROKE", passed=g <= lim, subject_sha=subject_sha, metric="gradient_share", value=g,
                        threshold=TH.describe("img.gradient_share_max", "<="), evidence=f"smooth-gradient share {g:.3f}",
                        fix_hint="code_palette_snap")


def style_metrics(im: Image.Image) -> dict[str, float]:
    """The SOFT style-profile numbers (IMG-09): median stroke width, gradient share, specular share, high-frequency energy ratio."""
    from scipy import ndimage as ndi

    arr = np.asarray(im.convert("RGBA"))
    a = arr[..., 3]
    fg = visible(a)
    if not fg.any():
        return {"stroke_px_median": 0.0, "gradient_share": 0.0, "specular_share": 0.0, "hf_energy": 0.0}
    dist = ndi.distance_transform_edt(fg)
    from skimage.morphology import skeletonize

    sk = skeletonize(fg)
    med = float(np.median(2.0 * dist[sk])) if sk.any() else 0.0
    lab = P.srgb_to_lab(arr[..., :3])
    spec = ((lab[..., 0] >= float(TH.get("img.specular_l_min"))) & (np.hypot(lab[..., 1], lab[..., 2]) < float(TH.get("img.specular_chroma_max"))) & fg).sum() / max(1, fg.sum())
    lum = lab[..., 0] * fg
    f = np.abs(np.fft.fft2(lum - lum[fg].mean() * fg)) ** 2
    h, w = f.shape
    yy = np.fft.fftfreq(h)[:, None]
    xx = np.fft.fftfreq(w)[None, :]
    r = np.hypot(yy, xx)
    hf = float(f[r > float(TH.get("img.hf_cutoff"))].sum() / max(f.sum(), float(TH.get("num.eps"))))
    return {"stroke_px_median": med, "gradient_share": gradient_share(im), "specular_share": float(spec), "hf_energy": hf}


def check_style_profile(im: Image.Image, profile: Mapping[str, float] | None = None, *, subject_sha: str = "") -> CheckResult:
    """A_STYLE (CHK-A08-STYLE, SOFT forever, ranking input): median stroke width within the band of the duo's profile, specular share,
    gradient share and high-frequency energy ratio. With no profile only the absolute limits are tested."""
    m = style_metrics(im)
    lo, hi = TH.get("img.stroke_ratio_band")
    problems = []
    if profile and profile.get("stroke_px_median"):
        ratio = m["stroke_px_median"] / profile["stroke_px_median"]
        if not (lo <= ratio <= hi):
            problems.append(f"stroke width ratio {ratio:.2f} outside {lo}-{hi}")
    if m["specular_share"] > float(TH.get("img.specular_share_max")):
        problems.append(f"specular share {m['specular_share']:.3f}")
    if m["gradient_share"] > float(TH.get("img.gradient_share_max")):
        problems.append(f"gradient share {m['gradient_share']:.3f}")
    if profile and profile.get("hf_energy") and m["hf_energy"] > profile["hf_energy"] * float(TH.get("img.hf_energy_ratio_max")):
        problems.append("high-frequency energy above the profile")
    return build_result("A_STYLE", passed=not problems, subject_sha=subject_sha, metric="style_profile", value=m["stroke_px_median"],
                        threshold=TH.describe("img.stroke_ratio_band", "in"), evidence="; ".join(problems) or "within the style profile")


# ------------------------------------------------------------------ A_SYMMETRY, A_HIGHLIGHT, A_GUIDE_LEFT
def mirror_iou(mask: np.ndarray, about: str = "bbox") -> float:
    """IoU of a mask with its left-right mirror, about the bbox centre (default) or the canvas centre."""
    bb = bbox_of(mask)
    if bb is None:
        return 0.0
    if about == "canvas":
        return silhouette_iou(mask, mask[:, ::-1])
    x0, _, x1, _ = bb
    cx2 = x0 + x1 - 1                          # twice the centre column
    w = mask.shape[1]
    xs = np.arange(w)
    src = cx2 - xs
    ok = (src >= 0) & (src < w)
    flipped = np.zeros_like(mask)
    flipped[:, ok] = mask[:, src[ok]]
    return silhouette_iou(mask, flipped)


def check_symmetry(im: Image.Image, *, subject_sha: str = "", about: str = "bbox") -> CheckResult:
    """A_SYMMETRY (CHK-A07, HARD for parts declared symmetric): left-right mirror IoU >= 0.90."""
    iou = mirror_iou(visible(alpha_of(im)), about)
    return build_result("A_SYMMETRY", passed=iou >= float(TH.get("img.symmetry_iou_min")), subject_sha=subject_sha, metric="mirror_iou",
                        value=iou, threshold=TH.describe("img.symmetry_iou_min", ">="), evidence=f"mirror IoU {iou:.3f}",
                        fix_hint="regenerate")


def highlight_blobs(im: Image.Image) -> int:
    """Small near-white blobs that sit inside the opaque iris (a painted highlight)."""
    arr = np.asarray(im.convert("RGBA"))
    fg = visible(arr[..., 3])
    if not fg.any():
        return 0
    lab = P.srgb_to_lab(arr[..., :3])
    near_white = (lab[..., 0] >= float(TH.get("img.white_l_min"))) & (np.hypot(lab[..., 1], lab[..., 2]) < float(TH.get("img.white_chroma_max"))) & P.erode(fg, 1)
    lab_i, n = label_components(near_white)
    if n == 0:
        return 0
    limit = float(TH.get("img.highlight_blob_frac")) * fg.sum()
    return int(sum(1 for a in np.bincount(lab_i.ravel())[1:] if 0 < a <= limit))


def check_no_highlight(im: Image.Image, *, subject_sha: str = "") -> CheckResult:
    """A_HIGHLIGHT (FACE-04, HARD): the AI iris carries no white highlight blobs; highlights are drawn by code only."""
    n = highlight_blobs(im)
    return build_result("A_HIGHLIGHT", passed=n <= int(TH.get("img.highlight_max_blobs")), subject_sha=subject_sha,
                        metric="highlight_blobs", value=float(n), threshold=TH.describe("img.highlight_max_blobs", "<="),
                        evidence=f"{n} near-white blob(s) inside the iris", fix_hint="masked_edit")


def guide_left_share(im: Image.Image, guide_hex: str, region: np.ndarray | None = None) -> float:
    """Share of ``region`` (default: everything visible) whose colour is within ``img.guide_left_de`` of the guide grey."""
    arr = np.asarray(im.convert("RGBA"))
    zone = (arr[..., 3] > 0) if region is None else region.astype(bool)
    if not zone.any():
        return 0.0
    de = P.deltaE2000(P.srgb_to_lab(arr[..., :3]), P.hex_to_lab(guide_hex))
    return float((de[zone] <= float(TH.get("img.guide_left_de"))).mean())


def check_guide_left(im: Image.Image, guide_hex: str, *, region: np.ndarray | None = None, subject_sha: str = "") -> CheckResult:
    """A_GUIDE_LEFT (HAIR-04, HARD): no guide-grey pixels remain where the model had to paint over them (< 0.5%)."""
    share = guide_left_share(im, guide_hex, region)
    return build_result("A_GUIDE_LEFT", passed=share < float(TH.get("img.guide_left_max")), subject_sha=subject_sha,
                        metric="guide_grey_share", value=share, threshold=TH.describe("img.guide_left_max", "<"),
                        evidence=f"{share:.4f} of the editable area is still guide grey", fix_hint="masked_edit")


# ------------------------------------------------------------------ A_SIL_GUIDE
def check_sil_guide(fg: np.ndarray, guide: np.ndarray, *, rows: tuple[int, int] | None = None, min_iou: float | None = None,
                    max_outside: float | None = None, subject_sha: str = "") -> CheckResult:
    """A_SIL_GUIDE (CHK-G1-03, HARD): silhouette IoU of the foreground ``fg`` against the code guide mask ``guide``.

    ``rows=(y0, y1)`` restricts both masks to a band (the body below the neckline). Concept figures need IoU >= 0.85 and at most 3%
    of the guide area as foreground outside the guide body; hair views use ``hair.guide_iou_min`` (0.98) for the head.
    """
    f, g = fg.astype(bool), guide.astype(bool)
    if rows is not None:
        keep = np.zeros_like(f)
        keep[rows[0]:rows[1]] = True
        f, g = f & keep, g & keep
    iou_lim = float(TH.get("con.body_iou_min")) if min_iou is None else min_iou
    out_lim = float(TH.get("con.volume_outside_max")) if max_outside is None else max_outside
    iou = silhouette_iou(f, g)
    outside = float((f & ~g).sum() / max(1, g.sum()))
    ok = iou >= iou_lim and outside <= out_lim
    return build_result("A_SIL_GUIDE", passed=ok, subject_sha=subject_sha, metric="guide_iou", value=iou,
                        threshold=f">= {iou_lim} (con.body_iou_min, DES); outside <= {out_lim}",
                        evidence=f"IoU {iou:.3f}; foreground outside the guide {outside:.3f} of its area", fix_hint="regenerate")


# ------------------------------------------------------------------ A_DRIFT
def check_drift(final: Image.Image, draft: Image.Image, *, bg_hex: str | None = None, subject_sha: str = "") -> CheckResult:
    """A_DRIFT (CHK-A10 / CHK-G1-08, HARD): the finalized image still is the chosen draft.

    Foreground silhouette IoU >= 0.92 (alpha, or "not the flat background ``bg_hex``" for opaque concept images), every colour of
    the draft (clusters >= 2%) moved at most CIEDE2000 5, and the component count is equal.
    """
    if final.size != draft.size:
        return build_result("A_DRIFT", passed=False, subject_sha=subject_sha, metric="size", evidence=f"size {final.size} vs {draft.size}",
                            fix_hint="regenerate")
    fa = P.foreground_mask(final, bg_hex=bg_hex)
    da = P.foreground_mask(draft, bg_hex=bg_hex)
    iou = silhouette_iou(fa, da)
    nf, _ = count_components(fa)
    nd, _ = count_components(da)
    k, merge = int(TH.get("img.drift_palette_k")), float(TH.get("img.drift_merge_de"))
    pd = P.extract_palette(draft, k=k, mask=da, exclude_hex=[bg_hex] if bg_hex else (), merge_de=merge)
    pf = P.extract_palette(final, k=k, mask=fa, exclude_hex=[bg_hex] if bg_hex else (), merge_de=merge)
    shift = 0.0
    if pd and pf:
        lf = np.array([c.lab for c in pf])
        for c in pd:
            if c.share >= float(TH.get("img.palette_min_cluster")):
                shift = max(shift, float(P.deltaE2000(np.array(c.lab)[None, :], lf).min()))
    iou_lim = float(TH.get("img.final_iou_min"))
    de_lim = float(TH.get("img.final_de_max"))
    problems = []
    if iou < iou_lim:
        problems.append(f"silhouette IoU {iou:.3f} < {iou_lim}")
    if shift > de_lim:
        problems.append(f"a colour moved dE {shift:.1f} > {de_lim}")
    if nf != nd:
        problems.append(f"components {nf} vs draft {nd}")
    return build_result("A_DRIFT", passed=not problems, subject_sha=subject_sha, metric="silhouette_iou", value=iou,
                        threshold=TH.describe("img.final_iou_min", ">="), evidence="; ".join(problems) or f"IoU {iou:.3f}, colour shift {shift:.1f}",
                        fix_hint="regenerate")


# ------------------------------------------------------------------ A_SWATCH, A_LEAK
def dominant_colour(im: Image.Image, mask: np.ndarray, *, exclude_hex: Sequence[str] = ()) -> str | None:
    """The most common colour (k-means, shading merged) inside ``mask``."""
    cl = P.extract_palette(im, k=int(TH.get("img.dominant_k")), mask=mask, exclude_hex=exclude_hex, merge_de=float(TH.get("img.dominant_merge_de")))
    return cl[0].hex if cl else None


def check_swatch(im: Image.Image, zones: Mapping[str, tuple[np.ndarray, str]], *, exclude_hex: Sequence[str] = (),
                 subject_sha: str = "") -> CheckResult:
    """A_SWATCH (concept, SOFT): the dominant colour of each guide zone is within dE 15 of the planned colour (warning only, the
    palette is re-extracted after Gate 1). ``zones`` maps a zone name to ``(mask, planned_hex)``."""
    lim = float(TH.get("con.swatch_de_warn"))
    off = []
    worst = 0.0
    for name, (mask, planned) in zones.items():
        dom = dominant_colour(im, mask, exclude_hex=exclude_hex)
        if dom is None:
            off.append(f"{name}: empty zone")
            worst = max(worst, float(TH.get("num.far_de")))
            continue
        d = P.de2000_hex(dom, planned)
        worst = max(worst, d)
        if d > lim:
            off.append(f"{name}: {dom} vs plan {planned} dE {d:.1f}")
    return build_result("A_SWATCH", passed=not off, subject_sha=subject_sha, metric="swatch_de2000_max", value=worst,
                        threshold=TH.describe("con.swatch_de_warn", "<="), evidence="; ".join(off) or f"worst dE {worst:.1f}")


def partner_only_colours(own_hex: Sequence[str], partner_hex: Sequence[str], shared_hex: Sequence[str] = ()) -> list[str]:
    """The partner's role colours that are more than ``con.partner_only_de`` (12) from **every** own colour and shared anchor colour.

    Empty when the structure shares or swaps colours on purpose (same_club, mirror, seasonal_twins pairs), so those pairs pass A_LEAK.
    """
    limit = float(TH.get("con.partner_only_de"))
    keep = [P.normalise_hex(h) for h in own_hex] + [P.normalise_hex(h) for h in shared_hex]
    out = []
    for h in partner_hex:
        nh = P.normalise_hex(h)
        if not keep or all(P.de2000_hex(nh, k) > limit for k in keep):
            out.append(nh)
    return sorted(set(out))


def leak_share(im: Image.Image, partner_only: Sequence[str], *, bg_hex: str | Sequence[str] | None = "#f2f2f2") -> float:
    """Share of the figure's interior pixels within ``con.partner_only_de`` of a partner-only colour."""
    if not partner_only:
        return 0.0
    arr = np.asarray(im.convert("RGBA"))
    fg = P.foreground_mask(im, bg_hex=bg_hex)
    interior = P.erode(fg, 1)
    if not interior.any():
        return 0.0
    lab = P.srgb_to_lab(arr[..., :3][interior])
    plab = P.palette_lab(partner_only)
    de = P.deltaE2000(lab[:, None, :], plab[None, :, :]).min(axis=1)
    return float((de <= float(TH.get("con.partner_only_de"))).mean())


def check_leak(im: Image.Image, own_hex: Sequence[str], partner_hex: Sequence[str], shared_hex: Sequence[str] = (), *,
               bg_hex: str | Sequence[str] | None = "#f2f2f2", subject_sha: str = "") -> CheckResult:
    """A_LEAK (CHK-G1-04, HARD): no more than 3% of a figure carries a partner-only colour (see ``partner_only_colours``)."""
    po = partner_only_colours(own_hex, partner_hex, shared_hex)
    share = leak_share(im, po, bg_hex=bg_hex)
    lim = float(TH.get("con.leak_area_max"))
    return build_result("A_LEAK", passed=share <= lim, subject_sha=subject_sha, metric="partner_only_share", value=share,
                        threshold=TH.describe("con.leak_area_max", "<="),
                        evidence=f"{share:.3f} of the figure within dE {TH.get('con.partner_only_de')} of {len(po)} partner-only colour(s)",
                        fix_hint="regenerate")


# ------------------------------------------------------------------ A_VIEWS
def check_views(views: Mapping[str, Image.Image], *, subject_sha: str = "") -> CheckResult:
    """A_VIEWS (HARD): a multiview set has front, back, left and right; subject heights agree within 3%, the ground line within 1% of
    the height, front and back are horizontally centred within 2%, and no view is cropped (margin >= 6%)."""
    need = ("front", "back", "left", "right")
    missing = [v for v in need if v not in views]
    if missing:
        return build_result("A_VIEWS", passed=False, subject_sha=subject_sha, metric="views", evidence=f"missing {missing}",
                            fix_hint="regenerate")
    boxes = {}
    for v in need:
        a = visible(alpha_of(views[v]))
        bb = bbox_of(a)
        if bb is None:
            return build_result("A_VIEWS", passed=False, subject_sha=subject_sha, metric="views", evidence=f"{v} view is empty")
        boxes[v] = (bb, views[v].size)
    heights = np.array([b[0][3] - b[0][1] for b in boxes.values()], float)
    med = float(np.median(heights))
    h_dev = float(np.abs(heights - med).max() / med)
    grounds = np.array([b[0][3] / b[1][1] for b in boxes.values()])
    g_dev = float(grounds.max() - grounds.min())
    centre_dev = max(abs((boxes[v][0][0] + boxes[v][0][2]) / 2 / boxes[v][1][0] - 1 / 2) for v in ("front", "back"))
    margin = min(min(b[0][0] / b[1][0], 1 - b[0][2] / b[1][0], b[0][1] / b[1][1], 1 - b[0][3] / b[1][1]) for b in boxes.values())
    problems = []
    if h_dev > float(TH.get("img.views_height_tol")):
        problems.append(f"heights differ {h_dev:.3f}")
    if g_dev > float(TH.get("img.views_ground_tol")):
        problems.append(f"ground line differs {g_dev:.3f}")
    if centre_dev > float(TH.get("img.views_centre_tol")):
        problems.append(f"front/back off-centre {centre_dev:.3f}")
    if margin < float(TH.get("img.margin_min")):
        problems.append(f"a view is cropped (margin {margin:.3f})")
    return build_result("A_VIEWS", passed=not problems, subject_sha=subject_sha, metric="height_dev", value=h_dev,
                        threshold=TH.describe("img.views_height_tol", "<="), evidence="; ".join(problems) or "views agree",
                        fix_hint="code_recrop")


# ------------------------------------------------------------------ A_BADGE (I6)
def solidity(mask: np.ndarray) -> float:
    """Area over convex-hull area of the largest piece (spikes lower it)."""
    import cv2

    m = mask.astype(np.uint8)
    if not m.any():
        return 0.0
    cnts, _ = cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    c = max(cnts, key=cv2.contourArea)
    hull = cv2.convexHull(c)
    hull_px = np.zeros(m.shape, np.uint8)
    cv2.fillConvexPoly(hull_px, hull, 1)             # the hull in pixels (the polygon area would sit half a pixel inside the edge)
    ha = int(hull_px.sum())
    return min(1.0, float(mask.sum() / ha)) if ha > 0 else 0.0


def check_badge(im: Image.Image, *, subject_sha: str = "") -> CheckResult:
    """A_BADGE (I6, HARD): one compact silhouette (solidity >= 0.8, spikes inflate the bounds) with no holes."""
    from scipy import ndimage as ndi

    m = visible(alpha_of(im))
    sol = solidity(m)
    holes = int(ndi.binary_fill_holes(m).sum() - m.sum())
    ok = sol >= float(TH.get("acc.slab_convexity_min")) and holes == 0
    return build_result("A_BADGE", passed=ok, subject_sha=subject_sha, metric="solidity", value=sol,
                        threshold=TH.describe("acc.slab_convexity_min", ">="), evidence=f"solidity {sol:.3f}, {holes} hole pixel(s)",
                        fix_hint="code_alpha_cleanup" if holes else "regenerate")


# ------------------------------------------------------------------ rung-1 clean-up (bible §2.5)
@dataclass
class CleanupReport:
    islands_removed: int = 0
    holes_filled: int = 0
    edge_px_decontaminated: int = 0
    snap_changed_px: int = 0
    actions: list[str] = field(default_factory=list)


def decontaminate_edges(im: Image.Image) -> tuple[Image.Image, int]:
    """Recolour every pixel with alpha < 255 to the RGB of its nearest opaque pixel (alpha is kept). Returns ``(image, count)``."""
    from scipy import ndimage as ndi

    arr = np.array(im.convert("RGBA"), dtype=np.uint8)
    solid = arr[..., 3] == 255
    partial = ~solid
    if not solid.any() or not partial.any():
        return Image.fromarray(arr, "RGBA"), 0
    idx = ndi.distance_transform_edt(partial, return_distances=False, return_indices=True)
    arr[..., :3] = np.where(partial[..., None], arr[idx[0], idx[1], :3], arr[..., :3])
    return Image.fromarray(arr, "RGBA"), int(((arr[..., 3] > 0) & partial).sum())


def cleanup_alpha_asset(im: Image.Image, palette_hex: Sequence[str] | None = None, *, binarize: bool = True, fill_holes: bool = True,
                        min_island_frac: float | None = None) -> tuple[Image.Image, CleanupReport]:
    """The code auto-fix of bible §2.5, in its order: binarise alpha at 128 -> remove islands under 0.2% of the bbox -> fill holes ->
    decontaminate edge colours -> palette-snap interiors only. ``binarize=False`` keeps soft edges (and still decontaminates)."""
    from scipy import ndimage as ndi

    rep = CleanupReport()
    arr = np.array(im.convert("RGBA"), dtype=np.uint8)
    thr = int(TH.get("img.alpha_binarize"))
    frac = float(TH.get("img.component_min_area")) if min_island_frac is None else min_island_frac
    if binarize:
        arr[..., 3] = np.where(arr[..., 3] >= thr, 255, 0)
        rep.actions.append("binarize_alpha")
    vis = arr[..., 3] >= thr
    bb = bbox_of(vis)
    if bb is not None:
        lab, n = label_components(vis)
        area = (bb[2] - bb[0]) * (bb[3] - bb[1])
        sizes = np.bincount(lab.ravel())
        for i in range(1, n + 1):
            if sizes[i] < frac * area:
                arr[lab == i, 3] = 0
                rep.islands_removed += 1
        if rep.islands_removed:
            rep.actions.append("remove_islands")
    out = Image.fromarray(arr, "RGBA")
    if fill_holes:
        vis = arr[..., 3] >= thr
        filled = ndi.binary_fill_holes(vis)
        holes = filled & ~vis
        if holes.any():
            idx = ndi.distance_transform_edt(~vis, return_distances=False, return_indices=True)
            arr[..., :3] = np.where(holes[..., None], arr[idx[0], idx[1], :3], arr[..., :3])
            arr[..., 3] = np.where(holes, 255, arr[..., 3])
            rep.holes_filled = int(holes.sum())
            rep.actions.append("fill_holes")
            out = Image.fromarray(arr, "RGBA")
    out, n_dec = decontaminate_edges(out)
    rep.edge_px_decontaminated = n_dec
    rep.actions.append("decontaminate_edges")
    if palette_hex:
        out, st = P.snap_to_palette(out, palette_hex, interior_only=True)
        rep.snap_changed_px = st.changed_px
        rep.actions.append("snap_interiors")
    return out, rep

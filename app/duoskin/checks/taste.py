"""Taste checks: SOFT warnings only, forever (APP_SPEC §3.4 and §3.6, PROPOSAL_DECISION #3).

They never block, never use a fix from the cap, never change an approved part and never set a target look: each one only flags the bad
tail. All of them are **pure functions over supplied pixels and masks**; the render and ID-pass code (``render/``) and the compositor
supply the arrays, so nothing here renders anything, reads a file or needs a model. The kind of each result comes from ``data/checks.json``
(class ``taste``), and a crash becomes ``ran=False`` through ``checks/runner.py`` (soft results never block either way).

* :func:`phone_top_colours` (DUO-03): is the planned main colour among the top colours at phone size? (KEEP)
* :func:`main_colour_contrast` (DUO-03): the old "main dE >= 15" check, folded in; only for structures that want different mains.
* :func:`silhouette_overlap` and :func:`accessory_phone_size` (DUO-04): A-vs-B hair and accessory silhouette overlap (a difference
  measure only, never "bigger is better"), and accessories that are tiny at phone size. (KEEP)
* :func:`garment_layout_similarity` (TASTE_LAYOUT): the colour-agnostic adjusted Rand index of the two compositor label maps.
* :func:`colour_plan_ratio` (TASTE_RATIO): optional declared-versus-built colour shares (no universal ratio).
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np

from duoskin.checks import runner
from duoskin.checks.model import CheckResult
from duoskin.prompts.limits import RGB_CHANNELS, describe, thr


def _fail(check_id: str, why: str, subject_sha: str) -> CheckResult:
    return runner.fail_closed(check_id, why, subject_sha)


# --------------------------------------------------------------------------------------------- downscaling
def area_downscale(render: np.ndarray, height: int) -> np.ndarray:
    """Area-downscale an ``(H, W, 3 or 4)`` uint8 image so it is ``height`` px tall (the phone-size view of DUO-03)."""
    from PIL import Image

    try:
        h, w, channels = render.shape
    except ValueError:
        raise ValueError("render must be (H, W, 3 or 4)") from None
    if channels < RGB_CHANNELS:
        raise ValueError("render must be (H, W, 3 or 4)")
    if height >= h:
        return np.ascontiguousarray(render[..., :3])
    width = max(1, round(w * height / h))
    im = Image.fromarray(render[..., :3].astype(np.uint8), "RGB").resize((width, height), Image.Resampling.BOX)
    return np.asarray(im)


def mode_downscale(labels: np.ndarray, shape: tuple[int, int]) -> np.ndarray:
    """Downscale an integer label map to ``shape`` by taking the most frequent label of each source block (no blending of ids)."""
    h, w = labels.shape
    oh, ow = shape
    ys = np.linspace(0, h, oh + 1).astype(int)
    xs = np.linspace(0, w, ow + 1).astype(int)
    out = np.zeros((oh, ow), dtype=labels.dtype)
    lo = int(labels.min()) if labels.size else 0
    shifted = (labels - lo).astype(np.int64)
    for i in range(oh):
        for j in range(ow):
            block = shifted[ys[i]:max(ys[i + 1], ys[i] + 1), xs[j]:max(xs[j + 1], xs[j] + 1)]
            out[i, j] = np.bincount(block.ravel()).argmax() + lo
    return out


# --------------------------------------------------------------------------------------------- DUO-03
def top_colours(render: np.ndarray, labels: np.ndarray, clothes_labels: Sequence[int], *, thumb_height: int | None = None,
                k: int | None = None) -> list[tuple[str, float]]:
    """The dominant colours of clothes and hair at phone size as ``[(hex, share), ...]`` (largest first).

    Area-downscales the render to the phone height, mode-downscales the label map to match, keeps only the pixels whose label is in
    ``clothes_labels`` (skin is simply not listed), erodes that mask by 1 px and runs k-means in CIELAB with shading bands merged
    (``imaging.palette.extract_palette``).
    """
    from PIL import Image

    from duoskin.imaging.palette import extract_palette

    if render.shape[:2] != labels.shape:
        raise ValueError(f"render {render.shape[:2]} and label map {labels.shape} differ in size")
    height = int(thumb_height or thr("duo.thumb_height_px")[1])
    small = area_downscale(render, height)
    lab_small = mode_downscale(labels, small.shape[:2])
    mask = np.isin(lab_small, np.asarray(list(clothes_labels)))
    clusters = extract_palette(Image.fromarray(small, "RGB"), k=int(k or thr("taste.kmeans_k")), mask=mask, erode_px=1)
    return [(c.hex, c.share) for c in clusters]


def phone_top_colours(render: np.ndarray, labels: np.ndarray, clothes_labels: Sequence[int], planned_main_hex: str, *,
                      thumb_height: int | None = None, k: int | None = None, top_n: int | None = None,
                      subject_sha: str = "") -> CheckResult:
    """DUO-03 (SOFT): warn when the planned main colour is not among the top ``taste.top_n`` (2) colours at phone size."""
    try:
        from duoskin.imaging.palette import de2000_hex

        tops = top_colours(render, labels, clothes_labels, thumb_height=thumb_height, k=k)
        if not tops:
            return _fail("DUO-03", "no clothes or hair pixels at phone size", subject_sha)
        n = int(top_n or thr("taste.top_n"))
        tol = float(thr("img.palette_de_max"))
        dists = [de2000_hex(h, planned_main_hex) for h, _ in tops[:n]]
        best = min(dists)
        listing = ", ".join(f"{h} {share:.0%}" for h, share in tops[:n + 1])
        return runner.build_result("DUO-03", passed=best <= tol, subject_sha=subject_sha, metric="planned_main_in_top_colours", value=best,
                                   threshold=f"planned main within dE2000 {tol:g} of a top-{n} colour", evidence=f"top colours at phone size: {listing}",
                                   fix_hint="none")
    except Exception as exc:    # noqa: BLE001 - checks fail closed
        return _fail("DUO-03", f"{type(exc).__name__}: {exc}", subject_sha)


def main_colour_contrast(top_a: Sequence[str], top_b: Sequence[str], structure: str, *, subject_sha: str = "") -> CheckResult:
    """DUO-03 (SOFT): the old main-colour dE check, only under structures that want different mains (complement, leader_chaotic,
    seasonal_twins, object_mascot): the dominant phone-size colours of A and B should differ by ``pln.contrast_colour_de``."""
    try:
        from duoskin.checks.plan_rules import profile
        from duoskin.imaging.palette import de2000_hex

        rule = profile(structure)["colour_rule"]
        if rule["kind"] not in ("main_contrast", "season_split") or not top_a or not top_b:
            return runner.build_result("DUO-03", passed=True, subject_sha=subject_sha, metric="main_colour_contrast",
                                       evidence=f"{structure}: no different-mains rule")
        need = float(thr("pln.contrast_colour_de"))
        d = de2000_hex(top_a[0], top_b[0])
        return runner.build_result("DUO-03", passed=d >= need, subject_sha=subject_sha, metric="main_colour_contrast", value=d,
                                   threshold=describe("pln.contrast_colour_de", ">="), evidence=f"dominant colours {top_a[0]} and {top_b[0]} differ by dE2000 {d:.1f}")
    except Exception as exc:    # noqa: BLE001
        return _fail("DUO-03", f"{type(exc).__name__}: {exc}", subject_sha)


# --------------------------------------------------------------------------------------------- DUO-04
def mask_iou(a: np.ndarray, b: np.ndarray) -> float:
    """Intersection over union of two boolean masks of the same shape (``0.0`` when both are empty)."""
    if a.shape != b.shape:
        raise ValueError(f"masks differ in size: {a.shape} and {b.shape}")
    a, b = a.astype(bool), b.astype(bool)
    union = int(np.logical_or(a, b).sum())
    return float(np.logical_and(a, b).sum() / union) if union else 0.0


def silhouette_overlap(mask_a: np.ndarray, mask_b: np.ndarray, *, threshold: float | None = None, subject_sha: str = "") -> CheckResult:
    """DUO-04 (SOFT): IoU of A's and B's hair+accessory masks (same frame; the ID pass supplies them at Gate 3, the 2D front-view alpha
    minus the head guide at Gate 2). Warns above the caller's 95th percentile of approved duos, or above ``duo.silhouette_iou_cold``
    until ``calib.percentile_min_duos`` approved duos exist (DUO-04)."""
    try:
        limit = float(threshold if threshold is not None else thr("duo.silhouette_iou_cold"))
        source = "caller's percentile" if threshold is not None else "duo.silhouette_iou_cold"
        iou = mask_iou(mask_a, mask_b)
        return runner.build_result("DUO-04", passed=iou <= limit, subject_sha=subject_sha, metric="hair_accessory_silhouette_iou", value=iou,
                                   threshold=f"<= {limit:g} ({source})", evidence=f"A-vs-B hair and accessory silhouette IoU {iou:.2f}")
    except Exception as exc:    # noqa: BLE001
        return _fail("DUO-04", f"{type(exc).__name__}: {exc}", subject_sha)


def accessory_phone_size(masks: Mapping[str, np.ndarray], full_height_px: int, *, thumb_height: int | None = None,
                         min_px: float | None = None, subject_sha: str = "") -> CheckResult:
    """DUO-04 (SOFT): warn when an accessory's longest side is below ``taste.acc_min_px`` at phone size (area-downscaled)."""
    try:
        thumb = int(thumb_height or thr("duo.thumb_height_px")[1])
        floor = float(min_px if min_px is not None else thr("taste.acc_min_px"))
        scale = thumb / float(full_height_px)
        sizes: dict[str, float] = {}
        for name, m in masks.items():
            ys, xs = np.nonzero(m)
            if len(ys):
                sizes[name] = float(max(ys.max() - ys.min() + 1, xs.max() - xs.min() + 1)) * scale
        tiny = {n: s for n, s in sizes.items() if s < floor}
        worst = min(sizes.values()) if sizes else None
        return runner.build_result("DUO-04", passed=not tiny, subject_sha=subject_sha, metric="accessory_phone_size_px", value=worst,
                                   threshold=f">= {floor:g} px at {thumb} px tall", evidence=", ".join(f"{n} {s:.1f}px" for n, s in sorted(tiny.items())) or "no tiny accessory")
    except Exception as exc:    # noqa: BLE001
        return _fail("DUO-04", f"{type(exc).__name__}: {exc}", subject_sha)


# --------------------------------------------------------------------------------------------- TASTE_LAYOUT
def adjusted_rand_index(a: np.ndarray, b: np.ndarray) -> float:
    """Adjusted Rand index of two label partitions of the same pixels (1 means the same layout, around 0 means unrelated); colour-agnostic."""
    x, y = np.asarray(a).ravel(), np.asarray(b).ravel()
    if x.shape != y.shape:
        raise ValueError("label maps differ in size")
    n = x.size
    if n < 2:
        return 1.0
    _, xi = np.unique(x, return_inverse=True)
    _, yi = np.unique(y, return_inverse=True)
    table = np.zeros((xi.max() + 1, yi.max() + 1), dtype=np.int64)
    np.add.at(table, (xi, yi), 1)

    def comb2(v: np.ndarray) -> np.ndarray:
        return v.astype(np.float64) * (v - 1) / 2.0           # float64: pixel-pair counts overflow int64 products at full size

    sum_ij = comb2(table).sum()
    sum_a = comb2(table.sum(axis=1)).sum()
    sum_b = comb2(table.sum(axis=0)).sum()
    total = comb2(np.int64(n))
    expected = sum_a * sum_b / total
    maximum = (sum_a + sum_b) / 2.0
    if maximum == expected:
        return 1.0
    return float((sum_ij - expected) / (maximum - expected))


def garment_layout_similarity(labels_a: np.ndarray, labels_b: np.ndarray, *, ignore: Sequence[int] = (), threshold: float | None = None,
                              subject_sha: str = "") -> CheckResult:
    """TASTE_LAYOUT (SOFT): colour-block layout similarity of A's and B's compositor label maps over the front and back areas, ignoring
    the colours (ARI over the label partitions) and the labels in ``ignore`` (transparent or skin). Warns above ``taste.layout_ari_warn``."""
    try:
        if labels_a.shape != labels_b.shape:
            raise ValueError("label maps differ in size")
        skip = np.asarray(list(ignore))
        keep = ~(np.isin(labels_a, skip) | np.isin(labels_b, skip)) if skip.size else np.ones(labels_a.shape, bool)
        limit = float(threshold if threshold is not None else thr("taste.layout_ari_warn"))
        ari = adjusted_rand_index(labels_a[keep], labels_b[keep])
        return runner.build_result("TASTE_LAYOUT", passed=ari <= limit, subject_sha=subject_sha, metric="layout_ari", value=ari,
                                   threshold=f"<= {limit:g} (taste.layout_ari_warn)", evidence=f"colour-block layouts of A and B agree with ARI {ari:.2f}")
    except Exception as exc:    # noqa: BLE001
        return _fail("TASTE_LAYOUT", f"{type(exc).__name__}: {exc}", subject_sha)


# --------------------------------------------------------------------------------------------- TASTE_RATIO
def colour_plan_ratio(pixel_counts: Sequence[int], colour_plan: str, *, subject_sha: str = "") -> CheckResult:
    """TASTE_RATIO (SOFT, optional): the built colour shares (clothes pixels only, largest first) against the declared ``colour_plan``.
    Warns when a share is off by more than ``taste.plan_ratio_share_off`` points. ``allover_pattern`` has no ratio and always passes."""
    try:
        from duoskin.prompts.catalog import data_json

        target = data_json("rules.json")["plan"]["colour_plan_ratios"][colour_plan]
        counts = sorted((int(c) for c in pixel_counts if int(c) > 0), reverse=True)
        if not target or not counts:
            return runner.build_result("TASTE_RATIO", passed=True, subject_sha=subject_sha, metric="colour_plan_share_off",
                                       evidence=f"{colour_plan}: no ratio to check")
        total = float(sum(counts))
        shares = [c / total for c in counts]
        shares += [0.0] * max(0, len(target) - len(shares))
        pts = float(data_json("rules.json")["plan"]["points_per_unit"])
        tol = float(thr("taste.plan_ratio_share_off")) / pts
        off = max(abs(shares[i] - t) for i, t in enumerate(target))
        return runner.build_result("TASTE_RATIO", passed=off <= tol, subject_sha=subject_sha, metric="colour_plan_share_off", value=off * pts,
                                   threshold=describe("taste.plan_ratio_share_off", "<="),
                                   evidence=f"{colour_plan}: built shares " + "/".join(f"{s:.0%}" for s in shares[:len(target)]))
    except Exception as exc:    # noqa: BLE001
        return _fail("TASTE_RATIO", f"{type(exc).__name__}: {exc}", subject_sha)


__all__: list[Any] = [
    "accessory_phone_size",
    "adjusted_rand_index",
    "area_downscale",
    "colour_plan_ratio",
    "garment_layout_similarity",
    "main_colour_contrast",
    "mask_iou",
    "mode_downscale",
    "phone_top_colours",
    "silhouette_overlap",
    "top_colours",
]

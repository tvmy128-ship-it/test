"""Roblox rule validators for the classic clothing templates (APP_SPEC §5.4 / §10.5, FAILURE_MODES §7.5).

``validate_template(png, kind, label_map, recipe)`` returns one ``CheckResult`` per rule (fail closed: a check that raises is
``ran=False``). ``validate_accessory`` (the 3D accessory file gate) lives in ``roblox/mesh_validators.py`` and is re-exported
here, so ``roblox.validators`` has the two functions of the spec's module table.

Rules (ids from ``data/checks.json``; the SOFT sub-flags are unregistered ids that only warn):

======================  ======  =================================================================================================
id                      kind    what
======================  ======  =================================================================================================
CHK-B01                 assert  every region crop has its exact size; paint-ids round trip (CLO-01)
CHK-B02                 assert  585x559, RGBA, 8 bit, PNG, no colour chunks, file size under the limit (CLO-02)
CHK-B03                 hard    shared 2-px gaps filled 1 px per side; open sides opaque 2 px beyond an opaque edge (CLO-03)
CHK-B04                 hard    base fabric layer: seam ΔE2000 mean <= 6 and max <= 15 over every adjacency pair (CLO-04)
CHK-B05                 assert  shoes/gloves/bracelets inside their bands; kit pieces only on the right template (CLO-07)
CHK-B06                 hard    semi-transparent garment pixels <= 0.5% (alpha policy, CLO-10)
CHK-B07                 assert  layer-stack order/hash; prints inside their region unless wrap; no mirrored art (CLO-12/15/16)
CHK-B08                 assert  numbered-edge orientation self-test: cap edges, side seams, R/L limbs (CLO-05/06)
CHK-B11                 assert  nothing outside the 18 boxes and their bleed ring; bare skin is alpha 0 (CLO-18)
A_OCR                   hard    no template label words / text (hook; ``imaging.ocr`` by default)
CHK-B05.split_rows      soft    trim/print edges within 2 px of rows 170, 418/419, 467 (CLO-07)
CHK-B05.print_inset     soft    print pixels within 5 px of a region edge (CLO-08)
CHK-B05.hidden_rows     soft    prints in the possibly hidden leg rows 355-377 or on Pants leg U faces (CLO-08)
CHK-B04.composite       soft    the same seam metric on the final composite (CLO-04)
CHK-B06.skin_in_clothing soft   opaque garment pixels within ΔE 6 of the skin tone (CLO-11)
CHK-B06.waistband_hidden soft   a pants waistband under an opaque shirt (CLO-09)
CHK-MOD01               hard    modesty colour present and ΔE >= 10 from the skin tone (plan; BODY-01)
CHK-MOD01.midriff       soft    bare midriff gap above the pants waist (POL-03)
======================  ======  =================================================================================================
"""
from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping, Sequence
from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np

from duoskin.checks import thresholds as TH
from duoskin.checks.model import CheckResult, not_applicable, not_run
from duoskin.checks.policy import effective_kind
from duoskin.checks.runner import CheckUnavailable
from duoskin.models.common import sha256_of
from duoskin.roblox import limits_clothing as LC
from duoskin.roblox import template as T
from duoskin.roblox.mesh_validators import validate_accessory

STAGE_ORDER = ("blocks", "fabric", "folds", "details", "prints", "kit", "finish")
LABEL_PRINT, LABEL_BRACELET, LABEL_SHOES, LABEL_LEGWEAR = 3, 5, 6, 7
SOFT_IDS = {"CHK-B05.split_rows": ["CLO-07"], "CHK-B05.print_inset": ["CLO-08"], "CHK-B05.hidden_rows": ["CLO-08"],
            "CHK-B04.composite": ["CLO-04"], "CHK-B06.skin_in_clothing": ["CLO-11"],
            "CHK-B06.waistband_hidden": ["CLO-09"], "CHK-MOD01.midriff": ["POL-03"]}
FM = {"CHK-B01": ["CLO-01"], "CHK-B02": ["CLO-02", "EXP-07"], "CHK-B03": ["CLO-03"], "CHK-B04": ["CLO-04"],
      "CHK-B05": ["CLO-07", "CLO-08"], "CHK-B06": ["CLO-10"], "CHK-B07": ["CLO-12", "CLO-15", "CLO-16"],
      "CHK-B08": ["CLO-05", "CLO-06", "CLO-17", "PRM-11"], "CHK-B11": ["CLO-18"], "A_OCR": ["POL-04", "CLO-18"],
      "CHK-MOD01": ["BODY-01"], **SOFT_IDS}
DEFAULT_KIND = {"CHK-B01": "assert", "CHK-B02": "assert", "CHK-B03": "hard", "CHK-B04": "hard", "CHK-B05": "assert",
                "CHK-B06": "hard", "CHK-B07": "assert", "CHK-B08": "assert", "CHK-B11": "assert", "A_OCR": "hard",
                "CHK-MOD01": "hard"}


class NotApplicable(Exception):
    """Raised by a check that finds an OPTIONAL input absent (no plan, no skin tone, no paired template, no OCR engine): the result is
    a recorded ``not_applicable`` with the reason. A REQUIRED input that is missing raises ``CheckUnavailable`` instead, which is
    ``ran=False`` (fail closed, a failed hard/assert check). Kit-flag N/A (no head/body base) is the runner's job, not ours."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


# --------------------------------------------------------------------------------------------------------------------
# result helpers
# --------------------------------------------------------------------------------------------------------------------
def _kind(check_id: str) -> str:
    return effective_kind(check_id, declared=DEFAULT_KIND.get(check_id, "soft"))  # type: ignore[arg-type]


def _res(check_id: str, passed: bool, metric: str = "", value: float | None = None, threshold: str = "", evidence: str = "",
         fix_hint: str = "none", sha: str = "") -> CheckResult:
    return CheckResult(check_id=check_id, fm_ids=list(FM.get(check_id, [])), subject_sha=sha, kind=_kind(check_id), passed=bool(passed),
                       metric=metric, value=None if value is None else float(value), threshold=threshold,
                       evidence=" ".join(str(evidence).split())[:600], fix_hint=fix_hint if not passed else "none",  # type: ignore[arg-type]
                       thresholds_version=TH.THRESHOLDS_VERSION)


def _guard(check_id: str, fn: Callable[[], CheckResult | list[CheckResult]], sha: str) -> list[CheckResult]:
    """Run one check function fail-closed: an exception is ``ran=False``; ``NotApplicable`` is a recorded N/A."""
    try:
        out = fn()
    except NotApplicable as e:
        return [not_applicable(check_id, _kind(check_id), e.reason, fm_ids=list(FM.get(check_id, [])), subject_sha=sha,  # type: ignore[arg-type]
                               thresholds_version=TH.THRESHOLDS_VERSION)]
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException as e:  # noqa: BLE001 - fail closed on anything, including AssertionError
        why = f"{type(e).__name__}: {e}"
        if isinstance(e, CheckUnavailable):
            why = f"unavailable: {e}"
        return [not_run(check_id, _kind(check_id), why[:500], fm_ids=list(FM.get(check_id, [])), subject_sha=sha,  # type: ignore[arg-type]
                        thresholds_version=TH.THRESHOLDS_VERSION)]
    return out if isinstance(out, list) else [out]


# --------------------------------------------------------------------------------------------------------------------
# inputs
# --------------------------------------------------------------------------------------------------------------------
def _load(png: Any) -> tuple[np.ndarray, bytes | None, T.PngFacts | None]:
    """(RGBA uint8 array, raw bytes if known, PNG facts if bytes)."""
    if isinstance(png, (str, Path)):
        png = Path(png).read_bytes()
    if isinstance(png, (bytes, bytearray)):
        data = bytes(png)
        facts = T.inspect_png(data)
        if not facts.is_png:
            raise CheckUnavailable("the file is not a PNG")
        return T.decode_png_rgba8(data), data, facts
    if isinstance(png, np.ndarray):
        return png, None, None
    return np.array(png.convert("RGBA"), dtype=np.uint8), None, None   # PIL image


def _as_array(x: Any) -> np.ndarray | None:
    if x is None:
        return None
    if isinstance(x, np.ndarray):
        return x
    return _load(x)[0]


def _rgb(c: Any) -> tuple[int, int, int] | None:
    from duoskin.imaging.palette import hex_to_rgb

    if c is None or c == "":
        return None
    if isinstance(c, str):
        return hex_to_rgb(c)
    return (int(c[0]), int(c[1]), int(c[2]))


def _recipe_ctx(recipe: Any) -> dict[str, Any]:
    """Normalise ``recipe`` to a context dict: recipe_id, attrs, vars, hem_rows, print_slots, requires_bottom."""
    if recipe is None:
        return {}
    if isinstance(recipe, Mapping):
        ctx = dict(recipe)
    else:                                                  # an imaging.recipes.Recipe: resolve its defaults
        from duoskin.imaging import recipes as RCP

        attrs = recipe.attrs_with_defaults({})
        env = RCP.resolve_vars(recipe, attrs, {})
        ctx = {"recipe_id": recipe.recipe_id, "attrs": attrs, "vars": env, "print_slots": [s.model_dump() for s in recipe.print_slots],
               "requires_bottom": dict(recipe.requires_bottom)}
    env = ctx.get("vars") or {}
    if "hem_rows" not in ctx:
        ctx["hem_rows"] = sorted({round(env[k]) for k in ("torso_end", "sleeve_end", "leg_hem") if k in env})
    return ctx


# --------------------------------------------------------------------------------------------------------------------
# CHK-B01, CHK-B02
# --------------------------------------------------------------------------------------------------------------------
@lru_cache(maxsize=1)
def _crop_selftest() -> str:
    """'' when every region crop has its exact size and the paint-ids round trip is exact; else a message."""
    probe = T.paint_region_ids()
    painted = 0
    for k in T.REGION_ORDER:
        c = T.crop(probe, k)
        if (c.shape[1], c.shape[0]) != T.SIZE[k]:
            return f"{k}: crop is {c.shape[1]}x{c.shape[0]}, expected {T.SIZE[k]}"
        if not (c[..., 0] == T.REGION_IDS[k]).all() or not (c[..., 3] == 255).all():
            return f"{k}: round trip changed pixels"
        painted += c.shape[0] * c.shape[1]
    if int((probe[..., 3] > 0).sum()) != painted:
        return "paint-ids wrote pixels outside the boxes"
    sizes = {"torso_f": (128, 128), "torso_b": (128, 128), "torso_u": (128, 64), "torso_d": (128, 64), "torso_r": (64, 128),
             "torso_l": (64, 128), "rlimb_u": (64, 64), "llimb_d": (64, 64), "rlimb_f": (64, 128)}
    for k, s in sizes.items():
        if T.SIZE[k] != s:
            return f"{k}: table says {T.SIZE[k]}, the template says {s}"
    return ""


def check_b01(img: np.ndarray) -> CheckResult:
    if img.shape != (T.HEIGHT, T.WIDTH, 4):
        return _res("CHK-B01", False, "image_shape", None, f"== {T.HEIGHT}x{T.WIDTH}x4", f"image shape is {img.shape}")
    for k in T.REGION_ORDER:
        c = T.crop(img, k)
        if (c.shape[1], c.shape[0]) != T.SIZE[k]:
            return _res("CHK-B01", False, "crop_size", None, "exact", f"{k} crop is {c.shape[1]}x{c.shape[0]}, expected {T.SIZE[k]}")
    msg = _crop_selftest()
    return _res("CHK-B01", not msg, "region_crops_exact", 0.0 if not msg else 1.0, "every crop exact; paint-ids round trip exact",
                msg or "18 regions: exact sizes, round trip exact")


def check_b02(img: np.ndarray, data: bytes | None, facts: T.PngFacts | None) -> CheckResult:
    problems: list[str] = []
    if data is None:                                  # an array: encode with the single writer and check what it produced
        data = T.encode_png_rgba8(img) if img.shape == (T.HEIGHT, T.WIDTH, 4) else b""
        facts = T.inspect_png(data) if data else None
        note = "array input; encoded by the template writer. "
    else:
        note = ""
    if facts is None or not facts.is_png:
        problems.append("not a PNG")
    else:
        if (facts.width, facts.height) != T.SIZE_WH:
            problems.append(f"size {facts.width}x{facts.height} != {T.WIDTH}x{T.HEIGHT}")
        if facts.color_type != LC.COLOUR_TYPE_RGBA or facts.bit_depth != LC.BIT_DEPTH:
            problems.append(f"colour type {facts.color_type} depth {facts.bit_depth} (need RGBA 8-bit)")
        if facts.colour_chunks:
            problems.append(f"colour chunks {list(facts.colour_chunks)}")
    if data and len(data) > LC.MAX_FILE_BYTES:
        problems.append(f"file is {len(data)} bytes, over {LC.MAX_FILE_BYTES}")
    if img.shape != (T.HEIGHT, T.WIDTH, 4) or img.dtype != np.uint8:
        problems.append(f"pixels are {img.shape} {img.dtype}")
    return _res("CHK-B02", not problems, "png_facts", float(len(problems)), f"{T.WIDTH}x{T.HEIGHT} RGBA 8-bit PNG, no colour chunks, <= {LC.MAX_FILE_BYTES} bytes (tpl.size_wh, DOC)",
                note + ("; ".join(problems) if problems else f"{len(data)} bytes, RGBA 8-bit, no colour chunks"), "code_recrop")


# --------------------------------------------------------------------------------------------------------------------
# CHK-B03 gap fill and bleed, CHK-B11 outside pixels
# --------------------------------------------------------------------------------------------------------------------
def check_b03(img: np.ndarray) -> CheckResult:
    lo, hi = LC.bleed_range_px()
    ys, xs, sy, sx, _ = T._ring_tables(lo, 1)
    dst, src = img[ys, xs], img[sy, sx]
    same_alpha = dst[:, 3] == src[:, 3]
    same_rgb = np.all(dst[:, :3] == src[:, :3], axis=1) | (src[:, 3] == 0)
    bad = ~(same_alpha & same_rgb)
    n_gap = int(sum(1 for s in T.ADJACENCY if s.gap))
    return _res("CHK-B03", not bad.any(), "ring_pixels_not_copies", float(bad.sum()),
                f"{n_gap} shared {LC.gap_px()}-px gaps filled 1 px per side; open sides copy their edge {lo}-{hi} px (tpl.gap_px, tpl.open_side_bleed_px)",
                f"{int(bad.sum())} of {len(ys)} ring pixels differ from the edge pixel they should copy", "code_bleed")


def check_b11(img: np.ndarray, label_map: np.ndarray | None, ctx: dict[str, Any]) -> CheckResult:
    hi = LC.bleed_range_px()[1]
    allowed = T.allowed_pixels(hi)
    alpha = img[..., 3]
    stray = (alpha > 0) & ~allowed
    parts = [f"{int(stray.sum())} stray pixels outside the 18 boxes and their {hi}-px bleed ring"]
    ok = not stray.any()
    if label_map is not None:
        inside = T.region_label_map() > 0
        skin_opaque = (label_map == 0) & (alpha > 0) & inside
        garment_clear = (label_map > 0) & (alpha == 0) & inside
        parts.append(f"{int(skin_opaque.sum())} bare-skin pixels with alpha > 0")
        parts.append(f"{int(garment_clear.sum())} garment-labelled pixels with alpha 0")
        ok = ok and not skin_opaque.any() and not garment_clear.any()
    return _res("CHK-B11", ok, "stray_alpha_pixels", float(stray.sum()), "outside boxes+ring alpha == 0; bare skin alpha == 0 (CLO-18)",
                "; ".join(parts), "code_alpha_cleanup")


# --------------------------------------------------------------------------------------------------------------------
# CHK-B04 seam continuity
# --------------------------------------------------------------------------------------------------------------------
def seam_stats(img: np.ndarray, seams: Sequence[T.Seam] | None = None) -> list[dict[str, Any]]:
    """Per seam: mean/max/95th-percentile CIEDE2000 between the touching edge pixels (both sides garment), and the share of
    pixels where only one side is garment (an intentional cut edge or an error)."""
    from duoskin.imaging.palette import deltaE2000, srgb_to_lab

    out: list[dict[str, Any]] = []
    for s in (seams if seams is not None else T.ADJACENCY):
        ea, eb = T.seam_edges(img, s)
        both = (ea[:, 3] > 0) & (eb[:, 3] > 0)
        one = (ea[:, 3] > 0) != (eb[:, 3] > 0)
        if both.any():
            d = deltaE2000(srgb_to_lab(ea[both, :3].astype(np.float64)), srgb_to_lab(eb[both, :3].astype(np.float64)))
            mean, mx, p95 = float(d.mean()), float(d.max()), float(np.percentile(d, 95))
        else:
            mean = mx = p95 = 0.0
        out.append({"seam": f"{s.a}.{s.side_a}|{s.b}.{s.side_b}", "kind": s.kind, "mean": mean, "max": mx, "p95": p95, "n": int(both.sum()),
                    "one_sided": float(one.sum() / len(ea))})
    return out


def check_b04(base: np.ndarray) -> CheckResult:
    mean_max, max_max = LC.seam_de_limits()
    stats = seam_stats(base)
    worst_mean = max(stats, key=lambda s: s["mean"])
    worst_max = max(stats, key=lambda s: s["max"])
    ok = worst_mean["mean"] <= mean_max and worst_max["max"] <= max_max
    return _res("CHK-B04", ok, "seam_de_mean_worst", worst_mean["mean"],
                f"mean <= {mean_max} and max <= {max_max} on every pair (tpl.seam_de_mean_max, tpl.seam_de_max)",
                f"{len(stats)} seams; worst mean {worst_mean['mean']:.2f} at {worst_mean['seam']}; worst max {worst_max['max']:.2f} at {worst_max['seam']}",
                "change_technique")


def check_b04_from_labels(img: np.ndarray, label_map: np.ndarray) -> CheckResult:
    """CHK-B04 without the compositor's base layer: the seam pixel pairs where both sides are plain fabric (label 1) on the final file.
    Stitches and folds sit on label 1 too, so the tail is judged at the 95th percentile instead of the strict maximum (approximate;
    pass ``base_layer`` for the exact check)."""
    mean_max, max_max = LC.seam_de_limits()
    fab = np.where(label_map[..., None] == 1, img, 0).astype(np.uint8)
    stats = [s for s in seam_stats(fab) if s["n"] > 0]
    if not stats:
        return _res("CHK-B04", True, "seam_de_mean_worst", 0.0, f"mean <= {mean_max}, p95 <= {max_max} (approximate)", "no seam has fabric on both sides")
    wm = max(stats, key=lambda s: s["mean"])
    wp = max(stats, key=lambda s: s["p95"])
    ok = wm["mean"] <= mean_max and wp["p95"] <= max_max
    return _res("CHK-B04", ok, "seam_de_mean_worst", wm["mean"], f"mean <= {mean_max} and p95 <= {max_max} on every pair (no base layer: label-1 pixels)",
                f"{len(stats)} seams with fabric on both sides; worst mean {wm['mean']:.2f} at {wm['seam']}; worst p95 {wp['p95']:.2f} at {wp['seam']}",
                "change_technique")


def check_b04_composite(img: np.ndarray) -> CheckResult:
    """The seam metric on the final composite (SOFT, CLO-04). A 1-px stitch or an anti-aliased colour edge may reach a seam on
    purpose, so the tail is judged at the 95th percentile instead of the single worst pixel (the HARD check on the base fabric
    layer keeps the strict maximum)."""
    mean_max, max_max = LC.seam_de_limits()
    stats = [s for s in seam_stats(img) if s["n"] > 0]
    if not stats:
        return _res("CHK-B04.composite", True, "seam_de_mean_worst", 0.0, "SOFT: same limits as CHK-B04", "no seam has garment on both sides")
    wm = max(stats, key=lambda s: s["mean"])
    wp = max(stats, key=lambda s: s["p95"])
    ok = wm["mean"] <= mean_max and wp["p95"] <= max_max
    return _res("CHK-B04.composite", ok, "seam_de_mean_worst", wm["mean"],
                f"SOFT: mean <= {mean_max} and 95th percentile <= {max_max} on every pair",
                f"composite seams (prints may end at an edge on purpose): worst mean {wm['mean']:.2f} at {wm['seam']}, "
                f"worst p95 {wp['p95']:.2f} at {wp['seam']}")


# --------------------------------------------------------------------------------------------------------------------
# CHK-B05 / CHK-B06 / alpha policy
# --------------------------------------------------------------------------------------------------------------------
def _row_groups(rows: np.ndarray) -> list[tuple[int, int]]:
    """Consecutive runs of row numbers as (first, last)."""
    if len(rows) == 0:
        return []
    rows = np.unique(rows)
    cuts = np.nonzero(np.diff(rows) > 1)[0]
    starts = np.concatenate([[0], cuts + 1])
    ends = np.concatenate([cuts, [len(rows) - 1]])
    return [(int(rows[a]), int(rows[b])) for a, b in zip(starts, ends)]


def check_b05(kind: str, label_map: np.ndarray | None) -> CheckResult:
    if label_map is None:
        raise CheckUnavailable("no label map: shoe, glove and bracelet placement cannot be verified")
    problems: list[str] = []
    lo, hi = LC.shoe_top_row_range()
    shoes = label_map == LABEL_SHOES
    legwear = label_map == LABEL_LEGWEAR
    brace = label_map == LABEL_BRACELET
    if kind == "shirt" and (shoes.any() or legwear.any()):
        problems.append("shoes or legwear labels on a Shirt")
    if kind == "pants" and brace.any():
        problems.append("bracelet/glove labels on Pants")
    for k in T.REGION_ORDER:
        if T.PART_OF[k] == "torso":
            if (T.crop(shoes, k).any() or T.crop(brace, k).any()):
                problems.append(f"kit piece on {k}")
            continue
        face = T.FACE_OF[k]
        _x0, y0, _x1, _y1 = T.REGIONS[k]
        if face in ("f", "b", "l", "r"):
            rows = np.nonzero(T.crop(shoes, k).any(axis=1))[0] + y0
            if len(rows) and not (lo <= int(rows.min()) <= hi):
                problems.append(f"{k}: shoe top edge at row {int(rows.min())} outside rows {lo}-{hi}")
            rows = np.nonzero(T.crop(brace, k).any(axis=1))[0] + y0
            for a, b in _row_groups(rows):
                if T.band_of_rows(k, a, b) is None:
                    problems.append(f"{k}: bracelet/glove rows {a}-{b} are not inside one band {list(T.LIMB_BANDS)}")
        elif face == "u":
            if T.crop(shoes, k).any() or T.crop(brace, k).any():
                problems.append(f"kit piece on {k}")
    return _res("CHK-B05", not problems, "kit_pieces_outside_bands", float(len(problems)),
                f"shoe top in rows {lo}-{hi}; gloves/bracelets inside one band of {list(LC.limb_bands())} (tpl.limb_bands, tpl.shoe_top_row_range)",
                "; ".join(problems[:6]) or "shoes, legwear, bracelets and gloves sit inside their bands", "regenerate")


def check_split_rows(img: np.ndarray, ctx: dict[str, Any]) -> CheckResult:
    """CLO-07 (SOFT): trim or print edges within 2 px of the R15 split rows 170, 418/419 and 467.

    Forbidden rows are the rows closer than 2 px to a split (torso 169-171, limbs 417-420 and 466-468). A step between two
    opaque rows is flagged when either row is forbidden and the colour changes by more than CIEDE2000 20; an alpha edge is
    flagged when the OPAQUE row is forbidden (a piece that ends at row 465 or starts at 469 lies inside its band). Alpha edges
    at the recipe's own hem rows (torso hem, sleeve end, leg hem, waist) are by design and exempt."""
    from duoskin.imaging.palette import deltaE2000, srgb_to_lab

    hem = set(ctx.get("hem_rows") or [])
    flagged: list[str] = []
    total = 0
    t_lo, t_hi = T.FORBIDDEN_ROWS_TORSO
    groups: list[tuple[tuple[str, ...], set[int]]] = [(("torso_f", "torso_b", "torso_l", "torso_r"), set(range(t_lo, t_hi + 1)))]
    forb_limb = {r for a, b in T.FORBIDDEN_ROWS_LIMB for r in range(a, b + 1)}
    groups.append((("rlimb_f", "rlimb_b", "rlimb_l", "rlimb_r", "llimb_f", "llimb_b", "llimb_l", "llimb_r"), forb_limb))
    for regions, forb in groups:
        r_lo, r_hi = min(forb) - 1, max(forb)
        for region in regions:
            x0, _y0, x1, _y1 = T.REGIONS[region]
            cols = np.zeros(x1 - x0 + 1, dtype=bool)
            for r in range(r_lo, r_hi + 1):
                a, b = img[r, x0:x1 + 1], img[r + 1, x0:x1 + 1]
                oa, ob = a[:, 3] > 0, b[:, 3] > 0
                if r not in hem:
                    top_edge = oa & ~ob                       # garment ends at row r
                    bot_edge = ~oa & ob                       # garment starts at row r+1
                    cols |= (top_edge & (r in forb)) | (bot_edge & ((r + 1) in forb))
                both = oa & ob
                if both.any() and (r in forb or (r + 1) in forb):
                    d = deltaE2000(srgb_to_lab(a[both, :3].astype(np.float64)), srgb_to_lab(b[both, :3].astype(np.float64)))
                    step = np.zeros_like(both)
                    step[both] = d > 20.0
                    cols |= step
            n = int(cols.sum())
            if n >= LC.SPLIT_FLAG_MIN_COLUMNS:
                flagged.append(f"{region}: {n} px")
                total += n
    return _res("CHK-B05.split_rows", not flagged, "split_row_step_columns", float(total), "SOFT: no trim or print edge within 2 px of rows 170, 418/419, 467",
                "; ".join(flagged[:5]) or "no steps across the split rows (hem rows exempt)")


def check_print_inset(img: np.ndarray, label_map: np.ndarray | None, placements: Sequence[Any] | None, kind: str) -> list[CheckResult]:
    if label_map is None:
        raise NotApplicable("no_label_map")        # soft flags: nothing to flag without knowing where the prints are
    inset = LC.bevel_inset_px()
    wrap_regions = {p.region for p in (placements or []) if getattr(p, "wrap", False)}
    near = []
    hidden = []
    for k in T.REGION_ORDER:
        m = T.crop(label_map, k) == LABEL_PRINT
        if not m.any():
            continue
        h, w = m.shape
        ring = np.ones_like(m)
        ring[inset:h - inset, inset:w - inset] = False
        if k not in wrap_regions and (m & ring).any():
            near.append(f"{k}: {int((m & ring).sum())} px")
        if kind == "pants" and T.PART_OF[k] in T.LIMB_PARTS:
            if T.FACE_OF[k] == "u":
                hidden.append(f"{k}: {int(m.sum())} px (Pants U faces are never sampled)")
            elif T.FACE_OF[k] in ("f", "b", "l", "r"):
                y0 = T.REGIONS[k][1]
                a, b = LC.hidden_leg_rows()
                rows = np.nonzero(m.any(axis=1))[0] + y0
                n = int(((rows >= a) & (rows <= b)).sum())
                if n:
                    hidden.append(f"{k}: rows {a}-{b} ({n} rows)")
    return [_res("CHK-B05.print_inset", not near, "print_px_in_bevel", float(len(near)), f"SOFT: prints >= {inset} px inside region edges (tpl.bevel_inset_px)",
                 "; ".join(near[:5]) or "all print pixels are inside the bevel inset"),
            _res("CHK-B05.hidden_rows", not hidden, "print_in_hidden_rows", float(len(hidden)),
                 f"SOFT: no prints in rows {LC.hidden_leg_rows()[0]}-{LC.hidden_leg_rows()[1]} of Pants legs or on Pants U faces [UNVERIFIED until FM-T1]",
                 "; ".join(hidden[:5]) or "no prints in hidden rows")]


def check_b06(img: np.ndarray) -> CheckResult:
    """Alpha policy (CLO-10): garment alpha is clean. (1) Semi-transparent pixels away from a cut edge are haze that lets the body
    colour ghost through: at most 0.5% of the garment. (2) Transparent areas are FULLY transparent: a semi-transparent pixel with no
    opaque neighbour (a speck or a faint wash over bare skin) is never allowed."""
    from scipy import ndimage as ndi

    a = img[..., 3]
    inside = T.region_label_map() > 0
    semi = (a > 0) & (a < 255) & inside
    transparent = (a == 0)
    box = np.ones((3, 3), bool)
    near_cut = ndi.binary_dilation(transparent, structure=box)
    interior_semi = semi & ~near_cut
    attached = ndi.binary_dilation(a == 255, structure=box)
    specks = semi & ~attached
    garment = int(((a > 0) & inside).sum())
    share = float(interior_semi.sum() / garment) if garment else 0.0
    lim = LC.semi_alpha_share_max()
    ok = share <= lim and not specks.any()
    return _res("CHK-B06", ok, "semi_alpha_share", share, f"<= {lim} of garment pixels (tpl.semi_alpha_share_max); no semi-transparent specks over bare skin",
                f"{int(interior_semi.sum())} semi-transparent interior pixels of {garment}; {int((semi & near_cut).sum())} on cut edges (not counted); "
                f"{int(specks.sum())} specks with no opaque neighbour", "code_alpha_cleanup")


def check_skin_in_clothing(img: np.ndarray, skin: tuple[int, int, int] | None) -> CheckResult:
    if skin is None:
        raise NotApplicable("no_skin_tone")
    from duoskin.imaging.palette import deltaE2000, srgb_to_lab

    inside = (T.region_label_map() > 0) & (img[..., 3] == 255)
    if not inside.any():
        return _res("CHK-B06.skin_in_clothing", True, "skin_like_share", 0.0, "SOFT", "no opaque garment pixels")
    px = img[..., :3][inside]
    packed = (px[:, 0].astype(np.uint32) << 16) | (px[:, 1].astype(np.uint32) << 8) | px[:, 2].astype(np.uint32)
    uniq, _inv, counts = np.unique(packed, return_inverse=True, return_counts=True)
    ur = np.stack([(uniq >> 16) & 255, (uniq >> 8) & 255, uniq & 255], axis=1).astype(np.float64)
    d = deltaE2000(srgb_to_lab(ur), srgb_to_lab(np.array(skin, dtype=np.float64)))
    share = float(counts[d <= LC.skin_in_clothing_de()].sum() / counts.sum())
    return _res("CHK-B06.skin_in_clothing", share < LC.SKIN_SHARE_FLAG, "skin_like_share", share,
                f"SOFT: < {LC.SKIN_SHARE_FLAG:.0%} of garment pixels within dE {LC.skin_in_clothing_de()} of the skin tone (tpl.skin_in_clothing_de)",
                f"{share:.1%} of opaque garment pixels look like skin; bare skin must be alpha 0 so the body colour shows")


def check_waistband_hidden(kind: str, img: np.ndarray, label_map: np.ndarray | None, other: np.ndarray | None) -> CheckResult:
    """CLO-09 (SOFT): a pants waistband (trim on torso rows 170-201) hidden under an opaque shirt."""
    if other is None:
        raise NotApplicable("no_paired_template")
    pants, shirt = (img, other) if kind == "pants" else (other, img)
    lab = label_map if (kind == "pants" and label_map is not None) else None
    hidden = total = 0
    for k in ("torso_f", "torso_b"):
        p, s = T.crop(pants, k), T.crop(shirt, k)
        y0 = T.REGIONS[k][1]
        sl = slice(T.LOWER_TORSO_ROWS[0] - y0, T.LOWER_TORSO_ROWS[1] - y0 + 1)
        band = p[sl, :, 3] > 0
        if lab is not None:
            band &= T.crop(lab, k)[sl] == 4
        total += int(band.sum())
        hidden += int((band & (s[sl, :, 3] == 255)).sum())
    if total == 0:
        raise NotApplicable("no_pants_detail_on_lower_torso")
    share = hidden / total
    return _res("CHK-B06.waistband_hidden", share < LC.WAISTBAND_HIDDEN_SHARE, "waistband_hidden_share", share,
                f"SOFT: < {LC.WAISTBAND_HIDDEN_SHARE:.0%} of the pants waistband under an opaque shirt (shirt covers torso rows 170-201)",
                f"{share:.0%} of the pants detail on torso rows 170-201 is covered by the shirt; draw the belt on the shirt or leave those rows transparent")


# --------------------------------------------------------------------------------------------------------------------
# CHK-B07 layer stack, placement, mirrored art
# --------------------------------------------------------------------------------------------------------------------
def check_b07(stack: Sequence[Mapping[str, Any]] | None, golden_hash: str | None, placements: Sequence[Any] | None,
              label_map: np.ndarray | None) -> CheckResult:
    if stack is None and placements is None and label_map is None:
        raise CheckUnavailable("no layer stack, no placements and no label map: nothing to verify")
    problems: list[str] = []
    detail = []
    if stack is not None:
        last = -1
        for e in stack:
            try:
                i = STAGE_ORDER.index(e["stage"])
            except (KeyError, ValueError):
                problems.append(f"unknown stage {e.get('stage')!r}")
                continue
            if i < last:
                problems.append(f"stage {e['stage']} ran after {STAGE_ORDER[last]} (CLO-15)")
            last = max(last, i)
        h = sha256_of(list(stack))
        detail.append(f"stack hash {h[:12]}")
        if golden_hash is not None and h != golden_hash:
            problems.append(f"layer stack hash {h[:12]} != golden {golden_hash[:12]}")
    for p in placements or []:
        x0, y0, x1, y1 = T.REGIONS[p.region]
        bx0, by0, bx1, by1 = p.box
        if not p.wrap and not (x0 <= bx0 and bx1 <= x1 and y0 <= by0 and by1 <= y1):
            problems.append(f"print {p.part_id} {p.box} leaves {p.region} without wrap (CLO-16)")
    if label_map is not None:
        masks = {k: (T.crop(label_map, k) == LABEL_PRINT) for k in T.REGION_ORDER}
        done: set[frozenset[str]] = set()
        for a, ma in masks.items():
            if int(ma.sum()) < LC.MIRROR_MIN_PX or np.array_equal(ma, ma[:, ::-1]):
                continue                                           # symmetric art cannot be told apart from a mirror
            for b, mb in masks.items():
                if a == b or ma.shape != mb.shape or frozenset((a, b)) in done:
                    continue
                if np.array_equal(ma[:, ::-1], mb):
                    problems.append(f"print mask of {b} is the mirror image of {a} (CLO-12)")
                    done.add(frozenset((a, b)))
    return _res("CHK-B07", not problems, "stack_placement_problems", float(len(problems)),
                "stages in order; hash equals the golden; prints inside their region unless wrap; no mirrored art",
                "; ".join(problems[:5]) or ("; ".join(detail) or "ok"), "regenerate")


# --------------------------------------------------------------------------------------------------------------------
# CHK-B08 orientation self-test (CLO-05 / CLO-06)
# --------------------------------------------------------------------------------------------------------------------
@lru_cache(maxsize=1)
def _orientation_selftest() -> str:
    if list(T.ADJACENCY) != T.derive_adjacency():
        return "stored seams differ from the face frames"
    tex = T.position_texture()
    for s in T.ADJACENCY:
        ea, eb = T.seam_edges(tex, s)
        worst = int(np.abs(ea[:, :3].astype(int) - eb[:, :3].astype(int)).max())
        if worst > 4:
            return f"numbered-edge texture breaks at {s.a}.{s.side_a}|{s.b}.{s.side_b}: max channel difference {worst}"
    for part in T.PARTS:                                           # UP/U front edge at the image bottom, DOWN/D at the image top
        u, d = f"{part}_u", f"{part}_d"
        zu = T.crop(tex, u)[..., 2].astype(int)
        zd = T.crop(tex, d)[..., 2].astype(int)
        if not (zu[-1].mean() > zu[0].mean() + 100 and zd[0].mean() > zd[-1].mean() + 100):
            return f"{part}: cap front edge is not at the image bottom (UP/U) or top (DOWN/D)"
    if not (T.REGIONS["rlimb_f"][0] < T.REGIONS["llimb_f"][0] and T.char_side_to_image_side("right") == "image_left"
            and T.char_side_to_image_side("left") == "image_right"):
        return "character right/left limbs are not mapped to image left/right"
    fx = T.crop(tex, "torso_f")[..., 0].astype(int)
    if not (fx[:, -1].mean() > fx[:, 0].mean() + 100):
        return "FRONT does not run from the character's right (image left) to its left"
    return ""


def check_b08(body_base_present: bool | None) -> CheckResult:
    msg = _orientation_selftest()
    note = "" if body_base_present else " UV bounds vs R15_Block: not_applicable (no_body_base)."
    return _res("CHK-B08", not msg, "orientation_selftest", 0.0 if not msg else 1.0, "numbered-edge texture continuous across all 36 seams; cap and side orientation right",
                (msg or "36 seams continuous; cap edges and R/L limbs correct.") + note)


# --------------------------------------------------------------------------------------------------------------------
# modesty (plan)
# --------------------------------------------------------------------------------------------------------------------
def check_modesty(kind: str, img: np.ndarray, label_map: np.ndarray | None, plan: Mapping[str, Any] | None,
                  ctx: dict[str, Any]) -> list[CheckResult]:
    if plan is None:
        raise NotApplicable("no_plan")
    from duoskin.imaging.palette import de2000_rgb

    skin = _rgb(plan.get("skin_tone"))
    mod = _rgb(plan.get("modesty_colour"))
    out: list[CheckResult] = []
    if mod is None:
        out.append(_res("CHK-MOD01", False, "modesty_layer_present", 0.0, "the plan must carry a modesty colour (both layers on every character, APP_SPEC S16)",
                        "no modesty colour in the plan: skin shows through the garment's transparent areas with nothing under it", "revise_plan"))
    else:
        d = de2000_rgb(mod, skin) if skin is not None else None
        ok = d is None or d >= 10.0
        out.append(_res("CHK-MOD01", ok, "modesty_vs_skin_de", d, ">= 10 from the skin tone (FAILURE_MODES BODY-01, palette rule)",
                        "modesty colour present" + (f", dE {d:.1f} from the skin" if d is not None else "; skin tone unknown, distance not checked"), "revise_plan"))
    if kind == "shirt":
        env = ctx.get("vars") or {}
        torso_end = env.get("torso_end")
        if torso_end is not None and torso_end < T.LOWER_TORSO_ROWS[1] - LC.MIDRIFF_MAX_GAP_PX:
            gap = int(T.LOWER_TORSO_ROWS[1] - torso_end)
            covered = plan.get("bottom_waist") == "high" or plan.get("allow_midriff") is True
            out.append(_res("CHK-MOD01.midriff", covered, "midriff_gap_px", float(gap),
                            f"SOFT: a bare midriff of more than {LC.MIDRIFF_MAX_GAP_PX} px needs a bottom with waist: high (bible §6.2 crop_top, POL-03)",
                            f"the top ends at row {int(torso_end)} leaving {gap} rows above the waist; the bottom is not high-waisted",
                            "revise_plan"))
    return out


# --------------------------------------------------------------------------------------------------------------------
# OCR hook (A_OCR)
# --------------------------------------------------------------------------------------------------------------------
def _ocr_result(img: np.ndarray, hook: Any) -> CheckResult:
    """A_OCR on the template. A custom hook decides; by default the real OCR engine (rapidocr) is used when it is installed.

    Without an engine the check is ``not_applicable`` (``no_ocr_engine``) rather than falling back to the glyph-shape detector:
    a code-composed template has no text source (the compositor starts from a transparent canvas, CLO-18), and dashed stitches
    look like text to a glyph detector. Prints are text-checked on their own tile (A_OCR on the print asset)."""
    from PIL import Image

    im = Image.fromarray(img, "RGBA")
    if callable(hook):
        out = hook(im)
        if isinstance(out, CheckResult):
            return out
        if isinstance(out, (list, tuple)):
            return _res("A_OCR", len(out) == 0, "text_boxes", float(len(out)), "no text on the template (a template label word is a bug, CLO-18)",
                        "found: " + ", ".join(map(str, out[:5])) if out else "no text found", "regenerate")
        return _res("A_OCR", bool(out), "ocr_hook", None, "hook returned a verdict", str(out))
    from duoskin.imaging import ocr

    if ocr.get_engine() is None:
        raise NotApplicable("no_ocr_engine")
    return ocr.check_no_text(im, engine="rapidocr")


# --------------------------------------------------------------------------------------------------------------------
# the entry point
# --------------------------------------------------------------------------------------------------------------------
def validate_template(png: Any, kind: str, label_map: np.ndarray | None = None, recipe: Any = None, *,
                      base_layer: Any = None, placements: Sequence[Any] | None = None,
                      stack: Sequence[Mapping[str, Any]] | None = None, golden_hash: str | None = None,
                      plan: Mapping[str, Any] | None = None, skin: Any = None, other_png: Any = None,
                      ocr_hook: Any = None, body_base_present: bool | None = False) -> list[CheckResult]:
    """Run every classic-clothing rule on one template file and return the ``CheckResult`` list.

    * ``png``: PNG bytes, a path, a PIL image or an RGBA ndarray (bytes give the exact file facts for CHK-B02).
    * ``kind``: ``"shirt"`` or ``"pants"``. Shirt-only: bracelets/gloves; Pants-only: shoes/legwear, hidden leg rows, Pants U faces.
    * ``label_map``: the compositor's uint8 585x559 class map (0 skin, 1 fabric, 2 secondary, 3 print, 4 trim, 5 bracelet/glove,
      6 shoes, 7 legwear); CHK-B05 and CHK-B11's skin rule need it (without it they are ``ran=False``; the SOFT print flags are N/A).
    * ``recipe``: an ``imaging.recipes.Recipe`` or the compositor's ``meta`` dict (``vars``, ``hem_rows`` ...): the hem rows the
      recipe cuts on purpose are exempt from the split-row warning.
    * ``base_layer``: the base fabric layer (``ComposeResult.base_layer_png``) for the strict HARD seam check CHK-B04; without it CHK-B04
      measures the label-1 (plain fabric) seam pixels of the file instead (95th percentile; approximate), and without a label
      map too it is ``ran=False``.
    * ``placements``, ``stack``, ``golden_hash``: the compositor's print placements and layer stack (CHK-B07).
    * ``plan``: ``{"skin_tone", "modesty_colour", "bottom_waist", "allow_midriff"}`` for the modesty rules; ``skin`` an RGB/hex for
      the skin-in-clothing flag (defaults to ``plan["skin_tone"]``); ``other_png`` the paired template for the waistband flag.
    * ``ocr_hook``: ``callable(PIL image) -> CheckResult | list[str]``; None uses ``imaging.ocr`` when rapidocr is installed (else
      A_OCR is ``not_applicable``: ``no_ocr_engine``); ``False`` skips the text check.

    Never raises: a check that cannot run is ``ran=False`` (a failed hard/assert check, fail closed).
    """
    if kind not in ("shirt", "pants"):
        raise ValueError("kind must be 'shirt' or 'pants'")
    try:
        img, data, facts = _load(png)
    except Exception as e:  # noqa: BLE001 - an unreadable file fails every rule closed
        why = f"{type(e).__name__}: {e}"
        return [not_run(cid, _kind(cid), why, fm_ids=list(FM.get(cid, []))) for cid in ("CHK-B01", "CHK-B02", "CHK-B03", "CHK-B11")]  # type: ignore[arg-type]
    sha = hashlib.sha256(np.ascontiguousarray(img).tobytes()).hexdigest()
    try:
        ctx = _recipe_ctx(recipe)
    except Exception:  # noqa: BLE001
        ctx = {}
    if label_map is not None and label_map.shape != (T.HEIGHT, T.WIDTH):
        label_map = None
    skin_rgb = _rgb(skin) if skin is not None else (_rgb(plan.get("skin_tone")) if plan else None)
    base = _as_array(base_layer)
    other = _as_array(other_png)
    shape_ok = img.shape == (T.HEIGHT, T.WIDTH, 4) and img.dtype == np.uint8
    results: list[CheckResult] = []
    results += _guard("CHK-B01", lambda: check_b01(img), sha)
    results += _guard("CHK-B02", lambda: check_b02(img, data, facts), sha)
    if not shape_ok:                                  # nothing else can be evaluated on a wrong-size file
        return results
    results += _guard("CHK-B03", lambda: check_b03(img), sha)

    def b04() -> CheckResult:
        if base is not None:
            return check_b04(base)
        if label_map is None:
            raise CheckUnavailable("no base fabric layer and no label map: the fabric seams cannot be measured")
        return check_b04_from_labels(img, label_map)

    results += _guard("CHK-B04", b04, sha)
    results += _guard("CHK-B04.composite", lambda: check_b04_composite(img), sha)
    results += _guard("CHK-B05", lambda: check_b05(kind, label_map), sha)
    results += _guard("CHK-B05.split_rows", lambda: check_split_rows(img, ctx), sha)
    results += _guard("CHK-B05.print_inset", lambda: check_print_inset(img, label_map, placements, kind), sha)
    results += _guard("CHK-B06", lambda: check_b06(img), sha)
    results += _guard("CHK-B06.skin_in_clothing", lambda: check_skin_in_clothing(img, skin_rgb), sha)
    results += _guard("CHK-B06.waistband_hidden", lambda: check_waistband_hidden(kind, img, label_map, other), sha)
    results += _guard("CHK-B07", lambda: check_b07(stack, golden_hash, placements, label_map), sha)
    results += _guard("CHK-B08", lambda: check_b08(body_base_present), sha)
    results += _guard("CHK-B11", lambda: check_b11(img, label_map, ctx), sha)
    results += _guard("CHK-MOD01", lambda: check_modesty(kind, img, label_map, plan, ctx), sha)
    if ocr_hook is not False:
        results += _guard("A_OCR", lambda: _ocr_result(img, ocr_hook), sha)
    return results


def failures(results: Sequence[CheckResult]) -> list[CheckResult]:
    """The results that block: hard/assert checks that failed or did not run."""
    return [r for r in results if r.kind in ("hard", "assert") and not r.passed]


def warnings(results: Sequence[CheckResult]) -> list[CheckResult]:
    """The SOFT results that ran and did not pass."""
    return [r for r in results if r.kind == "soft" and r.ran and not r.passed]


__all__ = [
    "check_b01",
    "check_b02",
    "check_b03",
    "check_b04",
    "check_b05",
    "check_b06",
    "check_b07",
    "check_b08",
    "check_b11",
    "failures",
    "seam_stats",
    "validate_accessory",
    "validate_template",
    "warnings",
]

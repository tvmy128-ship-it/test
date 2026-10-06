"""``hair.register``: make a Tripo or manual hair mesh fit the real head (APP_SPEC 10.7 and the issue-file FIX; CHK-M21).

Problem: the hair views fed to Tripo come from "I4 hair with head", so the model contains the grey bald-head guide cube. Nothing
removed it, aligned the hair to the head, or scaled it to HairAttachment, and the shipped hair would carry an opaque grey cube
over the face. ``register_hair`` fixes that in four steps:

1. find the cube head (the guide colour #9A9A9A, or the auto-switched colour, is the fiducial; the cube is 1.2 studs);
   without a cube the hair is placed from a bounding-box hint measured on the I4 hair-only mask (``bbox_from_guide_mask``);
2. compute the similarity transform (uniform scale + translation) that puts the cube on the mannequin head in the
   HairAttachment frame;
3. boolean DIFFERENCE with the head box inflated by about 0.02 stud (manifold3d, UVs carried through the cut), then re-check
   that the result is still watertight;
4. CHK-M21: guide-colour texel share <= 0.5% and >= 80% of the head's front face visible in the front render.

Prefer sending hair-ONLY views to Tripo (the I4 hair-only RGBA); the with-head version stays for the Gate 2 tile.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

from duoskin.checks.model import CheckResult, not_run
from duoskin.mesh import boolean
from duoskin.mesh import colour as col
from duoskin.mesh import geometry as geo
from duoskin.mesh import repair as rep
from duoskin.mesh import texture as tx
from duoskin.mesh.decimate import decimate
from duoskin.mesh.types import MeshData, MeshError
from duoskin.render import avatar, raster
from duoskin.roblox import limits

GUIDE_GREY: tuple[int, int, int] = (154, 154, 154)      # #9A9A9A (bible 8.2 guide_bald_head)
GUIDE_PX_PER_STUD = 280.0                               # the guide: 1 stud = 280 px, head 1.2 studs = 336 px
GUIDE_HEAD_TOP_PX = 560.0
GUIDE_HEAD_CX_PX = 512.0


@dataclass
class HairRegisterOptions:
    head_studs: float = 1.2
    guide_rgb: tuple[tuple[int, int, int], ...] = (GUIDE_GREY,)
    guide_tol_de: float = 14.0
    inflate: float | None = None                         # default: hair.head_cut_inflate_stud (0.02)
    tris_target: int = 3600
    z_centre_studs: float = -0.1                         # head-centred z of the hair bbox centre when placed from a hint
    target_bbox: tuple[tuple[float, float, float], tuple[float, float, float]] | None = None   # (lo, hi) head-centred, from the I4 mask
    mannequin: avatar.Mannequin | None = None
    attachment: str = "HairAttachment"


@dataclass
class HairRegisterResult:
    mesh: MeshData
    facts: dict[str, Any]
    checks: list[CheckResult] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)


def bbox_from_guide_mask(mask: np.ndarray, *, px_per_stud: float = GUIDE_PX_PER_STUD, head_top_px: float = GUIDE_HEAD_TOP_PX,
                         head_cx_px: float = GUIDE_HEAD_CX_PX, head_studs: float = 1.2) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    """Hair bounding box in the HEAD-CENTRED frame (studs, y up) from the I4 hair-only mask drawn at guide scale.

    x and y only (the front view carries no depth): z is returned as 0 for both ends and is chosen by ``z_centre_studs``.
    """
    ys, xs = np.nonzero(mask)
    if len(ys) == 0:
        raise MeshError("empty_mask", "the hair-only mask is empty")
    x0, x1 = (xs.min() - head_cx_px) / px_per_stud, (xs.max() + 1 - head_cx_px) / px_per_stud
    top = (head_top_px - ys.min()) / px_per_stud + head_studs / 2          # above the head centre (the head top is half a head up)
    bottom = (head_top_px - (ys.max() + 1)) / px_per_stud + head_studs / 2
    return (float(x0), float(bottom), 0.0), (float(x1), float(top), 0.0)


# --------------------------------------------------------------------------------------------------------------------
# finding the guide cube
# --------------------------------------------------------------------------------------------------------------------
def _texture_rgb(mesh: MeshData) -> np.ndarray | None:
    if mesh.texture is None:
        return None
    return tx.to_rgba_array(tx.as_pil(mesh.texture))[..., :3]


def guide_mask_texels(tex: np.ndarray, guide_rgb: tuple[tuple[int, int, int], ...], tol_de: float) -> np.ndarray:
    """Texels of ``tex`` within ``tol_de`` dE2000 of any guide colour."""
    lab = col.srgb_to_lab(tex.reshape(-1, 3))
    best = np.full(len(lab), np.inf)
    for g in guide_rgb:
        best = np.minimum(best, col.delta_e2000(lab, col.srgb_to_lab(np.array(g, float))[None, :]))
    return (best <= tol_de).reshape(tex.shape[:2])


def grey_faces(mesh: MeshData, guide_rgb: tuple[tuple[int, int, int], ...], tol_de: float) -> np.ndarray:
    """Faces whose centroid texel is guide-coloured (the cube head, wherever it survives in the mesh)."""
    tex = _texture_rgb(mesh)
    if tex is None or mesh.uv is None or mesh.n_tris == 0:
        return np.zeros(mesh.n_tris, bool)
    texmask = guide_mask_texels(tex, guide_rgb, tol_de)
    h, w = texmask.shape
    uvc = mesh.uv[mesh.faces].mean(axis=1)
    xi = np.clip((uvc[:, 0] * w).astype(int), 0, w - 1)
    yi = np.clip((uvc[:, 1] * h).astype(int), 0, h - 1)
    # all three corners must also be guide-coloured: avoids picking hair triangles that merely touch the grey island
    corners = mesh.uv[mesh.faces]
    ok = texmask[yi, xi]
    for k in range(3):
        cx = np.clip((corners[:, k, 0] * w).astype(int), 0, w - 1)
        cy = np.clip((corners[:, k, 1] * h).astype(int), 0, h - 1)
        ok &= texmask[cy, cx]
    return ok


def find_head_cube(mesh: MeshData, opts: HairRegisterOptions) -> dict[str, Any] | None:
    """Locate the guide cube in ``mesh`` (front = +Z, Y up). Returns ``{lo, hi, width, grey_faces, grey_area_frac}`` or None."""
    gf = grey_faces(mesh, opts.guide_rgb, opts.guide_tol_de)
    if not gf.any():
        return None
    w = geo.weld(mesh.vertices, mesh.faces)
    n, area = geo.face_normals_areas(w.vertices, w.faces)
    idx = np.nonzero(gf)[0]
    sub = w.faces[idx]
    labels, count = geo.shell_labels(sub, len(w.vertices))
    # biggest grey cluster by area
    best, best_area = 0, -1.0
    for k in range(count):
        a = float(area[idx[labels == k]].sum())
        if a > best_area:
            best, best_area = k, a
    cl = idx[labels == best]
    total = float(area.sum())
    if best_area < 0.01 * total or len(cl) < 6:
        return None
    pts = w.vertices[np.unique(w.faces[cl])]
    front = cl[n[cl, 2] > 0.7]
    fpts = w.vertices[np.unique(w.faces[front])] if len(front) >= 2 else pts
    width = float(fpts[:, 0].max() - fpts[:, 0].min())
    if width <= 1e-9:
        return None
    cx = float((fpts[:, 0].max() + fpts[:, 0].min()) / 2)
    z_front = float(pts[:, 2].max())
    y_bottom = float(pts[:, 1].min())
    lo = np.array([cx - width / 2, y_bottom, z_front - width])
    hi = np.array([cx + width / 2, y_bottom + width, z_front])
    return {"lo": lo, "hi": hi, "width": width, "grey_faces": len(cl), "grey_area_frac": best_area / total, "front_faces": len(front)}


# --------------------------------------------------------------------------------------------------------------------
# registration
# --------------------------------------------------------------------------------------------------------------------
def ensure_manifold(mesh: MeshData) -> MeshData:
    """Best-effort watertight clean-up so manifold3d accepts the mesh (the pipeline normally repairs first)."""
    cur, _ = rep.remove_degenerate_and_duplicates(mesh)
    cur, _ = rep.fix_winding_welded(cur)
    cur, _ = rep.remove_nonmanifold(cur)
    cur, _ = rep.fill_holes(cur)
    cur, _ = rep.fix_winding_welded(cur)
    return cur


def _cavity_uv(mesh: MeshData, guide_rgb, tol_de: float) -> tuple[float, float]:
    """A UV whose texel is dark and not guide-coloured: the hair interior colour for faces created by the cut."""
    tex = _texture_rgb(mesh)
    if tex is None or mesh.uv is None:
        return 0.5, 0.5
    h, w = tex.shape[:2]
    texmask = guide_mask_texels(tex, guide_rgb, tol_de)
    uvc = mesh.uv[mesh.faces].mean(axis=1)
    xi = np.clip((uvc[:, 0] * w).astype(int), 0, w - 1)
    yi = np.clip((uvc[:, 1] * h).astype(int), 0, h - 1)
    ok = ~texmask[yi, xi]
    if not ok.any():
        return 0.5, 0.5
    lum = tex[yi, xi].astype(float).mean(axis=1)
    lum[~ok] = np.inf
    k = int(np.argsort(lum)[max(0, int(0.1 * ok.sum()) - 1)])
    return float(np.clip(uvc[k, 0], 0.001, 0.999)), float(np.clip(uvc[k, 1], 0.001, 0.999))


def head_frame(mannequin: avatar.Mannequin, attachment: str, head_studs: float) -> tuple[np.ndarray, np.ndarray]:
    """Head box (lo, hi) in the attachment frame (origin = the attachment point), from the mannequin in use."""
    head = mannequin.head
    centre = np.asarray(head.centre, float) - mannequin.attachment(attachment)
    half = np.asarray(head.size, float) / 2
    if abs(head.size[0] - head_studs) > 1e-6:
        half = np.full(3, head_studs / 2)
    return centre - half, centre + half


def register_hair(mesh: MeshData, opts: HairRegisterOptions | None = None) -> HairRegisterResult:
    """Align a hair mesh to the mannequin head, cut the head out and verify (steps 1-4 above).

    ``mesh`` must already be oriented (front +Z, Y up) and repaired to a closed surface. The result is in the
    HairAttachment frame with ``meta['attachment_offset']`` describing where the attachment point sits (Handle space).
    """
    opts = opts or HairRegisterOptions()
    mq = opts.mannequin or avatar.default_mannequin()
    inflate = float(limits.threshold("hair.head_cut_inflate_stud")) if opts.inflate is None else opts.inflate
    messages: list[str] = []
    facts: dict[str, Any] = {"guide_rgb": [list(g) for g in opts.guide_rgb], "inflate": inflate, "head_studs": opts.head_studs, "attachment": opts.attachment}
    head_lo, head_hi = head_frame(mq, opts.attachment, opts.head_studs)
    head_centre = (head_lo + head_hi) / 2
    cube = find_head_cube(mesh, opts)
    cur = mesh.copy()
    if cube is not None:
        s = opts.head_studs / cube["width"]
        src_centre = (cube["lo"] + cube["hi"]) / 2
        cur.vertices = (mesh.vertices - src_centre) * s + head_centre
        facts.update({"head_found": True, "mode": "fiducial", "scale": s, "grey_faces": cube["grey_faces"], "grey_area_frac": round(cube["grey_area_frac"], 4),
                      "cube_width_mesh_units": cube["width"]})
    elif opts.target_bbox is not None:
        lo_t, hi_t = (np.asarray(opts.target_bbox[0], float), np.asarray(opts.target_bbox[1], float))
        ext = np.maximum(mesh.extents, 1e-9)
        sx, sy = (hi_t[0] - lo_t[0]) / ext[0], (hi_t[1] - lo_t[1]) / ext[1]
        s = float(np.sqrt(sx * sy))
        stretch = float(max(sx, sy) / max(min(sx, sy), 1e-9))
        lo, hi = mesh.bounds
        cur.vertices = (mesh.vertices - np.array([(lo[0] + hi[0]) / 2, hi[1], (lo[2] + hi[2]) / 2])) * s
        cur.vertices = cur.vertices + np.array([(lo_t[0] + hi_t[0]) / 2, hi_t[1], opts.z_centre_studs]) + head_centre
        facts.update({"head_found": False, "mode": "bbox_hint", "scale": s, "bbox_aspect_mismatch": round(stretch, 3)})
        if stretch > 1.15:
            messages.append(f"the model's width/height ratio differs from the approved hair by {stretch - 1:.0%}: check the proportions")
    else:
        raise MeshError("no_alignment", "no guide-grey head was found in the model and no hair bounding box was supplied: cannot place the hair on the head")
    # --- the cut: head box inflated by `inflate`
    cur = ensure_manifold(cur)
    cut_uv = _cavity_uv(cur, opts.guide_rgb, opts.guide_tol_de)
    box = boolean.box_mesh(head_lo - inflate, head_hi + inflate)
    tris_before = cur.n_tris
    try:
        res = boolean.difference(cur, box, cut_uv=cut_uv)
    except MeshError as exc:
        raise MeshError("hair_not_manifold", "the hair surface is not closed, so the head cannot be cut out: " + exc.message, fix_hint="regenerate") from exc
    res.texture = cur.texture
    res.meta = dict(mesh.meta)
    res, island = rep.remove_islands(res, sliver_frac=float(limits.threshold("mesh.sliver_area_frac_max")),
                                     diag_frac=float(limits.threshold("mesh.island_diag_frac_min")))
    res, _ = rep.remove_degenerate_and_duplicates(res)
    if res.n_tris > opts.tris_target:
        res, dec = decimate(res, opts.tris_target)
        res, _ = rep.fix_winding_welded(res)
        facts["decimated_after_cut"] = {"from": dec["tris_before"], "to": dec["tris_after"]}
    res = rep.merge_duplicate_vertices(res)
    topo = rep.topology(res)
    facts.update({"cut": {"tris_before": tris_before, "tris_after": res.n_tris, "removed_islands": island}, "watertight_after_cut": bool(topo["watertight"]),
                  "head_lo": head_lo.round(5).tolist(), "head_hi": head_hi.round(5).tolist()})
    res.meta["attachment_offset"] = [0.0, 0.0, 0.0]
    res = rep.recentre_for_export(res)
    res.meta.update({"attachment": opts.attachment, "asset_type": "Hair",
                     "hair_register": {"guide_rgb": [list(g) for g in opts.guide_rgb], "guide_tol_de": opts.guide_tol_de, "inflate": inflate,
                                       "mode": facts["mode"], "head_studs": opts.head_studs}})
    checks = check_registered(res, res.meta["hair_register"], approved_views=None, mannequin=mq)
    return HairRegisterResult(res, facts, checks, messages)


# --------------------------------------------------------------------------------------------------------------------
# CHK-M21
# --------------------------------------------------------------------------------------------------------------------
def guide_texel_share(mesh: MeshData, guide_rgb: tuple[tuple[int, int, int], ...], tol_de: float) -> float:
    """Share of the UV-covered texels that are guide-coloured."""
    tex = _texture_rgb(mesh)
    if tex is None or mesh.uv is None:
        return 0.0
    cov = tx.uv_coverage(mesh.uv, mesh.faces, tex.shape[1], tex.shape[0], grow=0)
    if not cov.any():
        return 0.0
    g = guide_mask_texels(tex, guide_rgb, tol_de)
    return float((g & cov).sum() / cov.sum())


def guide_surface_share(mesh: MeshData, guide_rgb: tuple[tuple[int, int, int], ...], tol_de: float, samples: int = 4000) -> float:
    """Share of the SURFACE (area weighted samples, UV interpolated) whose texel is guide-coloured. Unlike the texel share it also
    sees meshes whose UV triangles are degenerate."""
    tex = _texture_rgb(mesh)
    if tex is None or mesh.uv is None or mesh.n_tris == 0:
        return 0.0
    rng = np.random.default_rng(23)
    _, area = geo.face_normals_areas(mesh.vertices, mesh.faces)
    if area.sum() <= 0:
        return 0.0
    idx = rng.choice(mesh.n_tris, size=samples, p=area / area.sum())
    r1, r2 = rng.random(samples), rng.random(samples)
    s_ = np.sqrt(r1)
    b = np.stack([1 - s_, s_ * (1 - r2), s_ * r2], axis=1)
    uv = np.einsum("sk,skj->sj", b, mesh.uv[mesh.faces[idx]])
    h, w = tex.shape[:2]
    texmask = guide_mask_texels(tex, guide_rgb, tol_de)
    return float(texmask[np.clip((uv[:, 1] * h).astype(int), 0, h - 1), np.clip((uv[:, 0] * w).astype(int), 0, w - 1)].mean())


def front_face_visibility(mesh: MeshData, mannequin: avatar.Mannequin, attachment: str, size: int = 256) -> float:
    """Share of the head's front face still visible (not covered by hair) in the front render (ID pass)."""
    head = mannequin.head
    head_rm = avatar.part_mesh(head, {}, None, object_id_=avatar.OBJECT_IDS["Head"])
    hair_rm = avatar.place_mesh(mannequin, mesh, attachment, name="hair")
    cam = raster.make_camera("front", head.centre, size / 2.2)
    alone = raster.render([head_rm], cam, size, size, ss=1)
    full = raster.render([head_rm, hair_rm], cam, size, size, ss=1)
    total = int(alone.mask.sum())
    if total == 0:
        return 0.0
    return float(((full.ids == avatar.OBJECT_IDS["Head"]) & alone.mask).sum() / total)


def check_registered(mesh: MeshData, meta: dict[str, Any], *, approved_views: dict[str, Any] | None = None,
                     mannequin: avatar.Mannequin | None = None) -> list[CheckResult]:
    """CHK-M21 (HARD): guide-colour share <= 0.5% (the larger of the UV texel share and the surface-sample share) and >= 80% of the
    head's front face visible in the front render."""
    fm = ["HAIR-12", "HAIR-02"]
    try:
        mq = mannequin or avatar.default_mannequin()
        guide = tuple(tuple(g) for g in meta.get("guide_rgb", [GUIDE_GREY])) or (GUIDE_GREY,)
        tol = float(meta.get("guide_tol_de", 14.0))
        share = max(guide_texel_share(mesh, guide, tol), guide_surface_share(mesh, guide, tol))
        att = meta.get("attachment") or mesh.meta.get("attachment") or "HairAttachment"
        vis = front_face_visibility(mesh, mq, att)
        s_max, v_min = float(limits.threshold("hair.guide_texel_share_max")), float(limits.threshold("hair.front_face_visible_min"))
        problems = []
        if share > s_max:
            problems.append(f"{share:.2%} of the texture is still the grey guide colour (max {s_max:.1%}): the head cube was not removed")
        if vis < v_min:
            problems.append(f"only {vis:.0%} of the head's front face is visible in the front render (min {v_min:.0%}): the hair covers the face")
        return [CheckResult(check_id="CHK-M21", fm_ids=fm, kind="hard", passed=not problems, metric="guide_texel_share", value=round(share, 5),
                            threshold=limits.describe("hair.guide_texel_share_max", "<=") + "; " + limits.describe("hair.front_face_visible_min", "front visible >="),
                            evidence="; ".join(problems) or f"guide texels {share:.3%}; head front face {vis:.0%} visible",
                            fix_hint="none" if not problems else "regenerate")]
    except Exception as exc:  # noqa: BLE001
        return [not_run("CHK-M21", "hard", f"{type(exc).__name__}: {exc}", fm_ids=fm)]


# --------------------------------------------------------------------------------------------------------------------
# kit hair (APP_SPEC 10.7 "Kit" route): assemble, adjust, recolour
# --------------------------------------------------------------------------------------------------------------------
BAND_EDGES = (85, 170)          # kit convention: greyscale luminance < 85 shadow, < 170 base, else highlight


@dataclass
class KitHairFit:
    mesh: MeshData
    facts: dict[str, Any]
    checks: list[CheckResult] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)


def hair_palette(palette: dict[str, Any]) -> dict[str, tuple[int, int, int]]:
    """``{"base", "shadow", "highlight"}`` RGB; missing tones are derived from ``base`` in Lab."""
    from duoskin.mesh import primitives

    base = tuple(int(c) for c in palette.get("base", (90, 60, 40)))
    pal = primitives.derive_palette(base, {"base": base, **{k: tuple(int(c) for c in v) for k, v in palette.items() if k in ("shade", "accent")}})
    shadow = tuple(int(c) for c in palette["shadow"]) if "shadow" in palette else pal["shade"]
    highlight = tuple(int(c) for c in palette["highlight"]) if "highlight" in palette else pal["accent"]
    return {"base": base, "shadow": shadow, "highlight": highlight}


def band_index(grey: np.ndarray) -> np.ndarray:
    """0 shadow, 1 base, 2 highlight for each texel of a greyscale band texture (luminance in 0-255)."""
    lum = grey if grey.ndim == 2 else grey[..., :3].astype(float).mean(axis=2)
    return np.where(lum < BAND_EDGES[0], 0, np.where(lum < BAND_EDGES[1], 1, 2))


def recolour_bands(texture: Any, palette: dict[str, Any]):
    """Recolour a greyscale band texture by luminance band with HARD snapped colours (shadow, base, highlight)."""
    from PIL import Image as PILImage

    pal = hair_palette(palette)
    arr = tx.to_rgba_array(tx.as_pil(texture))[..., :3]
    idx = band_index(arr)
    lut = np.array([pal["shadow"], pal["base"], pal["highlight"]], np.uint8)
    return PILImage.fromarray(lut[idx], "RGB")


def check_recolour(texture: Any, palette: dict[str, Any]) -> CheckResult:
    """CHK-M17 (HARD): every band colour of the recoloured texture is within dE2000 <= 5 of its spec colour (base, shadow, highlight)."""
    fm = ["HAIR-10"]
    try:
        pal = hair_palette(palette)
        arr = tx.to_rgba_array(tx.as_pil(texture))[..., :3]
        lab = col.srgb_to_lab(arr.reshape(-1, 3))
        spec = col.srgb_to_lab(np.array([pal["shadow"], pal["base"], pal["highlight"]], float))
        d = np.stack([col.delta_e2000(lab, s[None, :]) for s in spec], axis=1)       # (texels, 3)
        nearest = d.min(axis=1)
        lim = float(limits.threshold("hair.recolour_band_de_max"))
        worst = float(np.percentile(nearest, 99))
        bands_used = sorted({int(i) for i in np.argmin(d, axis=1)[nearest <= lim]})
        ok = worst <= lim
        return CheckResult(check_id="CHK-M17", fm_ids=fm, kind="hard", passed=ok, metric="band_de2000_p99", value=round(worst, 3),
                           threshold=limits.describe("hair.recolour_band_de_max", "<="),
                           evidence=f"99% of texels are within dE {worst:.2f} of a spec colour; bands present: {bands_used}", fix_hint="none" if ok else "code_palette_snap")
    except Exception as exc:  # noqa: BLE001
        return not_run("CHK-M17", "hard", f"{type(exc).__name__}: {exc}", fm_ids=fm)


_ADJUST_SCALE = {"less": 0.8, "same": 1.0, "more": 1.2}
_VOLUME_SCALE = {"less": 0.93, "same": 1.0, "more": 1.08}


def _scale_about(v: np.ndarray, factors: tuple[float, float, float], pivot: np.ndarray) -> np.ndarray:
    return (v - pivot) * np.asarray(factors) + pivot


def fit_kit_hair(*, style_path: str, module_paths: list[str] | tuple[str, ...] = (), palette: dict[str, Any] | None = None,
                 adjustments: list[dict[str, Any]] | None = None, mannequin: avatar.Mannequin | None = None) -> KitHairFit:
    """Assemble kit hair: the style mesh plus fringe/back modules (all authored in the HairAttachment frame, studs, +Z front),
    apply the discrete adjustments that a static mesh supports, recolour the band texture and union everything.

    Supported adjustments (``{"param", "value"}``): ``volume`` (x/z about the head), ``fringe_length`` and ``back_length``
    (module height about its top), ``side_length`` (style height about its top). ``part_side`` and ``clump_size`` need authored
    variants: they are reported in ``facts['unsupported_adjustments']`` and never silently ignored.
    """
    from duoskin.mesh import load

    messages: list[str] = []
    mq = mannequin or avatar.default_mannequin()
    style = load.load_gltf(style_path)
    modules = [load.load_gltf(m) for m in module_paths]
    adj = {a["param"]: a["value"] for a in (adjustments or []) if "param" in a}
    unsupported = [a for a in (adjustments or []) if a.get("param") in ("part_side", "clump_size") and a.get("value") not in ("same", "centre")]
    head_c = np.asarray(mq.head.centre, float) - mq.attachment("HairAttachment")
    vol = _VOLUME_SCALE.get(adj.get("volume", "same"), 1.0)
    parts = []
    kinds = ["style"] + [f"module{i}" for i in range(len(modules))]
    for kind, ld in zip(kinds, [style] + modules, strict=True):
        m = ld.mesh.copy()
        v = m.vertices
        top = np.array([0.0, v[:, 1].max(), 0.0])
        if kind == "style" and "side_length" in adj:
            v = _scale_about(v, (1.0, _ADJUST_SCALE.get(adj["side_length"], 1.0), 1.0), top)
        elif kind.startswith("module"):
            key = "fringe_length" if (v[:, 2].mean() > 0) else "back_length"
            if key in adj:
                v = _scale_about(v, (1.0, _ADJUST_SCALE.get(adj[key], 1.0), 1.0), top)
        if vol != 1.0:
            v = _scale_about(v, (vol, 1.0, vol), head_c)
        m.vertices = v
        parts.append({"v": m.vertices, "f": m.faces, "uv": m.uv, "img": m.texture, "factor": (1.0, 1.0, 1.0, 1.0), "vertex_colours": False, "name": kind})
    merged = load.merge_parts(parts, messages)
    if palette:
        merged.texture = recolour_bands(merged.texture, palette)
    try:
        one = boolean.union_all([MeshData(p_["v"], p_["f"], p_["uv"]) for p_ in _remap_parts(parts, merged)])
        one.texture = merged.texture
        one.meta = dict(merged.meta)
        mesh = one
        united = True
    except MeshError:
        mesh, united = merged, False
        messages.append("the kit parts overlap and could not be fused into one surface; they stay separate shells")
    mesh.meta.update({"attachment_offset": [0.0, 0.0, 0.0], "attachment": "HairAttachment", "asset_type": "Hair"})
    checks = [check_recolour(mesh.texture, palette)] if palette else []
    facts = {"style": str(style_path), "modules": [str(m) for m in module_paths], "adjustments": adjustments or [], "unsupported_adjustments": unsupported,
             "fused": united, "volume_scale": vol}
    if unsupported:
        messages.append("not applied (the kit style needs authored variants): " + ", ".join(f"{a['param']}={a['value']}" for a in unsupported))
    return KitHairFit(mesh, facts, checks, messages)


def _remap_parts(parts: list[dict[str, Any]], merged: MeshData) -> list[dict[str, Any]]:
    """The merged mesh stores all parts back to back: split it again so each part keeps its (atlas-remapped) UVs."""
    out, off = [], 0
    for p_ in parts:
        n_v = len(p_["v"])
        out.append({"v": merged.vertices[off:off + n_v], "f": p_["f"], "uv": merged.uv[off:off + n_v]})
        off += n_v
    return out

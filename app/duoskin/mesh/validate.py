"""Facts and checks for exported accessory files (FAILURE_MODES 5.5, 7.5 CHK-M01 ... M21). Runs in the mesh worker.

The checks run on the EXPORTED file (re-parsed with node transforms baked), never on Tripo metadata or on the in-memory mesh
that produced it. ``compute_facts`` measures; ``duoskin.roblox.mesh_validators`` turns the Roblox-rule facts into
``CheckResult`` objects; this module adds the checks that need renders or context: orientation (M08), view match (M13), the
mannequin (M14), the SOFT group (M18), sticker slabs (M20) and the hair register (M21).
"""
from __future__ import annotations

import itertools
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from duoskin.checks.model import CheckResult, not_applicable, not_run
from duoskin.mesh import colour as col
from duoskin.mesh import geometry as geo
from duoskin.mesh import gltf_io
from duoskin.mesh import orient as orient_mod
from duoskin.mesh import texture as tx
from duoskin.mesh import views as V
from duoskin.mesh.types import MeshData, MeshError
from duoskin.render import raster
from duoskin.render.avatar import Mannequin, default_mannequin
from duoskin.roblox import limits
from duoskin.roblox.mesh_validators import validate_accessory


@dataclass
class ValidateContext:
    asset_type: str = "Hat"
    attachment: str = ""
    target_studs: tuple[float, float, float] | None = None
    approved_views: dict[str, Any] = field(default_factory=dict)
    mannequin: Mannequin | None = None
    hair_mesh: MeshData | None = None          # the chosen hair in the Hair attachment frame (DUO-06 fit check)
    forward_axis: str = "+Z"
    scale_type: str = "Classic"
    code_built: bool = False                   # slabs and primitives: no approved 3D views exist, M08/M13 are not applicable
    expect_slab: bool = False
    expect_hair_register: bool = False
    asset_id: str = ""


# --------------------------------------------------------------------------------------------------------------------
# measurements
# --------------------------------------------------------------------------------------------------------------------
def coplanar_intersections(v: np.ndarray, f: np.ndarray, *, chunk_pairs: int = 150_000) -> int:
    """Pairs of triangles that lie in the same plane and overlap with positive area (touching along an edge does not count)."""
    if len(f) < 2:
        return 0
    n, area = geo.face_normals_areas(v, f)
    diag = max(geo.bbox_diag(v), 1e-12)
    sign = np.where(n[:, [0]] != 0, np.sign(n[:, [0]]), 1.0)
    nz = np.abs(n) > 1e-9
    first = np.argmax(nz, axis=1)
    sign = np.sign(n[np.arange(len(n)), first])
    sign[sign == 0] = 1.0
    nc = n * sign[:, None]
    d = np.einsum("ij,ij->i", nc, v[f[:, 0]])
    key = np.concatenate([np.round(nc / 1e-3), np.round(d / (diag * 1e-4))[:, None]], axis=1).astype(np.int64)
    ok = area > 1e-14
    idx_all = np.nonzero(ok)[0]
    if len(idx_all) < 2:
        return 0
    _, inv = np.unique(key[idx_all], axis=0, return_inverse=True)
    inv = inv.reshape(-1)
    order = np.argsort(inv, kind="stable")
    bounds = np.concatenate([[0], np.nonzero(np.diff(inv[order]))[0] + 1, [len(order)]])
    total = 0
    eps = diag * 1e-7
    for s0, s1 in itertools.pairwise(bounds):
        if s1 - s0 < 2:
            continue
        g = idx_all[order[s0:s1]]
        nrm = nc[g[0]]
        a = np.cross(nrm, [1.0, 0.0, 0.0] if abs(nrm[0]) < 0.9 else [0.0, 1.0, 0.0])
        a /= np.linalg.norm(a)
        b = np.cross(nrm, a)
        tri = v[f[g]]                                   # (g, 3, 3)
        p2 = np.stack([tri @ a, tri @ b], axis=2)       # (g, 3, 2)
        lo, hi = p2.min(axis=1), p2.max(axis=1)
        m = len(g)
        ii, jj = np.triu_indices(m, 1)
        for c0 in range(0, len(ii), chunk_pairs):
            pi, pj = ii[c0:c0 + chunk_pairs], jj[c0:c0 + chunk_pairs]
            cand = np.all((lo[pi] < hi[pj] - eps) & (lo[pj] < hi[pi] - eps), axis=1)
            if not cand.any():
                continue
            pi, pj = pi[cand], pj[cand]
            A, B = p2[pi], p2[pj]                       # (P, 3, 2)
            edges = np.concatenate([np.roll(A, -1, axis=1) - A, np.roll(B, -1, axis=1) - B], axis=1)   # (P, 6, 2)
            axes = np.stack([-edges[..., 1], edges[..., 0]], axis=2)                                  # (P, 6, 2)
            pa = np.einsum("pke,pae->pak", axes, A)
            pb = np.einsum("pke,pae->pak", axes, B)
            sep = (pa.max(axis=1) <= pb.min(axis=1) + eps) | (pb.max(axis=1) <= pa.min(axis=1) + eps)
            total += int((~sep.any(axis=1)).sum())
    return total


def view_coverage(v: np.ndarray, f: np.ndarray, size: int = 192) -> dict[str, float]:
    """Silhouette area over the bounding-box face area in each of the 6 orthographic views."""
    out = {}
    rm = raster.RenderMesh("m", v, f, two_sided=True)
    used = v[np.unique(f)]
    for view in ("front", "back", "left", "right", "top", "bottom"):
        cam = raster.fit_camera(view, used, size, size, margin=0.02)
        pr, pu = used @ cam.right, used @ cam.up
        rect = max((pr.max() - pr.min()) * cam.scale, 1.0) * max((pu.max() - pu.min()) * cam.scale, 1.0)
        mask = raster.silhouette(rm, cam, size, size)
        out[view] = float(min(1.0, mask.sum() / rect))
    return out


def spike_shrink(v: np.ndarray, f: np.ndarray, frac: float = 0.01, samples: int = 5000) -> float:
    """How much the bounding box shrinks when the most extreme ``frac`` of the surface is ignored (thin spikes, floating bits)."""
    pts, _ = geo.sample_surface(v, f, samples, seed=7)
    if len(pts) == 0:
        return 0.0
    k = max(1, round(frac * len(pts)))
    worst = 0.0
    for axis in range(3):
        col_ = np.sort(pts[:, axis])
        full = max(col_[-1] - col_[0], 1e-12)
        for lo, hi in ((col_[k], col_[-1]), (col_[0], col_[-1 - k])):
            worst = max(worst, 1.0 - (hi - lo) / full)
    return float(worst)


def normals_outward_fraction(wv: np.ndarray, wf: np.ndarray, samples: int = 600) -> float:
    """Share of surface samples whose face normal points out of the solid (generalised winding number ray-free test)."""
    st = geo.topology_stats(wf)
    if st["watertight"] and st["winding_consistent"]:
        return 1.0 if geo.signed_volume(wv, wf) > 0 else 0.0
    pts, idx = geo.sample_surface(wv, wf, samples, seed=11)
    if len(pts) == 0:
        return 0.0
    n, _ = geo.face_normals_areas(wv, wf)
    eps = max(geo.bbox_diag(wv) * 1e-3, 1e-9)
    probe = pts + n[idx] * eps
    w = geo.winding_numbers(probe, wv, wf)
    return float((np.abs(w) < 0.5).mean())


def thickness_facts(wv: np.ndarray, wf: np.ndarray) -> dict[str, float | None]:
    st = geo.topology_stats(wf)
    if not (st["watertight"] and st["winding_consistent"]) or geo.signed_volume(wv, wf) <= 0:
        return {"thickness_p5": None, "thickness_min": None, "thin_area_frac": None}
    t = geo.thickness_probe(wv, wf, samples=800, seed=5)
    finite = t[np.isfinite(t)]
    if len(finite) == 0:
        return {"thickness_p5": None, "thickness_min": None, "thin_area_frac": None}
    tmin = float(limits.threshold("mesh.thickness_min"))
    return {"thickness_p5": float(np.percentile(finite, 5)), "thickness_min": float(finite.min()), "thin_area_frac": float((finite < tmin).mean())}


def texture_facts(mesh: MeshData, file_facts: dict[str, Any]) -> dict[str, Any]:
    img = mesh.texture
    if img is None:
        return {"tex_size": None, "tex_mode": None, "tex_min_alpha": None, "tex_std": None}
    img = tx.as_pil(img)
    return {"tex_size": list(img.size), "tex_mode": img.mode, "tex_min_alpha": tx.min_alpha(img), "tex_std": tx.channel_std(img)}


def box_facts(v_att: np.ndarray, extents: np.ndarray, asset_type: str, attachment: str) -> dict[str, Any]:
    """Margins of every vertex (in the attachment frame) to the Classic box faces; positive = inside."""
    box = limits.box_for(asset_type, attachment or None)
    lo, hi = box.lo_hi()
    below, above = v_att - lo, hi - v_att
    margins = np.minimum(below.min(axis=0), above.min(axis=0))
    outside = int((np.any(v_att < lo - 1e-6, axis=1) | np.any(v_att > hi + 1e-6, axis=1)).sum())
    return {"box_margins": margins.round(6).tolist(), "vertices_outside_box": outside, "box": {"size": list(box.size), "offset": list(box.offset_file),
            "attachment": box.attachment}, "bbox_studs": extents.round(6).tolist()}


def compute_facts(path: str | Path, ctx: ValidateContext) -> tuple[dict[str, Any], MeshData | None]:
    """Measure an exported ``.gltf``/``.glb``. Returns ``(facts, mesh)``; ``mesh`` is in the canonical frame (front +Z) or None
    when the file could not be parsed (the facts then carry ``load_error``)."""
    from duoskin.mesh import load

    p = Path(path)
    facts: dict[str, Any] = {}
    try:
        file_facts = gltf_io.gltf_structure_facts(p)
    except Exception as exc:  # noqa: BLE001
        return {"load_error": f"{type(exc).__name__}: {exc}"}, None
    facts.update(file_facts)
    facts["bytes"] = p.stat().st_size
    try:
        loaded = load.load_gltf(p)
    except MeshError as exc:
        facts["load_error"] = exc.message
        return facts, None
    except Exception as exc:  # noqa: BLE001
        facts["load_error"] = f"{type(exc).__name__}: {exc}"
        return facts, None
    mesh = loaded.mesh
    extras = file_facts.get("extras", {}) or {}
    front_axis = extras.get("front", ctx.forward_axis if ctx.forward_axis else "+Z")
    if front_axis not in ("+Z", "Z"):
        mesh.vertices = orient_mod.from_export_frame(mesh.vertices, front_axis)
    facts["front_axis"] = front_axis
    att_off = np.asarray(extras.get("attachment_offset", mesh.meta.get("attachment_offset", [0.0, 0.0, 0.0])), float)
    if front_axis not in ("+Z", "Z"):
        att_off = orient_mod.from_export_frame(att_off[None, :], front_axis)[0]
    mesh.meta["attachment_offset"] = att_off.tolist()
    v, f = mesh.vertices, mesh.faces
    uv = mesh.uv
    facts["uv_in_01"] = bool(uv is not None and uv.min() >= -1e-6 and uv.max() <= 1 + 1e-6)
    facts["uv_sets"] = file_facts["uv_sets"]
    w = geo.weld(v, f)
    st = geo.topology_stats(w.faces)
    labels, nshell = geo.shell_labels(w.faces, len(w.vertices))
    closed = sum(1 for k in range(nshell) if geo.topology_stats(w.faces[labels == k])["watertight"])
    diag_all = geo.bbox_diag(w.vertices)
    micro = 0
    for k in range(nshell):
        idx = np.nonzero(labels == k)[0]
        pts = w.vertices[np.unique(w.faces[idx])]
        if len(idx) < 4 or (nshell > 1 and np.linalg.norm(pts.max(axis=0) - pts.min(axis=0)) < float(limits.threshold("mesh.island_diag_frac_min")) * diag_all):
            micro += 1
    _, areas = geo.face_normals_areas(w.vertices, w.faces)
    diag = geo.bbox_diag(w.vertices)
    facts.update({
        "tris": len(f), "vertices": len(v), "welded_vertices": len(w.vertices), "shells": int(nshell), "closed_shells": int(closed), "micro_shells": int(micro),
        "watertight": bool(st["watertight"]), "boundary_edges": int(st["boundary_edges"]), "nonmanifold_edges": int(st["nonmanifold_edges"]),
        "winding_consistent": bool(st["winding_consistent"]), "zero_area_faces": int((areas <= 1e-12 * max(diag * diag, 1e-30)).sum()),
        "normals_out": normals_outward_fraction(w.vertices, w.faces), "determinant": 1.0, "scale_min": 1.0,
        "surface_area": float(areas.sum()), "centre_offset": float(np.linalg.norm(mesh.bounds.mean(axis=0))),
        "extents": mesh.extents.round(6).tolist(), "bbox_min_extent": float(mesh.extents.min()),
    })
    facts.update(thickness_facts(w.vertices, w.faces))
    facts.update(texture_facts(mesh, file_facts))
    facts.update(box_facts(v - att_off, mesh.extents, ctx.asset_type, ctx.attachment))
    facts["coplanar_intersections"] = coplanar_intersections(w.vertices, w.faces)
    facts["view_coverage"] = {k: round(val, 4) for k, val in view_coverage(v, f).items()}
    facts["spike_shrink"] = round(spike_shrink(v, f), 5)
    facts["attachment_offset"] = att_off.round(6).tolist()
    facts["scale_type"] = ctx.scale_type
    facts["has_color0"] = bool(file_facts["has_color0"])
    return facts, mesh


# --------------------------------------------------------------------------------------------------------------------
# checks that need renders or context
# --------------------------------------------------------------------------------------------------------------------
def check_orientation(mesh: MeshData, ctx: ValidateContext) -> tuple[CheckResult, orient_mod.OrientResult | None]:
    fm = ["MESH-12", "ACC-01"]
    if not ctx.approved_views:
        if ctx.code_built:
            return not_applicable("CHK-M08", "hard", "built by code: the geometry is made in the file frame, there is no model to orient", fm_ids=fm), None
        return not_run("CHK-M08", "hard", "no approved views supplied", fm_ids=fm), None
    try:
        res = orient_mod.search_orientation(mesh, ctx.approved_views)
    except Exception as exc:  # noqa: BLE001
        return not_run("CHK-M08", "hard", f"orientation search failed: {type(exc).__name__}: {exc}", fm_ids=fm), None
    problems = []
    if res.iou < res.min_iou:
        problems.append(f"best of 24 rotations matches the approved views at IoU {res.iou:.2f} (< {res.min_iou})")
    if res.mirrored:
        problems.append("mirrored model: check the Left/Right slots (the mirrored match is better by "
                        f"{res.mirror_iou - res.iou:.2f}); not flipped automatically")
    if res.identity_iou < res.iou - 0.01:
        problems.append(f"the model is rotated: rotation {res.rotation_index} fits better than the stored orientation")
    ok = not problems
    return CheckResult(check_id="CHK-M08", fm_ids=fm, kind="hard", passed=ok, metric="orientation_iou", value=round(res.iou, 4),
                       threshold=limits.describe("mesh.orient_iou_min", ">=") + "; not mirrored (" + limits.describe("acc.mirror_margin", "margin") + ")",
                       evidence="; ".join(problems) or f"orientation ok: IoU {res.iou:.2f}, mirrored match {res.mirror_iou:.2f}"
                                                         + (" (left-right symmetric)" if res.ambiguous else ""),
                       fix_hint="none" if ok else "human"), res


def check_view_match(mesh: MeshData, ctx: ValidateContext) -> CheckResult:
    """CHK-M13 (code part): silhouette IoU per approved view, palette dE and thin-part IoU. The Sonnet yes/no is the judge step's."""
    fm = ["ACC-07", "ACC-08"]
    if not ctx.approved_views:
        if ctx.code_built:
            return not_applicable("CHK-M13", "hard", "built by code: there are no approved multiview images to compare with", fm_ids=fm)
        return not_run("CHK-M13", "hard", "no approved views supplied", fm_ids=fm)
    try:
        masks = V.approved_masks(ctx.approved_views)
        if "front" not in masks:
            return not_run("CHK-M13", "hard", "an approved front view is required", fm_ids=fm)
        v, f = mesh.vertices, mesh.faces
        per = {name: V.silhouette_iou(v, f, name, m, size=256, norm=192) for name, m in masks.items()}
        front_min, view_min = float(limits.threshold("acc.front_iou_min")), float(limits.threshold("acc.view_iou_min"))
        problems = []
        for name, val in per.items():
            need = front_min if name == "front" else view_min
            if val < need:
                problems.append(f"{name} IoU {val:.2f} < {need}")
        # thin parts (front)
        thin_iou = None
        approved_front = V.normalise_mask(masks["front"], 192)
        thin_a = V.thin_mask(approved_front)
        if thin_a.sum() >= 0.005 * approved_front.sum():
            thin_b = V.thin_mask(V.normalise_mask(V.mesh_silhouette(v, f, "front", 256), 192))
            thin_iou = V.iou(thin_a, thin_b)
            if thin_iou < float(limits.threshold("mesh.thin_part_iou_min")):
                problems.append(f"thin-part IoU {thin_iou:.2f} < {limits.threshold('mesh.thin_part_iou_min')}")
        # palette dE (front, unlit albedo)
        pal_de = None
        if mesh.texture is not None and mesh.uv is not None:
            front_img = V.load_view_image(ctx.approved_views[next(k for k in ctx.approved_views if V.canonical_view_name(str(k)) == "front")])
            rgba = np.asarray(front_img.convert("RGBA"))
            fmask = masks["front"] if masks["front"].shape == rgba.shape[:2] else None
            if fmask is not None:
                from scipy import ndimage

                core = ndimage.binary_erosion(fmask, iterations=2)          # drop anti-aliased edge pixels blended with the background
                fmask = core if core.sum() >= 0.2 * fmask.sum() else fmask
            ref = col.dominant_colours(rgba[..., :3], fmask, k=3)
            pts = v[np.unique(f)]
            cam = raster.fit_camera("front", pts, 256, 256, margin=0.05)
            tex = np.asarray(tx.as_pil(mesh.texture).convert("RGB"))
            out = raster.render([raster.RenderMesh("m", v, f, mesh.uv, tex, unlit=True)], cam, 256, 256, ss=1)
            mine = col.dominant_colours(out.beauty[..., :3], out.mask, k=5)
            if ref and mine:
                worst = 0.0
                ref_lab = col.srgb_to_lab(np.array([c for c, share in ref if share >= 0.05] or [ref[0][0]]))
                mine_lab = col.srgb_to_lab(np.array([c for c, _ in mine]))
                for rl in ref_lab:
                    worst = max(worst, float(col.delta_e2000(rl[None, :], mine_lab).min()))
                pal_de = worst
                lim = float(limits.threshold("acc.view_palette_de_max"))
                if pal_de > lim:
                    problems.append(f"palette dE2000 {pal_de:.1f} > {lim}")
        ev = "; ".join(problems) or ("IoU " + ", ".join(f"{k} {x:.2f}" for k, x in per.items())
                                      + (f"; thin {thin_iou:.2f}" if thin_iou is not None else "") + (f"; palette dE {pal_de:.1f}" if pal_de is not None else ""))
        return CheckResult(check_id="CHK-M13", fm_ids=fm, kind="hard", passed=not problems, metric="front_iou", value=round(per["front"], 4),
                           threshold=limits.describe("acc.front_iou_min", "front >=") + "; " + limits.describe("acc.view_iou_min", "others >="), evidence=ev,
                           fix_hint="none" if not problems else "regenerate")
    except Exception as exc:  # noqa: BLE001
        return not_run("CHK-M13", "hard", f"{type(exc).__name__}: {exc}", fm_ids=fm)


def _box_distance(points: np.ndarray, lo: np.ndarray, hi: np.ndarray) -> np.ndarray:
    d = np.maximum(np.maximum(lo - points, points - hi), 0.0)
    return np.linalg.norm(d, axis=1)


def _penetration(points: np.ndarray, lo: np.ndarray, hi: np.ndarray) -> np.ndarray:
    inside = np.all((points > lo) & (points < hi), axis=1)
    depth = np.minimum(points - lo, hi - points).min(axis=1)
    return np.where(inside, depth, 0.0)


def on_mannequin_points(mesh: MeshData, mannequin: Mannequin, attachment: str, n_samples: int = 3000) -> np.ndarray:
    """Vertices plus surface samples of ``mesh`` in mannequin space (the attachment point meets the mannequin's)."""
    off = np.asarray(mesh.meta.get("attachment_offset", [0.0, 0.0, 0.0]), float)
    pos = mannequin.attachment(attachment)
    pts, _ = geo.sample_surface(mesh.vertices, mesh.faces, n_samples, seed=13)
    return np.vstack([mesh.vertices, pts]) - off + pos


def hair_zone_skin_share(mannequin: Mannequin, hair: MeshData, attachment: str, size: int = 160) -> dict[str, Any]:
    """Share of the head's hair zone (everything but the lower 55% of the front face) still showing skin in 5 views (HAIR-06)."""
    from duoskin.render import avatar

    head = mannequin.head
    cell = 64
    lab = np.full((cell * 3, cell * 4), 2, np.uint16)
    lab[int(cell * 1 + cell * 0.45):cell * 2, cell:cell * 2] = 1          # lower 55% of the front face = protected
    zone_head = avatar.part_mesh(head, {f: (cx * cell, cy * cell, (cx + 1) * cell, (cy + 1) * cell) for f, (cx, cy) in
                                        {"+Z": (1, 1), "-Z": (3, 1), "+X": (2, 1), "-X": (0, 1), "+Y": (1, 0), "-Y": (1, 2)}.items()},
                                 np.zeros((cell * 3, cell * 4, 3), np.uint8), label_map=lab, object_id_=avatar.OBJECT_IDS["Head"], tex_size=(cell * 4, cell * 3))
    hair_rm = avatar.place_mesh(mannequin, hair, attachment, name="hair")
    zone_total = 0
    skin_visible = 0
    for view in ("front", "left", "right", "back", "top"):
        cam = raster.make_camera(view, head.centre, size / 3.2)
        a = raster.render([zone_head], cam, size, size, ss=1, labels=True)
        b = raster.render([zone_head, hair_rm], cam, size, size, ss=1, labels=False)
        zone = (a.labels == 2) & a.mask
        zone_total += int(zone.sum())
        skin_visible += int((zone & (b.ids == avatar.OBJECT_IDS["Head"])).sum())
    share = skin_visible / zone_total if zone_total else 0.0
    return {"skin_share": share, "zone_px": zone_total, "skin_px": skin_visible}


def check_on_mannequin(mesh: MeshData, ctx: ValidateContext) -> CheckResult:
    """CHK-M14: penetration into the body (and the chosen hair), gap to the surface, skin showing in the hair zone."""
    fm = ["MESH-16", "HAIR-06"]
    try:
        mq = ctx.mannequin or default_mannequin()
        box = limits.box_for(ctx.asset_type, ctx.attachment or None)
        pts = on_mannequin_points(mesh, mq, box.attachment)
        pen, gap = 0.0, 1e9
        for p in mq.parts:
            pen = max(pen, float(_penetration(pts, p.lo, p.hi).max()))
            gap = min(gap, float(_box_distance(pts, p.lo, p.hi).min()))
        problems = []
        pen_max, gap_max = float(limits.threshold("mesh.clip_depth_max")), float(limits.threshold("mesh.gap_max"))
        if pen > pen_max:
            problems.append(f"clips {pen:.3f} stud into the body (max {pen_max})")
        if gap > gap_max:
            problems.append(f"floats {gap:.3f} stud away from the body (max {gap_max})")
        extra = {}
        if ctx.hair_mesh is not None and ctx.asset_type in ("Hat", "Face"):
            hp = on_mannequin_points(ctx.hair_mesh, mq, "HairAttachment", 3000)
            hair = ctx.hair_mesh
            hv = hair.vertices - np.asarray(hair.meta.get("attachment_offset", [0, 0, 0]), float) + mq.attachment("HairAttachment")
            wn = geo.winding_numbers(pts[::max(1, len(pts) // 800)], hv, hair.faces)
            inside = pts[::max(1, len(pts) // 800)][np.abs(wn) > 0.5]
            if len(inside):
                from scipy.spatial import cKDTree

                dist = cKDTree(hp).query(inside)[0]
                extra["hair_penetration"] = float(dist.max())
                if dist.max() > pen_max:
                    problems.append(f"clips {dist.max():.3f} stud into the chosen hair (max {pen_max})")
        if ctx.asset_type == "Hair":
            zs = hair_zone_skin_share(mq, mesh, box.attachment)
            extra["skin_in_hair_zone"] = zs["skin_share"]
            lim = float(limits.threshold("hair.skin_in_zone_max"))
            if zs["skin_share"] > lim:
                problems.append(f"{zs['skin_share']:.1%} of the hair zone shows skin (max {lim:.0%})")
        ev = "; ".join(problems) or f"penetration {pen:.3f}, gap {gap:.3f}" + "".join(f", {k} {v:.3f}" for k, v in extra.items())
        return CheckResult(check_id="CHK-M14", fm_ids=fm, kind="hard", passed=not problems, metric="penetration_stud", value=round(pen, 5),
                           threshold=limits.describe("mesh.clip_depth_max", "<=") + "; " + limits.describe("mesh.gap_max", "gap <="), evidence=ev,
                           fix_hint="none" if not problems else "regenerate")
    except Exception as exc:  # noqa: BLE001
        return not_run("CHK-M14", "hard", f"{type(exc).__name__}: {exc}", fm_ids=fm)


def check_soft_group(mesh: MeshData, facts: dict[str, Any], ctx: ValidateContext) -> CheckResult:
    """CHK-M18 (SOFT): flat-card depth, lighting ramp or metallic look, shading bands vs the approved view, vertex density."""
    fm = ["HAIR-05", "ACC-14", "ACC-16", "MESH-17"]
    problems = []
    ext = np.asarray(facts["extents"], float)
    if ctx.asset_type == "Hair":
        ratio = float(ext[2] / max(ext[0], 1e-9))
        if ratio < float(limits.threshold("hair.depth_ratio_min")):
            problems.append(f"flat card: depth is {ratio:.0%} of the width (route to the kit)")
    if mesh.texture is not None:
        img = tx.as_pil(mesh.texture)
        ramp = tx.luminance_ramp(img)
        if ramp > float(limits.threshold("mesh.lighting_ramp_max")):
            problems.append(f"baked lighting ramp {ramp:.0%} across the texture")
        if ctx.approved_views:
            try:
                front = next(k for k in ctx.approved_views if V.canonical_view_name(str(k)) == "front")
                fimg = V.load_view_image(ctx.approved_views[front]).convert("RGB")
                bands_a = tx.shading_band_count(fimg)
                bands_m = tx.shading_band_count(img)
                if abs(bands_a - bands_m) > int(limits.threshold("mesh.shading_band_diff_max")):
                    problems.append(f"shading bands differ: texture {bands_m} vs approved view {bands_a}")
            except StopIteration:
                pass
    tol = float(limits.threshold("mesh.vertex_weld_stud"))
    keyed = np.round(mesh.vertices / tol).astype(np.int64)
    if mesh.uv is not None:
        keyed = np.concatenate([keyed, np.round(mesh.uv * 1e6).astype(np.int64)], axis=1)
    dup = len(mesh.vertices) - len(np.unique(keyed, axis=0))
    if dup:
        problems.append(f"{dup} near-duplicate vertices closer than {tol} stud (they would be welded)")
    return CheckResult(check_id="CHK-M18", fm_ids=fm, kind="soft", passed=not problems, metric="soft_mesh_warnings", value=float(len(problems)),
                       threshold="depth >= 30% (hair), ramp <= 15%, bands within 1", evidence="; ".join(problems) or "no warnings",
                       fix_hint="none" if not problems else "change_technique")


# --------------------------------------------------------------------------------------------------------------------
# the gate
# --------------------------------------------------------------------------------------------------------------------
def validate_file(path: str | Path, ctx: ValidateContext) -> tuple[dict[str, Any], list[CheckResult]]:
    """CHK-M01 ... M14, M18, M20 and M21 for one exported file. Fails closed: anything that cannot run is ``ran=False``."""
    facts, mesh = compute_facts(path, ctx)
    checks: list[CheckResult] = []
    if mesh is None:
        err = facts.get("load_error", "the file could not be read")
        checks.append(CheckResult(check_id="CHK-M01", fm_ids=["SYS-04", "ACC-12", "MESH-14"], kind="hard", passed=False, metric="load_contract",
                                  threshold="parsed", evidence=str(err), fix_hint="human"))
        return facts, checks
    checks.extend(validate_accessory(facts, ctx.asset_type, ctx.attachment, ctx.scale_type))
    problems = gltf_io.check_gltf_files(path)
    if problems:
        checks.append(CheckResult(check_id="CHK-M01", fm_ids=["MESH-14"], kind="hard", passed=False, metric="uris", threshold="files next to the .gltf",
                                  evidence="; ".join(problems), fix_hint="human"))
    orient_res = None
    c8, orient_res = check_orientation(mesh, ctx)
    checks.append(c8)
    if orient_res is not None:
        facts["orientation"] = orient_res.facts()
        facts["mirrored"] = orient_res.mirrored
    checks.append(check_view_match(mesh, ctx))
    checks.append(check_on_mannequin(mesh, ctx))
    try:
        checks.append(check_soft_group(mesh, facts, ctx))
    except Exception as exc:  # noqa: BLE001
        checks.append(not_run("CHK-M18", "soft", f"{type(exc).__name__}: {exc}"))
    extras = facts.get("extras", {}) or {}
    if ctx.expect_slab or extras.get("kind") in ("sticker_slab", "hair_clip_slab"):
        from duoskin.mesh import slab

        checks.extend(slab.check_slab(mesh, extras.get("slab", {}), approved_views=ctx.approved_views))
    else:
        checks.append(not_applicable("CHK-M20", "hard", "not a sticker slab", fm_ids=["ACC-17", "ACC-18"]))
    if ctx.asset_type == "Hair" and (ctx.expect_hair_register or extras.get("hair_register")):
        from duoskin.mesh import hair

        checks.extend(hair.check_registered(mesh, extras.get("hair_register", {}), approved_views=ctx.approved_views, mannequin=ctx.mannequin))
    else:
        checks.append(not_applicable("CHK-M21", "hard", "not a Tripo or manual hair mesh" if ctx.asset_type != "Hair" else "hair was not registered by hair.register",
                                     fm_ids=["HAIR-12"]))
    return json.loads(json.dumps(facts, default=_json_default)), checks


def _json_default(o: Any) -> Any:
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        return float(o)
    if isinstance(o, np.ndarray):
        return o.tolist()
    if isinstance(o, (np.bool_,)):
        return bool(o)
    return str(o)

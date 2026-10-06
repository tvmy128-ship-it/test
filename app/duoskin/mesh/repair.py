"""Mesh repair for rigid accessories (APP_SPEC 10.9 "Repair steps", bible 15.3). Runs in the mesh worker only.

Order: sanity -> degenerate and duplicate faces -> micro-islands, slivers and internal shells (closed shells such as plush
eyes are kept) -> UV-preserving decimation -> winding, non-manifold edges and holes -> orientation search -> scale to the planned
studs and place on the attachment -> texture (gutter dilation, <= 1024, opaque RGB). Topology is always tested on a position-welded
copy; the mesh itself keeps its UV seam splits (MESH-08).

``pymeshlab`` is optional and is never imported here; the built-in UV-aware decimator (``duoskin.mesh.decimate``) is used and
the result says so in ``RepairResult.degraded``.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from duoskin.checks.model import CheckResult
from duoskin.mesh import colour as col
from duoskin.mesh import geometry as geo
from duoskin.mesh import orient as orient_mod
from duoskin.mesh import texture as tx
from duoskin.mesh.decimate import decimate
from duoskin.mesh.types import MeshData, MeshError
from duoskin.render import raster
from duoskin.roblox import limits


@dataclass
class RepairOptions:
    asset_type: str = "Hat"
    attachment: str = ""
    target_studs: tuple[float, float, float] | None = None
    tris_target: int = 3800
    texture_px: int = 1024
    approved_views: dict[str, Any] = field(default_factory=dict)
    placement: str = "auto"                  # auto | keep | centre | bottom | top | back | front
    anchor_offset: tuple[float, float, float] | None = None
    scale_mode: str = "fit"                  # fit | max_dim | none
    orient: bool = True
    decimator: Callable[[MeshData, int], tuple[MeshData, dict[str, Any]]] | None = None   # e.g. the worker's pymeshlab wrapper
    max_hole_edges: int = 400
    mannequin: Any = None                    # duoskin.render.avatar.Mannequin: enables the snap onto the body surface
    snap_to_body: bool = True


@dataclass
class RepairResult:
    mesh: MeshData
    report: dict[str, Any]
    checks: list[CheckResult] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)
    degraded: list[str] = field(default_factory=list)
    orientation: orient_mod.OrientResult | None = None


# --------------------------------------------------------------------------------------------------------------------
# helpers on seam-split meshes (topology is read through a welded copy)
# --------------------------------------------------------------------------------------------------------------------
def topology(mesh: MeshData) -> dict[str, Any]:
    """Topology facts of the welded copy: tris, shells, boundary/non-manifold edges, watertight, winding, zero-area faces."""
    if mesh.n_tris == 0:
        return {"tris": 0, "shells": 0, "boundary_edges": 0, "nonmanifold_edges": 0, "watertight": False, "winding_consistent": False,
                "zero_area": 0, "closed_shells": 0, "vertices": len(mesh.vertices)}
    w = geo.weld(mesh.vertices, mesh.faces)
    st = geo.topology_stats(w.faces)
    labels, count = geo.shell_labels(w.faces, len(w.vertices))
    closed = 0
    for k in range(count):
        if geo.topology_stats(w.faces[labels == k])["watertight"]:
            closed += 1
    _, area = geo.face_normals_areas(w.vertices, w.faces)
    diag = geo.bbox_diag(w.vertices)
    return {"tris": mesh.n_tris, "vertices": len(mesh.vertices), "welded_vertices": len(w.vertices), "shells": count, "closed_shells": closed,
            "boundary_edges": st["boundary_edges"], "nonmanifold_edges": st["nonmanifold_edges"], "watertight": st["watertight"],
            "winding_consistent": st["winding_consistent"], "zero_area": int((area <= 1e-12 * max(diag * diag, 1e-30)).sum())}


def welded_volume(mesh: MeshData) -> float:
    """Signed volume of the mesh measured on its position-welded copy (positive = outward normals on a closed surface)."""
    w = geo.weld(mesh.vertices, mesh.faces)
    return geo.signed_volume(w.vertices, w.faces)


def _select_faces(mesh: MeshData, keep: np.ndarray) -> MeshData:
    out = MeshData(mesh.vertices, mesh.faces[keep], mesh.uv, mesh.texture, dict(mesh.meta))
    return out.compact()


def fix_winding_welded(mesh: MeshData) -> tuple[MeshData, int]:
    """Consistent, outward winding (decided on the welded copy, applied to the seam-split faces). Returns (mesh, faces flipped)."""
    w = geo.weld(mesh.vertices, mesh.faces)
    _, flipped = geo.orient_outward(w.vertices, w.faces)
    out = mesh.copy()
    out.faces[flipped] = out.faces[flipped][:, ::-1]
    return out, int(flipped.sum())


def remove_degenerate_and_duplicates(mesh: MeshData) -> tuple[MeshData, dict[str, int]]:
    w = geo.weld(mesh.vertices, mesh.faces)
    wf = w.faces
    _, area = geo.face_normals_areas(w.vertices, wf)
    diag = geo.bbox_diag(w.vertices)
    repeated = (wf[:, 0] == wf[:, 1]) | (wf[:, 1] == wf[:, 2]) | (wf[:, 0] == wf[:, 2])
    tiny = area <= 1e-12 * max(diag * diag, 1e-30)
    bad = repeated | tiny
    key = np.sort(wf, axis=1)
    _, first = np.unique(key, axis=0, return_index=True)
    dup = np.ones(len(wf), bool)
    dup[first] = False
    keep = ~(bad | dup)
    stats = {"degenerate": int(bad.sum()), "duplicate": int((dup & ~bad).sum())}
    return (mesh if keep.all() else _select_faces(mesh, keep)), stats


def remove_islands(mesh: MeshData, *, sliver_frac: float, diag_frac: float) -> tuple[MeshData, dict[str, Any]]:
    """Delete micro-shells, open slivers and fully internal closed shells. Closed shells (plush eyes) are kept."""
    w = geo.weld(mesh.vertices, mesh.faces)
    labels, count = geo.shell_labels(w.faces, len(w.vertices))
    info: dict[str, Any] = {"shells_before": count, "removed_micro": 0, "removed_sliver": 0, "removed_internal": 0, "removed_tiny_faces": 0}
    if count <= 1:
        info["shells_after"] = count
        return mesh, info
    shells = geo.shell_info(w.vertices, w.faces, labels, count)
    diag = geo.bbox_diag(w.vertices)
    largest = max(s["area"] for s in shells)
    drop = np.zeros(count, bool)
    for s in shells:
        sdiag = float(np.linalg.norm(s["bbox"][1] - s["bbox"][0]))
        if s["n_faces"] < 4:
            drop[s["label"]] = True
            info["removed_tiny_faces"] += 1
        elif sdiag < diag_frac * diag:
            drop[s["label"]] = True
            info["removed_micro"] += 1
        elif not s["closed"] and s["area"] < sliver_frac * largest:
            drop[s["label"]] = True
            info["removed_sliver"] += 1
    # fully internal closed shells (a closed shell inside another closed shell)
    closed = [s for s in shells if s["closed"] and not drop[s["label"]]]
    for s in closed:
        for o in closed:
            if o is s or drop[o["label"]] or abs(o["volume"]) <= abs(s["volume"]):
                continue
            lo, hi = o["bbox"]
            if not (np.all(s["bbox"][0] >= lo - 1e-9) and np.all(s["bbox"][1] <= hi + 1e-9)):
                continue
            sub_v = np.unique(w.faces[s["faces"]])
            pts = w.vertices[sub_v]
            if len(pts) > 24:
                pts = pts[np.linspace(0, len(pts) - 1, 24).astype(int)]
            wn = geo.winding_numbers(pts, w.vertices, w.faces[o["faces"]])
            inside = np.abs(wn) > 0.5
            if inside.all():
                drop[s["label"]] = True
                info["removed_internal"] += 1
                break
    if not drop.any():
        info["shells_after"] = count
        return mesh, info
    keep = ~drop[labels]
    out = _select_faces(mesh, keep)
    info["shells_after"] = int(count - drop.sum())
    return out, info


def remove_nonmanifold(mesh: MeshData) -> tuple[MeshData, int]:
    """Drop the smallest faces on every edge shared by more than two faces until each edge has at most two."""
    removed_total = 0
    cur = mesh
    for _ in range(6):
        w = geo.weld(cur.vertices, cur.faces)
        _edges, counts, inverse = geo.edge_table(w.faces)
        bad_edges = np.nonzero(counts > 2)[0]
        if len(bad_edges) == 0:
            break
        _, area = geo.face_normals_areas(w.vertices, w.faces)
        he_face = np.repeat(np.arange(len(w.faces)), 3)
        drop = np.zeros(len(w.faces), bool)
        bad_set = set(bad_edges.tolist())
        order = np.argsort(inverse, kind="stable")
        inv_sorted = inverse[order]
        starts = np.searchsorted(inv_sorted, bad_edges, side="left")
        ends = np.searchsorted(inv_sorted, bad_edges, side="right")
        for e, s0, s1 in zip(bad_edges.tolist(), starts.tolist(), ends.tolist(), strict=True):
            if e not in bad_set:
                continue
            fs = he_face[order[s0:s1]]
            fs = fs[~drop[fs]]
            if len(fs) <= 2:
                continue
            ranked = fs[np.argsort(-area[fs])]
            drop[ranked[2:]] = True
        if not drop.any():
            break
        removed_total += int(drop.sum())
        cur = _select_faces(cur, ~drop)
    return cur, removed_total


def boundary_loops(wfaces: np.ndarray) -> list[list[int]]:
    """Simple closed loops of welded vertex ids along boundary edges, in the direction the existing faces use them.

    A loop that touches itself at a vertex (a pinched hole) is split into simple cycles at the repeated vertex.
    """
    _edges, counts, inverse = geo.edge_table(wfaces)
    he = np.stack([wfaces[:, [0, 1]], wfaces[:, [1, 2]], wfaces[:, [2, 0]]], axis=1).reshape(-1, 2)
    is_b = counts[inverse] == 1
    nxt: dict[int, list[int]] = {}
    for a, b in he[is_b].tolist():
        nxt.setdefault(a, []).append(b)
    loops: list[list[int]] = []
    used: set[tuple[int, int]] = set()
    for a0 in list(nxt):
        for b0 in nxt[a0]:
            if (a0, b0) in used:
                continue
            path = [a0]
            index = {a0: 0}
            cur, nv = a0, b0
            while True:
                used.add((cur, nv))
                if nv in index:
                    k = index[nv]
                    cyc = path[k:]
                    if len(cyc) >= 3:
                        loops.append(cyc)
                    for v in path[k + 1:]:
                        del index[v]
                    path = path[:k + 1]
                    cur = path[-1]
                else:
                    path.append(nv)
                    index[nv] = len(path) - 1
                    cur = nv
                cands = [c for c in nxt.get(cur, []) if (cur, c) not in used]
                if not cands:
                    break
                nv = cands[0]
    return loops


def fill_holes(mesh: MeshData, max_edges: int = 400) -> tuple[MeshData, dict[str, int]]:
    """Close boundary loops with triangles that reuse the loop's own seam-split vertices (so the UV set is untouched)."""
    import manifold3d as m3d

    w = geo.weld(mesh.vertices, mesh.faces)
    loops = boundary_loops(w.faces)
    stats = {"loops": len(loops), "filled": 0, "skipped": 0, "fan_fallback": 0, "faces_added": 0}
    if not loops:
        return mesh, stats
    # split id for each (welded vertex, boundary face): use the first face corner at that vertex that touches a boundary edge
    split_of: dict[int, int] = {}
    _edges, counts, inverse = geo.edge_table(w.faces)
    he_b = (counts[inverse] == 1).reshape(-1, 3)
    for fi in np.nonzero(he_b.any(axis=1))[0]:
        for k in range(3):
            if he_b[fi, k] or he_b[fi, (k - 1) % 3]:
                split_of.setdefault(int(w.faces[fi, k]), int(mesh.faces[fi, k]))
    verts, faces_new, uv_new = [mesh.vertices], [], [mesh.uv] if mesh.uv is not None else None
    n_vert = len(mesh.vertices)
    for loop in loops:
        if len(loop) > max_edges or any(v not in split_of for v in loop):
            stats["skipped"] += 1
            continue
        ids = [split_of[v] for v in loop]
        pts = mesh.vertices[ids]
        rev = ids[::-1]                              # new faces must run against the boundary direction
        rpts = pts[::-1]
        c = rpts.mean(axis=0)
        _u, _s, vt = np.linalg.svd(rpts - c, full_matrices=False)
        ax, ay = vt[0], vt[1]
        xy = np.stack([(rpts - c) @ ax, (rpts - c) @ ay], axis=1)
        area2 = 0.5 * np.sum(xy[:, 0] * np.roll(xy[:, 1], -1) - np.roll(xy[:, 0], -1) * xy[:, 1])
        if area2 < 0:
            xy[:, 0] = -xy[:, 0]
        tris = None
        try:
            tris = np.asarray(m3d.triangulate([xy]), np.int64)
            if len(tris) < len(rev) - 2:
                tris = None
        except Exception:  # noqa: BLE001 - fall back to a fan
            tris = None
        if tris is not None:
            faces_new.append(np.array(rev, np.int64)[tris])
            stats["faces_added"] += len(tris)
        else:
            cid = n_vert
            n_vert += 1
            verts.append(c[None])
            if uv_new is not None:
                uv_new.append(mesh.uv[ids].mean(axis=0)[None])
            m = len(rev)
            fan = np.array([[cid, rev[i], rev[(i + 1) % m]] for i in range(m)], np.int64)
            faces_new.append(fan)
            stats["faces_added"] += m
            stats["fan_fallback"] += 1
        stats["filled"] += 1
    if not faces_new:
        return mesh, stats
    out = MeshData(np.vstack(verts), np.vstack([mesh.faces] + faces_new), None if uv_new is None else np.vstack(uv_new), mesh.texture,
                   dict(mesh.meta))
    return out, stats


def merge_duplicate_vertices(mesh: MeshData) -> MeshData:
    """Merge seam-split vertices that share position AND uv (MESH-17); keeps real seams."""
    if mesh.uv is None:
        w = geo.weld(mesh.vertices, mesh.faces)
        return MeshData(w.vertices, w.faces, None, mesh.texture, dict(mesh.meta)).compact()
    w = geo.weld(mesh.vertices, mesh.faces)
    key = np.concatenate([w.vmap[:, None], np.round(mesh.uv * 1e6).astype(np.int64)], axis=1)
    _, first, inv = np.unique(key, axis=0, return_index=True, return_inverse=True)
    inv = inv.reshape(-1)
    return MeshData(mesh.vertices[first], inv[mesh.faces], mesh.uv[first], mesh.texture, dict(mesh.meta)).compact()


# --------------------------------------------------------------------------------------------------------------------
# scale and placement
# --------------------------------------------------------------------------------------------------------------------
def scale_to_target(mesh: MeshData, target: tuple[float, float, float] | None, box: limits.AccessoryBox, mode: str) -> tuple[MeshData, dict[str, Any]]:
    """Uniform scale to the planned stud box (``fit``: inside it, ``max_dim``: longest planned side), then never larger than the Classic box."""
    ext = np.maximum(mesh.extents, 1e-9)
    info: dict[str, Any] = {"mode": mode, "extents_before": ext.round(5).tolist()}
    s = 1.0
    if mode != "none" and target is not None:
        t = np.asarray(target, float)
        if mode == "max_dim":
            s = float(t.max() / ext.max())
        else:
            s = float(np.min(t / ext))
    clamp = float(np.min(np.asarray(box.size) * 0.995 / (ext * s)))
    if clamp < 1.0:
        s *= clamp
        info["clamped_to_box"] = True
    out = mesh.copy()
    out.vertices = mesh.vertices * s
    info["scale"] = round(s, 6)
    info["extents_after"] = (ext * s).round(5).tolist()
    return out, info


def place_on_attachment(mesh: MeshData, box: limits.AccessoryBox, anchor: str, anchor_offset: tuple[float, float, float]) -> tuple[MeshData, dict[str, Any]]:
    """Translate so the chosen anchor of the bbox sits at the attachment (``keep`` leaves the mesh where it is), then push it
    the shortest way into the Classic box when the extents allow."""
    lo, hi = mesh.bounds
    c = (lo + hi) / 2
    centre_box = np.asarray(box.offset_file, float)
    off = np.asarray(anchor_offset, float)
    info: dict[str, Any] = {"anchor": anchor}
    if anchor == "keep":
        move = np.zeros(3)
    else:
        target_c = centre_box.copy()
        if anchor == "bottom":
            target_c[1] = off[1] + (hi[1] - lo[1]) / 2
        elif anchor == "top":
            target_c[1] = off[1] - (hi[1] - lo[1]) / 2
        elif anchor == "back":
            target_c[2] = off[2] + (hi[2] - lo[2]) / 2
        elif anchor == "front":
            target_c[2] = off[2] - (hi[2] - lo[2]) / 2
        elif anchor != "centre":
            raise MeshError("bad_anchor", f"unknown anchor {anchor!r}")
        target_c[0] += off[0]
        if anchor == "centre":
            target_c += off
        move = target_c - c
    out = mesh.copy()
    out.vertices = mesh.vertices + move
    blo, bhi = box.lo_hi()
    lo2, hi2 = out.bounds
    shift = np.zeros(3)
    for i in range(3):
        if hi2[i] - lo2[i] <= bhi[i] - blo[i] + 1e-9:
            if lo2[i] < blo[i]:
                shift[i] = blo[i] - lo2[i]
            elif hi2[i] > bhi[i]:
                shift[i] = bhi[i] - hi2[i]
    if np.any(shift != 0):
        out.vertices = out.vertices + shift
        info["pushed_into_box"] = shift.round(5).tolist()
    info["moved"] = (move + shift).round(5).tolist()
    return out, info


def _snap_candidates(asset_type: str, attachment: str) -> list[np.ndarray]:
    """Outward directions an item may be pushed along to leave the body, by asset type (unit vectors, attachment frame)."""
    side = -1.0 if attachment.startswith("Right") else 1.0          # the character's right is -X
    if asset_type in ("Hat", "Neck"):
        raw = [(0, 1, 0)]
    elif asset_type == "Shoulder":
        raw = [(np.sin(t) * side, np.cos(t), 0.0) for t in np.radians(np.arange(0, 91, 15))]
    elif asset_type in ("Face", "Front"):
        raw = [(0, 0, 1)]
    elif asset_type == "Back":
        raw = [(0, 0, -1)]
    elif asset_type == "Waist":
        raw = [(0, 0, -1)] if "Back" in attachment else [(0, 0, 1)]
    else:
        raw = []
    return [np.asarray(r, float) / np.linalg.norm(r) for r in raw]


def _push_out(world_pts: np.ndarray, parts: list[Any], d: np.ndarray, tol: float) -> float | None:
    """Smallest shift along ``d`` that takes every sample point out of every body box (iterated, since leaving one box can enter another)."""
    total = 0.0
    nz = np.abs(d) > 1e-9
    for _ in range(8):
        w = world_pts + d * total
        need = 0.0
        for part in parts:
            lo, hi = part.lo, part.hi
            inside = np.all((w > lo + tol) & (w < hi - tol), axis=1)
            if not inside.any():
                continue
            p = w[inside]
            t_axis = np.where(d > 0, (hi - p) / np.where(nz, d, 1.0), (lo - p) / np.where(nz, d, 1.0))
            t_axis = np.where(nz, t_axis, np.inf)
            need = max(need, float(t_axis.min(axis=1).max()))
        if need <= 1e-9:
            return total
        total += need
    return None


def snap_to_body(mesh: MeshData, mannequin: Any, box: limits.AccessoryBox, asset_type: str, max_shift: float = 1.5, depth_ok: float = 0.01) -> tuple[MeshData, dict[str, Any]]:
    """Push the mesh (attachment frame) the shortest way out of the mannequin's body boxes (CHK-M14: penetration <= 0.02 stud).

    Collar and waist attachments sit slightly INSIDE the body, so an item placed on them would otherwise clip; shoulder items
    may leave upward or outward. The shift is reported; the attachment point then sits behind the mesh by that amount.
    """
    cands = _snap_candidates(asset_type, box.attachment)
    info: dict[str, Any] = {"candidates": len(cands)}
    if not cands or mannequin is None:
        return mesh, info
    att = mannequin.attachment(box.attachment)
    pts, _ = geo.sample_surface(mesh.vertices, mesh.faces, 1500, seed=17)
    world = np.vstack([mesh.vertices, pts]) + att
    best: tuple[float, np.ndarray] | None = None
    for d in cands:
        t = _push_out(world, mannequin.parts, d, depth_ok * 0.5)
        if t is not None and (best is None or t < best[0]):
            best = (t, d)
    if best is None or best[0] <= 1e-6:
        return mesh, info
    if best[0] > max_shift:
        info["skipped"] = f"would need to move {best[0]:.2f} stud (> {max_shift})"
        return mesh, info
    out = mesh.copy()
    out.vertices = mesh.vertices + best[1] * best[0]
    info.update({"shift": round(best[0], 5), "direction": best[1].round(4).tolist()})
    # the shift must not push the item out of the Classic box: shrink it about its contact point when it would
    blo, bhi = box.lo_hi()
    lo, hi = out.bounds
    if np.any(lo < blo - 1e-9) or np.any(hi > bhi + 1e-9):
        support = out.vertices @ best[1]
        contact = out.vertices[support <= support.min() + 0.05 * max(support.max() - support.min(), 1e-9)].mean(axis=0)
        rel = out.vertices - contact
        with np.errstate(divide="ignore", invalid="ignore"):
            up = np.where(rel > 1e-9, (bhi - contact) / rel, np.inf)
            dn = np.where(rel < -1e-9, (blo - contact) / rel, np.inf)
        scale = float(min(1.0, up.min(), dn.min()))
        if scale >= 0.85:
            out.vertices = contact + rel * (scale * 0.999)
            info["shrunk_to_fit_box"] = round(scale * 0.999, 5)
        else:
            info["box_violation_after_snap"] = True
    return out, info


def recentre_for_export(mesh: MeshData) -> MeshData:
    """Handle space: the bbox centre becomes the mesh origin and ``meta['attachment_offset']`` is where the attachment point is
    (the Studio importer re-centres anyway; CHK-M11 wants the centre within 1 stud of the origin)."""
    c = mesh.bounds.mean(axis=0)
    prev = np.asarray(mesh.meta.get("attachment_offset", [0.0, 0.0, 0.0]), float)
    out = mesh.copy()
    out.vertices = mesh.vertices - c
    out.meta["attachment_offset"] = (prev - c).round(6).tolist()
    return out


# --------------------------------------------------------------------------------------------------------------------
# fidelity after decimation (CHK-M15)
# --------------------------------------------------------------------------------------------------------------------
def _texture_array(mesh: MeshData) -> np.ndarray | None:
    if mesh.texture is None:
        return None
    return tx.to_rgba_array(tx.as_pil(mesh.texture))[..., :3]


def decimation_fidelity(before: MeshData, after: MeshData, size: int = 192) -> dict[str, Any]:
    """Mean ΔE2000 between textured front/left/top renders of the mesh before and after decimation (same camera)."""
    pts = np.vstack([before.vertices, after.vertices])
    des, covered = [], 0.0
    for view in ("front", "left", "top"):
        cam = raster.fit_camera(view, pts, size, size, margin=0.05)
        a = raster.render([raster.RenderMesh("a", before.vertices, before.faces, before.uv, _texture_array(before), smooth=True)], cam, size, size, ss=1)
        b = raster.render([raster.RenderMesh("b", after.vertices, after.faces, after.uv, _texture_array(after), smooth=True)], cam, size, size, ss=1)
        both = a.mask & b.mask
        covered += float(both.sum() / max(a.mask.sum(), 1))
        if both.any():
            la = col.srgb_to_lab(a.beauty[..., :3][both])
            lb = col.srgb_to_lab(b.beauty[..., :3][both])
            des.append(float(col.delta_e2000(la, lb).mean()))
    return {"mean_de": float(np.mean(des)) if des else 0.0, "views": len(des), "overlap": covered / 3.0}


# --------------------------------------------------------------------------------------------------------------------
# the pipeline
# --------------------------------------------------------------------------------------------------------------------
def repair_mesh(mesh: MeshData, opts: RepairOptions) -> RepairResult:
    """Run every repair step in order and return the repaired mesh in Handle space with a step-by-step report."""
    report: dict[str, Any] = {"steps": []}
    messages: list[str] = []
    degraded: list[str] = []
    checks: list[CheckResult] = []

    def step(name: str, **kw: Any) -> None:
        report["steps"].append({"step": name, **kw})

    if opts.asset_type not in limits.ASSET_TYPES:
        raise MeshError("bad_asset_type", f"unknown asset type {opts.asset_type!r}")
    box = limits.box_for(opts.asset_type, opts.attachment or None)
    cur = mesh.copy()
    # 0. sanity
    finite = np.isfinite(cur.vertices).all(axis=1)
    if not finite.all():
        keep = finite[cur.faces].all(axis=1)
        cur = MeshData(np.nan_to_num(cur.vertices), cur.faces[keep], cur.uv, cur.texture, cur.meta)
    if cur.n_tris == 0:
        raise MeshError("empty", "the file contains no triangles")
    report["before"] = topology(cur)
    step("sanity", nonfinite_vertices=int((~finite).sum()))
    # 3. degenerate and duplicate faces
    cur, st = remove_degenerate_and_duplicates(cur)
    step("degenerate_duplicate", **st)
    # 6. micro-islands, slivers, internal shells (keep closed shells)
    cur, st = remove_islands(cur, sliver_frac=float(limits.threshold("mesh.sliver_area_frac_max")), diag_frac=float(limits.threshold("mesh.island_diag_frac_min")))
    step("islands", **st)
    pre_decimate = cur
    # 4. decimation (texture-aware)
    reserve = max(8, int(0.01 * opts.tris_target))
    dec_report: dict[str, Any] | None = None
    if cur.n_tris > opts.tris_target:
        dec_fn = opts.decimator or decimate
        cur, dec_report = dec_fn(cur, max(opts.tris_target - reserve, 50))
        if opts.decimator is None:
            degraded.append("pymeshlab is not installed: the built-in UV-preserving quadric decimation was used")
        step("decimate", **dec_report)
    # 5. winding, non-manifold edges, holes
    cur, flipped = fix_winding_welded(cur)
    cur, removed_nm = remove_nonmanifold(cur)
    if removed_nm:
        cur, flipped2 = fix_winding_welded(cur)
        flipped += flipped2
    cur, hole_stats = fill_holes(cur, opts.max_hole_edges)
    cur, flipped3 = fix_winding_welded(cur)
    cur, st = remove_degenerate_and_duplicates(cur)
    step("topology", flipped_faces=flipped + flipped3, nonmanifold_removed=removed_nm, holes=hole_stats, **{f"cleanup_{k}": v for k, v in st.items()})
    if cur.n_tris > opts.tris_target:
        cur, dec2 = (opts.decimator or decimate)(cur, opts.tris_target)
        cur, _ = fix_winding_welded(cur)
        step("decimate_again", **dec2)
        dec_report = dec2 if dec_report is None else {**dec_report, "tris_after": dec2["tris_after"], "collapses": dec_report["collapses"] + dec2["collapses"]}
    cur = merge_duplicate_vertices(cur)
    # CHK-M15: UV preserved and the textured render unchanged by decimation
    if dec_report is not None:
        fid = decimation_fidelity(pre_decimate, cur)
        limit = float(limits.threshold("mesh.decimate_de_max"))
        ok = bool(cur.uv is not None and fid["mean_de"] <= limit)
        checks.append(CheckResult(check_id="CHK-M15", fm_ids=["MESH-15"], kind="hard", passed=ok, metric="mean_de2000_after_decimation",
                                  value=round(fid["mean_de"], 4), threshold=limits.describe("mesh.decimate_de_max", "<="),
                                  evidence=f"UV set preserved={cur.uv is not None}; textured-render mean dE2000 {fid['mean_de']:.2f} over {fid['views']} views; "
                                           f"{dec_report['tris_before']} -> {dec_report['tris_after']} triangles by {dec_report['method']}",
                                  fix_hint="none" if ok else "regenerate"))
        report["decimation"] = {**dec_report, "mean_de": round(fid["mean_de"], 4)}
    # 8. orientation
    orientation = None
    if opts.orient and opts.approved_views:
        try:
            orientation = orient_mod.search_orientation(cur, opts.approved_views)
            cur = orient_mod.apply_rotation(cur, orientation.rotation)
            step("orientation", **orientation.facts())
        except ValueError as exc:
            messages.append(f"orientation search skipped: {exc}")
            step("orientation", skipped=str(exc))
    else:
        step("orientation", skipped="no approved views supplied" if not opts.approved_views else "disabled")
    # 9. scale and placement
    cur, sc = scale_to_target(cur, opts.target_studs, box, opts.scale_mode)
    step("scale", **sc)
    anchor = opts.placement if opts.placement != "auto" else limits.default_anchor(opts.asset_type)
    anchor_offset = opts.anchor_offset if opts.anchor_offset is not None else (0.0, 0.0, 0.0)
    cur, pl = place_on_attachment(cur, box, anchor, anchor_offset)
    step("placement", **pl)
    if opts.snap_to_body and opts.mannequin is not None and anchor != "keep":
        cur, sn = snap_to_body(cur, opts.mannequin, box, opts.asset_type)
        step("snap_to_body", **sn)
    cur.meta["attachment_offset"] = [0.0, 0.0, 0.0]
    cur = recentre_for_export(cur)
    cur.meta.update({"attachment": box.attachment, "asset_type": opts.asset_type})
    # 7. texture
    if cur.texture is not None and cur.uv is not None:
        img, trep = tx.prepare_texture(tx.as_pil(cur.texture), cur.uv, cur.faces, opts.texture_px)
        cur.texture = img
        step("texture", **trep, flat=tx.is_flat(img))
    else:
        step("texture", missing=True)
        messages.append("the model has no texture or no UV coordinates: re-export it with its texture")
    report["after"] = topology(cur)
    if not report["after"]["watertight"]:
        messages.append("the surface is still not watertight after repair: try the next seed or re-export from your 3D tool")
    return RepairResult(cur, report, checks, messages, degraded, orientation)

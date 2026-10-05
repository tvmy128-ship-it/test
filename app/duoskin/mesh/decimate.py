"""UV-preserving quadric edge-collapse decimation (APP_SPEC 10.9 step 4; FM MESH-15, CHK-M15).

This is the built-in fallback for ``pymeshlab``'s ``meshing_decimation_quadric_edge_collapse_with_texture``. Geometry-only
simplifiers (fast-simplification, trimesh's quadric call) throw the UVs away, which MESH-15 forbids for textured meshes, so
they are not used here.

How it keeps the texture: topology is the position-welded mesh, but every face corner remembers its own seam-split vertex
(position + UV). An edge ``a -> b`` (remove ``a``, keep ``b`` where it is) is collapsible only when

* the link condition holds (the result stays a 2-manifold);
* no remaining triangle flips in 3D, and none flips or degenerates in UV space;
* every UV island that touches ``a`` also contains a collapsed triangle, so each corner can be re-pointed to an existing
  UV of ``b``. Seam vertices therefore only collapse along seams, so islands never tear and UVs are never invented.

Costs: the sum of plane quadrics (Garland-Heckbert) evaluated at ``b``, boundary-edge penalty planes, plus a small UV
stretch term. Pure Python with a lazy heap: about 100 microseconds per collapse, so run it in the worker.
"""
from __future__ import annotations

import heapq
from typing import Any

import numpy as np
from scipy.spatial import cKDTree

from duoskin.mesh.geometry import bbox_diag, edge_table, face_normals_areas, sample_surface, weld
from duoskin.mesh.types import MeshData, MeshError

MAX_INPUT_TRIS = 400_000


def _quadric_from_plane(n: np.ndarray, d: np.ndarray, w: np.ndarray) -> np.ndarray:
    a, b, c = n[:, 0], n[:, 1], n[:, 2]
    return w[:, None] * np.stack([a * a, a * b, a * c, a * d, b * b, b * c, b * d, c * c, c * d, d * d], axis=1)


def decimate(mesh: MeshData, target_tris: int, *, uv_weight: float = 0.4, max_input_tris: int = MAX_INPUT_TRIS) -> tuple[MeshData, dict[str, Any]]:
    """Reduce ``mesh`` to at most ``target_tris`` triangles keeping its UVs. Returns ``(mesh, report)``.

    ``report`` has ``method``, ``tris_before``, ``tris_after``, ``reached_target``, ``collapses``, ``uv_preserved``
    and ``max_deviation_stud`` (symmetric surface sampling distance to the original).
    """
    n_before = mesh.n_tris
    report: dict[str, Any] = {"method": "builtin_uv_qem", "tris_before": n_before, "tris_after": n_before, "reached_target": True,
                              "collapses": 0, "uv_preserved": mesh.uv is not None}
    if n_before <= target_tris:
        return mesh, report
    if mesh.uv is None:
        raise MeshError("no_uv", "the model has no UV coordinates, so a textured decimation is not possible")
    if n_before > max_input_tris:
        raise MeshError("too_many_triangles", f"the model has {n_before:,} triangles; re-export with a face limit below {max_input_tris:,}",
                        fix_hint="regenerate")
    v_split, f_split, uv_split = mesh.vertices, mesh.faces, mesh.uv
    w = weld(v_split, f_split)
    wv, wf = w.vertices, w.faces
    nv, nf = len(wv), len(wf)
    diag = max(bbox_diag(wv), 1e-12)
    # --- per-corner split vertex identity: (welded vertex, quantised uv) -> split id
    q_uv = np.round(uv_split * 1e7).astype(np.int64)
    corner_key = np.concatenate([w.vmap[:, None], q_uv], axis=1)
    _, first_idx, inverse = np.unique(corner_key, axis=0, return_index=True, return_inverse=True)
    inverse = inverse.reshape(-1)                      # source split vertex -> merged split id
    split_w = w.vmap[first_idx].tolist()
    split_uv = uv_split[first_idx].tolist()
    fc = inverse[f_split].tolist()                     # (M, 3) split ids per corner
    fv = wf.tolist()                                   # (M, 3) welded ids per corner
    # --- quadrics (scaled to unit bounding box so costs are comparable across models)
    scale = 1.0 / diag
    pos = (wv * scale).tolist()
    pv = wv * scale
    normals, areas = face_normals_areas(pv, wf)
    d = -np.einsum("ij,ij->i", normals, pv[wf[:, 0]])
    qf = _quadric_from_plane(normals, d, areas)
    Q = np.zeros((nv, 10))
    for k in range(3):
        np.add.at(Q, wf[:, k], qf)
    # boundary penalty planes
    edges, counts, inv_he = edge_table(wf)
    he_face = np.repeat(np.arange(nf), 3)
    he = np.stack([wf[:, [0, 1]], wf[:, [1, 2]], wf[:, [2, 0]]], axis=1).reshape(-1, 2)
    bmask = counts[inv_he] == 1
    if bmask.any():
        bidx = np.nonzero(bmask)[0]
        p0, p1 = pv[he[bidx, 0]], pv[he[bidx, 1]]
        e = p1 - p0
        nb = np.cross(e, normals[he_face[bidx]])
        ln = np.linalg.norm(nb, axis=1)
        ok = ln > 1e-18
        nb = nb[ok] / ln[ok, None]
        p0, e, bidx = p0[ok], e[ok], bidx[ok]
        db = -np.einsum("ij,ij->i", nb, p0)
        wb = 64.0 * np.einsum("ij,ij->i", e, e)
        qb = _quadric_from_plane(nb, db, wb)
        for k in range(2):
            np.add.at(Q, he[bidx, k], qb)
    Ql = Q.tolist()
    # --- connectivity
    vfaces: list[set[int]] = [set() for _ in range(nv)]
    for fi, (a, b, c) in enumerate(fv):
        vfaces[a].add(fi)
        vfaces[b].add(fi)
        vfaces[c].add(fi)
    alive_f = bytearray(b"\x01") * nf
    alive_v = bytearray(b"\x01") * nv
    ver = [0] * nv
    n_alive = nf

    def qerr(q: list[float], p: list[float]) -> float:
        x, y, z = p
        return (q[0] * x * x + 2 * q[1] * x * y + 2 * q[2] * x * z + 2 * q[3] * x + q[4] * y * y + 2 * q[5] * y * z + 2 * q[6] * y
                + q[7] * z * z + 2 * q[8] * z + q[9])

    def corner_of(fi: int, v: int) -> int:
        row = fv[fi]
        return 0 if row[0] == v else (1 if row[1] == v else 2)

    def tri_normal(pa, pb, pc):
        ux, uy, uz = pb[0] - pa[0], pb[1] - pa[1], pb[2] - pa[2]
        vx, vy, vz = pc[0] - pa[0], pc[1] - pa[1], pc[2] - pa[2]
        return (uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx)

    def uv_area2(c0, c1, c2):
        u0, u1, u2 = split_uv[c0], split_uv[c1], split_uv[c2]
        return (u1[0] - u0[0]) * (u2[1] - u0[1]) - (u2[0] - u0[0]) * (u1[1] - u0[1])

    def evaluate(a: int, b: int):
        """Cost of collapsing a into b, or None when illegal. Returns (cost, cmap)."""
        fa, fb = vfaces[a], vfaces[b]
        dead = fa & fb
        if not 1 <= len(dead) <= 2:
            return None
        opp = set()
        for fi in dead:
            for x in fv[fi]:
                if x != a and x != b:
                    opp.add(x)
        na = {x for fi in fa for x in fv[fi]}
        nb_ = {x for fi in fb for x in fv[fi]}
        na.discard(a)
        na.discard(b)
        nb_.discard(a)
        nb_.discard(b)
        if (na & nb_) != opp:
            return None
        cmap: dict[int, int] = {}
        for fi in dead:
            cmap[fc[fi][corner_of(fi, a)]] = fc[fi][corner_of(fi, b)]
        pb_ = pos[b]
        uv_cost = 0.0
        for fi in fa:
            if fi in dead:
                continue
            row, crow = fv[fi], fc[fi]
            k = corner_of(fi, a)
            old_c = crow[k]
            if old_c not in cmap:
                return None
            ids = [pos[row[0]], pos[row[1]], pos[row[2]]]
            n_old = tri_normal(*ids)
            ids[k] = pb_
            n_new = tri_normal(*ids)
            ln_o = (n_old[0] ** 2 + n_old[1] ** 2 + n_old[2] ** 2) ** 0.5
            ln_n = (n_new[0] ** 2 + n_new[1] ** 2 + n_new[2] ** 2) ** 0.5
            if ln_n < 1e-14 or (ln_o > 0 and (n_old[0] * n_new[0] + n_old[1] * n_new[1] + n_old[2] * n_new[2]) < 0.2 * ln_o * ln_n):
                return None
            new_c = list(crow)
            new_c[k] = cmap[old_c]
            a_old, a_new = uv_area2(*crow), uv_area2(*new_c)
            if a_old * a_new < 0 or (abs(a_old) > 1e-14 and abs(a_new) < 0.05 * abs(a_old)):
                return None
        for ca, cb in cmap.items():
            ua, ub = split_uv[ca], split_uv[cb]
            uv_cost += (ua[0] - ub[0]) ** 2 + (ua[1] - ub[1]) ** 2
        q = [Ql[a][i] + Ql[b][i] for i in range(10)]
        cost = max(qerr(q, pb_), 0.0) + uv_weight * uv_cost * 1e-3
        return cost, cmap

    heap: list[tuple[float, int, int, int, int]] = []
    counter = 0

    def push_edge(a: int, b: int) -> None:
        nonlocal counter
        best = None
        for x, y in ((a, b), (b, a)):
            r = evaluate(x, y)
            if r is not None and (best is None or r[0] < best[0]):
                best = (r[0], x, y)
        if best is not None:
            counter += 1
            heapq.heappush(heap, (best[0], counter, best[1], best[2], ver[best[1]] * 1_000_003 + ver[best[2]]))

    for a, b in edges.tolist():
        push_edge(a, b)

    collapses = 0
    while n_alive > target_tris and heap:
        cost, _, a, b, stamp = heapq.heappop(heap)
        if not alive_v[a] or not alive_v[b] or stamp != ver[a] * 1_000_003 + ver[b]:
            continue
        r = evaluate(a, b)           # neighbours may have changed since the push
        if r is None:
            continue
        cost2, cmap = r
        dead = vfaces[a] & vfaces[b]
        for fi in dead:
            alive_f[fi] = 0
            for x in fv[fi]:
                vfaces[x].discard(fi)
        n_alive -= len(dead)
        moved = list(vfaces[a])
        for fi in moved:
            k = corner_of(fi, a)
            fc[fi][k] = cmap[fc[fi][k]]
            fv[fi][k] = b
            vfaces[b].add(fi)
        vfaces[a].clear()
        alive_v[a] = 0
        Ql[b] = [Ql[a][i] + Ql[b][i] for i in range(10)]
        ver[b] += 1
        nbrs = {x for fi in vfaces[b] for x in fv[fi]}
        nbrs.discard(b)
        for x in nbrs:
            push_edge(b, x)
        collapses += 1
    # --- rebuild seam-split arrays
    keep = [fi for fi in range(nf) if alive_f[fi]]
    corners = np.array([fc[fi] for fi in keep], np.int64).reshape(-1, 3)
    used, new_faces = np.unique(corners, return_inverse=True)
    new_faces = new_faces.reshape(-1, 3)
    split_w_arr = np.array(split_w, np.int64)
    out_v = wv[split_w_arr[used]]
    out_uv = np.array(split_uv, float)[used]
    out = MeshData(out_v, new_faces, out_uv, mesh.texture, dict(mesh.meta))
    report.update({"tris_after": out.n_tris, "reached_target": out.n_tris <= target_tris, "collapses": collapses})
    report["max_deviation_stud"] = round(surface_deviation(mesh, out), 6)
    report["deviation_frac_of_diag"] = round(report["max_deviation_stud"] / diag, 6)
    return out, report


def surface_deviation(a: MeshData, b: MeshData, samples: int = 6000, dense: int = 60000) -> float:
    """Symmetric 99th-percentile surface distance between two meshes (an approximate Hausdorff distance).

    Each mesh is probed with ``samples`` points against a ``dense`` sampling of the other, so the sampling spacing stays far
    below the deviation being measured.
    """
    pa, _ = sample_surface(a.vertices, a.faces, samples, seed=1)
    pb, _ = sample_surface(b.vertices, b.faces, samples, seed=2)
    da, _ = sample_surface(a.vertices, a.faces, dense, seed=3)
    db, _ = sample_surface(b.vertices, b.faces, dense, seed=4)
    if min(len(pa), len(pb), len(da), len(db)) == 0:
        return float("inf")
    d1 = cKDTree(db).query(pa)[0]
    d2 = cKDTree(da).query(pb)[0]
    return float(max(np.percentile(d1, 99), np.percentile(d2, 99)))

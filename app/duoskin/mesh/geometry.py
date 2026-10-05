"""Pure numpy/scipy mesh geometry helpers (no trimesh, no rtree, no embree).

Everything here works on plain arrays so it can run in the mesh worker with only numpy and scipy installed:
welding, edge/topology statistics, shells, signed volume, generalised winding number (inside tests), a vectorised
ray-triangle intersector and a surface thickness probe.
"""
from __future__ import annotations

from typing import NamedTuple

import numpy as np
from scipy import sparse
from scipy.sparse import csgraph
from scipy.spatial import cKDTree


class Welded(NamedTuple):
    vertices: np.ndarray    # (K, 3) merged vertices
    faces: np.ndarray       # (M, 3) faces in the SAME order as the input faces
    vmap: np.ndarray        # (N,) input vertex -> merged vertex


def bbox_diag(vertices: np.ndarray) -> float:
    if len(vertices) == 0:
        return 0.0
    return float(np.linalg.norm(vertices.max(axis=0) - vertices.min(axis=0)))


def weld(vertices: np.ndarray, faces: np.ndarray, tol: float | None = None) -> Welded:
    """Merge vertices closer than ``tol`` (default ``1e-6`` of the bbox diagonal). Face order is preserved.

    Used for topology tests only: UV seams split vertices, and the exported file keeps them split (MESH-08).
    """
    vertices = np.asarray(vertices, float)
    n = len(vertices)
    if n == 0:
        return Welded(vertices.reshape(0, 3), np.asarray(faces, np.int64).reshape(-1, 3), np.zeros(0, np.int64))
    if tol is None:
        tol = max(1e-12, bbox_diag(vertices) * 1e-6)
    tree = cKDTree(vertices)
    pairs = tree.query_pairs(tol, output_type="ndarray")
    if len(pairs) == 0:
        vmap = np.arange(n, dtype=np.int64)
        return Welded(vertices.copy(), np.asarray(faces, np.int64).reshape(-1, 3).copy(), vmap)
    graph = sparse.coo_matrix((np.ones(len(pairs)), (pairs[:, 0], pairs[:, 1])), shape=(n, n))
    _, labels = csgraph.connected_components(graph, directed=False)
    # representative = first vertex of each group (stable)
    order = np.argsort(labels, kind="stable")
    first = np.ones(n, dtype=bool)
    first[1:] = labels[order][1:] != labels[order][:-1]
    reps = order[first]
    new_id = np.empty(labels.max() + 1, dtype=np.int64)
    new_id[labels[reps]] = np.arange(len(reps))
    vmap = new_id[labels]
    wv = vertices[reps]
    return Welded(wv, vmap[np.asarray(faces, np.int64).reshape(-1, 3)], vmap)


def face_normals_areas(v: np.ndarray, f: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Unit normals (zero vector for degenerate faces) and areas."""
    if len(f) == 0:
        return np.zeros((0, 3)), np.zeros(0)
    a, b, c = v[f[:, 0]], v[f[:, 1]], v[f[:, 2]]
    cr = np.cross(b - a, c - a)
    ln = np.linalg.norm(cr, axis=1)
    n = np.zeros_like(cr)
    ok = ln > 1e-300
    n[ok] = cr[ok] / ln[ok, None]
    return n, ln / 2.0


def surface_area(v: np.ndarray, f: np.ndarray) -> float:
    return float(face_normals_areas(v, f)[1].sum())


def signed_volume(v: np.ndarray, f: np.ndarray) -> float:
    if len(f) == 0:
        return 0.0
    a, b, c = v[f[:, 0]], v[f[:, 1]], v[f[:, 2]]
    return float(np.einsum("ij,ij->i", a, np.cross(b, c)).sum() / 6.0)


def edge_table(f: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Undirected edges of a face array.

    Returns ``(edges (E,2) sorted pairs, counts (E,), inverse (3M,))`` where ``inverse`` maps each half-edge
    (face i, local edge j -> index ``3*i + j``) to its edge row.
    """
    f = np.asarray(f, np.int64)
    he = np.stack([f[:, [0, 1]], f[:, [1, 2]], f[:, [2, 0]]], axis=1).reshape(-1, 2)
    se = np.sort(he, axis=1)
    if len(se) == 0:
        return se.reshape(0, 2), np.zeros(0, np.int64), np.zeros(0, np.int64)
    edges, inverse, counts = np.unique(se, axis=0, return_inverse=True, return_counts=True)
    return edges, counts, inverse.reshape(-1)


def topology_stats(f: np.ndarray) -> dict[str, int | bool]:
    """Boundary edges, non-manifold edges and winding consistency of a (welded) face array."""
    edges, counts, inverse = edge_table(f)
    boundary = int((counts == 1).sum())
    nonman = int((counts > 2).sum())
    # winding consistency: every interior edge must be used once in each direction
    consistent = True
    if len(f):
        he = np.stack([f[:, [0, 1]], f[:, [1, 2]], f[:, [2, 0]]], axis=1).reshape(-1, 2)
        forward = he[:, 0] < he[:, 1]
        fwd = np.bincount(inverse, weights=forward.astype(float), minlength=len(edges))
        bwd = np.bincount(inverse, weights=(~forward).astype(float), minlength=len(edges))
        two = counts == 2
        consistent = bool(np.all((fwd[two] == 1) & (bwd[two] == 1)))
        if nonman:
            consistent = False
    return {"edges": int(len(edges)), "boundary_edges": boundary, "nonmanifold_edges": nonman,
            "winding_consistent": consistent, "watertight": bool(boundary == 0 and nonman == 0 and len(f) > 0)}


def shell_labels(f: np.ndarray, n_vertices: int) -> tuple[np.ndarray, int]:
    """Connected components of faces (faces sharing a vertex are connected). Returns ``(label per face, count)``."""
    if len(f) == 0:
        return np.zeros(0, np.int64), 0
    rows = np.concatenate([f[:, 0], f[:, 1], f[:, 2]])
    cols = np.concatenate([f[:, 1], f[:, 2], f[:, 0]])
    g = sparse.coo_matrix((np.ones(len(rows)), (rows, cols)), shape=(n_vertices, n_vertices))
    _, vl = csgraph.connected_components(g, directed=False)
    fl = vl[f[:, 0]]
    uniq, inv = np.unique(fl, return_inverse=True)
    return inv.astype(np.int64), int(len(uniq))


def shell_info(v: np.ndarray, f: np.ndarray, labels: np.ndarray, count: int) -> list[dict]:
    """Per shell: face count, area, closed flag, bbox and signed volume."""
    out = []
    for k in range(count):
        idx = np.nonzero(labels == k)[0]
        sub = f[idx]
        st = topology_stats(sub)
        pts = v[np.unique(sub)]
        out.append({
            "label": k, "faces": idx, "n_faces": int(len(idx)), "area": surface_area(v, sub),
            "closed": bool(st["watertight"]), "bbox": np.stack([pts.min(axis=0), pts.max(axis=0)]),
            "volume": signed_volume(v, sub),
        })
    return out


def winding_numbers(points: np.ndarray, v: np.ndarray, f: np.ndarray, chunk: int = 200_000) -> np.ndarray:
    """Generalised winding number of ``points`` w.r.t. a triangle soup (about 1 inside a closed outward mesh, 0 outside)."""
    points = np.asarray(points, float).reshape(-1, 3)
    if len(f) == 0 or len(points) == 0:
        return np.zeros(len(points))
    a0, b0, c0 = v[f[:, 0]], v[f[:, 1]], v[f[:, 2]]
    m = len(f)
    step = max(1, chunk // max(m, 1))
    out = np.empty(len(points))
    for s in range(0, len(points), step):
        p = points[s:s + step, None, :]
        a, b, c = a0[None] - p, b0[None] - p, c0[None] - p
        la, lb, lc = (np.linalg.norm(x, axis=2) for x in (a, b, c))
        num = np.einsum("pmj,pmj->pm", a, np.cross(b, c))
        den = (la * lb * lc + np.einsum("pmj,pmj->pm", a, b) * lc + np.einsum("pmj,pmj->pm", b, c) * la
               + np.einsum("pmj,pmj->pm", c, a) * lb)
        out[s:s + step] = (2.0 * np.arctan2(num, den)).sum(axis=1) / (4.0 * np.pi)
    return out


def ray_nearest(origins: np.ndarray, dirs: np.ndarray, v: np.ndarray, f: np.ndarray, t_min: float = 1e-9,
                chunk: int = 400_000) -> np.ndarray:
    """Distance to the nearest triangle hit along each ray (``inf`` for a miss). Moller-Trumbore, both faces."""
    origins = np.asarray(origins, float).reshape(-1, 3)
    dirs = np.asarray(dirs, float).reshape(-1, 3)
    n = len(origins)
    best = np.full(n, np.inf)
    if len(f) == 0 or n == 0:
        return best
    v0 = v[f[:, 0]]
    e1 = v[f[:, 1]] - v0
    e2 = v[f[:, 2]] - v0
    m = len(f)
    step = max(1, chunk // m)
    for s in range(0, n, step):
        o = origins[s:s + step, None, :]
        d = dirs[s:s + step, None, :]
        p = np.cross(d, e2[None])                      # (r, m, 3)
        det = np.einsum("mj,rmj->rm", e1, p)
        ok = np.abs(det) > 1e-14
        inv = np.where(ok, 1.0 / np.where(ok, det, 1.0), 0.0)
        tvec = o - v0[None]
        u = np.einsum("rmj,rmj->rm", tvec, p) * inv
        q = np.cross(tvec, e1[None])
        w = np.einsum("rmj,rmj->rm", d, q) * inv
        t = np.einsum("mj,rmj->rm", e2, q) * inv
        hit = ok & (u >= 0) & (w >= 0) & (u + w <= 1.0) & (t > t_min)
        t = np.where(hit, t, np.inf)
        best[s:s + step] = t.min(axis=1)
    return best


def sample_surface(v: np.ndarray, f: np.ndarray, count: int, seed: int = 0) -> tuple[np.ndarray, np.ndarray]:
    """Deterministic area-weighted surface samples: ``(points, face index)``."""
    normals, areas = face_normals_areas(v, f)
    total = areas.sum()
    if len(f) == 0 or total <= 0:
        return np.zeros((0, 3)), np.zeros(0, np.int64)
    rng = np.random.default_rng(seed)
    idx = rng.choice(len(f), size=count, p=areas / total)
    r1, r2 = rng.random(count), rng.random(count)
    s = np.sqrt(r1)
    bu, bv, bw = 1 - s, s * (1 - r2), s * r2
    pts = bu[:, None] * v[f[idx, 0]] + bv[:, None] * v[f[idx, 1]] + bw[:, None] * v[f[idx, 2]]
    return pts, idx


def thickness_probe(v: np.ndarray, f: np.ndarray, samples: int = 600, seed: int = 0) -> np.ndarray:
    """Wall thickness at sampled surface points: the distance from the point to the opposite wall along the inward normal.

    ``inf`` where the inward ray leaves the mesh without a second hit (open meshes). The mesh must be outward wound.
    """
    pts, idx = sample_surface(v, f, samples, seed)
    if len(pts) == 0:
        return np.zeros(0)
    normals, _ = face_normals_areas(v, f)
    n = normals[idx]
    diag = max(bbox_diag(v), 1e-9)
    origins = pts - n * (diag * 1e-6)
    return ray_nearest(origins, -n, v, f, t_min=diag * 1e-7)


def remove_unused(v: np.ndarray, f: np.ndarray, *extra: np.ndarray | None) -> tuple:
    """Renumber vertices after face removal. Returns ``(v, f, *extra_rows_kept)``."""
    if len(f) == 0:
        return (v[:0], f, *[None if e is None else e[:0] for e in extra])
    used = np.unique(f)
    remap = -np.ones(len(v), np.int64)
    remap[used] = np.arange(len(used))
    return (v[used], remap[f], *[None if e is None else e[used] for e in extra])


def rotation_from_axes(perm: tuple[int, int, int], signs: tuple[int, int, int]) -> np.ndarray:
    """Axis-aligned rotation/reflection matrix: row i takes axis ``perm[i]`` times ``signs[i]``."""
    m = np.zeros((3, 3))
    for i in range(3):
        m[i, perm[i]] = signs[i]
    return m


def all_axis_rotations() -> list[np.ndarray]:
    """The 24 proper rotations that map the axes onto themselves, in a fixed order (index 0 = identity)."""
    import itertools

    out = []
    for perm in itertools.permutations(range(3)):
        for signs in itertools.product((1, -1), repeat=3):
            m = rotation_from_axes(perm, signs)
            if np.linalg.det(m) > 0:
                out.append(m)
    # identity first
    ident = next(i for i, m in enumerate(out) if np.allclose(m, np.eye(3)))
    out.insert(0, out.pop(ident))
    return out


def fix_winding(f: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Make the winding of every edge-connected patch consistent (faces joined through manifold edges only).

    Returns ``(faces, flipped)`` where ``flipped`` marks the faces whose vertex order was reversed. The orientation of
    each patch is arbitrary; use :func:`orient_outward` to pick the outward side of closed patches.
    """
    from collections import deque

    f = np.asarray(f, np.int64).reshape(-1, 3)
    m = len(f)
    flipped = np.zeros(m, bool)
    if m == 0:
        return f.copy(), flipped
    edges, counts, inverse = edge_table(f)
    he_face = np.repeat(np.arange(m), 3)
    he = np.stack([f[:, [0, 1]], f[:, [1, 2]], f[:, [2, 0]]], axis=1).reshape(-1, 2)
    forward = he[:, 0] < he[:, 1]
    order = np.argsort(inverse, kind="stable")
    inv_sorted = inverse[order]
    two = counts[inv_sorted] == 2
    idx = order[two].reshape(-1, 2)           # the two half-edges of each manifold edge
    fa, fb = he_face[idx[:, 0]], he_face[idx[:, 1]]
    rel = (forward[idx[:, 0]] == forward[idx[:, 1]])    # same direction => one of them must flip
    adj: list[list[tuple[int, bool]]] = [[] for _ in range(m)]
    for a, b, r in zip(fa.tolist(), fb.tolist(), rel.tolist()):
        if a != b:
            adj[a].append((b, r))
            adj[b].append((a, r))
    seen = np.zeros(m, bool)
    for s in range(m):
        if seen[s]:
            continue
        seen[s] = True
        dq = deque([s])
        while dq:
            a = dq.popleft()
            for b, r in adj[a]:
                if not seen[b]:
                    seen[b] = True
                    flipped[b] = flipped[a] ^ r
                    dq.append(b)
    out = f.copy()
    out[flipped] = out[flipped][:, ::-1]
    return out, flipped


def orient_outward(v: np.ndarray, f: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Fix winding, then reverse every CLOSED shell whose signed volume is negative. Returns ``(faces, flipped)``."""
    f1, flipped = fix_winding(f)
    labels, count = shell_labels(f1, len(v))
    for k in range(count):
        idx = np.nonzero(labels == k)[0]
        sub = f1[idx]
        if topology_stats(sub)["boundary_edges"] == 0 and signed_volume(v, sub) < 0:
            f1[idx] = sub[:, ::-1]
            flipped[idx] = ~flipped[idx]
    return f1, flipped

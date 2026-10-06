"""manifold3d booleans that keep UVs (used by primitives, slabs and ``hair.register``).

A ``MeshData`` is seam-split (several vertices per position when UVs differ). Manifold needs a closed 2-manifold, so the
UV is carried as two extra vertex properties and the duplicated positions are declared equal through the
``merge_from_vert`` / ``merge_to_vert`` vectors. The result keeps the property seams, i.e. it comes back seam-split.
"""
from __future__ import annotations

import numpy as np

from duoskin.mesh.geometry import weld
from duoskin.mesh.types import MeshData, MeshError


def _manifold_module():
    import manifold3d

    return manifold3d


def to_manifold(mesh: MeshData, *, default_uv: tuple[float, float] = (0.5, 0.5)):
    """Build a Manifold from ``mesh``. Raises ``MeshError('not_manifold', ...)`` when the surface is not closed/manifold."""
    m3d = _manifold_module()
    n = len(mesh.vertices)
    uv = mesh.uv if mesh.uv is not None else np.tile(np.array(default_uv, float), (n, 1))
    props = np.concatenate([mesh.vertices, uv], axis=1).astype(np.float32)
    w = weld(mesh.vertices, mesh.faces)
    # vertices that were merged by position: from -> representative (first occurrence of that merged id)
    rep = np.full(len(w.vertices), -1, np.int64)
    for i in range(n - 1, -1, -1):
        rep[w.vmap[i]] = i
    from_v = np.nonzero(rep[w.vmap] != np.arange(n))[0].astype(np.uint32)
    to_v = rep[w.vmap][from_v].astype(np.uint32)
    mm = m3d.Mesh(vert_properties=props, tri_verts=mesh.faces.astype(np.uint32), merge_from_vert=from_v, merge_to_vert=to_v)
    man = m3d.Manifold(mm)
    status = str(man.status())
    if "NoError" not in status or man.is_empty():
        raise MeshError("not_manifold", f"the surface is not a closed manifold ({status}); fill holes and fix normals first",
                        fix_hint="regenerate")
    return man


def from_manifold(man, *, texture=None, meta: dict | None = None) -> MeshData:
    """Back to a seam-split ``MeshData`` (properties 3-4 are the UV)."""
    out = man.to_mesh()
    props = np.asarray(out.vert_properties, np.float64)
    faces = np.asarray(out.tri_verts, np.int64)
    uv = props[:, 3:5] if props.shape[1] >= 5 else None
    return MeshData(props[:, :3], faces, uv, texture, dict(meta or {}))


def _binary(op: str, a: MeshData, b: MeshData, *, b_uv: tuple[float, float] | None = None) -> MeshData:
    ma = to_manifold(a)
    if b.uv is None:
        b = MeshData(b.vertices, b.faces, np.tile(np.array(b_uv or (0.5, 0.5), float), (len(b.vertices), 1)))
    mb = to_manifold(b)
    r = {"union": lambda: ma + mb, "difference": lambda: ma - mb, "intersection": lambda: ma ^ mb}[op]()
    return from_manifold(r, texture=a.texture, meta=a.meta)


def union(a: MeshData, b: MeshData) -> MeshData:
    """Boolean union keeping ``a``'s texture and UVs (``b`` must carry UVs into the same texture)."""
    return _binary("union", a, b)


def difference(a: MeshData, b: MeshData, *, cut_uv: tuple[float, float] | None = None) -> MeshData:
    """``a`` minus ``b``. Faces created on the cut take ``b``'s UV (``cut_uv`` when ``b`` has none)."""
    return _binary("difference", a, b, b_uv=cut_uv)


def union_all(parts: list[MeshData]) -> MeshData:
    if not parts:
        raise ValueError("nothing to union")
    m3d = _manifold_module()
    mans = [to_manifold(p) for p in parts]
    r = m3d.Manifold.batch_boolean(mans, m3d.OpType.Add) if len(mans) > 1 else mans[0]
    return from_manifold(r, texture=parts[0].texture, meta=parts[0].meta)


def box_mesh(lo: np.ndarray, hi: np.ndarray, uv: tuple[float, float] | None = None) -> MeshData:
    """An axis-aligned box as closed, outward wound triangles (one UV for every vertex when ``uv`` is given)."""
    lo, hi = np.asarray(lo, float), np.asarray(hi, float)
    c = np.array([[x, y, z] for x in (lo[0], hi[0]) for y in (lo[1], hi[1]) for z in (lo[2], hi[2])])
    quads = [(0, 1, 3, 2), (4, 6, 7, 5), (0, 4, 5, 1), (2, 3, 7, 6), (0, 2, 6, 4), (1, 5, 7, 3)]
    faces = []
    for a, b, cc, d in quads:
        faces += [[a, b, cc], [a, cc, d]]
    faces = np.array(faces)
    # orient outward
    ctr = c.mean(axis=0)
    for i, f in enumerate(faces):
        n = np.cross(c[f[1]] - c[f[0]], c[f[2]] - c[f[0]])
        if np.dot(n, c[f].mean(axis=0) - ctr) < 0:
            faces[i] = f[[0, 2, 1]]
    return MeshData(c, faces, None if uv is None else np.tile(np.array(uv, float), (8, 1)))

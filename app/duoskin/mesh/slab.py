"""Sticker and hair-clip slabs (APP_SPEC 10.8; FM ACC-17, ACC-18, CHK-M20).

``build_slab`` turns a badge image with an alpha silhouette into a thin watertight accessory: alpha contour (marching squares)
-> manifold3d clean-up and simplification to the triangle budget -> extrusion to at least 0.08 stud with a bevelled rim ->
UVs in ONE opaque 1024 px atlas: the front art, the back on its OWN UV island (un-mirrored art, or a plain colour) and a rim
cell. Stickers never go to Tripo.

Geometry frame: the slab lies in the XY plane, the front faces +Z, the canvas of the art is centred on the origin.
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from PIL import Image
from scipy import ndimage

from duoskin.checks.model import CheckResult, not_run
from duoskin.mesh import colour as col
from duoskin.mesh import geometry as geo
from duoskin.mesh import texture as tx
from duoskin.mesh.types import MeshData, MeshError
from duoskin.render import raster
from duoskin.roblox import limits

ATLAS_PX = 1024


@dataclass
class SlabResult:
    mesh: MeshData
    facts: dict[str, Any]
    messages: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------------------------------------------------
# alpha silhouette -> polygons
# --------------------------------------------------------------------------------------------------------------------
def alpha_mask(art: Image.Image, threshold: int = 128, min_speck: float = 0.0005) -> np.ndarray:
    """Binary silhouette of an RGBA image (alpha >= ``threshold``), tiny specks removed."""
    a = np.asarray(art.convert("RGBA"))[..., 3] >= threshold
    if not a.any():
        raise MeshError("empty_art", "the badge art has no opaque pixels")
    lab, n = ndimage.label(a)
    if n > 1:
        sizes = ndimage.sum(a, lab, range(1, n + 1))
        keep = np.nonzero(sizes >= max(4, min_speck * a.sum()))[0] + 1
        a = np.isin(lab, keep)
    return a


def marching_squares(field_: np.ndarray, level: float = 0.5) -> list[np.ndarray]:
    """Closed iso-contours of a 2D float field as ``(n, 2)`` arrays of ``(x, y)`` (pixel units, y down). The field must be
    padded so that its border is below ``level`` (every contour then closes)."""
    _h, _w = field_.shape
    inside = field_ >= level
    # crossing point on horizontal edges (y, x)-(y, x+1) and vertical edges (y, x)-(y+1, x)
    def pos(k):
        kind, y, x = k
        if kind == "h":
            a, b = field_[y, x], field_[y, x + 1]
            return (x + (level - a) / (b - a), float(y))
        a, b = field_[y, x], field_[y + 1, x]
        return (float(x), y + (level - a) / (b - a))

    segs: dict[tuple, list[tuple]] = {}

    def hkey(y, x):
        return ("h", y, x)

    def vkey(y, x):
        return ("v", y, x)

    def add(a, b):
        segs.setdefault(a, []).append(b)
        segs.setdefault(b, []).append(a)

    ys, xs = np.nonzero(inside[:-1, :-1] | inside[1:, :-1] | inside[:-1, 1:] | inside[1:, 1:])
    for y, x in zip(ys.tolist(), xs.tolist(), strict=True):
        tl, tr, br, bl = inside[y, x], inside[y, x + 1], inside[y + 1, x + 1], inside[y + 1, x]
        case = (tl << 3) | (tr << 2) | (br << 1) | bl
        if case in (0, 15):
            continue
        top, right, bottom, left = hkey(y, x), vkey(y, x + 1), hkey(y + 1, x), vkey(y, x)
        if case in (1, 14):
            add(left, bottom)
        elif case in (2, 13):
            add(bottom, right)
        elif case in (3, 12):
            add(left, right)
        elif case in (4, 11):
            add(top, right)
        elif case in (6, 9):
            add(top, bottom)
        elif case in (7, 8):
            add(left, top)
        elif case in (5, 10):
            centre = field_[y:y + 2, x:x + 2].mean() >= level
            if (case == 5) == centre:
                add(left, top)
                add(bottom, right)
            else:
                add(left, bottom)
                add(top, right)
    loops: list[np.ndarray] = []
    seen: set[tuple] = set()
    for start in list(segs):
        if start in seen:
            continue
        loop = [start]
        seen.add(start)
        prev, cur = None, start
        while True:
            nxt = [n for n in segs[cur] if n != prev] if prev is not None else list(segs[cur])
            nxt = [n for n in nxt if n not in seen or n == start]
            if not nxt:
                break
            n = nxt[0]
            if n == start:
                break
            loop.append(n)
            seen.add(n)
            prev, cur = cur, n
        if len(loop) >= 3:
            loops.append(np.array([pos(k) for k in loop], float))
    return loops


def mask_to_polygons(mask: np.ndarray, work_px: int = 256, sigma: float = 0.8) -> tuple[list[np.ndarray], float]:
    """Contours of ``mask`` in a ``work_px`` raster, in units of the ORIGINAL canvas width (x right, y up, centred, canvas = 1 wide).

    Returns ``(polygons, aspect)`` where ``aspect`` is canvas height / width.
    """
    h, w = mask.shape
    s = work_px / max(h, w)
    nh, nw = max(4, round(h * s)), max(4, round(w * s))
    im = Image.fromarray((mask * 255).astype(np.uint8)).resize((nw, nh), Image.Resampling.BOX)
    f = ndimage.gaussian_filter(np.asarray(im, np.float64) / 255.0, sigma)
    f = np.pad(f, 2, mode="constant")
    loops = marching_squares(f, 0.5)
    polys = []
    for lp in loops:
        x = (lp[:, 0] - 2 + 0.5) / nw           # 0..1 across the canvas width (pixel centres)
        y = (lp[:, 1] - 2 + 0.5) / nw
        polys.append(np.stack([x - 0.5, (nh / nw) / 2 - y], axis=1))
    return polys, h / w


def _cross_section(polys: list[np.ndarray]):
    import manifold3d as m3d

    return m3d.CrossSection([np.asarray(p, float) for p in polys], m3d.FillRule.EvenOdd)


def simplified_polygons(polys: list[np.ndarray], eps: float) -> list[np.ndarray]:
    cs = _cross_section(polys)
    if eps > 0:
        cs = cs.simplify(eps)
    return [np.asarray(p, float) for p in cs.to_polygons() if len(p) >= 3]


def inset_polygon(poly: np.ndarray, b: float, miter_limit: float = 2.5) -> np.ndarray:
    """Move every vertex ``b`` to the material side (left of travel for outer CCW and hole CW polygons) with a mitre."""
    p = np.asarray(poly, float)
    prev, nxt = np.roll(p, 1, axis=0), np.roll(p, -1, axis=0)
    e0, e1 = p - prev, nxt - p
    n0 = np.stack([-e0[:, 1], e0[:, 0]], axis=1)
    n1 = np.stack([-e1[:, 1], e1[:, 0]], axis=1)
    n0 /= np.maximum(np.linalg.norm(n0, axis=1, keepdims=True), 1e-18)
    n1 /= np.maximum(np.linalg.norm(n1, axis=1, keepdims=True), 1e-18)
    m = (n0 + n1) / np.maximum(1.0 + np.einsum("ij,ij->i", n0, n1), 1e-6)[:, None]
    ln = np.linalg.norm(m, axis=1, keepdims=True)
    m = np.where(ln > miter_limit, m / np.maximum(ln, 1e-12) * miter_limit, m)
    return p + m * b


def _valid_inset(polys: list[np.ndarray], insets: list[np.ndarray], b: float) -> bool:
    """The inset outline is acceptable when every polygon keeps its orientation and the enclosed area shrinks sensibly."""
    try:
        for a, c in zip(polys, insets, strict=True):
            sa = 0.5 * np.sum(a[:, 0] * np.roll(a[:, 1], -1) - np.roll(a[:, 0], -1) * a[:, 1])
            sc = 0.5 * np.sum(c[:, 0] * np.roll(c[:, 1], -1) - np.roll(c[:, 0], -1) * c[:, 1])
            if sa * sc <= 0:
                return False
            if (sa > 0 and abs(sc) > abs(sa) * 1.001) or (sa < 0 and abs(sc) < abs(sa) * 0.999):
                return False
            # a short edge at a sharp tip (a diamond or a hexagon with its corners rounded by the tracing) is turned round by the mitres of its two
            # ends: the inset outline then crosses itself at every tip, the cap triangulation overlaps itself and the caps come out reversed
            # (coplanar intersecting triangles and a back island on the front: CHK-M11, CHK-M20). Every edge must keep its direction.
            if bool((np.einsum("ij,ij->i", np.roll(a, -1, axis=0) - a, np.roll(c, -1, axis=0) - c) <= 0.0).any()):
                return False
        base_area = _cross_section(polys).area()
        inset_area = _cross_section(insets).area()
        per = sum(float(np.linalg.norm(np.roll(p, -1, axis=0) - p, axis=1).sum()) for p in polys)
        return 0.0 < inset_area < base_area and inset_area >= 0.6 * (base_area - b * per)
    except Exception:  # noqa: BLE001
        return False


def _strip_faces(r0: np.ndarray, r1: np.ndarray) -> list[list[int]]:
    """Outward quads between two stacked rings of one loop (ring ``r1`` is the +Z one); ring arrays hold K+1 ids (seam duplicate)."""
    faces = []
    for k in range(len(r0) - 1):
        a, b, c, d = r0[k], r0[k + 1], r1[k + 1], r1[k]
        faces += [[a, b, c], [a, c, d]]
    return faces


def build_slab(art: Image.Image, *, size_studs: float, thickness: float = 0.1, bevel: float | None = None, tris_budget: int = 2800,
               back: str = "plain", back_rgb: tuple[int, int, int] | None = None, rim_rgb: tuple[int, int, int] | None = None,
               texture_px: int = ATLAS_PX, kind: str = "sticker_slab", max_area: float = 66.0, alpha_threshold: int = 128) -> SlabResult:
    """Extrude ``art``'s alpha silhouette into a slab.

    ``size_studs`` is the length of the canvas' longer side; ``thickness`` is raised to the validator minimum
    (``slab.thickness_min``, 0.08 stud); ``back`` is ``"plain"`` (own flat island) or ``"same_art"`` (un-mirrored art on the
    back island); the slab is scaled down when its surface area would exceed ``max_area`` stud^2 (CHK-M10).
    """
    import manifold3d as m3d

    messages: list[str] = []
    art = art.convert("RGBA")
    t_min = float(limits.threshold("slab.thickness_min"))
    if thickness < t_min:
        messages.append(f"thickness raised from {thickness} to the validator minimum {t_min} stud")
        thickness = t_min
    bevel = float(limits.threshold("slab.bevel_stud")) if bevel is None else bevel
    bevel = min(bevel, thickness * 0.3)
    mask = alpha_mask(art, alpha_threshold)
    polys0, aspect = mask_to_polygons(mask)
    if not polys0:
        raise MeshError("no_contour", "no outline could be traced from the badge art")
    scale = float(size_studs)                      # polygons are in canvas-width units (width = 1)
    # --- simplify to the triangle budget (caps ~2N + side strips 6N per outline vertex with a bevel)
    eps = 0.0015
    polys = simplified_polygons(polys0, eps)
    for _ in range(14):
        n_vert = sum(len(p) for p in polys)
        if 8 * n_vert <= tris_budget:
            break
        eps *= 1.45
        polys = simplified_polygons(polys0, eps)
    if not polys:
        raise MeshError("no_contour", "the outline vanished while simplifying")
    n_vert = sum(len(p) for p in polys)
    # --- area clamp: front + back + walls
    area_cs = _cross_section(polys).area() * scale * scale
    perim = sum(float(np.linalg.norm(np.roll(p, -1, axis=0) - p, axis=1).sum()) for p in polys) * scale
    surf = 2 * area_cs + perim * thickness
    if surf > max_area:
        shrink = float(np.sqrt(max_area / surf)) * 0.98
        scale *= shrink
        messages.append(f"slab scaled down by {shrink:.2f} so the surface area stays within {max_area} stud^2")
        area_cs = _cross_section(polys).area() * scale * scale
        perim = sum(float(np.linalg.norm(np.roll(p, -1, axis=0) - p, axis=1).sum()) for p in polys) * scale
        surf = 2 * area_cs + perim * thickness
    # world polygons (studs)
    wpolys = [p * scale for p in polys]
    # --- bevel: inset rings (mitre), fall back to a plain slab when the inset is not valid
    b_used = bevel
    insets: list[np.ndarray] = []
    for _ in range(4):
        insets = [inset_polygon(p, b_used) for p in wpolys]
        if _valid_inset(wpolys, insets, b_used):
            break
        b_used *= 0.5
    else:
        b_used = 0.0
        messages.append("the outline is too fine for a bevelled rim: built a plain slab")
    half = thickness / 2
    # ring z levels
    if b_used > 0:
        levels = [(-half, 1), (-half + b_used, 0), (half - b_used, 0), (half, 1)]
    else:
        levels = [(-half, 0), (half, 0)]
    # --- atlas layout (cells in pixels)
    if back == "same_art":
        front_cell, back_cell, rim_cell = (0, 0, 512, 512), (512, 0, 1024, 512), (0, 512, 1024, 768)
    else:
        front_cell, back_cell, rim_cell = (0, 0, 768, 768), (768, 0, 1024, 256), (0, 768, 1024, 1024)
    A = ATLAS_PX

    def uv_rect(cell):
        x0, y0, x1, y1 = cell
        pad = 2.5
        return (x0 + pad) / A, (y0 + pad) / A, (x1 - pad) / A, (y1 - pad) / A

    fu0, fv0, fu1, fv1 = uv_rect(front_cell)
    bu0, bv0, bu1, bv1 = uv_rect(back_cell)
    ru0, rv0, ru1, rv1 = uv_rect(rim_cell)
    canvas_w, canvas_h = scale, scale * aspect

    def front_uv(xy):
        u = xy[:, 0] / canvas_w + 0.5
        v = 0.5 - xy[:, 1] / canvas_h
        return np.stack([fu0 + u * (fu1 - fu0), fv0 + v * (fv1 - fv0)], axis=1)

    def back_uv(xy):
        u = 0.5 - xy[:, 0] / canvas_w                      # mirrored in x, so the art reads correctly seen from behind
        v = 0.5 - xy[:, 1] / canvas_h
        return np.stack([bu0 + u * (bu1 - bu0), bv0 + v * (bv1 - bv0)], axis=1)

    verts: list[np.ndarray] = []
    uvs: list[np.ndarray] = []
    faces: list[list[int]] = []

    def new_verts(xy: np.ndarray, z: float, uv: np.ndarray) -> np.ndarray:
        base = sum(len(v) for v in verts)
        verts.append(np.column_stack([xy, np.full(len(xy), z)]))
        uvs.append(uv)
        return np.arange(base, base + len(xy))

    # side strips: ring stack per loop
    n_strips = len(levels) - 1
    for p, q in zip(wpolys, insets, strict=True):
        k = len(p)
        arc = np.concatenate([[0.0], np.cumsum(np.linalg.norm(np.roll(p, -1, axis=0) - p, axis=1))])
        u_arc = arc / max(arc[-1], 1e-12)
        ring_ids = []
        for li, (z, use_inset) in enumerate(levels):
            xy = np.vstack([q if use_inset else p, (q if use_inset else p)[:1]])      # seam duplicate
            vv = (li / n_strips) if n_strips else 0.0
            uv = np.stack([ru0 + u_arc * (ru1 - ru0), np.full(k + 1, rv0 + vv * (rv1 - rv0))], axis=1)
            ring_ids.append(new_verts(xy, z, uv))
        for a, b2 in itertools.pairwise(ring_ids):
            faces.extend(_strip_faces(a, b2))
    # caps from the (inset) outlines
    cap_polys = insets if b_used > 0 else wpolys
    flat = np.vstack(cap_polys)
    tri = np.asarray(m3d.triangulate([np.asarray(c, float) for c in cap_polys]), np.int64)
    if len(tri) == 0:
        raise MeshError("triangulation_failed", "the outline could not be triangulated")
    z_front, z_back = half, -half
    f_ids = new_verts(flat, z_front, front_uv(flat))
    b_ids = new_verts(flat, z_back, back_uv(flat))
    faces.extend(f_ids[tri].tolist())
    faces.extend(b_ids[tri][:, ::-1].tolist())
    v = np.vstack(verts)
    uv = np.vstack(uvs)
    mesh = MeshData(v, np.array(faces, np.int64), uv)
    # --- texture
    rim = rim_rgb or tuple(int(c) for c in _edge_colour(art, mask))
    base_rgb = back_rgb or tuple(int(c * 0.82) for c in rim)
    atlas = Image.new("RGB", (A, A), rim)
    flat_art = Image.new("RGBA", art.size, rim + (255,))
    flat_art = Image.alpha_composite(flat_art, art).convert("RGB")
    fw, fh = front_cell[2] - front_cell[0], front_cell[3] - front_cell[1]
    atlas.paste(flat_art.resize((fw, fh), Image.Resampling.LANCZOS), (front_cell[0], front_cell[1]))
    bw, bh = back_cell[2] - back_cell[0], back_cell[3] - back_cell[1]
    if back == "same_art":
        atlas.paste(flat_art.resize((bw, bh), Image.Resampling.LANCZOS), (back_cell[0], back_cell[1]))
    else:
        grad = np.linspace(0.92, 1.0, bh)[None, :, None] * np.array(base_rgb, float)[None, None, :]
        atlas.paste(Image.fromarray(np.repeat(grad, bw, axis=0).transpose(1, 0, 2).astype(np.uint8), "RGB"), (back_cell[0], back_cell[1]))
    rx0, ry0, rx1, ry1 = rim_cell
    ramp = np.linspace(1.0, 0.8, ry1 - ry0)[:, None, None] * np.array(rim, float)[None, None, :]
    atlas.paste(Image.fromarray(np.repeat(ramp, rx1 - rx0, axis=1).astype(np.uint8), "RGB"), (rx0, ry0))
    img, _ = tx.prepare_texture(atlas, mesh.uv, mesh.faces, texture_px)
    mesh.texture = img
    ext = mesh.extents
    facts = {
        "kind": kind, "thickness": float(ext[2]), "thickness_requested": thickness, "bevel": b_used, "tris": mesh.n_tris, "outline_vertices": n_vert,
        "simplify_eps": eps, "size_studs": [canvas_w, canvas_h], "surface_area": surf, "back": back, "front_cell": list(front_cell), "back_cell": list(back_cell),
        "rim_cell": list(rim_cell), "atlas_px": A, "extent_ratio": float(ext.min() / ext.max()), "loops": len(wpolys),
    }
    mesh.meta.update({"kind": kind, "slab": {k: facts[k] for k in ("thickness", "bevel", "back", "front_cell", "back_cell", "rim_cell", "atlas_px", "size_studs")}})
    mesh.meta["attachment_offset"] = [0.0, 0.0, 0.0]
    return SlabResult(mesh, facts, messages)


def _edge_colour(art: Image.Image, mask: np.ndarray) -> np.ndarray:
    """Dominant colour of the outermost opaque ring (the sticker's border colour)."""
    arr = np.asarray(art.convert("RGBA"))
    ring = mask & ~ndimage.binary_erosion(mask, iterations=max(2, mask.shape[0] // 128))
    px = arr[..., :3][ring] if ring.any() else arr[..., :3][mask]
    dom = col.dominant_colours(px.reshape(-1, 1, 3), None, k=1)
    return dom[0][0] if dom else np.array([200, 200, 200.0])


# --------------------------------------------------------------------------------------------------------------------
# CHK-M20
# --------------------------------------------------------------------------------------------------------------------
def _slab_view(mesh: MeshData, view: str, size: int = 128) -> tuple[np.ndarray, np.ndarray]:
    """Unlit render of a slab view on white: ``(RGB uint8, coverage mask)``. Both views share the same scale and canvas centre."""
    used = mesh.vertices[np.unique(mesh.faces)]
    cam = raster.fit_camera(view, used, size, size, margin=0.04)
    # the back camera must frame the same canvas as the front one: mirror the framing by using the front camera's extents
    tex = np.asarray(tx.as_pil(mesh.texture).convert("RGB"))
    out = raster.render([raster.RenderMesh("slab", mesh.vertices, mesh.faces, mesh.uv, tex, unlit=True)], cam, size, size, ss=1)
    return out.on_background((255, 255, 255)), out.mask


def _is_plain(img: np.ndarray, mask: np.ndarray) -> bool:
    """True when the visible area of a render is (nearly) one colour."""
    inner = ndimage.binary_erosion(mask, iterations=3)
    if not inner.any():
        return False
    return bool(img[inner].reshape(-1, 3).astype(float).std(axis=0).max() < 10.0)


def check_slab(mesh: MeshData, slab_meta: dict[str, Any], *, approved_views: dict[str, Any] | None = None) -> list[CheckResult]:
    """CHK-M20: thickness, extent ratio, a separate un-mirrored back island, no coplanar duplicate faces."""
    fm = ["ACC-17", "ACC-18"]
    try:
        problems: list[str] = []
        ext = np.sort(mesh.extents)
        t_min = float(limits.threshold("slab.thickness_min"))
        ratio_min = float(limits.threshold("slab.extent_ratio_min"))
        if ext[0] < t_min - 1e-6:
            problems.append(f"thickness {ext[0]:.3f} stud < {t_min}")
        ratio = float(ext[0] / max(ext[2], 1e-12))
        if ratio < ratio_min:
            problems.append(f"smallest/largest extent {ratio:.3f} < {ratio_min}")
        # islands: back-cap faces (normal -Z) must not share UV area with the front-cap faces (+Z)
        n, _area = geo.face_normals_areas(mesh.vertices, mesh.faces)
        front = np.nonzero(n[:, 2] > 0.999)[0]
        backf = np.nonzero(n[:, 2] < -0.999)[0]
        island_ok, same_art = True, slab_meta.get("back") == "same_art"
        if len(front) and len(backf) and mesh.uv is not None:
            fu, bu = mesh.uv[mesh.faces[front]].reshape(-1, 2), mesh.uv[mesh.faces[backf]].reshape(-1, 2)
            f_lo, f_hi, b_lo, b_hi = fu.min(0), fu.max(0), bu.min(0), bu.max(0)
            overlap = np.all(f_lo < b_hi - 1e-6) and np.all(b_lo < f_hi - 1e-6)
            if overlap:
                island_ok = False
                problems.append("the back faces share UV area with the front faces (no own island)")
        else:
            island_ok = False
            problems.append("no distinct front and back faces found")
        # the back must not be the mirrored front art (pHash on unlit view renders; FM ACC-17)
        if island_ok and mesh.texture is not None:
            f_img, f_mask = _slab_view(mesh, "front")
            b_img, b_mask = _slab_view(mesh, "back")
            f_flip, f_mask_flip = f_img[:, ::-1], f_mask[:, ::-1]
            lim = int(limits.threshold("slab.back_mirror_phash_min"))

            def strong_share(x: np.ndarray, y: np.ndarray, region: np.ndarray) -> float:
                region = ndimage.binary_erosion(region, iterations=2)
                if not region.any():
                    return 1.0
                return float((np.abs(x.astype(int) - y.astype(int)).max(axis=2)[region] > 48).mean())

            mask_sym = float(np.logical_and(f_mask, f_mask_flip).sum() / max(np.logical_or(f_mask, f_mask_flip).sum(), 1))
            sym = mask_sym > 0.97 and strong_share(f_img, f_flip, f_mask & f_mask_flip) <= 0.004
            share_same = strong_share(b_img, f_img, f_mask & b_mask)
            share_mirror = strong_share(b_img, f_flip, f_mask_flip & b_mask)
            if same_art:
                if share_same > 0.004 and not sym:
                    problems.append(f"the back does not show the un-mirrored art ({share_same:.1%} of pixels differ from the front art, {share_mirror:.1%} from its mirror)")
            elif _is_plain(b_img, b_mask):
                pass                                           # a plain colour back cannot be the mirrored art
            else:
                d_mirror = col.hamming(col.phash(b_img), col.phash(f_flip))
                if d_mirror <= lim and not sym:
                    problems.append(f"the back looks like the mirrored front art (pHash distance {d_mirror} <= {lim})")
        # coplanar duplicate faces
        from duoskin.mesh.validate import coplanar_intersections

        w = geo.weld(mesh.vertices, mesh.faces)
        cop = coplanar_intersections(w.vertices, w.faces)
        if cop:
            problems.append(f"{cop} coplanar intersecting triangles")
        ok = not problems
        return [CheckResult(check_id="CHK-M20", fm_ids=fm, kind="hard", passed=ok, metric="slab_thickness_stud", value=round(float(ext[0]), 5),
                            threshold=limits.describe("slab.thickness_min", ">=") + "; " + limits.describe("slab.extent_ratio_min", "ratio >="),
                            evidence="; ".join(problems) or f"thickness {ext[0]:.3f}, ratio {ratio:.2f}, own un-mirrored back island, no coplanar duplicates",
                            fix_hint="none" if ok else "regenerate")]
    except Exception as exc:  # noqa: BLE001
        return [not_run("CHK-M20", "hard", f"{type(exc).__name__}: {exc}", fm_ids=fm)]

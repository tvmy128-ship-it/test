"""Numpy z-buffer rasteriser: beauty pass, ID pass, depth and label pass (APP_SPEC 10.11). No GPU, no extra packages.

* Orthographic cameras (``front``, ``left``, ``back``, ``right``, ``top``, ``bottom``, ``three_quarter``) built in the
  file frame of the app: studs, Y up, the object's front faces +Z, the subject's own left is +X.
  ``left`` is the *subject's* left side (the camera stands on +X), so the object's front points to the image's left edge.
* Beauty pass: flat albedo (texture or colour) with one soft light band, sRGB, no tone mapping, optional supersampling
  with area downscale. ID pass: no anti-aliasing and nearest sampling, one flat id per mesh. Depth is kept.
* Label pass: a per-mesh integer label canvas sampled through the same UVs (nearest), so every pixel gets a garment label.

The rasteriser is fully vectorised (fragments are generated per triangle bounding box in chunks and resolved with a
sort), so a 4000-triangle accessory at 1024 px takes well under a second.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

import numpy as np

VIEWS = ("front", "left", "back", "right", "top", "bottom", "three_quarter")
GREY_BG = (217, 217, 217)

# camera basis per view: (forward = viewing direction, up). right = forward x up.
_VIEW_BASIS = {
    "front": ((0, 0, -1), (0, 1, 0)),
    "back": ((0, 0, 1), (0, 1, 0)),
    "left": ((-1, 0, 0), (0, 1, 0)),     # the subject's left side (camera on +X)
    "right": ((1, 0, 0), (0, 1, 0)),     # the subject's right side (camera on -X)
    "top": ((0, -1, 0), (0, 0, -1)),
    "bottom": ((0, 1, 0), (0, 0, 1)),
}


@dataclass
class RenderMesh:
    """One drawable. UVs use the glTF convention (v down); ``texture`` is HxWx3|4 uint8."""

    name: str
    vertices: np.ndarray
    faces: np.ndarray
    uv: np.ndarray | None = None
    texture: np.ndarray | None = None
    color: tuple[int, int, int] = (200, 200, 200)
    object_id: int = 1
    label_map: np.ndarray | None = None      # (H, W) integer labels addressed through the same UVs as ``texture``
    two_sided: bool = True
    smooth: bool = False                     # interpolate vertex normals (organic meshes) instead of flat faces
    unlit: bool = False

    def __post_init__(self) -> None:
        self.vertices = np.asarray(self.vertices, float).reshape(-1, 3)
        self.faces = np.asarray(self.faces, np.int64).reshape(-1, 3)
        if self.uv is not None:
            self.uv = np.asarray(self.uv, float).reshape(-1, 2)
        if self.texture is not None:
            self.texture = np.asarray(self.texture)


@dataclass
class Camera:
    """An orthographic camera: world point ``p`` maps to ``x = w/2 + (p-c).right * s``, ``y = h/2 - (p-c).up * s``."""

    right: np.ndarray
    up: np.ndarray
    forward: np.ndarray
    centre: np.ndarray
    scale: float                     # pixels per stud
    view: str = ""

    def project(self, p: np.ndarray, width: int, height: int) -> tuple[np.ndarray, np.ndarray]:
        """Screen xy (pixels, y down) and depth (grows away from the camera) of world points ``p`` (..., 3)."""
        d = np.asarray(p, float) - self.centre
        x = width / 2.0 + d @ self.right * self.scale
        y = height / 2.0 - d @ self.up * self.scale
        z = d @ self.forward
        return np.stack([x, y], axis=-1), z


def _basis(view: str, azimuth_deg: float = 35.0, elevation_deg: float = 20.0) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    if view == "three_quarter":
        az, el = np.radians(azimuth_deg), np.radians(elevation_deg)
        d = np.array([np.sin(az) * np.cos(el), np.sin(el), np.cos(az) * np.cos(el)])   # camera position direction
        forward = -d
        up_w = np.array([0.0, 1.0, 0.0])
        right = np.cross(forward, up_w)
        right /= np.linalg.norm(right)
        up = np.cross(right, forward)
        return right, up, forward
    if view not in _VIEW_BASIS:
        raise ValueError(f"unknown view {view!r}; expected one of {VIEWS}")
    f, u = (np.array(v, float) for v in _VIEW_BASIS[view])
    return np.cross(f, u), u, f


def make_camera(view: str, centre: Iterable[float], px_per_stud: float, *, azimuth_deg: float = 35.0,
                elevation_deg: float = 20.0) -> Camera:
    """A camera of a named ``view`` looking at ``centre`` at a fixed scale (the same scale for every view of a set)."""
    r, u, f = _basis(view, azimuth_deg, elevation_deg)
    return Camera(r, u, f, np.asarray(list(centre), float), float(px_per_stud), view)


def fit_camera(view: str, points: np.ndarray, width: int, height: int, margin: float = 0.08, *,
               azimuth_deg: float = 35.0, elevation_deg: float = 20.0) -> Camera:
    """A camera of ``view`` that frames ``points`` (N, 3) with a relative ``margin`` on every side."""
    r, u, f = _basis(view, azimuth_deg, elevation_deg)
    pts = np.asarray(points, float).reshape(-1, 3)
    if len(pts) == 0:
        pts = np.zeros((1, 3))
    pr, pu = pts @ r, pts @ u
    cx, cy = (pr.min() + pr.max()) / 2, (pu.min() + pu.max()) / 2
    ex, ey = max(pr.max() - pr.min(), 1e-6), max(pu.max() - pu.min(), 1e-6)
    s = min(width * (1 - 2 * margin) / ex, height * (1 - 2 * margin) / ey)
    centre = r * cx + u * cy + f * float(((pts @ f).min() + (pts @ f).max()) / 2)
    return Camera(r, u, f, centre, float(s), view)


# --------------------------------------------------------------------------------------------------------------------
# triangle rasteriser
# --------------------------------------------------------------------------------------------------------------------
def _resolve_chunk(xy: np.ndarray, z: np.ndarray, tri_ids: np.ndarray, width: int, height: int,
                   zbuf: np.ndarray, tbuf: np.ndarray) -> None:
    """Rasterise triangles ``xy`` (m,3,2) / ``z`` (m,3) and merge the nearest fragment per pixel into the buffers."""
    x0, y0 = xy[:, 0, 0], xy[:, 0, 1]
    x1, y1 = xy[:, 1, 0], xy[:, 1, 1]
    x2, y2 = xy[:, 2, 0], xy[:, 2, 1]
    denom = (y1 - y2) * (x0 - x2) + (x2 - x1) * (y0 - y2)
    ok = np.abs(denom) > 1e-12
    xmin = np.floor(np.minimum(np.minimum(x0, x1), x2) - 0.5).astype(np.int64)
    xmax = np.ceil(np.maximum(np.maximum(x0, x1), x2) - 0.5).astype(np.int64)
    ymin = np.floor(np.minimum(np.minimum(y0, y1), y2) - 0.5).astype(np.int64)
    ymax = np.ceil(np.maximum(np.maximum(y0, y1), y2) - 0.5).astype(np.int64)
    xmin, ymin = np.maximum(xmin, 0), np.maximum(ymin, 0)
    xmax, ymax = np.minimum(xmax, width - 1), np.minimum(ymax, height - 1)
    w = xmax - xmin + 1
    h = ymax - ymin + 1
    ok &= (w > 0) & (h > 0)
    if not ok.any():
        return
    sel = np.nonzero(ok)[0]
    w, h = w[sel], h[sel]
    counts = w * h
    total = int(counts.sum())
    if total == 0:
        return
    tri = np.repeat(np.arange(len(sel)), counts)
    starts = np.cumsum(counts) - counts
    local = np.arange(total) - np.repeat(starts, counts)
    wt = w[tri]
    px = xmin[sel][tri] + local % wt
    py = ymin[sel][tri] + local // wt
    cx, cy = px + 0.5, py + 0.5
    g = sel[tri]
    d = denom[g]
    l0 = ((y1[g] - y2[g]) * (cx - x2[g]) + (x2[g] - x1[g]) * (cy - y2[g])) / d
    l1 = ((y2[g] - y0[g]) * (cx - x2[g]) + (x0[g] - x2[g]) * (cy - y2[g])) / d
    l2 = 1.0 - l0 - l1
    eps = -1e-7
    inside = (l0 >= eps) & (l1 >= eps) & (l2 >= eps)
    if not inside.any():
        return
    px, py, g = px[inside], py[inside], g[inside]
    depth = (l0[inside] * z[g, 0] + l1[inside] * z[g, 1] + l2[inside] * z[g, 2])
    pix = py * width + px
    order = np.lexsort((depth, pix))
    pix_s = pix[order]
    first = np.ones(len(order), bool)
    first[1:] = pix_s[1:] != pix_s[:-1]
    win = order[first]
    wp = pix[win]
    wd = depth[win]
    zf = zbuf.reshape(-1)
    tf = tbuf.reshape(-1)
    better = wd < zf[wp]
    wp, wd, wg = wp[better], wd[better], g[win][better]
    zf[wp] = wd
    tf[wp] = tri_ids[wg]


def rasterise(xy: np.ndarray, z: np.ndarray, width: int, height: int, max_fragments: int = 3_000_000) -> tuple[np.ndarray, np.ndarray]:
    """Nearest-triangle buffers for projected triangles. Returns ``(zbuf (H,W) float32 [inf = empty], tri (H,W) int32 [-1])``."""
    zbuf = np.full((height, width), np.inf, np.float64)
    tbuf = np.full((height, width), -1, np.int64)
    m = len(xy)
    if m == 0:
        return zbuf.astype(np.float32), tbuf.astype(np.int32)
    xmin = np.maximum(np.floor(xy[:, :, 0].min(axis=1) - 0.5), 0)
    xmax = np.minimum(np.ceil(xy[:, :, 0].max(axis=1) - 0.5), width - 1)
    ymin = np.maximum(np.floor(xy[:, :, 1].min(axis=1) - 0.5), 0)
    ymax = np.minimum(np.ceil(xy[:, :, 1].max(axis=1) - 0.5), height - 1)
    area = np.maximum(xmax - xmin + 1, 0) * np.maximum(ymax - ymin + 1, 0)
    cum = np.cumsum(area)
    ids = np.arange(m)
    start = 0
    while start < m:
        base = cum[start - 1] if start else 0
        end = int(np.searchsorted(cum, base + max_fragments, side="right"))
        end = max(end, start + 1)
        end = min(end, m)
        _resolve_chunk(xy[start:end], z[start:end], ids[start:end], width, height, zbuf, tbuf)
        start = end
    return zbuf.astype(np.float32), tbuf.astype(np.int32)


# --------------------------------------------------------------------------------------------------------------------
# scene assembly
# --------------------------------------------------------------------------------------------------------------------
@dataclass
class RenderOutput:
    beauty: np.ndarray                 # (H, W, 4) uint8 RGBA, alpha = coverage (anti-aliased when ss > 1)
    ids: np.ndarray                    # (H, W) int32, 0 = background, else RenderMesh.object_id
    depth: np.ndarray                  # (H, W) float32, inf = background (camera depth in studs)
    labels: np.ndarray | None          # (H, W) uint16 or None
    mask: np.ndarray                   # (H, W) bool coverage of the ID pass
    camera: Camera
    meta: dict = field(default_factory=dict)

    def on_background(self, rgb: tuple[int, int, int] = GREY_BG) -> np.ndarray:
        """The beauty pass composited over a flat colour (RGB uint8)."""
        a = self.beauty[..., 3:4].astype(np.float32) / 255.0
        out = self.beauty[..., :3].astype(np.float32) * a + np.array(rgb, np.float32) * (1 - a)
        return np.clip(out + 0.5, 0, 255).astype(np.uint8)


def _stack(meshes: list[RenderMesh]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """All vertices, all faces (re-indexed) and the owning mesh index per face."""
    verts, faces, owner = [], [], []
    off = 0
    for i, m in enumerate(meshes):
        if len(m.faces) == 0:
            continue
        verts.append(m.vertices)
        faces.append(m.faces + off)
        owner.append(np.full(len(m.faces), i, np.int64))
        off += len(m.vertices)
    if not verts:
        return np.zeros((0, 3)), np.zeros((0, 3), np.int64), np.zeros(0, np.int64)
    return np.concatenate(verts), np.concatenate(faces), np.concatenate(owner)


def _vertex_normals(v: np.ndarray, f: np.ndarray) -> np.ndarray:
    a, b, c = v[f[:, 0]], v[f[:, 1]], v[f[:, 2]]
    fn = np.cross(b - a, c - a)           # area weighted
    vn = np.zeros_like(v)
    for k in range(3):
        np.add.at(vn, f[:, k], fn)
    ln = np.linalg.norm(vn, axis=1, keepdims=True)
    return vn / np.maximum(ln, 1e-12)


def _sample_texture(tex: np.ndarray, uv: np.ndarray, nearest: bool) -> np.ndarray:
    """Sample HxWxC texture at glTF UVs (v down), clamp-to-edge. Returns float32 (K, C)."""
    h, w = tex.shape[:2]
    u = uv[:, 0] * w
    v = uv[:, 1] * h
    if nearest:
        xi = np.clip(np.floor(u).astype(np.int64), 0, w - 1)
        yi = np.clip(np.floor(v).astype(np.int64), 0, h - 1)
        return tex[yi, xi].astype(np.float32)
    u -= 0.5
    v -= 0.5
    x0 = np.floor(u).astype(np.int64)
    y0 = np.floor(v).astype(np.int64)
    fx = (u - x0).astype(np.float32)[:, None]
    fy = (v - y0).astype(np.float32)[:, None]
    x1, y1 = np.clip(x0 + 1, 0, w - 1), np.clip(y0 + 1, 0, h - 1)
    x0, y0 = np.clip(x0, 0, w - 1), np.clip(y0, 0, h - 1)
    t = tex.astype(np.float32)
    return (t[y0, x0] * (1 - fx) * (1 - fy) + t[y0, x1] * fx * (1 - fy) + t[y1, x0] * (1 - fx) * fy + t[y1, x1] * fx * fy)


_LIGHT_CAM = np.array([-0.35, 0.55, 0.76])
_LIGHT_CAM = _LIGHT_CAM / np.linalg.norm(_LIGHT_CAM)
AMBIENT, DIFFUSE = 0.70, 0.30


def _raster_pass(meshes: list[RenderMesh], cam: Camera, width: int, height: int):
    v, f, owner = _stack(meshes)
    if len(f) == 0:
        return v, f, owner, np.full((height, width), np.inf, np.float32), np.full((height, width), -1, np.int32), None, None
    xy, z = cam.project(v, width, height)
    txy, tz = xy[f], z[f]
    zbuf, tbuf = rasterise(txy, tz, width, height)
    return v, f, owner, zbuf, tbuf, txy, tz


def silhouette(meshes: list[RenderMesh] | RenderMesh, cam: Camera, width: int, height: int) -> np.ndarray:
    """Boolean coverage mask only (no shading)."""
    ms = [meshes] if isinstance(meshes, RenderMesh) else list(meshes)
    _, _, _, zbuf, _, _, _ = _raster_pass(ms, cam, width, height)
    return np.isfinite(zbuf)


def _shade_pass(meshes, cam, width, height, *, nearest: bool, want_labels: bool):
    meshes = [m for m in meshes if len(m.faces)]
    v, f, owner, zbuf, tbuf, txy, tz = _raster_pass(meshes, cam, width, height)
    covered = tbuf >= 0
    rgb = np.zeros((height, width, 3), np.float32)
    ids = np.zeros((height, width), np.int32)
    labels = np.zeros((height, width), np.uint16) if want_labels else None
    if not covered.any():
        return rgb, covered, ids, zbuf, labels
    ys, xs = np.nonzero(covered)
    tri = tbuf[ys, xs].astype(np.int64)
    # barycentrics at the pixel centres
    p = txy[tri]                                  # (K, 3, 2)
    cx, cy = xs + 0.5, ys + 0.5
    x0, y0, x1, y1, x2, y2 = p[:, 0, 0], p[:, 0, 1], p[:, 1, 0], p[:, 1, 1], p[:, 2, 0], p[:, 2, 1]
    d = (y1 - y2) * (x0 - x2) + (x2 - x1) * (y0 - y2)
    l0 = ((y1 - y2) * (cx - x2) + (x2 - x1) * (cy - y2)) / d
    l1 = ((y2 - y0) * (cx - x2) + (x0 - x2) * (cy - y2)) / d
    l2 = 1.0 - l0 - l1
    own = owner[tri]
    face_counts = np.array([len(m.faces) for m in meshes], np.int64)
    face_starts = np.concatenate([[0], np.cumsum(face_counts)[:-1]])
    for i, m in enumerate(meshes):
        sel = np.nonzero(own == i)[0]
        if len(sel) == 0:
            continue
        fv = m.faces[tri[sel] - face_starts[i]]    # (k, 3) local vertex ids
        b0, b1, b2 = l0[sel][:, None], l1[sel][:, None], l2[sel][:, None]
        py_, px_ = ys[sel], xs[sel]
        ids[py_, px_] = m.object_id
        va, vb, vc = m.vertices[fv[:, 0]], m.vertices[fv[:, 1]], m.vertices[fv[:, 2]]
        if m.smooth:
            vn = _vertex_normals(m.vertices, m.faces)
            nrm = b0 * vn[fv[:, 0]] + b1 * vn[fv[:, 1]] + b2 * vn[fv[:, 2]]
        else:
            nrm = np.cross(vb - va, vc - va)
        nrm = nrm / np.maximum(np.linalg.norm(nrm, axis=1, keepdims=True), 1e-12)
        ncam = np.stack([nrm @ cam.right, nrm @ cam.up, -(nrm @ cam.forward)], axis=1)   # +z toward the camera
        if m.two_sided:
            flip = ncam[:, 2] < 0
            ncam[flip] *= -1.0
        ndl = np.clip(ncam @ _LIGHT_CAM, 0.0, 1.0)
        light = np.ones(len(sel), np.float32) if m.unlit else (AMBIENT + DIFFUSE * ndl).astype(np.float32)
        colour = np.tile(np.array(m.color, np.float32), (len(sel), 1))
        if m.uv is not None and (m.texture is not None or (want_labels and m.label_map is not None)):
            uv = b0 * m.uv[fv[:, 0]] + b1 * m.uv[fv[:, 1]] + b2 * m.uv[fv[:, 2]]
            if m.texture is not None:
                tex = m.texture
                if tex.ndim == 2:
                    tex = np.repeat(tex[..., None], 3, axis=2)
                s = _sample_texture(tex, uv, nearest)
                colour = s[:, :3]
                if s.shape[1] == 4:
                    a = s[:, 3:4] / 255.0
                    colour = colour * a + np.array(m.color, np.float32) * (1 - a)
            if want_labels and m.label_map is not None:
                lm = m.label_map
                h_, w_ = lm.shape[:2]
                xi = np.clip(np.floor(uv[:, 0] * w_).astype(np.int64), 0, w_ - 1)
                yi = np.clip(np.floor(uv[:, 1] * h_).astype(np.int64), 0, h_ - 1)
                labels[py_, px_] = lm[yi, xi].astype(np.uint16)
        rgb[py_, px_] = colour * light[:, None]
    return rgb, covered, ids, zbuf, labels


def render(meshes: list[RenderMesh], cam: Camera, width: int = 512, height: int = 512, *, ss: int = 2,
           labels: bool = False) -> RenderOutput:
    """Render all passes. ``ss`` supersamples only the beauty pass; ID, depth and label passes are never anti-aliased."""
    meshes = [m for m in meshes if len(m.faces)]
    ss = max(1, int(ss))
    if ss > 1:
        rgb_s, cov_s, _, _, _ = _shade_pass(meshes, Camera(cam.right, cam.up, cam.forward, cam.centre, cam.scale * ss, cam.view),
                                            width * ss, height * ss, nearest=False, want_labels=False)
        a = cov_s.astype(np.float32)
        pm = rgb_s * a[..., None]
        pm = pm.reshape(height, ss, width, ss, 3).mean(axis=(1, 3))
        al = a.reshape(height, ss, width, ss).mean(axis=(1, 3))
        col = np.where(al[..., None] > 0, pm / np.maximum(al[..., None], 1e-6), 0.0)
        beauty = np.concatenate([col, al[..., None] * 255.0], axis=2)
        _, cov1, ids, zbuf, lab = _shade_pass_ids(meshes, cam, width, height, labels)
    else:
        rgb1, cov1, ids, zbuf, lab = _shade_pass(meshes, cam, width, height, nearest=False, want_labels=labels)
        beauty = np.concatenate([rgb1, cov1[..., None].astype(np.float32) * 255.0], axis=2)
    beauty = np.clip(beauty + 0.5, 0, 255).astype(np.uint8)
    return RenderOutput(beauty, ids, zbuf, lab, cov1, cam, {"width": width, "height": height, "ss": ss})


def _shade_pass_ids(meshes, cam, width, height, labels):
    rgb, cov, ids, zbuf, lab = _shade_pass(meshes, cam, width, height, nearest=True, want_labels=labels)
    return rgb, cov, ids, zbuf, lab


def render_ids(meshes: list[RenderMesh], cam: Camera, width: int = 512, height: int = 512, *, labels: bool = False) -> RenderOutput:
    """ID/depth/label passes only (cheaper): beauty is the plain 1x pass."""
    return render(meshes, cam, width, height, ss=1, labels=labels)

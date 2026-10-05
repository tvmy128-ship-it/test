"""Parametric accessory builders for the no-Tripo path (APP_SPEC 10.8, ``code_primitive``).

Everything is built in studs, Y up, front = +Z, as closed outward-wound triangle meshes with one UV island per part and
one opaque palette atlas (so the result already satisfies the single-mesh, single-material, single-texture gate).

Kinds: ``bead``, ``ring``, ``loop``, ``strap``, ``box``, ``cylinder``, ``sphere``, ``torus`` and ``charm`` (a bead with a
hanging ring). ``build`` returns a ``MeshData`` whose texture is the palette atlas; ``recolour`` repaints the atlas
without touching the geometry; ``attach_loop`` unions a keychain loop onto any watertight textured mesh (manifold3d).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable

import numpy as np
from PIL import Image

from duoskin.mesh.colour import lab_to_srgb, srgb_to_lab
from duoskin.mesh.geometry import face_normals_areas, orient_outward, weld
from duoskin.mesh.types import MeshData, MeshError

RGB = tuple[int, int, int]
KINDS = ("bead", "ring", "loop", "strap", "box", "cylinder", "sphere", "torus", "charm")


@dataclass
class Part:
    """A closed surface with UVs in its own local unit square, painted with one colour slot."""

    name: str
    vertices: np.ndarray
    faces: np.ndarray
    uv: np.ndarray           # (N, 2) local 0-1
    slot: str = "base"       # key into the palette: base, shade, accent, trim


# --------------------------------------------------------------------------------------------------------------------
# surface generators
# --------------------------------------------------------------------------------------------------------------------
def _orient_outward(v: np.ndarray, f: np.ndarray) -> np.ndarray:
    w = weld(v, f)
    return _flip_like(f, orient_outward(w.vertices, w.faces)[1])


def _flip_like(f: np.ndarray, flipped: np.ndarray) -> np.ndarray:
    out = f.copy()
    out[flipped] = out[flipped][:, ::-1]
    return out


def _drop_degenerate(v: np.ndarray, f: np.ndarray, eps: float = 1e-12) -> np.ndarray:
    _, area = face_normals_areas(v, f)
    return f[area > eps]


def grid_surface(p: np.ndarray, uv: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Triangles over a (nu+1, nv+1) grid of points (seam rows/columns are duplicated, so UVs stay clean)."""
    nu1, nv1 = p.shape[:2]
    idx = np.arange(nu1 * nv1).reshape(nu1, nv1)
    a, b = idx[:-1, :-1].ravel(), idx[1:, :-1].ravel()
    c, d = idx[1:, 1:].ravel(), idx[:-1, 1:].ravel()
    f = np.concatenate([np.stack([a, b, c], axis=1), np.stack([a, c, d], axis=1)])
    return p.reshape(-1, 3), f, uv.reshape(-1, 2)


def _part_from_grid(name: str, p: np.ndarray, uv: np.ndarray, slot: str) -> Part:
    v, f, u = grid_surface(p, uv)
    f = _drop_degenerate(v, f)
    f = _orient_outward(v, f)
    return Part(name, v, f, u, slot)


def sphere_part(radius: float, segments: int = 16, rings: int = 10, centre=(0.0, 0.0, 0.0), slot: str = "base",
                name: str = "sphere") -> Part:
    segments, rings = max(segments, 6), max(rings, 4)
    u = np.linspace(0, 1, segments + 1)
    v = np.linspace(0, 1, rings + 1)
    th, ph = np.meshgrid(u * 2 * math.pi, v * math.pi, indexing="ij")
    p = np.stack([radius * np.sin(ph) * np.cos(th), radius * np.cos(ph), radius * np.sin(ph) * np.sin(th)], axis=-1)
    p = p + np.asarray(centre, float)
    uv = np.stack(np.meshgrid(u, v, indexing="ij"), axis=-1)
    return _part_from_grid(name, p, uv, slot)


def torus_part(major: float, minor: float, segments: int = 24, tube_segments: int = 10, centre=(0.0, 0.0, 0.0),
               slot: str = "base", name: str = "torus") -> Part:
    """A torus lying in the XY plane (its axis is Z), so a ring hangs facing the front."""
    segments, tube_segments = max(segments, 8), max(tube_segments, 5)
    u = np.linspace(0, 1, segments + 1)
    v = np.linspace(0, 1, tube_segments + 1)
    a, b = np.meshgrid(u * 2 * math.pi, v * 2 * math.pi, indexing="ij")
    rr = major + minor * np.cos(b)
    p = np.stack([rr * np.cos(a), rr * np.sin(a), minor * np.sin(b)], axis=-1) + np.asarray(centre, float)
    uv = np.stack(np.meshgrid(u, v, indexing="ij"), axis=-1)
    return _part_from_grid(name, p, uv, slot)


def _fan_cap(ring_pts: np.ndarray, centre: np.ndarray, flip: bool, uv_centre=(0.5, 0.5), uv_radius=0.45):
    """A triangle fan closing a planar convex ring (vertices are new, with disc UVs)."""
    m = len(ring_pts)
    ang = np.linspace(0, 2 * math.pi, m, endpoint=False)
    v = np.vstack([centre[None], ring_pts])
    uv = np.vstack([np.array(uv_centre)[None], np.stack([uv_centre[0] + uv_radius * np.cos(ang), uv_centre[1] + uv_radius * np.sin(ang)], axis=1)])
    f = np.array([[0, 1 + i, 1 + (i + 1) % m] for i in range(m)])
    return v, (f[:, ::-1] if flip else f), uv


def cylinder_part(radius: float, height: float, segments: int = 20, centre=(0.0, 0.0, 0.0), axis: str = "y",
                  slot: str = "base", name: str = "cylinder") -> Part:
    """A capped cylinder. UV layout: side strip in the top 60% of the island, the two caps as discs below."""
    segments = max(segments, 6)
    ang = np.linspace(0, 2 * math.pi, segments + 1)
    top = np.stack([radius * np.cos(ang), np.full_like(ang, height / 2), radius * np.sin(ang)], axis=1)
    bot = top.copy()
    bot[:, 1] = -height / 2
    side_v = np.vstack([bot, top])
    uu = np.linspace(0, 1, segments + 1)
    side_uv = np.vstack([np.stack([uu, np.full_like(uu, 0.58)], axis=1), np.stack([uu, np.zeros_like(uu)], axis=1)])
    n1 = segments + 1
    sf = []
    for i in range(segments):
        a, b, c, d = i, i + 1, n1 + i + 1, n1 + i
        sf += [[a, c, b], [a, d, c]]
    verts, faces, uvs = [side_v], [np.array(sf)], [side_uv]
    off = len(side_v)
    for sign, ring_y, cx in ((1, height / 2, 0.25), (-1, -height / 2, 0.75)):
        ring = np.stack([radius * np.cos(ang[:-1]), np.full(segments, ring_y), radius * np.sin(ang[:-1])], axis=1)
        v, f, u = _fan_cap(ring, np.array([0.0, ring_y, 0.0]), flip=sign > 0, uv_centre=(cx, 0.79), uv_radius=0.2)
        verts.append(v)
        faces.append(f + off)
        uvs.append(u)
        off += len(v)
    v = np.vstack(verts)
    f = np.vstack(faces)
    u = np.vstack(uvs)
    f = _orient_outward(v, _drop_degenerate(v, f))
    if axis == "z":        # rotate +Y onto +Z
        v = np.stack([v[:, 0], -v[:, 2], v[:, 1]], axis=1)
    elif axis == "x":      # rotate +Y onto +X
        v = np.stack([v[:, 1], -v[:, 0], v[:, 2]], axis=1)
    v = v + np.asarray(centre, float)
    f = _orient_outward(v, f)
    return Part(name, v, f, u, slot)


def box_part(size: tuple[float, float, float], centre=(0.0, 0.0, 0.0), slot: str = "base", name: str = "box") -> Part:
    """A box with one UV face per side in a 4x3 cross layout (hard edges: every face owns its vertices)."""
    sx, sy, sz = (s / 2 for s in size)
    faces_def = [  # (corner a, b, c, d counter-clockwise seen from outside) and the cross cell (col,row)
        ([(-sx, -sy, sz), (sx, -sy, sz), (sx, sy, sz), (-sx, sy, sz)], (1, 1)),      # +Z front
        ([(sx, -sy, -sz), (-sx, -sy, -sz), (-sx, sy, -sz), (sx, sy, -sz)], (3, 1)),  # -Z back
        ([(sx, -sy, sz), (sx, -sy, -sz), (sx, sy, -sz), (sx, sy, sz)], (2, 1)),      # +X
        ([(-sx, -sy, -sz), (-sx, -sy, sz), (-sx, sy, sz), (-sx, sy, -sz)], (0, 1)),  # -X
        ([(-sx, sy, sz), (sx, sy, sz), (sx, sy, -sz), (-sx, sy, -sz)], (1, 0)),      # +Y
        ([(-sx, -sy, -sz), (sx, -sy, -sz), (sx, -sy, sz), (-sx, -sy, sz)], (1, 2)),  # -Y
    ]
    v, f, u = [], [], []
    for corners, (cx, cy) in faces_def:
        base = len(v)
        v += corners
        x0, x1 = cx / 4 + 0.004, (cx + 1) / 4 - 0.004
        y0, y1 = cy / 3 + 0.004, (cy + 1) / 3 - 0.004
        u += [(x0, y1), (x1, y1), (x1, y0), (x0, y0)]
        f += [[base, base + 1, base + 2], [base, base + 2, base + 3]]
    v = np.array(v, float) + np.asarray(centre, float)
    return Part(name, v, _orient_outward(v, np.array(f)), np.array(u, float), slot)


def sweep_part(path: np.ndarray, profile: np.ndarray, closed: bool, slot: str = "base", name: str = "sweep",
               up_hint=(0.0, 0.0, 1.0)) -> Part:
    """Sweep a closed convex ``profile`` ((m, 2) in the path's normal/binormal plane) along a planar ``path``.

    Closed paths give a torus-like tube; open paths get two fan caps. ``up_hint`` is the normal of the path's plane.
    """
    path = np.asarray(path, float)
    n = len(path)
    if closed:
        tan = np.roll(path, -1, axis=0) - np.roll(path, 1, axis=0)
    else:
        tan = np.gradient(path, axis=0)
    tan /= np.maximum(np.linalg.norm(tan, axis=1, keepdims=True), 1e-12)
    hint = np.asarray(up_hint, float)
    bn = np.cross(tan, hint)
    bn /= np.maximum(np.linalg.norm(bn, axis=1, keepdims=True), 1e-12)     # in-plane normal
    nm = np.cross(bn, tan)                                                  # == plane normal
    prof = np.asarray(profile, float)
    m = len(prof)
    rings = path[:, None, :] + bn[:, None, :] * prof[None, :, 0, None] + nm[:, None, :] * prof[None, :, 1, None]
    if closed:
        rings = np.concatenate([rings, rings[:1]], axis=0)
    rings = np.concatenate([rings, rings[:, :1]], axis=1)                  # duplicate the profile seam
    nu1 = rings.shape[0]
    u = np.linspace(0, 1, nu1)
    v = np.linspace(0, 0.9 if not closed else 1.0, m + 1)
    uv = np.stack(np.meshgrid(u, v, indexing="ij"), axis=-1)
    verts, faces, uvs = grid_surface(rings, uv)
    parts_v, parts_f, parts_u = [verts], [faces], [uvs]
    if not closed:
        off = len(verts)
        for idx, flip, cx in ((0, True, 0.2), (n - 1, False, 0.7)):
            ring = rings[idx, :m]
            v2, f2, u2 = _fan_cap(ring, ring.mean(axis=0), flip=flip, uv_centre=(cx, 0.95), uv_radius=0.04)
            parts_v.append(v2)
            parts_f.append(f2 + off)
            parts_u.append(u2)
            off += len(v2)
    v = np.vstack(parts_v)
    f = np.vstack(parts_f)
    uv_all = np.vstack(parts_u)
    f = _orient_outward(v, _drop_degenerate(v, f))
    return Part(name, v, f, uv_all, slot)


def _circle_profile(radius: float, segments: int = 10) -> np.ndarray:
    a = np.linspace(0, 2 * math.pi, segments, endpoint=False)
    return np.stack([radius * np.cos(a), radius * np.sin(a)], axis=1)


def _rect_profile(w: float, t: float) -> np.ndarray:
    return np.array([[-w / 2, -t / 2], [w / 2, -t / 2], [w / 2, t / 2], [-w / 2, t / 2]], float)


# --------------------------------------------------------------------------------------------------------------------
# palette and atlas
# --------------------------------------------------------------------------------------------------------------------
def derive_palette(base: RGB, palette: dict[str, RGB] | None = None) -> dict[str, RGB]:
    """base, shade, accent and trim colours; missing ones are derived from ``base`` in Lab."""
    p = dict(palette or {})
    p.setdefault("base", tuple(int(c) for c in base))
    lab = srgb_to_lab(np.array(p["base"], float))

    def shifted(dl: float, scale_c: float = 1.0) -> RGB:
        out = lab.copy()
        out[0] = np.clip(out[0] + dl, 0, 100)
        out[1:] *= scale_c
        return tuple(int(x) for x in lab_to_srgb(out))

    p.setdefault("shade", shifted(-16))
    p.setdefault("accent", shifted(+14, 1.15))
    p.setdefault("trim", shifted(-30))
    return {k: tuple(int(x) for x in v) for k, v in p.items()}


@dataclass
class Atlas:
    """A square atlas cut into ``cols x rows`` cells; every part owns one cell (its UV island)."""

    size: int = 1024
    cols: int = 2
    rows: int = 2
    cells: list[str] = field(default_factory=list)      # colour slot of each allocated cell

    def alloc(self, slot: str) -> int:
        if len(self.cells) >= self.cols * self.rows:
            raise MeshError("atlas_full", "too many parts for one atlas")
        self.cells.append(slot)
        return len(self.cells) - 1

    def rect(self, cell: int) -> tuple[float, float, float, float]:
        c, r = cell % self.cols, cell // self.cols
        pad = 3.0 / self.size
        return (c / self.cols + pad, r / self.rows + pad, (c + 1) / self.cols - pad, (r + 1) / self.rows - pad)

    def map_uv(self, local: np.ndarray, cell: int) -> np.ndarray:
        u0, v0, u1, v1 = self.rect(cell)
        return np.stack([u0 + local[:, 0] * (u1 - u0), v0 + local[:, 1] * (v1 - v0)], axis=1)

    def paint(self, palette: dict[str, RGB]) -> Image.Image:
        """Render the atlas: each cell a vertical gradient between its slot colour and a lighter/darker tone (never flat)."""
        arr = np.zeros((self.size, self.size, 3), np.uint8)
        cw, ch = self.size // self.cols, self.size // self.rows
        for i, slot in enumerate(self.cells):
            col = np.array(palette.get(slot, palette["base"]), np.float64)
            lab = srgb_to_lab(col)
            other = lab.copy()
            other[0] = np.clip(lab[0] + (14 if lab[0] < 50 else -14), 0, 100)
            c0, c1 = col, lab_to_srgb(other).astype(np.float64)
            t = np.linspace(0, 1, ch)[:, None, None]
            block = c0[None, None, :] * (1 - t) + c1[None, None, :] * t
            c, r = i % self.cols, i // self.cols
            arr[r * ch:(r + 1) * ch, c * cw:(c + 1) * cw] = np.repeat(block, cw, axis=1).astype(np.uint8)
        # unused cells repeat the base colour so dilated gutters never show black
        for i in range(len(self.cells), self.cols * self.rows):
            c, r = i % self.cols, i // self.cols
            arr[r * ch:(r + 1) * ch, c * cw:(c + 1) * cw] = np.array(palette["base"], np.uint8)
        return Image.fromarray(arr, "RGB")


def assemble(parts: list[Part], palette: dict[str, RGB], texture_px: int = 1024) -> MeshData:
    """Join parts into one MeshData with a palette atlas texture (parts stay separate shells, like plush eyes)."""
    if not parts:
        raise MeshError("no_parts", "nothing to build")
    cols = 1 if len(parts) == 1 else 2
    rows = int(math.ceil(len(parts) / cols))
    atlas = Atlas(texture_px, cols, rows)
    verts, faces, uvs, off = [], [], [], 0
    layout = []
    for p in parts:
        cell = atlas.alloc(p.slot)
        verts.append(p.vertices)
        faces.append(p.faces + off)
        uvs.append(atlas.map_uv(p.uv, cell))
        layout.append({"part": p.name, "slot": p.slot, "cell": cell})
        off += len(p.vertices)
    mesh = MeshData(np.vstack(verts), np.vstack(faces), np.vstack(uvs), atlas.paint(palette))
    mesh.meta.update({"primitive": True, "palette": {k: list(v) for k, v in palette.items()},
                      "atlas": {"size": texture_px, "cols": cols, "rows": rows, "cells": layout}})
    return mesh


# --------------------------------------------------------------------------------------------------------------------
# kinds
# --------------------------------------------------------------------------------------------------------------------
def _stadium_path(width: float, height: float, n: int = 40) -> np.ndarray:
    """A closed stadium (rounded rectangle) path in the XY plane, ``width`` x ``height`` studs, centre at the origin."""
    r = min(width, height) / 2
    pts = []
    straight = max(width, height) - 2 * r
    horizontal = width >= height
    per = max(n // 4, 4)
    if horizontal:
        cx = [straight / 2, -straight / 2]
        for t in np.linspace(-math.pi / 2, math.pi / 2, per, endpoint=False):
            pts.append((cx[0] + r * math.cos(t), r * math.sin(t)))
        for t in np.linspace(math.pi / 2, 3 * math.pi / 2, per, endpoint=False):
            pts.append((cx[1] + r * math.cos(t), r * math.sin(t)))
    else:
        cy = [straight / 2, -straight / 2]
        for t in np.linspace(0, math.pi, per, endpoint=False):
            pts.append((r * math.cos(t), cy[0] + r * math.sin(t)))
        for t in np.linspace(math.pi, 2 * math.pi, per, endpoint=False):
            pts.append((r * math.cos(t), cy[1] + r * math.sin(t)))
    pts = np.array(pts, float)
    return np.concatenate([pts, np.zeros((len(pts), 1))], axis=1)


def _kind_bead(p: dict[str, Any]) -> list[Part]:
    return [sphere_part(float(p.get("diameter", 0.8)) / 2, int(p.get("segments", 16)), int(p.get("rings", 10)))]


def _kind_ring(p: dict[str, Any]) -> list[Part]:
    outer = float(p.get("diameter", 1.2))
    tube = float(p.get("tube", 0.16))
    return [torus_part(outer / 2 - tube / 2, tube / 2, int(p.get("segments", 28)), int(p.get("tube_segments", 10)))]


def _kind_loop(p: dict[str, Any]) -> list[Part]:
    w, h = float(p.get("width", 1.4)), float(p.get("height", 0.9))
    tube = float(p.get("tube", 0.16))
    return [sweep_part(_stadium_path(w - tube, h - tube, int(p.get("segments", 40))), _circle_profile(tube / 2, int(p.get("tube_segments", 8))), True)]


def _kind_strap(p: dict[str, Any]) -> list[Part]:
    length = float(p.get("length", 2.0))
    width = float(p.get("width", 0.4))
    thick = float(p.get("thickness", 0.12))
    bend = float(p.get("bend", 0.9))     # radians of arc
    n = int(p.get("segments", 20))
    if abs(bend) < 1e-3:
        path = np.stack([np.linspace(-length / 2, length / 2, n), np.zeros(n), np.zeros(n)], axis=1)
    else:
        radius = length / abs(bend)
        t = np.linspace(-bend / 2, bend / 2, n)
        path = np.stack([radius * np.sin(t), radius * (1 - np.cos(t)) - radius * (1 - math.cos(bend / 2)) / 2, np.zeros(n)], axis=1)
    return [sweep_part(path, _rect_profile(width, thick), False, up_hint=(0.0, 0.0, 1.0))]


def _kind_box(p: dict[str, Any]) -> list[Part]:
    s = p.get("size", (1.0, 1.0, 1.0))
    return [box_part(tuple(float(x) for x in s))]


def _kind_cylinder(p: dict[str, Any]) -> list[Part]:
    return [cylinder_part(float(p.get("diameter", 1.0)) / 2, float(p.get("height", 1.0)), int(p.get("segments", 20)), axis=str(p.get("axis", "y")))]


def _kind_sphere(p: dict[str, Any]) -> list[Part]:
    return [sphere_part(float(p.get("diameter", 1.0)) / 2, int(p.get("segments", 18)), int(p.get("rings", 12)))]


def _kind_torus(p: dict[str, Any]) -> list[Part]:
    return [torus_part(float(p.get("major", 0.5)), float(p.get("minor", 0.12)), int(p.get("segments", 24)), int(p.get("tube_segments", 10)))]


def _kind_charm(p: dict[str, Any]) -> list[Part]:
    d = float(p.get("diameter", 0.9))
    ring_d = float(p.get("ring_diameter", 0.5))
    tube = float(p.get("tube", 0.09))
    bead = sphere_part(d / 2, 18, 12, slot="base", name="bead")
    ring = torus_part(ring_d / 2 - tube / 2, tube / 2, 24, 8, centre=(0, d / 2 + ring_d / 2 - tube * 0.6, 0), slot="accent", name="ring")
    return [bead, ring]


_KIND_FUNCS: dict[str, Callable[[dict[str, Any]], list[Part]]] = {
    "bead": _kind_bead, "ring": _kind_ring, "loop": _kind_loop, "strap": _kind_strap, "box": _kind_box,
    "cylinder": _kind_cylinder, "sphere": _kind_sphere, "torus": _kind_torus, "charm": _kind_charm,
}


def build(kind: str, params: dict[str, Any] | None = None, palette: dict[str, RGB] | RGB | None = None,
          texture_px: int = 1024) -> MeshData:
    """Build a primitive accessory. ``palette`` may be one base colour or a dict (base, shade, accent, trim).

    Parameters are in studs. The result is closed, watertight, UV-mapped (one island per part) and carries a 1024 px
    (or ``texture_px``) opaque RGB atlas; ``meta['primitive']`` is True.
    """
    if kind not in _KIND_FUNCS:
        raise MeshError("unknown_primitive", f"unknown primitive {kind!r}; choose one of {', '.join(KINDS)}")
    if palette is None:
        palette = (200, 160, 80)
    pal = derive_palette(palette if isinstance(palette, tuple) else palette["base"], None if isinstance(palette, tuple) else palette)
    parts = _KIND_FUNCS[kind](dict(params or {}))
    mesh = assemble(parts, pal, texture_px)
    mesh.meta["primitive_kind"] = kind
    mesh.meta["primitive_params"] = dict(params or {})
    return mesh


def recolour(mesh: MeshData, palette: dict[str, RGB] | RGB) -> MeshData:
    """Repaint a primitive's atlas with a new palette (geometry and UVs untouched)."""
    atlas_meta = mesh.meta.get("atlas")
    if not atlas_meta:
        raise MeshError("not_primitive", "recolour only works on meshes built by duoskin.mesh.primitives")
    base = palette if isinstance(palette, tuple) else palette["base"]
    pal = derive_palette(base, None if isinstance(palette, tuple) else palette)
    atlas = Atlas(atlas_meta["size"], atlas_meta["cols"], atlas_meta["rows"], [c["slot"] for c in atlas_meta["cells"]])
    out = mesh.copy()
    out.texture = atlas.paint(pal)
    out.meta["palette"] = {k: list(v) for k, v in pal.items()}
    return out


def attach_loop(mesh: MeshData, *, at: tuple[float, float, float], width: float = 0.6, height: float = 0.5,
                tube: float = 0.1, rgb: RGB = (190, 190, 200)) -> MeshData:
    """Union a keychain loop onto a watertight textured ``mesh`` (APP_SPEC 10.8; manifold3d, UVs preserved).

    The loop is a small stadium tube centred on ``at`` and facing the front. The texture gains a swatch strip at the
    bottom that holds the loop colour (existing UVs are rescaled, nothing is repainted).
    """
    from duoskin.mesh.boolean import union

    if mesh.texture is None or mesh.uv is None:
        raise MeshError("no_texture", "attach_loop needs a textured mesh")
    tex = mesh.texture.convert("RGB")
    w, h = tex.size
    strip = max(32, h // 8)
    canvas = Image.new("RGB", (w, h + strip), rgb)
    canvas.paste(tex, (0, 0))
    uv = mesh.uv.copy()
    uv[:, 1] = uv[:, 1] * h / (h + strip)
    base = MeshData(mesh.vertices, mesh.faces, uv, canvas, dict(mesh.meta))
    part = _kind_loop({"width": width, "height": height, "tube": tube})[0]
    part.vertices = part.vertices + np.asarray(at, float)
    loop = MeshData(part.vertices, part.faces, np.tile(np.array([0.5, 1.0 - strip / (2.0 * (h + strip))]), (len(part.vertices), 1)))
    out = union(base, loop)
    out.texture = canvas
    out.meta["loop_attached"] = True
    return out

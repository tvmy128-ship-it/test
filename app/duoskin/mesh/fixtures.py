"""Code-built meshes used by the exporter round-trip test (CHK-M19), the unit tests and the offline demo mode.

``f_fixture`` is the asymmetric "F" with a four-corner marker texture: any mirrored, rotated or V-flipped export shows up
as a wrong marker colour at a known vertex. ``plush_fixture`` is a body with ears and two separate eye shells,
``dense_sphere`` is a high-resolution textured sphere for decimation tests, ``hair_with_head_fixture`` is a hair cap
wrapped around the 1.2-stud grey guide cube head (the Tripo hair problem of APP_SPEC 10.7).
"""
from __future__ import annotations

import math

import numpy as np
from PIL import Image, ImageDraw

from duoskin.mesh import primitives as prim
from duoskin.mesh.boolean import box_mesh, union_all
from duoskin.mesh.types import MeshData

F_POLY = np.array([(0, 0), (0.3, 0), (0.3, 0.45), (0.7, 0.45), (0.7, 0.65), (0.3, 0.65), (0.3, 0.8), (0.9, 0.8), (0.9, 1.0), (0, 1.0)], float)
MARK_RED, MARK_GREEN, MARK_BLUE, MARK_YELLOW = (230, 30, 30), (30, 200, 60), (40, 70, 230), (240, 220, 30)
GUIDE_GREY = (154, 154, 154)      # #9A9A9A, the bald-head guide colour (bible 8.2)


def f_texture(size: int = 256) -> Image.Image:
    """White texture with a black F (same footprint as the mesh) and four corner markers (red TL, green TR, blue BL, yellow BR)."""
    img = Image.new("RGB", (size, size), (245, 245, 245))
    d = ImageDraw.Draw(img)
    pts = [(0.05 * size + x * 0.9 * size, 0.95 * size - y * 0.9 * size) for x, y in F_POLY]
    d.polygon(pts, fill=(15, 15, 15))
    m = size // 10
    d.rectangle([0, 0, m, m], fill=MARK_RED)
    d.rectangle([size - m - 1, 0, size - 1, m], fill=MARK_GREEN)
    d.rectangle([0, size - m - 1, m, size - 1], fill=MARK_BLUE)
    d.rectangle([size - m - 1, size - m - 1, size - 1, size - 1], fill=MARK_YELLOW)
    return img


def f_fixture(width: float = 1.8, height: float = 2.0, depth: float = 0.4, texture_px: int = 256) -> MeshData:
    """The asymmetric F letter extruded along Z. Front (+Z) shows the letter readable left to right. Centred at the origin."""
    import manifold3d as m3d

    cs = m3d.CrossSection([F_POLY * np.array([width, height])])
    man = m3d.Manifold.extrude(cs, depth)
    mesh = man.to_mesh()
    v = np.asarray(mesh.vert_properties, float)[:, :3]
    f = np.asarray(mesh.tri_verts, np.int64)
    v = v - np.array([width / 2, height / 2, depth / 2])
    uv = np.stack([0.05 + 0.9 * (v[:, 0] + width / 2) / width, 0.95 - 0.9 * (v[:, 1] + height / 2) / height], axis=1)
    return MeshData(v, f, uv, f_texture(texture_px), {"fixture": "F"})


def marker_colours(mesh: MeshData, texture: Image.Image | None = None) -> dict[str, tuple[int, int, int]]:
    """Texture colour sampled (nearest) at the F's top-left, bottom-left and top-right extreme vertices."""
    tex = np.asarray((texture or mesh.texture).convert("RGB"))
    h, w = tex.shape[:2]
    v = mesh.vertices
    out = {}
    for name, sel in (("top_left", np.lexsort((-v[:, 1], v[:, 0]))[0]), ("bottom_left", np.lexsort((v[:, 1], v[:, 0]))[0])):
        # extreme corner of the front face: pick among z > 0 vertices
        front = np.nonzero(v[:, 2] > 0)[0]
        if name == "top_left":
            i = front[np.argmin(v[front, 0] - v[front, 1])]
        else:
            i = front[np.argmin(v[front, 0] + v[front, 1])]
        u, vv = mesh.uv[i]
        out[name] = tuple(int(c) for c in tex[min(h - 1, max(0, int(vv * h))), min(w - 1, max(0, int(u * w)))])
        del sel
    return out


def _noise_texture(size: int, base: tuple[int, int, int], seed: int = 3, amp: float = 18.0) -> Image.Image:
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:size, 0:size] / size
    grad = (np.sin(xx * 6.0) * np.cos(yy * 5.0)) * amp
    arr = np.array(base, float)[None, None, :] + grad[..., None] + rng.normal(0, 2.0, (size, size, 1))
    return Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8), "RGB")


def dense_sphere(radius: float = 1.0, segments: int = 96, rings: int = 64, texture_px: int = 512) -> MeshData:
    """A UV sphere with a seam and poles, textured with a smooth colour field (a stand-in for a Tripo model)."""
    part = prim.sphere_part(radius, segments, rings)
    tex = np.zeros((texture_px, texture_px, 3), np.uint8)
    yy, xx = np.mgrid[0:texture_px, 0:texture_px] / texture_px
    tex[..., 0] = (120 + 100 * xx).astype(np.uint8)
    tex[..., 1] = (60 + 140 * yy).astype(np.uint8)
    tex[..., 2] = (200 - 120 * xx * yy).astype(np.uint8)
    return MeshData(part.vertices, part.faces, part.uv, Image.fromarray(tex, "RGB"), {"fixture": "dense_sphere"})


def plush_fixture(texture_px: int = 512) -> MeshData:
    """Body + two ears fused into one closed surface, plus two tiny separate closed eye shells (kept by the repair)."""
    pal = prim.derive_palette((236, 170, 190))
    atlas = prim.Atlas(texture_px, 2, 2)
    parts = [prim.sphere_part(0.6, 20, 14, name="body", slot="base"),
             prim.sphere_part(0.22, 12, 8, centre=(-0.4, 0.55, 0.0), name="ear_l", slot="accent"),
             prim.sphere_part(0.22, 12, 8, centre=(0.4, 0.55, 0.0), name="ear_r", slot="accent")]
    meshes = []
    for p in parts:
        cell = atlas.alloc(p.slot)
        meshes.append(MeshData(p.vertices, p.faces, atlas.map_uv(p.uv, cell)))
    fused = union_all(meshes)
    eyes = []
    for sx in (-0.2, 0.2):
        p = prim.sphere_part(0.07, 8, 6, centre=(sx, 0.1, 0.66), slot="trim", name="eye")
        cell = atlas.alloc("trim") if len(atlas.cells) < 4 else 3
        eyes.append((p, cell))
    verts, faces, uvs = [fused.vertices], [fused.faces], [fused.uv]
    off = len(fused.vertices)
    for p, cell in eyes:
        verts.append(p.vertices)
        faces.append(p.faces + off)
        uvs.append(atlas.map_uv(p.uv, min(cell, 3)))
        off += len(p.vertices)
    tex = atlas.paint(pal)
    return MeshData(np.vstack(verts), np.vstack(faces), np.vstack(uvs), tex, {"fixture": "plush"})


def hair_texture(size: int = 256, hair_rgb=(90, 50, 30), guide=GUIDE_GREY) -> Image.Image:
    """Atlas: left half hair colours (light/dark bands), right half the guide-grey head colour."""
    arr = np.zeros((size, size, 3), np.uint8)
    arr[:, : size // 2] = hair_rgb
    arr[: size // 4, : size // 2] = (np.array(hair_rgb) * 1.4).clip(0, 255).astype(np.uint8)
    arr[3 * size // 4:, : size // 2] = (np.array(hair_rgb) * 0.6).astype(np.uint8)
    arr[:, size // 2:] = guide
    return Image.fromarray(arr, "RGB")


def hair_with_head_fixture(*, fused: bool = True, head: float = 1.2, texture_px: int = 256, hair_rgb=(90, 50, 30)) -> MeshData:
    """A hair cap wrapped around a ``head`` stud guide cube, like a Tripo result of the "hair with head" front view.

    The cube sits centred at the origin (HairAttachment frame is applied by ``hair.register``). ``fused=True`` gives one
    closed surface (union of the cube and the hair); ``fused=False`` keeps two shells (hair shell around a cube shell).
    UVs: cube faces map into the right half of the texture (guide grey), hair into the left half.
    """
    h = head / 2
    cube = box_mesh([-h, -h, -h], [h, h, h], uv=(0.75, 0.5))
    t = 0.18        # hair thickness
    # hair = a rounded shell: box slightly larger on top/back/sides, open at the face-bottom (kept simple: a box cap)
    hair = box_mesh([-h - t, -h + 0.45, -h - t], [h + t, h + t + 0.25, h + t * 0.5], uv=(0.25, 0.5))
    back = box_mesh([-h - t, -h - 0.2, -h - t], [h + t, h + t, -h + 0.05], uv=(0.25, 0.4))   # hair falling down the back
    if fused:
        mesh = union_all([cube, hair, back])
    else:
        mesh = union_all([hair, back])
        verts = np.vstack([mesh.vertices, cube.vertices])
        faces = np.vstack([mesh.faces, cube.faces + len(mesh.vertices)])
        uv = np.vstack([mesh.uv, cube.uv])
        mesh = MeshData(verts, faces, uv)
    mesh.texture = hair_texture(texture_px, hair_rgb)
    mesh.meta["fixture"] = "hair_with_head"
    mesh.meta["head_size"] = head
    return mesh


def stud_scale(mesh: MeshData, factor: float) -> MeshData:
    out = mesh.copy()
    out.vertices = out.vertices * factor
    return out


def rotate_mesh(mesh: MeshData, matrix: np.ndarray) -> MeshData:
    """Rotate (and fix the winding if the matrix mirrors)."""
    out = mesh.copy()
    out.vertices = out.vertices @ np.asarray(matrix, float).T
    if np.linalg.det(matrix) < 0:
        out.faces = out.faces[:, ::-1].copy()
    return out


def tilt(mesh: MeshData, degrees: float, axis: int = 2) -> MeshData:
    a = math.radians(degrees)
    c, s = math.cos(a), math.sin(a)
    m = np.eye(3)
    i, j = [(1, 2), (0, 2), (0, 1)][axis]
    m[i, i], m[i, j], m[j, i], m[j, j] = c, -s, s, c
    return rotate_mesh(mesh, m)

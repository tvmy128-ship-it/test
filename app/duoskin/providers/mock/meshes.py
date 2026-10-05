"""Procedural meshes, textures and orthographic renders for the Tripo mock (and the sample GLBs in ``providers/fixtures/meshes``).

Everything is a pure function of its arguments. Meshes are built with numpy and ``trimesh``: textured triangle meshes
(about 2,000 triangles), one PBR material with a 1024 x 1024 base-colour PNG, Y up, the subject's front facing +Z
(the glTF convention). ``render_views`` is a small painter's-algorithm orthographic rasteriser (no GL needed) that draws
the four named views: ``front``, ``left`` (the SUBJECT's own left side, 90 degrees), ``back`` and ``right``.

Kinds: ``icosphere`` (a UV sphere), ``rounded_box``, ``one_sided_prop`` (a box with a nose on the front and a bump on the
right, so left/right and front/back are distinguishable), ``plush_pet`` (body, head, ears, feet) and ``hair_with_head``
(a hair-like shell with the grey cube head, ``#9A9A9A``, still attached, as Tripo returns for a hair request).
"""
from __future__ import annotations

import io
import math
import random
from pathlib import Path
from typing import Any

import numpy as np
import trimesh
from PIL import Image, ImageDraw

RGB = tuple[int, int, int]
KINDS = ("icosphere", "rounded_box", "one_sided_prop", "plush_pet", "hair_with_head")
GUIDE_GREY: RGB = (0x9A, 0x9A, 0x9A)
VIEW_YAW = {"front": 0.0, "left": -90.0, "back": 180.0, "right": 90.0}       # rotation of the object about +Y for each camera
Part = tuple[np.ndarray, np.ndarray, np.ndarray]                              # vertices, faces, uv


# --------------------------------------------------------------------------------------------------------------
# Parts
# --------------------------------------------------------------------------------------------------------------

def _orient_outward(v: np.ndarray, f: np.ndarray, center: np.ndarray) -> np.ndarray:
    """Flip triangles whose normal points toward ``center`` (all our parts are convex enough for this test)."""
    a, b, c = v[f[:, 0]], v[f[:, 1]], v[f[:, 2]]
    n = np.cross(b - a, c - a)
    cen = (a + b + c) / 3.0
    flip = (n * (cen - center)).sum(axis=1) < 0
    f = f.copy()
    f[flip] = f[flip][:, [0, 2, 1]]
    return f


def _drop_degenerate(v: np.ndarray, f: np.ndarray, uv: np.ndarray) -> np.ndarray:
    a, b, c = v[f[:, 0]], v[f[:, 1]], v[f[:, 2]]
    area = np.linalg.norm(np.cross(b - a, c - a), axis=1)
    return f[area > 1e-10]


def _rect_uv(uv: np.ndarray, rect: tuple[float, float, float, float]) -> np.ndarray:
    u0, v0, u1, v1 = rect
    return np.stack([u0 + uv[:, 0] * (u1 - u0), v0 + uv[:, 1] * (v1 - v0)], axis=1)


def uv_sphere(nu: int, nv: int, radius: float = 0.5, center: tuple[float, float, float] = (0, 0, 0),
              uv_rect: tuple[float, float, float, float] = (0, 0, 1, 1), theta_max: float = math.pi,
              scale: tuple[float, float, float] = (1, 1, 1)) -> Part:
    """A latitude/longitude sphere with a seam (duplicated vertices) so the UVs are clean; ``theta_max`` < pi gives a cap."""
    j, i = np.mgrid[0:nv + 1, 0:nu + 1]
    theta = theta_max * j / nv
    phi = 2 * math.pi * i / nu
    x = radius * np.sin(theta) * np.sin(phi) * scale[0]
    y = radius * np.cos(theta) * scale[1]
    z = radius * np.sin(theta) * np.cos(phi) * scale[2]
    v = np.stack([x, y, z], axis=-1).reshape(-1, 3) + np.array(center)
    uv = np.stack([i / nu, 1.0 - j / nv], axis=-1).reshape(-1, 2)
    idx = np.arange((nv + 1) * (nu + 1)).reshape(nv + 1, nu + 1)
    faces = []
    for jj in range(nv):
        for ii in range(nu):
            v00, v10, v01, v11 = idx[jj, ii], idx[jj, ii + 1], idx[jj + 1, ii], idx[jj + 1, ii + 1]
            faces += [(v00, v01, v11), (v00, v11, v10)]
    f = _drop_degenerate(v, np.array(faces), uv)
    f = _orient_outward(v, f, np.array(center) + np.array([0, 0.0, 0]))
    return v, f, _rect_uv(uv, uv_rect)


def grid_box(n: int, half: tuple[float, float, float] = (0.5, 0.5, 0.5), roundness: float = 0.35,
             center: tuple[float, float, float] = (0, 0, 0), uv_rect: tuple[float, float, float, float] = (0, 0, 1, 1)) -> Part:
    """A rounded box: six ``n x n`` grids (12 n^2 triangles), each face with its own UV tile of a 3 x 2 atlas."""
    verts, faces, uvs = [], [], []
    g = np.linspace(-1.0, 1.0, n + 1)
    a, b = np.meshgrid(g, g, indexing="ij")
    one = np.ones_like(a)
    sides = [(one, a, b), (-one, a, b), (a, one, b), (a, -one, b), (a, b, one), (a, b, -one)]
    base = 0
    for k, (x, y, z) in enumerate(sides):
        p = np.stack([x, y, z], axis=-1).reshape(-1, 3)
        sph = p / np.linalg.norm(p, axis=1, keepdims=True) * 1.15
        p = (1 - roundness) * p + roundness * sph
        p = p * np.array(half) + np.array(center)
        col, row = k % 3, k // 3
        u = (col + (a.reshape(-1) + 1) / 2) / 3.0
        w = (row + (b.reshape(-1) + 1) / 2) / 2.0
        idx = np.arange((n + 1) ** 2).reshape(n + 1, n + 1) + base
        for i in range(n):
            for j in range(n):
                p00, p10, p01, p11 = idx[i, j], idx[i + 1, j], idx[i, j + 1], idx[i + 1, j + 1]
                faces += [(p00, p10, p11), (p00, p11, p01)]
        verts.append(p)
        uvs.append(np.stack([u, w], axis=1))
        base += (n + 1) ** 2
    v = np.concatenate(verts)
    uv = np.concatenate(uvs)
    f = _orient_outward(v, np.array(faces), np.array(center))
    return v, f, _rect_uv(uv, uv_rect)


def _merge(parts: list[Part]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    vs, fs, uvs, off = [], [], [], 0
    for v, f, uv in parts:
        vs.append(v)
        fs.append(f + off)
        uvs.append(uv)
        off += len(v)
    return np.concatenate(vs), np.concatenate(fs), np.concatenate(uvs)


# --------------------------------------------------------------------------------------------------------------
# Texture
# --------------------------------------------------------------------------------------------------------------

def _tint(c: RGB, f: float) -> RGB:
    return (max(0, min(255, int(c[0] * f))), max(0, min(255, int(c[1] * f))), max(0, min(255, int(c[2] * f))))


def make_texture(kind: str, color: RGB, seed: int, size: int = 1024) -> Image.Image:
    """A flat-colour base-colour atlas with a few tinted blocks (so UV placement is visible). 1024 x 1024 RGB."""
    rng = random.Random(seed)
    im = Image.new("RGB", (size, size), color)
    d = ImageDraw.Draw(im)
    accent = _tint(color, 0.62) if sum(color) > 300 else _tint(color, 1.5)
    if kind == "hair_with_head":
        d.rectangle([0, 0, size // 4 - 1, size - 1], fill=GUIDE_GREY)                  # the grey cube head tile
        for k in range(8):
            x0 = size // 4 + k * (size * 3 // 4) // 8
            d.rectangle([x0, 0, x0 + (size * 3 // 4) // 16, size - 1], fill=_tint(color, 0.85 if k % 2 else 1.0))
        return im
    if kind in ("plush_pet", "one_sided_prop"):
        d.rectangle([int(size * 0.7), 0, size - 1, size - 1], fill=accent)           # the secondary-colour strip
    for _ in range(6):
        x0, y0 = rng.randrange(0, size - 200), rng.randrange(0, size - 200)
        d.rectangle([x0, y0, x0 + rng.randrange(60, 200), y0 + rng.randrange(60, 200)], fill=_tint(color, rng.choice([0.8, 1.15])))
    d.rectangle([0, 0, size - 1, 8], fill=accent)                                    # a marker row at v = 1 (the top edge)
    return im


# --------------------------------------------------------------------------------------------------------------
# Meshes
# --------------------------------------------------------------------------------------------------------------

def build_mesh(kind: str, color: RGB = (180, 90, 60), seed: int = 0) -> trimesh.Trimesh:
    """The textured mesh of ``kind`` (one material, one 1024 x 1024 texture)."""
    if kind not in KINDS:
        raise ValueError(f"unknown mesh kind {kind!r}; known: {', '.join(KINDS)}")
    if kind == "icosphere":
        parts = [uv_sphere(32, 32, 0.5)]
    elif kind == "rounded_box":
        parts = [grid_box(13, (0.5, 0.45, 0.4), 0.38)]
    elif kind == "one_sided_prop":
        body = grid_box(9, (0.36, 0.5, 0.24), 0.22, uv_rect=(0, 0, 0.7, 1))
        nose = uv_sphere(14, 10, 0.13, (0.0, 0.0, 0.3), (0.7, 0.0, 1.0, 0.5))
        bump = uv_sphere(14, 10, 0.09, (0.38, 0.15, 0.0), (0.7, 0.5, 1.0, 1.0))
        parts = [body, nose, bump]
    elif kind == "plush_pet":
        main = (0.0, 0.0, 0.7, 1.0)
        strip = (0.7, 0.0, 1.0, 1.0)
        parts = [uv_sphere(24, 16, 0.35, (0, 0.38, 0), main), uv_sphere(24, 16, 0.28, (0, 0.82, 0.05), main),
                 uv_sphere(10, 8, 0.1, (-0.17, 1.04, 0.0), strip), uv_sphere(10, 8, 0.1, (0.17, 1.04, 0.0), strip)]
        # the feet are big enough and sit deep enough that no more than ~4% of the surface is thinner than 0.05 stud at 0.8 stud and up (CHK-M04
        # allows 5%): with the old 0.1 feet a charm-sized plush pet failed the mesh gate on every Tripo try and the duo waited for a manual model
        parts += [uv_sphere(12, 8, 0.16, (sx * 0.2, 0.14, sz * 0.2), strip) for sx in (-1, 1) for sz in (-1, 1)]
    else:  # hair_with_head
        head = grid_box(8, (0.5, 0.5, 0.5), 0.0, (0, 0, 0), (0.0, 0.0, 0.25, 1.0))
        hair_rect = (0.25, 0.0, 1.0, 1.0)
        cap = uv_sphere(28, 14, 0.66, (0, 0.05, -0.02), hair_rect, theta_max=math.pi * 0.62)
        fringe = grid_box(6, (0.5, 0.1, 0.1), 0.2, (0, 0.5, 0.52), hair_rect)
        locks = [grid_box(5, (0.08, 0.35, 0.28), 0.25, (sx * 0.62, -0.1, -0.05), hair_rect) for sx in (-1, 1)]
        parts = [head, cap, fringe, *locks]
    v, f, uv = _merge(parts)
    v = v - np.array([0.0, 0.0, 0.0])
    tex = make_texture(kind, color, seed)
    mat = trimesh.visual.material.PBRMaterial(name="mock_material", baseColorTexture=tex, metallicFactor=0.0, roughnessFactor=1.0)
    return trimesh.Trimesh(vertices=v, faces=f, visual=trimesh.visual.TextureVisuals(uv=uv, material=mat), process=False)


def build_glb(kind: str, color: RGB = (180, 90, 60), seed: int = 0) -> bytes:
    """The mesh as binary glTF (``glTF`` magic bytes); deterministic."""
    return bytes(build_mesh(kind, color, seed).export(file_type="glb"))


def mesh_stats(glb: bytes) -> dict[str, Any]:
    scene = trimesh.load(io.BytesIO(glb), file_type="glb", force="scene")
    geoms = list(scene.geometry.values())
    faces = sum(len(g.faces) for g in geoms)
    tex = geoms[0].visual.material.baseColorTexture if geoms else None
    return {"triangles": faces, "geometries": len(geoms), "texture_size": tex.size if tex is not None else None,
            "materials": len({id(g.visual.material) for g in geoms})}


# --------------------------------------------------------------------------------------------------------------
# Orthographic views
# --------------------------------------------------------------------------------------------------------------

def render_views(mesh: trimesh.Trimesh, size: int = 768, background: RGB = (255, 255, 255),
                 views: tuple[str, ...] = ("front", "left", "back", "right")) -> dict[str, bytes]:
    """The four named orthographic views as PNG bytes: same scale, same ground line, object centred; flat Lambert shading
    from the camera, textures sampled at triangle centres. ``left`` is the subject's own left side (camera at +X)."""
    v = np.asarray(mesh.vertices, dtype=np.float64)
    f = np.asarray(mesh.faces)
    uv = np.asarray(mesh.visual.uv)
    tex = np.asarray(mesh.visual.material.baseColorTexture.convert("RGB"))
    th, tw = tex.shape[:2]
    lo, hi = v.min(axis=0), v.max(axis=0)
    c = (lo + hi) / 2
    radius = max(float(np.linalg.norm((v - c)[:, [0, 2]], axis=1).max()), float(hi[1] - lo[1]) / 2)
    scale = 0.44 * size / max(radius, 1e-6)
    out: dict[str, bytes] = {}
    tri_uv = uv[f].mean(axis=1)
    px = tex[np.clip(((1.0 - tri_uv[:, 1]) * (th - 1)).astype(int), 0, th - 1), np.clip((tri_uv[:, 0] * (tw - 1)).astype(int), 0, tw - 1)]
    for name in views:
        a = math.radians(VIEW_YAW[name])
        ca, sa = math.cos(a), math.sin(a)
        p = v - c
        x = p[:, 0] * ca + p[:, 2] * sa
        z = -p[:, 0] * sa + p[:, 2] * ca
        y = p[:, 1] + (c[1] - (lo[1] + hi[1]) / 2)
        tri = f
        a3, b3, c3 = np.stack([x, y, z], 1)[tri[:, 0]], np.stack([x, y, z], 1)[tri[:, 1]], np.stack([x, y, z], 1)[tri[:, 2]]
        n = np.cross(b3 - a3, c3 - a3)
        nl = np.linalg.norm(n, axis=1)
        nz = np.where(nl > 0, n[:, 2] / np.maximum(nl, 1e-12), 0)
        facing = nz > 0                                                           # back-face culling (camera looks down -Z)
        order = np.argsort(((a3[:, 2] + b3[:, 2] + c3[:, 2]) / 3)[facing])         # far to near
        idx = np.nonzero(facing)[0][order]
        im = Image.new("RGB", (size, size), background)
        d = ImageDraw.Draw(im)
        sx = size / 2 + x * scale
        sy = size * 0.5 - y * scale
        for t in idx:
            k = 0.35 + 0.65 * float(nz[t])
            col = tuple(int(max(0, min(255, ch * k))) for ch in px[t])
            d.polygon([(sx[tri[t, 0]], sy[tri[t, 0]]), (sx[tri[t, 1]], sy[tri[t, 1]]), (sx[tri[t, 2]], sy[tri[t, 2]])], fill=col)  # type: ignore[arg-type]
        buf = io.BytesIO()
        im.save(buf, "PNG")
        out[name] = buf.getvalue()
    return out


def render_glb_views(glb: bytes, size: int = 768) -> dict[str, bytes]:
    scene = trimesh.load(io.BytesIO(glb), file_type="glb", force="scene")
    mesh = trimesh.util.concatenate(list(scene.geometry.values())) if len(scene.geometry) > 1 else next(iter(scene.geometry.values()))
    if not isinstance(mesh.visual, trimesh.visual.TextureVisuals):
        raise TypeError("render_glb_views needs a textured mesh")
    return render_views(mesh, size)


# --------------------------------------------------------------------------------------------------------------
# Sample files for providers/fixtures/meshes (written once by ``write_samples``; tests and demos import them)
# --------------------------------------------------------------------------------------------------------------

SAMPLES: dict[str, tuple[str, RGB, int]] = {
    "icosphere_2k.glb": ("icosphere", (70, 130, 200), 1),
    "rounded_box_2k.glb": ("rounded_box", (200, 120, 60), 2),
    "one_sided_prop.glb": ("one_sided_prop", (90, 170, 110), 3),
    "plush_pet.glb": ("plush_pet", (230, 170, 190), 4),
    "hair_with_grey_head.glb": ("hair_with_head", (120, 70, 40), 5),
}


def write_samples(directory: Path | str) -> list[Path]:
    """(Re)write the sample GLBs. Deterministic: running it twice gives identical files."""
    out = Path(directory)
    out.mkdir(parents=True, exist_ok=True)
    paths = []
    for name, (kind, color, seed) in SAMPLES.items():
        p = out / name
        p.write_bytes(build_glb(kind, color, seed))
        paths.append(p)
    return paths


# --------------------------------------------------------------------------------------------------------------
# Other formats for the mock ``convert`` endpoint
# --------------------------------------------------------------------------------------------------------------

def to_obj_zip(mesh: trimesh.Trimesh) -> bytes:
    """A ZIP (``PK``) with ``model.obj``, ``model.mtl`` and ``texture.png``."""
    import zipfile
    v, f, uv = np.asarray(mesh.vertices), np.asarray(mesh.faces), np.asarray(mesh.visual.uv)
    lines = ["mtllib model.mtl", "o mock"]
    lines += [f"v {x:.6f} {y:.6f} {z:.6f}" for x, y, z in v]
    lines += [f"vt {a:.6f} {b:.6f}" for a, b in uv]
    lines += ["usemtl mock_material"]
    lines += [f"f {a + 1}/{a + 1} {b + 1}/{b + 1} {c + 1}/{c + 1}" for a, b, c in f]
    buf = io.BytesIO()
    tex = io.BytesIO()
    mesh.visual.material.baseColorTexture.save(tex, "PNG")
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in (("model.obj", "\n".join(lines) + "\n"), ("model.mtl", "newmtl mock_material\nKd 1 1 1\nmap_Kd texture.png\n")):
            zi = zipfile.ZipInfo(name, (2026, 1, 1, 0, 0, 0))
            z.writestr(zi, data)
        zi = zipfile.ZipInfo("texture.png", (2026, 1, 1, 0, 0, 0))
        z.writestr(zi, tex.getvalue())
    return buf.getvalue()


def fake_fbx(mesh: trimesh.Trimesh) -> bytes:
    """Bytes with the binary-FBX magic (``Kaydara FBX Binary``) so magic-byte checks pass. NOT a loadable FBX: the mock cannot
    write one. Tests that need a real FBX use Blender fixtures instead."""
    return b"Kaydara FBX Binary  \x00\x1a\x00" + (7400).to_bytes(4, "little") + b"MOCK-FBX:" + str(len(mesh.faces)).encode() + b"\x00" * 64

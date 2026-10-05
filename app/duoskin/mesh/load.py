"""Mesh loading (APP_SPEC 10.9 step 1, FAILURE_MODES MESH-13, MESH-14, ACC-12, CHK-M01).

* The type comes from the file's magic bytes, never from the suffix.
* glTF/GLB are read with ``trimesh`` (scene graph, node transforms baked, quads/N-gons triangulated, skins ignored) after a
  JSON pre-scan that refuses ``extensionsRequired`` we cannot decode: meshopt, Draco (unless DracoPy is installed), KTX2/WebP
  textures and anything unknown.
* FBX, ``.blend`` and zipped OBJ need headless Blender; without it the answer is the plain sentence
  "Blender not installed: export GLB instead".
* Several primitives or materials are merged into 1 mesh, 1 material and 1 UV set (a grid atlas when the textures differ).

All of this belongs in the mesh worker subprocess: a malformed file can crash native code.
"""
from __future__ import annotations

import hashlib
import io
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from duoskin.mesh import gltf_io
from duoskin.mesh.types import MeshData, MeshError

BLENDER_MISSING = "Blender not installed: export GLB instead (Blender is needed to read FBX, .blend and zipped OBJ files)."
MAX_FILE_BYTES = 50 * 1024 * 1024

_MESHOPT = {"EXT_meshopt_compression", "KHR_meshopt_compression"}
_DRACO = {"KHR_draco_mesh_compression"}
_IMAGE_EXT = {"KHR_texture_basisu", "EXT_texture_webp", "EXT_texture_avif"}


@dataclass
class LoadedMesh:
    mesh: MeshData
    facts: dict[str, Any] = field(default_factory=dict)
    messages: list[str] = field(default_factory=list)


def sniff(path: str | Path) -> str:
    """File kind from magic bytes: ``glb gltf fbx zip blend obj stl ply png jpeg webp unknown``."""
    p = Path(path)
    with open(p, "rb") as fh:
        head = fh.read(512)
    if head[:4] == b"glTF":
        return "glb"
    if head.startswith(b"Kaydara FBX Binary"):
        return "fbx"
    if head.startswith(b"; FBX"):
        return "fbx"
    if head[:4] in (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"):
        return "zip"
    if head.startswith(b"BLENDER"):
        return "blend"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    if head.startswith((b"ply\n", b"ply\r\n")):
        return "ply"
    text = head.lstrip(b"\xef\xbb\xbf \t\r\n")
    if text.startswith(b"{"):
        try:
            with open(p, "rb") as fh:
                doc = json.loads(fh.read(MAX_FILE_BYTES + 1).decode("utf-8-sig"))
            if isinstance(doc, dict) and "asset" in doc:
                return "gltf"
        except (ValueError, UnicodeDecodeError):
            return "unknown"
        return "unknown"
    if text.startswith(b"solid"):
        return "stl"
    size = p.stat().st_size
    if size >= 84:
        with open(p, "rb") as fh:
            fh.seek(80)
            n = int.from_bytes(fh.read(4), "little")
        if 84 + 50 * n == size:
            return "stl"
    try:
        sample = head.decode("utf-8")
    except UnicodeDecodeError:
        return "unknown"
    lines = [ln.strip() for ln in sample.splitlines() if ln.strip() and not ln.startswith("#")]
    if lines and all(ln.split()[0] in ("v", "vt", "vn", "f", "o", "g", "s", "mtllib", "usemtl", "l") for ln in lines[:8]) \
            and any(ln.startswith("v ") for ln in lines):
        return "obj"
    return "unknown"


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def check_extensions(doc: dict[str, Any]) -> list[str]:
    """Plain-language problems with ``extensionsRequired``/``extensionsUsed``; empty when the file can be read."""
    problems: list[str] = []
    req = set(doc.get("extensionsRequired", []))
    used = set(doc.get("extensionsUsed", []))
    if (req | used) & _MESHOPT:
        problems.append("The file uses meshopt compression, which cannot be read here: re-export without compression "
                        "(in Tripo choose 'No compression', in Blender keep the glTF compression box unticked).")
    if (req | used) & _DRACO:
        try:
            import DracoPy  # noqa: F401
        except ImportError:
            problems.append("The file uses Draco compression and the optional DracoPy package is not installed: "
                            "re-export without compression.")
    if req & _IMAGE_EXT:
        problems.append("The file needs a KTX2/WebP/AVIF texture extension: re-export with a PNG texture.")
    unknown = sorted(req - _MESHOPT - _DRACO - _IMAGE_EXT)
    if unknown:
        problems.append("The file requires glTF extensions that cannot be read (" + ", ".join(unknown) + "): re-export as a plain GLB.")
    return problems


# --------------------------------------------------------------------------------------------------------------------
# glTF / GLB through trimesh
# --------------------------------------------------------------------------------------------------------------------
def _material_info(geom) -> tuple[Image.Image | None, tuple[float, float, float, float], bool]:
    """(base colour image, base colour factor 0-1, has_vertex_colours) of a trimesh geometry."""
    vis = getattr(geom, "visual", None)
    img, factor = None, (1.0, 1.0, 1.0, 1.0)
    mat = getattr(vis, "material", None)
    if mat is not None:
        img = getattr(mat, "baseColorTexture", None) or getattr(mat, "image", None)
        fac = getattr(mat, "baseColorFactor", None)
        if fac is None and img is None and getattr(mat, "main_color", None) is not None:       # a flat colour; with a texture, no factor in the file means white (glTF 2.0),
            fac = np.asarray(mat.main_color, float) / 255.0                                  # and trimesh's main_color is then the mean of the texture, not a factor
        if fac is not None:
            f = np.asarray(fac, float).reshape(-1)
            if f.max() > 1.5:
                f = f / 255.0
            factor = tuple(float(x) for x in (list(f) + [1.0] * 4)[:4])
    kind = getattr(vis, "kind", None)
    return img, factor, kind == "vertex"


def _scene_parts(scene) -> list[dict[str, Any]]:
    """One record per geometry instance, vertices already in world space."""
    parts = []
    graph = scene.graph
    for node in graph.nodes_geometry:
        transform, gname = graph[node]
        geom = scene.geometry[gname]
        if not hasattr(geom, "faces") or len(geom.faces) == 0:
            continue
        v = np.asarray(geom.vertices, float)
        v = v @ np.asarray(transform)[:3, :3].T + np.asarray(transform)[:3, 3]
        f = np.asarray(geom.faces, np.int64).copy()
        if np.linalg.det(np.asarray(transform)[:3, :3]) < 0:
            f = f[:, ::-1].copy()
        uv = None
        vis = geom.visual
        if getattr(vis, "uv", None) is not None and len(vis.uv) == len(v):
            uv = np.asarray(vis.uv, float).copy()
            uv[:, 1] = 1.0 - uv[:, 1]          # trimesh keeps OpenGL UVs (v up); we keep glTF/image UVs (v down)
        img, factor, vcol = _material_info(geom)
        parts.append({"v": v, "f": f, "uv": uv, "img": img, "factor": factor, "vertex_colours": vcol, "name": gname})
    return parts


def _same_image(a: Image.Image | None, b: Image.Image | None) -> bool:
    if a is None or b is None:
        return a is b
    return a is b or (a.size == b.size and a.mode == b.mode and a.tobytes() == b.tobytes())


def merge_parts(parts: list[dict[str, Any]], messages: list[str], atlas_px: int = 2048) -> MeshData:
    """Merge several primitives into one mesh with one UV set; distinct textures go into a grid atlas."""
    if not parts:
        raise MeshError("empty", "the file contains no triangles")
    # unique textures (or flat colours)
    keys: list[tuple[Image.Image | None, tuple]] = []
    part_key: list[int] = []
    for p in parts:
        fac = tuple(round(x, 4) for x in p["factor"][:3])
        found = None
        for i, (img, kfac) in enumerate(keys):
            if _same_image(img, p["img"]) and (p["img"] is not None or kfac == fac):
                found = i
                break
        if found is None:
            keys.append((p["img"], fac))
            found = len(keys) - 1
        part_key.append(found)
    n_tex = len(keys)
    verts, faces, uvs, off = [], [], [], 0
    if n_tex == 1:
        img = keys[0][0]
        tex = img.convert("RGBA") if img is not None else Image.new("RGB", (4, 4), tuple(int(c * 255) for c in keys[0][1]))
        fac = parts[0]["factor"]
        if img is not None and any(abs(c - 1.0) > 1e-3 for c in fac[:3]):
            arr = np.asarray(tex, np.float64)
            arr[..., :3] *= np.array(fac[:3])
            tex = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8), tex.mode)
            messages.append("baseColorFactor was multiplied into the texture")
        for p in parts:
            verts.append(p["v"])
            faces.append(p["f"] + off)
            uvs.append(p["uv"] if p["uv"] is not None else np.zeros((len(p["v"]), 2)))
            off += len(p["v"])
        has_uv = all(p["uv"] is not None for p in parts)
        return MeshData(np.vstack(verts), np.vstack(faces), np.vstack(uvs) if has_uv else None, tex,
                        {"materials_merged": 1, "has_uv": has_uv})
    # several materials: grid atlas
    cols = math.ceil(math.sqrt(n_tex))
    rows = math.ceil(n_tex / cols)
    cell = max(64, atlas_px // max(cols, rows))
    atlas = Image.new("RGB", (cols * cell, rows * cell), (128, 128, 128))
    for i, (img, fac) in enumerate(keys):
        c, r = i % cols, i // cols
        if img is None:
            tile = Image.new("RGB", (cell, cell), tuple(round(x * 255) for x in fac))
        else:
            tile = img.convert("RGB").resize((cell, cell), Image.Resampling.LANCZOS)
        atlas.paste(tile, (c * cell, r * cell))
    for p, k in zip(parts, part_key, strict=True):
        c, r = k % cols, k // cols
        uv = p["uv"]
        if uv is None:
            if keys[k][0] is not None:
                raise MeshError("no_uv", "a textured part of the file has no UV coordinates")
            uv = np.full((len(p["v"]), 2), 0.5)
        else:
            f_uv = uv[p["f"]]
            if uv.min() < -1e-6 or uv.max() > 1 + 1e-6:
                span = np.floor(f_uv.min(axis=1))[:, None, :]
                if np.any(np.floor(f_uv.max(axis=1) - 1e-9) != span[:, 0, :]):
                    raise MeshError("tiled_uv", "UVs outside 0-1 cross tile borders and cannot be merged into one atlas: "
                                    "bake the textures in your 3D tool (one material, UVs in 0-1) and export again")
                uv = uv.copy()
                # per-face wrap is not representable per vertex; only the common case (whole mesh shifted by an integer) is handled
                uv = uv - np.floor(uv.min(axis=0))
        uv = uv.copy()
        uv[:, 0] = (c + np.clip(uv[:, 0], 0, 1)) / cols
        uv[:, 1] = (r + np.clip(uv[:, 1], 0, 1)) / rows
        verts.append(p["v"])
        faces.append(p["f"] + off)
        uvs.append(uv)
        off += len(p["v"])
    messages.append(f"{n_tex} materials were merged into one {atlas.size[0]} px atlas (built-in grid atlas)")
    return MeshData(np.vstack(verts), np.vstack(faces), np.vstack(uvs), atlas, {"materials_merged": n_tex, "has_uv": True})


def _load_trimesh_scene(path: Path, file_type: str | None = None):
    import trimesh

    kwargs: dict[str, Any] = {"force": "scene", "process": False}
    if file_type:
        kwargs["file_type"] = file_type
    return trimesh.load(str(path), **kwargs)


def load_gltf(path: str | Path) -> LoadedMesh:
    """Load a ``.glb`` or ``.gltf`` (with its ``.bin`` and image next to it)."""
    p = Path(path)
    if p.stat().st_size > MAX_FILE_BYTES:
        raise MeshError("too_big", f"the file is larger than {MAX_FILE_BYTES // (1024 * 1024)} MB")
    doc, _ = gltf_io.read_gltf_json(p)
    problems = check_extensions(doc)
    if problems:
        raise MeshError("unsupported_extension", " ".join(problems))
    facts = gltf_io.gltf_structure_facts(p)
    if p.suffix.lower() == ".gltf":
        bad = gltf_io.check_gltf_files(p)
        if bad:
            raise MeshError("missing_files", "The .gltf refers to files that are not next to it (" + "; ".join(bad)
                            + "). Export a single .glb instead.")
    messages: list[str] = []
    scene = _load_trimesh_scene(p, "glb" if sniff(p) == "glb" else "gltf")
    parts = _scene_parts(scene) if hasattr(scene, "graph") else []
    mesh = merge_parts(parts, messages)
    if facts.get("skins"):
        messages.append("armature/skin data was ignored (rigid accessories are never skinned)")
    if facts.get("animations"):
        messages.append("animations were ignored")
    mesh.meta.update({"source_format": "glb" if sniff(p) == "glb" else "gltf", "source_primitives": len(parts)})
    facts.update({"file_type": mesh.meta["source_format"], "n_parts": len(parts), "materials_merged": mesh.meta.get("materials_merged", 1),
                  "bytes": p.stat().st_size, "has_uv": mesh.uv is not None, "vertex_colours_present": any(x["vertex_colours"] for x in parts)})
    return LoadedMesh(mesh, facts, messages)


def load_obj(path: str | Path) -> LoadedMesh:
    """Plain OBJ (+ MTL + texture next to it) through trimesh. Used only when Blender is not available."""
    p = Path(path)
    scene = _load_trimesh_scene(p, "obj")
    messages = ["read with the built-in OBJ reader (Blender not used); check the texture and orientation"]
    parts = _scene_parts(scene) if hasattr(scene, "graph") else []
    mesh = merge_parts(parts, messages)
    mesh.meta["source_format"] = "obj"
    facts = {"file_type": "obj", "n_parts": len(parts), "bytes": p.stat().st_size, "has_uv": mesh.uv is not None,
             "materials_merged": mesh.meta.get("materials_merged", 1)}
    return LoadedMesh(mesh, facts, messages)


def load_mesh(path: str | Path, *, blender: str | None = None, workdir: str | Path | None = None) -> LoadedMesh:
    """Load any supported mesh file into one ``MeshData``.

    ``blender`` is the path of a Blender executable (None = auto-detect). FBX, ``.blend`` and zipped OBJ are converted
    to GLB by headless Blender into ``workdir`` first; without Blender a ``MeshError('blender_missing', ...)`` explains
    what to do instead.
    """
    p = Path(path)
    if not p.is_file():
        raise MeshError("not_found", f"file not found: {p.name}")
    kind = sniff(p)
    if kind in ("glb", "gltf"):
        return load_gltf(p)
    if kind in ("fbx", "blend", "zip"):
        from duoskin.mesh import blender as bl

        exe = bl.find_blender(blender or "")
        if kind == "zip":
            raise MeshError("zip_not_extracted", "a ZIP must be extracted first (mesh_import.import_file does this safely)")
        if exe is None:
            raise MeshError("blender_missing", BLENDER_MISSING)
        out_dir = Path(workdir) if workdir else p.parent
        res = bl.import_to_glb(exe, p, out_dir / f"{p.stem}.imported.glb")
        if not res.ok:
            raise MeshError("blender_failed", f"Blender could not read the file: {res.error}")
        loaded = load_gltf(res.files["glb"])
        loaded.messages.insert(0, f"converted from {kind.upper()} by headless Blender {res.version}")
        loaded.facts["file_type"] = kind
        loaded.facts["converted_by"] = "blender"
        return loaded
    if kind == "obj":
        from duoskin.mesh import blender as bl

        exe = bl.find_blender(blender or "")
        if exe is None:
            return load_obj(p)
        out_dir = Path(workdir) if workdir else p.parent
        res = bl.import_to_glb(exe, p, out_dir / f"{p.stem}.imported.glb")
        if res.ok:
            loaded = load_gltf(res.files["glb"])
            loaded.facts["file_type"] = "obj"
            loaded.facts["converted_by"] = "blender"
            return loaded
        return load_obj(p)
    if kind in ("stl", "ply"):
        raise MeshError("no_texture_format", f"{kind.upper()} files carry no texture: export a GLB with the texture from your 3D tool")
    raise MeshError("unknown_format", "this file type is not a 3D model we can read: use a .glb (preferred), .gltf with its .bin and "
                    "texture, .fbx (needs Blender) or a .zip of those")


def image_to_bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()

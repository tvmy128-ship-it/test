"""Mesh export (APP_SPEC 10.9 step 10, S6): ``.gltf`` + ``.bin`` + PNG with relative URIs (primary), a ``.glb`` archive copy,
and ``.fbx`` through Blender when it is installed.

All files are in studs, Y up, front +Z. The glTF is written by hand so every Roblox rule is explicit: exactly 1 scene node,
1 mesh, 1 primitive, 1 material (``OPAQUE``, ``metallicFactor`` written as 0, no emissive, no metal/rough map), 1 UV set, no
``COLOR_0``, no extensions, an RGB PNG texture addressed by a bare file name.
"""
from __future__ import annotations

import json
import struct
from pathlib import Path
from typing import Any

import numpy as np

from duoskin.checks.model import CheckResult, not_applicable, not_run
from duoskin.mesh import texture as tx
from duoskin.mesh.gltf_io import check_gltf_files, gltf_structure_facts, read_gltf_json  # noqa: F401  (re-exported)
from duoskin.mesh.geometry import face_normals_areas, weld
from duoskin.mesh.types import MeshData, MeshError

GENERATOR = "DuoSkin Studio mesh export"


def smooth_vertex_normals(mesh: MeshData, seam_angle_deg: float = 50.0) -> np.ndarray:
    """Per-vertex normals: area-weighted over the vertex's own faces, then blended across UV seams (same position) when
    the surfaces meet at less than ``seam_angle_deg``. Hard edges that are not seams stay hard."""
    v, f = mesh.vertices, mesh.faces
    fn, area = face_normals_areas(v, f)
    own = np.zeros_like(v)
    w = fn * area[:, None]
    for k in range(3):
        np.add.at(own, f[:, k], w)
    ln = np.linalg.norm(own, axis=1, keepdims=True)
    own = np.where(ln > 1e-18, own / np.maximum(ln, 1e-18), np.array([0.0, 1.0, 0.0]))
    welded = weld(v, f)
    groups = welded.vmap
    counts = np.bincount(groups, minlength=len(welded.vertices))
    out = own.copy()
    multi = np.nonzero(counts[groups] > 1)[0]
    if len(multi):
        cos_lim = np.cos(np.radians(seam_angle_deg))
        # faces per welded group
        face_group = groups[f]                       # (M, 3)
        by_group: dict[int, list[int]] = {}
        for fi in range(len(f)):
            for g in set(face_group[fi].tolist()):
                if counts[g] > 1:
                    by_group.setdefault(g, []).append(fi)
        for i in multi.tolist():
            g = int(groups[i])
            fs = np.array(by_group.get(g, []), np.int64)
            if len(fs) == 0:
                continue
            ok = (fn[fs] @ own[i]) >= cos_lim
            acc = (fn[fs[ok]] * area[fs[ok], None]).sum(axis=0)
            n = np.linalg.norm(acc)
            if n > 1e-18:
                out[i] = acc / n
    return out


def _pad4(b: bytes, fill: bytes = b"\x00") -> bytes:
    return b + fill * ((4 - len(b) % 4) % 4)


def build_gltf(mesh: MeshData, *, png_bytes: bytes | None, bin_uri: str | None, png_uri: str | None, name: str = "accessory",
               extras: dict[str, Any] | None = None, glb: bool = False) -> tuple[dict[str, Any], bytes]:
    """The glTF JSON and the binary buffer. With ``glb=True`` the PNG is appended to the buffer as a bufferView."""
    if mesh.uv is None or mesh.texture is None or png_bytes is None:
        raise MeshError("no_texture", "the mesh needs UVs and a texture to be exported")
    v = mesh.vertices.astype(np.float32)
    n = smooth_vertex_normals(mesh).astype(np.float32)
    uv = mesh.uv.astype(np.float32)
    idx_dtype = np.uint16 if len(v) < 65535 else np.uint32
    idx = mesh.faces.astype(idx_dtype).reshape(-1)
    chunks: list[tuple[str, bytes]] = [("pos", v.tobytes()), ("nrm", n.tobytes()), ("uv", uv.tobytes()), ("idx", idx.tobytes())]
    offsets, blob = {}, b""
    for key, data in chunks:
        blob = _pad4(blob)
        offsets[key] = (len(blob), len(data))
        blob += data
    views = [
        {"buffer": 0, "byteOffset": offsets["pos"][0], "byteLength": offsets["pos"][1], "target": 34962},
        {"buffer": 0, "byteOffset": offsets["nrm"][0], "byteLength": offsets["nrm"][1], "target": 34962},
        {"buffer": 0, "byteOffset": offsets["uv"][0], "byteLength": offsets["uv"][1], "target": 34962},
        {"buffer": 0, "byteOffset": offsets["idx"][0], "byteLength": offsets["idx"][1], "target": 34963},
    ]
    image: dict[str, Any]
    if glb:
        blob = _pad4(blob)
        views.append({"buffer": 0, "byteOffset": len(blob), "byteLength": len(png_bytes)})
        blob += png_bytes
        image = {"bufferView": 4, "mimeType": "image/png", "name": "albedo"}
    else:
        image = {"uri": png_uri, "name": "albedo"}
    blob = _pad4(blob)
    lo, hi = v.min(axis=0).astype(float).tolist(), v.max(axis=0).astype(float).tolist()
    node: dict[str, Any] = {"name": name, "mesh": 0}
    if extras:
        node["extras"] = {"duoskin": extras}
    doc: dict[str, Any] = {
        "asset": {"version": "2.0", "generator": GENERATOR},
        "scene": 0,
        "scenes": [{"name": "scene", "nodes": [0]}],
        "nodes": [node],
        "meshes": [{"name": name, "primitives": [{
            "attributes": {"POSITION": 0, "NORMAL": 1, "TEXCOORD_0": 2}, "indices": 3, "material": 0, "mode": 4}]}],
        "materials": [{"name": "albedo", "alphaMode": "OPAQUE", "doubleSided": False,
                       "pbrMetallicRoughness": {"baseColorTexture": {"index": 0}, "baseColorFactor": [1.0, 1.0, 1.0, 1.0],
                                                "metallicFactor": 0.0, "roughnessFactor": 1.0}}],
        "textures": [{"sampler": 0, "source": 0}],
        "samplers": [{"magFilter": 9729, "minFilter": 9729, "wrapS": 33071, "wrapT": 33071}],
        "images": [image],
        "accessors": [
            {"bufferView": 0, "componentType": 5126, "count": len(v), "type": "VEC3", "min": lo, "max": hi},
            {"bufferView": 1, "componentType": 5126, "count": len(v), "type": "VEC3"},
            {"bufferView": 2, "componentType": 5126, "count": len(v), "type": "VEC2"},
            {"bufferView": 3, "componentType": 5123 if idx_dtype == np.uint16 else 5125, "count": int(len(idx)), "type": "SCALAR",
             "min": [int(idx.min())] if len(idx) else [0], "max": [int(idx.max())] if len(idx) else [0]},
        ],
        "bufferViews": views,
        "buffers": [{"byteLength": len(blob)} if glb else {"byteLength": len(blob), "uri": bin_uri}],
    }
    return doc, blob


def png_for_export(mesh: MeshData) -> bytes:
    if mesh.texture is None:
        raise MeshError("no_texture", "the mesh has no texture")
    return tx.png_bytes(tx.as_pil(mesh.texture))


def default_extras(mesh: MeshData, extras: dict[str, Any] | None) -> dict[str, Any]:
    e = {"units": "studs", "up": "+Y", "front": "+Z", "scale_type": "Classic"}
    for key in ("attachment_offset", "attachment", "asset_type", "asset_id"):
        if key in mesh.meta:
            val = mesh.meta[key]
            e[key] = np.asarray(val).tolist() if isinstance(val, np.ndarray) else val
    e.update(extras or {})
    return e


def write_gltf_set(mesh: MeshData, out_dir: str | Path, stem: str, *, extras: dict[str, Any] | None = None) -> dict[str, str]:
    """``<stem>.gltf``, ``<stem>.bin`` and ``<stem>_albedo.png`` in ``out_dir``. Returns ``{"gltf", "bin", "png"}`` paths."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    png = png_for_export(mesh)
    doc, blob = build_gltf(mesh, png_bytes=png, bin_uri=f"{stem}.bin", png_uri=f"{stem}_albedo.png", name=stem,
                           extras=default_extras(mesh, extras))
    paths = {"gltf": out / f"{stem}.gltf", "bin": out / f"{stem}.bin", "png": out / f"{stem}_albedo.png"}
    paths["bin"].write_bytes(blob)
    paths["png"].write_bytes(png)
    paths["gltf"].write_text(json.dumps(doc, indent=1), encoding="utf-8")
    return {k: str(p) for k, p in paths.items()}


def glb_bytes(mesh: MeshData, *, name: str = "accessory", extras: dict[str, Any] | None = None) -> bytes:
    png = png_for_export(mesh)
    doc, blob = build_gltf(mesh, png_bytes=png, bin_uri=None, png_uri=None, name=name, extras=default_extras(mesh, extras), glb=True)
    js = _pad4(json.dumps(doc, separators=(",", ":")).encode("utf-8"), b" ")
    total = 12 + 8 + len(js) + 8 + len(blob)
    return b"".join([struct.pack("<4sII", b"glTF", 2, total), struct.pack("<I4s", len(js), b"JSON"), js,
                     struct.pack("<I4s", len(blob), b"BIN\x00"), blob])


def write_glb(mesh: MeshData, path: str | Path, *, extras: dict[str, Any] | None = None) -> str:
    """The ``.glb`` archive copy (texture embedded)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(glb_bytes(mesh, name=p.stem, extras=extras))
    return str(p)


def export_all(mesh: MeshData, out_dir: str | Path, stem: str, *, blender: str | None = None, want_fbx: bool = True,
               extras: dict[str, Any] | None = None, timeout_s: int = 120) -> tuple[dict[str, str], list[str]]:
    """Write the glTF set, the GLB archive and (when Blender is available) the FBX. Returns ``(files, messages)``."""
    files = write_gltf_set(mesh, out_dir, stem, extras=extras)
    files["glb_archive"] = write_glb(mesh, Path(out_dir) / f"{stem}.glb", extras=extras)
    messages: list[str] = []
    if want_fbx:
        from duoskin.mesh import blender as bl

        exe = bl.find_blender(blender or "")
        if exe is None:
            messages.append("fbx: not produced (Blender not installed: the glTF set is the upload format; install Blender 4.x for the FBX backup)")
        else:
            res = bl.export_fbx(exe, files["glb_archive"], Path(out_dir) / f"{stem}.fbx", timeout_s=timeout_s)
            if res.ok:
                files["fbx"] = res.files["fbx"]
            else:
                messages.append(f"fbx: not produced ({res.error})")
    return files, messages


# --------------------------------------------------------------------------------------------------------------------
# exporter round trip with the asymmetric F fixture (CHK-M19, MESH-13)
# --------------------------------------------------------------------------------------------------------------------
def roundtrip_compare(original: MeshData, reloaded: MeshData, *, tol: float = 1e-4) -> dict[str, Any]:
    """Compare a fixture with its re-imported copy: vertex set, handedness and the marker colours at known corners."""
    from duoskin.mesh import fixtures

    a = np.sort(np.round(original.vertices, 5), axis=0)
    b = np.sort(np.round(reloaded.vertices, 5), axis=0)
    same_geometry = bool(a.shape == b.shape and np.allclose(a, b, atol=max(tol, 1e-4)))
    mo = fixtures.marker_colours(original)
    mr = fixtures.marker_colours(reloaded) if reloaded.uv is not None and reloaded.texture is not None else {}
    markers_ok = bool(mr) and all(np.abs(np.array(mo[k]) - np.array(mr[k])).max() <= 12 for k in mo)
    from duoskin.mesh.geometry import signed_volume

    vo = signed_volume(original.vertices, original.faces)
    vr = signed_volume(reloaded.vertices, reloaded.faces)
    handed = bool(vo * vr > 0)
    return {"same_geometry": same_geometry, "markers_ok": markers_ok, "handedness_ok": handed,
            "markers_expected": {k: list(v) for k, v in mo.items()}, "markers_found": {k: list(v) for k, v in mr.items()}}


def gltf_roundtrip(work_dir: str | Path) -> CheckResult:
    """CHK-M19 for the glTF exporter: export the F fixture, reload it through trimesh, compare."""
    from duoskin.mesh import fixtures, load

    try:
        f = fixtures.f_fixture()
        files = write_gltf_set(f, work_dir, "f_fixture")
        back = load.load_mesh(files["gltf"]).mesh
        res = roundtrip_compare(f, back)
        ok = res["same_geometry"] and res["markers_ok"] and res["handedness_ok"]
        return CheckResult(check_id="CHK-M19", fm_ids=["MESH-13"], kind="assert", passed=ok, metric="gltf_roundtrip_F",
                           value=1.0 if ok else 0.0, threshold="markers equal, geometry equal, not mirrored",
                           evidence=json.dumps(res), fix_hint="none" if ok else "human")
    except Exception as exc:  # noqa: BLE001 - fail closed
        return not_run("CHK-M19", "assert", f"gltf round trip raised {type(exc).__name__}: {exc}", fm_ids=["MESH-13"])


def fbx_roundtrip(work_dir: str | Path, blender: str = "") -> CheckResult:
    """CHK-M19 for the FBX exporter: needs Blender. Without it the check is not applicable (no FBX is produced)."""
    from duoskin.mesh import blender as bl
    from duoskin.mesh import fixtures, load

    exe = bl.find_blender(blender)
    if exe is None:
        return not_applicable("CHK-M19", "assert", "Blender not installed: no FBX is produced, so there is no FBX exporter to round-trip",
                              fm_ids=["MESH-13"])
    try:
        wd = Path(work_dir)
        f = fixtures.f_fixture()
        glb = write_glb(f, wd / "f_fixture.glb")
        res = bl.export_fbx(exe, glb, wd / "f_fixture.fbx")
        if not res.ok:
            return not_run("CHK-M19", "assert", f"FBX export failed: {res.error}", fm_ids=["MESH-13"])
        back_glb = bl.import_to_glb(exe, res.files["fbx"], wd / "f_fixture_back.glb")
        if not back_glb.ok:
            return not_run("CHK-M19", "assert", f"FBX re-import failed: {back_glb.error}", fm_ids=["MESH-13"])
        back = load.load_mesh(back_glb.files["glb"]).mesh
        cmp = roundtrip_compare(f, back)
        ok = cmp["markers_ok"] and cmp["handedness_ok"]
        return CheckResult(check_id="CHK-M19", fm_ids=["MESH-13"], kind="assert", passed=ok, metric="fbx_roundtrip_F",
                           value=1.0 if ok else 0.0, threshold="markers equal, not mirrored", evidence=json.dumps(cmp))
    except Exception as exc:  # noqa: BLE001
        return not_run("CHK-M19", "assert", f"fbx round trip raised {type(exc).__name__}: {exc}", fm_ids=["MESH-13"])


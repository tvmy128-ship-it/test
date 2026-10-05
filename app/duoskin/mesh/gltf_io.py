"""glTF/GLB file inspection (no mesh parsing): extensions, node/primitive/material/UV counts, COLOR_0, emissive, metallic,
URIs. These facts come from the file itself, never from trimesh, because trimesh hides most of them (CHK-M01, M02, M07,
M14). Pure JSON handling, so it is safe to run in the server process.
"""
from __future__ import annotations

import json
import struct
from pathlib import Path
from typing import Any

from duoskin.mesh.types import MeshError

# --------------------------------------------------------------------------------------------------------------------
def read_gltf_json(path: str | Path) -> tuple[dict[str, Any], bytes | None]:
    """The JSON document of a ``.gltf`` or ``.glb`` and the GLB binary chunk (None for ``.gltf``)."""
    data = Path(path).read_bytes()
    if data[:4] == b"glTF":
        _, version, length = struct.unpack_from("<4sII", data, 0)
        off, js, binchunk = 12, None, None
        while off + 8 <= min(len(data), length):
            clen, ctype = struct.unpack_from("<I4s", data, off)
            body = data[off + 8: off + 8 + clen]
            if ctype == b"JSON":
                js = json.loads(body.decode("utf-8"))
            elif ctype == b"BIN\x00":
                binchunk = bytes(body)
            off += 8 + clen
        if js is None:
            raise MeshError("bad_glb", "the GLB has no JSON chunk")
        return js, binchunk
    return json.loads(data.decode("utf-8")), None


def gltf_structure_facts(path: str | Path) -> dict[str, Any]:
    """FM 5.5 structural facts of a glTF/GLB (extensions, node/primitive/material/UV counts, COLOR_0, emissive, metallic, URIs)."""
    doc, _ = read_gltf_json(path)
    meshes = doc.get("meshes", [])
    prims = [p for m in meshes for p in m.get("primitives", [])]
    mats = doc.get("materials", [])
    mat = mats[0] if mats else None
    pbr = (mat or {}).get("pbrMetallicRoughness", {})
    attrs = prims[0].get("attributes", {}) if prims else {}
    uris = [b.get("uri") for b in doc.get("buffers", []) if b.get("uri")] + [i.get("uri") for i in doc.get("images", []) if i.get("uri")]

    def bad_uri(u: str) -> bool:
        if u.startswith("data:"):
            return False
        return "/" in u or "\\" in u or ":" in u or u.startswith(".")

    mimes = []
    for im in doc.get("images", []):
        if im.get("mimeType"):
            mimes.append(im["mimeType"])
        elif im.get("uri"):
            mimes.append("image/png" if im["uri"].lower().endswith(".png") or im["uri"].startswith("data:image/png")
                         else im["uri"].rsplit(".", 1)[-1])
    return {
        "ext_required": list(doc.get("extensionsRequired", [])),
        "ext_used": list(doc.get("extensionsUsed", [])),
        "mesh_nodes": sum(1 for n in doc.get("nodes", []) if "mesh" in n),
        "primitives": len(prims),
        "materials": len(mats),
        "uv_sets": sum(1 for k in ("TEXCOORD_0", "TEXCOORD_1", "TEXCOORD_2") if k in attrs),
        "has_color0": "COLOR_0" in attrs,
        "emissive": bool(mat and ("emissiveTexture" in mat or any(mat.get("emissiveFactor", [0, 0, 0])))),
        "metallic_factor": float(pbr.get("metallicFactor", 1.0)),
        "mr_texture": "metallicRoughnessTexture" in pbr,
        "normal_texture": bool(mat and "normalTexture" in mat),
        "occlusion_texture": bool(mat and "occlusionTexture" in mat),
        "alpha_mode": (mat or {}).get("alphaMode", "OPAQUE"),
        "double_sided": bool((mat or {}).get("doubleSided", False)),
        "image_mimes": mimes,
        "bad_uris": [u for u in uris if bad_uri(u)],
        "n_images": len(doc.get("images", [])),
        "skins": len(doc.get("skins", [])),
        "animations": len(doc.get("animations", [])),
        "extras": (doc.get("nodes", [{}])[0].get("extras", {}) or {}).get("duoskin", {}),
    }


def check_gltf_files(path: str | Path) -> list[str]:
    """Problems that would make the texture or buffers vanish in Studio (MESH-14); empty when fine."""
    p = Path(path)
    problems: list[str] = []
    if p.suffix.lower() == ".gltf":
        doc, _ = read_gltf_json(p)
        for uri in [b.get("uri") for b in doc.get("buffers", [])] + [i.get("uri") for i in doc.get("images", [])]:
            if not uri:
                continue
            if uri.startswith("data:"):
                continue
            if "/" in uri or "\\" in uri or uri.startswith(".") or ":" in uri:
                problems.append(f"uri {uri!r} is not a bare file name")
            elif not (p.parent / uri).is_file():
                problems.append(f"file {uri!r} next to the .gltf is missing")
    return problems



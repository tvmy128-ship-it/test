"""glTF writer, loader, sniffing, extension policy and the F round trip (MESH-13, MESH-14, ACC-12, CHK-M19)."""
from __future__ import annotations

import json
import struct
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from duoskin.checks.model import gate_verdict
from duoskin.mesh import export, fixtures, gltf_io, load
from duoskin.mesh import primitives as prim
from duoskin.mesh.types import MeshError, uv_gl_to_gltf, uv_gltf_to_gl


@pytest.fixture()
def f_files(tmp_path):
    return export.write_gltf_set(fixtures.f_fixture(), tmp_path, "f_fixture")


def test_gltf_set_has_exactly_the_contract(f_files, tmp_path):
    assert sorted(Path(p).name for p in f_files.values()) == ["f_fixture.bin", "f_fixture.gltf", "f_fixture_albedo.png"]
    facts = gltf_io.gltf_structure_facts(f_files["gltf"])
    assert facts["ext_required"] == [] and facts["ext_used"] == []
    assert (facts["mesh_nodes"], facts["primitives"], facts["materials"], facts["uv_sets"]) == (1, 1, 1, 1)
    assert not facts["has_color0"] and not facts["emissive"] and not facts["mr_texture"] and not facts["normal_texture"]
    assert facts["metallic_factor"] == 0.0                       # written explicitly (the glTF default is 1.0)
    assert facts["alpha_mode"] == "OPAQUE" and facts["double_sided"] is False
    assert facts["image_mimes"] == ["image/png"] and facts["bad_uris"] == []
    assert facts["extras"]["units"] == "studs" and facts["extras"]["front"] == "+Z"
    doc = json.loads(Path(f_files["gltf"]).read_text(encoding="utf-8"))
    assert doc["buffers"][0]["uri"] == "f_fixture.bin" and doc["images"][0]["uri"] == "f_fixture_albedo.png"
    assert export.check_gltf_files(f_files["gltf"]) == []
    png = Image.open(f_files["png"])
    assert png.mode == "RGB" and "icc_profile" not in png.info and "gamma" not in png.info


def test_gltf_uvs_are_written_in_gltf_convention(f_files):
    """Independent of trimesh: the top-left vertex of the F carries a small v (image origin top-left)."""
    doc = json.loads(Path(f_files["gltf"]).read_text(encoding="utf-8"))
    binary = Path(f_files["bin"]).read_bytes()
    pos_view, uv_view = doc["bufferViews"][0], doc["bufferViews"][2]
    pos = np.frombuffer(binary, np.float32, count=pos_view["byteLength"] // 4, offset=pos_view["byteOffset"]).reshape(-1, 3)
    uv = np.frombuffer(binary, np.float32, count=uv_view["byteLength"] // 4, offset=uv_view["byteOffset"]).reshape(-1, 2)
    top_left = np.argmin(np.where(pos[:, 2] > 0, pos[:, 0] - pos[:, 1], 9))
    assert uv[top_left][1] < 0.1 and uv[top_left][0] < 0.1


def test_roundtrip_with_the_f_fixture_passes_and_detects_flipped_v(tmp_path):
    chk = export.gltf_roundtrip(tmp_path / "rt")
    assert chk.check_id == "CHK-M19" and chk.kind == "assert" and chk.passed, chk.evidence
    f = fixtures.f_fixture()
    bad = f.copy()
    bad.uv = uv_gltf_to_gl(f.uv)                                # a writer that forgot the V flip
    back = load.load_mesh(export.write_gltf_set(bad, tmp_path / "bad", "b")["gltf"]).mesh
    cmp = export.roundtrip_compare(f, back)
    assert not cmp["markers_ok"]
    mirrored = f.copy()
    mirrored.vertices = f.vertices * np.array([-1, 1, 1])
    back = load.load_mesh(export.write_gltf_set(mirrored, tmp_path / "mir", "m")["gltf"]).mesh
    assert not export.roundtrip_compare(f, back)["same_geometry"] or not export.roundtrip_compare(f, back)["markers_ok"]


def test_fbx_roundtrip_is_not_applicable_without_blender(tmp_path, monkeypatch):
    monkeypatch.delenv("DUOSKIN_BLENDER", raising=False)
    monkeypatch.setattr("duoskin.mesh.blender.find_blender", lambda explicit="": None)
    chk = export.fbx_roundtrip(tmp_path)
    assert chk.status == "not_applicable" and chk.passed and "Blender not installed" in chk.na_reason


def test_glb_archive_loads_back_identically(tmp_path):
    m = prim.build("charm", {}, (200, 90, 90))
    p = export.write_glb(m, tmp_path / "x.glb")
    assert load.sniff(p) == "glb"
    back = load.load_mesh(p).mesh
    assert back.n_tris == m.n_tris and np.allclose(np.sort(back.vertices, axis=0), np.sort(m.vertices.astype(np.float32), axis=0), atol=1e-5)
    assert back.texture.size == m.texture.size
    facts = gltf_io.gltf_structure_facts(p)
    assert facts["image_mimes"] == ["image/png"] and facts["bad_uris"] == []


def test_uv_convention_helpers_are_involutions():
    uv = np.array([[0.1, 0.2], [0.9, 0.7]])
    assert np.allclose(uv_gl_to_gltf(uv_gltf_to_gl(uv)), uv) and np.allclose(uv_gltf_to_gl(uv)[:, 1], 1 - uv[:, 1])


def test_smooth_normals_blend_seams_but_keep_hard_edges():
    cube = fixtures.f_fixture()
    n = export.smooth_vertex_normals(cube)
    assert np.allclose(np.linalg.norm(n, axis=1), 1.0, atol=1e-5)
    box = prim.box_part((1, 1, 1))
    from duoskin.mesh.types import MeshData
    nb = export.smooth_vertex_normals(MeshData(box.vertices, box.faces, box.uv))
    assert np.allclose(np.abs(nb).max(axis=1), 1.0, atol=1e-6)         # each box corner keeps its face normal (hard edges)


def test_export_without_texture_is_a_clean_error(tmp_path):
    m = fixtures.f_fixture()
    m.texture = None
    with pytest.raises(MeshError):
        export.write_gltf_set(m, tmp_path, "x")


def test_export_frame_rotation_and_forward_axis(tmp_path):
    m = fixtures.f_fixture()
    m.meta["attachment_offset"] = [0.0, 0.0, 1.0]
    e = export.to_export_frame_mesh(m, "-Z")
    assert np.allclose(e.vertices[:, 2], -m.vertices[:, 2], atol=1e-9) and np.allclose(e.meta["attachment_offset"], [0, 0, -1], atol=1e-6)
    files = export.write_gltf_set(e, tmp_path, "x")
    assert gltf_io.gltf_structure_facts(files["gltf"])["extras"]["front"] == "-Z"


# ----------------------------------------------------------------------------------------------- sniffing and policy
def make_glb(doc: dict, binary: bytes = b"") -> bytes:
    js = json.dumps(doc).encode()
    js += b" " * ((4 - len(js) % 4) % 4)
    binary += b"\x00" * ((4 - len(binary) % 4) % 4)
    chunks = struct.pack("<I4s", len(js), b"JSON") + js
    if binary:
        chunks += struct.pack("<I4s", len(binary), b"BIN\x00") + binary
    return struct.pack("<4sII", b"glTF", 2, 12 + len(chunks)) + chunks


def test_sniff_by_magic_bytes_not_suffix(tmp_path):
    cases = {
        "a.glb": (make_glb({"asset": {"version": "2.0"}}), "glb"),
        "b.txt": (b"Kaydara FBX Binary  \x00\x1a\x00" + b"\0" * 40, "fbx"),
        "c.bin": (b"PK\x03\x04" + b"\0" * 30, "zip"),
        "d.glb": (b"BLENDER-v400" + b"\0" * 20, "blend"),
        "e.dat": (b"# obj\nv 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n", "obj"),
        "f.gltf": (json.dumps({"asset": {"version": "2.0"}}).encode(), "gltf"),
        "g.png": (b"\x89PNG\r\n\x1a\n" + b"\0" * 20, "png"),
        "h.glb": (b"nonsense bytes here", "unknown"),
    }
    for name, (data, kind) in cases.items():
        p = tmp_path / name
        p.write_bytes(data)
        assert load.sniff(p) == kind, name


@pytest.mark.parametrize("ext,fragment", [
    (["EXT_meshopt_compression"], "meshopt"),
    (["KHR_draco_mesh_compression"], "Draco"),
    (["KHR_texture_basisu"], "KTX2"),
    (["KHR_materials_something_new"], "extensions"),
])
def test_unreadable_extensions_are_refused_with_a_reexport_message(tmp_path, ext, fragment):
    p = tmp_path / "x.glb"
    p.write_bytes(make_glb({"asset": {"version": "2.0"}, "extensionsRequired": ext, "extensionsUsed": ext}))
    with pytest.raises(MeshError) as e:
        load.load_mesh(p)
    assert e.value.code == "unsupported_extension" and fragment.lower() in e.value.message.lower() and "re-export" in e.value.message.lower()


def test_gltf_with_missing_or_absolute_files_is_refused(tmp_path, f_files):
    doc = json.loads(Path(f_files["gltf"]).read_text(encoding="utf-8"))
    (tmp_path / "ok").mkdir()
    Path(f_files["gltf"]).replace(tmp_path / "ok" / "f.gltf")                  # the .bin and .png stay behind
    with pytest.raises(MeshError) as e:
        load.load_mesh(tmp_path / "ok" / "f.gltf")
    assert e.value.code == "missing_files"
    doc["images"][0]["uri"] = "C:/Users/me/tex.png"
    p = tmp_path / "abs.gltf"
    p.write_text(json.dumps(doc), encoding="utf-8")
    assert "not a bare file name" in "; ".join(export.check_gltf_files(p))


def test_blender_is_required_for_fbx_with_the_exact_guidance(tmp_path, monkeypatch):
    monkeypatch.setattr("duoskin.mesh.blender.find_blender", lambda explicit="": None)
    p = tmp_path / "m.fbx"
    p.write_bytes(b"Kaydara FBX Binary  \x00\x1a\x00" + b"\0" * 64)
    with pytest.raises(MeshError) as e:
        load.load_mesh(p)
    assert e.value.code == "blender_missing" and e.value.message.startswith("Blender not installed: export GLB instead")


def test_stl_ply_and_unknown_files_have_friendly_errors(tmp_path):
    p = tmp_path / "a.stl"
    p.write_bytes(b"solid x\nendsolid x\n")
    with pytest.raises(MeshError) as e:
        load.load_mesh(p)
    assert e.value.code == "no_texture_format"
    q = tmp_path / "junk.glb"
    q.write_bytes(b"junk" * 20)
    with pytest.raises(MeshError) as e2:
        load.load_mesh(q)
    assert e2.value.code == "unknown_format"


def test_obj_with_mtl_and_texture_loads_without_blender(tmp_path, monkeypatch):
    monkeypatch.setattr("duoskin.mesh.blender.find_blender", lambda explicit="": None)
    f = fixtures.f_fixture()
    tex = f.texture
    tex.save(tmp_path / "tex.png")
    uv = uv_gltf_to_gl(f.uv)
    lines = ["mtllib m.mtl", "usemtl m0"] + [f"v {x} {y} {z}" for x, y, z in f.vertices] + [f"vt {u} {v}" for u, v in uv]
    lines += [f"f {a + 1}/{a + 1} {b + 1}/{b + 1} {c + 1}/{c + 1}" for a, b, c in f.faces]
    (tmp_path / "m.obj").write_text("\n".join(lines), encoding="utf-8")
    (tmp_path / "m.mtl").write_text("newmtl m0\nKd 1 1 1\nmap_Kd tex.png\n", encoding="utf-8")
    res = load.load_mesh(tmp_path / "m.obj")
    assert res.mesh.n_tris == f.n_tris and res.mesh.texture is not None
    assert export.roundtrip_compare(f, res.mesh)["markers_ok"]        # the V convention survived the OBJ path


def test_two_materials_are_merged_into_one_atlas(tmp_path):
    a = prim.build("sphere", {"diameter": 1.0}, (250, 20, 20))
    b = prim.build("box", {"size": (0.5, 0.5, 0.5)}, (20, 20, 250))
    import trimesh

    sa = trimesh.Trimesh(a.vertices, a.faces, process=False)
    sb = trimesh.Trimesh(b.vertices + [2, 0, 0], b.faces, process=False)
    from trimesh.visual.material import PBRMaterial
    from trimesh.visual.texture import TextureVisuals

    sa.visual = TextureVisuals(uv=uv_gltf_to_gl(a.uv), material=PBRMaterial(baseColorTexture=a.texture))
    sb.visual = TextureVisuals(uv=uv_gltf_to_gl(b.uv), material=PBRMaterial(baseColorTexture=b.texture))
    scene = trimesh.Scene([sa, sb])
    p = tmp_path / "two.glb"
    p.write_bytes(scene.export(file_type="glb"))
    loaded = load.load_mesh(p)
    assert loaded.facts["materials_merged"] == 2 and loaded.mesh.uv.max() <= 1 and loaded.mesh.uv.min() >= 0
    assert loaded.mesh.n_tris == a.n_tris + b.n_tris and any("merged" in m for m in loaded.messages)
    # the sphere's UVs now point into the first atlas cell and the colour there is the sphere's red
    tex = np.asarray(loaded.mesh.texture.convert("RGB"))
    h, w = tex.shape[:2]
    i = int(np.argmin(loaded.mesh.vertices[:, 0]))
    u, v = loaded.mesh.uv[i]
    assert tex[int(v * h), int(u * w)][0] > 150 > tex[int(v * h), int(u * w)][2]


def test_node_transforms_are_baked(tmp_path):
    import trimesh

    m = prim.build("box", {"size": (1, 1, 1)}, (10, 200, 10))
    tm = trimesh.Trimesh(m.vertices, m.faces, process=False)
    from trimesh.visual.material import PBRMaterial
    from trimesh.visual.texture import TextureVisuals

    tm.visual = TextureVisuals(uv=uv_gltf_to_gl(m.uv), material=PBRMaterial(baseColorTexture=m.texture))
    scene = trimesh.Scene()
    scene.add_geometry(tm, transform=trimesh.transformations.compose_matrix(scale=[0.01, 0.01, 0.01], translate=[0, 5, 0]))
    p = tmp_path / "t.glb"
    p.write_bytes(scene.export(file_type="glb"))
    got = load.load_mesh(p).mesh
    assert np.allclose(got.extents, [0.01, 0.01, 0.01], atol=1e-6) and abs(got.bounds.mean(axis=0)[1] - 5) < 1e-5     # auto_size trap


def test_size_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(load, "MAX_FILE_BYTES", 100)
    p = tmp_path / "x.glb"
    p.write_bytes(make_glb({"asset": {"version": "2.0"}}) + b"\0" * 200)
    with pytest.raises(MeshError) as e:
        load.load_gltf(p)
    assert e.value.code == "too_big"


def test_gate_verdict_is_usable_on_roundtrip_results(tmp_path):
    assert gate_verdict([export.gltf_roundtrip(tmp_path)]) == "pass"

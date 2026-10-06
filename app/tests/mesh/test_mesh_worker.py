"""The worker op contract (APP_SPEC 10.9) in-process and across the real subprocess boundary."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageDraw
from pydantic import ValidationError

from duoskin.checks.model import gate_verdict
from duoskin.mesh import export, fixtures, load, worker
from duoskin.mesh import geometry as geo
from duoskin.mesh.fixtures import chiral_solid, save_approved_views
from duoskin.mesh.types import MeshJob, MeshResult


@pytest.fixture(scope="module")
def solid():
    return chiral_solid()


@pytest.fixture(scope="module")
def approved(solid, tmp_path_factory):
    return save_approved_views(solid, tmp_path_factory.mktemp("approved"))


def raw_glb(solid, tmp_path, *, rotate=None, scale=3.1, name="raw.glb"):
    m = solid.copy()
    if rotate is not None:
        m = fixtures.rotate_mesh(m, rotate)
    m.vertices = m.vertices * scale + np.array([2.0, -1.0, 4.0])
    return export.write_glb(m, tmp_path / name)


def job(op, tmp_path, input_path="", asset_type="Hat", attachment="HatAttachment", **kw):
    base = {"op": op, "input_path": str(input_path), "out_dir": str(tmp_path / "out"), "asset_type": asset_type, "attachment": attachment,
                "target_studs": (2.0, 1.4, 1.4), "tris_target": 3800, "texture_px": 1024, "asset_id": "a.acc.0"}
    base.update(kw)
    return MeshJob(**base)


def failed(res):
    return {c.check_id: c.evidence for c in res.checks if c.kind in ("hard", "assert") and not c.passed}


def test_job_contract_matches_the_spec_fields():
    names = set(MeshJob.model_fields)
    assert {"op", "input_path", "out_dir", "asset_type", "attachment", "target_studs", "tris_target", "texture_px", "approved_views", "mannequin",
            "forward_axis"} <= names
    assert set(MeshResult.model_fields) >= {"ok", "files", "facts", "checks"}
    with pytest.raises(ValidationError):
        MeshJob(op="explode", input_path="", out_dir="", asset_type="Hat", attachment="", target_studs=(1, 1, 1), tris_target=1, texture_px=1)


def test_import_op_describes_a_file(tmp_path, solid):
    res = worker.execute(job("import", tmp_path, raw_glb(solid, tmp_path)))
    assert res.ok and res.checks[0].check_id == "CHK-M01" and res.checks[0].passed
    assert res.facts["tris"] == solid.n_tris and res.facts["has_texture"] and len(res.facts["sha256"]) == 64


def test_repair_op_end_to_end_for_an_accessory(tmp_path, solid, approved):
    spun = geo.all_axis_rotations()[9]
    res = worker.execute(job("repair", tmp_path, raw_glb(solid, tmp_path, rotate=spun), approved_views=approved,
                             params={"want_fbx": True}))
    assert res.ok, res.messages
    assert not failed(res), failed(res)
    assert gate_verdict(res.checks) == "pass"
    for k in ("gltf", "bin", "png", "glb_archive", "renders/front", "renders/judge_sheet", "renders/scale_front"):
        assert Path(res.files[k]).is_file(), k
    f = res.facts
    for key in ("tris", "shells", "bbox_studs", "box_margins", "surface_area", "coplanar_frac", "centre_offset", "normals_out", "watertight",
                "texture_px", "orientation", "mirrored"):
        assert key in f, key
    assert f["watertight"] and not f["mirrored"] and f["orientation"]["iou"] > 0.9 and f["texture_px"] <= 1024
    assert f["frame"] == {"units": "studs", "up": "+Y", "front": "+Z"}
    assert any("pymeshlab" in d for d in res.degraded) or f["tris"] <= 3800
    assert any("fbx: not produced" in m for m in res.messages)                      # no Blender on this machine
    json.loads(res.model_dump_json())                                                # fully serialisable


def test_repair_is_deterministic(tmp_path, solid, approved):
    src = raw_glb(solid, tmp_path)
    a = worker.execute(job("repair", tmp_path / "a", src, approved_views=approved, params={"render": False}))
    b = worker.execute(job("repair", tmp_path / "b", src, approved_views=approved, params={"render": False}))
    assert Path(a.files["bin"]).read_bytes() == Path(b.files["bin"]).read_bytes()
    assert Path(a.files["png"]).read_bytes() == Path(b.files["png"]).read_bytes()


def test_a_mirrored_import_is_flagged_and_fails_the_gate_without_being_flipped(tmp_path, solid, approved):
    mirrored = np.diag([-1.0, 1.0, 1.0])
    res = worker.execute(job("repair", tmp_path, raw_glb(solid, tmp_path, rotate=mirrored), approved_views=approved, params={"render": False}))
    assert res.ok and res.facts["mirrored"]
    assert "CHK-M08" in failed(res) and "mirrored model" in failed(res)["CHK-M08"]
    assert gate_verdict(res.checks) == "fail"


def test_flip_lr_op_resolves_a_mirrored_import(tmp_path, solid, approved):
    mirrored = np.diag([-1.0, 1.0, 1.0])
    first = worker.execute(job("repair", tmp_path / "a", raw_glb(solid, tmp_path, rotate=mirrored), approved_views=approved, params={"render": False}))
    assert first.facts["mirrored"] and "CHK-M08" in failed(first)
    second = worker.execute(job("flip_lr", tmp_path / "b", first.files["gltf"], approved_views=approved, params={"render": False}))
    assert second.ok and not failed(second), failed(second)
    assert second.facts["flipped_lr"] and not second.facts["mirrored"] and any("logged" in m for m in second.messages)
    # winding stays outward, texture and UVs untouched
    from duoskin.mesh import load

    assert load.load_gltf(second.files["gltf"]).mesh.n_tris == load.load_gltf(first.files["gltf"]).mesh.n_tris


def test_over_budget_dense_model_is_decimated_with_uvs(tmp_path):
    dense = fixtures.dense_sphere(0.5, 96, 64)
    src = export.write_glb(dense, tmp_path / "dense.glb")
    res = worker.execute(job("repair", tmp_path, src, tris_target=2500, params={"render": False}))
    assert res.ok and res.facts["tris"] <= 2500
    assert next(c for c in res.checks if c.check_id == "CHK-M15").passed
    assert any("pymeshlab" in d for d in res.degraded)


def test_hair_repair_registers_the_head_out(tmp_path):
    m = fixtures.hair_with_head_fixture(fused=True)
    m.vertices = m.vertices * 2.0 + np.array([1.0, 2.0, 0.0])
    src = export.write_glb(m, tmp_path / "hair.glb")
    res = worker.execute(job("repair", tmp_path, src, asset_type="Hair", attachment="HairAttachment", tris_target=3600,
                             target_studs=(3.0, 5.0, 3.5), params={"render": False}))
    assert res.ok, res.messages
    problems = failed(res)
    # no approved views were supplied (M08, M13 cannot run) and the fixture's boxy cap leaves the sides of the head bare (M14 sees the skin)
    assert set(problems) == {"CHK-M08", "CHK-M13", "CHK-M14"}, problems
    assert "hair zone shows skin" in problems["CHK-M14"]
    assert res.facts["hair_register"]["head_found"] and next(c for c in res.checks if c.check_id == "CHK-M21").passed
    assert res.facts["tris"] <= 3800 and res.facts["watertight"]


def test_register_hair_op_with_a_bbox_hint_when_the_model_has_no_head(tmp_path):
    helmet = fixtures.hair_with_head_fixture(fused=True)
    helmet.texture = fixtures.hair_texture(256)
    helmet.uv[:] = 0.25
    src = export.write_glb(helmet, tmp_path / "hair_only.glb")
    res = worker.execute(job("register_hair", tmp_path, src, asset_type="Hair", attachment="HairAttachment", tris_target=3600,
                             params={"render": False, "hair_bbox": [[-0.7, -0.5, 0.0], [0.7, 0.9, 0.0]], "want_fbx": False}))
    assert res.ok and res.facts["hair_register"]["mode"] == "bbox_hint"
    nohint = worker.execute(job("register_hair", tmp_path / "x", src, asset_type="Hair", attachment="HairAttachment", params={"render": False}))
    assert not nohint.ok and nohint.error == "no_alignment"


def test_slab_op(tmp_path):
    img = Image.new("RGBA", (512, 512), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse([40, 60, 470, 450], fill=(230, 60, 60, 255))
    d.polygon([(100, 300), (160, 300), (100, 400)], fill=(0, 0, 255, 255))
    art = tmp_path / "badge.png"
    img.save(art)
    res = worker.execute(job("slab", tmp_path, "", asset_type="Front", attachment="BodyFrontAttachment", target_studs=(2.0, 2.0, 0.1),
                             params={"art_path": str(art), "size_studs": 2.0, "back": "plain", "render": False}))
    assert res.ok, res.messages
    assert not failed(res), failed(res)
    m20 = next(c for c in res.checks if c.check_id == "CHK-M20")
    assert m20.passed and m20.status == "passed"
    assert res.facts["slab"]["thickness"] >= 0.08


def test_primitive_op(tmp_path):
    res = worker.execute(job("primitive", tmp_path, "", asset_type="Waist", attachment="WaistFrontAttachment", target_studs=(0.8, 0.8, 0.8),
                             params={"kind": "charm", "params": {"diameter": 0.8}, "palette": [220, 120, 150], "render": False}))
    assert res.ok, res.messages
    assert not failed(res), failed(res)
    assert res.facts["primitive"] == "charm" and res.facts["watertight"]


def test_validate_op_on_an_exported_file_and_with_roundtrip(tmp_path, solid, approved):
    rep_res = worker.execute(job("repair", tmp_path / "r", raw_glb(solid, tmp_path), approved_views=approved, params={"render": False}))
    res = worker.execute(job("validate", tmp_path / "v", rep_res.files["gltf"], approved_views=approved, params={"roundtrip": True}))
    assert res.ok and not failed(res), failed(res)
    m19 = [c for c in res.checks if c.check_id == "CHK-M19"]
    assert m19[0].passed and m19[0].kind == "assert"
    assert m19[1].status == "not_applicable"                                 # FBX exporter: no Blender here


def test_render_views_op(tmp_path, solid, approved):
    rep_res = worker.execute(job("repair", tmp_path / "r", raw_glb(solid, tmp_path), approved_views=approved, params={"render": False}))
    res = worker.execute(job("render_views", tmp_path / "rv", rep_res.files["gltf"], params={"view_size": 256}))
    assert res.ok and {"renders/front", "renders/left", "renders/back", "renders/right", "renders/top", "renders/three_quarter"} <= set(res.files)
    with Image.open(res.files["renders/front"]) as im:
        assert im.size == (256, 256)


def test_fit_hair_op(tmp_path):
    from duoskin.mesh import primitives as prim

    def piece(path, centre, size):
        part = prim.box_part(size, centre=centre)
        grey = np.zeros((64, 64, 3), np.uint8)
        grey[:, :32], grey[:, 32:] = 128, 40
        export.write_glb(__import__("duoskin.mesh.types", fromlist=["MeshData"]).MeshData(part.vertices, part.faces, part.uv * 0.5, Image.fromarray(grey, "RGB")), path)

    style, fringe = tmp_path / "s.glb", tmp_path / "f.glb"
    piece(style, (0.0, -0.3, -0.3), (1.6, 1.0, 1.6))
    piece(fringe, (0.0, -0.35, 0.5), (1.2, 0.5, 0.3))
    res = worker.execute(job("fit_hair", tmp_path, style, asset_type="Hair", attachment="HairAttachment", tris_target=3600,
                             params={"module_paths": [str(fringe)], "palette": {"base": [150, 40, 60]}, "adjustments": [{"param": "volume", "value": "more"}], "render": False}))
    assert res.ok, res.messages
    assert next(c for c in res.checks if c.check_id == "CHK-M17").passed
    assert res.facts["hair_fit"]["fused"]


def test_failures_become_results_not_exceptions(tmp_path):
    missing = worker.execute(job("repair", tmp_path, tmp_path / "nope.glb"))
    assert not missing.ok and missing.checks and not missing.checks[0].passed and "not found" in missing.messages[0]
    junk = tmp_path / "junk.glb"
    junk.write_bytes(b"x" * 100)
    res = worker.execute(job("import", tmp_path / "j", junk))
    assert not res.ok and res.error == "unknown_format"
    import struct

    doc = json.dumps({"asset": {"version": "2.0"}, "extensionsRequired": ["EXT_meshopt_compression"]}).encode()
    doc += b" " * ((4 - len(doc) % 4) % 4)
    ch = struct.pack("<I4s", len(doc), b"JSON") + doc
    comp = tmp_path / "comp.glb"
    comp.write_bytes(struct.pack("<4sII", b"glTF", 2, 12 + len(ch)) + ch)
    res2 = worker.execute(job("repair", tmp_path / "c", comp))
    assert not res2.ok and res2.error == "unsupported_extension" and "re-export without compression" in res2.messages[0].lower().replace("re-export", "re-export")


def test_main_writes_result_json_atomically_and_validates_usage(tmp_path, solid):
    j = job("import", tmp_path, raw_glb(solid, tmp_path))
    jp = tmp_path / "job.json"
    jp.write_text(j.model_dump_json(), encoding="utf-8")
    assert worker.main([str(jp)]) == 0
    res = MeshResult.model_validate_json((tmp_path / "out" / "result.json").read_text(encoding="utf-8"))
    assert res.ok
    assert not list((tmp_path / "out").glob("*.tmp"))
    assert worker.main([]) == 2


# ------------------------------------------------------------------------------------------- subprocess boundary
def test_run_job_through_a_real_subprocess(tmp_path, solid):
    res = worker.run_job(job("import", tmp_path, raw_glb(solid, tmp_path)), timeout_s=120)
    assert res.ok and res.checks[0].passed
    assert (tmp_path / "out" / "job.json").is_file() and (tmp_path / "out" / "result.json").is_file()


def test_a_crashing_worker_never_raises(tmp_path, solid):
    crash = tmp_path / "crash.py"
    crash.write_text("import sys; sys.stderr.write('segfault'); sys.exit(139)", encoding="utf-8")
    runner = tmp_path / "run.sh"
    runner.write_text(f"#!/bin/sh\nexec {sys.executable} {crash} \"$@\"\n", encoding="utf-8")
    runner.chmod(0o755)
    if sys.platform == "win32":
        pytest.skip("POSIX runner")
    res = worker.run_job(job("import", tmp_path, raw_glb(solid, tmp_path)), timeout_s=30, python=str(runner))
    assert not res.ok and res.error == "worker_crashed" and "139" in res.messages[0]


def test_a_hanging_worker_is_stopped_at_the_timeout(tmp_path, solid):
    if sys.platform == "win32":
        pytest.skip("POSIX runner")
    runner = tmp_path / "hang.sh"
    runner.write_text(f"#!/bin/sh\nexec {sys.executable} -c 'import time; time.sleep(60)'\n", encoding="utf-8")
    runner.chmod(0o755)
    res = worker.run_job(job("import", tmp_path, raw_glb(solid, tmp_path)), timeout_s=2, python=str(runner))
    assert not res.ok and res.error == "timeout" and "longer than 2 seconds" in res.messages[0]


def test_memory_ceiling_is_enforced_for_the_worker(tmp_path, solid):
    if sys.platform == "win32":
        pytest.skip("POSIX runner")
    runner = tmp_path / "mem.sh"
    runner.write_text(f"#!/bin/sh\nexec {sys.executable} -c 'x = bytearray(700*1024*1024); import time; time.sleep(60)'\n", encoding="utf-8")
    runner.chmod(0o755)
    res = worker.run_job(job("import", tmp_path, raw_glb(solid, tmp_path)), timeout_s=30, python=str(runner), max_rss_mb=150)
    assert not res.ok and res.error == "out_of_memory"


# ------------------------------------------------------------------------------------------- pymeshlab hook
def test_pymeshlab_is_optional_and_never_imported_outside_the_worker():
    assert worker.pymeshlab_decimator() is None                 # not installed here: the built-in decimator is used and reported
    for mod in ("repair", "load", "validate", "export", "decimate", "hair", "slab"):
        text = (Path(worker.__file__).parent / f"{mod}.py").read_text(encoding="utf-8")
        assert "import pymeshlab" not in text, mod


def test_obj_writer_and_reader_round_trip_for_the_pymeshlab_exchange(tmp_path):
    f = fixtures.f_fixture()
    worker.write_obj(f, tmp_path / "m.obj")
    back = worker.read_obj(tmp_path / "m.obj", f.texture)
    assert back.n_tris == f.n_tris and np.allclose(np.sort(back.uv, axis=0), np.sort(f.uv, axis=0), atol=1e-6)
    assert (tmp_path / "m.mtl").read_text(encoding="utf-8").startswith("newmtl")
    assert load.sniff(tmp_path / "m.obj") == "obj"

"""mesh/validate.py: facts measured on EXPORTED files and the checks that need renders (CHK-M01 ... M14, M18)."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from duoskin.checks.model import gate_verdict
from duoskin.mesh import export, fixtures
from duoskin.mesh import primitives as prim
from duoskin.mesh import repair as rep
from duoskin.mesh import validate as val
from duoskin.mesh.boolean import box_mesh
from duoskin.mesh.fixtures import chiral_solid, save_approved_views
from duoskin.mesh.types import MeshData
from duoskin.render.avatar import default_mannequin


def ids(results):
    return {r.check_id: r for r in results}


@pytest.fixture(scope="module")
def solid():
    return chiral_solid()


@pytest.fixture(scope="module")
def views(solid, tmp_path_factory):
    return save_approved_views(solid, tmp_path_factory.mktemp("approved"))


@pytest.fixture(scope="module")
def good_file(solid, views, tmp_path_factory):
    """A repaired, exported, oriented accessory file."""
    d = tmp_path_factory.mktemp("good")
    r = rep.repair_mesh(solid, rep.RepairOptions(asset_type="Hat", attachment="HatAttachment", target_studs=(2.0, 1.4, 1.4), approved_views=views,
                                                 mannequin=default_mannequin()))
    files = export.write_gltf_set(export.to_export_frame_mesh(r.mesh, "+Z"), d, "acc")
    return files["gltf"], r.mesh


def ctx(views, **kw):
    return val.ValidateContext(asset_type="Hat", attachment="HatAttachment", approved_views=views, mannequin=default_mannequin(), **kw)


def test_a_repaired_export_passes_the_whole_gate(good_file, views):
    facts, checks = val.validate_file(good_file[0], ctx(views))
    res = ids(checks)
    failed = {k: v.evidence for k, v in res.items() if v.kind in ("hard", "assert") and not v.passed}
    assert not failed, failed
    assert gate_verdict(checks) == "pass"
    for cid in ("CHK-M08", "CHK-M13", "CHK-M14"):
        assert res[cid].ran and res[cid].passed
    assert res["CHK-M20"].status == "not_applicable" and res["CHK-M21"].status == "not_applicable"
    assert facts["tris"] == good_file[1].n_tris and facts["watertight"] and facts["normals_out"] == 1.0
    assert facts["texture_px"] if "texture_px" in facts else facts["tex_size"]
    assert json.loads(json.dumps(facts))                                  # JSON-able


def test_facts_describe_the_file_in_handle_space(good_file, views):
    facts, mesh = val.compute_facts(good_file[0], ctx(views))
    assert np.allclose(facts["extents"], [2.0, 1.4, 1.4], atol=0.7) and facts["centre_offset"] < 1e-5
    assert facts["box"]["attachment"] == "HatAttachment" and facts["box"]["size"] == [3, 4, 3]
    assert min(facts["box_margins"]) >= 0 and facts["vertices_outside_box"] == 0
    assert set(facts["view_coverage"]) == {"front", "back", "left", "right", "top", "bottom"}
    assert facts["metallic_factor"] == 0.0 and not facts["has_color0"]
    assert mesh.meta["attachment_offset"] == facts["attachment_offset"]


def test_box_check_uses_the_attachment_offset_not_the_mesh_origin(solid, views, tmp_path):
    r = rep.repair_mesh(solid, rep.RepairOptions(asset_type="Hat", attachment="HatAttachment", target_studs=(2, 1.4, 1.4)))
    m = r.mesh.copy()
    m.meta["attachment_offset"] = [0.0, -3.0, 0.0]               # the attachment point is 3 studs below the mesh: out of the 4-high box
    files = export.write_gltf_set(m, tmp_path, "x")
    _, checks = val.validate_file(files["gltf"], val.ValidateContext(asset_type="Hat", attachment="HatAttachment"))
    assert not ids(checks)["CHK-M09"].passed


def test_too_many_triangles_and_untextured_files_fail(tmp_path):
    big = fixtures.dense_sphere(0.5, 80, 60)
    files = export.write_gltf_set(big, tmp_path, "big")
    facts, checks = val.validate_file(files["gltf"], val.ValidateContext(asset_type="Hat"))
    res = ids(checks)
    assert not res["CHK-M03"].passed and facts["tris"] > 3800
    assert gate_verdict(checks) == "fail"


def test_a_non_watertight_file_fails_m04(good_file, tmp_path, views):
    m = good_file[1].copy()
    m.faces = m.faces[:-6]
    files = export.write_gltf_set(m, tmp_path, "open")
    _, checks = val.validate_file(files["gltf"], val.ValidateContext(asset_type="Hat", attachment="HatAttachment"))
    assert not ids(checks)["CHK-M04"].passed


def test_file_level_violations_are_caught(good_file, tmp_path):
    src = Path(good_file[0])
    doc = json.loads(src.read_text(encoding="utf-8"))
    for name in ("good.bin", "good_albedo.png"):
        (tmp_path / name).write_bytes((src.parent / ("acc.bin" if name.endswith(".bin") else "acc_albedo.png")).read_bytes())
    doc["buffers"][0]["uri"] = "good.bin"
    doc["images"][0]["uri"] = "good_albedo.png"
    doc["materials"][0]["pbrMetallicRoughness"].pop("metallicFactor")            # glTF default is 1.0 (metallic look)
    doc["materials"][0]["emissiveFactor"] = [1, 0, 0]
    doc["meshes"][0]["primitives"][0]["attributes"]["COLOR_0"] = 1
    doc["extensionsUsed"] = ["KHR_materials_unlit"]
    p = tmp_path / "bad.gltf"
    p.write_text(json.dumps(doc), encoding="utf-8")
    facts = val.gltf_io.gltf_structure_facts(p)
    assert facts["metallic_factor"] == 1.0 and facts["emissive"] and facts["has_color0"]
    _, checks = val.validate_file(p, val.ValidateContext(asset_type="Hat", attachment="HatAttachment"))
    res = ids(checks)
    assert not res["CHK-M07"].passed and not res["CHK-M01"].passed


def test_semi_transparent_texture_fails_m06(good_file, tmp_path):
    m = good_file[1].copy()
    arr = np.asarray(m.texture.convert("RGBA")).copy()
    arr[..., 3] = 254
    m.texture = Image.fromarray(arr, "RGBA")
    # the writer drops alpha on purpose, so write the PNG by hand with alpha to emulate a Tripo export
    files = export.write_gltf_set(m, tmp_path, "a")
    m.texture.save(files["png"])
    facts, checks = val.validate_file(files["gltf"], val.ValidateContext(asset_type="Hat", attachment="HatAttachment"))
    assert facts["tex_min_alpha"] == 254 and not ids(checks)["CHK-M06"].passed


def test_flat_texture_fails_m06(good_file, tmp_path):
    m = good_file[1].copy()
    m.texture = Image.new("RGB", (64, 64), (120, 120, 120))
    _, checks = val.validate_file(export.write_gltf_set(m, tmp_path, "flat")["gltf"], val.ValidateContext(asset_type="Hat", attachment="HatAttachment"))
    assert not ids(checks)["CHK-M06"].passed and "flat" in ids(checks)["CHK-M06"].evidence


def test_unreadable_file_is_a_failed_m01_not_an_exception(tmp_path):
    p = tmp_path / "junk.gltf"
    p.write_text("not json", encoding="utf-8")
    facts, checks = val.validate_file(p, val.ValidateContext())
    assert len(checks) == 1 and checks[0].check_id == "CHK-M01" and not checks[0].passed and "load_error" in facts


# ----------------------------------------------------------------------------------------------- measurements
def test_coplanar_intersections_ignore_touching_but_count_overlap():
    cube = box_mesh([-1, -1, -1], [1, 1, 1])
    assert val.coplanar_intersections(cube.vertices, cube.faces) == 0          # quads = 2 triangles sharing an edge
    dup = np.vstack([cube.faces, cube.faces[:1]])
    assert val.coplanar_intersections(cube.vertices, dup) == 1
    v = np.vstack([cube.vertices, cube.vertices[[0, 1, 2]] * np.array([1, 1, 1]) + [0, 0, 0]])
    shrunk = np.array([[0.1, -0.9, 1.0], [0.5, -0.9, 1.0], [0.1, -0.5, 1.0]])    # a small triangle lying inside the +Z face
    v2 = np.vstack([cube.vertices, shrunk])
    f2 = np.vstack([cube.faces, [[8, 9, 10]]])
    assert val.coplanar_intersections(v2, f2) >= 1 and v is not None


def test_view_coverage_sparse_and_dense_shapes():
    ball = fixtures.dense_sphere(0.5, 24, 16)
    cov = val.view_coverage(ball.vertices, ball.faces)
    assert all(0.7 < c < 0.85 for c in cov.values())                           # pi/4
    ring = prim.build("ring", {"diameter": 1.2, "tube": 0.1}, (1, 2, 3))
    cr = val.view_coverage(ring.vertices, ring.faces)
    assert cr["front"] < 0.5 and cr["left"] > 0.8                              # a thin ring is sparse seen from the front
    plate = prim.build("box", {"size": (1, 1, 0.02)}, (1, 2, 3))
    assert val.view_coverage(plate.vertices, plate.faces)["front"] > 0.95


def test_spike_test_sees_a_thin_spike_but_not_a_cone():
    ball = fixtures.dense_sphere(0.5, 32, 24)
    assert val.spike_shrink(ball.vertices, ball.faces) < 0.06
    spike = box_mesh([-0.01, 0.5, -0.01], [0.01, 2.5, 0.01])
    v = np.vstack([ball.vertices, spike.vertices])
    f = np.vstack([ball.faces, spike.faces + len(ball.vertices)])
    assert val.spike_shrink(v, f) > 0.1
    cone = prim.cylinder_part(0.5, 1.0, 24)
    assert val.spike_shrink(cone.vertices, cone.faces) < 0.1


def test_normals_outward_fraction_and_thickness():
    cube = box_mesh([-1, -1, -1], [1, 1, 1])
    assert val.normals_outward_fraction(cube.vertices, cube.faces) == 1.0
    assert val.normals_outward_fraction(cube.vertices, cube.faces[:, ::-1]) == 0.0
    partly = cube.faces.copy()
    partly[:6] = partly[:6][:, ::-1]
    assert 0.2 < val.normals_outward_fraction(cube.vertices, partly) < 0.9
    t = val.thickness_facts(cube.vertices, cube.faces)
    assert abs(t["thickness_p5"] - 2.0) < 0.05 and t["thin_area_frac"] == 0.0
    thin = box_mesh([-1, -1, -0.01], [1, 1, 0.01])
    tf = val.thickness_facts(thin.vertices, thin.faces)
    assert tf["thickness_min"] < 0.05 and tf["thin_area_frac"] > 0.5


# ----------------------------------------------------------------------------------------------- view match and mannequin
def test_view_match_passes_for_the_approved_object_and_fails_for_another(solid, views):
    ok = val.check_view_match(solid, ctx(views))
    assert ok.passed and ok.check_id == "CHK-M13" and ok.value > 0.9, ok.evidence
    other = fixtures.dense_sphere(0.8, 24, 16)
    bad = val.check_view_match(other, ctx(views))
    assert not bad.passed and "IoU" in bad.evidence


def test_view_match_checks_the_palette(solid, views):
    recoloured = solid.copy()
    recoloured.texture = Image.new("RGB", (64, 64), (20, 20, 20))
    bad = val.check_view_match(recoloured, ctx(views))
    assert not bad.passed and "palette" in bad.evidence


def test_view_match_without_views_fails_closed(solid):
    r = val.check_view_match(solid, val.ValidateContext())
    assert not r.ran and not r.passed


def test_thin_part_iou_flags_a_lost_ring(tmp_path):
    ring = prim.build("charm", {"diameter": 0.9, "ring_diameter": 0.6, "tube": 0.08}, (230, 120, 160))
    approved = save_approved_views(ring, tmp_path)
    ok = val.check_view_match(ring, val.ValidateContext(asset_type="Hat", approved_views=approved))
    assert ok.passed, ok.evidence
    bead_only = prim.build("bead", {"diameter": 0.9}, (230, 120, 160))
    lost = val.check_view_match(bead_only, val.ValidateContext(asset_type="Hat", approved_views=approved))
    assert not lost.passed


def test_on_mannequin_check_flags_clipping_and_floating():
    mq = default_mannequin()
    ball = prim.build("sphere", {"diameter": 1.0}, (200, 50, 50))
    r = rep.repair_mesh(ball, rep.RepairOptions(asset_type="Hat", target_studs=(1, 1, 1), mannequin=mq))
    c = val.ValidateContext(asset_type="Hat", attachment="HatAttachment", mannequin=mq)
    assert val.check_on_mannequin(r.mesh, c).passed
    sunk = r.mesh.copy()
    sunk.meta["attachment_offset"] = (np.asarray(sunk.meta["attachment_offset"]) + [0, 0.4, 0]).tolist()
    res = val.check_on_mannequin(sunk, c)
    assert not res.passed and "clips" in res.evidence
    floating = r.mesh.copy()
    floating.meta["attachment_offset"] = (np.asarray(floating.meta["attachment_offset"]) - [0, 0.8, 0]).tolist()
    res2 = val.check_on_mannequin(floating, c)
    assert not res2.passed and "floats" in res2.evidence


def test_hat_over_the_chosen_hair_is_checked_for_penetration():
    mq = default_mannequin()
    hat = prim.build("box", {"size": (1.4, 0.5, 1.4)}, (50, 50, 200))
    r = rep.repair_mesh(hat, rep.RepairOptions(asset_type="Hat", target_studs=(1.4, 0.5, 1.4), mannequin=mq))
    hair_box = box_mesh([-0.9, -0.2, -0.9], [0.9, 0.6, 0.9], uv=(0.5, 0.5))
    hair = MeshData(hair_box.vertices, hair_box.faces, hair_box.uv, r.mesh.texture, {"attachment_offset": [0, 0, 0]})
    c = val.ValidateContext(asset_type="Hat", attachment="HatAttachment", mannequin=mq, hair_mesh=hair)
    res = val.check_on_mannequin(r.mesh, c)
    assert not res.passed and "chosen hair" in res.evidence


def test_soft_group_warns_about_flat_hair_and_baked_ramps(good_file):
    facts, mesh = val.compute_facts(good_file[0], val.ValidateContext(asset_type="Hair", attachment="HairAttachment"))
    flat = val.check_soft_group(mesh, {**facts, "extents": [3.0, 3.0, 0.4]}, val.ValidateContext(asset_type="Hair"))
    assert flat.kind == "soft" and not flat.passed and "flat card" in flat.evidence
    ramp = mesh.copy()
    g = np.linspace(0, 255, 64, dtype=np.uint8)
    ramp.texture = Image.fromarray(np.tile(g[:, None, None], (1, 64, 3)).astype(np.uint8), "RGB")
    r = val.check_soft_group(ramp, facts, val.ValidateContext(asset_type="Hat"))
    assert not r.passed and "ramp" in r.evidence
    assert gate_verdict([r]) == "pass"                                        # soft only warns


def test_forward_axis_minus_z_exports_and_validates(solid, views, tmp_path):
    r = rep.repair_mesh(solid, rep.RepairOptions(asset_type="Hat", attachment="HatAttachment", target_studs=(2, 1.4, 1.4), approved_views=views))
    exp = export.to_export_frame_mesh(r.mesh, "-Z")
    files = export.write_gltf_set(exp, tmp_path, "m")
    facts, checks = val.validate_file(files["gltf"], val.ValidateContext(asset_type="Hat", attachment="HatAttachment", approved_views=views, forward_axis="-Z"))
    assert facts["front_axis"] == "-Z" and ids(checks)["CHK-M08"].passed and ids(checks)["CHK-M09"].passed
    assert rep.welded_volume(r.mesh) > 0


def test_micro_islands_are_rejected_by_the_shell_check(good_file, tmp_path):
    m = good_file[1].copy()
    tiny = prim.sphere_part(0.003, 6, 4, centre=(0.0, 0.0, 1.2))
    m = MeshData(np.vstack([m.vertices, tiny.vertices]), np.vstack([m.faces, tiny.faces + len(m.vertices)]), np.vstack([m.uv, tiny.uv * 0 + 0.5]), m.texture, m.meta)
    files = export.write_gltf_set(m, tmp_path, "micro")
    facts, checks = val.validate_file(files["gltf"], val.ValidateContext(asset_type="Hat", attachment="HatAttachment"))
    assert facts["micro_shells"] == 1
    r = ids(checks)["CHK-M05"]
    assert not r.passed and "micro-islands" in r.evidence


def test_large_meshes_use_32_bit_indices_and_still_load(tmp_path):
    big = fixtures.dense_sphere(0.5, 300, 240)                # more than 65 535 vertices
    assert len(big.vertices) > 65535
    files = export.write_gltf_set(big, tmp_path, "big")
    doc = json.loads(Path(files["gltf"]).read_text(encoding="utf-8"))
    assert doc["accessors"][3]["componentType"] == 5125
    from duoskin.mesh import load

    back = load.load_mesh(files["gltf"]).mesh
    assert back.n_tris == big.n_tris

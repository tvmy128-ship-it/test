"""UV-preserving decimation and the repair pipeline (MESH-01, 08, 09, 15, 17, CHK-M15)."""
from __future__ import annotations

import numpy as np
import pytest
from PIL import Image

from duoskin.mesh import decimate as dec
from duoskin.mesh import fixtures
from duoskin.mesh import geometry as geo
from duoskin.mesh import primitives as prim
from duoskin.mesh import repair as rep
from duoskin.mesh import texture as tx
from duoskin.mesh.types import MeshData, MeshError


# ------------------------------------------------------------------------------------------------------ decimation
@pytest.fixture(scope="module")
def dense():
    return fixtures.dense_sphere(1.0, 96, 64)            # ~12k triangles, UV seam and poles


def test_decimation_hits_the_target_and_stays_watertight(dense):
    out, rep_ = dec.decimate(dense, 2500)
    assert out.n_tris <= 2500 and rep_["reached_target"] and rep_["method"] == "builtin_uv_qem"
    w = geo.weld(out.vertices, out.faces)
    st = geo.topology_stats(w.faces)
    assert st["watertight"] and st["winding_consistent"]
    assert geo.signed_volume(w.vertices, w.faces) > 0
    assert rep_["deviation_frac_of_diag"] < 0.02


def test_decimation_keeps_uvs_inside_0_1_and_the_texture_look(dense):
    out, _ = dec.decimate(dense, 1500)
    assert out.uv is not None and out.uv.min() >= 0 and out.uv.max() <= 1
    fid = rep.decimation_fidelity(dense, out)
    assert fid["mean_de"] < 3.0 and fid["overlap"] > 0.9
    # every output UV is an existing UV of the source (never invented)
    src = {tuple(np.round(u, 7)) for u in dense.uv}
    assert all(tuple(np.round(u, 7)) in src for u in out.uv[:: max(1, len(out.uv) // 200)])


def test_decimation_never_flips_triangles_in_uv_space(dense):
    out, _ = dec.decimate(dense, 2000)
    tri = out.uv[out.faces]
    area = (tri[:, 1, 0] - tri[:, 0, 0]) * (tri[:, 2, 1] - tri[:, 0, 1]) - (tri[:, 2, 0] - tri[:, 0, 0]) * (tri[:, 1, 1] - tri[:, 0, 1])
    src_tri = dense.uv[dense.faces]
    src_area = (src_tri[:, 1, 0] - src_tri[:, 0, 0]) * (src_tri[:, 2, 1] - src_tri[:, 0, 1]) - (src_tri[:, 2, 0] - src_tri[:, 0, 0]) * (src_tri[:, 1, 1] - src_tri[:, 0, 1])
    majority = np.sign(np.median(src_area[np.abs(src_area) > 1e-12]))
    assert (np.sign(area[np.abs(area) > 1e-12]) == majority).mean() > 0.99


def test_decimation_below_target_is_a_no_op_and_missing_uv_is_an_error(dense):
    small = prim.build("sphere", {}, (1, 2, 3))
    out, rep_ = dec.decimate(small, 5000)
    assert out is small and rep_["collapses"] == 0
    nouv = MeshData(dense.vertices, dense.faces)
    with pytest.raises(MeshError) as e:
        dec.decimate(nouv, 1000)
    assert e.value.code == "no_uv"
    with pytest.raises(MeshError) as e2:
        dec.decimate(dense, 100, max_input_tris=1000)
    assert e2.value.code == "too_many_triangles"


def test_seam_vertices_do_not_tear(dense):
    out, _ = dec.decimate(dense, 1200)
    w = geo.weld(out.vertices, out.faces)
    assert geo.topology_stats(w.faces)["boundary_edges"] == 0            # a torn seam would leave boundary edges


def test_open_meshes_keep_their_boundary():
    sphere = fixtures.dense_sphere(1.0, 48, 32)
    keep = sphere.vertices[sphere.faces][:, :, 1].mean(axis=1) < 0.5       # remove the cap: an open bowl
    bowl = MeshData(sphere.vertices, sphere.faces[keep], sphere.uv, sphere.texture).compact()
    out, _ = dec.decimate(bowl, 400)
    w1 = geo.weld(out.vertices, out.faces)
    assert geo.topology_stats(w1.faces)["boundary_edges"] > 0
    assert w1.vertices[:, 1].max() > 0.4                                    # the rim did not collapse away


# ------------------------------------------------------------------------------------------------------ repair
def damaged_plush():
    m = fixtures.plush_fixture()
    f = m.faces.copy()
    f[:5] = f[:5][:, ::-1]                                  # flipped faces
    keep = np.ones(len(f), bool)
    keep[100:106] = False                                   # holes
    keep[300:302] = False
    f = np.vstack([f[keep], f[:5]])                         # duplicates
    v = m.vertices * 3.7 + np.array([5.0, 1.0, 2.0])
    debris = np.array([[0, 0, 0], [0.01, 0, 0], [0, 0.01, 0]]) + np.array([6.0, 2.0, 3.0])
    v2 = np.vstack([v, debris])
    uv2 = np.vstack([m.uv, [[0.1, 0.1]] * 3])
    f2 = np.vstack([f, [[len(v), len(v) + 1, len(v) + 2]]])
    return MeshData(v2, f2, uv2, m.texture)


def test_repair_makes_a_damaged_mesh_watertight_and_keeps_closed_eye_shells():
    d = damaged_plush()
    before = rep.topology(d)
    assert not before["watertight"] and before["nonmanifold_edges"] > 0
    r = rep.repair_mesh(d, rep.RepairOptions(asset_type="Shoulder", attachment="RightCollarAttachment", target_studs=(1.4, 1.2, 1.1)))
    after = r.report["after"]
    assert after["watertight"] and after["winding_consistent"] and after["boundary_edges"] == 0 and after["nonmanifold_edges"] == 0
    assert after["shells"] == 3 and after["closed_shells"] == 3          # body + two plush eyes; the debris triangle is gone
    steps = {s["step"]: s for s in r.report["steps"]}
    assert steps["islands"]["removed_tiny_faces"] == 1 and steps["topology"]["holes"]["filled"] == 2
    assert steps["degenerate_duplicate"]["duplicate"] == 5
    assert rep.welded_volume(r.mesh) > 0


def test_repair_scales_to_the_planned_box_and_recentres_with_an_attachment_offset():
    r = rep.repair_mesh(damaged_plush(), rep.RepairOptions(asset_type="Shoulder", attachment="RightCollarAttachment", target_studs=(1.4, 1.2, 1.1)))
    ext = r.mesh.extents
    assert np.all(ext <= np.array([1.4, 1.2, 1.1]) + 1e-6) and (ext >= np.array([1.4, 1.2, 1.1]) - 1e-6).any()
    assert np.allclose(r.mesh.bounds.mean(axis=0), 0, atol=1e-6)         # Handle space: bbox centre at the origin
    off = np.asarray(r.mesh.meta["attachment_offset"])
    assert off.shape == (3,) and r.mesh.meta["asset_type"] == "Shoulder"
    # the attachment point, relative to the mesh, lies inside the Classic 3x3x3 box around it
    assert np.all(np.abs(r.mesh.bounds - off) <= 1.5 + 1e-6)


def test_repair_never_exceeds_the_classic_box_even_when_the_plan_is_too_big():
    m = prim.build("sphere", {"diameter": 6.0}, (200, 10, 10))
    r = rep.repair_mesh(m, rep.RepairOptions(asset_type="Hat", target_studs=(6, 6, 6)))
    assert np.all(r.mesh.extents <= np.array([3, 4, 3]) + 1e-6)
    assert any(s.get("clamped_to_box") for s in r.report["steps"] if s["step"] == "scale")


def test_repair_decimates_and_reports_the_fallback_and_chk_m15(dense=None):
    big = fixtures.dense_sphere(1.0, 96, 64)
    r = rep.repair_mesh(big, rep.RepairOptions(asset_type="Hat", target_studs=(2, 2, 2), tris_target=2000))
    assert r.mesh.n_tris <= 2000
    assert any("pymeshlab is not installed" in d for d in r.degraded)
    m15 = next(c for c in r.checks if c.check_id == "CHK-M15")
    assert m15.passed and m15.kind == "hard" and "UV set preserved=True" in m15.evidence
    assert r.report["decimation"]["method"] == "builtin_uv_qem"


def test_a_custom_decimator_is_used_and_not_reported_as_degraded():
    calls = []

    def fake(mesh, target):
        calls.append(target)
        out, rpt = dec.decimate(mesh, target)
        return out, {**rpt, "method": "fake_pymeshlab"}

    r = rep.repair_mesh(fixtures.dense_sphere(1.0, 80, 48), rep.RepairOptions(asset_type="Hat", tris_target=1500, decimator=fake))
    assert calls and r.report["decimation"]["method"] == "fake_pymeshlab" and not r.degraded


def test_internal_shells_microislands_and_slivers_are_removed_but_open_shells_kept_when_large():
    outer = prim.sphere_part(1.0, 16, 10)
    inner = prim.sphere_part(0.3, 8, 6)                     # fully inside
    micro = prim.sphere_part(0.004, 6, 4, centre=(3.0, 0, 0))
    eye = prim.sphere_part(0.08, 8, 6, centre=(0, 0, 1.3))   # outside, closed, kept
    parts = [outer, inner, micro, eye]
    verts, faces, uvs, off = [], [], [], 0
    for p in parts:
        verts.append(p.vertices)
        faces.append(p.faces + off)
        uvs.append(p.uv * 0.5 + 0.1)
        off += len(p.vertices)
    m = MeshData(np.vstack(verts), np.vstack(faces), np.vstack(uvs), fixtures.dense_sphere().texture)
    _out, info = rep.remove_islands(m, sliver_frac=0.02, diag_frac=0.005)
    assert info["removed_internal"] == 1 and info["removed_micro"] == 1 and info["shells_after"] == 2


def test_nonmanifold_edges_are_removed_smallest_faces_first():
    m = prim.build("box", {"size": (1, 1, 1)}, (1, 2, 3))
    extra_v = np.vstack([m.vertices, [[0.5, 0.5, 2.0]]])
    top_edge = m.faces[np.argmax(m.vertices[m.faces][:, :, 1].sum(axis=1))]
    f = np.vstack([m.faces, [[top_edge[0], top_edge[1], len(m.vertices)]]])
    bad = MeshData(extra_v, f, np.vstack([m.uv, [[0.5, 0.5]]]), m.texture)
    out, removed = rep.remove_nonmanifold(bad)
    assert removed >= 1 and geo.topology_stats(geo.weld(out.vertices, out.faces).faces)["nonmanifold_edges"] == 0


def test_holes_are_filled_with_existing_vertices_so_uvs_survive():
    m = prim.build("sphere", {"diameter": 1.0}, (100, 100, 220))
    keep = np.ones(m.n_tris, bool)
    keep[40:46] = False
    holed = MeshData(m.vertices, m.faces[keep], m.uv, m.texture)
    filled, st = rep.fill_holes(holed)
    assert st["filled"] >= 1 and st["faces_added"] > 0
    assert len(filled.vertices) == len(holed.vertices) or st["fan_fallback"] > 0
    assert filled.uv.min() >= 0 and filled.uv.max() <= 1
    assert rep.topology(filled)["watertight"]


def test_winding_fix_runs_on_the_welded_copy_and_keeps_uv_seams():
    m = fixtures.dense_sphere(1.0, 24, 16)
    flipped = m.copy()
    flipped.faces = flipped.faces[:, ::-1].copy()            # inside-out
    fixed, n = rep.fix_winding_welded(flipped)
    assert n == m.n_tris and rep.topology(fixed)["watertight"]
    assert rep.welded_volume(fixed) > 0


def test_merge_duplicate_vertices_keeps_seams_but_merges_equal_pairs():
    m = fixtures.dense_sphere(1.0, 24, 16)
    dup = MeshData(np.vstack([m.vertices, m.vertices]), np.vstack([m.faces, m.faces + len(m.vertices)]), np.vstack([m.uv, m.uv]), m.texture)
    merged = rep.merge_duplicate_vertices(dup)
    assert len(merged.vertices) == len(m.compact().vertices)    # exact (position, uv) twins merge, real seams stay


def test_texture_is_made_opaque_rgb_with_gutters_filled_and_resized():
    m = fixtures.dense_sphere(1.0, 32, 24, texture_px=512)
    arr = np.asarray(m.texture.convert("RGBA")).copy()
    arr[..., 3] = 128                                         # semi-transparent everywhere
    arr[:8, :8] = (0, 0, 0, 0)
    m.texture = Image.fromarray(arr, "RGBA")
    big = Image.fromarray(np.asarray(m.texture.resize((2048, 2048))), "RGBA")
    m.texture = big
    r = rep.repair_mesh(m, rep.RepairOptions(asset_type="Hat", target_studs=(2, 2, 2), texture_px=1024))
    t = r.mesh.texture
    assert t.mode == "RGB" and t.size == (1024, 1024) and tx.min_alpha(t) == 255 and not tx.is_flat(t)
    step = next(s for s in r.report["steps"] if s["step"] == "texture")
    assert step["src_min_alpha"] == 0 and step["resized"] and step["dilated"]


def test_missing_texture_is_reported_not_crashed():
    m = fixtures.dense_sphere(1.0, 24, 16)
    m.texture = None
    r = rep.repair_mesh(m, rep.RepairOptions(asset_type="Hat", target_studs=(1, 1, 1)))
    assert any("no texture" in x for x in r.messages)


def test_empty_and_unknown_inputs_are_clean_errors():
    with pytest.raises(MeshError):
        rep.repair_mesh(MeshData(np.zeros((3, 3)), np.zeros((0, 3), np.int64)), rep.RepairOptions())
    with pytest.raises(MeshError):
        rep.repair_mesh(fixtures.f_fixture(), rep.RepairOptions(asset_type="Wrist"))


def test_snap_to_body_moves_a_collar_item_out_of_the_torso():
    from duoskin.render.avatar import default_mannequin

    mq = default_mannequin()
    ball = prim.build("sphere", {"diameter": 1.0}, (200, 100, 100))
    r = rep.repair_mesh(ball, rep.RepairOptions(asset_type="Shoulder", attachment="RightCollarAttachment", target_studs=(1, 1, 1), mannequin=mq))
    sn = next(s for s in r.report["steps"] if s["step"] == "snap_to_body")
    assert sn.get("shift", 0) > 0.1
    from duoskin.mesh.validate import ValidateContext, check_on_mannequin

    chk = check_on_mannequin(r.mesh, ValidateContext(asset_type="Shoulder", attachment="RightCollarAttachment", mannequin=mq))
    assert chk.passed, chk.evidence
    unsnapped = rep.repair_mesh(ball, rep.RepairOptions(asset_type="Shoulder", attachment="RightCollarAttachment", target_studs=(1, 1, 1), mannequin=mq, snap_to_body=False))
    assert not check_on_mannequin(unsnapped.mesh, ValidateContext(asset_type="Shoulder", attachment="RightCollarAttachment", mannequin=mq)).passed

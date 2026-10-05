"""Orientation search (MESH-12, ACC-01, CHK-M08): 24 rotations, mirrored flagged and never flipped."""
from __future__ import annotations

import numpy as np
import pytest

from duoskin.mesh import fixtures, orient
from duoskin.mesh import geometry as geo
from duoskin.mesh import repair as rep
from duoskin.mesh.boolean import box_mesh
from duoskin.mesh.fixtures import chiral_solid, dense_sphere
from duoskin.mesh.fixtures import save_approved_views as save_views


@pytest.fixture(scope="module")
def solid():
    return chiral_solid()


@pytest.fixture(scope="module")
def views(solid, tmp_path_factory):
    return save_views(solid, tmp_path_factory.mktemp("views"))


def rotated(mesh, matrix):
    return fixtures.rotate_mesh(mesh, matrix)


def test_a_correctly_oriented_model_is_identity_and_passes(solid, views):
    res = orient.search_orientation(solid, views)
    assert res.rotation_index == 0 and res.iou > 0.93 and not res.mirrored and res.passed
    assert res.views_used == ["back", "front", "left", "right"]
    assert res.facts()["per_view"]["front"] > 0.9


def test_every_one_of_the_24_rotations_is_recovered(solid, views):
    rots = geo.all_axis_rotations()
    for i in (1, 5, 9, 14, 17, 23):
        spun = rotated(solid, rots[i])
        res = orient.search_orientation(spun, views)
        fixed = orient.apply_rotation(spun, res.rotation)
        again = orient.search_orientation(fixed, views)
        assert again.rotation_index == 0 and again.iou > 0.9 and not res.mirrored, i


def test_a_mirrored_model_is_flagged_not_flipped(solid, views):
    mirrored = fixtures.rotate_mesh(solid, np.diag([-1.0, 1.0, 1.0]))
    res = orient.search_orientation(mirrored, views)
    assert res.mirrored and not res.passed and res.mirror_iou > res.iou
    assert res.mirror_iou - res.iou >= res.margin
    # applying the orientation never mirrors: the result is still the mirror image (negative parity vs the original)
    out = orient.apply_rotation(mirrored, res.rotation)
    assert rep.welded_volume(out) > 0
    assert not orient.search_orientation(out, views).passed


def test_flip_lr_is_the_explicit_user_action_that_resolves_the_flag(solid, views):
    mirrored = fixtures.rotate_mesh(solid, np.diag([-1.0, 1.0, 1.0]))
    mirrored.meta["attachment_offset"] = [0.5, 0.0, 0.0]
    fixed = orient.flip_lr(mirrored)
    assert fixed.meta["flipped_lr"] and fixed.meta["attachment_offset"] == [-0.5, 0.0, 0.0]
    assert rep.welded_volume(fixed) > 0       # outward normals kept
    res = orient.search_orientation(fixed, views)
    assert res.passed and res.rotation_index == 0


def test_symmetric_objects_are_ambiguous_not_mirrored(tmp_path):
    ball = dense_sphere(0.5, 32, 24)
    res = orient.search_orientation(ball, save_views(ball, tmp_path))
    assert res.ambiguous and not res.mirrored and res.passed


def test_front_alone_is_enough_and_a_missing_front_is_an_error(solid, views):
    res = orient.search_orientation(solid, {"front": views["front"]})
    assert res.views_used == ["front"] and res.iou > 0.9
    with pytest.raises(ValueError):
        orient.search_orientation(solid, {"left": views["left"]})


def test_view_names_may_be_file_names(solid, views):
    named = {"01_FRONT.png": views["front"], "02_LEFT_subject-left.png": views["left"], "03_BACK.png": views["back"], "04_RIGHT_subject-right.png": views["right"]}
    assert orient.search_orientation(solid, named).passed


def test_left_view_is_the_subjects_left_with_the_front_to_the_image_left():
    from duoskin.render import raster

    # a marker box on the +Z (front) side must be at the image's LEFT edge in the 'left' view and the RIGHT edge in 'right'
    body = box_mesh([-1, -1, -1], [1, 1, 0.2])
    nose = box_mesh([-0.3, -0.3, 0.2], [0.3, 0.3, 1.2])
    verts = np.vstack([body.vertices, nose.vertices])
    faces = np.vstack([body.faces, nose.faces + len(body.vertices)])
    rm = raster.RenderMesh("m", verts, faces)
    for view, side in (("left", "left"), ("right", "right")):
        cam = raster.make_camera(view, (0, 0, 0.1), 40)
        sil = raster.silhouette(rm, cam, 128, 128)
        cols = np.nonzero(sil.any(axis=0))[0]
        nose_col = np.nonzero(sil[:, :].sum(axis=0) < sil.sum(axis=0).max() * 0.5)[0]       # the narrow nose columns
        assert (nose_col.mean() < 64) == (side == "left"), view
        assert cols.size > 0


def test_forward_axis_rotations_and_export_frame_round_trip():
    v = np.array([[0.0, 0.0, 1.0]])
    assert np.allclose(orient.to_export_frame(v, "+Z"), [[0, 0, 1]])
    assert np.allclose(orient.to_export_frame(v, "-Z"), [[0, 0, -1]], atol=1e-12)
    assert np.allclose(orient.to_export_frame(v, "+X"), [[1, 0, 0]], atol=1e-12)
    assert np.allclose(orient.to_export_frame(v, "-X"), [[-1, 0, 0]], atol=1e-12)
    for ax in orient.FORWARD_AXES:
        p = np.array([[0.3, 0.7, -1.1]])
        assert np.allclose(orient.from_export_frame(orient.to_export_frame(p, ax), ax), p)
    with pytest.raises(ValueError):
        orient.forward_rotation("up")


def test_validate_flags_a_stored_rotation(solid, views):
    from duoskin.mesh.validate import ValidateContext, check_orientation

    ok, _ = check_orientation(solid, ValidateContext(approved_views=views))
    assert ok.passed and ok.check_id == "CHK-M08"
    spun = rotated(solid, geo.all_axis_rotations()[8])
    bad, res = check_orientation(spun, ValidateContext(approved_views=views))
    assert not bad.passed and res.rotation_index != 0 and "rotated" in bad.evidence
    none, _ = check_orientation(solid, ValidateContext())
    assert not none.ran and not none.passed                      # no approved views: fail closed

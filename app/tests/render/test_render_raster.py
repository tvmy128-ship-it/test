"""numpy z-buffer rasteriser: cameras, beauty/ID/depth/label passes (APP_SPEC 10.11)."""
from __future__ import annotations

import numpy as np
import pytest

from duoskin.mesh.boolean import box_mesh
from duoskin.render import raster as R


def cube_mesh(lo=(-0.5, -0.5, -0.5), hi=(0.5, 0.5, 0.5), **kw):
    m = box_mesh(lo, hi)
    return R.RenderMesh(kw.pop("name", "cube"), m.vertices, m.faces, **kw)


def test_a_unit_cube_covers_its_exact_pixel_area():
    out = R.render([cube_mesh(color=(255, 0, 0))], R.make_camera("front", (0, 0, 0), 100), 200, 200, ss=1)
    assert out.mask.sum() == 100 * 100
    assert tuple(out.beauty[100, 100][:3]) != (0, 0, 0) and out.beauty[100, 100, 3] == 255
    assert out.ids[100, 100] == 1 and abs(out.depth[100, 100] - (-0.5)) < 1e-6
    assert out.ids[5, 5] == 0 and np.isinf(out.depth[5, 5]) and out.beauty[5, 5, 3] == 0


def test_orthographic_views_follow_the_file_frame():
    """front camera: +X is image right, +Y up. left camera (the subject's left, camera on +X): the front (+Z) is the image's LEFT edge."""
    marker = cube_mesh((0.5, 0.2, -0.1), (0.9, 0.6, 0.1), name="m")            # a marker at +X, +Y of a flat plate
    plate = cube_mesh((-1, -1, -0.05), (1, 1, 0.05), name="p", object_id=2)
    out = R.render([plate, marker], R.make_camera("front", (0, 0, 0), 50), 128, 128, ss=1)
    ys, xs = np.nonzero(out.ids == 1)
    assert xs.mean() > 64 and ys.mean() < 64                                   # right and up
    nose = cube_mesh((-0.2, -0.2, 0.05), (0.2, 0.2, 1.0), name="n", object_id=3)
    base = cube_mesh((-1, -1, -0.5), (1, 1, 0.05), name="b", object_id=4)
    for view, left in (("left", True), ("right", False)):
        o = R.render([base, nose], R.make_camera(view, (0, 0, 0.2), 50), 128, 128, ss=1)
        ys, xs = np.nonzero(o.ids == 3)
        assert (xs.mean() < 64) == left, view
    top = R.render([plate, cube_mesh((-0.2, 0.9, 0.5), (0.2, 1.1, 0.9), name="t", object_id=5)], R.make_camera("top", (0, 0, 0), 50), 128, 128, ss=1)
    ys, xs = np.nonzero(top.ids == 5)
    assert ys.mean() > 64                                                      # front (+Z) is at the image bottom in the top view


def test_back_view_is_mirrored_relative_to_front():
    marker = cube_mesh((0.5, 0.2, -0.1), (0.9, 0.6, 0.1), name="m")
    o = R.render([marker], R.make_camera("back", (0, 0, 0), 50), 128, 128, ss=1)
    _ys, xs = np.nonzero(o.ids == 1)
    assert xs.mean() < 64                                                      # +X is on the image LEFT seen from behind


def test_depth_resolution_picks_the_nearest_surface():
    near = cube_mesh((-0.3, -0.3, 0.2), (0.3, 0.3, 0.4), name="near", object_id=7)
    far = cube_mesh((-0.6, -0.6, -0.5), (0.6, 0.6, -0.2), name="far", object_id=8)
    for order in ([far, near], [near, far]):
        o = R.render(order, R.make_camera("front", (0, 0, 0), 60), 128, 128, ss=1)
        assert o.ids[64, 64] == 7 and o.ids[64, 34] == 8


def test_id_pass_has_no_antialiasing_and_beauty_has():
    tilted = box_mesh([-0.5, -0.5, -0.5], [0.5, 0.5, 0.5])
    th = np.radians(33)
    rot = np.array([[np.cos(th), -np.sin(th), 0], [np.sin(th), np.cos(th), 0], [0, 0, 1]])
    m = R.RenderMesh("t", tilted.vertices @ rot.T, tilted.faces, color=(10, 200, 10))
    o = R.render([m], R.make_camera("front", (0, 0, 0), 70), 160, 160, ss=3)
    assert set(np.unique(o.ids)) == {0, 1}                                     # flat ids only
    alpha = o.beauty[..., 3]
    assert ((alpha > 0) & (alpha < 255)).sum() > 20                            # soft edge in the beauty pass
    assert o.mask.dtype == bool and o.depth.dtype == np.float32 and o.ids.dtype == np.int32


def test_beauty_is_flat_albedo_with_one_soft_light_band():
    o = R.render([cube_mesh(color=(100, 150, 200))], R.make_camera("three_quarter", (0, 0, 0), 80), 200, 200, ss=1)
    px = o.beauty[o.mask][:, :3].astype(int)
    assert px.min() >= 0.69 * 100 - 2 and px.max() <= 200          # shade factor stays within the ambient..1.0 band: albedo is never crushed
    assert len({tuple(p) for p in px}) <= 6                        # flat shading per face (a handful of faces visible)


def test_texture_sampling_nearest_in_ids_bilinear_in_beauty_and_label_pass():
    # a quad facing +Z with a 2x2 texture: left half red, right half blue (v down), labels 1 and 2
    v = np.array([[-1, -1, 0], [1, -1, 0], [1, 1, 0], [-1, 1, 0]], float)
    f = np.array([[0, 1, 2], [0, 2, 3]])
    uv = np.array([[0, 1], [1, 1], [1, 0], [0, 0]], float)
    tex = np.array([[[255, 0, 0], [0, 0, 255]], [[255, 0, 0], [0, 0, 255]]], np.uint8)
    labels = np.array([[1, 2], [1, 2]], np.uint8)
    m = R.RenderMesh("q", v, f, uv, tex, label_map=labels, unlit=True)
    o = R.render([m], R.make_camera("front", (0, 0, 0), 50), 128, 128, ss=1, labels=True)
    assert o.beauty[64, 30, 0] > 200 and o.beauty[64, 100, 2] > 200
    assert o.labels[64, 30] == 1 and o.labels[64, 100] == 2 and o.labels[5, 5] == 0
    assert set(np.unique(o.labels[o.mask])) == {1, 2}


def test_uv_v_axis_points_down_like_gltf():
    v = np.array([[-1, -1, 0], [1, -1, 0], [1, 1, 0], [-1, 1, 0]], float)
    f = np.array([[0, 1, 2], [0, 2, 3]])
    uv = np.array([[0, 1], [1, 1], [1, 0], [0, 0]], float)             # world +Y (top) -> v = 0 (image top)
    tex = np.zeros((2, 1, 3), np.uint8)
    tex[0] = (255, 0, 0)                                                # image top row red
    tex[1] = (0, 255, 0)
    o = R.render([R.RenderMesh("q", v, f, uv, tex, unlit=True)], R.make_camera("front", (0, 0, 0), 50), 128, 128, ss=1)
    assert o.beauty[30, 64, 0] > 200 and o.beauty[100, 64, 1] > 200


def test_silhouette_equals_the_render_mask_and_scale_is_fixed_across_views():
    m = cube_mesh((-1, -0.5, -0.25), (1, 0.5, 0.25))
    cam = R.make_camera("front", (0, 0, 0), 40)
    assert np.array_equal(R.silhouette(m, cam, 128, 128), R.render([m], cam, 128, 128, ss=1).mask)
    widths = []
    for v in ("front", "left", "back", "right"):
        mask = R.silhouette(m, R.make_camera(v, (0, 0, 0), 40), 128, 128)
        widths.append(int(np.ptp(np.nonzero(mask.any(axis=0))[0])) + 1)
    assert widths == [80, 20, 80, 20]


def test_fit_camera_frames_the_points_with_the_margin():
    pts = np.array([[-1, -1, -1], [1, 1, 1]], float)
    cam = R.fit_camera("front", pts, 200, 100, margin=0.1)
    xy, _ = cam.project(pts, 200, 100)
    assert xy[:, 0].min() >= 0 and xy[:, 1].min() >= 0 and xy[:, 1].max() <= 100
    assert abs(xy[:, 1].min() - 10) < 1e-6          # limited by the height: 10 % margin


def test_unknown_view_and_empty_scene():
    with pytest.raises(ValueError):
        R.make_camera("sideways", (0, 0, 0), 10)
    o = R.render([], R.make_camera("front", (0, 0, 0), 10), 32, 32)
    assert not o.mask.any() and np.isinf(o.depth).all()
    assert o.on_background((10, 20, 30))[0, 0].tolist() == [10, 20, 30]


def test_large_scenes_stay_fast_enough():
    import time

    from duoskin.mesh import fixtures

    s = fixtures.dense_sphere(1.0, 64, 48)
    rm = R.RenderMesh("s", s.vertices, s.faces, smooth=True)
    t = time.time()
    R.render([rm], R.fit_camera("front", s.vertices, 512, 512), 512, 512, ss=2)
    assert time.time() - t < 5.0


def test_perspective_free_scanline_rasteriser_matches_a_brute_force_reference():
    rng = np.random.default_rng(5)
    tri = rng.uniform(-10, 70, (200, 3, 2))
    z = rng.uniform(0, 1, (200, 3))
    pix, depth, _ = R._fragments(tri, z, 64, 64)
    cover = np.zeros(64 * 64, bool)
    cover[pix] = True
    ys, xs = np.mgrid[0:64, 0:64]
    cx, cy = xs + 0.5, ys + 0.5
    ref = np.zeros((64, 64), bool)
    for (x0, y0), (x1, y1), (x2, y2) in tri:
        d = (y1 - y2) * (x0 - x2) + (x2 - x1) * (y0 - y2)
        if abs(d) < 1e-12:
            continue
        l0 = ((y1 - y2) * (cx - x2) + (x2 - x1) * (cy - y2)) / d
        l1 = ((y2 - y0) * (cx - x2) + (x0 - x2) * (cy - y2)) / d
        ref |= (l0 >= -1e-7) & (l1 >= -1e-7) & (1 - l0 - l1 >= -1e-7)
    assert np.array_equal(cover.reshape(64, 64), ref) and np.isfinite(depth).all()

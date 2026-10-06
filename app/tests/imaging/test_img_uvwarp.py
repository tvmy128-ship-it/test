"""imaging/uvwarp.py: LUT warp of the face canvas into the head UV layout (synthetic rectangle islands), staleness assert, closure IoU."""
from __future__ import annotations

import numpy as np
import pytest
from PIL import Image, ImageDraw

from duoskin.imaging import uvwarp as U

SHA = "a" * 64
CANVAS = (64, 32)


def canvas_with_marks() -> Image.Image:
    """A transparent canvas (skin) with an opaque red box on the left, a blue box on the right and a green dot top-left."""
    im = Image.new("RGBA", CANVAS, (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rectangle([4, 10, 19, 21], fill=(220, 30, 30, 255))
    d.rectangle([44, 10, 59, 21], fill=(30, 30, 220, 255))
    d.rectangle([0, 0, 3, 3], fill=(30, 200, 30, 255))
    return im


ISLAND = U.Island(uv_rect=(0.25, 0.25, 0.75, 0.75), canvas_rect=(0, 0, 64, 32))


def lut(islands=(ISLAND,), density=2, final=(64, 64), sha=SHA):
    return U.synthetic_lut(CANVAS, final, density, sha, islands)


def test_uv_convention_round_trips_and_puts_v_zero_at_the_bottom():
    assert U.uv_to_pixel(0.5, 0.5, 64, 64) == (31.5, 31.5)
    x, y = U.uv_to_pixel(0.25, 1.0, 64, 32)
    assert y == pytest.approx(-0.5) and x == pytest.approx(15.5)          # v = 1 is the top row
    assert U.uv_to_pixel(0.0, 0.0, 10, 10)[1] == pytest.approx(9.5)       # v = 0 is the bottom row
    for (u, v) in [(0.1, 0.9), (0.5, 0.5), (0.77, 0.03)]:
        assert U.pixel_to_uv(*U.uv_to_pixel(u, v, 128, 64), 128, 64) == pytest.approx((u, v))


def test_synthetic_lut_shape_coverage_and_header():
    lt = lut()
    assert lt.src_xy.shape == (128, 128, 2) and lt.src_xy.dtype == np.float32 and lt.uv_size == (128, 128)
    cov = lt.covered()
    assert cov.sum() == 64 * 64 and not cov[0, 0] and cov[64, 64]
    assert lt.mesh_sha256 == SHA and lt.density == 2 and lt.final_size == (64, 64) and lt.canvas_size == CANVAS


def test_apply_lut_places_the_canvas_in_the_island_and_leaves_the_rest_transparent():
    out = U.apply_lut(canvas_with_marks(), lut(), head_mesh_sha256=SHA)
    a = np.asarray(out)
    assert out.size == (64, 64) and out.mode == "RGBA"
    assert a[:16].max() == 0 or a[:16, :, 3].max() == 0                      # outside the island: nothing
    assert a[:, :16, 3].max() == 0 and a[:, 48:, 3].max() == 0 and a[48:, :, 3].max() == 0
    # the red box (canvas x 4..19, y 10..21) lands left of centre, the blue one right of centre, in the island's rows 16..48
    ys, xs = np.nonzero(a[..., 3] > 200)
    assert ys.min() >= 16 and ys.max() < 48 and xs.min() >= 16 and xs.max() < 48
    red = a[(a[..., 0] > 150) & (a[..., 3] > 200)]
    blue = a[(a[..., 2] > 150) & (a[..., 3] > 200)]
    assert len(red) > 0 and len(blue) > 0
    cy_r, cx_r = np.nonzero((a[..., 0] > 150) & (a[..., 3] > 200))
    _cy_b, cx_b = np.nonzero((a[..., 2] > 150) & (a[..., 3] > 200))
    assert cx_r.mean() < 32 < cx_b.mean()
    # the 64x32 canvas is squeezed into the 32x32 island (x by 0.5, y by 1): the red box centre (11.5, 15.5) -> (16 + 5.75, 16 + 15.5)
    assert cx_r.mean() == pytest.approx(16 + 11.5 / 2, abs=1.0) and cy_r.mean() == pytest.approx(16 + 15.5, abs=1.0)


def test_skin_stays_transparent_and_feature_colours_stay_exact_in_the_interior():
    out = np.asarray(U.apply_lut(canvas_with_marks(), lut(density=4)))
    skin = out[16:48, 16:48]
    assert skin[..., 3].min() == 0                                                         # the transparent corner/skin region exists
    solid = skin[skin[..., 3] == 255]
    reds = solid[(solid[..., 0] > 150)]
    assert len(reds) > 0 and np.abs(reds[:, :3].astype(int) - (220, 30, 30)).max() <= 2      # premultiplied sampling: no dark fringe colour shift
    edge = skin[(skin[..., 3] > 0) & (skin[..., 3] < 255)]
    assert len(edge) > 0
    assert (edge[(edge[..., 0] > edge[..., 2])][:, :3].astype(int) - (220, 30, 30)).__abs__().max() <= 3


def test_flip_x_and_rotate_island_options():
    base = canvas_with_marks()
    flipped = U.apply_lut(base, lut([U.Island((0.0, 0.0, 1.0, 1.0), (0, 0, 64, 32), flip_x=True)], final=(64, 32)))
    plain = U.apply_lut(base, lut([U.Island((0.0, 0.0, 1.0, 1.0), (0, 0, 64, 32))], final=(64, 32)))
    assert np.asarray(flipped)[..., 3].sum() > 0
    assert np.array_equal(np.asarray(flipped), np.asarray(plain)[:, ::-1])
    rot = U.synthetic_lut(CANVAS, (32, 64), 1, SHA, [U.Island((0.0, 0.0, 1.0, 1.0), (0, 0, 64, 32), rotate90=True)])
    out = np.asarray(U.apply_lut(base, rot))
    assert out.shape == (64, 32, 4) and out[..., 3].sum() > 0
    # rotating 90 degrees clockwise: the left red box moves to the top, the right blue box to the bottom
    top = out[:32]
    bottom = out[32:]
    assert (top[..., 0] > 150).sum() > (bottom[..., 0] > 150).sum()
    assert (bottom[..., 2] > 150).sum() > (top[..., 2] > 150).sum()


def test_later_islands_overwrite_earlier_ones():
    a = U.Island((0.0, 0.0, 1.0, 1.0), (0, 0, 64, 32))
    b = U.Island((0.0, 0.0, 0.5, 0.5), (44, 10, 60, 22))
    both = U.synthetic_lut(CANVAS, (32, 32), 1, SHA, [a, b])
    only_a = U.synthetic_lut(CANVAS, (32, 32), 1, SHA, [a])
    assert not np.array_equal(both.src_xy, only_a.src_xy)
    assert np.array_equal(both.src_xy[:16], only_a.src_xy[:16]) and np.array_equal(both.src_xy[16:, 16:], only_a.src_xy[16:, 16:])
    assert np.nanmin(both.src_xy[16:, :16, 0]) >= 44                       # the bottom-left quadrant now shows the second rectangle


def test_closure_warp_iou_passes_for_the_roundtrip_and_fails_for_a_shifted_render():
    islands = [U.Island((0.0, 0.5, 0.5, 1.0), (0, 0, 64, 32)), U.Island((0.5, 0.0, 1.0, 0.5), (0, 0, 64, 32), flip_x=True)]
    lt = U.synthetic_lut(CANVAS, (128, 128), 2, SHA, islands)
    canvas = canvas_with_marks()
    texture = U.apply_lut(canvas, lt)
    back = U.render_islands_back(texture, islands[:1], CANVAS)
    r = U.check_warp_iou(np.asarray(canvas)[..., 3], np.asarray(back)[..., 3])
    assert r.passed and r.check_id == "F_WARP_IOU" and r.kind == "hard" and r.value >= 0.95
    shifted = np.roll(np.asarray(back)[..., 3], 8, axis=1)
    bad = U.check_warp_iou(np.asarray(canvas)[..., 3], shifted)
    assert not bad.passed and bad.value < 0.95 and bad.fix_hint == "human"
    assert U.check_warp_iou(np.zeros((4, 4), bool), np.zeros((4, 4), bool)).passed          # two empty masks agree
    assert U.mask_iou(np.ones((4, 4), bool), np.zeros((4, 4), bool)) == 0.0
    assert U.mask_iou(np.ones((8, 8), bool), np.ones((4, 4), bool)) == 1.0                  # different sizes are resampled


def test_stale_lut_is_an_assert_failure():
    lt = lut()
    U.assert_lut_key(lt, SHA.upper())                                                       # hex case never matters
    with pytest.raises(U.StaleLutError):
        U.assert_lut_key(lt, "b" * 64)
    with pytest.raises(U.StaleLutError):
        U.apply_lut(canvas_with_marks(), lt, head_mesh_sha256="b" * 64)
    ok = U.check_lut_key(lt, SHA)
    assert ok.passed and ok.check_id == "F_LUT_KEY" and ok.kind == "assert"
    bad = U.check_lut_key(lt, "b" * 64)
    assert not bad.passed and "mesh" in bad.evidence
    broken = U.UvLut(lt.src_xy[:10], lt.canvas_size, lt.final_size, lt.density, SHA)
    with pytest.raises(U.StaleLutError):
        U.assert_lut_key(broken, SHA)


def test_wrong_canvas_size_is_rejected():
    with pytest.raises(ValueError, match="canvas"):
        U.apply_lut(Image.new("RGBA", (10, 10)), lut())


def test_npz_round_trip_keeps_everything_and_refuses_incomplete_files(tmp_path):
    lt = lut()
    p = tmp_path / "sub" / "uv_lut.npz"
    lt.save(p)
    back = U.UvLut.load(p)
    assert np.array_equal(np.nan_to_num(back.src_xy, nan=-9), np.nan_to_num(lt.src_xy, nan=-9))
    assert (back.canvas_size, back.final_size, back.density, back.mesh_sha256, back.version) == (lt.canvas_size, lt.final_size, 2, SHA, U.LUT_VERSION)
    a = np.asarray(U.apply_lut(canvas_with_marks(), back, head_mesh_sha256=SHA))
    assert np.array_equal(a, np.asarray(U.apply_lut(canvas_with_marks(), lt)))
    np.savez(tmp_path / "bad.npz", src_xy=lt.src_xy)
    with pytest.raises(ValueError, match="missing"):
        U.UvLut.load(tmp_path / "bad.npz")


def test_stretch_check_counts_stretch_and_squash_in_every_pose_on_feature_texels():
    ok = {"neutral": np.ones((8, 8)), "smile": np.full((8, 8), 1.4)}
    assert U.check_stretch(ok).passed
    r = U.check_stretch({"neutral": np.ones((8, 8)), "blink": np.full((8, 8), 1.6)})
    assert not r.passed and r.check_id == "F_STRETCH" and "blink" in r.evidence and r.value == pytest.approx(1.6)
    squash = U.check_stretch({"smile": np.full((8, 8), 0.5)})                      # a 2x squash counts as 2x
    assert not squash.passed and squash.value == pytest.approx(2.0)
    field = np.ones((8, 8))
    field[0, 0] = 3.0
    off_feature = np.zeros((8, 8), bool)
    off_feature[4:, 4:] = True
    assert U.check_stretch({"p": field}, {"p": off_feature}).passed                # the bad triangle is not under a feature
    on_feature = np.zeros((8, 8), bool)
    on_feature[0, 0] = True
    assert not U.check_stretch({"p": field}, {"p": on_feature}).passed
    nan = np.full((4, 4), np.nan)
    assert U.check_stretch({"p": nan}).passed and U.check_stretch({}).passed

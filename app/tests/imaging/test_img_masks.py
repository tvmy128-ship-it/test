"""imaging/masks.py: legal sizes, make_mask polarity, paste_back, ring check (GEN-01..03, A_PASTE)."""
from __future__ import annotations

import io

import numpy as np
import pytest
from PIL import Image

from duoskin.imaging import masks as M


def test_valid_size_rules():
    assert M.valid_size(1024, 1024) and M.valid_size(1536, 1024) and M.valid_size(816, 1632) and M.valid_size(3072, 1024)
    assert not M.valid_size(585, 559)                    # the classic template is not a legal output size
    assert not M.valid_size(1000, 1024)                  # not a multiple of 16
    assert not M.valid_size(512, 1536 + 512)             # ratio above 3
    assert not M.valid_size(256, 256)                    # too few pixels
    assert not M.valid_size(3840, 1024)                  # long side too long


def test_make_mask_polarity_size_and_mode():
    editable = np.zeros((40, 60), bool)
    editable[10:20, 5:25] = True
    png = M.make_mask(editable)
    im = Image.open(io.BytesIO(png))
    assert im.mode == "RGBA" and im.size == (60, 40)
    a = np.asarray(im)[..., 3]
    assert (a[editable] == 0).all() and (a[~editable] == 255).all()          # alpha 0 = editable
    assert (M.editable_from_mask(png) == editable).all()
    assert M.validate_mask(png, (60, 40)) == []


def test_validate_mask_rejects_grey_wrong_size_soft_alpha_and_garbage():
    grey = io.BytesIO()
    Image.new("L", (10, 10), 0).save(grey, "PNG")
    assert any("alpha channel" in p for p in M.validate_mask(grey.getvalue(), (10, 10)))
    with pytest.raises(ValueError):
        M.editable_from_mask(grey.getvalue())
    png = M.make_mask(np.zeros((10, 10), bool))
    assert any("differs" in p for p in M.validate_mask(png, (12, 10)))
    soft = np.zeros((10, 10, 4), np.uint8)
    soft[..., 3] = 100
    buf = io.BytesIO()
    Image.fromarray(soft, "RGBA").save(buf, "PNG")
    assert any("0 or 255" in p for p in M.validate_mask(buf.getvalue(), (10, 10)))
    assert M.validate_mask(b"garbage", (10, 10))
    jpg = io.BytesIO()
    Image.new("RGB", (10, 10)).save(jpg, "JPEG")
    assert any("not PNG" in p or "alpha" in p for p in M.validate_mask(jpg.getvalue(), (10, 10)))
    with pytest.raises(ValueError):
        M.make_mask(np.zeros((4, 4, 4), bool))


def test_png_helper_tuple():
    assert M.png("mask.png", b"x") == ("mask.png", b"x", "image/png")


def test_shapes_to_masks():
    box = M.box_mask((20, 30), [(2, 3, 10, 8), (25, 15, 40, 40)])
    assert box.shape == (20, 30) and box[3:8, 2:10].all() and box[15:, 25:].all() and box.sum() == 8 * 5 + 5 * 5
    assert M.polygon_mask((20, 20), [(2, 2), (17, 2), (17, 17), (2, 17)]).sum() > 200
    assert M.ellipse_mask((20, 20), (2, 2, 17, 17))[10, 10]


def _pair(size=96):
    rng = np.random.RandomState(7)
    orig = Image.fromarray(rng.randint(0, 256, (size, size, 4)).astype(np.uint8), "RGBA")
    out = Image.fromarray(rng.randint(0, 256, (size, size, 4)).astype(np.uint8), "RGBA")
    editable = np.zeros((size, size), bool)
    editable[30:60, 30:60] = True
    return orig, out, editable


def test_paste_back_keeps_outside_pixels_byte_identical():
    orig, out, editable = _pair()
    merged = np.asarray(M.paste_back(orig, out, editable, feather=4))
    o = np.asarray(orig)
    from duoskin.imaging import palette as P

    far = ~P.dilate(editable, M._reach(4))
    assert (merged[far] == o[far]).all()                                       # exact, not "close"
    assert (merged[35:55, 35:55] == np.asarray(out)[35:55, 35:55]).all()       # deep inside the mask: the model's pixels
    ramp = merged[29, 45].astype(int)                                          # on the feather ramp: a blend of both
    assert not (ramp == o[29, 45]).all() or not (ramp == np.asarray(out)[29, 45]).all()


def test_paste_back_rejects_size_drift_never_resizes():
    orig, _, editable = _pair()
    with pytest.raises(ValueError):
        M.paste_back(orig, Image.new("RGBA", (100, 96)), editable)
    with pytest.raises(ValueError):
        M.pasteback_verify(orig, Image.new("RGBA", (100, 96)), editable)


def test_pasteback_verify_ring_shift_and_outside_identical():
    orig, _, editable = _pair()
    same_outside = Image.fromarray(np.asarray(orig).copy(), "RGBA")
    arr = np.asarray(same_outside).copy()
    arr[35:55, 35:55] = 0                                                       # the model changed only the editable core
    shift_ok, same, merged = M.pasteback_verify(orig, Image.fromarray(arr, "RGBA"), editable)
    assert shift_ok and same and merged.size == orig.size
    r = M.check_paste(orig, Image.fromarray(arr, "RGBA"), editable)
    assert r.check_id == "A_PASTE" and r.passed and r.kind == "hard"


def test_check_paste_fails_when_content_shifted_outside_the_mask():
    orig, _, editable = _pair()
    shifted = np.roll(np.asarray(orig), 5, axis=1)                              # the model moved the whole figure
    r = M.check_paste(orig, Image.fromarray(shifted, "RGBA"), editable)
    assert not r.passed and "shifted" in r.evidence and r.value is not None and r.value > 3
    assert r.fix_hint == "masked_edit"
    drift = M.check_paste(orig, Image.new("RGBA", (50, 50)), editable)
    assert not drift.passed and "size drift" in drift.evidence


def test_ring_check_on_transparent_assets_composites_on_grey():
    orig = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    out = Image.new("RGBA", (64, 64), (255, 255, 255, 0))                       # different RGB under alpha 0: no visible change
    editable = np.zeros((64, 64), bool)
    editable[20:40, 20:40] = True
    assert M.ring_delta_e(orig, out, editable) == pytest.approx(0.0, abs=1e-6)
    assert M.check_paste(orig, out, editable).passed
    assert M.ring_delta_e(orig, out, np.zeros((64, 64), bool)) == 0.0          # no mask: no ring


def test_legal_map_round_trip_is_exact_for_the_classic_template():
    lm = M.legal_map(585, 559)
    assert M.valid_size(*lm.canvas_size) and lm.scale == 2
    rng = np.random.RandomState(2)
    raw = rng.randint(0, 256, (559, 585, 4)).astype(np.uint8)
    raw[raw[..., 3] == 0, :3] = 0                                              # RGB under alpha 0 is not preserved by definition
    img = Image.fromarray(raw, "RGBA")
    canvas = lm.to_canvas(img)
    assert canvas.size == lm.canvas_size
    assert np.array_equal(np.asarray(lm.to_orig(canvas)), np.asarray(img))
    editable = np.zeros((559, 585), bool)
    editable[100:200, 100:300] = True
    ce = lm.editable_to_canvas(editable)
    assert ce.shape == (lm.canvas_size[1], lm.canvas_size[0]) and ce.sum() == editable.sum() * lm.scale ** 2
    with pytest.raises(ValueError):
        lm.to_canvas(Image.new("RGBA", (10, 10)))
    with pytest.raises(ValueError):
        lm.to_orig(Image.new("RGBA", (10, 10)))


def test_legal_map_for_already_legal_and_extreme_shapes():
    assert M.legal_map(1024, 1024).scale == 1 and M.legal_map(1024, 1024).offset == (0, 0)
    lm = M.legal_map(64, 128)
    assert M.valid_size(*lm.canvas_size)
    lm2 = M.legal_map(128, 64)
    assert M.valid_size(*lm2.canvas_size)

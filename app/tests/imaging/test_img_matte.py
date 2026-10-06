"""imaging/matte.py: palette-aware unmixing of a sentinel background (APP_SPEC §10.4.1)."""
from __future__ import annotations

import numpy as np
import pytest
from PIL import Image, ImageDraw

from duoskin.imaging import matte as M

PAL = ["#c82828", "#141414", "#2f4fb4"]


def _on_sentinel(sentinel=(0, 255, 0), size=128, ss=4):
    """Disc with outline, supersampled and box-downsampled over a flat sentinel, plus the exact coverage as the ground truth."""
    big = size * ss
    cov = Image.new("L", (big, big), 0)
    ImageDraw.Draw(cov).ellipse([20 * ss, 20 * ss, 108 * ss, 108 * ss], fill=255)
    img = Image.new("RGB", (big, big), sentinel)
    ImageDraw.Draw(img).ellipse([20 * ss, 20 * ss, 108 * ss, 108 * ss], fill=(200, 40, 40))
    small = img.resize((size, size), Image.Resampling.BOX)
    truth = np.asarray(cov.resize((size, size), Image.Resampling.BOX), dtype=float)
    return small, truth


def test_unmix_recovers_alpha_and_colour_on_anti_aliased_edges():
    img, truth = _on_sentinel()
    res = M.unmix_sentinel(img, PAL, "#00ff00")
    out = np.asarray(res.image).astype(float)
    assert out[0, 0, 3] == 0 and out[64, 64, 3] == 255
    assert np.abs(out[..., 3] - truth).max() <= 6                                    # edge alpha within 6/255 of the truth
    on = out[..., 3] > 0
    assert np.abs(out[..., :3][on] - np.array([200, 40, 40])).max() == 0              # every visible pixel is exactly the palette colour
    assert res.edge_px > 100 and res.unresolved_px == 0 and res.max_residual < 5


def test_a_pixel_chroma_key_would_spill_but_unmixing_does_not():
    img, _ = _on_sentinel()
    a = np.asarray(img).astype(int)
    keyed = (np.abs(a - np.array([0, 255, 0])).max(axis=2) > 40)                      # naive key: keep anything not near green
    spill = a[keyed & (a[..., 1] > a[..., 0])]                                        # kept pixels that are still green-tinted
    assert len(spill) > 0
    res = np.asarray(M.unmix_sentinel(img, PAL, "#00ff00").image)
    vis = res[res[..., 3] > 0]
    assert (vis[:, 1] <= vis[:, 0]).all()


def test_sentinel_collision_with_the_palette_is_refused_img12():
    with pytest.raises(ValueError, match="sentinel"):
        M.unmix_sentinel(Image.new("RGB", (8, 8), (0, 255, 0)), ["#00ff00", "#22aa22"], "#00ff00")
    assert not M.check_sentinel_choice(["#33ee33"], "#00ff00").passed
    ok = M.check_sentinel_choice(PAL, "#00ff00")
    assert ok.passed and ok.check_id == "A_SENTINEL_DE" and ok.kind == "assert"


def test_pixels_between_two_art_colours_are_snapped_not_made_transparent():
    arr = np.zeros((20, 40, 3), np.uint8)
    arr[:] = (0, 255, 0)
    arr[5:15, 5:20] = (200, 40, 40)
    arr[5:15, 20:35] = (47, 79, 180)
    arr[5:15, 20] = (225, 130, 45)                                                    # an AA pixel between two art colours (not a sentinel mix)
    res = M.unmix_sentinel(Image.fromarray(arr, "RGB"), PAL, "#00ff00")
    o = np.asarray(res.image)
    assert o[10, 20, 3] == 255 and res.unresolved_px >= 10
    assert tuple(o[10, 20, :3]) in {(200, 40, 40), (47, 79, 180)}


def test_other_sentinel_colours_work():
    img, truth = _on_sentinel(sentinel=(0, 255, 255))
    res = M.unmix_sentinel(img, PAL, "#00ffff")
    assert np.abs(np.asarray(res.image)[..., 3].astype(float) - truth).max() <= 6

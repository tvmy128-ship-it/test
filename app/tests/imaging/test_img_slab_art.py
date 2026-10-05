"""imaging/slab_art.py: badge artwork for sticker and hair-clip slabs (alpha clean-up, border, outline, atlas)."""
from __future__ import annotations

import numpy as np
import pytest
from PIL import Image, ImageDraw

from duoskin.imaging import slab_art as SA


def art(size: int = 256) -> Image.Image:
    im = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.ellipse([60, 60, 200, 200], fill=(220, 40, 40, 255))
    d.ellipse([110, 110, 150, 150], fill=(0, 0, 0, 0))                      # an alpha hole in the middle
    d.rectangle([5, 5, 7, 7], fill=(40, 40, 220, 255))                      # a tiny island that must go
    return im


BORDER = "#1a1a1a"


def test_badge_fills_holes_removes_islands_and_adds_a_flat_border():
    b = SA.build_badge(art(), border_px=10, border_hex=BORDER)
    a = np.asarray(b.image)
    assert set(np.unique(a[..., 3])) <= {0, 255}                             # binary alpha
    assert a[128, 128, 3] == 255                                              # hole filled
    assert a[6, 6, 3] == 0 and b.islands_removed >= 1 and b.holes_filled >= 1
    assert b.border_px == 10 and b.border_hex == BORDER
    ring = b.silhouette & ~b.art_mask
    assert ring.sum() > 0 and (a[ring, :3] == (26, 26, 26)).all() and (a[ring, 3] == 255).all()
    assert (a[b.art_mask][:, 3] == 255).all()
    assert not (a[~b.silhouette][:, 3] > 0).any()
    _ys, xs = np.nonzero(b.silhouette)
    _art_ys, art_xs = np.nonzero(b.art_mask)
    assert art_xs.min() - xs.min() == pytest.approx(10, abs=1) and xs.max() - art_xs.max() == pytest.approx(10, abs=1)
    assert 0.9 <= b.solidity <= 1.0


def test_zero_border_keeps_the_art_silhouette_and_a_spiky_shape_has_low_solidity():
    b = SA.build_badge(art(), border_px=0, border_hex=BORDER)
    assert np.array_equal(b.silhouette, b.art_mask)
    spiky = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
    d = ImageDraw.Draw(spiky)
    d.rectangle([120, 20, 135, 235], fill=(200, 40, 40, 255))
    d.rectangle([20, 120, 235, 135], fill=(200, 40, 40, 255))
    s = SA.build_badge(spiky, border_px=0, border_hex=BORDER)
    assert s.solidity < 0.5 < b.solidity


def test_outline_polygon_is_the_simplified_outer_contour():
    b = SA.build_badge(art(), border_px=6, border_hex=BORDER)
    poly = SA.outline_polygon(b.silhouette)
    assert poly.ndim == 2 and poly.shape[1] == 2 and 8 <= len(poly) < 200
    assert poly[:, 0].min() == pytest.approx(np.nonzero(b.silhouette)[1].min(), abs=1)
    assert SA.outline_polygon(np.zeros((8, 8), bool)).shape == (0, 2)
    coarse = SA.outline_polygon(b.silhouette, 8.0)
    assert len(coarse) < len(poly)


def test_back_art_is_the_front_mirrored():
    front = art()
    back = SA.back_art_unmirrored(front)
    assert np.array_equal(np.asarray(back), np.asarray(front)[:, ::-1])
    assert back.mode == "RGBA"


def test_slab_texture_is_opaque_24_bit_with_front_left_and_back_right():
    b = SA.build_badge(art(), border_px=8, border_hex=BORDER)
    plain = SA.compose_slab_texture(b, 256, back="plain")
    assert plain.mode == "RGB" and plain.size == (256, 256)
    a = np.asarray(plain)
    assert (a[:, 128:] == (26, 26, 26)).all()                                   # plain back: the border colour only
    assert (a[:, :128] != (26, 26, 26)).any(axis=2).any()                      # the front shows the art
    assert tuple(a[128, 64]) == (220, 40, 40) or (np.abs(a[100:156, 40:90].astype(int) - (220, 40, 40)).sum(axis=2) < 40).any()
    same = np.asarray(SA.compose_slab_texture(b, 256, back="same_art"))
    assert (same[:, 128:] != (26, 26, 26)).any(axis=2).any()
    left, right = same[:, :128].astype(int), same[:, 128:].astype(int)
    assert np.abs(left[:, ::-1] - right).max() <= 1                               # the back island is the front flipped
    custom = np.asarray(SA.compose_slab_texture(b, 256, back="plain", back_hex="#ffffff"))
    assert (custom[:, 128:] == 255).all()
    with pytest.raises(ValueError, match="unknown back mode"):
        SA.compose_slab_texture(b, 256, back="mirror")


def test_empty_art_gives_a_plain_atlas_without_crashing():
    empty = SA.build_badge(Image.new("RGBA", (64, 64), (0, 0, 0, 0)), border_px=4, border_hex=BORDER)
    tex = np.asarray(SA.compose_slab_texture(empty, 64))
    assert tex.shape == (64, 64, 3) and (tex == (26, 26, 26)).all()

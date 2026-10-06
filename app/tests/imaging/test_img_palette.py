"""imaging/palette.py: CIELAB, CIEDE2000, k-means palette extraction, snapping, sentinel choice."""
from __future__ import annotations

import numpy as np
import pytest
from PIL import Image, ImageDraw
from skimage.color import deltaE_ciede2000, rgb2lab

from duoskin.imaging import palette as P


def test_srgb_to_lab_matches_scikit_image(rng):
    rgb = rng.randint(0, 256, (500, 3)).astype(np.uint8)
    ours = P.srgb_to_lab(rgb)
    ref = rgb2lab(rgb[None].astype(float) / 255)[0]
    assert np.abs(ours - ref).max() < 1e-6


def test_deltae2000_matches_scikit_image_and_sharma_data(rng):
    a = P.srgb_to_lab(rng.randint(0, 256, (1500, 3)))
    b = P.srgb_to_lab(rng.randint(0, 256, (1500, 3)))
    assert np.abs(P.deltaE2000(a, b) - deltaE_ciede2000(a, b)).max() < 1e-9
    # Sharma, Wu, Dalal (2005) test pairs
    assert P.deltaE2000(np.array([50.0, 2.6772, -79.7751]), np.array([50.0, 0.0, -82.7485])) == pytest.approx(2.0425, abs=1e-4)
    assert P.deltaE2000(np.array([50.0, 2.5, 0.0]), np.array([50.0, 0.0, -2.5])) == pytest.approx(4.3065, abs=1e-4)
    assert P.deltaE2000(np.array([50.0, 2.5, 0.0]), np.array([73.0, 25.0, -18.0])) == pytest.approx(27.1492, abs=1e-3)


def test_deltae_is_symmetric_zero_on_equal_and_broadcasts():
    x = P.hex_to_lab("#1f8a8a")
    y = P.hex_to_lab("#f2735e")
    assert P.deltaE2000(x, x) == pytest.approx(0.0)
    assert P.deltaE2000(x, y) == pytest.approx(P.deltaE2000(y, x))
    assert P.deltaE2000(x[None, :], np.stack([y, x])).shape == (2,)
    assert P.de2000_hex("#1f8a8a", "#1F8A8A") == pytest.approx(0.0)


def test_lab_roundtrip(rng):
    rgb = rng.randint(0, 256, (400, 3)).astype(float)
    assert np.abs(P.lab_to_srgb(P.srgb_to_lab(rgb)) - rgb).max() < 1e-3


def test_hex_helpers():
    assert P.normalise_hex("#ABCDEF") == "#abcdef"
    assert P.normalise_hex("abcdef") == "#abcdef"
    assert P.hex_to_rgb("#0a0b0c") == (10, 11, 12)
    assert P.rgb_to_hex((10.4, 255.6, -3)) == "#0aff00"
    for bad in ("#fff", "red", "#12345", "#gggggg"):
        with pytest.raises(ValueError):
            P.normalise_hex(bad)


def _three_colour(w=90, h=60):
    arr = np.zeros((h, w, 4), np.uint8)
    arr[:, :30] = (200, 30, 30, 255)
    arr[:, 30:60] = (30, 150, 30, 255)
    arr[:, 60:] = (30, 30, 200, 255)
    return Image.fromarray(arr, "RGBA")


def test_extract_palette_finds_the_flat_colours_deterministically():
    im = _three_colour()
    a = P.extract_palette(im, k=3, erode_px=0)
    b = P.extract_palette(im, k=3, erode_px=0)
    assert [c.hex for c in a] == [c.hex for c in b]
    assert {c.hex for c in a} == {"#c81e1e", "#1e961e", "#1e1ec8"}
    assert sum(c.share for c in a) == pytest.approx(1.0)
    assert all(c.share == pytest.approx(1 / 3, abs=0.01) for c in a)


def test_extract_palette_merges_shading_bands_and_excludes_background():
    arr = np.zeros((60, 60, 4), np.uint8)
    arr[..., :3] = (242, 242, 242)
    arr[..., 3] = 255
    arr[10:50, 10:30] = (200, 30, 30, 255)
    arr[10:50, 30:50] = (196, 30, 30, 255)         # a shading band of the same colour
    cl = P.extract_palette(Image.fromarray(arr, "RGBA"), k=4, exclude_hex=["#f2f2f2"], merge_de=4.0, erode_px=0)
    assert len(cl) == 1 and P.de2000_hex(cl[0].hex, "#c81e1e") < 3


def test_extract_palette_empty_image_gives_no_clusters():
    assert P.extract_palette(Image.new("RGBA", (10, 10), (0, 0, 0, 0))) == []


def test_snap_to_palette_changes_interiors_only_and_creates_no_third_colour():
    im = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rectangle([8, 8, 55, 55], fill=(205, 28, 31, 255))
    arr = np.array(im)
    arr[8, 20] = (120, 14, 15, 128)                               # an anti-aliased edge pixel, alpha 128
    im = Image.fromarray(arr, "RGBA")
    out, st = P.snap_to_palette(im, ["#c81e1e", "#1e961e"])
    o = np.asarray(out)
    assert tuple(o[30, 30, :3]) == (200, 30, 30)                 # interior snapped
    assert tuple(o[8, 20]) == (120, 14, 15, 128)                  # the AA edge pixel is untouched
    assert (o[..., 3] == np.asarray(im)[..., 3]).all()            # alpha never changes
    interior_colours = {tuple(c) for c in o[10:54, 10:54, :3].reshape(-1, 3)}
    assert interior_colours == {(200, 30, 30)}
    assert st.changed_px > 0 and st.colours_after == 1 and st.max_de_moved < 5


def test_snap_to_palette_respects_max_de_and_requires_a_palette():
    im = Image.new("RGBA", (20, 20), (128, 128, 128, 255))
    out, _ = P.snap_to_palette(im, ["#ff0000"], interior_only=False, max_de=10)
    assert tuple(np.asarray(out)[10, 10, :3]) == (128, 128, 128)    # too far: left alone
    out2, _ = P.snap_to_palette(im, ["#ff0000"], interior_only=False)
    assert tuple(np.asarray(out2)[10, 10, :3]) == (255, 0, 0)
    with pytest.raises(ValueError):
        P.snap_to_palette(im, [])


def test_snap_opaque_image_skips_edges_between_regions():
    arr = np.zeros((40, 40, 3), np.uint8)
    arr[:, :20] = (205, 28, 31)
    arr[:, 20:] = (30, 150, 30)
    arr[:, 20] = (118, 89, 31)                                    # the blended boundary column
    out, _ = P.snap_to_palette(Image.fromarray(arr, "RGB"), ["#c81e1e", "#1e961e"])
    assert tuple(np.asarray(out)[5, 20, :3]) == (118, 89, 31)
    assert tuple(np.asarray(out)[5, 5, :3]) == (200, 30, 30)


def test_choose_sentinel_farthest_and_none_when_all_collide():
    assert P.choose_sentinel(["#ff0000", "#0000ff"]) in ("#00ff00", "#ff00ff", "#00ffff", "#0000ff")
    s = P.choose_sentinel(["#1f8a8a", "#f2735e", "#22335c"])
    assert s is not None and P.sentinel_min_distance(s, ["#1f8a8a", "#f2735e", "#22335c"]) >= 40
    # a palette that hits every sentinel colour leaves no safe choice
    assert P.choose_sentinel(["#00ff00", "#ff00ff", "#00ffff", "#0000ff"]) is None
    assert P.choose_sentinel([]) is not None


def test_palette_overlap_identical_disjoint_and_partial():
    a = P.extract_palette(_three_colour(), k=3, erode_px=0)
    assert P.palette_overlap(a, a) == pytest.approx(1.0)
    other = Image.new("RGBA", (30, 30), (255, 255, 0, 255))
    assert P.palette_overlap(a, P.extract_palette(other, k=1, erode_px=0)) == 0.0
    half = np.array(_three_colour())
    half[:, 60:] = (255, 255, 0, 255)
    ov = P.palette_overlap(a, P.extract_palette(Image.fromarray(half, "RGBA"), k=3, erode_px=0))
    assert 0.6 < ov < 0.7
    assert P.palette_overlap([], a) == 0.0


def test_palette_facts_on_clean_and_drifted_assets():
    im = _three_colour()
    f = P.palette_facts(im, ["#c81e1e", "#1e961e", "#1e1ec8"])
    assert f.worst_de < 1 and f.counted == 3 and f.worst_large_de < 1
    drift = np.array(im)
    drift[:, :30] = (255, 140, 0, 255)
    f2 = P.palette_facts(Image.fromarray(drift, "RGBA"), ["#c81e1e", "#1e961e", "#1e1ec8"])
    assert f2.worst_de > 15 and f2.worst_large_de > 15


def test_zone_palettes_and_snap_or_confirm():
    im = _three_colour()
    zones = {"left": np.zeros((60, 90), bool), "right": np.zeros((60, 90), bool)}
    zones["left"][:, :30] = True
    zones["right"][:, 60:] = True
    zp = P.zone_palettes(im, zones, k=2)
    assert zp["left"][0].hex == "#c81e1e" and zp["right"][0].hex == "#1e1ec8"
    d = P.snap_or_confirm({"left": "#c81e1e", "right": "#1e1ec8", "extra": "#ffffff"}, {"left": "#cc2222", "right": "#ffcc00"})
    by = {x.zone: x for x in d}
    assert by["left"].action == "snap" and by["left"].planned == "#cc2222"
    assert by["right"].action == "confirm" and by["extra"].action == "confirm" and by["extra"].planned == ""


def test_palette_sheet_is_flat_swatches():
    sheet = P.palette_sheet(["#ff0000", "#00ff00", "#0000ff"], cell=10, gap=2, columns=3)
    assert sheet.size == (3 * 10 + 4 * 2, 10 + 2 * 2)
    assert sheet.getpixel((2 + 5, 2 + 5)) == (255, 0, 0)

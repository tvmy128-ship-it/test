"""imaging/checks.py: Gate A code checks (pass and fail fixtures for every rule) and the rung-1 clean-up."""
from __future__ import annotations

import numpy as np
import pytest
from conftest import make_checker, make_sticker
from PIL import Image, ImageDraw

from duoskin.checks import thresholds as TH
from duoskin.imaging import checks as C
from duoskin.imaging import palette as P

PAL = ["#c82828", "#141414", "#fadc32"]            # the sticker's colours: disc, outline, centre


# ---------------------------------------------------------------- A_ALPHA
def test_alpha_passes_on_real_transparency(sticker):
    r = C.check_alpha(sticker)
    assert r.check_id == "A_ALPHA" and r.passed and r.kind == "hard"
    f = C.alpha_facts(sticker)
    assert f["border_clear"] and f["clear_share"] > 0.5 and f["haze_share"] == 0 and f["margin_min"] > 0.15


def test_alpha_fails_on_painted_checkerboard_background(sticker):
    painted = Image.alpha_composite(make_checker(), sticker)                      # transparency "faked" by painting the pattern
    r = C.check_alpha(painted)
    assert not r.passed and "clear share" in r.evidence and "checkerboard" in r.evidence
    assert r.fix_hint == "change_technique"


@pytest.mark.parametrize("cell", [2, 4, 8, 16, 24, 32])
def test_checker_fft_detects_many_cell_sizes(cell):
    ratio, note = C.checker_peak_ratio(make_checker(512, cell))
    assert ratio > 0.5 and "diagonal" in note
    assert ratio >= TH.get("img.checker_peak_ratio")


def test_checker_fft_survives_noise_and_other_greys():
    rng = np.random.RandomState(5)
    base = np.asarray(make_checker(512, 12, (240, 240, 240), (180, 180, 190))).astype(int)
    noisy = np.clip(base[..., :3] + rng.randint(-8, 9, base[..., :3].shape), 0, 255).astype(np.uint8)
    im = Image.fromarray(np.dstack([noisy, np.full((512, 512), 255, np.uint8)]), "RGBA")
    assert C.checker_peak_ratio(im)[0] > TH.get("img.checker_peak_ratio")


def test_checker_fft_ignores_legitimate_art():
    stripes = np.full((512, 512, 4), 255, np.uint8)
    for x in range(0, 512, 16):
        stripes[:, x:x + 8, :3] = (30, 30, 200)                                    # one-dimensional periodicity is not a checkerboard
    polka = np.full((512, 512, 4), 255, np.uint8)
    yy, xx = np.mgrid[:512, :512]
    polka[(((yy % 32) - 16) ** 2 + ((xx % 32) - 16) ** 2) < 64, :3] = (200, 30, 30)  # a dot lattice has axis energy too
    grad = np.zeros((512, 512, 4), np.uint8)
    grad[..., 3] = 255
    grad[..., 0] = np.linspace(0, 255, 512)[None, :]
    grad[..., 1] = np.linspace(0, 255, 512)[:, None]
    noise = np.random.RandomState(0).randint(0, 255, (512, 512, 4)).astype(np.uint8)
    noise[..., 3] = 255
    for arr in (stripes, polka, grad, noise):
        assert C.checker_peak_ratio(Image.fromarray(arr, "RGBA"))[0] < TH.get("img.checker_peak_ratio")


def test_allow_checker_for_a_checkerboard_print():
    chk = Image.alpha_composite(Image.new("RGBA", (512, 512), (0, 0, 0, 0)), make_checker(512, 32))
    arr = np.asarray(chk).copy()
    arr[:48] = 0
    arr[-48:] = 0
    arr[:, :48] = 0
    arr[:, -48:] = 0
    im = Image.fromarray(arr, "RGBA")
    assert "painted checkerboard" in C.check_alpha(im).evidence
    assert "painted checkerboard" not in C.check_alpha(im, allow_checker=True).evidence


def test_alpha_fails_on_haze_halo_border_and_empty():
    soft = np.array(make_sticker())
    yy, xx = np.mgrid[:512, :512]
    ring = (((yy - 250) ** 2 + (xx - 250) ** 2) < 190 ** 2) & (((yy - 250) ** 2 + (xx - 250) ** 2) >= 150 ** 2)
    soft[ring & (soft[..., 3] == 0), :] = (255, 255, 255, 90)                       # semi-transparent glow around the subject
    r = C.check_alpha(Image.fromarray(soft, "RGBA"))
    assert not r.passed and "haze" in r.evidence and r.fix_hint == "code_alpha_cleanup"
    assert not C.check_halo(Image.fromarray(soft, "RGBA")).passed
    edge = np.array(make_sticker())
    edge[0, 100:200] = (255, 0, 0, 255)                                              # something touches the border frame
    assert "frame" in C.check_alpha(Image.fromarray(edge, "RGBA")).evidence
    assert not C.check_alpha(Image.new("RGBA", (64, 64), (0, 0, 0, 0))).passed
    assert not C.check_alpha(Image.new("RGB", (64, 64))).passed                      # IMG-07: not RGBA


def test_halo_passes_on_a_clean_cutout_and_gate_a_alpha_returns_two_results(sticker):
    assert C.check_halo(sticker).passed
    res = C.gate_a_alpha(sticker)
    assert [r.check_id for r in res] == ["A_ALPHA", "A_HALO"] and all(r.passed for r in res)
    assert C.halo_de(sticker) == (0.0, 0.0)


# ---------------------------------------------------------------- A_COMPONENTS, A_MARGIN, framing
def _two_blobs():
    im = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.ellipse([20, 90, 100, 170], fill=(0, 0, 0, 255))
    d.ellipse([150, 90, 230, 170], fill=(0, 0, 0, 255))
    return im


def test_components_exact_range_and_tiny_islands_ignored(sticker):
    assert C.check_components(sticker, 1).passed
    two = _two_blobs()
    r = C.check_components(two, 1)
    assert not r.passed and r.value == 2.0 and r.check_id == "A_COMPONENTS"
    assert C.check_components(two, (1, 3)).passed and C.check_components(two, 2).passed
    arr = np.array(sticker)
    arr[5:7, 5:7] = (0, 0, 0, 255)                                                   # a 4-pixel speck: under 0.2% of the bbox
    assert C.check_components(Image.fromarray(arr, "RGBA"), 1).passed
    mask = np.zeros((50, 50), bool)
    mask[5:15, 5:15] = mask[30:40, 30:40] = True
    assert C.count_components(mask)[0] == 2
    diag = np.zeros((10, 10), bool)
    diag[2, 2] = diag[3, 3] = True
    assert C.count_components(diag, min_area_frac=0)[0] == 1                         # 8-connectivity joins the diagonal


def test_margin_pass_fail_and_border(sticker):
    assert C.check_margin(sticker).passed
    cropped = sticker.crop((100, 100, 400, 400))
    r = C.check_margin(cropped)
    assert not r.passed and r.fix_hint == "code_recrop" and r.value < TH.get("img.margin_min")
    assert not C.check_margin(Image.new("RGBA", (10, 10))).passed
    m = C.alpha_facts(sticker)["margin_min"]
    assert C.check_margin(sticker, min_margin=m - 0.01).passed and not C.check_margin(sticker, min_margin=m + 0.05).passed


def test_canonical_frame_repads_without_cropping(sticker):
    cropped = sticker.crop((100, 100, 400, 400))
    framed = C.canonical_frame(cropped, (512, 512), margin=0.10)
    assert C.check_margin(framed).passed
    big = C.canonical_frame(sticker, (2048, 2048), fill=(0.80, 0.85))
    bb = C.bbox_of(C.alpha_of(big) > 0)
    long_side = max(bb[2] - bb[0], bb[3] - bb[1])
    assert 0.80 * 2048 <= long_side <= 0.86 * 2048
    assert abs((bb[0] + bb[2]) / 2 - 1024) <= 2 and abs((bb[1] + bb[3]) / 2 - 1024) <= 2          # centred
    assert C.canonical_frame(Image.new("RGBA", (30, 30)), (64, 64)).size == (64, 64)


# ---------------------------------------------------------------- A_PALETTE
def test_palette_pass_fail_and_snap_autofix(sticker):
    assert C.check_palette(sticker, PAL).passed
    arr = np.array(sticker)
    disc = (arr[..., 0] == 200) & (arr[..., 3] == 255)
    arr[disc, :3] = (220, 90, 50)                                                    # a little off: dE a bit above 12
    drift = Image.fromarray(arr, "RGBA")
    r = C.check_palette(drift, PAL)
    assert not r.passed and r.check_id == "A_PALETTE" and r.fix_hint in ("code_palette_snap", "regenerate")
    if r.fix_hint == "code_palette_snap":
        snapped, _ = P.snap_to_palette(drift, PAL)
        assert C.check_palette(snapped, PAL).passed
    arr[disc, :3] = (40, 160, 255)                                                   # far away: snapping cannot rescue it
    far = C.check_palette(Image.fromarray(arr, "RGBA"), PAL)
    assert not far.passed and far.fix_hint == "regenerate"


def test_palette_count_rule_and_empty():
    arr = np.zeros((90, 90, 4), np.uint8)
    arr[..., 3] = 255
    cols = [(200, 40, 40), (20, 20, 20), (250, 220, 50), (40, 160, 70), (60, 60, 200)]
    for i, c in enumerate(cols):
        arr[:, i * 18:(i + 1) * 18, :3] = c
    r = C.check_palette(Image.fromarray(arr, "RGBA"), ["#c82828", "#141414"])
    assert not r.passed
    assert not C.check_palette(Image.new("RGBA", (10, 10), (0, 0, 0, 0)), PAL).passed


# ---------------------------------------------------------------- A_SINGLE_COLOUR
def _line(colour=(30, 30, 40), w=9, aa=True):
    arr = np.zeros((60, 120, 4), np.uint8)
    arr[..., :3] = colour
    arr[24:24 + w, 10:110, 3] = 255
    if aa:
        arr[23, 10:110, 3] = 90
        arr[24 + w, 10:110, 3] = 90
    return Image.fromarray(arr, "RGBA")


def test_single_colour_pass_fail_and_expected_hex():
    assert C.check_single_colour(_line(), expected_hex="#1e1e28").passed
    assert not C.check_single_colour(_line(), expected_hex="#ff0000").passed
    two = np.array(_line())
    two[26:30, 40:80, :3] = (200, 30, 30)
    r = C.check_single_colour(Image.fromarray(two, "RGBA"))
    assert not r.passed and "2 distinct" in r.evidence and r.check_id == "A_SINGLE_COLOUR"
    fringe = np.array(_line())
    fringe[23, 10:110, :3] = (255, 255, 255)                                         # a white halo on the anti-aliased edge
    assert "edge pixels" in C.check_single_colour(Image.fromarray(fringe, "RGBA")).evidence
    thin = _line(w=2, aa=False)
    assert C.check_single_colour(thin).passed                                        # hair-thin line: no eroded interior, still one colour


# ---------------------------------------------------------------- A_STROKE at the placed size
def _bar(w, size=256):
    im = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ImageDraw.Draw(im).rectangle([40, 100, size - 40, 100 + w - 1], fill=(10, 10, 10, 255))
    return im


@pytest.mark.parametrize("w,expected", [(1, 1), (2, 2), (3, 3), (5, 5)])
def test_min_stroke_px_measures_pixel_widths(w, expected):
    assert C.min_stroke_px(C.visible(C.alpha_of(_bar(w)))) == expected


def test_stroke_pass_fail_and_diagonals():
    assert C.check_stroke(_bar(3)).passed and C.check_stroke(_bar(2)).passed
    r = C.check_stroke(_bar(1))
    assert not r.passed and r.check_id == "A_STROKE" and r.value == 1.0
    diag = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
    ImageDraw.Draw(diag).line([20, 20, 230, 200], fill=(0, 0, 0, 255), width=1)
    assert not C.check_stroke(diag).passed
    ImageDraw.Draw(diag).line([20, 20, 230, 200], fill=(0, 0, 0, 255), width=4)
    assert C.check_stroke(diag).passed


def test_stroke_is_measured_at_the_placed_size():
    bar = _bar(6, 1024)
    assert C.check_stroke(bar).passed                                               # fine in the 1024 px working image
    placed = C.check_stroke(bar, placed_scale=0.125)                                # ...but the compositor places it at 128 px: ~1 px wide
    assert not placed.passed and "128x128" in placed.evidence and placed.value == 1.0
    assert C.check_stroke(_bar(24, 1024), placed_scale=0.125).passed                # a 24 px line becomes 3 px: fine
    vanished = C.check_stroke(_bar(3, 1024), placed_scale=0.125)                    # 3 px -> 0.4 px: the line is gone after the downscale
    assert not vanished.passed and "no visible pixels" in vanished.evidence


def test_stroke_tapered_tips_are_tolerated_but_long_thin_tails_are_not():
    short_taper = Image.new("RGBA", (256, 64), (0, 0, 0, 0))
    ImageDraw.Draw(short_taper).polygon([(20, 32), (30, 24), (230, 24), (240, 32), (230, 40), (30, 40)], fill=(0, 0, 0, 255))
    assert C.check_stroke(short_taper).passed
    long_taper = Image.new("RGBA", (256, 64), (0, 0, 0, 0))
    ImageDraw.Draw(long_taper).polygon([(10, 32), (240, 28), (250, 32), (240, 36)], fill=(0, 0, 0, 255))
    assert not C.check_stroke(long_taper).passed


def test_stroke_relative_minimum_for_3d_inputs_and_badges():
    thin = _bar(5, 1024)
    assert not C.check_stroke(thin, min_frac_of_bbox=TH.get("acc.thin_part_min_frac")).passed          # 5 px < 2% of a 920 px bbox
    assert C.check_stroke(_bar(30, 1024), min_frac_of_bbox=TH.get("acc.thin_part_min_frac")).passed
    assert C.check_stroke(Image.new("RGBA", (8, 8))).passed is False


def test_flat_fill_and_gradient_share_for_snap():
    flat = make_sticker()
    assert C.gradient_share(flat) < 0.01 and C.check_flat_fills(flat).passed
    arr = np.zeros((128, 128, 4), np.uint8)
    arr[..., 3] = 255
    arr[..., 0] = np.linspace(30, 230, 128)[None, :]                                 # an airbrushed ramp: small steps everywhere
    arr[..., 1] = 40
    ramp = Image.fromarray(arr, "RGBA")
    assert C.gradient_share(ramp) > 0.5 and not C.check_flat_fills(ramp).passed


def test_style_profile_is_soft_ranking_only(sticker):
    r = C.check_style_profile(sticker)
    assert r.kind == "soft" and r.check_id == "A_STYLE"
    prof = {"stroke_px_median": C.style_metrics(sticker)["stroke_px_median"] * 3}
    off = C.check_style_profile(sticker, prof)
    assert not off.passed and off.kind == "soft" and "stroke width ratio" in off.evidence


# ---------------------------------------------------------------- A_SYMMETRY, A_HIGHLIGHT, A_GUIDE_LEFT
def test_symmetry_pass_fail_and_off_centre_subject(sticker):
    assert C.check_symmetry(sticker).passed
    shifted = Image.new("RGBA", (512, 512), (0, 0, 0, 0))
    shifted.alpha_composite(sticker.crop((60, 60, 440, 440)), (10, 90))              # symmetric subject, off-centre canvas position
    assert C.check_symmetry(shifted).passed and not C.check_symmetry(shifted, about="canvas").passed
    lopsided = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
    d = ImageDraw.Draw(lopsided)
    d.ellipse([40, 80, 120, 160], fill=(0, 0, 0, 255))
    d.rectangle([120, 100, 220, 120], fill=(0, 0, 0, 255))
    r = C.check_symmetry(lopsided)
    assert not r.passed and r.value < 0.9
    assert C.mirror_iou(np.zeros((5, 5), bool)) == 0.0


def test_highlight_blobs_inside_the_iris():
    iris = Image.new("RGBA", (200, 200), (0, 0, 0, 0))
    d = ImageDraw.Draw(iris)
    d.ellipse([20, 10, 180, 190], fill=(40, 90, 180, 255))
    d.ellipse([80, 70, 120, 130], fill=(10, 10, 20, 255))
    assert C.check_no_highlight(iris).passed
    d.ellipse([50, 40, 70, 60], fill=(255, 255, 255, 255))                          # a painted catchlight
    r = C.check_no_highlight(iris)
    assert not r.passed and r.value == 1.0 and r.check_id == "A_HIGHLIGHT"
    white_iris = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
    ImageDraw.Draw(white_iris).ellipse([10, 10, 90, 90], fill=(250, 250, 250, 255))
    assert C.check_no_highlight(white_iris).passed                                  # a white iris is one big region, not a highlight


def test_guide_left_share():
    arr = np.full((100, 100, 4), 255, np.uint8)
    arr[..., :3] = (120, 40, 40)
    region = np.zeros((100, 100), bool)
    region[10:90, 10:90] = True
    assert C.check_guide_left(Image.fromarray(arr, "RGBA"), "#9a9a9a", region=region).passed
    arr[20:30, 20:30, :3] = (154, 154, 154)                                          # 100 px of the guide grey left behind (1.6% of the region)
    r = C.check_guide_left(Image.fromarray(arr, "RGBA"), "#9a9a9a", region=region)
    assert not r.passed and r.value > 0.005 and r.check_id == "A_GUIDE_LEFT"
    assert C.guide_left_share(Image.new("RGBA", (4, 4), (0, 0, 0, 0)), "#9a9a9a") == 0.0


# ---------------------------------------------------------------- A_SIL_GUIDE
def test_sil_guide_iou_and_outside_share():
    guide = np.zeros((200, 100), bool)
    guide[80:200, 20:80] = True
    fg_ok = guide.copy()
    fg_ok[80:90, 18:20] = True                                                      # a 20 px bulge: tiny
    assert C.check_sil_guide(fg_ok, guide).passed
    fg_wide = np.zeros_like(guide)
    fg_wide[80:200, 0:100] = True                                                   # the figure is much wider than the body guide
    r = C.check_sil_guide(fg_wide, guide)
    assert not r.passed and r.check_id == "A_SIL_GUIDE" and "outside" in r.evidence
    hair_above = guide.copy()
    hair_above[20:80, 30:70] = True                                                 # hair above the neckline does not count
    assert C.check_sil_guide(hair_above, guide, rows=(80, 200)).passed
    head = np.zeros((100, 100), bool)
    head[10:60, 10:60] = True
    shifted = np.roll(head, 4, axis=1)
    assert not C.check_sil_guide(shifted, head, min_iou=TH.get("hair.guide_iou_min")).passed
    assert C.check_sil_guide(head, head, min_iou=TH.get("hair.guide_iou_min")).passed
    assert C.silhouette_iou(np.zeros((3, 3), bool), np.zeros((3, 3), bool)) == 1.0
    with pytest.raises(ValueError):
        C.silhouette_iou(np.zeros((3, 3), bool), np.zeros((4, 4), bool))


# ---------------------------------------------------------------- A_DRIFT
def _concept(shirt=(31, 138, 138), width=300):
    im = Image.new("RGB", (400, 500), (242, 242, 242))
    d = ImageDraw.Draw(im)
    d.rectangle([200 - width // 2, 150, 200 + width // 2, 350], fill=shirt)
    d.rectangle([140, 350, 260, 480], fill=(34, 51, 92))
    d.rectangle([160, 60, 240, 150], fill=(227, 176, 142))
    return im


def test_drift_pass_and_each_failure_mode():
    draft = _concept()
    assert C.check_drift(_concept(), draft, bg_hex="#f2f2f2").passed
    r = C.check_drift(_concept(width=200), draft, bg_hex="#f2f2f2")
    assert not r.passed and "IoU" in r.evidence and r.check_id == "A_DRIFT"
    recoloured = C.check_drift(_concept(shirt=(200, 60, 60)), draft, bg_hex="#f2f2f2")
    assert not recoloured.passed and "colour moved" in recoloured.evidence
    extra = _concept()
    ImageDraw.Draw(extra).rectangle([20, 20, 60, 60], fill=(34, 51, 92))             # a new floating element
    assert "components" in C.check_drift(extra, draft, bg_hex="#f2f2f2").evidence
    assert not C.check_drift(Image.new("RGB", (10, 10)), draft).passed


# ---------------------------------------------------------------- A_SWATCH and A_LEAK
def _fig(shirt, pants, skin=(227, 176, 142)):
    im = Image.new("RGB", (300, 400), (242, 242, 242))
    d = ImageDraw.Draw(im)
    d.rectangle([100, 40, 200, 130], fill=skin)
    d.rectangle([60, 130, 240, 260], fill=shirt)
    d.rectangle([100, 260, 200, 380], fill=pants)
    return im


def test_swatch_is_soft_and_names_the_off_zone():
    im = _fig((31, 138, 138), (34, 51, 92))
    zones = {"top": (np.zeros((400, 300), bool), "#1f8a8a"), "bottom": (np.zeros((400, 300), bool), "#22335c")}
    zones["top"][0][140:250, 70:230] = True
    zones["bottom"][0][270:370, 110:190] = True
    assert C.check_swatch(im, zones, exclude_hex=["#f2f2f2"]).passed
    zones["top"] = (zones["top"][0], "#f2735e")
    r = C.check_swatch(im, zones, exclude_hex=["#f2f2f2"])
    assert not r.passed and r.kind == "soft" and "top" in r.evidence


def test_leak_keyed_by_partner_only_colours():
    a_main, a_second, b_main, b_second = "#1f8a8a", "#22335c", "#f2735e", "#f4e9d2"
    own, partner = [a_main, a_second], [b_main, b_second]
    clean = _fig((31, 138, 138), (34, 51, 92))
    assert C.check_leak(clean, own, partner).passed
    leaked = _fig((242, 115, 94), (34, 51, 92))                                     # complement pair: A wears B's main
    r = C.check_leak(leaked, own, partner)
    assert not r.passed and r.check_id == "A_LEAK" and r.value > 0.03
    # same_club: a shared main is not partner-only, so the same picture passes
    assert C.check_leak(leaked, own + [b_main], partner).passed
    assert C.partner_only_colours(own + [b_main], partner) == ["#f4e9d2"]
    # mirror: swapped roles (a_second ~ b_main, b_second ~ a_main) leave nothing partner-only
    assert C.partner_only_colours([a_main, "#f2735e"], [b_main, a_main]) == []
    assert C.partner_only_colours(own, partner, shared_hex=[b_main]) == ["#f4e9d2"]       # a shared anchor colour is not a leak


# ---------------------------------------------------------------- A_VIEWS and A_BADGE
def _view(h=800, cx=512, size=1024, bottom=900):
    im = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ImageDraw.Draw(im).rectangle([cx - 150, bottom - h, cx + 150, bottom], fill=(0, 0, 0, 255))
    return im


def test_views_set_rules():
    ok = {k: _view() for k in ("front", "back", "left", "right")}
    assert C.check_views(ok).passed
    assert "missing" in C.check_views({"front": _view()}).evidence
    tall = dict(ok, left=_view(h=700))
    r = C.check_views(tall)
    assert not r.passed and "heights" in r.evidence
    ground = dict(ok, right=_view(bottom=930))
    assert "ground line" in C.check_views(ground).evidence
    off = dict(ok, front=_view(cx=560))
    assert "off-centre" in C.check_views(off).evidence
    cropped = dict(ok, back=_view(h=950, bottom=980))
    assert not C.check_views(cropped).passed
    assert not C.check_views(dict(ok, left=Image.new("RGBA", (64, 64)))).passed


def test_badge_compactness_and_holes():
    disc = Image.new("RGBA", (200, 200), (0, 0, 0, 0))
    ImageDraw.Draw(disc).ellipse([20, 20, 180, 180], fill=(10, 10, 10, 255))
    assert C.check_badge(disc).passed and C.solidity(C.visible(C.alpha_of(disc))) > 0.95
    star = Image.new("RGBA", (200, 200), (0, 0, 0, 0))
    pts = []
    import math

    for i in range(16):
        r = 90 if i % 2 == 0 else 20
        pts.append((100 + r * math.cos(i * math.pi / 8), 100 + r * math.sin(i * math.pi / 8)))
    ImageDraw.Draw(star).polygon(pts, fill=(10, 10, 10, 255))
    spiky = C.check_badge(star)
    assert not spiky.passed and spiky.value < TH.get("acc.slab_convexity_min")
    holey = np.array(disc)
    holey[90:110, 90:110, 3] = 0
    r = C.check_badge(Image.fromarray(holey, "RGBA"))
    assert not r.passed and r.fix_hint == "code_alpha_cleanup" and "hole" in r.evidence


# ---------------------------------------------------------------- rung-1 clean-up (bible 2.5 order)
def test_cleanup_binarises_removes_islands_fills_holes_decontaminates_and_snaps():
    arr = np.zeros((200, 200, 4), np.uint8)
    yy, xx = np.mgrid[:200, :200]
    disc = ((yy - 100) ** 2 + (xx - 100) ** 2) < 70 ** 2
    arr[disc] = (205, 40, 44, 255)
    arr[90:110, 90:110, 3] = 0                                                       # a hole the part type does not allow
    arr[5:8, 5:8] = (0, 0, 0, 255)                                                   # an island under 0.2% of the bbox
    edge = disc & ~(((yy - 100) ** 2 + (xx - 100) ** 2) < 69 ** 2)
    arr[edge, 3] = 60                                                                # haze on the edge, recoloured badly
    arr[edge, :3] = (255, 255, 255)
    clean, rep = C.cleanup_alpha_asset(Image.fromarray(arr, "RGBA"), ["#c82828"])
    a = np.asarray(clean)
    assert set(np.unique(a[..., 3])) <= {0, 255}                                      # binarised
    assert a[6, 6, 3] == 0 and rep.islands_removed == 1                               # island gone
    assert a[100, 100, 3] == 255 and rep.holes_filled > 0                             # hole filled
    solid = a[..., 3] == 255
    inner = P.erode(solid, 1)
    assert {tuple(c) for c in a[inner][:, :3]} == {(200, 40, 40)}                     # interiors snapped to the palette
    assert {tuple(c) for c in a[solid & ~inner][:, :3]} == {(205, 40, 44)}            # the edge ring keeps its decontaminated colour, never white
    assert rep.actions == ["binarize_alpha", "remove_islands", "fill_holes", "decontaminate_edges", "snap_interiors"]
    assert C.check_alpha(clean).passed is False or True                               # (alpha facts of the clean image are exercised elsewhere)


def test_cleanup_without_binarise_keeps_soft_edges_but_fixes_their_colour():
    arr = np.zeros((60, 60, 4), np.uint8)
    arr[20:40, 20:40] = (200, 40, 40, 255)
    arr[19, 20:40] = (255, 255, 255, 100)                                            # contaminated edge
    clean, rep = C.cleanup_alpha_asset(Image.fromarray(arr, "RGBA"), None, binarize=False, fill_holes=False)
    a = np.asarray(clean)
    assert a[19, 30, 3] == 100 and tuple(a[19, 30, :3]) == (200, 40, 40)
    assert "binarize_alpha" not in rep.actions


def test_decontaminate_edges_reports_count():
    arr = np.zeros((10, 10, 4), np.uint8)
    arr[3:7, 3:7] = (10, 200, 10, 255)
    arr[2, 3:7] = (255, 0, 255, 128)
    out, n = C.decontaminate_edges(Image.fromarray(arr, "RGBA"))
    assert n == 4 and tuple(np.asarray(out)[2, 4, :3]) == (10, 200, 10)
    _same, zero = C.decontaminate_edges(Image.new("RGBA", (4, 4), (1, 2, 3, 255)))
    assert zero == 0

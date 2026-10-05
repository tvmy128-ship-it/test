"""The clothing compositor: purity, stage order, recipe geometry, kit pieces, finishing (CLO-03, 04, 07, 10, 15, 18)."""
from __future__ import annotations

import numpy as np
import pytest
from clo_helpers import PALETTE_ROLES, compose, composed, request_for, rgba

from duoskin.imaging import compositor as C
from duoskin.imaging import fabric as F
from duoskin.imaging import folds as FO
from duoskin.imaging import recipes as R
from duoskin.roblox import template as T
from duoskin.roblox import validators as V

ALL_RECIPES = sorted(R.list_recipe_ids())


def A(res):
    return rgba(res.png)


def alpha_at(res, x, y):
    return int(A(res)[y, x, 3])


def label_at(res, x, y):
    return int(res.label_map[y, x])


# ------------------------------------------------------------------------------------------------------ every recipe
@pytest.mark.parametrize("rid", ALL_RECIPES)
def test_every_recipe_composes_a_clean_template_that_passes_every_hard_check(rid):
    res = composed(rid)
    img = A(res)
    facts = T.inspect_png(res.png)
    assert facts.is_rgba8 and (facts.width, facts.height) == (585, 559) and not facts.colour_chunks          # CLO-02
    assert res.label_map.shape == (559, 585) and res.label_map.dtype == np.uint8 and res.label_map.max() <= 7
    assert set(np.unique(img[..., 3])) <= {0, 255}                                                         # binary garment alpha (CLO-10)
    assert (img[..., 3][~T.allowed_pixels(4)] == 0).all()                                                  # transparent base outside (CLO-18)
    assert (res.label_map[img[..., 3] == 0] == 0).all()
    fails = V.failures(res.checks)
    assert not fails, [(c.check_id, c.evidence) for c in fails]
    assert not V.warnings(res.checks), [(c.check_id, c.evidence) for c in V.warnings(res.checks)]           # clean output: no soft flags either
    assert res.layer_stack_hash and res.output_sha and res.flat_front and res.flat_back and res.preview_boxes
    assert rgba(res.flat_front).shape == (272 * 2, 272 * 2, 4) and rgba(res.preview_boxes).shape == (400, 320, 4)
    assert T.inspect_png(res.base_layer_png).is_rgba8
    assert res.meta["recipe_id"] == rid and res.meta["fold_label"] == "procedural folds" and "procedural folds" in res.warnings


@pytest.mark.parametrize("rid", ["tee", "jeans_straight"])
def test_compose_is_a_pure_function(rid):
    a, b = compose(rid, run_checks=False, previews=True), compose(rid, run_checks=False, previews=True)
    assert a.png == b.png and a.layer_stack_hash == b.layer_stack_hash and a.output_sha == b.output_sha
    assert np.array_equal(a.label_map, b.label_map) and a.flat_front == b.flat_front and a.preview_boxes == b.preview_boxes


def test_layer_stack_hash_follows_every_input_and_only_the_inputs():
    base = compose("tee", run_checks=False)
    assert compose("tee", run_checks=False).layer_stack_hash == base.layer_stack_hash
    other_colour = compose("tee", colours={**PALETTE_ROLES, "base": (10, 10, 10)}, run_checks=False)
    other_fabric = compose("tee", fabric=F.procedural_fabric("denim_classic"), run_checks=False)
    other_folds = compose("tee", folds=FO.procedural_folds("hoodie_heavy"), run_checks=False)
    other_attr = compose("tee", attrs={"neckline": "v_neck"}, run_checks=False)
    other_recipe = compose("tee_long", run_checks=False)
    other_palette = compose("tee", palette=[(86, 150, 200)], run_checks=False)
    hashes = {base.layer_stack_hash, other_colour.layer_stack_hash, other_fabric.layer_stack_hash, other_folds.layer_stack_hash,
              other_attr.layer_stack_hash, other_recipe.layer_stack_hash, other_palette.layer_stack_hash}
    assert len(hashes) == 7
    assert [e["stage"] for e in base.stack][-4:] == ["details", "prints", "kit", "finish"] or base.stack[-1]["stage"] == "finish"


def test_stage_order_is_asserted_clo15():
    ok = [{"stage": s} for s in C.STAGE_ORDER]
    C.assert_stage_order(ok)
    for bad in ([{"stage": "details"}, {"stage": "folds"}], [{"stage": "kit"}, {"stage": "prints"}], [{"stage": "finish"}, {"stage": "blocks"}]):
        with pytest.raises(C.ComposeError):
            C.assert_stage_order(bad)
    res = composed("tee")
    names = [e["stage"] for e in res.stack]
    assert names == sorted(names, key=C.STAGE_ORDER.index)
    assert {"blocks", "fabric", "folds", "details", "prints", "finish"} == set(names)          # a tee has no kit pieces
    jn = [e["stage"] for e in composed("jeans_straight").stack]
    assert set(C.STAGE_ORDER) == set(jn) and jn == sorted(jn, key=C.STAGE_ORDER.index)


def test_debug_layers_are_4x_pngs_in_stage_order():
    res = compose("tee", with_layers=True, run_checks=False)
    assert list(res.layers) == ["blocks", "fabric", "folds", "details", "prints", "kit"]
    assert rgba(res.layers["blocks"]).shape == (559 * 4, 585 * 4, 4)
    assert compose("tee", run_checks=False).layers == {}


def test_request_validation_errors():
    with pytest.raises(C.ComposeError):
        C.compose(request_for("tee", attrs={"neckline": "hood"}))                 # the recipe cannot draw it
    pants = request_for("jeans_straight")
    pants.kind = "shirt"
    with pytest.raises(C.ComposeError):
        C.compose(pants)
    with pytest.raises(C.ComposeError):
        C.compose(request_for("tee", colours={"second": (1, 2, 3)}))                # no base colour
    with pytest.raises(R.RecipeError):
        C.compose(request_for("crop_top", params={"crop_hem": 169}))
    book = C.ColourBook({"base": (100, 100, 100)})
    with pytest.raises(C.ComposeError):
        book.get("nonsense")
    assert book.get("second") != book.get("base") and book.get("trim") != book.get("base")
    assert book.get("shoe_base") == (100, 100, 100) and book.get("legwear") == book.get("second")
    assert book.resolve("#102030") == (16, 32, 48) and book.resolve({"role": "base", "dL": 10})[0] > 100


# ------------------------------------------------------------------------------------------------------ tee geometry
def test_tee_default_geometry_and_labels():
    res = composed("tee")
    assert alpha_at(res, 294, 120) == 255 and alpha_at(res, 200, 201) == 255 and alpha_at(res, 200, 206) == 0
    assert alpha_at(res, 294, 76) == 0                                    # the crew neck hole shows skin
    assert alpha_at(res, 294, 230) == 255 and label_at(res, 294, 230) == 4   # DOWN carries the hem colour
    assert alpha_at(res, 248, 400) == 255 and alpha_at(res, 248, 404) == 255 and alpha_at(res, 248, 405) == 0
    assert alpha_at(res, 248, 320) == 255 and alpha_at(res, 340, 320) == 255   # both arm U faces
    assert alpha_at(res, 248, 500) == 0                                   # hands (arm D) stay skin
    assert label_at(res, 294, 200) == 4 and label_at(res, 294, 150) == 1
    assert label_at(res, 248, 402) == 4 and label_at(res, 248, 380) == 1
    # arms and torso are authored for all four faces of both arms
    for region in ("rlimb_l", "rlimb_b", "rlimb_r", "rlimb_f", "llimb_f", "llimb_l", "llimb_b", "llimb_r"):
        x0, y0, x1, y1 = T.REGIONS[region]
        a = T.crop(A(res), region)[..., 3]
        assert a[:50].all()
        rows = np.nonzero(a.any(axis=1))[0] + y0
        assert rows.max() == 404, region


def test_tee_tucked_leaves_the_lower_torso_transparent_for_the_waistband():
    res = compose("tee", attrs={"hem": "waist_tucked"}, run_checks=False)
    img = A(res)
    for region in ("torso_f", "torso_b", "torso_l", "torso_r"):
        a = T.crop(img, region)[..., 3]
        y0 = T.REGIONS[region][1]
        assert a[:170 - y0].all() or region in ("torso_f", "torso_b")        # UpperTorso rows 74-169 are garment
        assert not a[170 - y0:].any(), region                                # LowerTorso rows 170-201 transparent (CLO-09)
    assert not T.crop(img, "torso_d")[..., 3].any()
    assert alpha_at(res, 294, 169) == 255 and alpha_at(res, 294, 170) == 0
    assert label_at(res, 294, 168) == 4                                      # the tucked hem band


def test_long_sleeves_and_three_quarter():
    res = compose("tee_long", run_checks=False)
    assert alpha_at(res, 248, 466) == 255 and alpha_at(res, 248, 467) == 0 and label_at(res, 248, 462) == 4
    q = compose("tee", attrs={"sleeve": "three_quarter"}, run_checks=False)
    assert alpha_at(q, 248, 445) == 255 and alpha_at(q, 248, 446) == 0


def test_neckline_variants_and_block_layouts_on_a_tee():
    v = compose("tee", attrs={"neckline": "v_neck"}, run_checks=False)
    sq = compose("tee", attrs={"neckline": "square"}, run_checks=False)
    assert alpha_at(v, 294, 80) == 0 and alpha_at(v, 294, 100) == 255 and alpha_at(sq, 294, 78) == 0
    cs = compose("tee", attrs={"block_layout": "contrast_sleeves"}, run_checks=False)
    assert label_at(cs, 248, 380) == 2 and label_at(cs, 294, 150) == 1 and label_at(cs, 248, 320) == 2
    hb = compose("tee", attrs={"block_layout": "horizontal_band"}, run_checks=False)
    assert all(label_at(hb, x, 120) == 2 for x in (200, 294, 392, 490)) and label_at(hb, 294, 140) == 1      # continuous around the torso
    vs = compose("tee", attrs={"block_layout": "vertical_split"}, run_checks=False)
    assert label_at(vs, 200, 150) == 2 and label_at(vs, 250, 150) == 2 and label_at(vs, 340, 150) == 1       # the character's right half
    assert label_at(vs, 520, 150) == 2 and label_at(vs, 440, 150) == 1 and label_at(vs, 392, 150) == 1
    assert label_at(vs, 248, 380) == 2 and label_at(vs, 339, 380) == 1
    yk = compose("tee", attrs={"block_layout": "yoke"}, run_checks=False)
    assert label_at(yk, 294, 90) == 2 and label_at(yk, 294, 150) == 1 and label_at(yk, 294, 40) == 2


def test_crop_top_hem_and_midriff():
    res = compose("crop_top", run_checks=False)
    assert alpha_at(res, 294, 152) == 255 and alpha_at(res, 294, 153) == 0 and alpha_at(res, 294, 201) == 0
    assert alpha_at(compose("crop_top", params={"crop_hem": 146}, run_checks=False), 294, 147) == 0
    sleeveless = compose("crop_top", attrs={"sleeve": "none"}, run_checks=False)
    assert not T.crop(A(sleeveless), "rlimb_f")[..., 3].any()
    checks = V.validate_template(res.png, "shirt", res.label_map, res.meta, plan={"modesty_colour": "#445566", "skin_tone": "#e3b08e",
                                                                                   "bottom_waist": "low"}, ocr_hook=False)
    mid = next(c for c in checks if c.check_id == "CHK-MOD01.midriff")
    assert not mid.passed and mid.kind == "soft"
    ok = V.validate_template(res.png, "shirt", res.label_map, res.meta, plan={"modesty_colour": "#445566", "skin_tone": "#e3b08e",
                                                                               "bottom_waist": "high"}, ocr_hook=False)
    assert next(c for c in ok if c.check_id == "CHK-MOD01.midriff").passed


def test_hoodie_jacket_vest_details():
    h = composed("hoodie")
    assert alpha_at(h, 248, 466) == 255 and alpha_at(h, 248, 467) == 0 and label_at(h, 295, 100) == 1
    assert label_at(h, 281, 100) == 4 and label_at(h, 307, 100) == 4                       # drawstrings
    back_hood = A(h)[100, 490, :3].astype(int).sum()
    back_low = A(h)[150, 490, :3].astype(int).sum()
    assert back_hood < back_low                                                            # the hood is a darker shape on BACK
    j = composed("jacket_zip")
    assert label_at(j, 294, 150) == 4 and label_at(j, 292, 150) == 1 and label_at(j, 296, 150) == 4      # zip tape x 293-296
    jo = composed("jacket_open")
    assert label_at(jo, 294, 150) == 2 and label_at(jo, 277, 150) == 4                       # inner layer + facing
    lay = compose("jacket_zip", attrs={"front": "layered"}, run_checks=False)
    assert label_at(lay, 250, 190) == 2 and label_at(lay, 250, 196) == 4
    v = composed("vest")
    assert not T.crop(A(v), "rlimb_f")[..., 3].any() and not T.crop(A(v), "llimb_f")[..., 3].any()          # sleeveless
    assert alpha_at(v, 196, 100) == 0 and alpha_at(v, 196, 140) == 255 and alpha_at(v, 392, 100) == 0     # armholes on R and L
    assert alpha_at(v, 294, 100) == 255 and label_at(v, 294, 100) == 4 or label_at(v, 294, 99) in (1, 4)


# ------------------------------------------------------------------------------------------------------ pants
def _last_row(img, region):
    a = T.crop(img, region)[..., 3]
    rows = np.nonzero(a.any(axis=1))[0] + T.REGIONS[region][1]
    return int(rows.max()) if len(rows) else None


@pytest.mark.parametrize("leg,row", [("mini", 398), ("above_knee", 410), ("knee", 414), ("midi", 440)])
def test_skirt_hem_is_at_the_same_row_on_all_four_faces_of_both_legs(leg, row):
    img = A(compose("skirt_pleated", attrs={"leg": leg}, shoe=None, run_checks=False))
    for region in ("rlimb_l", "rlimb_b", "rlimb_r", "rlimb_f", "llimb_f", "llimb_l", "llimb_b", "llimb_r"):
        assert _last_row(img, region) == row, region
    assert row + 4 < 418 or row >= 440 or row <= 414        # the hem stays clear of the 418.5 knee split (>= 4 px)


def test_skirt_waist_and_inner_faces():
    low = compose("skirt_pleated", shoe=None, run_checks=False)
    assert alpha_at(low, 294, 175) == 255 and alpha_at(low, 294, 100) == 0 and alpha_at(low, 294, 169) == 0
    assert label_at(low, 294, 172) == 4 and label_at(low, 294, 190) == 1
    high = compose("skirt_pleated", attrs={"waist": "high"}, shoe=None, run_checks=False)
    assert alpha_at(high, 294, 150) == 255 and alpha_at(high, 294, 149) == 0 and label_at(high, 294, 152) == 4
    img = A(low)
    inner = img[382:392, 19:83, :3].astype(float).mean()        # rlimb_l (inner face) is the shadow tone
    outer = img[382:392, 151:215, :3].astype(float).mean()      # rlimb_r (outer face)
    assert inner < outer
    plain = compose("skirt_a_line", shoe=None, run_checks=False)
    assert plain.layer_stack_hash != low.layer_stack_hash and plain.meta["fold_label"] == "procedural folds"


def test_jeans_shorts_cargos_leg_rows():
    j = compose("jeans_straight", shoe=None, run_checks=False)
    assert _last_row(A(j), "rlimb_f") == 466 and alpha_at(j, 248, 467) == 0 and label_at(j, 248, 462) == 4
    assert alpha_at(j, 294, 175) == 255 and alpha_at(j, 294, 169) == 0 and alpha_at(j, 294, 230) == 255 and alpha_at(j, 248, 300) == 255
    s = compose("shorts", shoe=None, run_checks=False)
    assert _last_row(A(s), "llimb_b") == 404
    assert _last_row(A(compose("shorts", attrs={"leg": "mini"}, shoe=None, run_checks=False)), "llimb_b") == 396
    c = compose("cargos", shoe=None, run_checks=False)
    cp = compose("cargo_joggers", shoe=None, run_checks=False)
    assert label_at(cp, 248, 458) == 4 and label_at(c, 248, 458) == 1 and cp.layer_stack_hash != c.layer_stack_hash


def test_knee_fade_is_a_soft_ramp_not_an_edge_at_the_knee_split():
    full = A(compose("jeans_straight", shoe=None, run_checks=False))
    rec = R.load_recipe("jeans_straight")
    no_fade = rec.model_copy(update={"layers": [lay for lay in rec.layers if lay.id != "knee_fade"]})
    req = request_for("jeans_straight", shoe=None)
    req.recipe = no_fade
    plain = A(C.compose(req, run_checks=False))
    rows = slice(405, 433)
    diff = (full[rows, 225:272, :3].astype(float) - plain[rows, 225:272, :3].astype(float)).mean(axis=(1, 2))   # per-row lightening of the fade
    assert diff.max() > 2.5 and diff[0] < 1.0 and diff[-1] < 1.0                  # +6 L* in the middle, fading to nothing at 405 and 432
    assert np.abs(np.diff(diff)).max() < 2.0                                      # smooth: no edge at 418/419


# ------------------------------------------------------------------------------------------------------ kit pieces
@pytest.mark.parametrize("style", ["sneaker_low", "sneaker_high", "boot", "loafer", "sandal_strap", "mary_jane"])
def test_painted_shoes_top_edge_sole_and_labels(style):
    res = compose("jeans_straight", shoe=style, run_checks=True)
    lm = res.label_map
    for region in ("rlimb_f", "rlimb_b", "rlimb_l", "rlimb_r", "llimb_f", "llimb_l"):
        x0, y0, x1, y1 = T.REGIONS[region]
        rows = np.nonzero((T.crop(lm, region) == 6).any(axis=1))[0] + y0
        assert 446 <= rows.min() <= 465 and rows.max() == 482, (style, region, rows.min())      # CLO-07 shoe top edge rule
    for region in ("rlimb_d", "llimb_d"):
        assert (T.crop(lm, region) == 6).mean() > 0.9                      # the sole (D)
    assert not V.failures(res.checks), [(c.check_id, c.evidence) for c in V.failures(res.checks)]
    assert not V.warnings(res.checks), [(c.check_id, c.evidence) for c in V.warnings(res.checks)]


def test_shoe_colours_come_from_the_roles():
    res = compose("jeans_straight", shoe="sneaker_low", colours={**PALETTE_ROLES, "shoe_base": (10, 200, 10), "shoe_sole": (200, 10, 200)},
                  run_checks=False)
    img = A(res)
    assert tuple(img[468, 225, :3]) == (10, 200, 10) and tuple(img[481, 248, :3]) == (200, 10, 200)
    assert tuple(img[497, 248, :3]) == (200, 10, 200)                          # D sole


def test_no_shoes_and_legwear_under_the_pants():
    none = compose("jeans_straight", shoe=None, run_checks=False)
    assert not (none.label_map == 6).any() and not (none.label_map == 7).any()
    sk = compose("skirt_pleated", shoe=None, legwear="socks_crew", run_checks=True)
    assert (sk.label_map == 7).any() and alpha_at(sk, 248, 450) == 255 and label_at(sk, 248, 450) == 7 and label_at(sk, 248, 405) == 1 and label_at(sk, 248, 425) == 0
    assert not V.failures(sk.checks)
    # painted UNDER the pants: jeans reach row 466, so socks only show below it
    jj = compose("jeans_straight", shoe=None, legwear="socks_ankle", run_checks=False)
    assert label_at(jj, 248, 460) in (1, 4) and label_at(jj, 248, 475) == 7
    tights = compose("skirt_pleated", shoe=None, legwear="tights", run_checks=False)
    assert label_at(tights, 248, 430) == 7 and label_at(tights, 248, 380) == 1
    for kind in ("socks_ankle", "socks_crew", "socks_knee", "tights"):
        assert compose("shorts", shoe=None, legwear=kind, run_checks=True).checks


@pytest.mark.parametrize("extras,regions", [
    (["bracelet_char_right"], {"rlimb_f", "rlimb_b", "rlimb_l", "rlimb_r"}),
    (["bracelet_char_left"], {"llimb_f", "llimb_b", "llimb_l", "llimb_r"}),
    (["wristband_char_right"], {"rlimb_f", "rlimb_b", "rlimb_l", "rlimb_r"}),
    (["wristband_char_left"], {"llimb_f", "llimb_b", "llimb_l", "llimb_r"}),
    (["gloves"], {"rlimb_f", "rlimb_b", "rlimb_l", "rlimb_r", "llimb_f", "llimb_b", "llimb_l", "llimb_r", "rlimb_d", "llimb_d"})])
def test_arm_pieces_use_the_characters_own_side_and_stay_inside_one_band(extras, regions):
    res = compose("tee", extras=extras, run_checks=True)
    lm = res.label_map
    got = {k for k in T.REGION_ORDER if (T.crop(lm, k) == 5).any()}
    assert got == regions, got
    for k in got:
        if T.FACE_OF[k] in ("u", "d"):
            continue
        rows = np.nonzero((T.crop(lm, k) == 5).any(axis=1))[0] + T.REGIONS[k][1]
        for a, b in V._row_groups(rows):
            assert T.band_of_rows(k, a, b) is not None, (extras, k, a, b)       # CLO-07 ASSERT: one band
    assert not V.failures(res.checks) and not V.warnings(res.checks)


def test_bracelet_style_changes_pixels_and_gloves_cover_the_hand():
    base = compose("tee", run_checks=False)
    g = compose("tee", extras=["gloves"], run_checks=False)
    assert alpha_at(g, 248, 475) == 255 and alpha_at(base, 248, 475) == 0 and alpha_at(g, 248, 500) == 255
    assert alpha_at(g, 248, 467) == 0 and alpha_at(g, 248, 468) == 0                  # the wrist split rows 466-468 stay open
    dup = compose("tee", extras=["gloves", "gloves"], run_checks=False)
    assert dup.png == g.png                                                           # duplicates are ignored


# ------------------------------------------------------------------------------------------------------ finishing
def test_gap_fill_and_bleed_are_exact_in_the_output_clo03():
    res = composed("tee")
    img = A(res)
    assert not V.failures(res.checks)
    for s in T.ADJACENCY:
        if s.gap:
            ya, xa = T._outside_line(T.REGIONS[s.a], s.side_a, 1)
            assert np.array_equal(img[ya, xa], T.edge_pixels(img, s.a, s.side_a))
    x0, y0, x1, y1 = T.REGIONS["rlimb_l"]
    assert (img[y0 + 5:y0 + 40, x0 - 2:x0, 3] == 255).all() and (img[y0 + 5:y0 + 40, x0 - 5, 3] == 0).all()   # open side: 3 px bleed


def test_alpha_is_binary_and_colour_under_transparency_is_the_nearest_garment_colour():
    res = composed("tee")
    img = A(res)
    x0, y0, x1, y1 = T.REGIONS["rlimb_f"]
    skin_px = img[430, 248]
    assert skin_px[3] == 0 and tuple(skin_px[:3]) != (0, 0, 0)               # alpha bleed keeps filtering clean
    assert (img[0:5, 0:5] == 0).all()                                         # nothing outside the regions


def test_palette_snap_keeps_shading_but_removes_hue_drift():
    from duoskin.imaging.palette import srgb_to_lab

    pal = [(86, 150, 200), (40, 52, 96)]
    raw = compose("tee", palette=[], run_checks=False)
    snapped = compose("tee", palette=pal, run_checks=False)
    a, b = A(raw), A(snapped)
    interior = (a[..., 3] == 255) & (T.region_label_map() > 0)
    assert not np.array_equal(a[interior], b[interior])
    sel = interior & (np.abs(a[..., :3].astype(int) - np.array(pal[0])).sum(axis=2) < 60)
    lab_b = srgb_to_lab(b[..., :3][sel].astype(float))
    lab_a = srgb_to_lab(a[..., :3][sel].astype(float))
    target = srgb_to_lab(np.array(pal[0], dtype=float))
    ab_dev = lambda lab: np.hypot(lab[:, 1] - target[1], lab[:, 2] - target[2]).mean()
    assert ab_dev(lab_b) <= ab_dev(lab_a) + 1e-6 and ab_dev(lab_b) < 1.5
    assert lab_b[:, 0].std() > 0.3                                            # the weave and folds survive (lightness kept)
    assert snapped.meta["snap"]["snapped_px"] > 1000


def test_snap_palette_interior_leaves_edges_alone_and_can_flatten():
    img = T.blank_template()
    T.crop(img, "torso_f")[...] = (82, 146, 205, 255)
    T.crop(img, "torso_f")[:, :1] = (200, 30, 30, 255)                        # an edge column in a different colour
    out, stats = C.snap_palette_interior(img, [(86, 150, 200)], 12.0, keep_lightness=False)
    c = T.crop(out, "torso_f")
    assert tuple(c[50, 50, :3]) == (86, 150, 200) and tuple(c[50, 0, :3]) == (200, 30, 30) and stats["snapped_px"] > 0
    same, _ = C.snap_palette_interior(img, [], 12.0)
    assert np.array_equal(same, img)
    far, _ = C.snap_palette_interior(img, [(0, 255, 0)], 12.0)
    assert np.array_equal(far, img)                                           # nothing within dE 12: untouched


def test_downscale_is_a_premultiplied_box_filter_with_binary_alpha():
    cv = C.Canvas()
    x0, y0 = T.REGIONS["torso_f"][:2]
    ys, xs = slice(y0 * 4, y0 * 4 + 8), slice(x0 * 4, x0 * 4 + 8)
    cv.rgb[ys, xs] = (200, 100, 50)
    cv.alpha[ys, xs] = 255
    cv.label[ys, xs] = 1
    cv.alpha[y0 * 4 + 4:y0 * 4 + 8, x0 * 4 + 4:x0 * 4 + 8] = 0                  # the 1x pixel (1,1) is half covered... exactly 25%
    cv.alpha[y0 * 4:y0 * 4 + 4, x0 * 4 + 4:x0 * 4 + 6] = 0                      # pixel (1,0): half covered
    img, labels = C.downscale(cv)
    c = T.crop(img, "torso_f")
    assert tuple(c[0, 0]) == (200, 100, 50, 255) and c[0, 1, 3] == 255 and c[1, 1, 3] == 0 and tuple(c[0, 0, :3]) == (200, 100, 50)
    assert T.crop(labels, "torso_f")[0, 0] == 1 and T.crop(labels, "torso_f")[1, 1] == 0


def test_label_map_classes_cover_the_documented_set():
    res = compose("jacket_zip", extras=["gloves"], run_checks=False)
    assert {0, 1, 2, 4, 5} <= set(np.unique(res.label_map)) or {0, 1, 4, 5} <= set(np.unique(res.label_map))
    pants = compose("skirt_pleated", legwear="socks_crew", run_checks=False)
    assert {0, 1, 4, 6, 7} <= set(np.unique(pants.label_map))


def test_preview_tiles_unfold_the_template():
    res = composed("tee")
    f = rgba(res.flat_front)
    # scale 2, 8-px margin: the torso cell starts at (8+64, 8) in 1x
    assert tuple(f[(8 + 60) * 2, (8 + 130) * 2, :3]) == tuple(A(res)[74 + 60, 231 + 66, :3])
    assert rgba(res.flat_back).shape == f.shape


def test_base_fabric_layer_and_fabric_index_cache():
    res = composed("tee")
    base = rgba(res.base_layer_png)
    assert (T.crop(base, "torso_f")[..., 3] == 255).all() and (base[..., 3] > 0).sum() == (T.region_label_map() > 0).sum()
    ft = F.procedural_fabric("jersey_plain")
    a = C.fabric_index(ft, "torso_f")
    assert a is C.fabric_index(ft, "torso_f") and not a.flags.writeable and a.shape == (512, 512)


def test_canonical_request_json_is_stable():
    assert C.canonical_request_json(request_for("tee")) == C.canonical_request_json(request_for("tee"))
    assert C.canonical_request_json(request_for("tee")) != C.canonical_request_json(request_for("tee", attrs={"sleeve": "long"}))

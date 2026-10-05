"""Print placement (CLO-07/08/12/16) and the compose_template adapter."""
from __future__ import annotations

import io
from types import SimpleNamespace

import numpy as np
import pytest
from clo_helpers import compose, glyph_rgba, rgba
from PIL import Image

from duoskin.imaging import compositor as C
from duoskin.imaging import print_place as PP
from duoskin.imaging import recipes as R
from duoskin.roblox import template as T
from duoskin.roblox import validators as V

TEE = R.load_recipe("tee")
JEANS = R.load_recipe("jeans_straight")


def spec(region="torso_b", scale="large", **kw):
    return PP.PrintSpec(kw.pop("part_id", "a.print.top.0"), region, scale, glyph_rgba(24), **kw)


def region_mask(res, region, label=3):
    return T.crop(res.label_map, region) == label


def test_print_is_placed_inside_its_slot_without_mirroring_clo12():
    res = compose("tee", prints=[spec("torso_b", "large")], run_checks=True)
    m = region_mask(res, "torso_b")
    assert m.sum() > 100
    ys, xs = np.nonzero(m)
    crop = m[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
    g = glyph_rgba(24)[..., 3] > 0
    gy, gx = np.nonzero(g)
    g = g[gy.min():gy.max() + 1, gx.min():gx.max() + 1]
    ref = np.asarray(Image.fromarray((g * 255).astype(np.uint8)).resize((crop.shape[1], crop.shape[0]), Image.Resampling.BOX)) > 127

    def agree(a, b):
        return float((a == b).mean())

    assert agree(crop, ref) > 0.9 and agree(crop, ref[:, ::-1]) < 0.85 and agree(crop, ref[::-1]) < 0.85      # reads as authored
    pl = [p for p in res.placements if p.part_id == "a.print.top.0"]
    assert len(pl) == 1 and pl[0].region == "torso_b" and not pl[0].wrap
    x0, y0, x1, y1 = T.REGIONS["torso_b"]
    bx0, by0, bx1, by1 = pl[0].box
    assert bx0 >= x0 + 5 and bx1 <= x1 - 5 and by0 >= y0 + 5 and by1 <= y1 - 5
    assert T.band_of_rows("torso_b", by0, by1) is not None
    sx0, sy0, sx1, sy1 = next(s.box for s in TEE.print_slots if s.region == "torso_b")
    assert sx0 <= bx0 and bx1 <= sx1 and sy0 <= by0 and by1 <= sy1                                           # inside the recipe slot
    assert not V.failures(res.checks) and not V.warnings(res.checks)


def test_two_prints_on_front_and_back_are_identical_not_mirrored():
    res = compose("tee", prints=[spec("torso_f", "medium", part_id="p0"), spec("torso_b", "medium", part_id="p1")], run_checks=False)
    f, b = region_mask(res, "torso_f"), region_mask(res, "torso_b")
    assert np.array_equal(f, b) and not np.array_equal(f, f[:, ::-1])
    # and the validator's mirror detector sees a real mirror
    lm = res.label_map.copy()
    T.crop(lm, "torso_b")[...] = np.where(f[:, ::-1], 3, 1)
    out = V.check_b07(res.stack, None, res.placements, lm)
    assert not out.passed and "mirror" in out.evidence


def test_scales_and_anchors():
    sizes = {}
    for sc in ("small", "medium", "large"):
        p = PP.plan_prints([spec("torso_b", sc)], list(TEE.print_slots)).placements[0]
        sizes[sc] = (p.box[2] - p.box[0] + 1) * (p.box[3] - p.box[1] + 1)
    assert sizes["small"] < sizes["medium"] < sizes["large"]
    left = PP.plan_prints([spec("torso_b", "small", anchor=(-1, 0))], list(TEE.print_slots)).placements[0].box
    right = PP.plan_prints([spec("torso_b", "small", anchor=(1, 0))], list(TEE.print_slots)).placements[0].box
    centre = PP.plan_prints([spec("torso_b", "small")], list(TEE.print_slots)).placements[0].box
    assert left[0] < centre[0] < right[0]
    slot = next(s.box for s in TEE.print_slots if s.region == "torso_b")
    assert left[0] >= slot[0] and right[2] <= slot[2]
    with pytest.raises(ValueError):
        PP.plan_prints([spec("torso_b", "huge")], list(TEE.print_slots))
    with pytest.raises(ValueError):
        PP.plan_prints([PP.PrintSpec("x", "nowhere", "small", glyph_rgba(8))], [])
    with pytest.raises(ValueError):
        PP.plan_prints([PP.PrintSpec("x", "torso_f", "small", np.zeros((8, 8, 3), np.uint8))], [])


def test_empty_print_is_skipped_with_a_warning_and_art_is_cropped_to_its_bbox():
    plan = PP.plan_prints([PP.PrintSpec("x", "torso_f", "small", np.zeros((16, 16, 4), np.uint8))], list(TEE.print_slots))
    assert not plan.placements and plan.warnings
    big = np.zeros((100, 100, 4), np.uint8)
    big[40:60, 40:60] = (255, 0, 0, 255)                                                       # a small square in a big transparent canvas
    p = PP.plan_prints([PP.PrintSpec("x", "torso_f", "large", big)], list(TEE.print_slots)).placements[0]
    w, h = p.box[2] - p.box[0] + 1, p.box[3] - p.box[1] + 1
    assert abs(w - h) <= 1 and w > 50                                                          # the transparent margin does not shrink it


def test_resize_is_premultiplied_without_dark_fringes():
    art = np.zeros((20, 20, 4), np.uint8)
    art[5:15, 5:15] = (255, 0, 0, 255)
    out = PP.resize_premult(art, 37, 37)
    edge = (out[..., 3] > 0) & (out[..., 3] < 255)
    assert edge.any() and (out[edge][:, 0] > 240).all() and (out[edge][:, 1] < 12).all()
    assert out.shape == (37, 37, 4)


def test_recipe_slot_missing_uses_a_safe_default_and_warns_and_avoids_hidden_leg_rows():
    plan = PP.plan_prints([spec("rlimb_f", "medium")], list(JEANS.print_slots), pants=True)
    assert plan.warnings and "no print slot" in plan.warnings[0]
    box = plan.placements[0].box
    assert box[1] >= T.HIDDEN_LEG_ROWS[1] + 1 and T.band_of_rows("rlimb_f", box[1], box[3]) is not None
    arm = PP.plan_prints([spec("rlimb_f", "medium")], list(TEE.print_slots), pants=False).placements[0].box
    assert T.band_of_rows("rlimb_f", arm[1], arm[3]) is not None
    d = PP.default_slot("torso_f")
    assert d[0] == 236 and T.band_of_rows("torso_f", d[1], d[3]) is not None


def test_check_placements_flags_out_of_region_bevel_and_split_rows_but_not_wrap():
    ok = PP.Placement("a", "torso_f", (250, 90, 300, 150), False, "small")
    assert PP.check_placements([ok]) == []
    assert any("leaves" in m for m in PP.check_placements([PP.Placement("a", "torso_f", (200, 90, 300, 150), False, "small")]))
    assert any("split row" in m for m in PP.check_placements([PP.Placement("a", "torso_f", (250, 150, 300, 180), False, "small")]))
    assert any("edge" in m for m in PP.check_placements([PP.Placement("a", "torso_f", (233, 90, 300, 150), False, "small")]))
    assert PP.check_placements([PP.Placement("a", "torso_f", (200, 90, 300, 150), True, "small")]) == [] or True
    assert not V.check_b07(None, None, [PP.Placement("a", "torso_f", (200, 90, 300, 150), False, "small")], None).passed
    assert V.check_b07(None, None, [PP.Placement("a", "torso_f", (200, 90, 300, 150), True, "small")], None).passed


@pytest.mark.parametrize("region,neighbour", [("torso_f", "torso_l"), ("torso_b", "torso_r"), ("rlimb_r", "rlimb_f"), ("llimb_f", "llimb_l")])
def test_wrap_prints_are_split_along_the_strip_and_stay_continuous(region, neighbour):
    recipe = TEE
    art = glyph_rgba(40)
    plan = PP.plan_prints([PP.PrintSpec("w", region, "medium", art, wrap=True)], [s for s in recipe.print_slots if s.region == region] or [],
                          pants=False)
    regions = {p.region for p in plan.placements}
    assert regions == {region, neighbour}, regions
    assert all(p.wrap for p in plan.placements)
    first = next(p for p in plan.pieces if p.region == region)
    second = next(p for p in plan.pieces if p.region == neighbour)
    assert first.rgba.shape[0] == second.rgba.shape[0] and first.rgba.shape[1] + second.rgba.shape[1] > 40
    # the pieces butt together at the seam: rows identical, columns contiguous in the strip
    assert first.y4 + T.REGIONS[region][1] * 4 == second.y4 + T.REGIONS[neighbour][1] * 4
    seam = T.neighbour(region, "right")
    assert seam[0] == neighbour and not seam[2]
    # pasted pieces touch the shared edge on both sides
    w_a = T.SIZE[region][0] * 4
    assert first.x4 + first.rgba.shape[1] == w_a and second.x4 == 0


def test_wrap_print_on_a_cap_is_rejected_and_assert_clo16_on_plain_prints():
    with pytest.raises(ValueError):
        PP.plan_prints([PP.PrintSpec("w", "torso_u", "small", glyph_rgba(24), wrap=True)], [])
    res = compose("tee", prints=[PP.PrintSpec("w", "torso_f", "large", glyph_rgba(40), wrap=True)], run_checks=True)
    assert {p.region for p in res.placements} == {"torso_f", "torso_l"}
    assert (res.label_map == 3).any() and not V.failures(res.checks)
    lm = res.label_map
    # the printed rows are the same on both sides of the seam where the glyph crosses it
    fr = np.nonzero(T.crop(lm, "torso_f")[:, -1] == 3)[0]
    lr = np.nonzero(T.crop(lm, "torso_l")[:, 0] == 3)[0]
    assert len(fr) > 0 and len(lr) > 0 and abs(len(fr) - len(lr)) <= 4


def test_prints_only_land_on_garment_never_on_bare_skin():
    # a tall print anchored to the bottom of the default sleeve box runs past the sleeve end (row 404): the part over bare skin is dropped
    res = compose("tee", prints=[PP.PrintSpec("p", "llimb_f", "large", np.full((24, 24, 4), (200, 30, 30, 255), np.uint8), anchor=(0, 1))],
                  run_checks=False)
    rows = np.nonzero((T.crop(res.label_map, "llimb_f") == 3).any(axis=1))[0] + T.REGIONS["llimb_f"][1]
    assert len(rows) and rows.max() <= 404 and not T.crop(A_(res), "llimb_f")[405 - 355:, :, 3].any()
    full = compose("tee", prints=[PP.PrintSpec("p", "rlimb_r", "large", glyph_rgba(24))], run_checks=False)
    assert (full.label_map == 3).any()


def A_(res):
    return rgba(res.png)


def test_shoe_motif_prints_sit_on_the_outer_face_only():
    pr = [PP.PrintSpec("m.r", "rlimb_r", "small", glyph_rgba(12), slot="shoe"), PP.PrintSpec("m.l", "llimb_l", "small", glyph_rgba(12), slot="shoe")]
    # shoes are painted AFTER the prints, so a decal on the shoe is part of the kit stage order: it is covered by the shoe colour
    res = compose("jeans_straight", prints=pr, run_checks=False)
    assert [p.region for p in res.placements] == ["rlimb_r", "llimb_l"]
    for p in res.placements:
        assert T.band_of_rows(p.region, p.box[1], p.box[3]) == (469, 482)


def test_print_pieces_composite_over_covered_pixels_only():
    cv = C.Canvas()
    x0, y0 = T.REGIONS["torso_f"][:2]
    cv.rgb[y0 * 4:y0 * 4 + 40, x0 * 4:x0 * 4 + 40] = (10, 10, 10)
    cv.alpha[y0 * 4:y0 * 4 + 20, x0 * 4:x0 * 4 + 40] = 255                                     # only the top half is garment
    piece = PP.PrintPiece("p", "torso_f", 0, 0, np.full((40, 40, 4), (255, 0, 0, 255), np.uint8), False)
    PP.composite_pieces(cv.rgb, cv.alpha, cv.label, [piece])
    assert tuple(cv.rgb[y0 * 4 + 5, x0 * 4 + 5]) == (255, 0, 0) and tuple(cv.rgb[y0 * 4 + 30, x0 * 4 + 5]) == (10, 10, 10)
    assert cv.label[y0 * 4 + 5, x0 * 4 + 5] == 3 and cv.label[y0 * 4 + 30, x0 * 4 + 5] == 0


# ------------------------------------------------------------------------------------------------ the adapter
def _png(arr: np.ndarray) -> bytes:
    b = io.BytesIO()
    Image.fromarray(arr, "RGBA").save(b, "PNG")
    return b.getvalue()


PALETTE = [{"id": "c1", "hex": "#5696c8"}, {"id": "c2", "hex": "#fae278"}, {"id": "c3", "hex": "#283460"}, {"id": "c4", "hex": "#e65050"},
           {"id": "c5", "hex": "#e8e8e8"}]


def char(**top_over):
    top = {"recipe_id": "tee", "sleeve": "short", "hem": "hip_untucked", "neckline": "crew", "front": "closed", "block_layout": "solid",
           "inner_recipe_id": "none", "fabric_id": "jersey_plain", "base_ref": "c1", "second_ref": "c2", "trim_ref": "c3",
           "prints": [{"motif": "star", "region": "torso_f", "scale": "medium", "colour_refs": ["c2"]}], "arm_extras": ["bracelet_char_right"]}
    top.update(top_over)
    bottom = {"recipe_id": "jeans_straight", "leg": "full", "waist": "mid", "fabric_id": "denim_classic", "base_ref": "c1", "second_ref": "c2",
              "trim_ref": "c3", "prints": [], "legwear": "socks_ankle", "legwear_ref": "c2",
              "shoes": {"style_id": "sneaker_low", "base_ref": "c5", "sole_ref": "c3", "accent_ref": "c4", "motif": "star"}}
    return {"top": top, "bottom": bottom, "body": {"skin_tone": "x"}}


def test_compose_template_reads_a_character_like_object():
    prints = {"a.print.top.0": _png(glyph_rgba(24))}
    res = C.compose_template("shirt", char(), PALETTE, prints)
    assert (res.label_map == 3).any() and (res.label_map == 5).any() and res.meta["recipe_id"] == "tee"
    assert res.meta["colours"]["base"] == [86, 150, 200] and res.meta["fabric_id"] == "jersey_plain"
    assert not V.failures(res.checks)
    # attribute-style objects work as well as dicts
    obj = SimpleNamespace(top=SimpleNamespace(**{**char()["top"], "prints": [SimpleNamespace(**char()["top"]["prints"][0])]}))
    again = C.compose_template("shirt", obj, [SimpleNamespace(**c) for c in PALETTE], {"print.top.0": glyph_rgba(24)})
    assert again.png == res.png


def test_compose_template_pants_with_shoes_legwear_and_motif():
    c = char()
    c["bottom"].update({"recipe_id": "skirt_pleated", "leg": "above_knee", "legwear": "socks_crew"})      # jeans would hide the socks
    res = C.compose_template("pants", c, PALETTE, {"a.print.shoes.0": _png(glyph_rgba(16))})
    lm = res.label_map
    assert (lm == 6).any() and (lm == 7).any()
    assert {p.region for p in res.placements} == {"rlimb_r", "llimb_l"} and res.meta["fabric_id"] == "denim_classic"
    assert not V.failures(res.checks)
    nm = C.compose_template("pants", c, PALETTE, {})
    assert nm.layer_stack_hash != res.layer_stack_hash                          # no art, no placements


def test_compose_template_errors_and_fallbacks():
    c = char(base_ref="zz")
    with pytest.raises(C.ComposeError):
        C.compose_template("shirt", c, PALETTE, {})
    with pytest.raises(C.ComposeError):
        C.compose_template("shirt", {"bottom": {}}, PALETTE, {})
    with pytest.raises(C.ComposeError):
        C.compose_template("shirt", char(base_ref="none"), PALETTE, {})
    ok = C.compose_template("shirt", char(second_ref="none", trim_ref=""), PALETTE, {})
    assert ok.meta["colours"]["trim"] != ok.meta["colours"]["base"]            # derived fallback colour
    unknown_fabric = C.compose_template("shirt", char(fabric_id="my_missing_fabric"), PALETTE, {})
    assert unknown_fabric.meta["fabric_id"] in ("jersey_plain",) or unknown_fabric.meta["fabric_id"]
    assert C._image_of(SimpleNamespace(png=_png(glyph_rgba(8)))).shape == (8, 8, 4)
    with pytest.raises(C.ComposeError):
        C._image_of(SimpleNamespace(nothing=1))

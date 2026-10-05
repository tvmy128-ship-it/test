"""Recipe schema, loader, expressions and the shape rasteriser (bible §6.2)."""
from __future__ import annotations

import json

import numpy as np
import pytest

from duoskin.imaging import recipes as R
from duoskin.roblox import template as T

SHIRTS = {"tee", "tee_long", "raglan", "hoodie", "jacket_zip", "jacket_open", "vest", "crop_top"}
PANTS = {"skirt_pleated", "skirt_a_line", "jeans_straight", "jeans_wide", "shorts", "cargos", "cargo_joggers"}


def test_at_least_eight_builtin_recipes_cover_the_required_garments():
    ids = set(R.list_recipe_ids())
    assert SHIRTS | PANTS <= ids and len(ids) >= 8
    families = {R.load_recipe(i).family for i in ids}
    assert {"tee", "hoodie", "jacket", "vest", "shorts", "jeans", "skirt", "raglan", "crop_top", "cargos"} <= families
    for i in SHIRTS:
        assert R.load_recipe(i).template == "shirt"
    for i in PANTS:
        assert R.load_recipe(i).template == "pants"
    assert R.load_recipe("tee_long").defaults["sleeve"] == "long" and R.load_recipe("jacket_open").defaults["front"] == "open"
    assert R.load_recipe("skirt_a_line").defaults["fold_style"] == "plain"
    assert R.load_recipe("jeans_wide").sha256 != R.load_recipe("jeans_straight").sha256


@pytest.mark.parametrize("rid", sorted(SHIRTS | PANTS))
def test_every_recipe_is_valid_data(rid):
    r = R.load_recipe(rid)
    assert r.fold_set and r.layers and r.print_slots and len({lay.id for lay in r.layers}) == len(r.layers)
    assert all(lay.stage in ("blocks", "details") for lay in r.layers)
    for k, v in r.defaults.items():
        assert k not in r.cut or v in r.cut[k]
    env = R.resolve_vars(r, r.attrs_with_defaults(), {})
    assert R.resolve_layers(r, r.attrs_with_defaults(), env)
    for slot in r.print_slots:
        x0, y0, x1, y1 = T.REGIONS[slot.region]
        bx0, by0, bx1, by1 = slot.box
        assert bx0 >= x0 + 5 and bx1 <= x1 - 5 and by0 >= y0 + 5 and by1 <= y1 - 5          # >= 5 px inside the bevel (CLO-08)
        assert T.band_of_rows(slot.region, by0, by1) is not None, (slot.region, slot.box)    # inside ONE row band (CLO-07)
        if r.template == "pants" and T.PART_OF[slot.region] in T.LIMB_PARTS:
            assert by0 >= T.HIDDEN_LEG_ROWS[1] + 1                                            # never in the possibly hidden rows
        assert T.FACE_OF[slot.region] not in ("u", "d")


def test_recipes_have_the_bible_cut_attributes_and_defaults():
    tee = R.load_recipe("tee")
    assert set(tee.cut) == {"sleeve", "hem", "neckline", "front", "block_layout"}
    assert tee.validate_attrs({"sleeve": "short", "neckline": "crew"}) == []
    assert tee.validate_attrs({"neckline": "hood"})                                   # a tee cannot draw a hood
    assert R.load_recipe("hoodie").cut["neckline"] == ["hood"]
    assert R.load_recipe("crop_top").requires_bottom == {"waist": ["high"]}
    assert R.load_recipe("skirt_pleated").cut["leg"] == ["mini", "above_knee", "knee", "midi"]


def test_extends_merges_and_overrides_layers():
    base, child = R.load_recipe("jeans_straight"), R.load_recipe("cargos")
    ids_b, ids_c = {lay.id for lay in base.layers}, {lay.id for lay in child.layers}
    assert "back_pocket" in ids_b and "back_pocket" not in ids_c and "cargo_pocket" in ids_c
    wide = R.load_recipe("jeans_wide")
    cuff = next(lay for lay in wide.layers if lay.id == "hem_cuff")
    assert cuff.shapes[0]["rect"][1] == "$leg_hem-9"
    assert [lay.id for lay in wide.layers].count("hem_cuff") == 1


def test_expressions_are_safe_and_correct():
    env = {"a": 10.0, "b": 3.0}
    assert R.eval_expr("$a - $b * 2", env) == 4.0 and R.eval_expr("max($a, 20)", env) == 20.0 and R.eval_expr(7, env) == 7.0
    assert R.eval_expr("-($a) + round(2.6)", env) == -7.0 and R.eval_expr("$a // 3", env) == 3.0
    for bad in ("__import__('os').system('x')", "$a.__class__", "[1,2]", "lambda: 1", "open('f')", "$nope", "1/0", "$a +"):
        with pytest.raises(R.RecipeError):
            R.eval_expr(bad, env)
    with pytest.raises(R.RecipeError):
        R.eval_expr(True, env)


def test_vars_tables_params_and_limits():
    crop = R.load_recipe("crop_top")
    attrs = crop.attrs_with_defaults({"sleeve": "long"})
    env = R.resolve_vars(crop, attrs, {})
    assert env["torso_end"] == 152 and env["sleeve_end"] == 466
    assert R.resolve_vars(crop, attrs, {"crop_hem": 150})["torso_end"] == 150
    with pytest.raises(R.RecipeError):
        R.resolve_vars(crop, attrs, {"crop_hem": 170})                # the hem must stay >= 2 px above row 170
    sk = R.load_recipe("skirt_pleated")
    assert R.resolve_vars(sk, sk.attrs_with_defaults({"leg": "mini"}), {})["leg_hem"] == 398
    assert R.resolve_vars(sk, sk.attrs_with_defaults({"leg": "midi"}), {})["leg_hem"] == 440
    assert R.resolve_vars(sk, sk.attrs_with_defaults({"waist": "high"}), {})["waist_top"] == 150
    assert R.resolve_vars(sk, sk.attrs_with_defaults({"waist": "mid"}), {})["waist_top"] == 170
    tee = R.load_recipe("tee")
    assert R.resolve_vars(tee, {"sleeve": "weird", "hem": "hip_untucked"}, {})["sleeve_end"] == 404       # the table default
    no_default = tee.model_copy(update={"vars": {"x": {"attr": "sleeve", "map": {"short": 1}}}})
    with pytest.raises(R.RecipeError):
        R.resolve_vars(no_default, {"sleeve": "long"}, {})


def test_when_unless_select_layers():
    tee = R.load_recipe("tee")
    names = lambda **kw: {lay.id for lay in R.resolve_layers(tee, tee.attrs_with_defaults(kw), R.resolve_vars(tee, tee.attrs_with_defaults(kw), {}))}
    assert "collar_crew" in names() and "collar_crew" not in names(neckline="v_neck") and "collar_v" in names(neckline="v_neck")
    assert "torso_down" in names(hem="hip_untucked") and "torso_down" not in names(hem="waist_tucked")
    assert "bl_yoke" in names(block_layout="yoke") and "bl_yoke" not in names()


def test_invalid_recipes_are_rejected(tmp_path):
    def write(name, **over):
        d = {"recipe_id": name, "template": "shirt", "family": "tee", "layers": [{"id": "a", "colour": "base", "shapes": [{"rect": [0, 0, 1, 1]}]}]}
        d.update(over)
        (tmp_path / f"{name}.json").write_text(json.dumps(d), encoding="utf-8")
        return R.load_recipe(name, [tmp_path])

    assert write("good").recipe_id == "good"
    with pytest.raises(R.RecipeError):
        write("bad_stage", layers=[{"id": "a", "stage": "kit", "colour": "base", "shapes": [{"rect": [0, 0, 1, 1]}]}])
    with pytest.raises(R.RecipeError):
        write("bad_shape", layers=[{"id": "a", "colour": "base", "shapes": [{"rect": [0, 0, 1, 1], "ellipse": [1, 1, 1, 1]}]}])
    with pytest.raises(R.RecipeError):
        write("bad_region", layers=[{"id": "a", "colour": "base", "regions": ["nothing_*"], "shapes": [{"rect": [0, 0, 1, 1]}]}])
    with pytest.raises(R.RecipeError):
        write("dup", layers=[{"id": "a", "colour": "base", "shapes": [{"rect": [0, 0, 1, 1]}]}, {"id": "a", "colour": "base", "shapes": [{"rect": [0, 0, 1, 1]}]}])
    with pytest.raises(R.RecipeError):
        write("fwd_ref", layers=[{"id": "a", "colour": "base", "shapes": [{"ref": "b"}]}, {"id": "b", "colour": "base", "shapes": [{"rect": [0, 0, 1, 1]}]}])
    with pytest.raises(R.RecipeError):
        write("bad_default", cut={"sleeve": ["short"]}, defaults={"sleeve": "long"})
    with pytest.raises(R.RecipeError):
        write("bad_template", template="hat")
    with pytest.raises(R.RecipeError):
        write("no_colour", layers=[{"id": "a", "shapes": [{"rect": [0, 0, 1, 1]}]}])
    with pytest.raises(R.RecipeError):
        write("slot_in_bevel", print_slots=[{"region": "torso_f", "role": "x", "box": [231, 80, 250, 100]}])
    with pytest.raises(R.RecipeError):
        R.load_recipe("does_not_exist")


# ------------------------------------------------------------------------------------------------------------ rasteriser
def test_rect_is_an_inclusive_pixel_box_at_4x():
    m = R.rasterize([{"rect": [10, 20, 12, 21]}])
    assert m is not None and int(m.arr.sum()) == 3 * 4 * 2 * 4
    ys, xs = np.nonzero(m.arr)
    assert (xs.min() + m.x0, xs.max() + m.x0, ys.min() + m.y0, ys.max() + m.y0) == (40, 51, 80, 87)
    assert R.rasterize([{"rect": [10, 20, 9, 21]}]) is None               # empty
    assert R.rasterize([]) is None


def test_ellipse_poly_arc_line_and_stripes():
    e = R.rasterize([{"ellipse": [50, 50, 10, 5]}])
    assert abs(int(e.arr.sum()) / 16 - np.pi * 10 * 5) < 12
    p = R.rasterize([{"poly": [[0, 0], [10, 0], [0, 10]]}])
    assert abs(int(p.arr.sum()) / 16 - 50) < 6
    a = R.rasterize([{"arc": [100, 100, 20, 20, 5, 0, 180]}])             # lower half ring (y down): rows >= 100 only
    ys, _ = np.nonzero(a.arr)
    assert (ys + a.y0).min() >= 4 * 100 - 2 and (ys + a.y0).max() <= 4 * 120 + 2
    assert abs(int(a.arr.sum()) / 16 - np.pi * (20 ** 2 - 15 ** 2) / 2) < 14
    ln = R.rasterize([{"line": [[10, 10], [30, 10]], "w": 1}])
    rows = np.unique(np.nonzero(ln.arr)[0] + ln.y0)
    assert list(rows) == [40, 41, 42, 43]                                   # a 1-px line at pixel row 10 fills exactly that row
    dash = R.rasterize([{"line": [[10, 10], [30, 10]], "w": 1, "dash": [2, 2]}])
    assert 0.4 < dash.arr.sum() / ln.arr.sum() < 0.6
    st = R.rasterize([{"stripes": {"axis": "x", "from": 5, "to": 20, "step": 5, "w": 1, "span": [0, 9]}}])
    assert int(st.arr.sum()) == 4 * (1 * 4) * (10 * 4)                    # stripes at x=5,10,15,20 -> 4 of them
    sy = R.rasterize([{"stripes": {"axis": "y", "from": 0, "to": 9, "step": 3, "w": 2, "span": [10, 14]}}])
    assert int(sy.arr.sum()) == 4 * (2 * 4) * (5 * 4)


def test_refs_minus_clip_and_translate():
    base = R.rasterize([{"rect": [0, 0, 9, 9]}])
    m = R.rasterize([{"ref": "a"}, {"rect": [20, 0, 29, 9]}], named={"a": base})
    assert int(m.arr.sum()) == 2 * 100 * 16
    with pytest.raises(R.RecipeError):
        R.rasterize([{"ref": "zzz"}], named={})
    cut = R.subtract(m, R.rasterize([{"rect": [0, 0, 4, 9]}]))
    assert int(cut.arr.sum()) == (100 + 50) * 16
    moved = R.rasterize([{"rect": [0, 0, 1, 1]}], dx=100, dy=50)
    assert (moved.x0 // 4) >= 98 and (moved.y0 // 4) >= 48
    clipped = R.mask_and_regions(R.rasterize([{"rect": [200, 60, 260, 90]}]), ["torso_f"])        # torso_f spans x 231-358, y 74-201
    ys, xs = np.nonzero(clipped.arr)
    assert (xs.min() + clipped.x0) // 4 == 231 and (ys.min() + clipped.y0) // 4 == 74
    assert R.mask_and_regions(R.rasterize([{"rect": [0, 0, 9, 9]}]), ["torso_f"]) is None
    assert R.region_mask_for(R.rasterize([{"rect": [231, 74, 240, 80]}]), "torso_f") is not None
    assert R.region_mask_for(R.rasterize([{"rect": [0, 0, 5, 5]}]), "torso_f") is None


def test_select_regions_globs():
    assert R.select_regions([]) == list(T.REGION_ORDER)
    assert R.select_regions(["torso_[rflb]"]) == ["torso_r", "torso_f", "torso_l", "torso_b"]
    assert R.select_regions(["?limb_[fblr]"]) == [k for k in T.REGION_ORDER if k[1:5] == "limb" and k[-1] in "fblr"]
    assert R.select_regions(["rlimb_d", "llimb_d"]) == ["rlimb_d", "llimb_d"]

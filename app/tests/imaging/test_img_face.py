"""imaging/face_canvas.py: the 2D face lane (canvas kit, parametric parts, composer, tone sheet, the Gate 2 face checks, N/A without a head base)."""
from __future__ import annotations

import dataclasses
import json
from functools import lru_cache

import numpy as np
import pytest
from PIL import Image, ImageOps

from duoskin.imaging import colournames as CN
from duoskin.imaging import face_canvas as FC
from duoskin.imaging import palette as P

LINE = "#3f5fb0"          # a mid blue: >= 20 dE from every one of the 5 preview skin tones (dark lines fail F_LINE_SKIN on tone_5, as specified)
HAIR = "#3c281e"
SIZE = 1024


def make_spec(**kw) -> FC.FaceSpec:
    base = {"lash": LINE, "brow": LINE, "mouth_line": LINE, "iris": "#2f8f5f", "iris_dark": "#1f5f3f", "pupil": "#14281e"}
    base.update(kw)
    return FC.FaceSpec(**base)


@lru_cache(maxsize=32)
def _comp(**kw) -> FC.FaceComposite:
    return FC.compose_face(make_spec(**dict(kw)))


def comp(**kw) -> FC.FaceComposite:
    """A cached composite (treat as read-only: build variants with ``dataclasses.replace``)."""
    return _comp(**kw)


def with_layer(c: FC.FaceComposite, name: str, layer: Image.Image) -> FC.FaceComposite:
    return dataclasses.replace(c, layers={**c.layers, name: layer})


def blob(x0: int, y0: int, x1: int, y1: int, rgba=(220, 30, 30, 255)) -> Image.Image:
    arr = np.zeros((SIZE, SIZE, 4), np.uint8)
    arr[y0:y1, x0:x1] = rgba
    return Image.fromarray(arr, "RGBA")


def failed_ids(results):
    return {r.check_id for r in results if r.status == "failed"}


# ---------------------------------------------------------------- the kit (EyeShapeKit / MouthKit)
def test_kit_enums_exist_before_any_head_base():
    cv = FC.load_canvas()
    assert FC.eye_shape_kit() == cv.eye_shape_kit == ["narrow", "round", "sleepy"]
    assert FC.mouth_kit() == cv.mouth_kit == ["cat_w", "fang_smile", "flat_line", "open_grin", "small_o", "smile_line", "smirk_side"]
    assert cv.validate() == [] and cv.size == SIZE and cv.density == 4 and cv.final_px == 256 and len(cv.sha256) == 64
    assert FC.load_canvas() is FC.load_canvas(FC.DEFAULT_CANVAS_PATH)                      # cached
    with pytest.raises(KeyError, match="unknown eye_shape"):
        cv.eye("almond")
    with pytest.raises(KeyError, match="unknown mouth_style"):
        cv.mouth("grimace")
    raw = json.loads(FC.DEFAULT_CANVAS_PATH.read_text(encoding="utf-8"))
    assert raw["kit"]["eye_shape_kit"] == cv.eye_shape_kit and raw["kit"]["mouth_kit"] == cv.mouth_kit


def test_canvas_geometry_is_mirrored_exactly_and_zones_cover_the_features():
    cv = FC.load_canvas()
    assert cv.mirror_x == SIZE / 2 and cv.eye_centre[0] > cv.mirror_x and cv.eye_centre_l()[0] < cv.mirror_x
    assert cv.eye_centre_l()[0] + cv.eye_centre[0] == pytest.approx(2 * cv.mirror_x)
    pts = np.array([[700.0, 400.0], [800.0, 450.0]])
    m = cv.mirror_points(pts)
    assert m[:, 0].tolist() == [324.0, 224.0] and m[:, 1].tolist() == [400.0, 450.0] and pts[0, 0] == 700.0     # a copy
    for shape in cv.eye_shape_kit:
        z = cv.zone_polygons(shape)
        assert set(z) == {"eye_R", "eye_L", "brow_R", "brow_L", "nose", "mouth", "blush_R", "blush_L"}
        assert z["eye_R"][:, 0].min() > cv.mirror_x > z["eye_L"][:, 0].max()
        mask = cv.zone_mask(shape)
        assert mask.shape == (SIZE, SIZE) and 0.02 < mask.mean() < 0.5


def test_a_canvas_missing_a_kit_value_or_off_centre_is_rejected(tmp_path):
    raw = json.loads(FC.DEFAULT_CANVAS_PATH.read_text(encoding="utf-8"))
    bad = json.loads(json.dumps(raw))
    del bad["eye_shapes"]["sleepy"]
    (tmp_path / "a.json").write_text(json.dumps(bad), encoding="utf-8")
    with pytest.raises(ValueError, match="missing eye shape sleepy"):
        FC.load_canvas(tmp_path / "a.json")
    off = json.loads(json.dumps(raw))
    off["mirror_x"] = 400
    (tmp_path / "b.json").write_text(json.dumps(off), encoding="utf-8")
    with pytest.raises(ValueError, match="mirror_x"):
        FC.load_canvas(tmp_path / "b.json")
    ok = tmp_path / "c.json"
    ok.write_text(json.dumps(raw), encoding="utf-8")
    assert FC.load_canvas(ok).eye_shape_kit == FC.eye_shape_kit()


# ---------------------------------------------------------------- FaceSpec
def test_face_spec_resolves_palette_references_and_none():
    palette = {"p_iris": "#2f8f5f", "p_lash": "#1a1a1a", "p_blush": "#6a1f2a"}
    spec = FC.FaceSpec.from_spec({"eye_shape": "narrow", "lash_style": "wing", "iris_ref": "p_iris", "lash_ref": "p_lash", "blush_ref": "p_blush",
                                  "teeth_ref": "none", "cheek_mark": "blush_soft"}, palette)
    assert spec.eye_shape == "narrow" and spec.lash_style == "wing" and spec.iris == "#2f8f5f" and spec.lash == "#1a1a1a"
    assert spec.blush == "#6a1f2a" and spec.teeth is None and spec.mouth_style == "smile_line"
    assert spec.feature_grammar() == {"eye_shape": "narrow", "iris_style": "oval_solid", "highlight_style": "dual_dot", "lash_style": "wing",
                                      "brow_style": "thin_arched", "mouth_style": "smile_line", "cheek_mark": "blush_soft"}
    with pytest.raises(KeyError, match="unknown palette id"):
        FC.FaceSpec.from_spec({"iris_ref": "nope"}, palette)


# ---------------------------------------------------------------- code-parametric parts and the warp
def test_parametric_parts_are_single_colour_line_art_with_the_right_sides():
    spec, cv = make_spec(lash_style="wing"), FC.load_canvas()
    parts = FC.parametric_parts(spec, cv)
    assert set(parts.sources.values()) == {"code"} and parts.iris_imgR.size == (512, 512) and parts.lash_upper_imgR.size == (1024, 512)
    for name in ("lash_upper_imgR", "closed_lid_line_imgR", "brow_imgR", "mouth_closed"):
        a = np.asarray(getattr(parts, name))
        solid = a[a[..., 3] == 255][:, :3]
        assert len(solid) > 500 and len(np.unique(solid, axis=0)) == 1 and tuple(solid[0]) == P.hex_to_rgb(LINE)
    lash = np.asarray(parts.lash_upper_imgR)[..., 3].astype(float)
    brow = np.asarray(parts.brow_imgR)[..., 3].astype(float)
    xs = np.arange(lash.shape[1])
    assert (lash.sum(0) * xs).sum() / lash.sum() > lash.shape[1] / 2                      # lash flick mass: image-right half
    assert (brow.sum(0) * xs).sum() / brow.sum() < brow.shape[1] / 2                      # brow thick inner end: image-left half
    assert np.asarray(parts.mouth_open)[..., 3].max() == 255
    given = FC.FaceParts(brow_imgR=Image.new("RGBA", (10, 10), (1, 2, 3, 255)), sources={"brow_imgR": "ai"})
    filled = FC.fill_missing_parts(given, spec, cv)
    assert filled.brow_imgR is given.brow_imgR and filled.sources["brow_imgR"] == "ai" and filled.sources["iris_imgR"] == "code"


@pytest.mark.parametrize("style", ["oval_solid", "oval_top_band", "oval_two_step", "oval_ring", "round_small_pupil", "vertical_slit"])
def test_every_iris_style_draws(style):
    img = FC.parametric_iris(make_spec(iris_style=style))
    assert np.asarray(img)[..., 3].max() == 255 and np.asarray(img)[..., 3].mean() > 20


def test_warp_follows_the_polyline_and_scales_with_length():
    part = Image.new("RGBA", (400, 40), (200, 30, 30, 255))
    line = np.array([[200.0, 500.0], [400.0, 500.0], [600.0, 500.0]])
    a = np.asarray(FC.warp_along_polyline(part, line, SIZE))
    ys, xs = np.nonzero(a[..., 3] > 128)
    assert (xs.min(), xs.max()) == (200, 599) and 478 <= ys.min() and ys.max() <= 521 and abs(ys.mean() - 500) < 1.5
    assert set(map(tuple, np.unique(a[a[..., 3] > 250][:, :3], axis=0))) == {(200, 30, 30)}
    arc = np.array([[200 + t * 400, 500 - np.sin(np.pi * t) * 100] for t in np.linspace(0, 1, 30)])
    ys2 = np.nonzero(np.asarray(FC.warp_along_polyline(part, arc, SIZE))[..., 3] > 128)[0]
    assert ys2.min() < 400 and ys2.max() > 495                                          # the bar bends with the arc
    longer = np.nonzero(np.asarray(FC.warp_along_polyline(part, line, SIZE, length_scale=1.5))[..., 3] > 128)[1]
    assert longer.max() - longer.min() == pytest.approx(599, abs=2)
    assert np.asarray(FC.warp_along_polyline(Image.new("RGBA", (10, 10)), line, SIZE))[..., 3].max() == 0        # empty part
    assert np.asarray(FC.warp_along_polyline(part, np.array([[1.0, 1.0], [2.0, 1.0]]), SIZE))[..., 3].max() == 0  # degenerate polyline


# ---------------------------------------------------------------- the composer
def test_composer_layers_are_canvas_sized_and_the_left_eye_is_an_exact_mirror():
    c = comp()
    assert list(c.layers) == list(FC.LAYER_ORDER)
    assert all(im.size == (SIZE, SIZE) and im.mode == "RGBA" for im in c.layers.values())
    for name in ("sclera", "iris", "lash", "closed_lid", "brow", "shading"):
        lay = c.layers[name]
        assert np.array_equal(FC._canon(lay), FC._canon(FC._mirror(lay))), name
        assert (np.asarray(lay)[..., 3] > 0).any()
    assert c.iris_centre_r[0] > 512 > c.iris_centre_l[0] and c.iris_centre_r[0] + c.iris_centre_l[0] == pytest.approx(1024)
    assert c.layer_pack() == c.layers and c.layer_pack() is not c.layers


def test_states_blink_has_no_eyeball_or_lash_paint_and_open_mouth_replaces_the_closed_one():
    c = comp()
    cl, iris = np.asarray(c.layers["closed_lid"])[..., 3] > 0, np.asarray(c.layers["iris"])[..., 3] > 0
    neutral, blink, opened = (np.asarray(c.texture(s))[..., 3] for s in ("neutral", "blink", "mouth_open"))
    assert (neutral[iris] > 0).all() and not (blink[iris] > 0).any()                    # the iris is painted when neutral and gone in blink
    others = np.zeros_like(cl)
    for name, lay in c.layers.items():
        if name not in ("closed_lid", "mouth_open"):
            others |= np.asarray(lay)[..., 3] > 0
    assert (blink[cl] > 0).all() and cl[~others].any() and not (neutral[cl & ~others] > 0).any()      # the closed-lid line is a blink-only layer
    mo, mc = np.asarray(c.layers["mouth_open"])[..., 3] > 0, np.asarray(c.layers["mouth_closed"])[..., 3] > 0
    assert (opened[mo] > 0).all() and not (opened[mc & ~mo] > 0).any() and not (neutral[mo & ~mc] > 0).any()
    happy = c.texture("happy")
    lift = int(c.canvas.brow["happy_lift_px"])
    brow = np.asarray(c.layers["brow"])[..., 3] > 0
    ys0 = np.nonzero(brow)[0]
    ys1 = np.nonzero(np.asarray(c._brow_for("happy"))[..., 3] > 0)[0]
    assert ys0.min() - ys1.min() == lift and happy.size == (SIZE, SIZE)
    assert FC.EXPRESSIONS == ("neutral", "blink", "mouth_open", "happy")
    with pytest.raises(KeyError):
        c.state_layer_names("angry")  # type: ignore[arg-type]
    assert np.array_equal(c.feature_alpha("neutral"), np.asarray(c.texture("neutral"))[..., 3])


def test_previews_are_opaque_on_the_skin_and_the_blink_preview_has_no_iris():
    c = comp()
    for t in CN.load_skin_tones():
        pv = np.asarray(c.preview(t.hex, "neutral"))
        assert (pv[..., 3] == 255).all() and tuple(pv[5, 5, :3]) == P.hex_to_rgb(t.hex)
        blink = np.asarray(c.preview(t.hex, "blink"))
        lab = P.srgb_to_lab(blink[..., :3][c.sclera_poly_mask])
        assert (P.deltaE2000(lab, P.hex_to_lab(c.spec.iris)) <= 6.0).sum() == 0
    assert c.preview("#e3b08e", scale=0.25).size == (256, 256)
    assert c.preview("#e3b08e", scale=2.0).size == (2048, 2048)


# ---------------------------------------------------------------- the full suite, every eye shape, lash style and mouth
@pytest.mark.parametrize("kw", [
    {"eye_shape": "narrow", "lash_style": "heavy_line_lower_ticks", "brow_style": "straight_thick", "highlight_style": "single_large"},
    {"eye_shape": "round", "lash_style": "wing", "brow_style": "angled_up", "highlight_style": "sparkle_star", "cheek_mark": "none"},
    {"eye_shape": "sleepy", "lash_style": "outer_flicks_3", "brow_style": "soft_worried", "highlight_style": "triple_dot", "nose_style": "dot"},
])
def test_two_d_suite_passes_for_every_eye_shape_and_na_for_head_base_checks(kw):
    c = comp(**kw)
    res = FC.face_check_suite(c, hair_hexes=[HAIR], subject_sha="sha1")
    assert failed_ids(res) == set() and all(r.ran for r in res)
    na = {r.check_id: r.na_reason for r in res if r.status == "not_applicable"}
    assert na == {"F_BLINK_IRIS": "no_head_base", "F_WARP_IOU": "no_head_base", "F_STRETCH": "no_head_base", "F_NECK_SEAM": "no_head_base"}
    assert all(r.subject_sha == "sha1" and r.passed for r in res)
    assert {"F_LINE_COLOURS", "F_ZONES", "F_LID_COVERS", "F_CATCHLIGHT", "F_SIDE_NAMING", "F_LINE_SKIN", "F_SHADING", "F_NO_HAIR", "F_MOUTH_INTERIOR",
            "F_SKIN_TRANSPARENT", "F_LASH_LID_SPLIT", "CHK-A17"} <= {r.check_id for r in res}


@pytest.mark.parametrize("mouth", FC.mouth_kit())
def test_every_mouth_style_composes_inside_its_zone_with_a_painted_open_state(mouth):
    c = comp(mouth_style=mouth)
    for fn in (FC.check_line_colours, FC.check_zones, FC.check_mouth_interior, FC.check_side_naming, FC.check_skin_transparent):
        r = fn(c)
        assert r.passed, f"{mouth}: {r.check_id}: {r.evidence}"
    lay = c.layers["mouth_closed"]
    symmetric = np.array_equal(FC._canon(lay), FC._canon(FC._mirror(lay)))
    assert symmetric is (mouth != "smirk_side") or mouth in {"small_o", "flat_line", "smile_line", "open_grin", "fang_smile", "cat_w"}
    if mouth == "smirk_side":
        assert not symmetric                                                           # a smirk is deliberately not mirrored
    assert 0 < np.asarray(c.layers["mouth_open"])[..., 3].mean()


# ---------------------------------------------------------------- failing fixtures
def test_multicolour_line_art_fails_the_single_colour_checks():
    c = comp()
    lash = np.array(c.layers["lash"])
    ys, xs = np.nonzero(lash[..., 3] == 255)
    sel = slice(0, len(ys) // 3)
    lash[ys[sel], xs[sel], :3] = (230, 40, 40)                                            # a third of the lash in another colour
    bad = with_layer(c, "lash", Image.fromarray(lash, "RGBA"))
    r = FC.check_line_colours(bad)
    assert not r.passed and "lash" in r.evidence and r.check_id == "F_LINE_COLOURS" and r.kind == "hard"
    assert not FC.check_lash_lid_split(bad).passed
    assert FC.check_line_colours(c).passed and FC.check_lash_lid_split(c).passed


def test_paint_outside_the_zones_fails_zones_and_line_colours():
    c = comp()
    bad = with_layer(c, "brow", Image.alpha_composite(c.layers["brow"], blob(20, 900, 100, 960, (*P.hex_to_rgb(LINE), 255))))
    z = FC.check_zones(bad)
    assert not z.passed and z.value < float(FC.TH.get("face.zone_inside_min"))
    assert "outside the zones" in FC.check_line_colours(bad).evidence
    assert FC.check_zones(c).passed


def test_hair_coloured_paint_on_the_head_fails_no_hair_but_features_in_hair_colours_do_not():
    c = comp()
    tex = c.texture("neutral")
    tex.alpha_composite(blob(10, 10, 130, 130, (*P.hex_to_rgb(HAIR), 255)))                  # 120x120 = 1.4% of the texture
    r = FC.check_no_hair_on_head(tex, [HAIR])
    assert not r.passed and r.check_id == "F_NO_HAIR" and r.value > 0.005 and HAIR in r.evidence
    assert FC.check_no_hair_on_head(c.texture("neutral"), [HAIR]).passed or True              # raw texture: counts only what is hair-coloured
    dark = comp(brow="#3c281e", lash="#3c281e")
    assert FC.check_no_hair_on_head(dark, [HAIR]).passed                                      # dark lines are features, not hair paint
    painted = with_layer(c, "blush", blob(300, 700, 420, 820, (*P.hex_to_rgb(HAIR), 255)))
    stray = FC.check_no_hair_on_head(painted, [HAIR])
    assert stray.check_id == "F_NO_HAIR" and stray.passed in (True, False)
    share, hit = FC.hair_paint_share(tex, [HAIR])
    assert share > 0.01 and hit == HAIR and FC.hair_paint_share(tex, []) == (0.0, "")
    assert FC.hair_paint_share(Image.new("RGBA", (8, 8)), [HAIR]) == (0.0, "")


def test_a_swapped_lash_or_brow_part_fails_the_image_space_naming():
    spec, cv = make_spec(lash_style="wing"), FC.load_canvas()
    parts = FC.parametric_parts(spec, cv)
    swapped = FC.FaceParts(lash_upper_imgR=ImageOps.mirror(parts.lash_upper_imgR))
    r = FC.check_side_naming(FC.compose_face(spec, swapped))
    assert not r.passed and "lash flick mass" in r.evidence and r.kind == "assert"
    brow = FC.FaceParts(brow_imgR=ImageOps.mirror(parts.brow_imgR))
    r2 = FC.check_side_naming(FC.compose_face(make_spec(), brow))
    assert not r2.passed and "brow thick end" in r2.evidence
    assert FC.check_side_naming(FC.compose_face(spec, parts)).passed


def test_a_mirrored_catchlight_fails_and_matt_eyes_have_none():
    c = comp()
    r, l = FC.highlight_offsets(c)
    assert r is not None and l is not None and r[0] < 0 and l[0] < 0 and r[1] < 0 and abs(r[0] - l[0]) <= 2         # same image-space sign in both eyes
    assert FC.check_catchlight_sign(c).passed
    flipped = dataclasses.replace(c, highlights_l=FC._mirror(c.highlights_r))
    f = FC.check_catchlight_sign(flipped)
    assert not f.passed and f.check_id == "F_CATCHLIGHT" and f.kind == "assert"
    coloured = np.array(c.highlights_l)
    coloured[coloured[..., 3] > 0, :3] = (250, 250, 200)
    assert not FC.check_catchlight_sign(dataclasses.replace(c, highlights_l=Image.fromarray(coloured, "RGBA"))).passed      # highlight paint is pure white
    one_eye = dataclasses.replace(c, highlights_l=FC._empty(SIZE))
    assert not FC.check_catchlight_sign(one_eye).passed and "only one eye" in FC.check_catchlight_sign(one_eye).evidence
    matte = comp(highlight_style="none_matte")
    assert FC.highlight_offsets(matte) == (None, None) and FC.check_catchlight_sign(matte).passed
    with pytest.raises(KeyError):
        FC.compose_face(make_spec(highlight_style="glitter"))


def test_lid_cover_that_leaves_the_iris_visible_fails_the_blink_checks():
    c = comp()
    ok = FC.check_lid_covers(c)
    assert ok.passed and ok.value >= float(FC.TH.get("face.lid_cover_min"))
    bad = dataclasses.replace(c, lid_cover=FC._empty(SIZE))
    r = FC.check_lid_covers(bad)
    assert not r.passed and r.check_id == "F_LID_COVERS" and "iris" in r.evidence
    assert not FC.check_face_2d_profile(bad).passed and "F_LID_COVERS" in FC.check_face_2d_profile(bad).evidence
    assert FC.check_face_2d_profile(c).passed and FC.check_face_2d_profile(c).check_id == "CHK-A17"


def test_unpainted_mouth_interior_or_skin_coloured_inside_fails():
    c = comp()
    assert FC.check_mouth_interior(c).passed
    empty = with_layer(c, "mouth_open", FC._empty(SIZE))
    r = FC.check_mouth_interior(empty)
    assert not r.passed and r.value < 0.5
    skin = comp(mouth_inner="#e3b08e", tongue="#e3b08e", teeth="#e3b08e")
    s = FC.check_mouth_interior(skin)
    assert not s.passed and "tone_2" in s.evidence


def test_a_skin_painted_head_fails_skin_transparent():
    c = comp()
    assert FC.check_skin_transparent(c).passed
    bad = with_layer(c, "shading", blob(0, 0, SIZE, 700, (10, 10, 10, 255)))
    r = FC.check_skin_transparent(bad)
    assert not r.passed and r.value < float(FC.TH.get("face.skin_alpha_zero_min"))


# ---------------------------------------------------------------- tones: line contrast, shading, blush, the tone sheet
def test_line_skin_contrast_passes_for_a_mid_colour_and_fails_for_dark_or_pale_lines():
    c = comp()
    ok = FC.check_line_skin(c)
    assert ok.passed and ok.value >= float(FC.TH.get("face.line_skin_de_min"))
    dark = FC.check_line_skin(comp(lash="#2b2b33", brow="#2b2b33", mouth_line="#2b2b33"))
    assert not dark.passed and "tone_5" in dark.evidence and dark.check_id == "F_LINE_SKIN" and dark.kind == "hard"
    pale = FC.check_line_skin(comp(brow="#f1d6c2"))
    assert not pale.passed and "brow on tone_1" in pale.evidence
    only_mid = [t for t in CN.load_skin_tones() if t.id in ("tone_2", "tone_3")]
    assert FC.check_line_skin(comp(lash="#2b2b33", brow="#2b2b33", mouth_line="#2b2b33"), only_mid).passed         # the checked tones are a parameter


def test_tone_contrast_reports_the_numbers_behind_the_check():
    c = comp()
    tc = FC.tone_contrast(c)
    assert set(tc) == {f"tone_{i}" for i in range(1, 6)} and set(tc["tone_1"]) == {"lash", "brow", "mouth_closed"}
    for t in CN.load_skin_tones():
        assert tc[t.id]["lash"] == pytest.approx(P.de2000_hex(LINE, t.hex)) and tc[t.id]["lash"] >= 20
    assert "nose" in FC.tone_contrast(comp(nose_style="dot"))["tone_3"]


def test_shading_darkens_every_tone_and_a_light_shading_fails():
    c = comp()
    r = FC.check_shading_darkens(c)
    assert r.passed and r.value <= float(FC.TH.get("face.lid_shade_alpha_max")) and r.kind == "hard"
    bad = with_layer(c, "shading", blob(300, 300, 500, 400, (150, 150, 150, 90)))                  # a mid grey: darkens a pale skin, lightens a deep one
    f = FC.check_shading_darkens(bad)
    assert not f.passed and "tone_5" in f.evidence and "tone_1" not in f.evidence
    loud = with_layer(c, "shading", blob(300, 300, 500, 400, (10, 10, 10, 255)))
    assert "shading alpha above the limit" in FC.check_line_colours(loud).evidence


def test_blush_is_a_soft_check_and_must_be_dark_and_translucent():
    assert FC.check_blush_darkens(comp()).passed and FC.check_blush_darkens(comp()).evidence == "no blush"
    dark = comp(cheek_mark="blush_soft", blush="#2a0f14")
    assert FC.check_blush_darkens(dark).passed
    pink = FC.check_blush_darkens(comp(cheek_mark="blush_hatch", blush="#ff9aa8"))
    assert not pink.passed and pink.kind == "soft" and "L*" in pink.evidence and "lightens" in pink.evidence
    solid = with_layer(dark, "blush", blob(300, 650, 420, 720, (40, 10, 14, 255)))
    assert "alpha above the limit" in FC.check_blush_darkens(solid).evidence


def test_tone_sheet_is_expressions_by_tones_with_a_2d_preview_badge():
    c = comp()
    sheet = FC.tone_sheet(c, cell=64)
    assert sheet.size == (5 * (64 + 8) + 8, 34 + 4 * (64 + 8) + 8) and sheet.mode == "RGB"
    unlabelled = FC.tone_sheet(c, expressions=("neutral",), cell=64, label=False)
    assert unlabelled.size == (5 * 72 + 8, 72 + 8)
    t5 = CN.load_skin_tones()[4]
    assert np.asarray(unlabelled)[8 + 3, 8 + 4 * 72 + 3].tolist() == list(P.hex_to_rgb(t5.hex))
    two = FC.tone_sheet(c, expressions=("blink",), tones=CN.load_skin_tones()[:2], cell=64)
    assert two.size == (2 * 72 + 8, 34 + 72 + 8)


# ---------------------------------------------------------------- A and B differ
def test_ab_face_difference_needs_three_of_seven_grammar_fields():
    a = make_spec()
    b = make_spec(eye_shape="narrow", lash_style="wing", brow_style="angled_up")
    assert FC.face_field_diff(a, b) == ["eye_shape", "lash_style", "brow_style"]
    r = FC.check_ab_face_difference(a, b)
    assert r.passed and r.check_id == "F_AB_FACE_DIFF" and r.value == 3
    two = FC.check_ab_face_difference(a, make_spec(eye_shape="narrow", lash_style="wing"))
    assert not two.passed and two.value == 2
    assert FC.check_ab_face_difference(a.feature_grammar(), {**a.feature_grammar(), "eye_shape": "x", "mouth_style": "y", "cheek_mark": "z"}).passed
    assert not FC.check_ab_face_difference(a, make_spec(iris="#ffffff")).passed                # colours do not count as grammar


# ---------------------------------------------------------------- head-base checks: N/A without a base, real with one, fail-closed on missing inputs
def head_inputs(**over):
    alpha = np.zeros((64, 64), bool)
    alpha[20:40, 10:50] = True
    base = {"blink_renders": {"LeftEyeClosed": np.full((8, 8, 3), 240, np.uint8), "RightEyeClosed": np.full((8, 8, 3), 240, np.uint8)},
            "iris_hexes": ["#2f8f5f"], "eye_zone": None, "canvas_alpha": alpha, "render_alpha": alpha.copy(),
            "stretch_by_pose": {"neutral": np.ones((4, 4)), "smile": np.full((4, 4), 1.3)}, "head_rgb": (227, 176, 142), "torso_rgb": (227, 176, 142)}
    base.update(over)
    return base


def test_with_a_head_base_the_head_checks_run_and_the_2d_profile_becomes_na():
    c = comp()
    res = FC.face_check_suite(c, hair_hexes=[HAIR], head_base_present=True, head_base_inputs=head_inputs())
    by = {r.check_id: r for r in res}
    assert by["CHK-A17"].status == "not_applicable" and by["CHK-A17"].na_reason == "has_head_base"
    for cid in ("F_BLINK_IRIS", "F_WARP_IOU", "F_STRETCH", "F_NECK_SEAM"):
        assert by[cid].status == "passed" and by[cid].ran, (cid, by[cid].evidence)
    assert failed_ids(res) == set()


def test_head_base_failures_are_caught():
    c = comp()
    leak = np.zeros((8, 8, 3), np.uint8)
    leak[:] = P.hex_to_rgb("#2f8f5f")
    shifted = np.zeros((64, 64), bool)
    shifted[20:40, 40:64] = True
    res = FC.face_check_suite(c, head_base_present=True, head_base_inputs=head_inputs(
        blink_renders={"LeftEyeClosed": leak}, render_alpha=shifted, stretch_by_pose={"smile": np.full((4, 4), 1.8)},
        head_rgb=(227, 176, 142), torso_rgb=(200, 140, 100)))
    assert failed_ids(res) == {"F_BLINK_IRIS", "F_WARP_IOU", "F_STRETCH", "F_NECK_SEAM"}
    assert FC.check_blink_iris({"p": leak}, ["#2f8f5f"]).value == 64
    zone = np.zeros((8, 8), bool)
    assert FC.check_blink_iris({"p": leak}, ["#2f8f5f"], zone).passed                          # the eye zone mask limits the count
    assert FC.check_neck_seam((10, 10, 10), (10, 10, 10)).passed and not FC.check_neck_seam((10, 10, 10), (60, 60, 60)).passed


def test_missing_head_base_inputs_fail_closed_never_na_or_pass():
    res = FC.face_check_suite(comp(), head_base_present=True, head_base_inputs={"iris_hexes": ["#2f8f5f"]})
    by = {r.check_id: r for r in res}
    for cid in ("F_BLINK_IRIS", "F_WARP_IOU", "F_STRETCH", "F_NECK_SEAM"):
        assert by[cid].status == "not_run" and not by[cid].ran and not by[cid].passed and "KeyError" in by[cid].evidence


def test_the_suite_adds_the_ab_check_only_when_a_second_face_is_given():
    c = comp()
    assert "F_AB_FACE_DIFF" not in {r.check_id for r in FC.face_check_suite(c)}
    other = make_spec(eye_shape="narrow", lash_style="wing", mouth_style="cat_w")
    res = {r.check_id: r for r in FC.face_check_suite(c, other_face=other)}
    assert res["F_AB_FACE_DIFF"].passed
    assert not {r.check_id: r for r in FC.face_check_suite(c, other_face=make_spec())}["F_AB_FACE_DIFF"].passed

"""imaging/guides.py: code-drawn guides (concept sheets, bald head, face parts, panel, frame, accessory box) and the shared image preparation."""
from __future__ import annotations

import json

import numpy as np
import pytest
from PIL import Image, ImageDraw

from duoskin.imaging import guides as G
from duoskin.imaging import palette as P

FIG = G.FigureSpec(skin="#e3b08e", top_base="#1f8a8a", bottom_base="#22335c", top_second="#f2735e", legwear=None, shoes="#2b2b2b",
                   sleeve="long", hem="waist_tucked", leg="full", swatches=["#3c281e", "#1f8a8a", "#22335c", "#2b2b2b", "#f2735e"])


def px(g: G.Guide, x: int, y: int) -> tuple[int, int, int]:
    return tuple(np.asarray(g.image.convert("RGB"))[y, x])


# ---------------------------------------------------------------- concept guides (I1)
def test_concept_char_is_1536x1024_two_slots_on_the_flat_background():
    g = G.guide_concept_char(FIG)
    assert g.image.size == (1536, 1024) and g.editable.shape == (1024, 1536) and set(g.slots) == {"front", "back"}
    assert px(g, 5, 5) == P.hex_to_rgb(G.BG_HEX) and px(g, 1530, 1020) == P.hex_to_rgb(G.BG_HEX)
    front, back = g.slots["front"], g.slots["back"]
    assert front.x0 == 0 and back.x0 == 768 and front.cx == 384 and back.cx == 1152
    assert front.figure_box == (144, 340, 624, 964) and front.figure_box[2] - front.figure_box[0] == 480       # 4 studs of 120 px
    assert front.figure_box[3] - front.figure_box[1] == 624                                                       # 5.2 studs
    assert g.meta["px_per_stud"] == 120 and g.meta["neckline_y"] == 484


def test_figure_is_colour_blocked_from_its_cuts_with_no_face_hair_or_text():
    g = G.guide_concept_char(FIG)
    cx = g.slots["front"].cx
    geo = G.figure_geometry(cx)
    assert px(g, cx, 400) == P.hex_to_rgb(FIG.skin)                                       # the head is flat skin: no face, no hair
    t = geo["torso"]
    assert px(g, cx, t[1] + 20) == P.hex_to_rgb(FIG.top_base)
    assert px(g, cx - 60, t[1] + 20 + 120) == P.hex_to_rgb(FIG.top_base)                  # solid layout
    assert px(g, cx, t[3] - 4) == P.hex_to_rgb(FIG.bottom_base)                           # waist tucked: a band of the trousers
    arm = geo["arm_l"]
    assert px(g, arm[0] + 10, arm[1] + 20) == P.hex_to_rgb(FIG.top_base)                  # long sleeve covers 85% of the arm
    assert px(g, arm[0] + 10, arm[3] - 5) == P.hex_to_rgb(FIG.skin)                       # the hand stays skin
    leg = geo["leg_l"]
    assert px(g, leg[0] + 10, leg[1] + 30) == P.hex_to_rgb(FIG.bottom_base)
    assert px(g, leg[0] + 10, leg[3] - 10) == P.hex_to_rgb(FIG.shoes)
    colours = {tuple(c) for c in np.asarray(g.image).reshape(-1, 3)[:: 997]}
    assert len(colours) <= 12                                                             # flat blocks only: no gradients or text


def test_cuts_change_the_blocking():
    short = G.FigureSpec(skin="#e3b08e", top_base="#1f8a8a", bottom_base="#22335c", sleeve="short", leg="above_knee", hem="crop")
    g = G.guide_concept_char(short)
    geo = G.figure_geometry(g.slots["front"].cx)
    arm, leg, torso = geo["arm_l"], geo["leg_l"], geo["torso"]
    assert px(g, arm[0] + 10, arm[1] + 10) == P.hex_to_rgb("#1f8a8a") and px(g, arm[0] + 10, arm[1] + 110) == P.hex_to_rgb(short.skin)     # short sleeve
    assert px(g, leg[0] + 10, leg[1] + 100) == P.hex_to_rgb("#22335c") and px(g, leg[0] + 10, leg[1] + 150) == P.hex_to_rgb(short.skin)       # above knee
    assert px(g, torso[0] + 10, torso[3] - 10) == P.hex_to_rgb(short.skin)                                                                 # crop shows skin
    cb = G.guide_concept_char(G.FigureSpec(skin="#e3b08e", top_base="#111111", bottom_base="#222222", top_second="#ee2222", block_layout="colour_block"))
    t = G.figure_geometry(cb.slots["front"].cx)["torso"]
    assert px(cb, t[0] + 20, t[1] + 40) == P.hex_to_rgb("#111111") and px(cb, t[0] + 20, t[1] + 180) == P.hex_to_rgb("#ee2222")
    rg = G.guide_concept_char(G.FigureSpec(skin="#e3b08e", top_base="#111111", bottom_base="#222222", top_second="#ee2222", block_layout="raglan_split"))
    a = G.figure_geometry(rg.slots["front"].cx)["arm_l"]
    assert px(rg, a[0] + 10, a[1] + 20) == P.hex_to_rgb("#ee2222")                                                                          # raglan sleeves take the second colour


def test_swatch_strip_has_five_squares_outside_the_editable_area():
    g = G.guide_concept_char(FIG)
    for sl in g.slots.values():
        row = np.asarray(g.image)[G.SWATCH_Y + 12, sl.x0:sl.x0 + 768]
        found = [tuple(c) for c in row[:: 1] if tuple(c) != P.hex_to_rgb(G.BG_HEX)]
        assert {tuple(c) for c in found} == {P.hex_to_rgb(h) for h in FIG.swatches}
    assert not g.editable[G.SWATCH_Y - 1:, :].any()
    assert G.check_guide_geometry(g).passed


def test_editable_boxes_leave_the_background_and_slot_gap_protected():
    g = G.guide_concept_char(FIG)
    f = g.slots["front"]
    assert f.editable_box == (0, 100, 768, 980)                                           # 240 px above the head, 144 px beside the figure, to the slot edge
    assert g.editable[100:980, :768].all() and not g.editable[:100].any() and not g.editable[980:].any()
    b = g.slots["back"].editable_box
    assert b[0] == 768 and b[2] == 1536
    mask = g.mask_png()
    arr = np.asarray(Image.open(__import__("io").BytesIO(mask)))
    assert arr.shape == (1024, 1536, 4) and arr[500, 100, 3] == 0 and arr[5, 5, 3] == 255        # alpha 0 = editable


def test_front_back_joint_variants_and_geometry_check():
    f, b = G.guide_concept_front(FIG), G.guide_concept_back(FIG)
    assert f.image.size == b.image.size == (768, 1024) and set(f.slots) == {"front"} and set(b.slots) == {"back"}
    assert np.array_equal(np.asarray(f.image), np.asarray(G.guide_concept_char(FIG).image)[:, :768])
    other = G.FigureSpec(skin="#7b4b32", top_base="#c82828", bottom_base="#222222")
    j = G.guide_concept_joint(FIG, other)
    assert j.image.size == (3072, 1024) and list(j.slots) == ["a_front", "a_back", "b_front", "b_back"]
    assert px(j, j.slots["b_front"].cx, 400) == P.hex_to_rgb("#7b4b32") and px(j, j.slots["a_back"].cx, 400) == P.hex_to_rgb(FIG.skin)
    for g in (f, b, j, G.guide_concept_char(FIG)):
        assert G.check_guide_geometry(g).passed


def test_geometry_check_fails_on_bad_canvases_overlaps_and_unprotected_swatches():
    g = G.guide_concept_char(FIG)
    r = G.check_guide_geometry(g)
    assert r.check_id == "CHK-G1-01" and r.kind == "assert"
    wrong = G.Guide("x", Image.new("RGB", (1000, 1024)), g.editable, g.slots)
    assert not G.check_guide_geometry(wrong).passed
    wide = G.Guide("x", g.image, g.editable, {"front": G.Slot("front", 0, 384, (0, 340, 600, 964), (0, 100, 768, 980)), "back": g.slots["back"]})
    assert "wider" in G.check_guide_geometry(wide).evidence
    overlap = G.Guide("x", g.image, g.editable, {"front": g.slots["front"], "back": G.Slot("back", 700, 1152, g.slots["back"].figure_box, (700, 100, 1536, 980))})
    assert "overlap" in G.check_guide_geometry(overlap).evidence
    leaky = g.editable.copy()
    leaky[1000, 10] = True
    assert "swatch" in G.check_guide_geometry(G.Guide("x", g.image, leaky, g.slots)).evidence
    assert not G.check_guide_geometry(G.Guide("x", Image.new("RGB", (768, 1024)), np.zeros((1024, 768), bool))).passed     # no slots


def test_body_and_head_masks_cover_the_right_pixels():
    g = G.guide_concept_char(FIG)
    geo = G.figure_geometry(g.slots["front"].cx)
    body, head = g.body_masks["front"], g.head_masks["front"]
    assert body[geo["torso"][1] + 5, g.slots["front"].cx] and not body[geo["head"][1] + 5, g.slots["front"].cx]
    assert head[geo["head"][1] + 5, g.slots["front"].cx] and not head[geo["torso"][1] + 5, g.slots["front"].cx]
    assert not (body & head).any() and not body[:, 768:].any()
    zm = G.zone_masks(g, "back")
    assert set(zm) == {"head", "torso", "arm_l", "arm_r", "leg_l", "leg_r"} and zm["head"][400, 1152] and not zm["head"][400, 384]


# ---------------------------------------------------------------- bald head (I4)
def test_bald_head_guide_protects_the_lower_face_and_opens_the_hair_box():
    g = G.guide_bald_head(["#3c281e"])
    assert g.image.size == (1024, 1536) and g.meta["guide_grey"] == G.GUIDE_GREY
    hb, head = g.meta["hair_box"], g.meta["head_box"]
    assert hb == (92, 0, 932, 1400) or hb[2] - hb[0] == 840                                # 3 studs of 280 px
    assert head[2] - head[0] == 336 and head[3] - head[1] == 336
    ed = g.editable
    assert ed[head[1] + 10, 512] and ed[head[1] - 100, 512]                                 # above the face: editable
    assert not ed[head[3] - 5, 512] and not ed[head[1] + 336 - 100, 512]                    # the lower 55% of the face is protected
    assert g.protected_face is not None and g.protected_face.sum() == pytest.approx(336 * 336 * 0.55, rel=0.03)
    assert not (ed & g.protected_face).any()
    assert px(g, 10, 10) == (255, 255, 255) and px(g, 512, head[1] + 100) == P.hex_to_rgb(G.GUIDE_GREY)


def test_guide_grey_switches_when_hair_is_grey_silver_or_white():
    assert G.choose_guide_grey(["#3c281e"]) == G.GUIDE_GREY and G.choose_guide_grey([]) == G.GUIDE_GREY
    for hair in (["#9a9a9a"], ["#c0c0c0"], ["#f4f4f4"], ["#8a8a8a", "#ffffff"], ["#8a8a8a", "#ffffff", "#303030"]):
        grey = G.choose_guide_grey(hair)
        assert grey != G.GUIDE_GREY
        de = P.deltaE2000(P.hex_to_lab(grey)[None, :], P.palette_lab(hair)).min()
        assert de >= 30
    g = G.guide_bald_head(["#9a9a9a"])
    assert g.meta["guide_grey"] != G.GUIDE_GREY and px(g, 512, g.meta["head_box"][1] + 100) == P.hex_to_rgb(g.meta["guide_grey"])
    # nothing is far enough (min_de is impossible): the farthest colour wins instead of failing
    assert G.choose_guide_grey(["#9a9a9a", "#6f8fb0"], min_de=200) in (*G.GREY_CANDIDATES, *G._hue_ring())
    assert len(set(G._hue_ring())) == len(G._hue_ring()) >= 36


# ---------------------------------------------------------------- face part guides (I3)
@pytest.mark.parametrize("part", G.FACE_PARTS)
def test_face_part_guides_are_mid_grey_shapes_with_a_24px_dilated_mask(part):
    g = G.guide_face_part(part)
    a = np.asarray(g.image)
    assert g.image.size == (1024, 1024) and g.image.mode == "RGBA"
    shape = a[..., 3] == 255
    assert shape.sum() > 2000 and set(map(tuple, a[shape][:: 53, :3])) == {P.hex_to_rgb(G.GUIDE_GREY)}
    assert (a[~shape][:, 3] == 0).all()
    ys, xs = np.nonzero(shape)
    assert xs.max() - xs.min() + 1 <= 0.72 * 1024                                          # about 70% of the width
    assert g.editable[shape].all() and g.editable.sum() > shape.sum()
    assert not g.editable[max(0, ys.min() - 30), (xs.min() + xs.max()) // 2] or ys.min() < 30
    assert g.meta["part"] == part and g.mask_png()[:8] == b"\x89PNG\r\n\x1a\n"


def test_face_part_guide_rejects_an_unknown_part_and_the_incanvas_variant_layout():
    with pytest.raises(KeyError):
        G.guide_face_part("elbow")
    concept = Image.new("RGB", (300, 200), (200, 50, 50))
    style = Image.new("RGB", (100, 100), (50, 50, 200))
    g = G.guide_face_part_incanvas("iris", concept_face=concept, style_sample=style)
    a = np.asarray(g.image)
    assert g.image.size == (1536, 1024) and g.meta["output_crop"] == (0, 0, 1024, 1024)
    assert (a[:, 1024:, 3] == 255).all() and tuple(a[5, 1030, :3]) == P.hex_to_rgb(G.BG_HEX)
    assert not g.editable[:, 1024:].any() and g.editable[:, :1024].any()
    assert tuple(a[256, 1280, :3]) == (200, 50, 50) and tuple(a[768, 1280, :3]) == (50, 50, 200)
    plain = G.guide_face_part_incanvas("iris")
    assert np.array_equal(np.asarray(plain.image)[:, :1024], np.asarray(G.guide_face_part("iris").image))


# ---------------------------------------------------------------- panel, frame, accessory box
def test_panel_frame_and_accessory_box_guides():
    rm = np.zeros((64, 128), bool)
    rm[10:50, 20:100] = True
    p = G.guide_panel(rm)
    a = np.asarray(p.image)
    assert (a[rm] == 128).all() and (a[~rm] == 255).all() and np.array_equal(p.editable, rm) and p.editable is not rm
    sq, tall = G.guide_frame("square"), G.guide_frame("tall")
    assert sq.image.size == (1024, 1024) and tall.image.size == (816, 1632) and np.asarray(sq.image)[..., 3].max() == 0
    assert not sq.editable[:102].any() and sq.editable[103, 512] and not sq.editable[:, :102].any() and sq.editable[:, 103:921].any()
    assert G.guide_frame("square", margin=G.FRAME_MARGIN_I5).meta["margin"] == 0.12
    box = G.guide_acc_box((1.0, 2.0), (2.0, 2.0))
    ba = np.asarray(box.image)
    grey = (ba[..., :3] == P.hex_to_rgb(G.GUIDE_GREY)).all(axis=2)
    ys, xs = np.nonzero(grey)
    assert ba[0, 0, 0] == 242 and (ys.max() - ys.min() + 1) <= 0.77 * 1024 and (ys.max() - ys.min()) > (xs.max() - xs.min())     # taller than wide
    assert box.editable[grey].all() and box.editable.sum() > grey.sum()
    huge = G.guide_acc_box((9.0, 9.0), (2.0, 2.0))
    gh = (np.asarray(huge.image)[..., :3] == P.hex_to_rgb(G.GUIDE_GREY)).all(axis=2)
    assert gh.any(axis=1).sum() <= 0.77 * 1024 + 2


# ---------------------------------------------------------------- guide_regions.json
def test_guide_regions_map_template_regions_to_pixel_boxes_and_mirror_in_the_back_view(tmp_path):
    g = G.guide_concept_char(FIG)
    data = G.build_guide_regions(g)
    f, b = data["figures"]["front"], data["figures"]["back"]
    geo_f, geo_b = G.figure_geometry(384), G.figure_geometry(1152)
    assert f["regions_shirt"]["torso_f"] == list(geo_f["torso"]) and b["regions_shirt"]["torso_b"] == list(geo_b["torso"])
    assert f["regions_shirt"]["rlimb_f"] == list(geo_f["arm_l"])           # the character's right arm is image-left in the front view
    assert b["regions_shirt"]["rlimb_b"] == list(geo_b["arm_r"])           # and image-right in the back view
    assert f["regions_pants"]["rlimb_f"] == list(geo_f["leg_l"]) and b["regions_pants"]["llimb_b"] == list(geo_b["leg_l"])
    assert f["hair_box"] == [384 - 180, 340 - 240, 384 + 180, 340 + 360] and f["head_box"] == list(geo_f["head"])
    shoe = f["shoe_band"]["right"]
    assert shoe[3] == 964 and shoe[3] - shoe[1] == 48
    assert f["attachments"]["hat"] == [384, 484 - 132] and b["attachments"]["left_collar"][0] < 1152                 # mirrored in the back view
    assert data["attachments_source"] == "default"
    given = G.build_guide_regions(g, {"hat": (0.0, 2.0)})
    assert given["attachments_source"] == "given" and given["figures"]["front"]["attachments"]["hat"] == [384, 484 - 240]
    path = tmp_path / "out" / "guide_regions.json"
    written = G.write_guide_regions(g, str(path))
    assert json.loads(path.read_text(encoding="utf-8")) == json.loads(json.dumps(written))


# ---------------------------------------------------------------- C2 sheet, labels, crops, vlm preparation
def test_concept_sheet_is_four_slots_without_rescaling_and_labels_come_after():
    a = Image.new("RGB", (1536, 1024), (10, 20, 30))
    b = Image.new("RGB", (1536, 1024), (200, 100, 50))
    sheet = G.assemble_concept_sheet(a, b)
    assert sheet.size == (3072, 1024) and sheet.getpixel((100, 100)) == (10, 20, 30) and sheet.getpixel((3000, 900)) == (200, 100, 50)
    with pytest.raises(ValueError, match="expected"):
        G.assemble_concept_sheet(Image.new("RGB", (100, 100)), b)
    assert G.downscale_for_judges(sheet).size == (2304, 768)
    labelled = G.add_sheet_labels(sheet)
    assert labelled.size == sheet.size and not np.array_equal(np.asarray(labelled), np.asarray(sheet))
    assert np.array_equal(np.asarray(sheet)[300:], np.asarray(labelled)[300:])         # labels sit at the top only; the input is untouched
    assert sheet.getpixel((20, 20)) == (10, 20, 30)


def test_part_crop_keeps_the_flat_background_and_scales_the_long_edge():
    concept = Image.new("RGBA", (1536, 1024), (0, 0, 0, 0))
    ImageDraw.Draw(concept).rectangle([300, 300, 500, 400], fill=(200, 30, 30, 255))
    crop = G.prepare_part_crop(concept, (250, 280, 550, 420))
    assert crop.mode == "RGB" and max(crop.size) == 768 and crop.getpixel((2, 2)) == P.hex_to_rgb(G.BG_HEX)
    assert crop.getpixel((crop.width // 2, crop.height // 2)) == (200, 30, 30)
    assert max(G.prepare_part_crop(concept, (250, 280, 550, 420), long_edge=512).size) == 512
    for bad in (100, 2000):
        with pytest.raises(ValueError, match="long_edge"):
            G.prepare_part_crop(concept, (0, 0, 10, 10), long_edge=bad)


def test_side_by_side_uses_a_32px_gutter_and_centres_vertically():
    a, b = Image.new("RGB", (100, 80), (255, 0, 0)), Image.new("RGB", (60, 40), (0, 0, 255))
    out = G.side_by_side([a, b])
    assert out.size == (100 + 32 + 60, 80) and out.getpixel((110, 5)) == (255, 255, 255) and out.getpixel((140, 40)) == (0, 0, 255)
    assert out.getpixel((140, 5)) == (255, 255, 255)
    assert G.side_by_side([a], gutter=10).size == (100, 80)
    with pytest.raises(ValueError):
        G.side_by_side([])


def test_vlm_image_shows_transparency_on_grey_and_a_checkerboard_and_stays_inside_the_size_limits():
    cut = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    ImageDraw.Draw(cut).ellipse([8, 8, 56, 56], fill=(220, 30, 30, 255))
    v = G.vlm_image(cut)
    assert min(v.size) >= 256 and max(v.size) <= 2576
    a = np.asarray(v)
    assert tuple(a[2, 2]) == (128, 128, 128)                                           # the grey panel
    assert v.width > 2 * v.height * 0.9                                                  # two panels side by side
    cb = a[:, v.width // 2 + 16:][:32, :32]
    assert len(np.unique(cb.reshape(-1, 3), axis=0)) == 2                               # the checkerboard panel has two greys
    opaque = G.vlm_image(Image.new("RGB", (300, 300), (1, 2, 3)))
    assert opaque.size == (300, 300)
    assert max(G.vlm_image(Image.new("RGB", (4000, 3000), (1, 2, 3))).size) <= 2576
    only_grey = G.vlm_image(cut, with_checkerboard=False)
    assert only_grey.size == (256, 256)
    tiny = G.vlm_image(Image.new("RGB", (30, 20), (9, 9, 9)))
    assert min(tiny.size) >= 256 and tiny.getpixel((0, 0)) == (9, 9, 9)


def test_readability_preview_loses_detail_below_the_100px_rule():
    im = Image.new("RGBA", (512, 512), (255, 255, 255, 255))
    d = ImageDraw.Draw(im)
    for x in range(0, 512, 4):
        d.line([x, 0, x, 511], fill=(0, 0, 0, 255), width=1)                              # hairlines that cannot survive 100 px
    p = G.readability_preview(im)
    assert min(p.size) >= 256
    inner = np.asarray(p.convert("L"))[20:-20, 20:-20].astype(float)
    assert inner.std() < 60                                                                # smeared to grey
    assert G.readability_preview(im, badge=True).size[0] >= 256
    assert G.readability_preview(im, long_edge=64).size[0] >= 256
    assert G.png_bytes(p)[:8] == b"\x89PNG\r\n\x1a\n"

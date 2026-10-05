"""validate_template: a pass and a fail case for every rule, fail-closed behaviour, Shirt vs Pants (CHK-B01..B11, soft flags)."""
from __future__ import annotations

import io
import struct
import zlib

import numpy as np
import pytest
from clo_helpers import compose, composed, official, rgba
from PIL import Image

from duoskin.checks.model import gate_verdict
from duoskin.imaging import print_place as PP
from duoskin.roblox import limits_clothing as LC
from duoskin.roblox import template as T
from duoskin.roblox import validators as V


def run(img, kind="shirt", lm=None, res=None, **kw):
    kw.setdefault("ocr_hook", False)
    if res is not None:
        kw.setdefault("recipe", res.meta)
        kw.setdefault("base_layer", res.base_layer_png)
        kw.setdefault("placements", res.placements)
        kw.setdefault("stack", res.stack)
        lm = res.label_map if lm is None else lm
    return {r.check_id: r for r in V.validate_template(img, kind, lm, **kw)}


@pytest.fixture(scope="module")
def tee():
    return composed("tee")


@pytest.fixture(scope="module")
def jeans():
    return composed("jeans_straight")


def png_of(arr):
    b = io.BytesIO()
    Image.fromarray(arr, "RGBA").save(b, "PNG")
    return b.getvalue()


def inject(data: bytes, tag: bytes, payload: bytes) -> bytes:
    chunk = struct.pack(">I", len(payload)) + tag + payload + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
    return data[:33] + chunk + data[33:]


# ------------------------------------------------------------------------------------------------------------ the good file
def test_a_composed_template_passes_everything(tee):
    out = run(tee.png, res=tee)
    assert set(out) >= {"CHK-B01", "CHK-B02", "CHK-B03", "CHK-B04", "CHK-B05", "CHK-B06", "CHK-B07", "CHK-B08", "CHK-B11"}
    assert all(r.passed for r in out.values()), [(k, r.evidence) for k, r in out.items() if not r.passed]
    assert gate_verdict(list(out.values())) == "pass"
    kinds = {k: r.kind for k, r in out.items()}
    assert kinds["CHK-B01"] == "assert" and kinds["CHK-B03"] == "hard" and kinds["CHK-B04"] == "hard" and kinds["CHK-B06"] == "hard"
    assert kinds["CHK-B05.split_rows"] == "soft" and kinds["CHK-B11"] == "assert"
    assert out["CHK-B04"].value < 3 and "A_OCR" not in out
    assert out["CHK-B01"].fm_ids == ["CLO-01"] and out["CHK-B11"].fm_ids == ["CLO-18"]


def test_the_title_of_the_task_signature_works_with_four_arguments(tee):
    results = V.validate_template(tee.png, "shirt", tee.label_map, tee.meta, ocr_hook=False)
    ids = {r.check_id: r for r in results}
    assert ids["CHK-B04"].passed and "approximate" in ids["CHK-B04"].threshold or "label-1" in ids["CHK-B04"].threshold     # label-map seam check
    assert ids["CHK-B07"].passed and not V.failures(results)
    assert V.validate_accessory is not None                                                                                       # re-export


def test_array_and_pil_and_path_inputs(tee, tmp_path):
    p = tmp_path / "t.png"
    p.write_bytes(tee.png)
    for src in (rgba(tee.png), Image.open(io.BytesIO(tee.png)), p, str(p), tee.png):
        out = run(src, res=tee)
        assert out["CHK-B01"].passed and out["CHK-B11"].passed


# ------------------------------------------------------------------------------------------------------------ CHK-B01 / B02
def test_b01_b02_wrong_size_mode_depth_chunks_and_format(tee, monkeypatch):
    small = T.blank_template()[:, :584]
    out = run(small)
    assert not out["CHK-B01"].passed and not out["CHK-B02"].passed and len(out) == 2        # nothing else is evaluated on a wrong-size file
    rgb = io.BytesIO()
    Image.new("RGB", (585, 559)).save(rgb, "PNG")
    r = run(rgb.getvalue())["CHK-B02"]
    assert not r.passed and "colour type" in r.evidence
    assert not run(inject(tee.png, b"gAMA", struct.pack(">I", 45455)), res=tee)["CHK-B02"].passed
    assert not run(inject(tee.png, b"iCCP", b"x\x00\x00abc"), res=tee)["CHK-B02"].passed
    pal = io.BytesIO()
    Image.new("P", (585, 559)).save(pal, "PNG")
    assert not run(pal.getvalue())["CHK-B02"].passed
    jpg = io.BytesIO()
    Image.new("RGB", (585, 559)).save(jpg, "JPEG")
    out = run(jpg.getvalue())
    assert all(not r.ran and not r.passed for r in out.values()) and "CHK-B02" in out          # fail closed: a JPEG proves nothing
    monkeypatch.setattr(LC, "MAX_FILE_BYTES", 1000)
    assert not run(tee.png, res=tee)["CHK-B02"].passed and "bytes" in run(tee.png, res=tee)["CHK-B02"].evidence


def test_b01_crop_selftest_is_cached_and_exact():
    assert V._crop_selftest() == ""
    assert V.check_b01(T.blank_template()).passed


# ------------------------------------------------------------------------------------------------------------ CHK-B03 / B11
def test_b03_gap_and_bleed(tee):
    img = rgba(tee.png).copy()
    y, x = T.REGIONS["torso_f"][1] + 40, T.REGIONS["torso_f"][2] + 1          # the gap pixel next to FRONT's right edge
    img[y, x] = (255, 0, 0, 255)
    assert not run(img, res=tee)["CHK-B03"].passed
    img2 = rgba(tee.png).copy()
    ox, oy = T.REGIONS["rlimb_l"][0] - 1, T.REGIONS["rlimb_l"][1] + 10            # the open-side bleed pixel
    img2[oy, ox] = (0, 0, 0, 0)
    r = run(img2, res=tee)["CHK-B03"]
    assert not r.passed and r.fix_hint == "code_bleed"
    assert run(T.finish_edges(img2), res=tee)["CHK-B03"].passed                    # the repair is exactly finish_edges


def test_b11_stray_pixels_skin_and_the_official_template_as_base(tee):
    img = rgba(tee.png).copy()
    img[3, 3] = (255, 255, 255, 255)
    r = run(img, res=tee)["CHK-B11"]
    assert not r.passed and r.fix_hint == "code_alpha_cleanup" and "stray" in r.evidence
    # the bleed ring is allowed, one pixel beyond max bleed (4) is not
    x0, y0 = T.REGIONS["rlimb_l"][:2]
    img2 = rgba(tee.png).copy()
    img2[y0 + 10, x0 - 5] = (9, 9, 9, 255)
    assert not run(img2, res=tee)["CHK-B11"].passed
    for kind in ("shirt", "pants"):                                                  # CLO-18: the official PNG is only a guide overlay
        out = run(official(kind), kind, lm=np.ones((559, 585), np.uint8))
        assert not out["CHK-B11"].passed and not out["CHK-B03"].passed
    img3 = rgba(tee.png).copy()
    lm = tee.label_map.copy()
    x, y = 294, 76                                                                  # the neck hole: skin => alpha 0
    img3[y, x] = (200, 100, 100, 255)
    assert not run(img3, lm=lm, res=tee)["CHK-B11"].passed and "bare-skin" in run(img3, lm=lm, res=tee)["CHK-B11"].evidence
    lm2 = lm.copy()
    lm2[420, 100] = 1                                                               # labelled garment but transparent
    assert not run(rgba(tee.png), lm=lm2, res=tee)["CHK-B11"].passed


# ------------------------------------------------------------------------------------------------------------ CHK-B04
def test_b04_seam_discontinuity_on_the_base_layer(tee):
    base = rgba(tee.base_layer_png).copy()
    assert V.check_b04(base).passed
    T.crop(base, "torso_l")[..., :3] = (255, 255, 255)                              # a different colour on one face breaks 4 seams
    r = V.check_b04(base)
    assert not r.passed and r.kind == "hard" and r.value > 6 and "torso_l" in r.evidence
    out = run(tee.png, res=tee, base_layer=base)
    assert not out["CHK-B04"].passed
    # a single mismatching pixel pair breaks the strict maximum even when the mean is fine
    base2 = rgba(tee.base_layer_png).copy()
    base2[T.REGIONS["torso_f"][1] + 5, T.REGIONS["torso_f"][2]] = (0, 0, 0, 255)
    assert not V.check_b04(base2).passed
    # reversing a seam's pixel order would also fail: the strict check compares the true neighbours
    st = V.seam_stats(rgba(tee.base_layer_png))
    assert len(st) == 36 and {s["kind"] for s in st} == {"side", "cap", "cap_rot"}


def test_b04_without_a_base_layer_uses_the_label_map_or_fails_closed(tee):
    img = rgba(tee.png)
    ok = V.check_b04_from_labels(img, tee.label_map)
    assert ok.passed and "label-1" in ok.threshold
    broken = img.copy()
    T.crop(broken, "torso_l")[..., :3] = (255, 255, 255)
    lm = np.ones((559, 585), np.uint8)
    assert not V.check_b04_from_labels(broken, lm).passed
    out = {r.check_id: r for r in V.validate_template(tee.png, "shirt", None, None, ocr_hook=False)}
    assert not out["CHK-B04"].ran and not out["CHK-B04"].passed and out["CHK-B04"].kind == "hard"
    assert not out["CHK-B05"].ran


def test_b04_composite_is_soft_and_tolerates_a_stitch_on_a_seam(tee):
    img = rgba(tee.png).copy()
    assert V.check_b04_composite(img).passed and V.check_b04_composite(img).kind == "soft"
    T.crop(img, "torso_l")[..., :3] = (255, 255, 255)
    r = V.check_b04_composite(img)
    assert not r.passed and r.kind == "soft"
    out = run(img, res=tee)
    assert gate_verdict(list(out.values())) == "pass" or not out["CHK-B04.composite"].passed        # a soft failure never blocks by itself


# ------------------------------------------------------------------------------------------------------------ CHK-B05
def test_b05_shoe_top_edge_gloves_bracelets_and_wrong_template(jeans, tee):
    lm = jeans.label_map.copy()
    assert V.check_b05("pants", lm).passed
    bad = lm.copy()
    _x0, y0, _x1, _y1 = T.REGIONS["rlimb_f"]
    T.crop(bad, "rlimb_f")[440 - y0:446 - y0] = 6                                  # a shoe whose top edge is at row 440
    r = V.check_b05("pants", bad)
    assert not r.passed and "440" in r.evidence and r.kind == "assert"
    tall = lm.copy()
    T.crop(tall, "llimb_b")[466 - y0:470 - y0] = 5                                  # a glove piece across the wrist split
    assert not V.check_b05("shirt", tall).passed
    straddle = np.zeros_like(lm)
    T.crop(straddle, "llimb_b")[410 - y0:425 - y0] = 5                              # a bracelet across the elbow split
    r = V.check_b05("shirt", straddle)
    assert not r.passed and "band" in r.evidence
    ok = np.zeros_like(lm)
    T.crop(ok, "llimb_b")[452 - y0:466 - y0] = 5
    T.crop(ok, "llimb_b")[469 - y0:483 - y0] = 5                                   # cuff + hand: two pieces, each inside one band
    assert V.check_b05("shirt", ok).passed
    assert not V.check_b05("shirt", lm).passed and not V.check_b05("pants", np.where(ok > 0, 5, 0).astype(np.uint8)).passed      # kit on the wrong template
    torso = np.zeros_like(lm)
    T.crop(torso, "torso_f")[10:20] = 6
    assert not V.check_b05("pants", torso).passed
    with pytest.raises(V.CheckUnavailable):
        V.check_b05("shirt", None)                                                    # needs the label map (fail closed through validate_template)


def test_b05_soft_split_rows_hem_exemption_and_print_flags(tee, jeans):
    img = rgba(tee.png).copy()
    assert V.check_split_rows(img, tee.meta).passed
    x0, _y0, x1, _y1 = T.REGIONS["torso_f"]
    bad = img.copy()
    bad[168 - 0:171, x0:x1 + 1, :3] = (250, 250, 0)                                  # a bright band across rows 168-170: edges within 2 px of 170
    r = V.check_split_rows(bad, tee.meta)
    assert not r.passed and r.kind == "soft" and "torso_f" in r.evidence
    # a vertical stitch crossing row 170 is NOT flagged (only horizontal steps count)
    vert = img.copy()
    vert[160:180, 280, :3] = (0, 0, 0)
    assert V.check_split_rows(vert, tee.meta).passed
    # an alpha edge at the recipe's own hem row is exempt, anywhere else it is flagged
    tucked = compose("tee", attrs={"hem": "waist_tucked"}, run_checks=False)
    timg = rgba(tucked.png)
    assert V.check_split_rows(timg, tucked.meta).passed and 169 in tucked.meta["hem_rows"]
    assert not V.check_split_rows(timg, {}).passed
    # pants: the waist edge at 169|170 is the recipe's hem
    assert V.check_split_rows(rgba(jeans.png), jeans.meta).passed


def test_b05_soft_print_inset_and_hidden_rows(jeans, tee):
    lm = np.zeros((559, 585), np.uint8)
    _x0, _y0, _x1, _y1 = T.REGIONS["rlimb_f"]
    T.crop(lm, "rlimb_f")[10:30, 1:10] = 3                                          # print pixels 1 px from the left edge, rows 365-384
    out = V.check_print_inset(rgba(jeans.png), lm, [], "pants")
    inset, hidden = out
    assert not inset.passed and not hidden.passed and "rlimb_f" in inset.evidence and "rows 355-377" in hidden.threshold
    shirt_out = V.check_print_inset(rgba(tee.png), lm, [], "shirt")
    assert not shirt_out[0].passed and shirt_out[1].passed                          # hidden rows only matter for Pants legs
    u = np.zeros_like(lm)
    T.crop(u, "rlimb_u")[20:40, 20:40] = 3
    assert not V.check_print_inset(rgba(jeans.png), u, [], "pants")[1].passed        # Pants U faces are never sampled
    assert V.check_print_inset(rgba(tee.png), u, [], "shirt")[1].passed
    clean = np.zeros_like(lm)
    T.crop(clean, "rlimb_f")[40:60, 20:40] = 3                                      # rows 395-414
    assert all(r.passed for r in V.check_print_inset(rgba(jeans.png), clean, [], "pants"))
    wrap = [PP.Placement("w", "rlimb_f", (217, 390, 230, 410), True, "small")]
    edge = np.zeros_like(lm)
    T.crop(edge, "rlimb_f")[40:50, 0:5] = 3
    assert V.check_print_inset(rgba(jeans.png), edge, wrap, "pants")[0].passed       # a wrap print may touch the shared edge


# ------------------------------------------------------------------------------------------------------------ CHK-B06
def test_b06_alpha_policy_haze_and_cut_edges(tee):
    img = rgba(tee.png).copy()
    assert V.check_b06(img).passed
    haze = img.copy()
    c = T.crop(haze, "torso_f")
    c[20:60, 20:60, 3] = 120                                                        # a 40x40 patch of semi-transparent haze in the garment
    r = V.check_b06(haze)
    assert not r.passed and r.kind == "hard" and r.value > 0.005 and r.fix_hint == "code_alpha_cleanup"
    edge = img.copy()
    edge[404, 19:83, 3] = 128                                                       # AA on the sleeve cut edge: not counted
    assert V.check_b06(edge).passed
    few = img.copy()
    T.crop(few, "torso_f")[30, 30:33, 3] = 200
    assert V.check_b06(few).passed                                                  # 3 px of 98,000 is below 0.5%


def test_b06_soft_skin_in_clothing_and_waistband(tee, jeans):
    skin = (214, 170, 140)
    img = rgba(tee.png).copy()
    assert V.check_skin_in_clothing(img, skin).passed
    T.crop(img, "torso_f")[20:100, 20:100, :3] = skin
    r = V.check_skin_in_clothing(img, skin)
    assert not r.passed and r.kind == "soft" and r.value > 0.01
    with pytest.raises(V.NotApplicable):
        V.check_skin_in_clothing(img, None)
    out = {x.check_id: x for x in V.validate_template(img, "shirt", tee.label_map, tee.meta, skin="#d6aa8c", ocr_hook=False)}
    assert not out["CHK-B06.skin_in_clothing"].passed and out["CHK-B06.skin_in_clothing"].kind == "soft"
    assert out["CHK-B06.waistband_hidden"].status == "not_applicable"
    # waistband: pants trim on torso rows 170-201 under an opaque shirt (untucked tee covers 74-201)
    pants_img, pants_lm = rgba(jeans.png), jeans.label_map
    r = V.check_waistband_hidden("pants", pants_img, pants_lm, rgba(tee.png))
    assert not r.passed and r.kind == "soft" and r.value > 0.9
    tucked = compose("tee", attrs={"hem": "waist_tucked"}, run_checks=False)
    assert V.check_waistband_hidden("pants", pants_img, pants_lm, rgba(tucked.png)).passed
    assert not V.check_waistband_hidden("shirt", rgba(tee.png), tee.label_map, pants_img).passed
    with pytest.raises(V.NotApplicable):
        V.check_waistband_hidden("pants", pants_img, pants_lm, None)


# ------------------------------------------------------------------------------------------------------------ CHK-B07
def test_b07_stack_order_hash_placements_and_mirrors(tee):
    assert V.check_b07(tee.stack, tee.layer_stack_hash, tee.placements, tee.label_map).passed
    swapped = list(tee.stack)
    swapped[0], swapped[-1] = swapped[-1], swapped[0]
    r = V.check_b07(swapped, None, None, None)
    assert not r.passed and "CLO-15" in r.evidence
    assert not V.check_b07(tee.stack, "0" * 64, None, None).passed
    unknown = [{"stage": "nonsense"}]
    assert not V.check_b07(unknown, None, None, None).passed
    with pytest.raises(V.CheckUnavailable):
        V.check_b07(None, None, None, None)
    # the hash in the evidence is the one the compositor reports
    from duoskin.models.common import sha256_of

    assert sha256_of(list(tee.stack)) == tee.layer_stack_hash


# ------------------------------------------------------------------------------------------------------------ CHK-B08
def test_b08_orientation_selftest_has_teeth(tee, monkeypatch):
    assert V.check_b08(False).passed and "no_body_base" in V.check_b08(False).evidence and V.check_b08(True).passed
    V._orientation_selftest.cache_clear()
    monkeypatch.setattr(T, "char_side_to_image_side", lambda s: "image_right" if s == "right" else "image_left")
    r = V.check_b08(False)
    assert not r.passed and r.kind == "assert" and "right/left" in r.evidence
    monkeypatch.undo()
    V._orientation_selftest.cache_clear()
    flipped = tuple(s._replace(reverse=not s.reverse) if s.edge == 4 else s for s in T.ADJACENCY)
    monkeypatch.setattr(T, "ADJACENCY", flipped)
    assert not V.check_b08(False).passed
    monkeypatch.undo()
    V._orientation_selftest.cache_clear()
    assert V.check_b08(False).passed


# ------------------------------------------------------------------------------------------------------------ OCR hook, modesty
def test_ocr_hook_default_and_custom(tee):
    out = {r.check_id: r for r in V.validate_template(tee.png, "shirt", tee.label_map, tee.meta, base_layer=tee.base_layer_png)}
    from duoskin.imaging import ocr

    if ocr.get_engine() is None:
        assert out["A_OCR"].status == "not_applicable" and out["A_OCR"].na_reason == "no_ocr_engine"
    r = {x.check_id: x for x in V.validate_template(tee.png, "shirt", tee.label_map, tee.meta, ocr_hook=lambda im: ["FRONT", "BACK"])}["A_OCR"]
    assert not r.passed and r.kind == "hard" and "FRONT" in r.evidence
    ok = {x.check_id: x for x in V.validate_template(tee.png, "shirt", tee.label_map, tee.meta, ocr_hook=lambda im: [])}["A_OCR"]
    assert ok.passed
    boom = {x.check_id: x for x in V.validate_template(tee.png, "shirt", tee.label_map, tee.meta, ocr_hook=lambda im: 1 / 0)}["A_OCR"]
    assert not boom.ran and not boom.passed and boom.kind == "hard"                  # a crashing hook fails closed
    assert "A_OCR" not in {x.check_id for x in V.validate_template(tee.png, "shirt", tee.label_map, tee.meta, ocr_hook=False)}


def test_modesty_layer_presence_and_distance_from_skin(tee):
    ok = V.check_modesty("shirt", None, None, {"modesty_colour": "#445566", "skin_tone": "#e3b08e"}, tee.meta)[0]
    assert ok.passed and ok.check_id == "CHK-MOD01" and ok.kind == "hard"
    missing = V.check_modesty("shirt", None, None, {"skin_tone": "#e3b08e"}, tee.meta)[0]
    assert not missing.passed and "no modesty colour" in missing.evidence
    close = V.check_modesty("shirt", None, None, {"modesty_colour": "#e3b090", "skin_tone": "#e3b08e"}, tee.meta)[0]
    assert not close.passed and close.value < 10
    with pytest.raises(V.NotApplicable):
        V.check_modesty("shirt", None, None, None, tee.meta)
    out = {r.check_id: r for r in V.validate_template(tee.png, "shirt", tee.label_map, tee.meta, ocr_hook=False)}
    assert out["CHK-MOD01"].status == "not_applicable" and out["CHK-MOD01"].na_reason == "no_plan"


# ------------------------------------------------------------------------------------------------------------ fail closed, kinds
def test_validate_template_never_raises_and_fails_closed_on_garbage():
    out = V.validate_template(b"not a png at all", "shirt", None, None)
    assert out and all(not r.ran and not r.passed for r in out)
    assert gate_verdict(out) == "fail"
    out2 = V.validate_template(np.zeros((10, 10), np.uint8), "pants", None, None, ocr_hook=False)
    assert out2 and gate_verdict(out2) == "fail"
    with pytest.raises(ValueError):
        V.validate_template(T.blank_template(), "hat")
    wrong_label = np.zeros((10, 10), np.uint8)
    out3 = V.validate_template(T.blank_template(), "shirt", wrong_label, None, ocr_hook=False)      # a label map of the wrong shape is ignored
    assert not {r.check_id: r for r in out3}["CHK-B05"].ran


def test_a_blank_template_is_valid_but_empty():
    out = run(T.blank_template(), lm=np.zeros((559, 585), np.uint8))
    assert out["CHK-B01"].passed and out["CHK-B02"].passed and out["CHK-B03"].passed and out["CHK-B11"].passed and out["CHK-B06"].passed


def test_shirt_and_pants_differences(tee, jeans):
    shirt_out = run(rgba(jeans.png), "shirt", res=jeans)
    assert not shirt_out["CHK-B05"].passed                                          # shoes on a Shirt
    pants_out = run(rgba(tee.png), "pants", res=tee)
    assert pants_out["CHK-B05"].passed                                              # a tee has no kit pieces: fine for Pants too
    assert run(rgba(jeans.png), "pants", res=jeans)["CHK-B05"].passed
    assert run(rgba(composed("tee").png), "shirt", res=composed("tee"))["CHK-B05"].passed
    gl = compose("tee", extras=["gloves"], run_checks=False)
    assert not run(rgba(gl.png), "pants", res=gl)["CHK-B05"].passed                 # gloves on Pants


def test_limits_clothing_agree_with_the_template_and_the_registry():
    assert LC.consistency_problems() == []
    d = LC.describe()
    assert d["template_size"] == [585, 559] and d["upload"]["fee_robux"] == 80 and d["upload"]["refundable"] is False
    assert LC.seam_de_limits() == (6.0, 15.0) and LC.bleed_range_px() == (2, 4) and LC.gap_px() == 2
    assert LC.limb_bands() == ((355, 416), (421, 465), (469, 482)) and LC.shoe_top_row_range() == (446, 465)
    assert LC.semi_alpha_share_max() == 0.005 and LC.skin_in_clothing_de() == 6.0 and LC.hidden_leg_rows() == (355, 377)

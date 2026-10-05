"""Golden tests for the classic template geometry (CLO-01, CLO-03, CLO-05, CLO-06, CLO-18)."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
from clo_helpers import official

from duoskin.roblox import template as T

# the literal tables of FAILURE_MODES §5.2 (inclusive boxes)
SPEC_BOXES = {
    "torso_u": (231, 8, 358, 71), "torso_r": (165, 74, 228, 201), "torso_f": (231, 74, 358, 201),
    "torso_l": (361, 74, 424, 201), "torso_b": (427, 74, 554, 201), "torso_d": (231, 204, 358, 267),
    "rlimb_u": (217, 289, 280, 352), "rlimb_l": (19, 355, 82, 482), "rlimb_b": (85, 355, 148, 482),
    "rlimb_r": (151, 355, 214, 482), "rlimb_f": (217, 355, 280, 482), "rlimb_d": (217, 485, 280, 548),
    "llimb_u": (308, 289, 371, 352), "llimb_f": (308, 355, 371, 482), "llimb_l": (374, 355, 437, 482),
    "llimb_b": (440, 355, 503, 482), "llimb_r": (506, 355, 569, 482), "llimb_d": (308, 485, 371, 548)}
SPEC_SIDE = [("torso_r", "torso_f"), ("torso_f", "torso_l"), ("torso_l", "torso_b"), ("torso_b", "torso_r"),
             ("rlimb_l", "rlimb_b"), ("rlimb_b", "rlimb_r"), ("rlimb_r", "rlimb_f"), ("rlimb_f", "rlimb_l"),
             ("llimb_f", "llimb_l"), ("llimb_l", "llimb_b"), ("llimb_b", "llimb_r"), ("llimb_r", "llimb_f")]
SPEC_CAP = [("torso_u", "torso_f"), ("torso_f", "torso_d"), ("rlimb_u", "rlimb_f"), ("rlimb_f", "rlimb_d"),
            ("llimb_u", "llimb_f"), ("llimb_f", "llimb_d")]
# the official template paints each face type in its own flat colour; labels and the dashed guides are drawn on top
FILL = {"u": (0, 162, 255), "d": (246, 136, 2), "f": (226, 35, 26), "b": (0, 116, 189), "l": (246, 183, 2), "r": (2, 183, 87)}
BG = (227, 227, 227)


def test_boxes_equal_the_spec_tables():
    assert T.REGIONS == SPEC_BOXES
    assert T.SIDE_SEAMS == SPEC_SIDE
    assert T.CAP_SEAMS == SPEC_CAP
    assert T.SIZE["torso_f"] == (128, 128) and T.SIZE["torso_u"] == (128, 64) and T.SIZE["torso_r"] == (64, 128)
    assert T.SIZE["rlimb_u"] == (64, 64) and T.SIZE["rlimb_f"] == (64, 128) and T.SIZE["llimb_d"] == (64, 64)
    assert T.SIZE_WH == (585, 559)


@pytest.mark.parametrize("kind", ["shirt", "pants"])
def test_regions_match_official_png_pixel_for_pixel(kind):
    a = official(kind)
    assert a.shape == (559, 585, 4) and (a[..., 3] == 255).all()
    inside = T.region_label_map() > 0
    all_fill = np.zeros(a.shape[:2], bool)
    for k, (x0, y0, x1, y1) in T.REGIONS.items():
        fill = np.all(a[..., :3] == np.array(FILL[T.FACE_OF[k]]), axis=2)
        all_fill |= fill
        box = np.zeros_like(fill)
        box[y0:y1 + 1, x0:x1 + 1] = True
        near = np.zeros_like(fill)
        near[max(y0 - 3, 0):y1 + 4, max(x0 - 3, 0):x1 + 4] = True
        ys, xs = np.nonzero(fill & near)
        # the region's flat fill reaches every edge of the box and never leaves it ...
        assert (xs.min(), ys.min(), xs.max(), ys.max()) == (x0, y0, x1, y1), k
        # ... the 1-px ring just outside carries no fill and no part of the box is background grey
        ring = np.zeros_like(fill)
        ring[max(y0 - 1, 0):y1 + 2, max(x0 - 1, 0):x1 + 2] = True
        assert not (fill & ring & ~box).any(), k
        assert not np.all(a[y0:y1 + 1, x0:x1 + 1, :3] == np.array(BG), axis=2).any(), k
    assert not (all_fill & ~inside).any()                      # no fill colour anywhere outside the 18 boxes
    # shared gaps are exactly 2 px of background grey
    for s in T.ADJACENCY:
        if s.gap:
            for region, side in ((s.a, s.side_a), (s.b, s.side_b)):
                ys, xs = T._outside_line(T.REGIONS[region], side, 1)
                # (the dashed white guide at row 170 crosses a few gap pixels, so allow a handful of non-grey ones)
                grey = np.all(a[ys, xs, :3] == np.array(BG), axis=-1)
                assert grey.mean() >= 0.9 and not np.any(np.all(a[ys, xs, :3] == np.array(FILL[T.FACE_OF[region]]), axis=-1)), (region, side)


def test_inclusive_crop_and_paste():
    img = np.arange(559 * 585, dtype=np.uint32).reshape(559, 585)
    c = T.crop(img, "torso_f")
    assert c.shape == (128, 128) and c[0, 0] == img[74, 231] and c[-1, -1] == img[201, 358]
    assert np.shares_memory(c, img)                          # a view, never a copy
    out = T.blank_template()
    T.paste(out, "rlimb_d", np.full((64, 64, 4), 7, np.uint8))
    assert (T.crop(out, "rlimb_d") == 7).all() and out[:, :, 3].sum() == 7 * 64 * 64
    with pytest.raises(ValueError):
        T.paste(out, "rlimb_d", np.zeros((64, 63, 4), np.uint8))


def test_paint_ids_round_trip_and_label_map():
    probe = T.paint_region_ids()
    lm = T.region_label_map()
    painted = 0
    for k in T.REGION_ORDER:
        c = T.crop(probe, k)
        assert (c[..., 0] == T.REGION_IDS[k]).all() and (c[..., 3] == 255).all()
        assert (T.crop(lm, k) == T.REGION_IDS[k]).all()
        painted += c.shape[0] * c.shape[1]
    assert int((probe[..., 3] > 0).sum()) == painted == int((lm > 0).sum())
    assert T.region_at(231, 74) == "torso_f" and T.region_at(230, 74) is None and T.region_at(358, 201) == "torso_f"
    assert T.region_at(-1, 0) is None and T.region_at(1000, 1000) is None
    assert T.region_mask("torso_f").sum() == 128 * 128


def test_blank_template_is_fully_transparent():
    b = T.blank_template()
    assert b.shape == (559, 585, 4) and b.dtype == np.uint8 and not b.any()


def test_adjacency_is_derived_from_the_frames_and_complete():
    assert list(T.ADJACENCY) == T.derive_adjacency()
    assert len(T.ADJACENCY) == 36
    for part in T.PARTS:
        seams = [s for s in T.ADJACENCY if T.PART_OF[s.a] == part]
        assert len(seams) == 12 and sorted(s.edge for s in seams) == list(range(1, 13))
    assert len(T.ROTATED_CAP_SEAMS) == 18
    # every side of every region has exactly one neighbour
    for k in T.REGION_ORDER:
        for side in T.SIDES:
            nb = T.neighbour(k, side)
            assert nb is not None, (k, side)
            back = T.neighbour(nb[0], nb[1])
            assert back is not None and back[0] == k and back[1] == side
    gaps = [s for s in T.ADJACENCY if s.gap]
    assert len(gaps) == 15          # 9 side seams (the 3 wraps B-R, F-L(limb) are not image-adjacent) + 6 caps with FRONT
    assert all(s.kind in ("side", "cap") for s in gaps)


def test_numbered_edge_texture_is_continuous_across_every_seam():
    tex = T.position_texture()
    worst = 0
    for s in T.ADJACENCY:
        ea, eb = T.seam_edges(tex, s)
        worst = max(worst, int(np.abs(ea[:, :3].astype(int) - eb[:, :3].astype(int)).max()))
    assert worst <= 4, worst
    # a wrong orientation flag breaks continuity: the test has teeth
    broken = 0
    for s in T.ADJACENCY:
        ea, eb = T.seam_edges(tex, s._replace(reverse=not s.reverse))
        broken = max(broken, int(np.abs(ea[:, :3].astype(int) - eb[:, :3].astype(int)).max()))
    assert broken > 60


def test_cap_orientation_front_edge_at_bottom_for_up_and_top_for_down():
    tex = T.position_texture()
    for part in T.PARTS:
        zu = T.crop(tex, f"{part}_u")[..., 2].astype(int)       # B channel = z (front +z = 255)
        zd = T.crop(tex, f"{part}_d")[..., 2].astype(int)
        assert zu[-1].mean() > 230 and zu[0].mean() < 25         # UP/U: front edge = image bottom
        assert zd[0].mean() > 230 and zd[-1].mean() < 25         # DOWN/D: front edge = image top


def test_numbered_edge_glyph_texture_keeps_the_position_code_outside_the_glyphs():
    g = T.numbered_edge_texture(glyphs=True)
    p = T.position_texture()
    assert (g[..., 3] == p[..., 3]).all()
    changed = (g != p).any(axis=2)
    assert 0 < changed.sum() < 0.12 * (p[..., 3] > 0).sum()
    for s in T.ADJACENCY:                                    # the glyph chips never touch the outermost edge pixels
        for region, side in ((s.a, s.side_a), (s.b, s.side_b)):
            assert np.array_equal(T.edge_pixels(g, region, side), T.edge_pixels(p, region, side))


def test_limb_sides_and_character_side_helper():
    assert T.char_side_to_image_side("right") == "image_left" and T.char_side_to_image_side("left") == "image_right"
    assert T.limb_prefix("Right") == "rlimb" and T.limb_region("left", "F") == "llimb_f"
    assert T.REGIONS["rlimb_f"][0] < T.REGIONS["llimb_f"][0]          # the character's right arm is on the template's left
    assert T.limb_face_role("rlimb_l") == "inner" and T.limb_face_role("rlimb_r") == "outer"
    assert T.limb_face_role("llimb_r") == "inner" and T.limb_face_role("llimb_l") == "outer"
    assert T.limb_face_role("rlimb_u") == "top"
    with pytest.raises(ValueError):
        T.char_side_to_image_side("middle")
    with pytest.raises(ValueError):
        T.limb_face_role("torso_f")


def test_fill_shared_gaps_one_pixel_per_side_and_interiors_untouched():
    rng = np.random.default_rng(1)
    img = T.blank_template()
    for k in T.REGION_ORDER:
        c = T.crop(img, k)
        c[...] = rng.integers(0, 256, c.shape, dtype=np.uint8)
        c[..., 3] = 255
    out = T.fill_shared_gaps(img)
    for k in T.REGION_ORDER:
        assert np.array_equal(T.crop(out, k), T.crop(img, k))
    for s in T.ADJACENCY:
        if not s.gap:
            continue
        ya, xa = T._outside_line(T.REGIONS[s.a], s.side_a, 1)
        yb, xb = T._outside_line(T.REGIONS[s.b], s.side_b, 1)
        assert np.array_equal(out[ya, xa], T.edge_pixels(img, s.a, s.side_a))
        assert np.array_equal(out[yb, xb], T.edge_pixels(img, s.b, s.side_b))
    changed = (out != img).any(axis=2)
    assert changed.sum() > 0 and not (changed & (T.region_label_map() > 0)).any()
    # only gap pixels changed: every changed pixel lies in a 2-px strip between two image-adjacent regions
    gap_px = np.zeros_like(changed)
    for s in T.ADJACENCY:
        if s.gap:
            for region, side in ((s.a, s.side_a), (s.b, s.side_b)):
                ys, xs = T._outside_line(T.REGIONS[region], side, 1)
                gap_px[ys, xs] = True
    assert not (changed & ~gap_px).any()


@pytest.mark.parametrize("px", [2, 3, 4])
def test_bleed_open_sides_depth_and_scope(px):
    img = T.blank_template()
    for k in T.REGION_ORDER:
        c = T.crop(img, k)
        c[...] = (10 * T.REGION_IDS[k], 20, 30, 255)
    out = T.bleed_open_sides(img, px)
    inside = T.region_label_map() > 0
    assert np.array_equal(out[inside], img[inside])                       # interiors byte identical
    x0, y0, x1, y1 = T.REGIONS["rlimb_l"]                                  # left edge is open: x=19
    assert (out[y0:y1 + 1, x0 - px:x0, 3] == 255).all() and (out[y0:y1 + 1, x0 - px - 1, 3] == 0).all()
    x0, y0, x1, y1 = T.REGIONS["torso_f"]                                  # the shared gap gets exactly 1 px per side
    assert (out[y0:y1 + 1, x1 + 1, :3] == img[y0, x1, :3]).all()
    assert (out[y0:y1 + 1, x1 + 2, :3] == T.crop(img, "torso_l")[0, 0, :3]).all()
    assert out[:, :, 3][~T.allowed_pixels(px)].sum() == 0
    with pytest.raises(ValueError):
        T.bleed_open_sides(img, 5)
    with pytest.raises(ValueError):
        T.bleed_open_sides(img, 1)


def test_bleed_copies_transparent_edges_as_transparent():
    img = T.blank_template()
    T.crop(img, "torso_f")[..., :] = (200, 0, 0, 255)
    T.crop(img, "torso_f")[:, :10, 3] = 0                                  # left strip transparent (a cut)
    out = T.finish_edges(img)
    x0, y0, x1, y1 = T.REGIONS["torso_f"]
    assert (out[y0:y1 + 1, x0 - 1, 3] == 0).all() and (out[y0:y1 + 1, x1 + 1, 3] == 255).all()


def test_ring_owner_map_is_unique_and_bounded():
    m3 = T.owner_map(3)
    assert m3[T.region_label_map() > 0].min() >= 1
    assert (m3 > 0).sum() > (T.region_label_map() > 0).sum()
    assert T.allowed_pixels(4).sum() > T.allowed_pixels(2).sum()


def test_png_facts_and_template_writer(tmp_path):
    img = T.blank_template()
    img[10, 10] = (1, 2, 3, 255)
    data = T.write_template(img)
    facts = T.inspect_png(data)
    assert facts.is_png and facts.is_rgba8 and (facts.width, facts.height) == (585, 559) and not facts.colour_chunks
    assert np.array_equal(T.decode_png_rgba8(data), img)
    with pytest.raises(ValueError):
        T.write_template(np.zeros((559, 584, 4), np.uint8))
    # a PNG with gAMA / iCCP / sRGB chunks is detected
    import struct
    import zlib

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return struct.pack(">I", len(payload)) + tag + payload + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)

    with_gama = data[:33] + chunk(b"gAMA", struct.pack(">I", 45455)) + chunk(b"sRGB", b"\x00") + data[33:]   # after IHDR (8 + 25 bytes)
    f2 = T.inspect_png(with_gama)
    assert f2.is_png and set(f2.colour_chunks) == {"gAMA", "sRGB"}
    assert not T.inspect_png(b"\xff\xd8\xff\xe0 JPEG").is_png and not T.inspect_png(b"").is_png


def test_row_bands_and_forbidden_rows():
    assert T.bands_of("torso_f") == ((74, 168), (172, 201))
    assert T.bands_of("rlimb_f") == ((355, 416), (421, 465), (469, 482))
    assert T.bands_of("torso_u") == ((8, 71),)
    assert T.band_of_rows("rlimb_f", 430, 440) == (421, 465)
    assert T.band_of_rows("rlimb_f", 410, 425) is None            # crosses the elbow/knee split
    assert T.band_of_rows("torso_f", 160, 175) is None            # crosses row 170
    assert T.TORSO_SPLIT_ROW == 170 and T.LIMB_SPLIT_ROWS == (418.5, 467) and T.SHOE_TOP_ROW_RANGE == (446, 465)


def test_template_json_copy_in_data_folder_is_identical():
    a = Path(T.__file__).with_name("template_regions.json")
    b = Path(T.__file__).resolve().parent.parent / "data" / "template_regions.json"
    assert a.read_bytes() == b.read_bytes()
    assert json.loads(a.read_text(encoding="utf-8"))["gap_px"] == 2


def test_strip_layout_and_wrap_order():
    off, length = T.strip_layout("torso")
    assert length == 384 and off == {"torso_r": 0.0, "torso_f": 64.0, "torso_l": 192.0, "torso_b": 256.0}
    off, length = T.strip_layout("rlimb")
    assert length == 256 and list(off) == ["rlimb_l", "rlimb_b", "rlimb_r", "rlimb_f"]


def _max_neighbour_jump(img: np.ndarray, bg: tuple[int, int, int, int]) -> int:
    solid = ~np.all(img == np.array(bg, dtype=np.uint8), axis=2)
    worst = 0
    for dy, dx in ((0, 1), (1, 0)):
        a = img[:img.shape[0] - dy, :img.shape[1] - dx, :3].astype(int)
        b = img[dy:, dx:, :3].astype(int)
        both = solid[:img.shape[0] - dy, :img.shape[1] - dx] & solid[dy:, dx:]
        if both.any():
            worst = max(worst, int(np.abs(a - b)[both].max()))
    return worst


def test_box_preview_of_the_position_texture_has_no_seam_on_any_visible_cube_edge():
    bg = (238, 238, 238, 255)
    tex = T.position_texture()
    for yaw, pitch in ((32.0, 18.0), (-35.0, 25.0), (150.0, 20.0), (-140.0, -20.0)):
        r = T.render_box_preview(texture=tex, yaw_deg=yaw, pitch_deg=pitch, only=["torso"], supersample=1, background=bg)
        assert _max_neighbour_jump(r, bg) <= 30, (yaw, pitch)
    # teeth: an UP face painted upside down (front edge at the wrong side) shows a jump on the cube edge
    bad = tex.copy()
    x0, y0, x1, y1 = T.REGIONS["torso_u"]
    bad[y0:y1 + 1, x0:x1 + 1] = bad[y0:y1 + 1, x0:x1 + 1][::-1]
    r = T.render_box_preview(texture=bad, yaw_deg=32.0, pitch_deg=18.0, only=["torso"], supersample=1, background=bg)
    assert _max_neighbour_jump(r, bg) > 60


def test_box_preview_default_figure_and_head_toggle():
    full = T.render_box_preview(size=(160, 200), supersample=1)
    torso = T.render_box_preview(size=(160, 200), supersample=1, only=["torso"])
    assert full.shape == torso.shape == (200, 160, 4) and (full != torso).any()
    again = T.render_box_preview(size=(160, 200), supersample=1)
    assert np.array_equal(full, again)                                    # pure


def test_flat_unfold_puts_the_characters_right_on_the_viewers_left_and_never_mirrors():
    tex = T.letter_texture()
    front = T.unfold_flat(tex, None, "front")
    back = T.unfold_flat(tex, None, "back")
    assert front.shape == (256, 256, 4)
    assert np.array_equal(front[0:128, 64:192], T.crop(tex, "torso_f"))
    assert np.array_equal(front[0:128, 0:64], T.crop(tex, "rlimb_f"))      # character's right arm: viewer's left in a front view
    assert np.array_equal(front[0:128, 192:256], T.crop(tex, "llimb_f"))
    assert np.array_equal(back[0:128, 64:192], T.crop(tex, "torso_b"))
    assert np.array_equal(back[0:128, 0:64], T.crop(tex, "llimb_b"))       # back view: the character's left is on the viewer's left
    assert np.array_equal(back[0:128, 192:256], T.crop(tex, "rlimb_b"))
    legs = T.unfold_flat(None, tex, "front")
    assert np.array_equal(legs[128:256, 64:128], T.crop(tex, "rlimb_f")) and np.array_equal(legs[128:256, 128:192], T.crop(tex, "llimb_f"))


def test_render_flat_preview_returns_two_png_tiles_with_skin_showing_through_transparent_clothing():
    shirt = T.blank_template()
    T.crop(shirt, "torso_f")[...] = (10, 200, 10, 255)
    pants = T.blank_template()
    T.crop(pants, "rlimb_f")[...] = (200, 10, 10, 255)
    f, b = T.render_flat_preview(shirt, pants, skin=(214, 170, 140), scale=1)
    ff, bb = T.decode_png_rgba8(f), T.decode_png_rgba8(b)
    assert ff.shape == bb.shape == (272, 272, 4)
    assert tuple(ff[8 + 10, 8 + 100, :3]) == (10, 200, 10)                  # shirt over the torso
    assert tuple(ff[8 + 10, 8 + 10, :3]) == (214, 170, 140)                 # bare arm = skin
    assert tuple(ff[8 + 200, 8 + 70, :3]) == (200, 10, 10)                  # pants leg on the viewer's left
    assert tuple(bb[8 + 10, 8 + 100, :3]) == (214, 170, 140)                # nothing authored on the back
    with pytest.raises(ValueError):
        T.render_flat_preview(np.zeros((10, 10, 4), np.uint8), None)

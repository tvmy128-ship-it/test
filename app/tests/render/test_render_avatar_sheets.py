"""Mannequin, dressing through the real UV regions, labels, and the fixed sheets (APP_SPEC 10.11, 10.8)."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
from PIL import Image
from scipy import ndimage

from duoskin.mesh import fixtures
from duoskin.mesh import primitives as prim
from duoskin.render import avatar as A
from duoskin.render import raster as R
from duoskin.render import sheets as S
from duoskin.roblox import limits

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


# ------------------------------------------------------------------------------------------------ mannequin
def test_mannequin_is_a_blocky_r15_5_2_studs_tall_and_4_wide():
    mq = A.default_mannequin()
    lo, hi = mq.bounds()
    assert np.allclose(lo, [-2.0, 0.0, -0.6]) and np.allclose(hi, [2.0, 5.2, 0.6])
    assert len(mq.parts) == 15 and {p.name for p in mq.parts} == set(A.OBJECT_IDS) - {"hair"}
    assert mq.head.size == (1.2, 1.2, 1.2)
    legs = sum(mq.part(f"Right{n}").size[1] for n in ("UpperLeg", "LowerLeg", "Foot"))
    assert abs(legs - 2.0) < 1e-9
    assert mq.part("RightUpperArm").centre[0] == -1.5 and mq.part("LeftUpperArm").centre[0] == 1.5          # the character's right is -X


def test_parts_do_not_overlap_each_other():
    mq = A.default_mannequin()
    for i, a in enumerate(mq.parts):
        for b in mq.parts[i + 1:]:
            overlap = np.minimum(a.hi, b.hi) - np.maximum(a.lo, b.lo)
            assert not np.all(overlap > 1e-6), (a.name, b.name)


def test_attachments_live_in_the_mannequin_not_in_the_code():
    mq = A.default_mannequin()
    assert abs(mq.attachment("HairAttachment")[1] - 5.193) < 1e-9 and np.allclose(mq.attachment("HatAttachment"), mq.attachment("HairAttachment"))
    assert mq.attachment("RightCollar")[0] < 0 < mq.attachment("LeftCollarAttachment")[0]
    assert mq.attachment("BodyFrontAttachment")[2] > 0 > mq.attachment("BodyBackAttachment")[2]
    with pytest.raises(KeyError):
        mq.attachment("RightWristAttachment")                                    # there is no wrist attachment
    custom = A.Mannequin.from_json({**mq.to_json(), "attachments": {**mq.to_json()["attachments"], "HairAttachment": [0, 6.0, 0]}})
    assert custom.attachment("HairAttachment")[1] == 6.0 and mq.attachment("HairAttachment")[1] != 6.0


def test_mannequin_json_round_trip_and_loader(tmp_path):
    mq = A.default_mannequin()
    p = tmp_path / "mannequin_blocky.json"
    p.write_text(json.dumps(mq.to_json()), encoding="utf-8")
    back = A.load_mannequin(p)
    assert back.to_json() == mq.to_json() and back.name == mq.name
    assert A.load_mannequin(tmp_path / "missing.json").name == "blocky_r15_code" and A.load_mannequin(None).name == "blocky_r15_code"


def test_every_attachment_the_limits_know_exists_on_the_mannequin():
    mq = A.default_mannequin()
    for t in limits.ASSET_TYPES:
        for att in limits.attachments_for(t):
            assert mq.attachment(att).shape == (3,), att


# ------------------------------------------------------------------------------------------------ dressing
def region_template(colours: dict[str, tuple[int, int, int]], regions: dict[str, tuple]) -> Image.Image:
    img = Image.new("RGBA", (585, 559), (0, 0, 0, 0))
    arr = np.asarray(img).copy()
    for name, rect in regions.items():
        x0, y0, x1, y1 = (round(v) for v in rect)
        arr[y0:y1, x0:x1] = colours[name] + (255,)
    return Image.fromarray(arr, "RGBA")


def unique_regions(layer: str) -> tuple[dict, dict]:
    """Every (part, face) rectangle of a layer painted in its own colour; returns the image regions and the colour table."""
    regions, colours, k = {}, {}, 0
    for part, faces in A.TEMPLATE_PART_FACES.items():
        if layer in A.PART_LAYERS[part]:
            for face, rect in faces.items():
                key = f"{part}:{face}"
                k += 1
                regions[key] = rect
                colours[key] = (20 + (k * 37) % 220, 20 + (k * 71) % 220, 20 + (k * 113) % 220)
    return regions, colours


def centre_colour(out: R.RenderOutput, mesh_name: str, meshes, face_axis: str) -> tuple[int, int, int]:
    ids = out.ids
    oid = A.OBJECT_IDS[mesh_name]
    ys, xs = np.nonzero(ids == oid)
    return tuple(int(c) for c in out.beauty[int(ys.mean()), int(xs.mean()), :3])


def test_templates_are_applied_through_the_real_uv_regions():
    s_regions, s_cols = unique_regions("shirt")
    shirt = region_template(s_cols, s_regions)
    meshes = A.dress(shirt=shirt, pants=None, skin_rgb=(226, 178, 140))
    for view, axis in (("front", "+Z"), ("back", "-Z"), ("left", "+X"), ("right", "-X")):
        cam = R.make_camera(view, (0, 2.6, 0), 100)
        out = R.render(meshes, cam, 440, 560, ss=1)
        for part in ("UpperTorso", "RightUpperArm", "LeftLowerArm", "RightHand", "LeftHand"):
            key = f"{part}:{axis}"
            if key not in s_cols:
                continue
            oid = A.OBJECT_IDS[part]
            vis = ndimage.binary_erosion(out.ids == oid, iterations=7)      # keep the face interior: edges blend with the 2 px template gaps
            if vis.sum() < 50:
                continue
            # the shirt colour of that exact face, shaded by at most the 0.70 ambient: compare after undoing the light band
            px = out.beauty[vis][:, :3].astype(float)
            expect = np.array(s_cols[key], float)
            ratio = px / expect
            assert ratio.min() > 0.65 and ratio.max() < 1.02 and np.ptp(ratio.mean(axis=1)) < 0.2 and np.abs(px.std(axis=0)).max() < 3, (view, part)


def test_shirt_front_and_back_labels_on_the_official_templates_read_correctly():
    shirt = Image.open(FIXTURES / "Template-Shirts-R15.png")
    pants = Image.open(FIXTURES / "Template-Pants-R15.png")
    meshes = A.dress(shirt=shirt, pants=pants)
    front = R.render(meshes, R.make_camera("front", (0, 2.6, 0), 120), 480, 700, ss=1)
    assert front.mask.sum() > 50000
    # the template's FRONT region is red, BACK blue: the torso must show the red face from the front and the blue one from behind
    t = front.ids == A.OBJECT_IDS["UpperTorso"]
    mean_front = front.beauty[t][:, :3].mean(axis=0)
    back = R.render(meshes, R.make_camera("back", (0, 2.6, 0), 120), 480, 700, ss=1)
    mean_back = back.beauty[back.ids == A.OBJECT_IDS["UpperTorso"]][:, :3].mean(axis=0)
    assert mean_front[0] > mean_front[2] and mean_back[2] > mean_back[0]


def test_layering_body_then_pants_then_shirt_on_the_torso_arms_shirt_only_legs_pants_only():
    _s_regions, _s_cols = unique_regions("shirt")
    _p_regions, _p_cols = unique_regions("pants")
    shirt_img = Image.new("RGBA", (585, 559), (0, 0, 0, 0))
    pants_img = Image.new("RGBA", (585, 559), (0, 0, 0, 0))
    sa, pa = np.asarray(shirt_img).copy(), np.asarray(pants_img).copy()
    sa[74:170, 231:359] = (200, 0, 0, 255)                         # shirt only on the upper-torso front rows 74..170
    pa[74:202, 231:359] = (0, 0, 200, 255)                         # pants cover the whole torso front incl. lower torso
    sa[355:483, 217:281] = (0, 200, 0, 255)                        # the limb FRONT column: right limb canvas, used by the right arm (shirt) ...
    pa[355:483, 217:281] = (250, 250, 0, 255)                      # ... and the right leg (pants) with another colour
    shirt, pants = Image.fromarray(sa, "RGBA"), Image.fromarray(pa, "RGBA")
    meshes = A.dress(shirt=shirt, pants=pants, skin_rgb=(100, 100, 100), body_rgb=(100, 100, 100))
    out = R.render(meshes, R.make_camera("front", (0, 2.6, 0), 100), 440, 560, ss=1)

    def mean(part):
        return out.beauty[out.ids == A.OBJECT_IDS[part]][:, :3].mean(axis=0)

    assert mean("UpperTorso")[0] > mean("UpperTorso")[2]           # shirt (red) over pants (blue) over body
    assert mean("LowerTorso")[2] > mean("LowerTorso")[0]           # lower torso: pants over body, shirt absent there
    assert mean("RightUpperArm")[1] > 130 and mean("RightUpperArm")[0] < 100          # arm: shirt only (green), no pants yellow
    leg = mean("RightUpperLeg")
    assert leg[0] > 150 and leg[1] > 150 and leg[2] < 100                              # leg: pants only (yellow), no shirt green


def test_labels_are_rendered_through_the_same_uvs():
    shirt = Image.new("RGBA", (585, 559), (0, 0, 0, 0))
    arr = np.asarray(shirt).copy()
    arr[74:202, 231:359] = (200, 0, 0, 255)
    shirt = Image.fromarray(arr, "RGBA")
    shirt_labels = np.full((559, 585), 9, np.uint16)                # compositor label 9 = e.g. "print"
    meshes = A.dress(shirt=shirt, shirt_labels=shirt_labels)
    out = R.render(meshes, R.make_camera("front", (0, 2.6, 0), 100), 440, 560, ss=1, labels=True)
    torso = out.ids == A.OBJECT_IDS["UpperTorso"]
    assert set(np.unique(out.labels[torso])) == {9}
    leg = out.ids == A.OBJECT_IDS["RightUpperLeg"]
    assert set(np.unique(out.labels[leg])) == {A.LABEL_SKIN}          # no pants: bare skin label
    default = A.dress(shirt=shirt)
    out2 = R.render(default, R.make_camera("front", (0, 2.6, 0), 100), 440, 560, ss=1, labels=True)
    assert set(np.unique(out2.labels[out2.ids == A.OBJECT_IDS["UpperTorso"]])) == {A.LABEL_SHIRT}


def test_modesty_layer_sits_under_the_clothes_and_over_the_skin():
    modesty = np.zeros((559, 585, 4), np.uint8)
    modesty[170:202, 231:359] = (30, 30, 30, 255)                   # lower torso front
    meshes = A.dress(modesty=Image.fromarray(modesty, "RGBA"), skin_rgb=(226, 178, 140))
    out = R.render(meshes, R.make_camera("front", (0, 2.6, 0), 100), 440, 560, ss=1, labels=True)
    low = out.ids == A.OBJECT_IDS["LowerTorso"]
    assert A.LABEL_MODESTY in set(np.unique(out.labels[low]))
    assert out.beauty[low][:, :3].mean() < 100


def test_head_overlay_goes_on_the_front_face_only():
    overlay = Image.new("RGBA", (128, 128), (0, 0, 0, 0))
    overlay.paste((20, 20, 220, 255), (40, 40, 90, 90))
    meshes = A.dress(head_overlay=overlay, skin_rgb=(226, 178, 140))
    front = R.render(meshes, R.make_camera("front", (0, 4.6, 0), 120), 300, 300, ss=1)
    back = R.render(meshes, R.make_camera("back", (0, 4.6, 0), 120), 300, 300, ss=1)
    hf = front.beauty[front.ids == A.OBJECT_IDS["Head"]][:, :3].astype(int)
    hb = back.beauty[back.ids == A.OBJECT_IDS["Head"]][:, :3].astype(int)
    assert (hf[:, 2] > hf[:, 0] + 50).sum() > 100 and (hb[:, 2] > hb[:, 0] + 50).sum() == 0


def test_wrong_template_size_is_a_clear_error():
    with pytest.raises(ValueError):
        A.dress(shirt=Image.new("RGBA", (100, 100)))


def test_object_ids_cover_body_hair_accessories_and_stickers():
    assert A.object_id("hair") == 40 and A.object_id("acc.0") == 50 and A.object_id("acc.3") == 53 and A.object_id("sticker.1") == 71
    assert len(set(A.OBJECT_IDS.values())) == len(A.OBJECT_IDS)
    with pytest.raises(KeyError):
        A.object_id("tail")


def test_place_mesh_puts_the_attachment_point_on_the_mannequins():
    mq = A.default_mannequin()
    ball = prim.build("sphere", {"diameter": 1.0}, (200, 60, 60))
    ball.meta["attachment_offset"] = [0.0, -0.5, 0.0]                 # the attachment point is at the sphere's bottom
    rm = A.place_mesh(mq, ball, "HatAttachment", name="acc.0")
    assert abs(rm.vertices[:, 1].min() - mq.attachment("HatAttachment")[1]) < 1e-6 and rm.object_id == 50


# ------------------------------------------------------------------------------------------------ sheets
def test_mesh_views_share_one_scale_and_the_judge_sheet_is_a_3x2_grid():
    ball = fixtures.dense_sphere(0.5, 24, 16)
    man = S.RenderManifest()
    views = S.render_mesh_views(ball, size=200, manifest=man)
    assert list(views) == list(S.JUDGE_VIEWS) and all(v.size == (200, 200) for v in views.values())
    S.assert_manifest(man, [f"mesh.{v}" for v in S.JUDGE_VIEWS], same_scale=True)
    sheet = S.mesh_judge_sheet(views)
    assert sheet.size == (3 * 200 + 4 * 8, 2 * 200 + 3 * 8)
    arr = np.asarray(views["front"])
    assert tuple(arr[2, 2]) == raster_grey()
    with pytest.raises(ValueError):
        S.mesh_judge_sheet({"front": views["front"]})


def raster_grey():
    return R.GREY_BG


def test_manifest_assertion_catches_missing_duplicate_and_unexpected_renders():
    man = S.RenderManifest()
    img = Image.new("RGB", (8, 8), (1, 2, 3))
    man.add("a", "front", img, 100)
    man.add("a", "front", img, 100)
    man.add("z", "back", img, 50)
    with pytest.raises(AssertionError) as e:
        S.assert_manifest(man, ["a", "b"], same_scale=True)
    msg = str(e.value)
    assert "missing renders: b" in msg and "duplicate renders: a" in msg and "unexpected renders: z" in msg and "different scales" in msg


def test_contact_sheet_and_duo_sheet_layouts():
    tiles = [Image.new("RGB", (100, 150), (i * 40, 0, 0)) for i in range(5)]
    sheet = S.contact_sheet(tiles, 3, gutter=10)
    assert sheet.size == (3 * 100 + 4 * 10, 2 * 150 + 3 * 10)
    a = {"front": Image.new("RGB", (80, 120), (255, 0, 0)), "back": Image.new("RGB", (80, 120), (200, 0, 0))}
    b = {"front": Image.new("RGB", (80, 120), (0, 0, 255)), "back": Image.new("RGB", (80, 120), (0, 0, 200))}
    duo = S.duo_sheet(a, b)
    assert duo.size == (4 * 80 + 5 * 12, 120 + 2 * 12)
    assert duo.getpixel((12 + 5, 12 + 5)) == (255, 0, 0) and duo.getpixel((12 + 3 * 92 + 5, 12 + 5)) == (0, 0, 200)
    with pytest.raises(ValueError):
        S.contact_sheet([], 2)


def test_phone_strip_is_downscaled_to_150_and_shown_2x_nearest():
    imgs = [Image.new("RGB", (600, 900), (10, 200, 10)), Image.new("RGB", (600, 900), (200, 10, 10))]
    strip = S.phone_strip(imgs)
    assert strip.height == (150 + 12) * 2 and strip.width == (100 * 2 + 18) * 2
    px = np.asarray(strip)
    assert len({tuple(p) for p in px[40, ::7]}) <= 4                  # nearest-neighbour: no blended colours
    assert S.phone_strip(imgs, target_h=75, scale=1).height == 75 + 12


def test_five_tone_and_face_pose_sheets():
    five = [Image.new("RGB", (60, 60), (i * 50, 100, 100)) for i in range(5)]
    assert S.five_tone_sheet(five).size == (5 * 60 + 6 * 8, 60 + 16)
    with pytest.raises(ValueError):
        S.five_tone_sheet(five[:4])
    assert S.face_pose_sheet(five[:4], cols=2).size == (2 * 60 + 24, 2 * 60 + 24)


def test_character_views_use_one_fixed_scale():
    meshes = A.dress()
    man = S.RenderManifest()
    views = S.render_character_views(meshes, ("front", "back"), manifest=man, ss=1)
    S.assert_manifest(man, ["char.front", "char.back"], same_scale=True)
    assert all(v.size == (480, 700) for v in views.values())
    f = np.asarray(views["front"]).astype(int)
    body = np.abs(f - np.array(S.SHEET_BG)).max(axis=2) > 10
    xs = np.nonzero(body.any(axis=0))[0]
    assert abs((xs.max() - xs.min() + 1) - 4 * 120) <= 2             # 4 studs wide at 120 px per stud


@pytest.mark.parametrize("asset_type,attachment", [("Hat", None), ("Hair", None), ("Face", "FaceFrontAttachment"), ("Shoulder", "LeftCollarAttachment"),
                                                   ("Front", None), ("Back", None), ("Waist", "WaistFrontAttachment"), ("Neck", None)])
def test_scale_render_draws_the_classic_box_with_its_offset_for_every_type(asset_type, attachment):
    mq = A.default_mannequin()
    img = S.scale_render(mq, asset_type, attachment, target_studs=(1.0, 1.0, 1.0), size=512)
    assert img.size == (512, 512)
    arr = np.asarray(img).astype(int)
    blue = (np.abs(arr - np.array([59, 130, 246])).max(axis=2) < 4)
    assert blue.sum() > 200                                           # the box outline
    red = (np.abs(arr - np.array([220, 38, 38])).max(axis=2) < 4)
    assert red.sum() > 10                                              # the attachment cross
    grey = (np.abs(arr - np.array(S.BODY_GREY)).max(axis=2) < 3)
    assert grey.sum() > 3000                                           # the character outline


def test_scale_render_box_and_attachment_positions_follow_the_limits():
    mq = A.default_mannequin()
    box = limits.box_for("Hair")
    img = S.scale_render(mq, "Hair", None, target_studs=(1, 1, 1), size=1024)
    arr = np.asarray(img).astype(int)
    blue = np.abs(arr - np.array([59, 130, 246])).max(axis=2) < 4
    ys, _xs = np.nonzero(blue)
    # measure the box height in pixels against the body: the box is 5 studs tall, the mannequin 5.2 studs
    _lo_m, _hi_m = mq.bounds()
    px = 1024 / max((box.size[0] if False else 4.0) + 1.6, 5.2 + 1.6)       # not needed exactly: compare the ratio instead
    del px
    grey = np.abs(arr - np.array(S.BODY_GREY)).max(axis=2) < 3
    gy = np.nonzero(grey.any(axis=1))[0]
    body_h = gy.max() - gy.min() + 1
    box_h = ys.max() - ys.min() + 1
    assert abs(box_h / body_h - 5.0 / 5.2) < 0.03
    # the Hair box is centred 0.5 stud BELOW the attachment: its centre is below the red cross
    red = np.abs(arr - np.array([220, 38, 38])).max(axis=2) < 4
    ry = np.nonzero(red)[0]
    assert (ys.min() + ys.max()) / 2 > ry.mean()


def test_scale_render_with_a_mesh_and_with_a_transparent_front_view():
    mq = A.default_mannequin()
    ball = prim.build("sphere", {"diameter": 1.0}, (230, 40, 40))
    from duoskin.mesh import repair as rep

    r = rep.repair_mesh(ball, rep.RepairOptions(asset_type="Hat", target_studs=(1, 1, 1), mannequin=mq))
    with_mesh = np.asarray(S.scale_render(mq, "Hat", None, target_studs=(1, 1, 1), mesh=r.mesh, size=512)).astype(int)
    assert ((with_mesh[..., 0] > 150) & (with_mesh[..., 1] < 100)).sum() > 500
    art = Image.new("RGBA", (200, 200), (0, 0, 0, 0))
    art.paste((40, 200, 40, 255), (50, 50, 150, 150))
    plan = np.asarray(S.scale_render(mq, "Hat", None, target_studs=(1.0, 1.0, 1.0), accessory_rgba=art, size=512)).astype(int)
    assert ((plan[..., 1] > 150) & (plan[..., 0] < 100) & (plan[..., 2] < 100)).sum() > 500
    side = S.scale_render(mq, "Shoulder", "RightCollarAttachment", target_studs=(1, 1, 1), mesh=r.mesh, view="left", size=512)
    assert side.size == (512, 512)


def test_part_face_rectangles_agree_with_the_clothing_tracks_template_regions():
    """The mannequin's UV rectangles must sit inside, and exactly tile, the template regions of roblox/template.py."""
    from duoskin.roblox import template as T

    face_to_region = {"+Z": "f", "-Z": "b", "+X": "l", "-X": "r", "+Y": "u", "-Y": "d"}
    covered: dict[tuple[str, str], list[tuple[float, float, float, float]]] = {}
    for part, faces in A.TEMPLATE_PART_FACES.items():
        group = "torso" if part.endswith("Torso") else ("rlimb" if part.startswith("Right") else "llimb")
        for face, rect in faces.items():
            region = f"{group}_{face_to_region[face]}"
            x0, y0, x1, y1 = T.REGIONS[region]
            assert rect[0] >= x0 and rect[2] <= x1 + 1 and rect[1] >= y0 and rect[3] <= y1 + 1, (part, face, rect, T.REGIONS[region])
            layer = "pants" if ("Leg" in part or "Foot" in part) else "shirt/torso"        # arms (Shirt) and legs (Pants) reuse the limb canvas
            covered.setdefault((region, layer), []).append(rect)
    for (region, _layer), rects in covered.items():
        x0, y0, x1, y1 = T.REGIONS[region]
        area = sum((r[2] - r[0]) * (r[3] - r[1]) for r in rects)
        # side faces of the three limb parts (and of the two torso parts) together cover the whole region
        if region.endswith(("_f", "_b", "_l", "_r")):
            assert abs(area - (x1 - x0 + 1) * (y1 - y0 + 1)) < 1e-6, region


def test_scale_render_composites_the_approved_hair_on_the_head_at_guide_scale():
    mq = A.default_mannequin()
    hair = Image.new("RGBA", (1024, 1536), (0, 0, 0, 0))
    hair.paste((90, 40, 160, 255), (512 - 190, 560 - 120, 512 + 190, 560 + 100))      # hair over the top of the head, guide scale
    plain = np.asarray(S.scale_render(mq, "Hat", None, target_studs=(1, 1, 1), size=1024)).astype(int)
    with_hair = np.asarray(S.scale_render(mq, "Hat", None, target_studs=(1, 1, 1), size=1024, hair_front_rgba=hair)).astype(int)
    purple = (np.abs(with_hair - np.array([90, 40, 160])).max(axis=2) < 3)
    assert purple.sum() > 2000 and not (np.abs(plain - np.array([90, 40, 160])).max(axis=2) < 3).any()
    ys, xs = np.nonzero(purple)
    grey = np.abs(plain - np.array(S.BODY_GREY)).max(axis=2) < 3
    gy, gx = np.nonzero(grey)
    top_of_head = gy.min()
    assert ys.min() < top_of_head and abs((xs.min() + xs.max()) / 2 - (gx.min() + gx.max()) / 2) < 8       # hair rises above the head, centred on it
    side = S.scale_render(mq, "Hat", None, target_studs=(1, 1, 1), view="left", size=256, hair_front_rgba=hair)
    assert side.size == (256, 256)


def test_head_guide_is_an_exportable_grey_cube_in_the_attachment_frame(tmp_path):
    from duoskin.mesh import export, load

    mq = A.default_mannequin()
    g = A.head_guide_meshdata(mq)
    assert np.allclose(g.extents, 1.2) and abs(g.vertices[:, 1].max() - (mq.head.hi[1] - mq.attachment("HairAttachment")[1])) < 1e-9
    back = load.load_mesh(export.write_glb(g, tmp_path / "head_guide.glb")).mesh
    assert back.n_tris == 12 and tuple(np.asarray(back.texture)[0, 0][:3]) == (154, 154, 154)

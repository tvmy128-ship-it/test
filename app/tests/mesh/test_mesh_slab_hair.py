"""Sticker slabs (ACC-17, ACC-18, CHK-M20) and hair.register / kit hair (APP_SPEC 10.7, CHK-M17, CHK-M21)."""
from __future__ import annotations

import numpy as np
import pytest
from PIL import Image, ImageDraw

from duoskin.checks.model import gate_verdict
from duoskin.mesh import fixtures, hair, slab
from duoskin.mesh import geometry as geo
from duoskin.mesh import repair as rep
from duoskin.mesh import texture as tx
from duoskin.mesh.types import MeshData, MeshError
from duoskin.render.avatar import default_mannequin


def badge(size=512) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse([40, 60, 470, 450], fill=(230, 60, 60, 255))
    d.ellipse([200, 200, 320, 320], fill=(0, 0, 0, 0))                    # a hole
    d.polygon([(100, 300), (160, 300), (100, 400)], fill=(0, 0, 255, 255))  # asymmetric mark
    return img


# ------------------------------------------------------------------------------------------------------- slab
@pytest.mark.parametrize("back", ["plain", "same_art"])
def test_slab_is_thin_watertight_bevelled_and_passes_chk_m20(back):
    r = slab.build_slab(badge(), size_studs=2.5, thickness=0.1, back=back)
    m = r.mesh
    w = geo.weld(m.vertices, m.faces)
    st = geo.topology_stats(w.faces)
    assert st["watertight"] and st["winding_consistent"] and geo.signed_volume(w.vertices, w.faces) > 0
    assert abs(m.extents[2] - 0.1) < 1e-6 and r.facts["bevel"] > 0 and r.facts["loops"] == 2
    assert m.n_tris <= 2800 and m.uv.min() >= 0 and m.uv.max() <= 1
    chk = slab.check_slab(m, m.meta["slab"])[0]
    assert chk.check_id == "CHK-M20" and chk.passed, chk.evidence
    assert m.texture.size == (1024, 1024) and tx.min_alpha(m.texture) == 255 and not tx.is_flat(m.texture)


@pytest.mark.parametrize("shape", ["diamond", "hexagon", "square", "triangle"])
def test_a_slab_with_sharp_corners_passes_the_file_gate_too(shape):
    """A diamond or a hexagon (corners rounded a little by the tracing, so each tip has a short edge) used to get an inset outline that crossed itself:
    the caps overlapped (CHK-M11 coplanar intersecting triangles) and came out reversed (CHK-M20 back island on the front). Found by running the whole
    app: two of the five badge shapes of the mock pictures failed, and the part waited behind a Tripo pack that could never be made."""
    from duoskin.mesh import validate

    img = Image.new("RGBA", (512, 512), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    pts = {"diamond": [(256, 60), (452, 256), (256, 452), (60, 256)],
           "hexagon": [(256, 50), (434, 153), (434, 359), (256, 462), (78, 359), (78, 153)],
           "square": [(70, 70), (440, 70), (440, 440), (70, 440)],
           "triangle": [(256, 60), (450, 440), (62, 440)]}[shape]
    d.polygon(pts, fill=(200, 80, 90, 255), outline=(40, 30, 50, 255), width=8)
    r = slab.build_slab(img, size_studs=1.0, thickness=0.1)
    m = r.mesh
    chk = slab.check_slab(m, m.meta["slab"])[0]
    assert chk.passed, chk.evidence
    w = geo.weld(m.vertices, m.faces)
    assert validate.coplanar_intersections(w.vertices, w.faces) == 0
    assert geo.topology_stats(w.faces)["watertight"] and geo.signed_volume(w.vertices, w.faces) > 0


def test_an_inset_whose_short_edge_is_turned_round_is_not_used():
    tip = np.array([[0.346, -0.002], [0.346, 0.004], [0.002, 0.346], [-0.004, 0.346], [-0.346, 0.002], [-0.346, -0.004], [-0.006, -0.345], [0.002, -0.346]])
    assert not slab._valid_inset([tip], [slab.inset_polygon(tip, 0.02)], 0.02), "a bevel wider than the tip's short edge is refused"
    assert slab._valid_inset([tip], [slab.inset_polygon(tip, 0.004)], 0.004), "a bevel narrower than it is fine"


def test_thickness_is_raised_to_the_validator_minimum():
    r = slab.build_slab(badge(), size_studs=2.0, thickness=0.03)
    assert abs(r.mesh.extents[2] - 0.08) < 1e-6 and any("raised" in m for m in r.messages)


def test_the_back_has_its_own_uv_island_and_unmirrored_art():
    r = slab.build_slab(badge(), size_studs=2.5, thickness=0.1, back="same_art")
    m = r.mesh
    n, _ = geo.face_normals_areas(m.vertices, m.faces)
    front, back = np.nonzero(n[:, 2] > 0.999)[0], np.nonzero(n[:, 2] < -0.999)[0]
    fu, bu = m.uv[m.faces[front]].reshape(-1, 2), m.uv[m.faces[back]].reshape(-1, 2)
    assert fu[:, 0].max() <= 0.5 + 1e-6 and bu[:, 0].min() >= 0.5 - 1e-6           # left half vs right half of the atlas
    # a vertex at the world +X side (left as seen from behind) maps to the LEFT of the back art (u small)
    bv = np.unique(m.faces[back])
    right = bv[np.argmax(m.vertices[bv, 0])]
    left = bv[np.argmin(m.vertices[bv, 0])]
    assert m.uv[right, 0] < m.uv[left, 0]


def test_mirrored_back_art_is_caught():
    r = slab.build_slab(badge(), size_studs=2.5, thickness=0.1, back="same_art")
    m = r.mesh.copy()
    n, _ = geo.face_normals_areas(m.vertices, m.faces)
    ids = np.unique(m.faces[np.nonzero(n[:, 2] < -0.999)[0]])
    m.uv[ids, 0] = (0.5 + 2.5 / 1024) + (1 - 2.5 / 1024) - m.uv[ids, 0]
    chk = slab.check_slab(m, m.meta["slab"])[0]
    assert not chk.passed and "un-mirrored" in chk.evidence


def test_shared_uv_island_and_thin_slabs_are_caught():
    r = slab.build_slab(badge(), size_studs=2.5, thickness=0.1, back="same_art")
    m = r.mesh.copy()
    n, _ = geo.face_normals_areas(m.vertices, m.faces)
    ids = np.unique(m.faces[np.nonzero(n[:, 2] < -0.999)[0]])
    m.uv[ids, 0] -= 0.5                                                    # back faces move into the front cell
    assert "no own island" in slab.check_slab(m, m.meta["slab"])[0].evidence
    thin = r.mesh.copy()
    thin.vertices = thin.vertices * np.array([1, 1, 0.3])                  # 0.03 stud
    assert not slab.check_slab(thin, thin.meta["slab"])[0].passed


def test_slab_is_scaled_down_to_respect_the_surface_area_limit():
    r = slab.build_slab(badge(), size_studs=9.0, thickness=0.1, max_area=66.0)
    assert r.facts["surface_area"] <= 66.0 and any("scaled down" in m for m in r.messages)
    assert geo.surface_area(r.mesh.vertices, r.mesh.faces) <= 70.0


def test_triangle_budget_is_respected_for_detailed_art():
    img = Image.new("RGBA", (512, 512), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    rng = np.random.default_rng(1)
    for _ in range(40):
        x, y = rng.integers(40, 440, 2)
        d.ellipse([x, y, x + 36, y + 36], fill=(200, 80, 80, 255))
    r = slab.build_slab(img, size_studs=3.0, tris_budget=1200)
    assert r.mesh.n_tris <= 1500                         # ~8 triangles per outline vertex, a little over budget is tolerated
    assert geo.topology_stats(geo.weld(r.mesh.vertices, r.mesh.faces).faces)["watertight"]


def test_empty_art_and_marching_squares_contours():
    with pytest.raises(MeshError):
        slab.build_slab(Image.new("RGBA", (64, 64), (0, 0, 0, 0)), size_studs=1.0)
    f = np.zeros((20, 20))
    f[5:15, 5:15] = 1
    loops = slab.marching_squares(np.pad(f, 1), 0.5)
    assert len(loops) == 1 and len(loops[0]) >= 8


def test_inset_polygon_shrinks_outer_and_grows_holes():
    outer = np.array([[0, 0], [2, 0], [2, 2], [0, 2]], float)            # CCW
    inner = outer[::-1] * 0.25 + 0.75                                      # CW hole inside
    assert abs(slab.inset_polygon(outer, 0.1)[:, 0].min() - 0.1) < 1e-9
    h = slab.inset_polygon(inner, 0.05)
    assert h[:, 0].min() < inner[:, 0].min()


# ------------------------------------------------------------------------------------------------------- hair
def test_head_cube_is_found_by_the_guide_colour():
    m = fixtures.hair_with_head_fixture(fused=True)
    cube = hair.find_head_cube(m, hair.HairRegisterOptions())
    assert cube is not None and abs(cube["width"] - 1.2) < 1e-6 and cube["grey_faces"] >= 6
    assert np.allclose(cube["lo"], [-0.6, -0.6, -0.6], atol=1e-6) and np.allclose(cube["hi"], [0.6, 0.6, 0.6], atol=1e-6)
    hairless = fixtures.f_fixture()
    assert hair.find_head_cube(hairless, hair.HairRegisterOptions()) is None


@pytest.mark.parametrize("fused", [True, False])
def test_register_removes_the_cube_aligns_to_the_head_and_passes_chk_m21(fused):
    m = fixtures.hair_with_head_fixture(fused=fused)
    m.vertices = m.vertices * 2.3 + np.array([3.0, -1.0, 5.0])         # Tripo returns an arbitrary scale and position
    res = hair.register_hair(m)
    assert res.facts["head_found"] and res.facts["mode"] == "fiducial" and res.facts["watertight_after_cut"]
    assert abs(res.facts["scale"] - 1.2 / (1.2 * 2.3)) < 1e-6
    chk = next(c for c in res.checks if c.check_id == "CHK-M21")
    assert chk.passed, chk.evidence
    mesh = res.mesh
    topo = rep.topology(mesh)
    assert topo["watertight"] and topo["shells"] == 1
    # the head cavity: no vertex of the hair lies strictly inside the (un-inflated) head box
    mq = default_mannequin()
    lo, hi = hair.head_frame(mq, "HairAttachment", 1.2)
    att = np.asarray(mesh.meta["attachment_offset"])
    v_att = mesh.vertices - att                                           # back to the HairAttachment frame
    inside = np.all((v_att > lo + 0.0199 - 0.02) & (v_att < hi - 1e-4), axis=1)
    assert not np.any(np.all((v_att > lo + 0.03) & (v_att < hi - 0.03), axis=1))
    del inside
    assert mesh.meta["hair_register"]["mode"] == "fiducial"


def test_register_keeps_the_hair_in_the_hair_box_after_alignment():
    m = fixtures.hair_with_head_fixture(fused=True)
    res = hair.register_hair(m)
    from duoskin.mesh.validate import box_facts

    att = np.asarray(res.mesh.meta["attachment_offset"])
    bf = box_facts(res.mesh.vertices - att, res.mesh.extents, "Hair", "HairAttachment")
    assert bf["vertices_outside_box"] == 0 and min(bf["box_margins"]) > 0


def test_guide_colour_left_in_the_texture_fails_chk_m21():
    m = fixtures.hair_with_head_fixture(fused=True)
    res = hair.register_hair(m)
    painted = res.mesh.copy()
    arr = np.asarray(painted.texture.convert("RGB")).copy()
    arr[:] = (154, 154, 154)                                               # every texel the hair uses is now guide grey
    arr[:10, :10] = (90, 50, 30)
    painted.texture = Image.fromarray(arr, "RGB")
    chk = hair.check_registered(painted, res.mesh.meta["hair_register"], mannequin=default_mannequin())[0]
    assert not chk.passed and "grey guide colour" in chk.evidence


def test_hair_that_covers_the_face_fails_chk_m21():
    m = fixtures.hair_with_head_fixture(fused=True, bangs=0.55)           # bangs reach down to the middle of the face
    res = hair.register_hair(m)
    chk = next(c for c in res.checks if c.check_id == "CHK-M21")
    assert not chk.passed and "front face is visible" in chk.evidence


def test_auto_switched_guide_colour_is_honoured():
    m = fixtures.hair_with_head_fixture(fused=True)
    m.texture = fixtures.hair_texture(256, hair_rgb=(230, 230, 235), guide=(60, 90, 160))     # white hair, blue guide head
    res = hair.register_hair(m, hair.HairRegisterOptions(guide_rgb=((60, 90, 160),)))
    assert res.facts["head_found"] and next(c for c in res.checks if c.check_id == "CHK-M21").passed


def test_without_a_cube_the_hair_is_placed_from_the_i4_mask_bbox_and_still_cut():
    solid = fixtures.hair_with_head_fixture(fused=False)
    # drop the head shell: a hair-only mesh (solid helmet that overlaps the head volume)
    helmet = fixtures.hair_with_head_fixture(fused=True)
    helmet.texture = fixtures.hair_texture(256)                            # no grey anywhere in the used texels
    helmet.uv[:] = 0.25
    mask = np.zeros((1536, 1024), bool)
    mask[480:900, 300:724] = True                                         # hair from above the head down past the chin (guide scale)
    lo, hi = hair.bbox_from_guide_mask(mask)
    assert lo[0] < 0 < hi[0] and hi[1] > 0.6 and lo[1] < -0.4
    res = hair.register_hair(helmet, hair.HairRegisterOptions(target_bbox=(lo, hi)))
    assert res.facts["mode"] == "bbox_hint" and res.facts["watertight_after_cut"]
    del solid
    with pytest.raises(MeshError) as e:
        hair.register_hair(helmet)
    assert e.value.code == "no_alignment"


def test_bbox_from_guide_mask_uses_the_guide_scale():
    mask = np.zeros((1536, 1024), bool)
    mask[560 - 280:560 + 100, 512 - 140:512 + 140] = True                 # 1 stud above the head top, 0.5 stud wide each side
    lo, hi = hair.bbox_from_guide_mask(mask)
    assert abs(lo[0] + 0.5) < 1e-6 and abs(hi[0] - 0.5) < 1e-6
    assert abs(hi[1] - (1.0 + 0.6)) < 1e-6                                # head centre is 0.6 below the head top
    with pytest.raises(MeshError):
        hair.bbox_from_guide_mask(np.zeros((10, 10), bool))


def test_hair_recolour_snaps_bands_to_the_palette_and_chk_m17():
    grey = np.zeros((90, 90, 3), np.uint8)
    grey[:, :30], grey[:, 30:60], grey[:, 60:] = 40, 128, 220
    pal = {"base": (120, 70, 40), "shadow": (70, 40, 25), "highlight": (190, 130, 90)}
    out = hair.recolour_bands(Image.fromarray(grey, "RGB"), pal)
    arr = np.asarray(out)
    assert tuple(arr[0, 0]) == (70, 40, 25) and tuple(arr[0, 45]) == (120, 70, 40) and tuple(arr[0, 80]) == (190, 130, 90)
    ok = hair.check_recolour(out, pal)
    assert ok.passed and ok.value < 0.5 and ok.check_id == "CHK-M17"
    off = Image.fromarray(np.full((8, 8, 3), (10, 200, 10), np.uint8), "RGB")
    assert not hair.check_recolour(off, pal).passed
    derived = hair.hair_palette({"base": (200, 50, 50)})
    assert derived["shadow"] != derived["base"] != derived["highlight"]


def test_kit_hair_fit_assembles_adjusts_and_recolours(tmp_path):
    from duoskin.mesh import export
    from duoskin.mesh import primitives as prim

    def kit_piece(path, centre, size, uv_off):
        part = prim.box_part(size, centre=centre)
        grey = np.zeros((64, 64, 3), np.uint8)
        grey[:, :32], grey[:, 32:] = 128, 40
        mesh = MeshData(part.vertices, part.faces, part.uv * 0.5 + uv_off, Image.fromarray(grey, "RGB"))
        export.write_glb(mesh, path)

    style, fringe = tmp_path / "style.glb", tmp_path / "fringe.glb"
    kit_piece(style, (0.0, -0.3, -0.3), (1.6, 1.0, 1.6), 0.0)
    kit_piece(fringe, (0.0, -0.35, 0.5), (1.2, 0.5, 0.3), 0.0)
    pal = {"base": (150, 40, 60)}
    fit = hair.fit_kit_hair(style_path=str(style), module_paths=[str(fringe)], palette=pal,
                            adjustments=[{"param": "fringe_length", "value": "more"}, {"param": "volume", "value": "more"},
                                         {"param": "clump_size", "value": "more"}])
    assert fit.facts["fused"] and fit.facts["unsupported_adjustments"] == [{"param": "clump_size", "value": "more"}]
    assert any("clump_size" in m for m in fit.messages)
    assert fit.checks[0].check_id == "CHK-M17" and fit.checks[0].passed
    assert rep.topology(fit.mesh)["watertight"]
    base_fit = hair.fit_kit_hair(style_path=str(style), module_paths=[str(fringe)], palette=pal, adjustments=[])
    assert fit.mesh.extents[0] > base_fit.mesh.extents[0]                 # 'more' volume widens the hair
    assert gate_verdict(fit.checks) == "pass"

"""roblox/limits.py, limits.json and roblox/mesh_validators.py (pure facts -> CheckResult)."""
from __future__ import annotations

import pytest

from duoskin.checks.model import gate_verdict
from duoskin.roblox import limits
from duoskin.roblox.mesh_validators import validate_accessory

GOOD = {
    "ext_required": [], "ext_used": [], "bad_uris": [], "bytes": 1000, "image_mimes": ["image/png"],
    "mesh_nodes": 1, "primitives": 1, "materials": 1, "uv_sets": 1, "uv_in_01": True,
    "tris": 3000, "watertight": True, "boundary_edges": 0, "nonmanifold_edges": 0, "winding_consistent": True, "zero_area_faces": 0,
    "normals_out": 1.0, "determinant": 1.0, "bbox_min_extent": 0.5, "thickness_p5": 0.3, "thin_area_frac": 0.0,
    "shells": 2, "closed_shells": 2,
    "tex_size": [1024, 1024], "tex_mode": "RGB", "tex_min_alpha": 255, "tex_std": 40.0, "alpha_mode": "OPAQUE",
    "has_color0": False, "emissive": False, "metallic_factor": 0.0, "mr_texture": False, "normal_texture": False,
    "bbox_studs": [1.4, 1.2, 1.1], "box_margins": [0.8, 0.9, 0.9], "vertices_outside_box": 0,
    "surface_area": 12.0, "coplanar_intersections": 10, "centre_offset": 0.0, "scale_min": 1.0,
    "view_coverage": {"front": 0.7, "back": 0.7, "left": 0.7, "right": 0.7, "top": 0.7, "bottom": 0.7}, "spike_shrink": 0.02,
}


def by_id(results):
    return {r.check_id: r for r in results}


def test_boxes_match_the_documented_classic_sizes():
    assert limits.box_for("Hat").size == (3, 4, 3)
    hair = limits.box_for("Hair")
    assert hair.size == (3, 5, 3.5) and hair.offset_file == (0, -0.5, -0.25) and hair.offset_roblox == (0, -0.5, 0.25)
    assert limits.box_for("Face").size == (3, 2, 2)
    assert limits.box_for("Shoulder", "RightCollarAttachment").size == (3, 3, 3)
    assert limits.box_for("Shoulder", "NeckAttachment").size == (7, 3, 3)
    assert limits.box_for("Front").size == (3, 3, 3)
    back = limits.box_for("Back")
    assert back.size == (10, 7, 4.5) and back.offset_file == (0, 0, -0.75)
    assert limits.box_for("Waist").size == (4, 3.5, 7) and limits.box_for("Waist").offset_file == (0, -0.25, 0)
    assert limits.box_for("Neck").size == (3, 3, 2)


def test_roblox_and_file_frame_offsets_differ_only_in_z():
    for t, atts in limits.load_limits()["types"].items():
        for name, a in atts["attachments"].items():
            r, f = a["offset_roblox"], a["offset_file"]
            assert (r[0], r[1]) == (f[0], f[1]), (t, name)
            assert r[2] == -f[2] or (r[2] == 0 and f[2] == 0), (t, name)


def test_no_wrist_attachment_and_unknown_attachment_rejected():
    assert limits.load_limits()["no_wrist_attachment"] is True
    with pytest.raises(KeyError):
        limits.box_for("Hat", "RightWristAttachment")
    with pytest.raises(KeyError):
        limits.box_for("Bracelet")
    with pytest.raises(KeyError):
        limits.box_for("Hat", "FaceFrontAttachment")


def test_attachment_names_are_normalised():
    assert limits.normalise_attachment("RightCollar") == "RightCollarAttachment"
    assert limits.normalise_attachment("rightcollarattachment") == "RightCollarAttachment"
    assert limits.box_for("Shoulder", "LeftShoulder").attachment == "LeftShoulderAttachment"


def test_box_corners_follow_the_attachment_and_offset():
    lo, hi = limits.box_for("Hair").lo_hi((0, 5.0, 0))
    assert list(lo) == [-1.5, 2.0, -2.0] and list(hi) == [1.5, 7.0, 1.5]   # 1.5 in front, 2 behind, 2 up, 3 down


def test_thresholds_come_from_the_registry_and_extras():
    assert limits.threshold("mesh.tris_max") == 3800
    assert limits.threshold("mesh.surface_area_warn") == 60.0       # local extra
    assert limits.threshold("hair.guide_texel_share_max") == 0.005
    with pytest.raises(KeyError):
        limits.threshold("mesh.nope")
    assert "mesh.tris_max" in limits.describe("mesh.tris_max", "<=")


def test_a_good_accessory_passes_every_hard_check():
    res = validate_accessory(GOOD, "Shoulder", "RightCollarAttachment")
    assert gate_verdict(res) == "pass"
    ids = by_id(res)
    for cid in ("CHK-M01", "CHK-M02", "CHK-M03", "CHK-M04", "CHK-M05", "CHK-M06", "CHK-M07", "CHK-M09", "CHK-M10", "CHK-M11", "CHK-M12"):
        assert ids[cid].passed and ids[cid].ran and ids[cid].kind == "hard", cid


@pytest.mark.parametrize("patch,check", [
    ({"ext_required": ["EXT_meshopt_compression"]}, "CHK-M01"),
    ({"bad_uris": ["C:/tmp/a.png"]}, "CHK-M01"),
    ({"image_mimes": ["image/jpeg"]}, "CHK-M01"),
    ({"bytes": 60 * 1024 * 1024}, "CHK-M01"),
    ({"materials": 2}, "CHK-M02"),
    ({"primitives": 3}, "CHK-M02"),
    ({"uv_in_01": False}, "CHK-M02"),
    ({"tris": 3801}, "CHK-M03"),
    ({"watertight": False, "boundary_edges": 4}, "CHK-M04"),
    ({"nonmanifold_edges": 1}, "CHK-M04"),
    ({"normals_out": 0.5}, "CHK-M04"),
    ({"zero_area_faces": 2}, "CHK-M04"),
    ({"bbox_min_extent": 0.01}, "CHK-M04"),
    ({"shells": 11}, "CHK-M05"),
    ({"tex_size": [4096, 4096]}, "CHK-M06"),
    ({"tex_min_alpha": 254}, "CHK-M06"),
    ({"alpha_mode": "BLEND"}, "CHK-M06"),
    ({"tex_std": 1.0}, "CHK-M06"),
    ({"has_color0": True}, "CHK-M07"),
    ({"emissive": True}, "CHK-M07"),
    ({"metallic_factor": 1.0}, "CHK-M07"),
    ({"mr_texture": True}, "CHK-M07"),
    ({"box_margins": [-0.2, 0.5, 0.5], "vertices_outside_box": 3}, "CHK-M09"),
    ({"bbox_studs": [3.2, 1, 1]}, "CHK-M09"),
    ({"surface_area": 71.0}, "CHK-M10"),
    ({"coplanar_intersections": 500}, "CHK-M11"),
    ({"centre_offset": 1.5}, "CHK-M11"),
    ({"view_coverage": {"front": 0.2, "left": 0.7}}, "CHK-M12"),
    ({"spike_shrink": 0.3}, "CHK-M12"),
])
def test_each_failure_is_caught_by_its_check(patch, check):
    res = by_id(validate_accessory({**GOOD, **patch}, "Shoulder", "RightCollarAttachment"))
    assert res[check].ran and not res[check].passed, check
    assert gate_verdict(list(res.values())) == "fail"


def test_warning_bands_are_soft_siblings():
    res = by_id(validate_accessory({**GOOD, "shells": 9, "surface_area": 65.0, "tex_size": [2048, 2048],
                                    "view_coverage": {"front": 0.4, "left": 0.9}}, "Shoulder", "RightCollarAttachment"))
    assert res["CHK-M05"].passed and not res["CHK-M05W"].passed and res["CHK-M05W"].kind == "soft"
    assert res["CHK-M10"].passed and not res["CHK-M10W"].passed
    assert res["CHK-M06"].passed and not res["CHK-M06W"].passed
    assert res["CHK-M12"].passed and not res["CHK-M12W"].passed
    assert gate_verdict(list(res.values())) == "pass"


def test_hair_target_is_a_soft_check_but_3800_is_hard():
    soft = by_id(validate_accessory({**GOOD, "tris": 3700}, "Hair", "HairAttachment"))
    assert soft["CHK-M03"].passed and not soft["CHK-M03T"].passed and soft["CHK-M03T"].kind == "soft"
    assert by_id(validate_accessory({**GOOD, "tris": 3700}, "Hat", "HatAttachment"))["CHK-M03T"].passed


def test_missing_facts_fail_closed():
    facts = dict(GOOD)
    del facts["tris"]
    del facts["watertight"]
    res = by_id(validate_accessory(facts, "Hat", "HatAttachment"))
    assert not res["CHK-M03"].ran and not res["CHK-M03"].passed
    assert not res["CHK-M04"].ran and not res["CHK-M04"].passed
    assert gate_verdict(list(res.values())) == "fail"


def test_wrong_scale_type_and_unknown_attachment_fail_the_box_check():
    assert not by_id(validate_accessory(GOOD, "Hat", "HatAttachment", scale="ProportionsNormal"))["CHK-M09"].passed
    r = by_id(validate_accessory(GOOD, "Hat", "RightWristAttachment"))["CHK-M09"]
    assert r.ran and not r.passed and "cannot use attachment" in r.evidence


def test_box_check_uses_the_off_centre_box_through_margins():
    # the same facts pass for Back (10 x 7 x 4.5) but fail the Handle-size test for Hat (3 x 4 x 3)
    facts = {**GOOD, "bbox_studs": [6.0, 5.0, 3.0], "box_margins": [1.0, 0.5, 0.5]}
    assert by_id(validate_accessory(facts, "Back", "BodyBackAttachment"))["CHK-M09"].passed
    assert not by_id(validate_accessory(facts, "Hat", "HatAttachment"))["CHK-M09"].passed

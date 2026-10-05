"""Hair and accessory specs as Roblox items (asset type, attachment, size in studs, budgets)."""
from __future__ import annotations

import pytest
from pfix import locked_spec

from duoskin.pipeline import itemspec
from duoskin.roblox import limits


def test_accessory_item_reads_the_spec_words_as_roblox_numbers():
    spec = locked_spec()
    a = itemspec.accessory_item(spec, "a.acc.0")
    assert (a.asset_type, a.attachment, a.build) == ("Waist", "WaistFrontAttachment", "tripo")
    b = itemspec.accessory_item(spec, "b.acc.0")
    assert (b.asset_type, b.attachment, b.kind) == ("Shoulder", "RightCollarAttachment", "plush_pet")
    assert a.tris_target == 3800 and a.face_limit == 3000


@pytest.mark.parametrize("size,frac", [("small", 0.40), ("medium", 0.62), ("large", 0.85)])
def test_the_target_is_a_cube_inside_the_classic_box(size, frac):
    spec = locked_spec()
    spec["b"]["accessories"][0]["size_class"] = size
    it = itemspec.accessory_item(spec, "b.acc.0")
    box = limits.box_for(it.asset_type, it.attachment)
    assert it.target_studs[0] == pytest.approx(frac * min(box.size), abs=0.01)
    assert all(t <= m for t, m in zip(it.target_studs, box.size, strict=True))


def test_small_props_get_the_small_texture():
    spec = locked_spec()
    assert itemspec.accessory_item(spec, "a.acc.0").texture_px == 512
    spec["a"]["accessories"][0]["size_class"] = "large"
    assert itemspec.accessory_item(spec, "a.acc.0").texture_px == 1024


def test_hair_item_is_the_hair_box_at_the_hair_attachment():
    h = itemspec.hair_item(locked_spec(), "a")
    assert h.is_hair and h.asset_type == "Hair" and h.attachment == "HairAttachment"
    assert h.tris_target == 3600 and h.face_limit == 3500
    assert itemspec.item_of(locked_spec(), "a.hair") == h


@pytest.mark.parametrize("name,asset,expected", [
    ("right_collar", "Shoulder", "RightCollarAttachment"), ("hat", "Hat", "HatAttachment"), ("face_front", "Face", "FaceFrontAttachment"),
    ("waist_back", "Waist", "WaistBackAttachment"), ("right_collar", "Hat", "HatAttachment")])      # a name the type does not allow falls back to the default
def test_attachment_names(name, asset, expected):
    assert itemspec.roblox_attachment(name, asset) == expected

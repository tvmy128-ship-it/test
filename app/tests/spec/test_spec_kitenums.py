"""models/kitenums.py: the kit inventory, the demo defaults (empty hair kit) and the kit enums."""
from __future__ import annotations

import json

import pytest
import specfix

from duoskin.models import kitenums as ke


def test_demo_defaults_have_an_empty_hair_kit_and_the_sentinels():
    inv = ke.builtin_inventory()
    assert inv.ids("HairKit") == ("hair_custom",) and inv.flags.hair_kit_empty
    assert inv.ids("FringeKit") == ("kit_default", "none") and inv.ids("BackKit") == ("kit_default",)
    assert "none" in inv.ids("InnerTopKit") and "tee" in inv.ids("InnerTopKit")
    assert inv.ids("SkinToneKit") == ("tone_1", "tone_2", "tone_3", "tone_4", "tone_5")
    assert inv.ids("EyeShapeKit") == ("narrow", "round", "sleepy")
    assert "smirk_side" in inv.ids("MouthKit")
    assert not inv.flags.makeup_available


def test_every_kind_is_sorted_and_the_builtin_phrases_are_complete():
    inv = ke.builtin_inventory()
    for kind in ke.KIT_KINDS:
        ids = inv.ids(kind)
        assert list(ids) == sorted(ids) and len(set(ids)) == len(ids)
    assert inv.kit_phrase_problems() == []
    assert inv.recipe("crop_top").requires_bottom == {"waist": ("high",)}
    assert inv.recipe("hoodie").cut["sleeve"] == ("long",)
    assert inv.recipe("tee").family == inv.recipe("tee_long").family == "tee"


def test_tops_and_bottoms_are_split_by_template():
    inv = ke.builtin_inventory()
    assert set(inv.ids("TopRecipeKit")).isdisjoint(inv.ids("BottomRecipeKit"))
    assert "skirt_pleated" in inv.ids("BottomRecipeKit") and "hoodie" in inv.ids("TopRecipeKit")


def test_manifest_merges_over_the_defaults():
    inv = specfix.demo_inventory()
    assert "hair_bob_03" in inv.ids("HairKit") and "hair_custom" in inv.ids("HairKit")
    assert not inv.flags.hair_kit_empty
    assert inv.hair_style("hair_bob_03").clump_k == 5 and inv.hair_style("hair_custom") is None
    assert "fringe_a" in inv.ids("FringeKit") and "back_a" in inv.ids("BackKit")
    assert inv.hair_iou("hair_spiky_05", "hair_short_crop_01") == pytest.approx(0.9)
    assert inv.hair_iou("hair_short_crop_01", "hair_spiky_05") == pytest.approx(0.9)       # symmetric key
    assert inv.hair_iou("hair_bob_03", "hair_buns_04") is None
    assert ke.builtin_inventory().ids("HairKit") == ("hair_custom",)                       # the base is never mutated


def test_a_bad_parting_is_rejected():
    with pytest.raises(ke.KitError):
        ke.inventory_from_manifest({"hair": {"h": {"prompt_phrase": "x", "parting": "sideways"}}})


def test_manifest_flags_and_recipes_and_fabrics():
    inv = ke.inventory_from_manifest({
        "recipes": {"parka": {"template": "shirt", "family": "jacket", "prompt_phrase": "padded parka", "cut": {"sleeve": ["long"]}}},
        "fabrics": {"felt_x": {"material": "knit", "prompt_phrase": "soft felt"}},
        "flags": {"head_base_present": True, "makeup": "available"}})
    assert "parka" in inv.ids("TopRecipeKit") and inv.recipe("parka").cut == {"sleeve": ("long",)}
    assert inv.fabric("felt_x").pattern_phrase == "plain weave"
    assert inv.flags.head_base_present and inv.flags.makeup_available


def test_manifest_sha_changes_with_the_kits_and_is_stable():
    base = ke.builtin_inventory()
    assert base.sha256 == ke.builtin_inventory().sha256 == ke.kit_manifest_sha(base)
    assert specfix.demo_inventory().sha256 != base.sha256


def test_the_annotation_validates_against_the_current_inventory():
    from pydantic import BaseModel, ValidationError

    class M(BaseModel):
        h: ke.HairKit
        s: ke.ShoeKit

    assert M(h=" HAIR_BOB_03 ", s="boot").h == "hair_bob_03"
    with ke.use_inventory(ke.builtin_inventory()), pytest.raises(ValidationError):
        M(h="hair_bob_03", s="boot")
    with pytest.raises(ValidationError):
        M(h="hair_bob_03", s="clog")


def test_kit_literal_and_enum_values():
    lit = ke.kit_literal("ShoeKit")
    assert "mary_jane" in lit.__args__
    vals = ke.enum_values()
    assert set(vals) == set(ke.KIT_KINDS) and vals["HairKit"][0] == "hair_bob_03"
    assert ke.validate_ids([("ShoeKit", "boot"), ("ShoeKit", "clog")]) == ["ShoeKit: unknown id 'clog'"]


def test_manifest_view_is_canonical_json_with_flags():
    view = specfix.demo_inventory().to_manifest_view()
    json.dumps(view, sort_keys=True)
    assert view["availability"]["makeup"] == "unavailable" and view["availability"]["hair_kit_empty"] is False
    assert any("crop_top requires bottom.waist" in c for c in view["compatibility"])
    assert view["hair"]["hair_bob_03"]["clump_k"] == 5


def test_load_inventory_reads_a_manifest_file(tmp_path):
    p = tmp_path / "manifest.json"
    p.write_text(json.dumps(specfix.DEMO_MANIFEST), encoding="utf-8")
    assert "hair_bob_03" in ke.load_inventory(p).ids("HairKit")
    assert ke.load_inventory(tmp_path / "missing.json").ids("HairKit") == ("hair_custom",)


def test_set_default_inventory_and_reset():
    inv = specfix.demo_inventory()
    ke.set_default_inventory(inv)
    try:
        # the autouse fixture's use_inventory still wins inside the test; leave it and check the default via a fresh context
        import contextvars

        ctx = contextvars.Context()
        assert "hair_bob_03" in ctx.run(lambda: ke.current_inventory().ids("HairKit"))
    finally:
        ke.set_default_inventory(None)

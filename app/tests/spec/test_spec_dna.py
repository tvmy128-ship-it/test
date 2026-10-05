"""models/dna.py: the DNA card is a view of the spec (APP_SPEC §3.2, §6.4)."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
import specfix

from duoskin.models import dna
from duoskin.models.spec import DuoSpec


def rec(spec: DuoSpec, version=1, spec_id="spc_1"):
    return SimpleNamespace(id=spec_id, version=version, spec=spec.model_dump(mode="json"))


def test_card_is_a_view_of_the_spec():
    s = specfix.load_spec("spec_complement_gb")
    card = dna.card_from_spec(rec(s, 3), locked=True, source="concept_extracted")
    assert card.spec_id == "spc_1" and card.version == 3 and card.locked and card.source == "concept_extracted"
    assert card.world == s.world and card.a == s.a.dna and card.b == s.b.dna
    assert card.hair_kit == {"a": "hair_bob_03", "b": "hair_spiky_05"}
    assert card.palette_hexes["p1"] == "#2F6B4F" and card.palette_family == "earthy_natural"
    assert [a.kind for a in card.anchors] == ["colour", "motif"]
    json.dumps(card.model_dump(mode="json"))


def test_card_from_a_bare_spec_and_a_dict_record():
    s = specfix.load_spec("spec_twins_bg")
    assert dna.card_from_spec(s).spec_id == ""
    assert dna.card_from_spec(SimpleNamespace(id="x", version=2, spec=s.model_dump(mode="json"))).version == 2
    with pytest.raises(TypeError):
        dna.card_from_spec(object())


def test_diff_from_previous_lists_changed_paths():
    s = specfix.load_spec("spec_complement_gb")
    c1 = dna.card_from_spec(rec(s, 1))
    d = s.model_dump(mode="json")
    d["a"]["dna"]["shape_language"] = "sharp_angular"
    d["palette"][0]["hex"] = "#2F6B50"
    d["world"]["detail_level"] = "maximal"
    d["b"]["hair"]["kit_style_id"] = "hair_wavy_06"
    s2 = DuoSpec.model_validate(d)
    c2 = dna.card_from_spec(rec(s2, 2), previous=c1)
    assert c2.diff_from_previous == ["/a/dna/shape_language", "/b/hair/kit_style_id", "/palette/p1", "/world/detail_level"]
    assert dna.card_from_spec(rec(s, 2), previous=c1).diff_from_previous == []


def test_character_field_differences_counts_the_six_fields():
    s = specfix.load_spec("spec_complement_gb")
    card = dna.card_from_spec(s)
    assert dna.character_field_differences(card) == ["shape_language", "colour_plan", "focal_location", "hair.kit_style_id", "motif_object",
                                                      "accessory_style"]


def test_text_fields_compare_normalised():
    d = specfix.load_dict("spec_complement_gb")
    d["b"]["dna"]["motif_object"] = "  MUSHROOM! "
    card = dna.card_from_spec(DuoSpec.model_validate(d))
    assert "motif_object" not in dna.character_field_differences(card)


def test_two_hair_custom_hairs_count_only_with_the_declared_contrast():
    card = dna.card_from_spec(specfix.load_spec("spec_empty_bb"))
    assert "hair.kit_style_id" not in dna.character_field_differences(card)
    assert "hair.kit_style_id" in dna.character_field_differences(card, hair_custom_contrast=True)


def test_one_hair_custom_and_one_kit_style_differ():
    d = specfix.load_dict("spec_complement_gb")
    d["a"]["hair"] = {**d["a"]["hair"], "kit_style_id": "hair_custom", "fringe_id": "none"}
    card = dna.card_from_spec(DuoSpec.model_validate(d))
    assert "hair.kit_style_id" in dna.character_field_differences(card)


def test_recent_cards_json_is_compact_and_has_no_hexes_or_story():
    cards = [dna.card_from_spec(specfix.load_spec(n)) for n in ("spec_complement_gb", "spec_twins_bg")]
    out = dna.recent_cards_json(cards, 1)
    assert len(out) == 1 and out[0]["pair_structure"] == "seasonal_twins"
    text = json.dumps(out)
    assert "#" not in text and "story" not in text and "palette_hexes" not in text

"""prompts/registry.py: every template loads with a version and passes the static rules; SCHEMAS.lock guards the LLM-facing schemas."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from duoskin.models import kitenums
from duoskin.prompts import registry

PROMPT_DIR = Path(registry.__file__).resolve().parent
EXPECTED_IMAGE_IDS = {
    "I0.finalize", "I1.concept_char", "I1e.concept_edit", "I1f.concept_front", "I1b.concept_back", "I1j.concept_joint",
    "I1p.concept_partner_style", "I2.print", "I2.print_frame", "I2.shoe_decal", "I2e.print_edit", "I3.face_part", "I3.face_part_incanvas",
    "I3e.face_part_edit", "I4.hair_front", "I4e.hair_edit", "I4k.hair_kit_first", "I5.accessory_front", "I5.accessory_frame",
    "I5e.accessory_edit", "I5g.accessory_guided", "I6.badge_art", "I6.badge_frame", "I6e.badge_edit", "I7.fabric", "I8.shading_panel",
    "I10.side_view", "I11.repair", "R1.face_part", "R2.print", "T2.edit_view"}
EXPECTED_LLM_IDS = {"L1.reference_analyst", "L2.taste_builder", "L3.planner", "L4.critic", "L5.pairwise_ranker", "L6.reviser",
                    "L7.change_interpreter", "L9.hair_kit_matcher", "L10.repair_writer", "L11.asset_checker", "L12.duo_judge", "L13.ip_screen",
                    "L14.reference_similarity", "L15.concept_inventory", "G1.second_opinion"}


def test_the_shipped_template_set_is_complete():
    ids = set(registry.template_ids())
    assert EXPECTED_IMAGE_IDS | EXPECTED_LLM_IDS == ids, ids ^ (EXPECTED_IMAGE_IDS | EXPECTED_LLM_IDS)
    assert set(registry.template_ids("llm")) == EXPECTED_LLM_IDS
    assert set(registry.template_ids("image")) | set(registry.template_ids("text")) == EXPECTED_IMAGE_IDS


def test_the_shipped_set_has_no_static_problems():
    assert registry.validate_all() == []


def test_file_name_equals_id_and_every_template_has_a_positive_integer_version():
    for path in sorted(PROMPT_DIR.glob("*.md")):
        t = registry.all_templates()[path.stem]
        assert t.id == path.stem and isinstance(t.version, int) and t.version >= 1
    assert registry.prompt_versions() == {i: t.version for i, t in sorted(registry.all_templates().items())}


def test_every_image_template_declares_its_slots_flags_and_images():
    for tid in registry.template_ids("image"):
        t = registry.get(tid)
        assert registry.static_problems(t) == [], tid
        assert 1 <= t.meta.must_lines <= 5 or t.meta.kind == "text", tid


def test_a_template_without_a_version_refuses_to_load(tmp_path):
    p = tmp_path / "X1.demo.md"
    p.write_text("---\nid: X1.demo\nkind: image\nprovider: openai\n---\nPURPOSE: x\n", encoding="utf-8")
    with pytest.raises(registry.TemplateError, match="version"):
        registry._read_template(p)


def test_a_template_whose_id_differs_from_its_file_name_refuses_to_load(tmp_path):
    p = tmp_path / "X2.demo.md"
    p.write_text("---\nid: X9.other\nversion: 1\nkind: image\nprovider: openai\n---\nPURPOSE: x\n", encoding="utf-8")
    with pytest.raises(registry.TemplateError, match="file name"):
        registry._read_template(p)


def test_front_matter_with_unknown_keys_or_no_front_matter_is_rejected(tmp_path):
    p = tmp_path / "X3.demo.md"
    p.write_text("---\nid: X3.demo\nversion: 1\nkind: image\nprovider: openai\nsurprise: 1\n---\nPURPOSE: x\n", encoding="utf-8")
    with pytest.raises(registry.TemplateError):
        registry._read_template(p)
    q = tmp_path / "X4.demo.md"
    q.write_text("PURPOSE: x\n", encoding="utf-8")
    with pytest.raises(registry.TemplateError, match="front matter"):
        registry._read_template(q)


def test_unknown_template_id_is_an_error():
    with pytest.raises(registry.TemplateError):
        registry.get("I99.nothing")


def test_static_problems_catch_undeclared_slots_and_sections_out_of_order(tmp_path):
    p = tmp_path / "X5.demo.md"
    p.write_text("---\nid: X5.demo\nversion: 1\nkind: image\nprovider: openai\nimage1_role: none\n---\n"
                 "SUBJECT: a {thing}\nPURPOSE: x\nMUST:\n1. one\n", encoding="utf-8")
    t = registry._read_template(p)
    problems = registry.static_problems(t)
    assert any("not declared" in x for x in problems) and any("out of order" in x for x in problems)


def test_priming_words_are_listed_per_template():
    table = registry.priming_table()
    assert "I5.accessory_front" in table and "hand" in table["I5.accessory_front"]


def test_iter_images_honours_flags_and_the_bootstrap_variant():
    t = registry.get("I3.face_part")
    full = [r.id for r, _ in registry.iter_images(t, {"bootstrap": False})]
    boot = [r.id for r, _ in registry.iter_images(t, {"bootstrap": True})]
    assert full[0] == "guide_face_part" and set(boot) <= set(full) and len(boot) <= len(full)


# ---------------------------------------------------------------------------------------------- SCHEMAS.lock
def test_the_schema_lock_matches_the_code():
    assert registry.check_lock() == []


def test_the_lock_hashes_every_llm_facing_class_and_nothing_else():
    locked = registry.read_lock()
    assert set(locked) == set(registry.schema_hashes())
    assert {"DuoSpec", "PlanSet", "AssetCheck", "Revision", "ChangePlan", "Critique"} <= set(locked)
    assert all(re.fullmatch(r"[0-9a-f]{64}", v) for v in locked.values())


def test_adding_a_hair_style_does_not_change_the_lock():
    before = registry.schema_hashes()
    bigger = kitenums.inventory_from_manifest({"hair": {"hair_extra_99": {"prompt_phrase": "a new style", "length_class": "short",
                                                                          "silhouette_class": "crop", "clump_k": 5, "parting": "none",
                                                                          "default_fringe": "none", "supported_adjustments": ["volume"]}}})
    with kitenums.use_inventory(bigger):
        assert registry.schema_hashes() == before


def test_a_schema_change_shows_up_as_a_lock_difference(tmp_path):
    lock = tmp_path / "SCHEMAS.lock"
    registry.write_lock(lock)
    assert registry.check_lock(lock) == []
    doc = json.loads(lock.read_text(encoding="utf-8"))
    doc["schemas"]["DuoSpec"] = "0" * 64
    doc["schemas"]["Ghost"] = "1" * 64
    del doc["schemas"]["PlanSet"]
    lock.write_text(json.dumps(doc), encoding="utf-8")
    diff = registry.check_lock(lock)
    assert any(d.startswith("DuoSpec: schema changed") for d in diff)
    assert "Ghost: in the lock but not in the code" in diff and "PlanSet: not in the lock" in diff


def test_a_missing_lock_fails_closed(tmp_path):
    assert registry.check_lock(tmp_path / "nothing.lock") == ["nothing.lock is missing"]


def test_the_lock_file_is_deterministic():
    a = registry.schema_hashes()
    b = registry.schema_hashes()
    assert a == b
    text = (PROMPT_DIR / "SCHEMAS.lock").read_text(encoding="utf-8")
    assert json.loads(text)["bible_version"] == registry.BIBLE_VERSION and text.endswith("\n")


def test_schemas_have_no_unions_defaults_or_dicts_for_the_provider_subset():
    """APP_SPEC §6.2: no Optional, no unions, no defaults, no free dict in any LLM-facing schema."""
    from duoskin.models import llm_io, spec

    classes = [*llm_io.SCHEMA_CLASSES.values(), spec.DuoSpec, spec.PlanSet]
    for cls in classes:
        with kitenums.schema_mode("placeholder"):
            schema = cls.model_json_schema()
        text = json.dumps(schema)
        assert '"anyOf"' not in text and '"oneOf"' not in text, cls.__name__
        assert '"default"' not in text, cls.__name__
        assert '"additionalProperties": true' not in text and '"patternProperties"' not in text, cls.__name__


def test_an_optional_llm_input_may_not_be_a_required_slot(tmp_path):
    p = tmp_path / "L99.demo.md"
    p.write_text("---\nid: L99.demo\nversion: 1\nkind: llm\nprovider: anthropic\nschema: Revision\nslots:\n  note: {source: x}\n"
                 "inputs:\n  note: {kind: str}\n---\n<role name=\"demo\">\nx\n</role>\n=== USER ===\n<note>{note}</note>\n", encoding="utf-8")
    problems = registry.static_problems(registry._read_template(p))
    assert any("optional input 'note' is a required slot" in x for x in problems)
    p.write_text(p.read_text(encoding="utf-8").replace("{note}", "{note?}"), encoding="utf-8")
    assert registry.static_problems(registry._read_template(p)) == []

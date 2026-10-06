"""The LLM-facing schemas (APP_SPEC §6.1, §6.2, bible §2.8 and §3.1): no Optional, no unions, no defaults, no dict, descriptions on every
field, lower-cased enums, the schema lock, and the Appendix D fixture."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from duoskin.models import kitenums, llm_io, spec
from duoskin.models import spec_rules as SR
from duoskin.prompts import registry

ROOT = Path(__file__).resolve().parents[3]


def _schema_walk(node, path=""):
    """Yield (path, schema node) for every nested schema object."""
    if isinstance(node, dict):
        yield path, node
        for k, v in node.items():
            yield from _schema_walk(v, f"{path}/{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _schema_walk(v, f"{path}/{i}")


ALL_CLASSES = {**llm_io.SCHEMA_CLASSES, **{n: getattr(spec, n) for n in ("DuoSpec", "PlanSet", "Character", "Face", "Hair", "Top", "Bottom", "Accessory")}}


@pytest.mark.parametrize("name", sorted(ALL_CLASSES))
def test_no_union_no_optional_no_default_no_dict(name):
    cls = ALL_CLASSES[name]
    sch = cls.model_json_schema()
    text = json.dumps(sch)
    assert "anyOf" not in text and "oneOf" not in text and "allOf" not in text, "unions are not allowed (bible §2.8)"
    assert '"default"' not in text, "defaults are not allowed"
    assert "additionalProperties" in text
    for p, node in _schema_walk(sch):
        if node.get("type") == "object" and "properties" in node:
            assert node.get("additionalProperties") is False, f"{name}{p}: additionalProperties must be false"
            assert set(node.get("required", [])) == set(node["properties"]), f"{name}{p}: every field is required"
        if node.get("type") == "object" and "properties" not in node and "$ref" not in node and p.count("/") > 0 and node.get("additionalProperties") not in (False, None):
            pytest.fail(f"{name}{p}: a free-form dict")


@pytest.mark.parametrize("name", sorted(ALL_CLASSES))
def test_every_field_has_a_description(name):
    cls = ALL_CLASSES[name]
    missing = [f for f, info in cls.model_fields.items() if not (info.description or "").strip()]
    assert not missing, f"{name}: fields without Field(description=...): {missing}"


def test_enums_are_lowercase_snake_case_and_input_is_lowercased():
    sch = spec.DuoSpec.model_json_schema()
    for _, node in _schema_walk(sch):
        for v in node.get("enum", []):
            if isinstance(v, str):
                assert v == v.lower() and " " not in v and "-" not in v, v
    d = json.loads((Path(__file__).parent / "fixtures" / "spec_complement_gb.json").read_text(encoding="utf-8"))
    d["combo"] = " GB "
    d["palette"][0]["role"] = "A_MAIN"
    d["a"]["face"]["iris_style"] = "Oval_Two_Step"
    d["a"]["hair"]["kit_style_id"] = "HAIR_BOB_03"
    s = spec.DuoSpec.model_validate(d)
    assert s.combo == "gb" and s.palette[0].role == "a_main" and s.a.hair.kit_style_id == "hair_bob_03"


def test_schema_size_is_modest():
    assert len(json.dumps(spec.PlanSet.model_json_schema())) < 60_000     # the bible quotes about 17 KB for PlanSet plus our descriptions


def test_plan_set_has_brief_constraints_and_no_other_changes():
    assert set(spec.PlanSet.model_fields) == {"specs", "brief_constraints", "how_they_differ"}
    assert set(spec.BriefConstraint.model_fields) == {"text", "spec_paths"}
    assert "parting" in spec.Hair.model_fields              # bible v1.2


def test_revision_op_and_change_op_are_separate_classes():
    assert set(llm_io.RevisionOp.model_fields) == {"op", "path", "value_json", "finding"}
    assert set(llm_io.ChangeOp.model_fields) == {"op", "path", "value_json", "reason"}
    assert llm_io.RevisionOp is not llm_io.ChangeOp


def test_schema_lock_matches_code():
    assert registry.check_lock() == []


def test_schema_lock_detects_drift_and_a_missing_lock(tmp_path):
    lock = tmp_path / "SCHEMAS.lock"
    assert registry.check_lock(lock) == [f"{lock.name} is missing"]       # fails closed
    registry.write_lock(lock)
    assert registry.check_lock(lock) == []
    doc = json.loads(lock.read_text(encoding="utf-8"))
    doc["schemas"]["Critique"] = "0" * 64
    del doc["schemas"]["Revision"]
    lock.write_text(json.dumps(doc), encoding="utf-8")
    problems = registry.check_lock(lock)
    assert any("Critique" in p and "changed" in p for p in problems)
    assert any("Revision" in p and "not in the lock" in p for p in problems)


def test_lock_ignores_the_kit_enum_values():
    before = registry.schema_hashes()
    inv = kitenums.inventory_from_manifest({"hair": {"hair_extra_99": {"prompt_phrase": "a new style"}}})
    with kitenums.use_inventory(inv):
        after = registry.schema_hashes()
    assert before == after


def test_schema_hash_changes_when_a_field_changes():
    from pydantic import BaseModel, Field

    class A(BaseModel):
        x: str = Field(description="one")

    class B(BaseModel):
        x: str = Field(description="two")

    assert registry.schema_hash(A) != registry.schema_hash(B)


def test_live_schema_lists_the_current_kit_ids_sorted():
    sch = spec.Hair.model_json_schema()
    ids = sch["properties"]["kit_style_id"]["enum"]
    assert ids == sorted(ids) and "hair_custom" in ids and "hair_bob_03" in ids
    with kitenums.schema_mode("string"):
        assert "enum" not in spec.Hair.model_json_schema()["properties"]["kit_style_id"]
    with kitenums.schema_mode("placeholder"):
        assert spec.Hair.model_json_schema()["properties"]["kit_style_id"]["x-kit"] == "HairKit"


def test_appendix_d_fixture_validates_and_matches_the_doc():
    doc = ROOT / "docs" / "PROMPT_BIBLE.md"
    fixture = json.loads((Path(__file__).parent / "fixtures" / "spec_bg_min.json").read_text(encoding="utf-8"))
    s = spec.DuoSpec.model_validate(fixture)
    assert s.world.material_family == "nylon" and s.a.hair.parting == "left"
    if doc.exists():
        import re

        m = re.search(r"## Appendix D.*?```json\n(.*?)\n```", doc.read_text(encoding="utf-8"), re.DOTALL)
        assert json.loads(m.group(1)) == fixture


def test_strict_models_reject_unknown_fields_and_bad_enum_values():
    d = json.loads((Path(__file__).parent / "fixtures" / "spec_complement_gb.json").read_text(encoding="utf-8"))
    d["surprise"] = 1
    with pytest.raises(ValidationError):
        spec.DuoSpec.model_validate(d)
    d.pop("surprise")
    d["a"]["face"]["eye_shape"] = "huge"
    with pytest.raises(ValidationError) as ei:
        spec.DuoSpec.model_validate(d)
    assert "unknown EyeShapeKit id" in str(ei.value) and "narrow" in str(ei.value)


def test_spec_rules_run_inside_the_model_and_can_be_skipped():
    d = json.loads((Path(__file__).parent / "fixtures" / "spec_complement_gb.json").read_text(encoding="utf-8"))
    d["palette"][1]["hex"] = "cream"
    with pytest.raises(ValidationError) as ei:
        spec.DuoSpec.model_validate(d)
    probs = SR.problems_from_error(ei.value)
    assert [p.code for p in probs] == ["palette_hex"] and probs[0].path == "/palette/1/hex"
    s = spec.DuoSpec.model_validate(d, context={"skip_rules": True})
    assert SR.spec_problems(s)[0].code == "palette_hex"
    assert spec.parse_spec(json.dumps(d), rules=False).palette[1].hex == "cream"

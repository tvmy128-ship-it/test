"""models/llm_io.py: the role schemas and the Gate B rule library (bible §7.2, §9, §10.5, §16, §17)."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from duoskin.checks import policy
from duoskin.models import llm_io as L

DOCS = Path(__file__).resolve().parents[3] / "docs"


def test_route_table_covers_every_role_and_is_a_class_each():
    assert set(L.ROLE_SCHEMAS) == {"L1", "L2", "L3", "L4", "L5", "L6", "L7", "L9", "L10", "L11", "L12a", "L12b", "L13", "L14", "L15"}
    for cls in L.ROLE_SCHEMAS.values():
        json.dumps(cls.model_json_schema())


def test_rule_library_has_the_65_rules_of_the_bible():
    lib = L.rule_library()
    assert len(lib) == len(L.ALL_RULE_IDS) == 65 and list(L.ALL_RULE_IDS) == sorted(L.ALL_RULE_IDS)
    assert lib["ip_no_brand"].hard and not lib["cn_restraint"].hard
    assert lib["fp_shape_word"].slots == ("grammar phrase", "part")


def test_rule_ids_and_hard_flags_match_the_bible_table():
    doc = DOCS / "PROMPT_BIBLE.md"
    if not doc.exists():
        pytest.skip("docs/ not present")
    text = doc.read_text(encoding="utf-8")
    start = text.index("### 7.2 Gate B")
    end = text.index("Code checks that the returned rule-ID set", start)
    rows = re.findall(r"^\| ([a-z0-9_]+) \| \"(.+?)\"", text[start:end], re.MULTILINE)
    ids = [r[0] for r in rows]
    assert sorted(ids) == list(L.ALL_RULE_IDS)
    for rid, stmt in rows:
        assert L.rule_library()[rid].statement == stmt


def test_hard_and_soft_flags_agree_with_the_policy_registry():
    wrong = []
    for rid, r in L.rule_library().items():
        if not policy.is_registered(rid):
            wrong.append(f"{rid}: not registered")
            continue
        kind = policy.meta(rid).kind
        if r.hard and kind == "soft":
            wrong.append(f"{rid}: hard in the bible, soft in checks.json")
        if not r.hard and kind != "soft":
            wrong.append(f"{rid}: soft in the bible, {kind} in checks.json")
    assert not wrong, wrong


def test_verdict_rule_id_is_one_closed_enum_and_input_is_lowercased():
    sch = L.AssetCheck.model_json_schema()
    enum = next(n["enum"] for n in _walk(sch) if "enum" in n and "ip_no_brand" in n["enum"])
    assert enum == list(L.ALL_RULE_IDS)
    v = L.Verdict.model_validate({"rule_id": "IP_NO_TEXT", "observation": "none", "verdict": "PASS", "location": "TOP"})
    assert (v.rule_id, v.verdict, v.location) == ("ip_no_text", "pass", "top")
    with pytest.raises(ValidationError):
        L.Verdict.model_validate({"rule_id": "not_a_rule", "observation": "", "verdict": "pass", "location": "none"})


def _walk(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def test_evidence_fields_come_before_the_verdict_fields():
    order = {
        L.Verdict: ("observation", "verdict"), L.IpItem: ("observation", "verdict"), L.Score: ("evidence", "level"),
        L.CritCompare: ("evidence", "better"), L.DuoCompare: ("evidence", "better"), L.DuoLevel: ("evidence", "level"),
        L.SimAspect: ("evidence", "level"),
    }
    for cls, (ev, verdict) in order.items():
        fields = list(cls.model_fields)
        assert fields.index(ev) < fields.index(verdict), cls.__name__


def test_criteria_lists_match_the_bible():
    assert L.RANKING_ONLY_CRITERIA == ("structure_readable", "accessory_pair_expresses")
    assert set(L.CRITERIA) >= set(L.RANKING_ONLY_CRITERIA) and len(L.CRITERIA) == 11


def test_hair_match_uses_the_kit_enums():
    ok = {"observations": ["long"], "choice": "c1", "fringe_id": "fringe_a", "back_id": "kit_default", "adjustments": [], "mismatch_notes": []}
    assert L.HairMatch.model_validate(ok).fringe_id == "fringe_a"
    with pytest.raises(ValidationError):
        L.HairMatch.model_validate({**ok, "fringe_id": "fringe_zzz"})


def test_change_plan_round_trip():
    d = {"understood_as": "teal jacket", "needs_clarification": "", "duo_contract_risks": [],
         "patch": [{"op": "replace", "path": "/b/top/base_ref", "value_json": "\"p12\"", "reason": "the user wants teal"}],
         "redo_parts": [{"part_id": "b.shirt", "reason": "colour"}],
         "image_fixes": [{"part_id": "b.shirt", "fix_sentence": "Make the jacket teal.", "scope": "global_edit", "region_hint": "none", "keep": ["print"]}]}
    assert L.ChangePlan.model_validate(d).patch[0].reason == "the user wants teal"
    with pytest.raises(ValidationError):
        L.ChangePlan.model_validate({**d, "patch": [{**d["patch"][0], "finding": "1"}]})        # a ChangeOp has no finding


def test_patch_ops_of_the_spec_record_have_the_same_shape_as_the_llm_schemas():
    """APP_SPEC §6.4: SpecRecord stores the patches the reviser and the change interpreter return (same fields, same enums)."""
    from duoskin.models import spec_record

    def shape(cls):
        sch = cls.model_json_schema()
        return {k: (v.get("type"), tuple(v.get("enum", ()))) for k, v in sch["properties"].items()}, tuple(sch["required"])

    assert shape(spec_record.RevisionOp) == shape(L.RevisionOp)
    assert shape(spec_record.ChangeOp) == shape(L.ChangeOp)
    op = L.RevisionOp(op="replace", path="/a/top/base_ref", value_json='"p3"', finding="1")
    assert spec_record.RevisionOp.model_validate(op.model_dump()).finding == "1"

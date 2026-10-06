"""``pipeline/change.py`` (pure layer): patches (scope, validation), diffs, the Gate 1 routing of image fixes (I1e vs I1) and CHK-G1-12."""
from __future__ import annotations

import copy

import pytest
import specfix

from duoskin.models.llm_io import ChangePlan
from duoskin.pipeline import change as CH

pytestmark = pytest.mark.usefixtures("demo_inv")


@pytest.fixture
def spec():
    return specfix.load_dict("spec_complement_gb")


def op(path, value=None, kind="replace", why="because"):
    import json

    return {"op": kind, "path": path, "value_json": "" if kind == "remove" else json.dumps(value), "reason": why}


def rev_op(path, value=None, kind="replace", finding="1"):
    import json

    return {"op": kind, "path": path, "value_json": "" if kind == "remove" else json.dumps(value), "finding": finding}


# ---------------------------------------------------------------------------------------------------- the patch itself
def test_a_change_may_change_a_palette_hex_and_the_result_is_a_valid_spec(spec):
    out = CH.apply_patch(spec, [op("/palette/0/hex", "#1E9696")], kind="change", strict=True, subject_sha="abc")
    assert out.ok and out.spec["palette"][0]["hex"] == "#1E9696" and spec["palette"][0]["hex"] != "#1E9696"
    assert out.check.check_id == "CHK-G0-11" and out.check.kind == "assert" and out.check.passed
    assert out.changed_paths == ["/palette/0/hex"]


@pytest.mark.parametrize("path,value", [("/combo", "bb"), ("/is_wildcard", True), ("/a/presentation", "boy"), ("/b/presentation", "girl")])
def test_a_change_may_not_touch_the_fixed_fields(spec, path, value):
    out = CH.apply_patch(spec, [op(path, value)], kind="change", strict=True)
    assert not out.ok and out.spec is None and not out.check.passed
    assert "may not be changed" in out.reason


def test_a_palette_id_is_never_patched(spec):
    out = CH.apply_patch(spec, [op("/palette/0/id", "p77")], kind="change", strict=True)
    assert not out.ok and "palette ids stay stable" in out.reason


def test_the_reviser_may_only_touch_the_paths_its_findings_name(spec):
    ok = CH.apply_patch(spec, [rev_op("/a/top/base_ref", "p2")], kind="reviser", finding_paths=["/a/top/base_ref"])
    assert ok.ok and ok.spec["a"]["top"]["base_ref"] == "p2"
    child = CH.apply_patch(spec, [rev_op("/a/face/lash_ref", "p2")], kind="reviser", finding_paths=["/a/face"])
    assert child.ok, "a child of a finding path is in scope"
    bad = CH.apply_patch(spec, [rev_op("/b/hair/description", "short hair")], kind="reviser", finding_paths=["/a/top/base_ref"])
    assert not bad.ok and not bad.check.passed and "not named by a finding" in bad.reason


def test_the_reviser_scope_needs_the_finding_paths(spec):
    with pytest.raises(ValueError):
        CH.apply_patch(spec, [rev_op("/a/top/base_ref", "p2")], kind="reviser")


def test_a_patch_that_would_break_the_spec_rules_is_rejected_when_strict(spec):
    bad_hex = CH.apply_patch(spec, [op("/palette/0/hex", "teal")], kind="change", strict=True)
    assert not bad_hex.ok and bad_hex.problems
    dangling = CH.apply_patch(spec, [op("/a/top/base_ref", "p99")], kind="change", strict=True)
    assert not dangling.ok
    lenient = CH.apply_patch(spec, [op("/a/top/base_ref", "p99")], kind="change", strict=False)
    assert lenient.ok, "the plan loop validates leniently and lets the linter report what is left"


def test_add_and_remove_work_on_lists_and_the_original_is_never_changed(spec):
    before = copy.deepcopy(spec)
    removed = CH.apply_patch(spec, [op("/a/accessories/0", kind="remove")], kind="change", strict=False)
    assert removed.ok and len(removed.spec["a"]["accessories"]) == len(spec["a"]["accessories"]) - 1
    acc = copy.deepcopy(spec["b"]["accessories"][0])
    added = CH.apply_patch(spec, [op("/b/accessories/-", acc, kind="add")], kind="change", strict=False)
    assert added.ok and len(added.spec["b"]["accessories"]) == len(spec["b"]["accessories"]) + 1
    assert spec == before


def test_a_malformed_or_unresolvable_op_is_rejected_with_a_reason(spec):
    assert not CH.apply_patch(spec, [{"op": "move", "path": "/a", "value_json": "", "reason": "x"}], kind="change").ok
    missing = CH.apply_patch(spec, [op("/a/no/such/field", 1)], kind="change")
    assert not missing.ok and "op 0" in missing.reason


def test_parse_value_reads_json_text_and_reports_bad_json(spec):
    ops = CH.ops_of([op("/a/top/base_ref", "p2")], kind="change")
    assert CH.parse_value(ops[0]) == "p2"
    broken = CH.ops_of([{"op": "replace", "path": "/a/top/base_ref", "value_json": "{not json", "reason": "x"}], kind="change")
    with pytest.raises(CH.PatchError):
        CH.parse_value(broken[0])


def test_the_envelope_patch_follows_the_same_scope_rule():
    env = {"brief_constraints": [{"text": "a bow", "spec_paths": ["/a/nowhere"]}], "how_they_differ": "x"}
    ok = CH.apply_envelope_patch(env, [rev_op("/brief_constraints/0/spec_paths", ["/a/accessories/0"])], ["/brief_constraints/0"])
    assert ok.ok and ok.spec["brief_constraints"][0]["spec_paths"] == ["/a/accessories/0"] and env["brief_constraints"][0]["spec_paths"] == ["/a/nowhere"]
    bad = CH.apply_envelope_patch(env, [rev_op("/how_they_differ", "changed")], ["/brief_constraints/0"])
    assert not bad.ok


# ---------------------------------------------------------------------------------------------------- diffs
def test_diff_specs_lists_changed_pointers_with_old_and_new_values(spec):
    new = CH.apply_patch(spec, [op("/palette/0/hex", "#1E9696"), op("/a/hair/description", "a short tidy bob")], kind="change").spec
    rows = {d["path"]: d for d in CH.diff_specs(spec, new)}
    assert rows["/palette/0/hex"]["new"] == "#1E9696" and rows["/palette/0/hex"]["old"] == spec["palette"][0]["hex"]
    assert rows["/a/hair/description"]["new"] == "a short tidy bob"


def test_characters_changed_follows_palette_references(spec):
    pid = spec["a"]["top"]["base_ref"]
    idx = next(i for i, c in enumerate(spec["palette"]) if c["id"] == pid)
    new = copy.deepcopy(spec)
    new["palette"][idx]["hex"] = "#123456"
    changed = CH.characters_changed(spec, new)
    assert "a" in changed
    only_b = copy.deepcopy(spec)
    only_b["b"]["hair"]["description"] = "a tall spiky crop"
    assert CH.characters_changed(spec, only_b) == ["b"]
    assert CH.character_of_part("a.shirt") == "a" and CH.character_of_part("b.acc.0") == "b" and CH.character_of_part("duo") is None


# ---------------------------------------------------------------------------------------------------- Gate 1 routing
def plan_with(*fixes):
    return {"image_fixes": [{"part_id": p, "fix_sentence": s, "scope": sc, "region_hint": "whole", "keep": ["its shape"]} for p, s, sc in fixes]}


def only_refs(node, acc=None):
    acc = set() if acc is None else acc
    if isinstance(node, dict):
        for v in node.values():
            only_refs(v, acc)
    elif isinstance(node, list):
        for v in node:
            only_refs(v, acc)
    elif isinstance(node, str):
        acc.add(node)
    return acc


def changed_spec(spec, who="b"):
    """The spec with one palette colour recoloured: a colour that only ``who`` uses, so only that character is touched."""
    new = copy.deepcopy(spec)
    other = "a" if who == "b" else "b"
    ids = [c["id"] for c in new["palette"]]
    mine = [i for i in ids if i in only_refs(new[who]) and i not in only_refs(new[other]) and i not in only_refs(new["shared_anchors"])]
    assert mine, "the fixture has a colour only this character uses"
    for c in new["palette"]:
        if c["id"] == mine[0]:
            c["hex"] = "#1E9696"
    assert CH.characters_changed(spec, new) == [who]
    return new


def test_an_edit_scope_routes_to_i1e_for_the_affected_character_only(spec):
    new = changed_spec(spec, "b")
    fixes = CH.route_concept_fixes(plan_with(("b.shirt", "Make the jacket teal, keeping its shape.", "global_edit")), spec, new, target="both")
    assert [(f.character, f.route) for f in fixes] == [("b", "i1e")]
    assert fixes[0].fix_sentence.startswith("Make the jacket teal") and fixes[0].keep == ("its shape",)


def test_a_regenerate_scope_routes_to_i1(spec):
    fixes = CH.route_concept_fixes(plan_with(("a.hair", "Draw the hair again as a tall crop.", "regenerate")), spec, spec, target="both")
    assert [(f.character, f.route) for f in fixes] == [("a", "i1")]


def test_a_changed_design_with_no_fix_gets_a_code_sentence_or_a_redraw(spec):
    new = changed_spec(spec, "a")
    fixes = CH.route_concept_fixes({"image_fixes": []}, spec, new, target="both")
    assert fixes and all(f.character == "a" for f in fixes)
    assert fixes[0].route == "i1e" and len(fixes[0].fix_sentence.split()) <= CH.FIX_WORDS_MAX and "Change every" in fixes[0].fix_sentence
    odd = copy.deepcopy(spec)
    odd["a"]["accessories"][0]["size_class"] = "large"
    redraw = CH.route_concept_fixes({"image_fixes": []}, spec, odd, target="both")
    assert [(f.character, f.route) for f in redraw] == [("a", "i1")], "no safe edit sentence: draw the character again"


def test_an_unsafe_fix_sentence_falls_back_to_i1(spec):
    new = changed_spec(spec, "b")
    fixes = CH.route_concept_fixes(plan_with(("b.shirt", "Print the word SALE on it in 3 colours", "global_edit")), spec, new, target="both")
    assert [(f.character, f.route) for f in fixes] == [("b", "i1")]


def test_the_target_limits_the_characters(spec):
    new = changed_spec(spec, "b")
    plan = plan_with(("b.shirt", "Make the jacket teal.", "global_edit"))
    assert CH.route_concept_fixes(plan, spec, new, target="a") == []
    assert [f.character for f in CH.route_concept_fixes(plan, spec, new, target="b")] == ["b"]


def test_chk_g1_12_passes_for_i1e_on_the_chosen_draft_and_fails_otherwise(spec):
    new = changed_spec(spec, "b")
    plan = plan_with(("b.shirt", "Make the jacket teal.", "global_edit"))
    fixes = CH.route_concept_fixes(plan, spec, new, target="both")
    chosen = {"a": "sha_a", "b": "sha_b"}
    good = CH.check_gate1_change_route(fixes, plan, {"b": {"mode": "i1e", "base_sha": "sha_b"}}, chosen)
    assert good.check_id == "CHK-G1-12" and good.kind == "assert" and good.passed
    wrong_mode = CH.check_gate1_change_route(fixes, plan, {"b": {"mode": "i1", "base_sha": ""}}, chosen)
    assert not wrong_mode.passed and "expected I1e" in wrong_mode.evidence
    wrong_base = CH.check_gate1_change_route(fixes, plan, {"b": {"mode": "i1e", "base_sha": "other"}}, chosen)
    assert not wrong_base.passed and "not the current chosen draft" in wrong_base.evidence
    touched_other = CH.check_gate1_change_route(fixes, plan, {"b": {"mode": "i1e", "base_sha": "sha_b"}, "a": {"mode": "i1e", "base_sha": "sha_a"}}, chosen)
    assert not touched_other.passed and "was not changed" in touched_other.evidence
    regenerate = plan_with(("b.shirt", "Draw it again.", "regenerate"))
    assert CH.check_gate1_change_route(fixes, regenerate, {"b": {"mode": "i1", "base_sha": ""}}, chosen).passed, "I1 may run for a regenerate"


# ---------------------------------------------------------------------------------------------------- L7 plan checks
def l7(fixes=(), redo=(), patch=()):
    return ChangePlan.model_validate({"understood_as": "x", "needs_clarification": "", "patch": list(patch), "redo_parts": list(redo),
                                      "image_fixes": [{"part_id": p, "fix_sentence": s, "scope": "global_edit", "region_hint": "whole", "keep": []} for p, s in fixes],
                                      "duo_contract_risks": []})


def test_validate_plan_checks_sentence_length_text_rules_and_part_ids(spec):
    assert CH.validate_plan(l7(fixes=[("b.shirt", "Make the jacket teal.")]), spec) == []
    assert CH.validate_plan(l7(fixes=[("concept_a", "Make it a little bigger.")]), spec) == [], "'little' is fine as a size word"
    long = " ".join(["word"] * (CH.FIX_WORDS_MAX + 1))
    assert any("at most" in p for p in CH.validate_plan(l7(fixes=[("b.shirt", long)]), spec))
    assert any("banned" in p for p in CH.validate_plan(l7(fixes=[("b.shirt", "Put the Roblox logo on it.")]), spec))
    assert any("unknown part" in p for p in CH.validate_plan(l7(fixes=[("b.cape", "Make it red.")]), spec))
    assert any("unknown part" in p for p in CH.validate_plan(l7(redo=[{"part_id": "x.y", "reason": "r"}]), spec))


def test_the_clicked_tile_and_the_gate1_parts_for_l7(spec):
    assert CH.clicked_tile_for("a") == "concept_a" and CH.clicked_tile_for("both") == "none" and CH.clicked_tile_for("b", "b.hair") == "b.hair"
    parts = CH.parts_for_l7(gate="concept", spec=spec, target="both")
    assert [p["part_id"] for p in parts] == ["concept_a", "concept_b"]
    assert [p["part_id"] for p in CH.parts_for_l7(gate="concept", spec=spec, target="b")] == ["concept_b"]


def test_the_change_fingerprint_depends_on_every_input():
    base = CH.change_fingerprint("s", "text", ["a"], "both")
    assert base == CH.change_fingerprint("s", "text", ["a"], "both")
    assert len({base, CH.change_fingerprint("s2", "text", ["a"], "both"), CH.change_fingerprint("s", "text2", ["a"], "both"),
                CH.change_fingerprint("s", "text", [], "both"), CH.change_fingerprint("s", "text", ["a"], "b")}) == 5

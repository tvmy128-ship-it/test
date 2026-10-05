"""Fix ladder (§8.9, §3.1), dependency invalidation (§9.8) and the two approval stamps (§9.7 + issue-file fix)."""
from __future__ import annotations

import copy

import pytest
from helpers_unit import make_part, make_project

from duoskin.checks.model import CheckResult
from duoskin.engine import deps, ladder
from duoskin.models.common import sha256_of
from duoskin.models.cost import Budget
from duoskin.models.part import DepEffect, LadderState, Part, PartKind, PartState
from duoskin.models.project import VersionPins

R, C, K = DepEffect.REGENERATE, DepEffect.RECOMPOSE, DepEffect.RECHECK


def hard(check_id="A_ALPHA", hint="none", passed=False, **kw):
    return CheckResult(check_id=check_id, kind="hard", passed=passed, fix_hint=hint, **kw)


def soft(check_id="A_STYLE", hint="none", passed=False):   # soft in the policy registry
    return CheckResult(check_id=check_id, kind="soft", passed=passed, fix_hint=hint)


def bud(remaining=10.0):
    return Budget(project_id="p", cap_usd=remaining, spent_usd=0.0, reserved_usd=0.0, ask_above_usd=2.0)


def mkpart(state=PartState.READY, **ladder_kw):
    return Part(id="a.face", project_id="p", character="a", kind=PartKind.FACE, label="x", state=state, ladder=LadderState(**ladder_kw))


# ------------------------------------------------------------------------------------------------- ladder
def test_soft_never_climbs():
    """test_ladder_soft_never_climbs (APP_SPEC §3.1): only soft failures end at Done with warnings and fixes_used stays 0."""
    part = mkpart()
    out = ladder.LoopOutcome.from_results([soft("A_STYLE", "regenerate"), soft("A_PHASH", "revise_plan"), hard(passed=True)], best="b" * 64,
                                          batch_pass_rate=0.0, near_miss=True, code_fixable=True, next_est=999.0)
    act = ladder.next_action(part, out, bud(remaining=0.0))
    assert isinstance(act, ladder.Done) and act.warnings == ["A_STYLE", "A_PHASH"] and act.best == "b" * 64
    assert part.ladder.fixes_used == 0
    assert ladder.record(part.ladder, act).fixes_used == 0 and ladder.record(part.ladder, act).rung == 0


def test_soft_failures_may_suggest_a_free_code_fix_only_on_unapproved_parts():
    out = ladder.LoopOutcome.from_results([soft("A_SWATCH", "code_palette_snap")])
    assert ladder.next_action(mkpart(), out, bud()).suggested_code_fix == ["code_palette_snap"]
    approved = ladder.next_action(mkpart(PartState.APPROVED), out, bud())
    assert isinstance(approved, ladder.Done) and approved.suggested_code_fix == []     # an approved part is never touched


def test_not_applicable_and_not_run_results_are_handled_fail_closed():
    from duoskin.checks.model import not_applicable, not_run

    na = not_applicable("FACE-02", "hard", "no_head_base")
    assert isinstance(ladder.next_action(mkpart(), ladder.LoopOutcome.from_results([na]), bud()), ladder.Done)
    nr = not_run("A_OCR", "hard", "ocr missing")                                         # could not run: counts as a failure
    assert not isinstance(ladder.next_action(mkpart(), ladder.LoopOutcome.from_results([nr]), bud()), ladder.Done)


def test_hard_failure_rungs_in_order():
    part = mkpart()
    o = ladder.LoopOutcome.from_results([hard(hint="code_alpha_cleanup")])
    assert isinstance(ladder.next_action(part, o, bud()), ladder.CodeFix)                 # rung 1 first: free
    o2 = ladder.LoopOutcome.from_results([hard(hint="code_alpha_cleanup")], code_fix_tried=True, regenerated_once=False, n=4)
    act = ladder.next_action(part, o2, bud())
    assert isinstance(act, ladder.Regenerate) and act.n == 8                              # rung 3, n doubled up to 8
    o3 = ladder.LoopOutcome.from_results([hard(hint="masked_edit")], masked_repairs=0)
    assert isinstance(ladder.next_action(part, o3, bud()), ladder.MaskedEdit)             # near miss: rung 2
    o4 = ladder.LoopOutcome.from_results([hard(hint="masked_edit")], masked_repairs=2, regenerated_once=True)
    assert ladder.next_action(part, o4, bud()) == ladder.NextTechnique(index=1)           # rung 4
    o5 = ladder.LoopOutcome.from_results([hard(hint="regenerate"), hard("A_X", "regenerate")], regenerated_once=True)
    assert ladder.next_action(mkpart(technique_index=2), o5, bud()) == ladder.NextTechnique(index=3)


def test_zero_percent_pass_rate_skips_the_regenerate_rung():
    o = ladder.LoopOutcome.from_results([hard()], batch_pass_rate=0.0, regenerated_once=False)
    assert isinstance(ladder.next_action(mkpart(), o, bud()), ladder.NextTechnique)


def test_the_three_fix_cap_and_the_budget_end_at_human_review():
    o = ladder.LoopOutcome.from_results([hard()], report="3 fixes tried", best="c" * 64)
    h = ladder.next_action(mkpart(fixes_used=3, best_asset_sha="d" * 64), o, bud())
    assert isinstance(h, ladder.Human) and h.best == "d" * 64 and h.report == "3 fixes tried"
    assert isinstance(ladder.next_action(mkpart(fixes_used=2), o, bud()), ladder.Regenerate)
    broke = ladder.LoopOutcome.from_results([hard()], next_est=5.0)
    assert isinstance(ladder.next_action(mkpart(), broke, bud(remaining=1.0)), ladder.Human)


def test_record_counts_rungs_2_to_5_but_not_the_free_code_fix():
    s = LadderState()
    s = ladder.record(s, ladder.CodeFix(hints=["code_recrop"]), outcome="ok")
    assert (s.fixes_used, s.rung) == (0, 1)
    s = ladder.record(s, ladder.MaskedEdit(), outcome="fail")
    s = ladder.record(s, ladder.Regenerate(n=8))
    s = ladder.record(s, ladder.NextTechnique(index=2), best="e" * 64)
    assert (s.fixes_used, s.rung, s.technique_index, s.best_asset_sha) == (3, 4, 2, "e" * 64)
    assert s.history == ["rung1:code_fix:ok", "rung2:masked_edit:fail", "rung3:regenerate:taken", "rung4:next_technique:taken"]
    assert ladder.record(s, ladder.RevisePlan()).fixes_used == 4


def test_pick_technique_skips_missing_keys_and_techniques_that_failed_twice():
    names = ["I3.face", "R1.face_part", "G2.backup", "manual"]
    provider_of = {"I3.face": "openai", "R1.face_part": "recraft", "G2.backup": "gemini", "manual": None}
    state = LadderState()
    assert ladder.pick_technique(names, 0, state, available_providers={"openai", "recraft"}, provider_of=provider_of) == 0
    assert ladder.pick_technique(names, 1, state, available_providers={"openai"}, provider_of=provider_of) == 3     # no recraft/gemini key
    state = ladder.note_failure(ladder.note_failure(state, "R1.face_part"), "R1.face_part")
    assert ladder.pick_technique(names, 1, state, available_providers={"recraft"}, provider_of=provider_of) == 3    # failed twice
    assert ladder.pick_technique(["I3.face"], 1, state, available_providers=set(), provider_of=provider_of) is None


def test_max_fixes_comes_from_the_threshold_registry():
    from duoskin.checks import thresholds

    assert ladder.max_fixes() == thresholds.get("ladder.max_fixes_per_part") == 3


# ------------------------------------------------------------------------------------------------- invalidation (§9.8)
SPEC = {
    "combo": "bg", "is_wildcard": False,
    "palette": [{"id": "p_main", "hex": "#112233"}, {"id": "p_acc", "hex": "#aa0000"}],
    "world": {"detail_level": "standard", "material_family": "cotton", "theme": "koi", "story": "s", "pair_structure": "same_club",
              "structure_note": "n", "palette_family": "warm"},
    "shared_anchors": [{"kind": "colour", "ref": "p_acc"}], "contrasts": [{"axis": "silhouette"}],
    "a": {"presentation": "boy",
          "face": {"iris_style": "round", "mouth_style": "smile", "eye_shape": "round", "nose_style": "dot", "iris_ref": "p_main"},
          "hair": {"kit_style_id": "bob", "description": "short", "colour_ref": "p_main"},
          "top": {"recipe_id": "tee", "fabric_id": "cotton", "main_ref": "p_main", "prints": [{"motif": "koi", "region": "chest", "scale": 1, "ink_ref": "p_main"}]},
          "bottom": {"recipe_id": "jeans", "shoes": {"style_id": "sneaker", "motif": "none"}, "prints": []},
          "accessories": [{"description": "bag", "kind": "bag", "material": "plush", "size_class": "s", "attachment": "back"}],
          "body": {"skin_tone": "tone2", "modesty_ref": "p_main"},
          "dna": {"shape_language": "round_soft", "motif_object": "koi", "colour_plan": "a", "focal_location": "chest", "accessory_style": "x", "energy": "calm"}},
    "b": {"presentation": "girl",
          "face": {"iris_style": "tall"}, "hair": {"kit_style_id": "braid"}, "top": {"recipe_id": "hoodie", "prints": []},
          "bottom": {"recipe_id": "skirt", "shoes": {"motif": "star"}, "prints": []}, "accessories": [], "body": {"skin_tone": "tone4"},
          "dna": {"shape_language": "sharp_angular", "motif_object": "star"}},
}
PARTS = deps.default_parts(SPEC)


def changed(path_values, redo=(), **kw):
    new = copy.deepcopy(SPEC)
    for path, value in path_values.items():
        node = new
        *head, last = path.strip("/").split("/")
        for seg in head:
            node = node[int(seg)] if isinstance(node, list) else node[seg]
        if isinstance(node, list):
            node[int(last)] = value
        else:
            node[last] = value
    return deps.affected_parts(SPEC, new, redo, existing_parts=PARTS, **kw)


def effects(report):
    return {p.part_id: p.effect for p in report.parts}


def test_default_parts_follow_the_spec():
    assert {"a.face", "a.hair", "a.shirt", "a.pants", "a.colours", "a.acc.0", "a.print.top.0", "b.face", "b.print.shoes.0"} <= set(PARTS)
    assert "a.print.shoes.0" not in PARTS and "b.acc.0" not in PARTS


@pytest.mark.parametrize("path,value,expect", [
    ("/a/face/iris_style", "square", {"a.face": R}),
    ("/a/face/mouth_style", "frown", {"a.face": R}),
    ("/a/face/eye_shape", "narrow", {"a.face": C}),
    ("/a/face/nose_style", "none", {"a.face": C}),
    ("/a/face/iris_ref", "p_acc", {"a.face": C}),
    ("/a/hair/kit_style_id", "bun", {"a.hair": R}),
    ("/a/hair/description", "long", {"a.hair": R}),
    ("/a/hair/colour_ref", "p_acc", {"a.hair": C}),
    ("/a/top/recipe_id", "polo", {"a.shirt": C}),
    ("/a/top/fabric_id", "denim", {"a.shirt": C}),
    ("/a/top/prints/0/motif", "fish", {"a.print.top.0": R, "a.shirt": C}),
    ("/a/top/prints/0/region", "back", {"a.shirt": C}),
    ("/a/top/prints/0/scale", 2, {"a.shirt": C}),
    ("/a/bottom/recipe_id", "shorts", {"a.pants": C}),
    ("/a/bottom/shoes/style_id", "boot", {"a.pants": C}),
    ("/b/bottom/shoes/motif", "heart", {"b.print.shoes.0": R, "b.pants": C}),
    ("/a/accessories/0/description", "backpack", {"a.acc.0": R}),
    ("/a/accessories/0/material", "metal", {"a.acc.0": R}),
    ("/a/accessories/0/size_class", "m", {"a.acc.0": K}),
    ("/a/accessories/0/attachment", "waist", {"a.acc.0": K}),
    ("/a/body/skin_tone", "tone3", {"a.colours": C, "a.face": K}),
    ("/a/body/modesty_ref", "p_acc", {"a.colours": C}),
])
def test_dependency_table_rows(path, value, expect):
    assert effects(changed({path: value})) == expect


def test_dna_shape_language_redoes_only_that_characters_routed_tiles():
    e = effects(changed({"/a/dna/shape_language": "boxy_sturdy"}))
    assert e == {"a.face": R, "a.hair": R, "a.acc.0": R, "a.print.top.0": R}              # prints, face, hair, accessory; not shirt/pants


def test_dna_motif_object_redoes_accessories_only():
    assert effects(changed({"/a/dna/motif_object": "star"})) == {"a.acc.0": R}


def test_world_detail_level_redoes_prints_and_face_parts_of_both_characters():
    e = effects(changed({"/world/detail_level": "maximal"}))
    assert e == {"a.face": R, "a.print.top.0": R, "b.face": R, "b.print.shoes.0": R}


@pytest.mark.parametrize("path", ["/a/dna/colour_plan", "/a/dna/focal_location", "/a/dna/energy", "/world/material_family", "/world/theme",
                                  "/world/story", "/world/pair_structure", "/world/palette_family"])
def test_lint_only_fields_invalidate_no_parts(path):
    r = changed({path: "something else"})
    assert r.parts == [] and r.lint_only == [path]


def test_palette_hex_change_recomposes_every_part_that_references_it_in_both_characters():
    r = changed({"/palette/0/hex": "#ffffff"})                                              # p_main
    e = effects(r)
    assert e["a.shirt"] == C and e["a.hair"] == C and e["a.face"] == C and e["a.colours"] == C
    assert e["a.print.top.0"] == K                                                          # flat art: $0 recolour + recheck
    assert "a.pants" not in e and "a.acc.0" not in e and "b.shirt" not in e                 # parts that do not reference p_main
    assert effects(changed({"/palette/1/hex": "#00ff00"})) == {}                            # p_acc is only used by shared_anchors


def test_palette_ids_and_identity_fields_are_not_patchable():
    for path, value in (("/palette/0/id", "p_other"), ("/combo", "gg"), ("/is_wildcard", True), ("/a/presentation", "girl")):
        r = changed({path: value})
        assert r.not_patchable == [path] and r.parts == []


def test_partner_parts_only_recheck_pair_dependent_checks():
    r = changed({"/a/hair/kit_style_id": "bun"})
    assert effects(r) == {"a.hair": R}
    assert set(r.pair_rechecks) == {"b.face", "b.hair", "b.shirt", "b.pants"}              # keep their approval if the checks pass (S21)
    assert not any(p.startswith("b.print") or p == "b.colours" for p in r.pair_rechecks)


def test_redo_parts_from_the_change_interpreter_are_merged_by_union():
    r = changed({"/a/top/recipe_id": "polo"}, redo=["a.hair", {"part_id": "a.shirt", "effect": "regenerate"}, {"part_id": "b.face", "effect": "recheck"}])
    e = effects(r)
    assert e["a.shirt"] == R and e["a.hair"] == R and e["b.face"] == K                      # the strongest effect wins
    assert "redo" in next(p for p in r.parts if p.part_id == "a.hair").reasons


def test_a_change_after_gate3_opened_stales_the_duo():
    assert changed({"/a/top/recipe_id": "polo"}, gate3_open=True).duo_stale is True
    assert changed({"/a/top/recipe_id": "polo"}).duo_stale is False
    assert changed({}, gate3_open=True).duo_stale is False


def test_identical_specs_affect_nothing_and_estimates_are_summed():
    r = deps.affected_parts(SPEC, copy.deepcopy(SPEC))
    assert r.parts == [] and r.changed_paths == []
    new = copy.deepcopy(SPEC)
    new["a"]["hair"]["kit_style_id"] = "bun"
    r = deps.affected_parts(SPEC, new, existing_parts=PARTS, estimator=lambda p: 1.25 if p.effect == R else 0.0)
    assert r.estimate_usd == 1.25 and r.parts[0].estimate_usd == 1.25


def test_added_and_removed_prints_and_accessories():
    new = copy.deepcopy(SPEC)
    new["a"]["accessories"].append({"description": "hat", "kind": "hat", "material": "felt", "size_class": "s", "attachment": "head"})
    r = deps.affected_parts(SPEC, new, existing_parts=PARTS + ["a.acc.1"])
    assert effects(r) == {"a.acc.1": R}
    new2 = copy.deepcopy(SPEC)
    new2["a"]["top"]["prints"].append({"motif": "star", "region": "back", "scale": 1})
    assert effects(deps.affected_parts(SPEC, new2, existing_parts=PARTS)) == {"a.print.top.1": R, "a.shirt": C}


def test_dna_field_users_filters_by_character_and_existing_parts():
    assert deps.dna_field_users("shape_language", "b", parts=PARTS) == ["b.face", "b.hair", "b.print.shoes.0"]
    assert deps.dna_field_users("motif_object", "a", parts=PARTS) == ["a.acc.0"]
    assert deps.dna_field_users("detail_level", "a", parts=PARTS) == ["a.face", "a.print.top.0", "b.face", "b.print.shoes.0"]
    assert deps.dna_field_users("colour_plan", "a", parts=PARTS) == []
    assert deps.dna_field_users("shape_language", "a", spec=SPEC)


def test_json_pointer_helpers():
    assert deps.resolve(SPEC, "/a/top/prints/0/motif") == "koi" and deps.resolve(SPEC, "/nope/x", "d") == "d"
    assert deps.resolve({"a/b": {"c~d": 1}}, "/a~1b/c~0d") == 1
    assert deps.changed_pointers({"a": [1, 2]}, {"a": [1, 3, 4]}) == ["/a/1", "/a/2"]
    assert deps.expand_pattern(SPEC, "/{c}/face/*", "a")[:2] == ["/a/face/iris_style", "/a/face/mouth_style"]
    assert deps.expand_pattern(SPEC, "/a/hair/missing", None) == ["/a/hair/missing"]        # exact pointers are kept even when absent


# ------------------------------------------------------------------------------------------------- approval stamps
SHA = {n: sha256_of(n) for n in "abcdef"}


def board_part(**kw):
    return Part(id="a.shirt", project_id="p", character="a", kind=PartKind.SHIRT, label="A", deps=deps.default_dep_rules("shirt", "a"),
                state=PartState.READY, board_assets={"flat": SHA["a"], "front": SHA["b"]}, **kw)


FACTS = deps.ApprovalFacts(input_shas=[SHA["c"]], prompts=[["I2", 1, SHA["d"]]], models=["m1"], kit_subset_sha="k1")
PINS = VersionPins(models={}, prompt_versions={}, schema_hashes={}, house_style_version=3, style_guide_version=1,
                   kit_manifest_sha=SHA["e"], thresholds_version="v1", rules_version=1, app_version="0.1.0")


def h(part=None, spec=SPEC, pins=PINS, facts=FACTS):
    return deps.approval_hash(part or board_part(), spec, pins, facts)


def test_approval_hash_is_stable_and_covers_exactly_the_output_affecting_inputs():
    base = h()
    assert base == h() and len(base) == 64
    other_spec = copy.deepcopy(SPEC)
    other_spec["a"]["top"]["recipe_id"] = "polo"
    assert h(spec=other_spec) != base                                                     # the part's own spec slice
    for path, value in (("/b/top/recipe_id", "x"), ("/a/hair/description", "long"), ("/a/accessories/0/size_class", "m"), ("/world/theme", "x"),
                        ("/a/dna/energy", "wild")):
        s2 = copy.deepcopy(SPEC)
        node = s2
        *head, last = path.strip("/").split("/")
        for seg in head:
            node = node[int(seg)] if isinstance(node, list) else node[seg]
        node[last] = value
        assert h(spec=s2) == base, path                                                   # other paths do not touch this part's hash
    p2 = board_part()
    p2.board_assets["flat"] = SHA["f"]
    assert h(p2) != base                                                                  # board outputs
    assert h(facts=deps.ApprovalFacts([SHA["f"]], FACTS.prompts, FACTS.models, "k1")) != base
    assert h(facts=deps.ApprovalFacts(FACTS.input_shas, [["I2", 2, SHA["d"]]], FACTS.models, "k1")) != base
    assert h(facts=deps.ApprovalFacts(FACTS.input_shas, FACTS.prompts, ["m2"], "k1")) != base
    assert h(facts=deps.ApprovalFacts(FACTS.input_shas, FACTS.prompts, FACTS.models, "k2")) != base
    assert h(pins=PINS.model_copy(update={"house_style_version": 4})) != base


def test_build_assets_never_change_the_approval_hash_but_do_change_the_build_hash():
    """The issue-file fix: two stamps. Building the part must not make the Gate 2 approval look stale."""
    part = board_part()
    approved = h(part)
    part.approval = deps.make_approval(part, SPEC, spec_id="spc_1", spec_version=1, pins=PINS, decision_id="dec_1", facts=FACTS)
    assert part.approval.approval_hash == approved and part.approval.build_hash is None
    built = part.model_copy(update={"build_assets": {"texture": SHA["e"], "mesh_gltf": SHA["f"]}})
    assert h(built) == approved                                                           # approval_hash ignores build_assets
    assert deps.check_approval(built, SPEC, PINS, FACTS).ok
    b1 = deps.build_hash(built)
    rebuilt = built.model_copy(update={"build_assets": {"texture": SHA["e"], "mesh_gltf": SHA["a"]}})
    assert deps.build_hash(rebuilt) != b1 and h(rebuilt) == approved                      # a rebuilt mesh: new build stamp only
    assert deps.build_hash(built, build_inputs=[SHA["c"]]) != b1
    reapproved = built.model_copy(update={"approval": built.approval.model_copy(update={"approval_hash": SHA["f"]})})
    assert deps.build_hash(reapproved) != b1                                              # the build stamp chains the approval


def test_stamp_build_confirm_build_and_verification_flows(rt):
    p = make_project(rt)
    part = make_part(rt, p, board={"flat": SHA["a"]})
    part = rt.repo.save_part(part)
    approval = deps.make_approval(part, SPEC, spec_id="s", spec_version=1, pins=PINS, decision_id="dec_1", facts=FACTS)
    rt.repo.mutate_part(p.id, "a.shirt", lambda x: (setattr(x, "approval", approval), setattr(x, "state", PartState.APPROVED)))
    rt.repo.upsert_approval(p.id, approval)
    rt.repo.mutate_part(p.id, "a.shirt", lambda x: x.build_assets.update({"texture": SHA["e"]}))
    stamped = deps.stamp_build(rt.repo, p.id, "a.shirt", build_inputs=[SHA["c"]])
    assert stamped.state == PartState.BUILT and stamped.approval.build_hash and stamped.approval.build_confirmed_at is None
    v = deps.verify_part(rt.repo, stamped, SPEC, PINS, facts=FACTS, build_inputs=[SHA["c"]])
    assert v.approval.ok and v.build.ok and not v.stale and not v.needs_gate3_look
    confirmed = deps.confirm_build(rt.repo, p.id, "a.shirt", "dec_pick")
    assert confirmed.approval.build_confirmed_decision_id == "dec_pick"
    # a rebuilt mesh: only the build stamp is off -> a new look at Gate 3, never a Gate 2 re-approval
    rebuilt = rt.repo.mutate_part(p.id, "a.shirt", lambda x: x.build_assets.update({"texture": SHA["f"]}))
    v2 = deps.verify_part(rt.repo, rebuilt, SPEC, PINS, facts=FACTS, build_inputs=[SHA["c"]])
    assert v2.approval.ok and not v2.build.ok and v2.needs_gate3_look and not v2.stale
    after = deps.apply_verification(rt.repo, p.id, v2, rt.bus)
    assert after.state == PartState.BUILT and "build_changed" in after.flags and after.approval.build_confirmed_at is None
    assert rt.repo.valid_approval(p.id, "a.shirt") is not None                              # the Gate 2 approval is still valid
    # a changed spec slice: the Gate 2 approval itself is stale -> re-approve
    other = copy.deepcopy(SPEC)
    other["a"]["top"]["fabric_id"] = "denim"
    v3 = deps.verify_part(rt.repo, rt.repo.get_part(p.id, "a.shirt"), other, PINS, facts=FACTS)
    assert v3.stale and "re-approve" in v3.approval.reason
    stale = deps.apply_verification(rt.repo, p.id, v3, rt.bus)
    assert stale.state == PartState.STALE and rt.repo.valid_approval(p.id, "a.shirt") is None
    assert [e for e in rt.bus.events_after(0) if e.type == "part.state" and e.payload["state"] == "stale"]


def test_check_functions_report_missing_stamps():
    part = board_part()
    assert not deps.check_approval(part, SPEC, PINS).ok and "no approval" in deps.check_approval(part, SPEC, PINS).reason
    assert not deps.check_build(part).ok and "never stamped" in deps.check_build(part).reason


def test_a_build_stamp_needs_an_approval_first(rt):
    p = make_project(rt)
    rt.repo.save_part(make_part(rt, p, board={"flat": SHA["a"]}))
    with pytest.raises(ValueError, match="no approval"):
        deps.stamp_build(rt.repo, p.id, "a.shirt")
    with pytest.raises(ValueError, match="no build stamp"):
        deps.confirm_build(rt.repo, p.id, "a.shirt", "dec")


def test_default_dep_rules_cover_the_9_8_table():
    shirt = deps.default_dep_rules("shirt", "a")
    assert [(r.pattern, r.effect) for r in shirt] == [("/a/top/*", C)]
    face = {r.pattern for r in deps.default_dep_rules("face", "b")}
    assert {"/b/face/*", "/b/body/skin_tone", "/world/detail_level", "/b/dna/shape_language"} <= face
    assert {r.pattern for r in deps.default_dep_rules("accessory", "a", 2)} == {"/a/accessories/2", "/a/dna/shape_language", "/a/dna/motif_object"}
    assert deps.default_dep_rules("duo", "a") == []
    part = board_part()
    assert deps.part_dep_paths(part, SPEC)[0].startswith("/a/top/")

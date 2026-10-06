"""The mock roles of the plan loop (``pipeline/mock_roles.py``): deterministic, valid answers that read the tags the real prompts send."""
from __future__ import annotations

import json
import random

import pytest

from duoskin.models import kitenums
from duoskin.models.llm_io import ChangePlan, Critique, PairJudgment, Revision
from duoskin.models.spec import PlanSet
from duoskin.pipeline import kits
from duoskin.pipeline import lint as LI
from duoskin.pipeline import mock_roles as MR
from duoskin.prompts.llm import compile_llm
from duoskin.providers.mock.llm import RoleCall


@pytest.fixture
def inv(unit_rt):
    kits.install_inventory(unit_rt)
    yield kitenums.current_inventory()
    kitenums.set_default_inventory(None)


def call(route, out, text, seed=1, content=()):
    return RoleCall(route=route, out=out, schema={}, system_text="", content_text=text, content=list(content), seed=seed, rng=random.Random(seed),
                    prompt_version=1)


def planner_text(**kw):
    inputs = {"brief_text": "a duo", "structure_request": "auto", "must_include": "none", "combo": "bg", "reference_analysis": "none",
              "taste_profile": "none", "recent_cards": "none", "recently_used": "none", "avoid": ""}
    inputs.update(kw)
    return compile_llm("L3.planner", inputs).user_text


def plan(seed=1, **kw):
    return MR.planner_builder(call("L3_planner", PlanSet, planner_text(**kw), seed))


def lint_clean(out):
    b = LI.lint_candidates([(f"s{i}", s) for i, s in enumerate(out["specs"])], LI.lint_context(None), how_they_differ=out["how_they_differ"])
    return b.unclean() == [] and b.set_clean


def test_the_planner_answers_three_clean_plans_and_is_deterministic(inv):
    out = plan(seed=3)
    assert len(out["specs"]) == 3 and sum(s["is_wildcard"] for s in out["specs"]) == 1 and lint_clean(out)
    assert len({s["world"]["pair_structure"] for s in out["specs"]}) == 3
    assert plan(seed=3) == out and plan(seed=4) != out
    PlanSet.model_validate(out)


@pytest.mark.parametrize("brief,combo,structure", [("twin sisters", "gg", "mirror"), ("matching team jackets", "bb", "same_club"),
                                                   ("a duo in changing seasons", "bg", "seasonal_twins"), ("a girl and her plush mascot", "gb", "object_mascot")])
def test_a_brief_that_names_a_structure_gets_it_three_times_with_one_wildcard(inv, brief, combo, structure):
    out = plan(brief_text=brief, combo=combo)
    assert {s["world"]["pair_structure"] for s in out["specs"]} == {structure}
    assert sum(s["is_wildcard"] for s in out["specs"]) == 1 and lint_clean(out)
    assert {s["combo"] for s in out["specs"]} == {combo}


def test_the_dropdown_request_wins_and_an_open_brief_with_avoid_skips_rejected_structures(inv):
    assert {s["world"]["pair_structure"] for s in plan(structure_request="mirror")["specs"]} == {"mirror"}
    avoid = json.dumps([{"pair_structure": s, "palette_family": "warm_pastel", "theme": "x", "reason": "no"} for s in ("complement", "mirror", "same_club")])
    out = plan(avoid=avoid)
    assert {s["world"]["pair_structure"] for s in out["specs"]}.isdisjoint({"complement", "mirror", "same_club"})
    assert all(s["world"]["palette_family"] != "warm_pastel" for s in out["specs"])


def test_a_replacement_request_gets_one_spec_with_the_asked_wildcard_role(inv):
    text = planner_text(replacement=True, wildcard_flag="true", dropped_reasons="a rule failed")
    one = MR.planner_builder(call("L3_planner", PlanSet, text))
    assert len(one["specs"]) == 1 and one["specs"][0]["is_wildcard"] is True
    plain = MR.planner_builder(call("L3_planner", PlanSet, planner_text(replacement=True, wildcard_flag="false", dropped_reasons="x")))
    assert plain["specs"][0]["is_wildcard"] is False
    no_wild = MR.planner_builder(call("L3_planner", PlanSet, planner_text(replacement=True, wildcard_flag="true", dropped_reasons="x",
                                                                      brief_text="[mock:no_wildcard_replacement]")))
    assert no_wild["specs"][0]["is_wildcard"] is False


def test_must_include_lines_are_echoed_with_a_path_every_spec_has(inv):
    out = plan(must_include="a teal bow\nsilver boots")
    assert [c["text"] for c in out["brief_constraints"]] == ["a teal bow", "silver boots"]
    assert all(c["spec_paths"] == ["/a/accessories/0"] for c in out["brief_constraints"])


@pytest.mark.parametrize("hook,value", [("[mock:dangling]", "p99"), ("[mock:unfixable]", "p98")])
def test_the_test_hooks_break_plan_two_in_the_brief(inv, hook, value):
    out = plan(brief_text=f"a duo {hook}")
    assert out["specs"][1]["a"]["top"]["base_ref"] == value
    with pytest.raises(ValueError):          # the answer breaks the spec rules on purpose
        PlanSet.model_validate(out)


def reviser_text(spec, findings):
    return compile_llm("L6.reviser", {"spec_json": json.dumps(spec, sort_keys=True), "findings": findings}).user_text


def test_the_reviser_fixes_a_dangling_reference_the_wildcard_flag_and_a_critic_fix_and_leaves_the_rest(inv):
    spec = plan()["specs"][0]
    spec["a"]["top"]["base_ref"] = "p99"
    rev = MR.revision_builder(call("L6_reviser", Revision, reviser_text(spec, "1. /a/top/base_ref: p99 is not a palette id (palette_integrity)")))
    Revision.model_validate(rev)
    assert rev["patch"] == [{"op": "replace", "path": "/a/top/base_ref", "value_json": '"p1"', "finding": "1"}]
    stuck = json.loads(json.dumps(spec))
    stuck["a"]["top"]["base_ref"] = "p98"
    assert MR.revision_builder(call("L6_reviser", Revision, reviser_text(stuck, "1. /a/top/base_ref: p98 is not a palette id (palette_integrity)")))["patch"] == []
    flag = MR.revision_builder(call("L6_reviser", Revision, reviser_text(spec, "1. /is_wildcard: the plan set has no wildcard: set is_wildcard to true on this spec (wildcard_count)")))
    assert flag["patch"][0]["value_json"] == "true"
    text = spec["a"]["accessories"][0]["description"]
    critic = MR.revision_builder(call("L6_reviser", Revision, reviser_text(spec, "1. /a/accessories/0/description: vague; name the colour (critic)")))
    assert critic["patch"][0]["path"] == "/a/accessories/0/description" and json.loads(critic["patch"][0]["value_json"]) != text


def critique_text(spec, brief="a duo", wildcard=False):
    inputs = {"spec_id": "X", "spec_json": json.dumps(spec, sort_keys=True), "measured_facts": "{}", "brief_text": brief}
    if wildcard:
        inputs["wildcard"] = True
    return compile_llm("L4.critic", inputs).user_text


def test_the_critic_scores_every_criterion_once_and_a_hook_adds_one_high_fix(inv):
    from duoskin.models.llm_io import CRITERIA

    spec = plan()["specs"][0]
    c = MR.critique_builder(call("L4_critic", Critique, critique_text(spec)))
    Critique.model_validate(c)
    assert [s["criterion"] for s in c["scores"]] == list(CRITERIA) and c["fixes"] == []
    assert c == MR.critique_builder(call("L4_critic", Critique, critique_text(spec))), "deterministic"
    fixed = MR.critique_builder(call("L4_critic", Critique, critique_text(spec, brief="x [mock:critic_fix]")))
    assert [f["severity"] for f in fixed["fixes"]] == ["high"] and fixed["fixes"][0]["path"] == "/a/accessories/0/description"
    wild = MR.critique_builder(call("L4_critic", Critique, critique_text(spec, wildcard=True)))
    assert next(s for s in wild["scores"] if s["criterion"] == "taste_fit")["level"] == "ok"


def test_the_pairwise_ranker_picks_the_same_plan_in_both_orders(inv):
    a, b = plan()["specs"][:2]

    def judge(first, second):
        text = compile_llm("L5.pairwise_ranker", {"first_json": json.dumps(first, sort_keys=True), "second_json": json.dumps(second, sort_keys=True),
                                                  "measured_facts": "{}", "brief_text": "a duo"}).user_text
        out = MR.pair_builder(call("L5_pairwise", PairJudgment, text))
        PairJudgment.model_validate(out)
        return out["overall"]

    one, two = judge(a, b), judge(b, a)
    assert {one, two} == {"first", "second"} or one == two == "tie"
    assert (one == "first") == (two == "second"), "the verdict follows the plan, not the position"


def change_text(spec, request, clicked="none"):
    return compile_llm("L7.change_interpreter", {"spec_json": json.dumps(spec, sort_keys=True), "parts": "[]", "clicked_tile": clicked,
                                                  "user_change_request": request}).user_text


def test_the_change_role_resolves_her_and_his_from_the_clicked_tile_or_the_presentation(inv):
    spec = plan(combo="bg")["specs"][0]
    plan_ = MR.change_builder(call("L7_change", ChangePlan, change_text(spec, "make her jacket teal")))
    ChangePlan.model_validate(plan_)
    assert plan_["patch"][0]["path"].startswith("/palette/") and plan_["image_fixes"][0]["part_id"].startswith("b."), "her = the girl, B in a boy-girl duo"
    his = MR.change_builder(call("L7_change", ChangePlan, change_text(spec, "make his jacket red")))
    assert his["image_fixes"][0]["part_id"].startswith("a.")
    clicked = MR.change_builder(call("L7_change", ChangePlan, change_text(spec, "make the jacket teal", clicked="concept_b")))
    assert clicked["image_fixes"][0]["part_id"].startswith("b.")
    vague = MR.change_builder(call("L7_change", ChangePlan, change_text(spec, "make it nicer")))
    assert vague["needs_clarification"] and vague["patch"] == []


def test_the_inventory_lists_what_the_picture_shows_and_marks_a_cape_as_not_buildable(inv):
    from duoskin.models.llm_io import ElementList

    spec = plan(brief_text="x [mock:notbuildable]")["specs"][0]

    def items(char):
        text = compile_llm("L15.concept_inventory", {"spec_json": json.dumps(spec, sort_keys=True), "measured_facts": json.dumps({"character": char})}).user_text
        out = MR.inventory_builder(call("L11_checker", ElementList, text))
        ElementList.model_validate(out)
        return out["items"]

    assert any(not i["buildable"] and "cape" in i["element"] for i in items("b"))
    assert all(i["buildable"] for i in items("a"))


def test_the_reference_and_taste_roles_answer_what_their_gate_a_keeps(inv):
    from duoskin.models.llm_io import ReferenceAnalysis, TasteProfile

    ref = MR.reference_builder(call("L1_reference", ReferenceAnalysis, "", content=[{"type": "image"}]))
    ReferenceAnalysis.model_validate(ref)
    axes = [r["axis"] for r in ref["rules"]]
    assert all(axes.count(a) <= 3 for a in axes) and len(ref["rules"]) >= 3
    tables = {"fields": {"hair_style": {"hair_bob_03": {"approved": ["a1", "a2"], "rejected": []}, "hair_spiky_05": {"approved": [], "rejected": ["r1", "r2"]}}}}
    text = compile_llm("L2.taste_builder", {"frequency_tables": json.dumps(tables), "rating_summary": "{}", "reference_rules": "none"}).user_text
    prof = MR.taste_builder(call("L2_taste", TasteProfile, text))
    TasteProfile.model_validate(prof)
    assert [r["evidence_ids"] for r in prof["likes"]] == [["a1", "a2"]] and [r["evidence_ids"] for r in prof["dislikes"]] == [["r1", "r2"]]


def test_the_not_buildable_hook_keeps_its_cape_when_the_first_plan_is_an_object_mascot_plan(inv):
    """The touch-up of an object-mascot plan rewrites the accessory descriptions: the hook ran before it and the cape was lost, so about one run in six
    of the tests that need a plan with a cape found none (the Gate 1 "cannot build" list was empty)."""
    out = plan(brief_text="a duo with a cape [mock:notbuildable]", structure_suggestion=json.dumps(["object_mascot", "mirror", "complement"]))
    assert out["specs"][0]["world"]["pair_structure"] == "object_mascot"
    assert "cape" in out["specs"][0]["b"]["accessories"][0]["description"]
    assert [s["world"]["pair_structure"] for s in out["specs"]] == ["object_mascot", "mirror", "complement"]


@pytest.mark.parametrize("hook", ["[mock:dangling]", "[mock:unfixable]"])
def test_the_broken_plan_of_the_hooks_is_never_the_wildcard(inv, hook):
    """The tests that read a dropped plan expect a plain plan: plan 1 was the wildcard one time in three, and they failed one run in three."""
    for seed in range(60):
        out = plan(seed=seed, brief_text=f"a duo {hook}")
        assert out["specs"][1]["is_wildcard"] is False and sum(s["is_wildcard"] for s in out["specs"]) == 1, seed
        assert out["specs"][1]["a"]["top"]["base_ref"] in ("p98", "p99")

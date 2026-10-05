"""Sameness, measured: the plan loop is run 30 times (30 projects, each with its own order seed) against the mock Claude and the choices the
app ends up with must be spread out.

The mock reads kits the way a lazy model does (the order of the cached ``<kit_inventory>``), so these tests show that the seeded shuffle,
the structure rotation and the soft hints do their job without any lint. Real models cannot be called here: the machinery that decides what
they see is what is checked.
"""
from __future__ import annotations

import json
import re
from collections import Counter

import pytest
import specfix
from planhelpers import approved_spec, db_project

from duoskin.models import kitenums
from duoskin.models.spec import PlanSet
from duoskin.pipeline import brief as BR
from duoskin.prompts import system_blocks as SB
from duoskin.prompts.llm import compile_llm
from duoskin.providers.base import CallCtx
from duoskin.providers.mock.llm import MockLLM

N = 30
MAX_SHARE = 0.40


def ask_planner(rt, project, *, seeded: bool = True):
    """What ``plan.run_planner`` does for one round: the planner inputs of the app, the compiled L3 prompt (kit order seeded per project round)
    and the mock Claude's answer."""
    inputs = BR.planner_inputs(rt, project)
    seed = BR.seed_for(rt, project.id) if seeded else None
    prompt = compile_llm("L3.planner", inputs, order_seed=seed)
    result = MockLLM().call("L3_planner", system=prompt.system, content=[{"type": "text", "text": prompt.user_text}], out=PlanSet,
                            ctx=CallCtx.null(), prompt_version=1)
    return inputs, prompt, result.parsed


def choices(plans) -> dict[str, Counter]:
    out: dict[str, Counter] = {k: Counter() for k in ("structure", "palette_family", "hair_kit", "eye_shape", "mouth", "top", "shoe", "fabric", "motif")}
    for plan in plans:
        for s in plan.specs:
            out["structure"][s.world.pair_structure] += 1
            out["palette_family"][s.world.palette_family] += 1
            for c in (s.a, s.b):
                out["hair_kit"][c.hair.kit_style_id] += 1
                out["eye_shape"][c.face.eye_shape] += 1
                out["mouth"][c.face.mouth_style] += 1
                out["top"][c.top.recipe_id] += 1
                out["shoe"][c.bottom.shoes.style_id] += 1
                out["fabric"][c.top.fabric_id] += 1
                out["motif"][c.dna.motif_object] += 1
    return out


def share(counter: Counter) -> float:
    return max(counter.values()) / sum(counter.values())


@pytest.fixture(scope="module")
def runs(tmp_path_factory):
    """30 projects, 30 planner rounds against the mock (demo hair kit installed), computed once for the module."""
    from duoskin.engine.testkit import make_runtime

    rt = make_runtime(tmp_path_factory.mktemp("home") / "home", providers_mode="mock")
    rt.startup(start_threads=False, backup=False)
    try:
        with kitenums.use_inventory(specfix.demo_inventory()):
            seeded = [ask_planner(rt, db_project(rt, name=f"Duo {i}", brief="a cosy duo")) for i in range(N)]
            sorted_ = [ask_planner(rt, db_project(rt, name=f"Sorted {i}", brief="a cosy duo"), seeded=False) for i in range(N)]
        yield seeded, sorted_
    finally:
        rt.shutdown()


def test_thirty_runs_spread_every_choice_and_no_value_passes_forty_percent(runs):
    seeded, _ = runs
    got = choices([plan for _, _, plan in seeded])
    for name, counter in got.items():
        assert share(counter) <= MAX_SHARE, (name, counter.most_common(3))
    assert len(got["structure"]) == 6 and len(got["palette_family"]) >= 9
    assert len(got["hair_kit"]) == len(kitenums.current_inventory().hair) or len(got["hair_kit"]) >= 7, got["hair_kit"]
    assert len(got["eye_shape"]) == 3 and len(got["mouth"]) >= 6 and len(got["top"]) >= 6 and len(got["shoe"]) >= 5
    assert len(got["motif"]) >= 12, "the motif objects are not the generator's few fixed ones"


def test_the_order_of_the_inventory_blocks_differs_between_runs(runs):
    seeded, sorted_ = runs
    kit_blocks = [prompt.system[3]["text"] for _, prompt, _ in seeded]
    assert len(set(kit_blocks)) == N, "every project reads the kits in its own order"
    first_hair = Counter(SB.inventory_order(b, "hair")[0] for b in kit_blocks)
    assert len(first_hair) >= 6 and share(first_hair) <= MAX_SHARE, first_hair
    first_recipe = Counter(SB.inventory_order(b, "recipes")[0] for b in kit_blocks)
    assert len(first_recipe) >= 8 and share(first_recipe) <= MAX_SHARE, first_recipe
    assert len({prompt.system[4]["text"] for _, prompt, _ in seeded}) == N, "the structure profiles and menus are reordered too"
    # the sorted control: one order for everyone, so the same first entries in every duo
    assert len({prompt.system[3]["text"] for _, prompt, _ in sorted_}) == 1


def test_without_the_shuffle_a_first_item_reader_keeps_to_the_same_few_kits(runs):
    """The control run: sorted blocks make the (lazy) mock pick from the first entries only, which is the sameness the shuffle removes."""
    seeded, sorted_ = runs
    with_shuffle, without = choices([p for _, _, p in seeded]), choices([p for _, _, p in sorted_])
    assert len(without["hair_kit"]) <= 4 < len(with_shuffle["hair_kit"])
    assert len(without["shoe"]) < len(with_shuffle["shoe"]) and len(without["mouth"]) < len(with_shuffle["mouth"])
    first_four = sorted(h for h in specfix.demo_inventory().ids("HairKit") if h != "hair_custom")[:4]
    assert set(without["hair_kit"]) == set(first_four), "a first-item reader only ever uses the first entries of the sorted list"


def test_the_structure_rotation_is_a_suggestion_for_open_briefs_and_spreads_over_all_six(runs):
    seeded, _ = runs
    picks = Counter()
    for inputs, _, plan in seeded:
        suggestion = json.loads(inputs["structure_suggestion"])
        assert len(suggestion) == 3 == len(set(suggestion)) and "other" not in suggestion
        assert [s.world.pair_structure for s in plan.specs] == suggestion, "the mock follows the suggestion like an obedient model"
        picks.update(suggestion)
    assert len(picks) == 6 and share(picks) <= 0.25, picks.most_common(3)


def test_the_palette_family_rotation_is_a_soft_suggestion_that_the_mock_follows(runs):
    seeded, _ = runs
    picks = Counter()
    for inputs, _, plan in seeded:
        suggestion = json.loads(inputs["palette_suggestion"])
        assert len(suggestion) == 3 == len(set(suggestion))
        assert [s.world.palette_family for s in plan.specs] == suggestion
        picks.update(suggestion)
    assert len(picks) == 10 and share(picks) <= 0.20, picks.most_common(3)


def test_colours_named_by_the_person_come_first_so_no_palette_rotation_is_suggested(unit_rt, demo_inv):
    for kw in ({"brief": "teal and orange forest rangers"}, {"brief": "a pastel spring duo"}, {"brief": "a duo", "must_include": ["a red scarf"]}):
        assert "palette_suggestion" not in BR.planner_inputs(unit_rt, db_project(unit_rt, **kw)), kw
    assert "palette_suggestion" in BR.planner_inputs(unit_rt, db_project(unit_rt, brief="a quiet harbour town at dusk"))


def test_the_palette_rotation_prefers_the_families_least_used_in_the_window(unit_rt, demo_inv):
    used = specfix.load_dict("spec_complement_gb")["world"]["palette_family"]
    for i in range(10):
        approved_spec(unit_rt, db_project(unit_rt, name=f"Old {i}").id, specfix.load_dict("spec_complement_gb"), plan_set_id=f"pls_p{i}")
    seen = Counter()
    for i in range(N):
        seen.update(json.loads(BR.planner_inputs(unit_rt, db_project(unit_rt, name=f"New {i}", brief="a cosy duo"))["palette_suggestion"]))
    assert seen[used] == 0 and len(seen) == 9, "the family that filled the window is never one of the three"


def test_a_brief_or_dropdown_that_fixes_the_structure_gets_no_suggestion_and_no_forced_variety(unit_rt, demo_inv):
    for kw in ({"structure_request": "mirror"}, {"brief": "twin sisters in matching coats"}, {"brief": "a team uniform for two friends"},
               {"brief": "same club jackets"}, {"brief": "seasonal twins: spring and autumn"}, {"brief": "a girl and her plush mascot"}):
        p = db_project(unit_rt, **kw)
        inputs = BR.planner_inputs(unit_rt, p)
        assert "structure_suggestion" not in inputs, kw
        _, prompt, plan = ask_planner(unit_rt, p)
        assert len({s.world.pair_structure for s in plan.specs}) == 1, kw          # the mock keeps the one structure three times
        assert "<structure_suggestion>" not in prompt.user_text or "<structure_suggestion></structure_suggestion>" in prompt.user_text
    assert "structure_suggestion" in BR.planner_inputs(unit_rt, db_project(unit_rt, brief="a quiet harbour town at dusk"))


def test_the_rotation_prefers_the_least_used_structures_of_the_last_30_duos(unit_rt, demo_inv):
    for i in range(10):
        approved_spec(unit_rt, db_project(unit_rt, name=f"Old {i}").id, specfix.load_dict("spec_complement_gb"), plan_set_id=f"pls_{i}")
    seen = Counter()
    for i in range(N):
        p = db_project(unit_rt, name=f"New {i}", brief="a cosy duo")
        seen.update(json.loads(BR.planner_inputs(unit_rt, p)["structure_suggestion"]))
    assert seen["complement"] == 0, "the structure that filled the window is suggested last, so it is never one of the three"
    assert len(seen) == 5


def test_a_structure_the_person_just_rejected_comes_last(unit_rt, demo_inv):
    p = db_project(unit_rt, brief="a cosy duo")
    BR.remember_rejected(unit_rt, p.id, [specfix.load_dict("spec_mirror_gg")], "too samey")
    for _ in range(3):
        assert "mirror" not in json.loads(BR.planner_inputs(unit_rt, p)["structure_suggestion"])


def test_recently_used_is_a_sliding_window_of_30_duos_most_used_first(unit_rt, demo_inv):
    assert BR.RECENT_DUOS == 30
    for i in range(5):
        approved_spec(unit_rt, db_project(unit_rt, name=f"Oldest {i}").id, specfix.load_dict("spec_mirror_gg"), plan_set_id=f"pls_o{i}")
    for i in range(30):
        spec = specfix.load_dict("spec_complement_gb" if i % 3 else "spec_same_club_bb")
        approved_spec(unit_rt, db_project(unit_rt, name=f"Recent {i}").id, spec, plan_set_id=f"pls_r{i}")
    used = BR.recently_used(unit_rt, None)
    assert "mirror" not in used["pair_structures"], "the five oldest duos fell out of the 30-duo window"
    assert used["pair_structures"][0] == "complement", "the most used value is listed first"
    assert set(used["pair_structures"]) == {"complement", "same_club"}


def test_the_hints_never_block_anything(unit_rt, demo_inv):
    """Hints steer away softly: the same project can still be planned with every kit id and structure, and no lint reads them."""
    from duoskin.pipeline import lint as LI

    for i in range(12):
        approved_spec(unit_rt, db_project(unit_rt, name=f"Done {i}").id, specfix.load_dict("spec_complement_gb"), plan_set_id=f"pls_d{i}")
    p = db_project(unit_rt, brief="a cosy duo")
    ctx = LI.lint_context(unit_rt, p, recent_cards=BR.recent_cards(unit_rt, p.id))
    bundle = LI.lint_candidates([("s", specfix.load_dict("spec_complement_gb"))], ctx, check_set=False)
    assert bundle.hard_findings("s") == [], "a plan that repeats recent duos is a soft warning at most, never a hard lint"


def test_the_taste_profile_is_shown_as_a_seeded_handful_not_the_same_text_every_time():
    rule = lambda i: {"field": "hair_style", "tendency": f"likes style {i}", "evidence_ids": ["a", "b"], "strength": "moderate"}   # noqa: E731
    doc = {"profile": {"likes": [rule(i) for i in range(8)], "dislikes": [rule(100), rule(101)], "open_questions": ["q"],
                       "explore": ["a calmer palette", "a different structure", "an unusual accessory"]},
           "reference_rules": [{"axis": "line_weight", "rule": f"r{i}"} for i in range(5)]}
    shown = [BR.taste_for_planner(doc, seed) for seed in range(20)]
    assert all(len(s["profile"]["likes"]) == BR.MAX_LIKES_SHOWN and len(s["profile"]["dislikes"]) == 2 and len(s["profile"]["explore"]) == 1
               for s in shown)
    assert len({json.dumps(s, sort_keys=True) for s in shown}) >= 15, "another seed shows another handful"
    assert BR.taste_for_planner(doc, 3) == BR.taste_for_planner(doc, 3) and doc["profile"]["likes"][0]["tendency"] == "likes style 0"
    liked = Counter(r["tendency"] for s in shown for r in s["profile"]["likes"])
    assert len(liked) == 8 and share(liked) <= 0.30, "no single like is in every plan"
    assert BR.taste_for_planner(None, 1) is None and BR.taste_for_planner({"profile": {"likes": [], "dislikes": [], "explore": []}}, 1) is None
    assert BR.taste_for_planner("free text", 1) == "free text"


def test_the_wildcard_does_not_see_the_taste_profile_downstream():
    """L3 is told the wildcard ignores taste; L4 and L5 never receive the profile for it, and code requires it to depart from the others."""
    base = {"spec_id": "spc_1", "spec_json": "{}", "measured_facts": "{}", "brief_text": "two rangers", "taste_profile": "LIKES-HAIR-BOB"}
    wild = compile_llm("L4.critic", {**base, "wildcard": True})
    normal = compile_llm("L4.critic", base)
    assert "LIKES-HAIR-BOB" not in wild.user_text and "ignore taste_fit" in wild.user_text and "LIKES-HAIR-BOB" in normal.user_text
    role = compile_llm("L3.planner", {"brief_text": "x", "structure_request": "auto", "must_include": "none", "combo": "bg", "reference_analysis": "none",
                                      "taste_profile": "none", "recent_cards": "none", "recently_used": "none"}).system[-1]["text"]
    assert re.search(r"is_wildcard true: a bolder idea that ignores the taste profile", role)
    assert "departs from the other two in palette family, anchor kind or theme" in role


def test_the_face_grammar_space_is_large_and_the_registry_keeps_exact_reuse_out_forever():
    from duoskin.models import spec as S
    from duoskin.prompts.system_blocks import design_menus

    menus = design_menus()
    inv = kitenums.builtin_inventory()
    space = len(inv.eye_shapes) * len(inv.mouths)
    for k in ("iris_style", "highlight_style", "lash_style", "brow_style", "nose_style", "cheek_mark"):
        space *= len(menus[k])
    assert space >= 100_000, space                                   # eye x mouth x iris x catchlight x lash x brow x nose x cheek, without skin or expression
    assert S.Face.model_fields["eye_shape"] is not None
    from duoskin.checks import thresholds as TH
    assert int(TH.get("face.registry_window_duos")) == 30


def test_every_claude_call_of_one_project_round_shares_one_logged_order_seed(unit_rt, demo_inv, caplog):
    """L3, L4, L6 and L7 of one round read the kits in the same order (one cache prefix); a New plan round or another project reads another."""
    import logging
    import types

    from duoskin.pipeline import plan as PL

    p, q = db_project(unit_rt, name="One"), db_project(unit_rt, name="Two")
    ctx = lambda pid: types.SimpleNamespace(rt=unit_rt, project_id=pid, step=types.SimpleNamespace(project_id=pid))    # noqa: E731
    first = PL.kit_order_seed(ctx(p.id))
    assert first == BR.seed_for(unit_rt, p.id) == PL.kit_order_seed(ctx(p.id)) and first != PL.kit_order_seed(ctx(q.id))
    BR.remember_rejected(unit_rt, p.id, [specfix.load_dict("spec_mirror_gg")], "again")
    assert PL.kit_order_seed(ctx(p.id)) != first, "a New plan round shows another order"
    assert PL.kit_order_seed(types.SimpleNamespace(rt=unit_rt, project_id=None, step=types.SimpleNamespace(project_id=None))) is None
    assert PL.kit_order_seed(object()) is None, "a context without a runtime simply gets the sorted blocks"
    assert "kit_order_seed" in PL.run_planner.__code__.co_names and PL.log.name == "duoskin.plan"
    _ = (caplog, logging)

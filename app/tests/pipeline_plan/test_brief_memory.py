"""``pipeline/brief.py``: the planner's inputs (no worked example), the sliding-window hints, the structure log, the avoid list and the
not-buildable / must-include read-backs."""
from __future__ import annotations

import json
import re

import pytest
import specfix
from planhelpers import approved_spec, db_project

from duoskin.pipeline import brief as BR
from duoskin.prompts.llm import compile_llm

pytestmark = pytest.mark.usefixtures("demo_inv")


def two_projects_with_approved_duos(rt):
    """Two finished duos (a complement pair and a same-club pair) and a fresh project."""
    a = db_project(rt, name="First")
    b = db_project(rt, name="Second")
    approved_spec(rt, a.id, specfix.load_dict("spec_complement_gb"), plan_set_id="pls_a")
    approved_spec(rt, b.id, specfix.load_dict("spec_same_club_bb"), plan_set_id="pls_b")
    return db_project(rt, name="Fresh")


def test_structure_choices_start_with_auto_and_have_plain_labels():
    rows = BR.structure_choices()
    assert rows[0] == {"value": "auto", "label": "Let the planner choose"}
    values = [r["value"] for r in rows]
    assert {"complement", "same_club", "mirror", "seasonal_twins", "object_mascot", "leader_chaotic"} <= set(values)
    assert all(r["label"] and r["label"] != r["value"] for r in rows[1:])
    assert BR.valid_structure_request("mirror") and BR.valid_structure_request("auto") and not BR.valid_structure_request("twin_sisters")


def test_combo_words_name_both_characters():
    assert BR.combo_words("bg") == "character A is a boy, character B is a girl"
    assert BR.combo_words("gg") == "character A is a girl, character B is a girl"


def test_planner_inputs_have_no_example_spec_and_use_none_for_empty_slots(unit_rt):
    p = db_project(unit_rt, brief="two friends at a night market", must_include=["a teal bow"])
    inputs = BR.planner_inputs(unit_rt, p)
    assert set(inputs) <= {"brief_text", "structure_request", "must_include", "combo", "reference_analysis", "taste_profile", "recent_cards",
                           "recently_used", "avoid", "replacement", "wildcard_flag", "dropped_reasons", "least_used"}
    assert inputs["reference_analysis"] == "none" and inputs["taste_profile"] == "none" and inputs["recent_cards"] == "none"
    assert inputs["recently_used"] == "none"
    assert inputs["must_include"] == "a teal bow" and inputs["structure_request"] == "auto" and inputs["combo"] == "bg"
    prompt = compile_llm("L3.planner", inputs)
    everything = prompt.user_text + "\n".join(b["text"] for b in prompt.system)
    for key in ('"shared_anchors"', '"contrasts"', '"is_wildcard"', '"palette":', '"hair_kit_ids"'):
        assert key not in prompt.user_text, f"{key} would be a copyable example in the user message"
    assert "<spec" not in prompt.user_text and "specs" not in re.findall(r"<(\w+)>", prompt.user_text)
    assert '"a":{"accessories"' not in everything


def test_recent_cards_and_recently_used_follow_the_last_approved_duos(unit_rt):
    fresh = two_projects_with_approved_duos(unit_rt)
    cards = BR.recent_cards(unit_rt, fresh.id)
    assert len(cards) == 2
    used = BR.recently_used(unit_rt, fresh.id)
    assert set(used) == {"hair_kit_ids", "eye_shapes", "mouth_styles", "palette_families", "fabric_ids", "pair_structures", "anchor_kinds"}
    assert set(used["pair_structures"]) == {"complement", "same_club"}
    assert used["hair_kit_ids"] and used["fabric_ids"]
    inputs = BR.planner_inputs(unit_rt, fresh)
    assert json.loads(inputs["recently_used"])["pair_structures"] == used["pair_structures"]
    assert json.loads(inputs["recent_cards"])


def test_a_project_never_sees_its_own_duo_as_memory(unit_rt):
    p = db_project(unit_rt)
    approved_spec(unit_rt, p.id, specfix.load_dict("spec_mirror_gg"))
    assert BR.recent_cards(unit_rt, p.id) == []
    assert BR.recently_used(unit_rt, p.id)["pair_structures"] == []
    assert BR.recently_used(unit_rt, None)["pair_structures"] == ["mirror"]


def test_only_approved_specs_count_as_memory(unit_rt):
    from duoskin.pipeline import plan as PL

    p, q = db_project(unit_rt), db_project(unit_rt, name="Other")
    PL.new_spec_record(unit_rt, p.id, "pls_x", 0, specfix.load_dict("spec_mirror_gg"), status="shown")
    PL.new_spec_record(unit_rt, p.id, "pls_x", 1, specfix.load_dict("spec_same_club_bb"), status="dropped")
    assert BR.approved_specs(unit_rt, exclude_project=q.id) == []


def test_the_structure_log_and_the_collapse_hint(unit_rt):
    for i in range(BR.STRUCTURE_LOG_MIN):
        p = db_project(unit_rt, name=f"Duo {i}")
        approved_spec(unit_rt, p.id, specfix.load_dict("spec_complement_gb"), plan_set_id=f"pls_{i}")
    assert BR.structure_log(unit_rt)[:3] == ["complement"] * 3
    shares = BR.structure_shares(unit_rt)
    assert shares["complement"] == pytest.approx(1.0)
    assert BR.structure_collapse(unit_rt) == "complement"
    assert "complement" not in BR.least_used_structures(unit_rt)[:2]


def test_the_lru_hint_appears_in_recently_used_when_the_setting_is_on(unit_rt):
    p = db_project(unit_rt)
    unit_rt.update_settings({"planner_structure_lru_hint": True})
    hint = json.loads(BR.planner_inputs(unit_rt, p)["recently_used"])["least_used_structures"]
    assert hint and set(hint) <= set(BR.PAIR_STRUCTURES)
    unit_rt.update_settings({"planner_structure_lru_hint": False})
    assert BR.planner_inputs(unit_rt, p)["recently_used"] == "none"


def test_new_plan_remembers_what_was_rejected_and_why(unit_rt):
    p = db_project(unit_rt)
    spec = specfix.load_dict("spec_mirror_gg")
    assert BR.plan_round(unit_rt, p.id) == 0 and BR.planner_inputs(unit_rt, p)["avoid"] == ""
    n = BR.remember_rejected(unit_rt, p.id, [spec, spec], "too busy")
    assert n == 2 and BR.plan_round(unit_rt, p.id) == 1
    entries = BR.avoid_entries(unit_rt, p.id)
    assert entries[0]["pair_structure"] == "mirror" and entries[0]["reason"] == "too busy"
    assert "world" not in entries[0] and "a" not in entries[0], "the avoid list is a summary, never the whole spec"
    inputs = BR.planner_inputs(unit_rt, p)
    assert "too busy" in inputs["avoid"]
    for _ in range(5):
        BR.remember_rejected(unit_rt, p.id, [spec], "again")
    assert len(BR.avoid_entries(unit_rt, p.id)) == BR.AVOID_KEEP


def test_replacement_inputs_carry_the_wildcard_role_and_the_reasons(unit_rt):
    p = db_project(unit_rt)
    inputs = BR.planner_inputs(unit_rt, p, replacement=True, wildcard=True, dropped_reasons=["lash too close to iris", "text on the bow"])
    assert inputs["replacement"] is True and inputs["wildcard_flag"] == "true"
    assert "lash too close" in inputs["dropped_reasons"]
    text = compile_llm("L3.planner", inputs).user_text
    assert "Return exactly one replacement spec" in text and "is_wildcard true" in text


def test_brief_read_as_lists_the_structures_without_repeats():
    specs = [specfix.load_dict(n) for n in ("spec_complement_gb", "spec_same_club_bb", "spec_complement_gb")]
    assert BR.brief_read_as(specs) == ["complement", "same_club"]


def test_must_include_coverage_needs_the_echo_and_a_path_that_resolves():
    spec = specfix.load_dict("spec_complement_gb")
    cons = [{"text": "a teal bow", "spec_paths": ["/a/accessories/0"]}, {"text": "a red cape", "spec_paths": ["/b/nowhere"]}]
    rows = BR.must_include_coverage(["a teal bow", "a red cape", "silver boots"], cons, spec)
    assert [r["covered"] for r in rows] == [True, False, False]
    assert [r["text"] for r in rows] == ["a teal bow", "a red cape", "silver boots"]


def test_the_plan_estimate_is_a_range_with_a_breakdown(unit_rt):
    p = db_project(unit_rt)
    est = BR.estimate_plan_loop(unit_rt, p)
    assert 0 < est["usd_low"] < est["usd_high"] and est["estimate"] is True
    labels = [r["label"] for r in est["breakdown"]]
    assert any("Planner" in x for x in labels) and any("Concept drafts" in x for x in labels)

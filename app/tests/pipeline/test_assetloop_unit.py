"""The asset loop's pure parts: technique ladders and key routing, per-technique overrides, and the compiled prompts of every lane's templates (the router
test: at most 2 DNA fields from the right character, at most 5 MUST lines, no partner style sheet)."""
from __future__ import annotations

import pytest
from pfix import locked_spec

from duoskin.pipeline import assetloop as AL


def _loop(**kw):
    base = {"part_id": "a.face", "character": "a", "role": "iris", "template_id": "R1.face_part", "slots": {"part": "iris"}, "technique_ladder": "face_part"}
    base.update(kw)
    return AL.AssetLoopSpec(**base)


def test_ladders_follow_the_available_providers():
    full = _loop(available=["openai", "recraft"])
    assert AL.ladder_for(full) == ["R1", "I3", "code_face"]
    no_recraft = _loop(available=["openai"])
    assert AL.ladder_for(no_recraft) == ["I3", "code_face"]
    nothing = _loop(available=["anthropic"])
    assert AL.ladder_for(nothing) == ["code_face"]
    prints = _loop(technique_ladder="print", available=["openai"])
    assert AL.ladder_for(prints) == ["I2"]


def test_every_ladder_names_known_techniques():
    for name, rungs in AL.TECHNIQUE_LADDERS.items():
        assert rungs, name
        assert all(r in AL.TECHNIQUES for r in rungs), name


def test_technique_overrides_change_images_mask_and_slots():
    loop = _loop(images=["a" * 64, "b" * 64], mask="c" * 64, slots={"kit_render": True},
                 overrides={"I4k": AL.TechOverride(images=["d" * 64], mask="", slots={})})
    assert AL.images_of(loop, "I4") == ["a" * 64, "b" * 64] and AL.images_of(loop, "I4k") == ["d" * 64]
    assert AL.mask_of(loop, "I4") == "c" * 64 and AL.mask_of(loop, "I4k") is None
    assert AL.slots_of(loop, "I4") == {"kit_render": True} and AL.slots_of(loop, "I4k") == {}


@pytest.mark.parametrize("technique,slots,character", [
    ("I2", {"print": "top.0", "aspect": "square"}, "a"), ("I2f", {"print": "top.0", "aspect": "tall"}, "b"), ("I2s", {"print": "shoes"}, "a"),
    ("R2", {"print": "top.0", "aspect": "square"}, "a"), ("R1", {"part": "iris"}, "a"), ("R1", {"part": "mouth_open"}, "b"), ("I3", {"part": "lash_upper"}, "a"),
    ("I4", {"kit_render": True}, "a"), ("I4k", {}, "b"), ("I5", {"accessory": 0}, "a"), ("I5f", {"accessory": 0}, "b"),
    ("I5g", {"accessory": 0, "no_crop": True}, "a"), ("I6", {"accessory": 0}, "a"), ("I6f", {"accessory": 0}, "b"),
    ("I10", {"view": "back", "target": "hair"}, "a"), ("I10", {"view": "left", "target": "acc:0"}, "b"),
])
def test_every_lane_prompt_compiles_through_the_router(technique, slots, character, rt):
    from duoskin.pipeline import kits
    from duoskin.prompts import compiler
    from duoskin.prompts.catalog import default_ctx

    spec = locked_spec()
    spec["a"]["bottom"]["shoes"]["motif"] = "tiny gear wheel"
    spec["b"]["bottom"]["shoes"]["motif"] = "small leaf"
    tech = AL.TECHNIQUES[technique]
    cp = compiler.compile(tech.template_id, spec, character, slots, default_ctx(kits.load_context(rt).inventory))
    assert cp.dna_fields is not None and len(cp.dna_fields) <= 2
    assert cp.text and "{" not in cp.text.replace("{{", "")                           # no slot leaked
    assert not any(ch in cp.text for ch in ("None", "undefined"))


def test_the_other_characters_dna_never_reaches_a_prompt(rt):
    from duoskin.pipeline import kits
    from duoskin.prompts import compiler
    from duoskin.prompts.catalog import default_ctx

    spec = locked_spec()
    other_motif = spec["b"]["dna"]["motif_object"]
    cp = compiler.compile("I5.accessory_front", spec, "a", {"accessory": 0}, default_ctx(kits.load_context(rt).inventory))
    assert other_motif.lower() not in cp.text.lower() or other_motif.lower() in spec["a"]["dna"]["motif_object"].lower()


# ---------------------------------------------------------------------------------------------------- Gate B details
def _result(check_id: str, passed: bool, evidence: str):
    from duoskin.checks.model import CheckResult

    return CheckResult(check_id=check_id, kind="hard", passed=passed, evidence=evidence)


def test_the_judge_location_of_a_failing_rule_is_read_from_its_evidence():
    res = [_result("fp_single_feature", False, "fail [lower_left]: two features"), _result("fp_front_view", False, "unsure: cannot tell"),
           _result("fp_orientation", True, "pass [upper_right]: fine")]
    assert AL._fail_locations(res, None) == {"fp_single_feature": "lower_left"}          # only failures, only a stated location


def test_a_failing_hard_verdict_is_confirmed_by_three_votes_before_it_costs_a_candidate(rt, monkeypatch):
    from types import SimpleNamespace

    calls = []

    def fake_gate_b_for(ctx, sha, loop, *, hard_only=False, votes=1, counter=None):
        calls.append((list(loop.gate_b_hard), list(loop.gate_b_soft), votes))
        if votes == 1:
            return [_result("fp_single_feature", False, "fail [whole]: two"), _result("fp_front_view", True, "pass")]
        return [_result("fp_single_feature", True, "pass: two of three say one feature")]          # the majority overrules the single vote

    monkeypatch.setattr(AL, "gate_b_for", fake_gate_b_for)
    loop = AL.AssetLoopSpec(part_id="a.face", character="a", role="iris", template_id="R1.face_part", slots={}, images=[], gate_b_hard=["fp_single_feature", "fp_front_view"],
                            gate_b_soft=[], technique_ladder="face_part", n_drafts=1)
    per = [{"sha": "s" * 64, "hard_ok": True, "score": (1, 1), "fails": []}]
    prev = SimpleNamespace(result={"per": per})
    monkeypatch.setattr(AL, "_prev_outputs", lambda ctx, step_id: ([], prev.result))
    ctx = SimpleNamespace(check_cancel=lambda: None)
    p = SimpleNamespace(from_step="x", loop=loop, technique="I3")
    out = AL.run_gate_b_step(ctx, p, [])
    assert calls == [(["fp_single_feature", "fp_front_view"], [], 1), (["fp_single_feature"], [], 3)]       # the second call asks only the failing rule, with 3 votes
    assert out.result["survivors"] == 1 and out.result["judged"] == 1

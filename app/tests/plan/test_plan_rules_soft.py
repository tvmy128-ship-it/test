"""C1, the SOFT classes (APP_SPEC §3.1, §3.4): taste, colour distance, restraint, novelty. They warn and rank; they never block."""
from __future__ import annotations

import planfix as F
import pytest

from duoskin.checks import plan_rules as P
from duoskin.checks import policy
from duoskin.models import dna as dna_mod
from duoskin.models.spec import DuoSpec

SOFT_METRICS = {
    "structure_colour_rule": "CHK-G0-06", "anchor_colour": "CHK-G0-06", "restraint": "CHK-G0-10-SOFT", "detail_count": "TASTE_DETAIL",
    "adjacent_colour_contrast": "CHK-G0-12", "fabric_vs_world_material": "CHK-G0-12", "accessory_visible_at_phone_size": "CHK-G0-12",
    "kit_hair_pair_iou": "PLN-15", "nearest_past_card": "CHK-G0-12",
}


def test_every_soft_rule_is_registered_soft():
    for metric, check_id in SOFT_METRICS.items():
        assert policy.meta(check_id).kind == "soft", (metric, check_id)
        assert F.result(F.lint(F.load()), metric).kind == "soft", metric


def test_soft_failures_never_block():
    """The same-club fixture warns on restraint, detail count and fabric/material and still passes C1."""
    rep = F.lint(F.load("spec_same_club_bb"))
    assert rep.passed and not rep.blocking and not rep.reviser_findings()
    assert {"restraint", "detail_count", "fabric_vs_world_material"} <= set(F.failed(rep))
    assert {w.metric for w in rep.warnings} == set(F.failed(rep))


def test_soft_findings_are_recorded_but_not_sent_to_the_reviser():
    rep = F.lint(F.load("spec_same_club_bb"))
    assert any(not f.hard for f in rep.findings) and not rep.reviser_findings()


# ---------------------------------------------------------------------------------------------- colour distance (CHK-G0-06)
def test_complement_mains_too_close_only_warn():
    def m(d):
        F.set_hex(d, "b_main", "#2F7A55")                       # close to a_main
    rep = F.mutate("spec_complement_gb", m)
    r = F.result(rep, "structure_colour_rule")
    assert not r.passed and r.kind == "soft" and rep.passed
    assert "a_main and b_main differ by dE2000" in r.evidence


def test_same_club_has_no_main_colour_rule():
    r = F.result(F.lint(F.load("spec_same_club_bb")), "structure_colour_rule")
    assert r.passed and "no main-colour rule" in r.evidence


def test_mirror_wants_swapped_roles_and_twins_want_different_seasons():
    mirror = F.result(F.lint(F.load("spec_mirror_gg")), "structure_colour_rule")
    assert mirror.passed and "a_main vs b_second" in mirror.evidence
    twins = F.result(F.lint(F.load("spec_twins_bg")), "structure_colour_rule")
    assert twins.passed and "season groups" in twins.evidence

    def same_season(d):
        F.set_hex(d, "b_main", "#B5651D")
        F.set_hex(d, "a_main", "#B8541A")
    r = F.result(F.mutate("spec_twins_bg", same_season), "structure_colour_rule")
    assert not r.passed and r.kind == "soft"


@pytest.mark.parametrize("hex_,expected", [("#F5C400", "spring"), ("#8A4B2B", "autumn"), ("#9CC8F0", "summer"), ("#1F3A8A", "winter"),
                                           ("#FAFAFA", "summer"), ("#1B1B22", "winter"), ("#7A8B3B", "autumn")])
def test_season_groups(hex_, expected):
    assert P.season_group(hex_) == expected


def _swap_ref(node, old, new):
    if isinstance(node, dict):
        return {k: _swap_ref(v, old, new) for k, v in node.items()}
    if isinstance(node, list):
        return [_swap_ref(v, old, new) for v in node]
    return new if node == old else node


def test_anchor_colours_that_differ_warn_only():
    def m(d):
        d["a"] = _swap_ref(d["a"], "p5", "p12")                   # A wears teal where B still wears the shared yellow
    rep = F.mutate("spec_complement_gb", m)
    r = F.result(rep, "anchor_colour")
    assert not r.passed and r.kind == "soft" and "/shared_anchors/0" in r.evidence
    assert "anchor_colour" not in F.hard_failed(rep)


def test_a_colour_anchor_worn_by_one_character_only_warns():
    def m(d):
        d["a"] = _swap_ref(d["a"], "p5", "p2")
    r = F.result(F.mutate("spec_complement_gb", m), "anchor_colour")
    assert not r.passed and "not worn on both" in r.evidence


# ---------------------------------------------------------------------------------------------- restraint and detail
def test_three_and_four_accessories_warn_at_different_levels():
    def m(d):
        for i, att in enumerate(("hat", "body_back", "right_shoulder")):
            d["a"]["accessories"].append({**d["a"]["accessories"][0], "kind": "prop", "attachment": att,
                                          "category": {"hat": "hat", "body_back": "back", "right_shoulder": "shoulder"}[att],
                                          "description": f"small prop number {'xyz'[i]}"})
    rep = F.mutate("spec_complement_gb", m)
    r = F.result(rep, "restraint")
    assert not r.passed and r.kind == "soft" and "4 accessories on A" in r.evidence
    assert "restraint" not in F.hard_failed(rep)


def test_maximal_detail_level_overrides_restraint():
    def m(d):
        d["world"]["detail_level"] = "maximal"
        for i, att in enumerate(("hat", "body_back", "right_shoulder")):
            d["a"]["accessories"].append({**d["a"]["accessories"][0], "attachment": att, "kind": "prop",
                                          "category": {"hat": "hat", "body_back": "back", "right_shoulder": "shoulder"}[att],
                                          "description": f"small prop number {'xyz'[i]}"})
    assert F.result(F.mutate("spec_complement_gb", m), "restraint").passed


def test_allover_pattern_overrides_restraint_for_that_character_only():
    def m(d):
        d["a"]["dna"]["colour_plan"] = "allover_pattern"
        d["a"]["top"]["prints"] = d["a"]["top"]["prints"] * 3
        d["b"]["top"]["prints"] = d["b"]["top"]["prints"] * 3
        for p in d["a"]["top"]["prints"] + d["b"]["top"]["prints"]:
            p["scale"] = "large"
    r = F.result(F.mutate("spec_complement_gb", m), "restraint")
    assert not r.passed and "B's top" in r.evidence and "A's top" not in r.evidence


def test_detail_count_follows_the_detail_level_range():
    r = F.result(F.lint(F.load("spec_empty_bb")), "detail_count")
    assert not r.passed and "A has 0 details" in r.evidence and r.kind == "soft"
    assert F.result(F.lint(F.load("spec_complement_gb")), "detail_count").passed


def test_a_small_back_item_warns_at_phone_size():
    def m(d):
        d["a"]["accessories"][0].update(category="back", attachment="body_back", size_class="small")
    r = F.result(F.mutate("spec_complement_gb", m), "accessory_visible_at_phone_size")
    assert not r.passed and r.kind == "soft"


def test_fabric_family_versus_world_material_warns_only():
    def m(d):
        d["world"]["material_family"] = "denim"
    rep = F.mutate("spec_complement_gb", m)
    r = F.result(rep, "fabric_vs_world_material")
    assert not r.passed and rep.passed and "world material is denim" in r.evidence


def test_adjacent_colours_that_nearly_match_warn():
    def m(d):
        d["a"]["bottom"]["base_ref"] = d["a"]["top"]["base_ref"]
    r = F.result(F.mutate("spec_complement_gb", m), "adjacent_colour_contrast")
    assert not r.passed and "top base and bottom base" in r.evidence


# ---------------------------------------------------------------------------------------------- hair IoU and novelty
def test_similar_kit_hair_outlines_warn_with_the_manifest_iou():
    def m(d):
        d["a"]["hair"]["kit_style_id"], d["b"]["hair"]["kit_style_id"] = "hair_short_crop_01", "hair_spiky_05"
    rep = F.mutate("spec_complement_gb", m)
    r = F.result(rep, "kit_hair_pair_iou")
    assert not r.passed and r.kind == "soft" and r.value == pytest.approx(0.9)
    assert "kit_hair_pair_iou" not in F.hard_failed(rep)


def test_hair_custom_pair_is_left_to_gate_two():
    r = F.result(F.lint(F.load("spec_empty_bb")), "kit_hair_pair_iou")
    assert r.passed and "Gate 2" in r.evidence


def test_novelty_is_a_soft_hint_over_recent_cards():
    spec = F.spec_of(F.load())
    card = dna_mod.card_from_spec(spec)
    rep = F.lint(spec, recent_cards=[card])
    r = F.result(rep, "nearest_past_card")
    assert not r.passed and r.kind == "soft" and r.value == 1.0 and rep.passed
    assert F.result(F.lint(spec), "nearest_past_card").value is None
    other = dna_mod.card_from_spec(F.spec_of(F.load("spec_twins_bg")))
    assert F.result(F.lint(spec, recent_cards=[other]), "nearest_past_card").passed


def test_recent_cards_may_be_the_compact_dicts_of_recent_cards_json():
    spec = F.spec_of(F.load())
    compact = dna_mod.recent_cards_json([dna_mod.card_from_spec(spec)], 3)
    assert P.nearest_card_share(spec, compact) == 1.0
    assert P.nearest_card_share(spec, []) is None


def test_there_is_no_cap_quota_or_hard_novelty_rule():
    assert not [r for r in F.lint(F.load()).results if "novelty" in r.metric and r.kind != "soft"]
    assert policy.meta("CHK-G0-12").kind == "soft"


def test_spec_dicts_that_do_not_pass_the_spec_rules_are_still_linted():
    d = F.load()
    d["a"]["dna"]["motif_object"] = ""
    assert isinstance(P.lint_spec(d), P.LintReport)
    assert isinstance(P.lint_spec(DuoSpec.model_validate(d, context={"skip_rules": True})), P.LintReport)

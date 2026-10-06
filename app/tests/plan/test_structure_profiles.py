"""data/structure_profiles.json and the rules built on it (APP_SPEC §3.3, bible §8.1.4b): one row per pair structure."""
from __future__ import annotations

import json

import planfix as F
import pytest

from duoskin.checks import plan_rules as P
from duoskin.models import spec as spec_mod
from duoskin.models.kitenums import current_inventory
from duoskin.prompts.catalog import data_json
from duoskin.prompts.limits import thr
from duoskin.prompts.system_blocks import structure_profiles_block

STRUCTURES = spec_mod.PAIR_STRUCTURES


def test_every_pair_structure_has_one_complete_profile_row():
    prof = data_json("structure_profiles.json")["profiles"]
    assert set(prof) == set(STRUCTURES) and len(STRUCTURES) == 7
    for name, row in prof.items():
        assert {"summary", "colour_rule", "contrast_rule", "cut_rule", "typical_anchor_kinds", "leak_policy"} <= set(row), name
        assert row["colour_rule"]["soft"] is True, f"{name}: colour distance is a warning only"
        assert row["cut_rule"]["top_or_bottom_type_differs"] is True and row["cut_rule"]["min_cut_differences"] == 2, \
            f"{name}: no structure has a garment-cut exception (APP_SPEC §2 S31)"


def test_axis_groups_cover_every_contrast_axis_once():
    groups = data_json("structure_profiles.json")["axis_groups"]
    listed = [ax for axes in groups.values() for ax in axes]
    assert sorted(listed) == sorted(spec_mod.CONTRAST_AXES) and len(listed) == len(set(listed))
    for ax in spec_mod.CONTRAST_AXES:
        assert P.axis_group(ax)


def test_threshold_keys_of_the_profiles_exist_in_the_registry():
    for name, row in data_json("structure_profiles.json")["profiles"].items():
        key = row["colour_rule"].get("threshold_key")
        if key:
            assert thr(key) is not None, name
    file = data_json("structure_profiles.json")
    assert thr(file["partner_only_min_de_key"]) and thr(file["leak_max_share_key"])


def test_anchor_kinds_are_real_anchor_kinds():
    kinds = set(spec_mod.ANCHOR_KINDS)
    for name, row in data_json("structure_profiles.json")["profiles"].items():
        assert set(row["typical_anchor_kinds"]) <= kinds, name


def test_the_planner_sees_exactly_the_file_the_linter_reads():
    block = structure_profiles_block()
    assert block.startswith("<structure_profiles>") and block.endswith("</structure_profiles>")
    body = json.loads(block.split("\n", 1)[1].rsplit("\n", 1)[0])
    assert body == data_json("structure_profiles.json")


def test_unknown_structure_is_a_key_error():
    with pytest.raises(KeyError):
        P.profile("triplets")


def _profile_result(d):
    return F.result(F.lint(d), "structure_profile_contrast")


def tally(counted=(), credits=()):
    return P.ContrastTally(declared=[(i, a) for i, a in enumerate(counted)], counted={a: "x" for a in counted}, unmeasurable=[],
                           duplicates=[], dna_credits={f"dna_{c}": c for c in credits})


def run_profile(d, t):
    R = P._Rep("sha")
    P.rule_profile_contrast(F.spec_of(d), R, t, current_inventory())
    return R.rep.results[0]


# ---------------------------------------------------------------------------------------------- same_club
def test_same_club_needs_four_contrasts_from_hair_face_cut_print_accessory():
    d = F.load("spec_same_club_bb")
    assert _profile_result(d).passed
    weak = tally(["colour_temperature", "value", "fabric", "shape_language", "hair_shape"])
    r = run_profile(d, weak)
    assert not r.passed and r.kind == "hard" and "at least 4 contrasts" in r.evidence
    strong = tally(["hair_shape", "top_type", "face_eyes", "accessory_kind", "fabric"])
    assert run_profile(d, strong).passed


# ---------------------------------------------------------------------------------------------- mirror
def test_mirror_needs_a_different_hair_kit_garment_type_and_accessory_category():
    d = F.load("spec_mirror_gg")
    assert _profile_result(d).passed

    d2 = F.load("spec_mirror_gg")
    d2["b"]["hair"]["kit_style_id"] = d2["a"]["hair"]["kit_style_id"]
    r = _profile_result(d2)
    assert not r.passed and "different hair kit style" in r.evidence and r.kind == "hard"

    d3 = F.load("spec_mirror_gg")
    d3["b"]["top"].update({k: d3["a"]["top"][k] for k in ("recipe_id", "sleeve", "hem", "neckline", "front", "block_layout")})
    d3["b"]["bottom"].update({k: d3["a"]["bottom"][k] for k in ("recipe_id", "leg", "waist")})
    assert "different garment type" in _profile_result(d3).evidence

    d4 = F.load("spec_mirror_gg")
    d4["b"]["accessories"][0].update(category="hat", attachment="hat")
    assert "different accessory category" in _profile_result(d4).evidence


# ---------------------------------------------------------------------------------------------- leader_chaotic
def test_leader_chaotic_needs_expression_or_shape_language():
    d = F.load("spec_empty_bb")
    assert _profile_result(d).passed
    r = run_profile(d, tally(["top_type", "bottom_type", "sleeve_length", "face_eyes", "hair_shape"]))
    assert not r.passed and "expression, shape_language" in r.evidence
    assert run_profile(d, tally(["top_type", "bottom_type", "sleeve_length", "face_eyes", "expression"])).passed
    assert run_profile(d, tally(["top_type", "bottom_type", "sleeve_length", "face_eyes"], credits=["shape_language"])).passed


# ---------------------------------------------------------------------------------------------- seasonal_twins
def test_seasonal_twins_need_four_non_colour_contrasts():
    d = F.load("spec_twins_bg")
    assert _profile_result(d).passed
    r = run_profile(d, tally(["colour_temperature", "value", "top_type", "bottom_type"]))
    assert not r.passed and "at least 4 non-colour" in r.evidence
    assert run_profile(d, tally(["colour_temperature", "top_type", "bottom_type"], credits=["motif_object", "accessory_style"])).passed


# ---------------------------------------------------------------------------------------------- object_mascot
def test_object_mascot_needs_a_linked_prop_or_an_accessory_pair_anchor():
    d = F.load("spec_complement_gb")
    d["world"]["pair_structure"] = "object_mascot"
    r = _profile_result(d)
    assert not r.passed and "linked plush_pet" in r.evidence and r.kind == "hard"
    d["b"]["accessories"][0]["linked_to_partner"] = True
    assert _profile_result(d).passed
    d2 = F.load("spec_complement_gb")
    d2["world"]["pair_structure"] = "object_mascot"
    d2["shared_anchors"][0]["kind"] = "accessory_pair"
    assert _profile_result(d2).passed


def test_a_linked_accessory_pair_must_differ_in_kind_or_category():
    d = F.load("spec_complement_gb")
    d["shared_anchors"][0]["kind"] = "accessory_pair"
    d["a"]["accessories"][0].update(kind="plush_pet", category="shoulder", attachment="left_collar", linked_to_partner=True)
    d["b"]["accessories"][0].update(kind="plush_pet", category="shoulder", attachment="right_collar", linked_to_partner=True,
                                    description="small round hedgehog plush with soft quills")
    r = F.result(F.lint(d), "accessory_complement")
    assert not r.passed and "linked accessory pair must differ" in r.evidence


# ---------------------------------------------------------------------------------------------- other, complement and the cut rule
def test_other_needs_a_structure_note_and_follows_the_complement_rules():
    d = F.load("spec_complement_gb")
    d["world"]["pair_structure"] = "other"
    d["world"]["structure_note"] = ""
    r = F.result(F.lint(d), "structure_note")
    assert not r.passed and r.check_id == "PLN-STR-01" and r.kind == "hard"
    d["world"]["structure_note"] = "two rivals who share a workshop"
    assert F.result(F.lint(d), "structure_note").passed and _profile_result(d).passed


def test_a_structure_note_outside_other_must_be_empty():
    d = F.load("spec_complement_gb")
    d["world"]["structure_note"] = "not allowed here"
    assert not F.result(F.lint(d), "structure_note").passed


@pytest.mark.parametrize("name", F.SPEC_NAMES)
def test_every_fixture_passes_its_own_structure_profile(name):
    assert _profile_result(F.load(name)).passed
    assert F.result(F.lint(F.load(name)), "garment_cut").passed

"""C1, the HARD classes (APP_SPEC §3.1): Roblox, buildability, IP and stray text, registry and integrity (bible §9.4, FAILURE_MODES §7.2).

Every rule has a pass case (the fixtures) and a fail case (one mutation), and the failing rule must be the one that is blocking.
"""
from __future__ import annotations

import json
from pathlib import Path

import planfix as F
import pytest

from duoskin.checks import plan_rules as P
from duoskin.checks import policy

BANNED = json.loads((Path(P.__file__).resolve().parents[1] / "data" / "banned_terms.json").read_text(encoding="utf-8"))["groups"]


@pytest.mark.parametrize("name", F.SPEC_NAMES)
def test_every_fixture_passes_every_hard_rule(name):
    rep = F.lint(F.load(name))
    assert rep.passed, [(r.check_id, r.metric, r.evidence) for r in rep.blocking]
    assert not rep.reviser_findings()


def test_a_clean_spec_has_no_warning_at_all():
    rep = F.lint(F.load("spec_complement_gb"))
    assert rep.warnings == [] and F.failed(rep) == []


def test_the_kind_of_every_result_comes_from_checks_json():
    for name in F.SPEC_NAMES:
        for r in F.lint(F.load(name)).results:
            assert r.kind == policy.meta(r.check_id).kind, (r.check_id, r.metric)


def test_the_report_has_one_result_per_rule_and_no_duplicates():
    rep = F.lint(F.load())
    keys = [(r.check_id, r.metric) for r in rep.results]
    assert len(keys) == len(set(keys)) >= 30


def _assert_blocks(rep: P.LintReport, metric: str, check_id: str, *, path: str | None = None) -> None:
    r = F.result(rep, metric)
    assert not r.passed and r.check_id == check_id and r.kind == "hard", (r.kind, r.evidence)
    assert not rep.passed and metric in F.hard_failed(rep)
    assert rep.reviser_findings(), "a HARD failure must reach the reviser as a numbered finding"
    if path is not None:
        assert any(f["path"] == path or f["path"].startswith(path) for f in rep.reviser_findings()), rep.reviser_findings()


# ----------------------------------------------------------------------------------------------- palette and integrity (CHK-G0-09)
def test_duplicate_palette_id_fails():
    def m(d):
        d["palette"][1]["id"] = d["palette"][0]["id"]
    _assert_blocks(F.mutate("spec_complement_gb", m), "palette_integrity", "CHK-G0-09")


def test_dangling_palette_ref_fails_with_a_path():
    def m(d):
        d["a"]["top"]["base_ref"] = "p99"
    _assert_blocks(F.mutate("spec_complement_gb", m), "palette_integrity", "CHK-G0-09", path="/a/top/base_ref")


def test_bad_hex_fails():
    def m(d):
        d["palette"][0]["hex"] = "#2F6B4"
    _assert_blocks(F.mutate("spec_complement_gb", m), "palette_integrity", "CHK-G0-09")


def test_blush_ref_must_match_cheek_mark():
    def m(d):
        d["b"]["face"]["blush_ref"] = d["b"]["face"]["iris_ref"]       # cheek_mark is none for B
    _assert_blocks(F.mutate("spec_complement_gb", m), "blush_and_legwear_refs", "CHK-G0-09", path="/b/face/blush_ref")


def test_lash_colour_too_close_to_the_iris_fails():
    def m(d):
        d["a"]["face"]["lash_ref"] = d["a"]["face"]["iris_ref"]
    rep = F.mutate("spec_complement_gb", m)
    _assert_blocks(rep, "lash_vs_iris", "CHK-G0-09", path="/a/face/lash_ref")
    assert F.result(rep, "lash_vs_iris").value == 0.0


def test_the_pupil_does_not_count_for_the_lash_rule():
    def m(d):
        d["a"]["face"]["pupil_ref"] = d["a"]["face"]["lash_ref"]
    assert F.result(F.mutate("spec_complement_gb", m), "lash_vs_iris").passed


# ----------------------------------------------------------------------------------------------- free text (CHK-G0-08)
@pytest.mark.parametrize("group", sorted(set(BANNED) - {"text_inviting"}))
def test_every_banned_group_blocks_in_a_slot_source_field(group):
    term = BANNED[group][0]
    d = F.load()
    d["a"]["dna"]["motif_object"] = term
    rep = F.lint(d)
    _assert_blocks(rep, "free_text", "CHK-G0-08", path="/a/dna/motif_object")


def test_text_inviting_words_block_in_a_print_motif():
    d = F.load()
    d["a"]["top"]["prints"][0]["motif"] = "a round logo"
    _assert_blocks(F.lint(d), "free_text", "CHK-G0-08", path="/a/top/prints/0/motif")


@pytest.mark.parametrize("bad", ["a leaf with 3 veins", "the letter A", "no stripes", "a leaf #FF0000", 'a "quoted" leaf'])
def test_strict_slot_lint_rejects_digits_letters_negation_hex_and_quotes(bad):
    d = F.load()
    d["a"]["top"]["prints"][0]["motif"] = bad
    _assert_blocks(F.lint(d), "free_text", "CHK-G0-08")


def test_word_cap_blocks():
    d = F.load()
    d["world"]["theme"] = "one two three four five six seven eight nine ten"
    _assert_blocks(F.lint(d), "word_caps", "CHK-G0-08", path="/world/theme")


def test_story_gets_the_light_lint_only():
    d = F.load()
    d["world"]["story"] = "Two rangers walk 3 miles."                      # digits are fine in metadata
    assert F.result(F.lint(d), "free_text").passed
    d["world"]["story"] = "Two rangers meet pikachu."
    _assert_blocks(F.lint(d), "free_text", "CHK-G0-08")


# ----------------------------------------------------------------------------------------------- kits and buildability (CHK-G0-01, 02, 05)
def test_recipe_cannot_draw_the_chosen_cut():
    def m(d):
        d["a"]["top"]["recipe_id"] = "hoodie"                            # a hoodie has long sleeves and a hood only
    _assert_blocks(F.mutate("spec_complement_gb", m), "kit_ids_and_compatibility", "CHK-G0-01", path="/a/top/sleeve")


def test_crop_top_requires_a_high_waist():
    def m(d):
        d["a"]["top"].update(recipe_id="crop_top", sleeve="short", hem="crop", neckline="crew", block_layout="solid")
        d["a"]["bottom"]["waist"] = "mid"
    _assert_blocks(F.mutate("spec_complement_gb", m), "kit_ids_and_compatibility", "CHK-G0-01", path="/a/bottom/waist")


def test_inner_layer_needs_an_open_front():
    def m(d):
        d["a"]["top"]["inner_recipe_id"] = "tee"
    _assert_blocks(F.mutate("spec_complement_gb", m), "kit_ids_and_compatibility", "CHK-G0-01", path="/a/top/inner_recipe_id")


def test_hair_custom_needs_a_description_and_cannot_use_a_kit_fringe():
    def m(d):
        d["a"]["hair"].update(kit_style_id="hair_custom", fringe_id="kit_default", description="")
    rep = F.mutate("spec_complement_gb", m)
    _assert_blocks(rep, "kit_ids_and_compatibility", "CHK-G0-01")
    assert len(rep.reviser_findings()) >= 2


def test_unknown_kit_id_is_a_schema_error_not_a_lint_result():
    d = F.load()
    d["a"]["top"]["recipe_id"] = "jeans_straight"
    with pytest.raises(ValueError, match="unknown TopRecipeKit id"):
        F.spec_of(d)


def test_accessory_attachment_must_fit_the_category():
    def m(d):
        d["a"]["accessories"][0]["attachment"] = "hat"                   # a waist bag cannot sit on the head
    rep = F.mutate("spec_complement_gb", m)
    _assert_blocks(rep, "slot_attachment", "CHK-G0-02", path="/a/accessories/0/attachment")
    _assert_blocks(rep, "category_policy", "CHK-G0-02")


def test_two_accessories_on_one_attachment_fail():
    def m(d):
        d["a"]["accessories"].append({**d["a"]["accessories"][0], "kind": "prop", "description": "small leaf lantern"})
    _assert_blocks(F.mutate("spec_complement_gb", m), "attachment_unique", "CHK-G0-02")


def test_a_large_item_does_not_fit_a_face_box():
    def m(d):
        d["a"]["accessories"][0].update(category="face", attachment="face_front", size_class="large", kind="prop")
    _assert_blocks(F.mutate("spec_complement_gb", m), "size_class_box", "CHK-G0-02", path="/a/accessories/0/size_class")


def test_a_shoulder_item_cannot_attach_to_the_head():
    def m(d):
        d["b"]["accessories"][0].update(category="shoulder", attachment="hat")
    rep = F.mutate("spec_complement_gb", m)
    assert not F.result(rep, "slot_attachment").passed and not rep.passed


def test_complete_hair_category_is_only_for_hairstyles():
    def m(d):
        d["a"]["accessories"][0].update(category="hair", attachment="hair", kind="bag")
    _assert_blocks(F.mutate("spec_complement_gb", m), "category_policy", "CHK-G0-02")


def test_makeup_is_unavailable_while_the_kit_says_so():
    def m(d):
        d["a"]["makeup"] = {"kind": "freckles", "description": "light freckles across the nose", "colour_refs": ["p3"]}
    _assert_blocks(F.mutate("spec_complement_gb", m), "makeup_routing", "CHK-G0-02", path="/a/makeup/kind")


def test_sparkle_star_follows_the_settings_switch():
    def m(d):
        d["a"]["face"]["highlight_style"] = "sparkle_star"
    assert F.result(F.mutate("spec_complement_gb", m), "head_texture_allow_list").passed
    _assert_blocks(F.mutate("spec_complement_gb", m, sparkle_star_allowed=False), "head_texture_allow_list", "CHK-G0-02",
                   path="/a/face/highlight_style")


def test_same_garments_fail_the_cut_rule_for_every_structure():
    for name in ("spec_complement_gb", "spec_same_club_bb"):
        d = F.load(name)
        d["b"]["top"].update({k: d["a"]["top"][k] for k in ("recipe_id", "sleeve", "hem", "neckline", "front", "block_layout")})
        d["b"]["bottom"].update({k: d["a"]["bottom"][k] for k in ("recipe_id", "leg", "waist")})
        _assert_blocks(F.lint(d), "garment_cut", "CHK-G0-05")


def _near_twin_cut(d):
    d["b"]["top"].update(recipe_id="tee_long", sleeve="three_quarter", hem="waist_tucked", neckline="crew", front="closed", block_layout="solid")
    d["b"]["bottom"].update(recipe_id="shorts", leg="above_knee", waist="mid")      # different garment families, one cut attribute apart


def test_one_cut_difference_is_not_enough_and_two_pass():
    d = F.load()
    _near_twin_cut(d)
    rep = F.lint(d)
    _assert_blocks(rep, "garment_cut", "CHK-G0-05", path="/b/top")
    assert F.result(rep, "garment_cut").value == 1
    d["b"]["top"]["neckline"] = "v_neck"
    ok = F.result(F.lint(d), "garment_cut")
    assert ok.passed and ok.value == 2


# ----------------------------------------------------------------------------------------------- the duo contract (CHK-G0-03/04, PLN-DNA-01)
def test_combo_must_match_the_presentations():
    def m(d):
        d["a"]["presentation"] = "boy"
    _assert_blocks(F.mutate("spec_complement_gb", m), "combo_presentation", "CHK-G0-03", path="/combo")


def test_fewer_than_five_distinct_contrast_axes_fail():
    def m(d):
        d["contrasts"] = d["contrasts"][:3]
    rep = F.mutate("spec_complement_gb", m)
    _assert_blocks(rep, "contrast_axes_distinct", "CHK-G0-03")


def test_a_repeated_axis_fails():
    def m(d):
        d["contrasts"].append({**d["contrasts"][0]})
    _assert_blocks(F.mutate("spec_complement_gb", m), "contrast_axes_distinct", "CHK-G0-03", path="/contrasts/")


def test_an_unmeasurable_contrast_does_not_count():
    def m(d):
        for c in d["contrasts"]:
            if c["axis"] == "bottom_type":
                c["axis"] = "fabric"                                    # both characters use canvas: nothing to measure
    rep = F.mutate("spec_complement_gb", m)
    assert "fabric" in F.result(rep, "contrasts_measurable").evidence


def test_a_declared_contrast_that_is_not_true_in_the_spec_does_not_count():
    def m(d):
        d["b"]["bottom"]["recipe_id"] = d["a"]["bottom"]["recipe_id"]
    rep = F.mutate("spec_complement_gb", m)
    assert "bottom_type" in F.result(rep, "contrasts_measurable").evidence


def test_same_club_allows_one_colour_axis_and_complement_two():
    d = F.load("spec_same_club_bb")
    d["contrasts"] += [{"axis": "colour_temperature", "a_value": "warm", "b_value": "cool"}, {"axis": "value", "a_value": "light", "b_value": "dark"}]
    F.set_hex(d, "a_main", "#F5C400")
    F.set_hex(d, "b_main", "#1F3A8A")
    rep = F.lint(d)
    r = F.result(rep, "colour_axes")
    _assert_blocks(rep, "colour_axes", "CHK-G0-03")
    assert r.value == 2 and "same_club" in r.threshold
    d["contrasts"] = [c for c in d["contrasts"] if c["axis"] != "value"]
    assert F.result(F.lint(d), "colour_axes").passed


def test_anchor_count_is_one_or_two():
    def m(d):
        d["shared_anchors"] = []
    _assert_blocks(F.mutate("spec_complement_gb", m), "anchors", "CHK-G0-03")
    d = F.load()
    d["shared_anchors"] = d["shared_anchors"] * 2
    assert not F.result(F.lint(d), "anchors").passed


def test_an_anchor_must_show_on_both_characters():
    def m(d):
        d["shared_anchors"][0]["on_b"] = ""
    _assert_blocks(F.mutate("spec_complement_gb", m), "anchors", "CHK-G0-03", path="/shared_anchors/0")


def test_face_grammar_needs_three_differences():
    def m(d):
        b = d["b"]["face"]
        for k in ("eye_shape", "iris_style", "highlight_style", "lash_style", "brow_style", "mouth_style"):
            b[k] = d["a"]["face"][k]
    rep = F.mutate("spec_complement_gb", m)
    _assert_blocks(rep, "face_grammar_difference", "CHK-G0-03", path="/b/face")


def test_same_kit_hair_fails_and_two_custom_hairs_need_a_declared_contrast():
    def m(d):
        d["b"]["hair"]["kit_style_id"] = d["a"]["hair"]["kit_style_id"]
    _assert_blocks(F.mutate("spec_complement_gb", m), "hair_pairing", "CHK-G0-03", path="/b/hair/kit_style_id")
    assert F.result(F.lint(F.load("spec_empty_bb")), "hair_pairing").passed          # the fixture declares hair_shape and differs

    def m2(d):
        d["contrasts"] = [c for c in d["contrasts"] if c["axis"] not in ("hair_shape", "hair_length")]
    _assert_blocks(F.mutate("spec_empty_bb", m2), "hair_pairing", "CHK-G0-03")


def test_b_cannot_repeat_a_accessory():
    def m(d):
        d["b"]["accessories"][0] = {**d["a"]["accessories"][0], "attachment": "body_front"}
    _assert_blocks(F.mutate("spec_complement_gb", m), "accessory_complement", "CHK-G0-03", path="/b/accessories/0")


def test_dna_difference_needs_two_character_fields():
    def m(d):
        a, b = d["a"]["dna"], d["b"]["dna"]
        for k in ("shape_language", "colour_plan", "focal_location", "motif_object", "accessory_style"):
            b[k] = a[k]
        d["b"]["hair"]["kit_style_id"] = "hair_buns_04"
    rep = F.mutate("spec_complement_gb", m)
    _assert_blocks(rep, "dna_character_difference", "PLN-DNA-01", path="/b/dna")
    assert F.result(rep, "dna_character_difference").value <= 1


def test_energy_alone_never_counts_as_character_difference():
    def m(d):
        a, b = d["a"]["dna"], d["b"]["dna"]
        for k in ("shape_language", "colour_plan", "focal_location", "motif_object", "accessory_style"):
            b[k] = a[k]
        b["energy"] = "calm"
        d["b"]["hair"]["kit_style_id"] = d["a"]["hair"]["kit_style_id"]
    rep = F.mutate("spec_complement_gb", m)
    assert "energy" not in F.result(rep, "dna_character_difference").evidence.replace("differing: none", "")


def test_palette_size_is_hard():
    def m(d):
        d["palette"] = d["palette"][:8]
        d["a"]["top"]["base_ref"] = "p1"
    rep = F.mutate("spec_complement_gb", m)
    _assert_blocks(rep, "palette_integrity", "CHK-G0-09")


# ----------------------------------------------------------------------------------------------- the registry (A_REGISTRY)
def test_exact_reuse_of_a_registered_file_is_hard():
    rep = F.lint(F.load(), registry_lookup=lambda spec: ["print:ab12cd"])
    _assert_blocks(rep, "registry_reuse", "A_REGISTRY")
    assert "print:ab12cd" in F.result(rep, "registry_reuse").evidence


def test_no_lookup_means_no_reuse():
    assert F.result(F.lint(F.load()), "registry_reuse").passed
    assert F.result(F.lint(F.load(), registry_lookup=lambda s: []), "registry_reuse").passed


# ----------------------------------------------------------------------------------------------- findings and fail-closed
def test_findings_are_numbered_from_one_and_hard_only():
    def m(d):
        d["a"]["top"]["base_ref"] = "p99"
        d["b"]["hair"]["kit_style_id"] = d["a"]["hair"]["kit_style_id"]
    rep = F.mutate("spec_complement_gb", m)
    nums = [f["number"] for f in rep.reviser_findings()]
    assert nums == list(range(1, len(nums) + 1)) and len(nums) >= 2
    for f in rep.reviser_findings():
        assert f["path"].startswith("/") and f["problem"] and f["rule"]


def test_a_dict_with_every_problem_is_linted_without_raising():
    d = F.load()
    d["palette"] = []
    d["a"]["top"]["base_ref"] = "p1"
    rep = P.lint_spec(d)
    assert not rep.passed


@pytest.mark.parametrize("description", ["tiny charm on a short chain", "bag with a long strap", "round ring with a hole", "spiky plush star", "charm with a dangling bead"])
def test_a_tripo_accessory_that_names_a_thin_part_is_blocked_before_any_image_is_paid_for(description):
    """Gate B ac_no_thin_parts rejects thin strings, chains, rings, holes and spikes, so the plan must not ask for them (code adds straps and rings)."""
    def m(d):
        d["a"]["accessories"][0]["description"] = description
    _assert_blocks(F.mutate("spec_complement_gb", m), "accessory_thin_parts", "CHK-G0-02", path="/a/accessories/0/description")


def test_thin_part_words_are_fine_for_code_built_accessories_and_for_solid_descriptions():
    def code_built(d):
        d["a"]["accessories"][0].update(build="code_primitive", description="thin strap across the chest")
    assert F.result(F.mutate("spec_complement_gb", code_built), "accessory_thin_parts").passed
    for name in F.SPEC_NAMES:
        assert F.result(F.lint(F.load(name)), "accessory_thin_parts").passed, name
    def keychain(d):
        d["a"]["accessories"][0]["description"] = "keychain charm shaped like a round bell, springlike curls"
    assert F.result(F.mutate("spec_complement_gb", keychain), "accessory_thin_parts").passed, "'keychain' and 'springlike' are not thin-part words"

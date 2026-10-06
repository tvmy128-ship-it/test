"""prompts/slots.py helpers, prompts/limits.py (the threshold reader with marked fallbacks) and catalog.CompileCtx."""
from __future__ import annotations

import pytest

from duoskin.checks import thresholds as TH
from duoskin.models.spec import DuoSpec
from duoskin.prompts import limits, registry, slots
from duoskin.prompts.catalog import default_ctx


# ---------------------------------------------------------------------------------------------- limits
def test_the_registry_wins_over_a_fallback(monkeypatch):
    assert limits.thr("con.partner_only_de") == TH.get("con.partner_only_de")
    monkeypatch.setitem(limits.FALLBACKS, "con.partner_only_de", (999, "SPEC", ["X"]))
    assert limits.thr("con.partner_only_de") == TH.get("con.partner_only_de") != 999


def test_a_fallback_is_used_only_when_the_registry_lacks_the_key():
    for key, (value, status, fm_ids) in limits.FALLBACKS.items():
        if key in limits.missing_keys():
            assert limits.thr(key) == value and limits.status_of(key) == status and limits.fm_ids_of(key) == fm_ids
            assert "fallback" in limits.describe(key, "<=")


def test_an_unknown_key_with_no_fallback_is_an_error():
    with pytest.raises(TH.UnknownThreshold):
        limits.thr("no.such_key")
    with pytest.raises(TH.UnknownThreshold):
        limits.thr("taste.silhouette_iou_warn")         # the real key is duo.silhouette_iou_cold


def test_keys_the_checks_read_resolve():
    for key in ("pln.contrasts_min", "pln.anchors", "pln.face_features_diff_min", "pln.lash_iris_de_min", "pln.dna_char_diff_min",
                "pln.acc_per_char_warn_hard", "pln.adjacent_de_min", "pln.kit_hair_iou_warn", "pln.contrast_colour_de", "pln.anchor_colour_de_max",
                "con.partner_only_de", "duo.silhouette_iou_cold", "duo.thumb_height_px", "taste.acc_min_px", "taste.layout_ari_warn",
                "taste.plan_ratio_share_off", "vlm.costly_votes", "prm.must_max", "prm.dna_fields_max", "prm.max_chars_excl_style",
                "prm.max_chars_total", "prm.exclude_nouns_max", "tpl.shoe_top_row_range", "img.palette_de_max"):
        assert limits.thr(key) is not None, key


def test_the_prompt_limits_are_the_specified_numbers():
    assert limits.thr("prm.must_max") == 5 and limits.thr("prm.dna_fields_max") == 2
    assert limits.thr("pln.dna_char_diff_min") == 2 and limits.thr("pln.contrasts_min") == 5


def test_the_fallbacks_that_remain_are_listed_for_the_owner_of_thresholds_py():
    assert set(limits.missing_keys()) <= set(limits.FALLBACKS) and "taste.plan_ratio_share_off" in limits.FALLBACKS


# ---------------------------------------------------------------------------------------------- slot helpers
def test_fit_clauses_drops_the_least_important_whole_clause_first():
    out = slots.fit_clauses([(0, "one two three"), (2, "four five six"), (1, "seven eight")], 5)
    assert out == "one two three, seven eight"
    assert slots.fit_clauses([(0, "a b"), (1, "c d")], 10) == "a b, c d"


def test_fit_clauses_never_drops_the_most_important_clause_and_cuts_it_at_a_comma_then_a_word():
    assert slots.fit_clauses([(0, "alpha beta, gamma delta epsilon")], 3) == "alpha beta"
    assert slots.fit_clauses([(0, "one two three four five")], 3) == "one two three"
    assert slots.fit_clauses([(1, ""), (0, "kept")], 5) == "kept"
    assert slots.fit_clauses([], 5) == ""


def test_dedupe_drops_a_clause_that_another_clause_already_contains():
    out = slots._dedupe([(0, "chin-length bob"), (1, "a chin-length bob with a fringe")])
    assert out == [(1, "a chin-length bob with a fringe")]
    assert slots._dedupe([(0, "same"), (1, "same")]) == [(0, "same")]
    assert slots._dedupe([(0, "x"), (1, "")]) == [(0, "x")]


def test_lower_first_and_a_missing_palette_id_are_handled():
    assert slots.lower_first("Make it") == "make it" and slots.lower_first("") == ""
    spec = DuoSpec.model_validate(__import__("specfix").load_dict("spec_complement_gb"), context={"skip_rules": True})
    assert slots.palette_hex(spec, "p1") == "#2F6B4F"
    with pytest.raises(slots.PromptBuildError):
        slots.palette_hex(spec, "p99")
    assert slots.role_hex(spec, "a_main") == "#2F6B4F" and slots.role_hex(spec, "nope") is None


def test_every_builder_named_in_a_template_exists_and_degrade_levels_are_bounded():
    for tid, t in registry.all_templates().items():
        if t.meta.kind in ("image", "text"):
            assert t.meta.builder in slots.BUILDERS, tid
            _, top = slots.BUILDERS[t.meta.builder]
            assert 0 <= top <= 3


def test_colour_names_are_dictionary_words_never_hex_and_distinct(specs):
    ctx = default_ctx()
    names = ctx.names_for(["#2F6B4F", "#D9822B", "#F1E6CC"], 3)
    assert len(set(names)) == 3 and all(n.replace(" ", "").isalpha() for n in names)
    assert ctx.names_for(["#2F6B4F"], 1) == [names[0]]


def test_the_ctx_exposes_the_installed_inventory_and_blocks_by_name(demo_kit_inventory):
    ctx = default_ctx()
    assert ctx.inventory is demo_kit_inventory or ctx.inventory == demo_kit_inventory
    assert ctx.style_block("HOUSE_STYLE_2D").startswith("Clean cel-shaded")
    assert ctx.phrases.version >= 1

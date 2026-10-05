"""The data files of track S (duoskin/data): phrase coverage for every spec enum, style guide, rules.json and the season map."""
from __future__ import annotations

import json
import re
import typing

import pytest

from duoskin.models import spec as S
from duoskin.prompts import catalog
from duoskin.prompts.catalog import TEXT_INVITING, Phrases, data_json, default_ctx

PH = Phrases(data_json("phrases.json"))


def literal_values(annotated) -> tuple[str, ...]:
    if typing.get_origin(annotated) is typing.Literal:
        return typing.get_args(annotated)
    for a in typing.get_args(annotated):
        if typing.get_origin(a) is typing.Literal:
            return typing.get_args(a)
    raise AssertionError(f"{annotated} is not an E(...) enum")


# ---------------------------------------------------------------------------------------------- phrases
@pytest.mark.parametrize("enum,form", [(S.ShapeLanguage, f) for f in ("line", "short")])
def test_every_shape_language_has_each_phrase_form(enum, form):
    for v in literal_values(enum):
        assert PH.get("dna", "shape_language", form, v).strip(), (v, form)


def test_every_detail_level_has_each_phrase_form():
    for v in literal_values(S.DetailLevel):
        for form in ("line", "short", "clause"):
            PH.section("dna", "detail_level", form)          # the table exists
        assert PH.get("dna", "detail_level", "line", v).strip() and PH.get("dna", "detail_level", "short", v) is not None


def test_every_accessory_kind_attachment_and_material_has_a_phrase():
    for kind in literal_values(S.Accessory.model_fields["kind"].annotation):
        assert PH.get("item_noun", kind) and PH.get("object_noun", kind)
    for att in S.ACCESSORY_ATTACHMENTS:
        assert PH.section("attachment_phrase").get(att) is not None, att
    for mat in literal_values(S.Accessory.model_fields["material"].annotation):
        assert PH.get("material_phrase", mat), mat


def test_every_print_region_seen_in_front_or_back_concept_views_has_a_placement_and_every_scale_a_size_word():
    """Regions that face front or back have a placement phrase; top, bottom and inner-side regions are not visible in the concept views."""
    regions = literal_values(S.Print.model_fields["region"].annotation)
    top, bottom = PH.section("print", "placement_top"), PH.section("print", "placement_bottom")
    visible = {"torso_f", "torso_b", "rlimb_f", "rlimb_b", "rlimb_r", "llimb_f", "llimb_b", "llimb_l"}
    assert visible <= set(regions) and visible == set(top) == set(bottom)
    for scale in literal_values(S.Print.model_fields["scale"].annotation):
        assert PH.section("print", "size_word").get(scale) is not None, scale


def test_cut_words_cover_every_cut_value_that_a_prompt_names():
    cut = PH.section("cut_words")
    for group, field_name, owner in (("top.sleeve", "sleeve", S.Top), ("top.hem", "hem", S.Top), ("top.neckline", "neckline", S.Top),
                                     ("top.front", "front", S.Top), ("bottom.leg", "leg", S.Bottom), ("bottom.waist", "waist", S.Bottom)):
        for v in literal_values(owner.model_fields[field_name].annotation):
            assert v in cut[group], (group, v)


def test_hair_parting_and_presentation_phrases_exist():
    for v in literal_values(S.Hair.model_fields["parting"].annotation):
        assert (PH.get("hair", "parting_phrase", v) == "") == (v == "none"), v          # no parting, no phrase
    for v in ("present", "absent"):
        assert PH.get("hair", "fringe_phrase", v)
    for presentation in ("boy", "girl"):
        assert PH.get("presentation_style", presentation)


def test_every_face_grammar_value_has_a_clause():
    face = PH.section("face")
    for field_name, table in (("eye_shape", None), ("iris_style", "iris_phrase"), ("mouth_style", None)):
        if table:
            for v in literal_values(S.Face.model_fields[field_name].annotation):
                assert v in face[table], (table, v)
    for v in literal_values(S.Face.model_fields["lash_style"].annotation):
        assert v in PH.section("r1", "flick_clause") or v in PH.section("r1", "lid_flick_clause") or v == "clean_line", v
    for v in literal_values(S.Face.model_fields["brow_style"].annotation):
        assert v in PH.section("r1", "brow_clause"), v


def test_view_direction_and_panel_phrases_exist():
    for v in ("back", "left", "right"):
        assert PH.get("view_phrase", v) and PH.get("own_view_phrase", v) and PH.get("direction_rule", v)
    for v in ("torso_f", "torso_b", "torso_side", "limb_face_shirt", "limb_face_pants"):
        assert PH.get("panel_phrase", v)
    for v in ("concept", "face_part", "print", "hair", "accessory", "badge", "fabric", "shading"):
        assert PH.get("asset_noun", v)


def test_a_missing_phrase_raises_instead_of_rendering_none():
    with pytest.raises(catalog.PhraseError):
        PH.get("dna", "shape_language", "line", "zigzag")
    with pytest.raises(catalog.PhraseError):
        PH.get("dna", "shape_language")             # a table, not a phrase


def test_fixed_phrases_pass_the_banned_lint_outside_the_exclude_line():
    """The phrase maps are fixed human-written text; none may carry a banned word (the EXCLUDE tables name them on purpose)."""
    b = default_ctx().banned
    exempt = {("i3", "exclude_base"), ("i3", "exclude_drop")}

    def leaves(node, path=()):
        if isinstance(node, str):
            yield path, node
        elif isinstance(node, dict):
            for k, v in node.items():
                yield from leaves(v, (*path, k))
        elif isinstance(node, list):
            for i, v in enumerate(node):
                yield from leaves(v, (*path, str(i)))

    for path, text in leaves(data_json("phrases.json")):
        if tuple(path[:2]) in exempt:
            continue
        assert not b.hits(text, [*b.everywhere, TEXT_INVITING]), (path, text)
        assert not re.search(r"#[0-9A-Fa-f]{3,8}\b", text), (path, text)


def test_kit_defaults_cover_every_builtin_recipe_and_shoe():
    from duoskin.models.kitenums import builtin_inventory

    inv = builtin_inventory()
    defaults = PH.section("kit_defaults")
    assert set(defaults) == {"recipe_phrase", "shoe_phrase"}
    for rid in inv.recipes:
        assert defaults["recipe_phrase"].get(rid, "").strip(), rid
    for sid in inv.shoes:
        assert defaults["shoe_phrase"].get(sid, "").strip(), sid


# ---------------------------------------------------------------------------------------------- style guide
def test_style_guide_has_both_verbatim_blocks_and_the_parameters():
    sg = data_json("style_guide.json")
    assert set(sg["blocks"]) == {"HOUSE_STYLE_2D", "HOUSE_STYLE_3D_INPUT"}
    ctx = default_ctx()
    assert ctx.style_block("HOUSE_STYLE_2D") == sg["blocks"]["HOUSE_STYLE_2D"] and ctx.style_block("none") == ""
    with pytest.raises(catalog.PhraseError):
        ctx.style_block("HOUSE_STYLE_9")
    assert sg["detail_density"] == {"minimal": [1, 2], "standard": [2, 3], "maximal": [4, 5]}
    assert "blocks" not in ctx.style.parameters() and ctx.style.version >= 1


def test_style_blocks_name_no_banned_term_and_no_hex():
    b = default_ctx().banned
    for name, text in data_json("style_guide.json")["blocks"].items():
        assert not b.hits(text, b.everywhere) and not re.search(r"#[0-9A-Fa-f]{3,8}\b", text), name


# ---------------------------------------------------------------------------------------------- rules.json and the season map
def test_rules_json_has_65_rules_with_unique_ids_statements_and_a_group():
    doc = data_json("rules.json")
    rules = doc["rules"]
    assert len(rules) == 65 and len({r["id"] for r in rules}) == 65
    assert all(r["statement"].strip() and r["group"] and isinstance(r["hard"], bool) for r in rules)
    assert doc["gate_b"] == {"max_rules_per_call": 5, "default_rules_per_call": 1, "hard_rules_own_call": True}


def test_plan_config_has_the_tables_the_linter_and_taste_checks_read():
    plan = data_json("rules.json")["plan"]
    assert set(plan["size_class_studs"]) == set(literal_values(S.Accessory.model_fields["size_class"].annotation))
    assert plan["plan_set"] == {"specs": 3, "wildcards": 1}
    assert set(plan["colour_plan_ratios"]) == set(literal_values(S.ColourPlan))
    for name, shares in plan["colour_plan_ratios"].items():
        assert not shares or abs(sum(shares) - 1.0) < 1e-9, name
    assert plan["restraint"] == {"main_colours_max": 4, "hero_prints_max": 1}


def test_lint_backdrop_words_are_listed_for_transparent_assets():
    words = data_json("rules.json")["lint"]["backdrop_words"]
    assert {"background", "backdrop"} <= set(words) and len(words) == len(set(words))


def test_season_map_is_well_formed():
    sm = data_json("season_map.json")
    lo, hi = sm["warm_hue_deg"]
    assert 0 <= lo < hi <= 360 and (lo, hi) == (15, 125)
    assert set(sm["groups"]) == {"spring", "summer", "autumn", "winter"} and sm["min_chroma"] > 0 and sm["light_l_min"] > 0


def test_every_data_file_is_utf8_json_without_a_bom():
    for p in sorted(catalog._DATA.glob("*.json")):
        raw = p.read_bytes()
        assert not raw.startswith(b"\xef\xbb\xbf"), p.name
        json.loads(raw.decode("utf-8"))

"""Painted kit pieces (shoes, legwear, bracelets, gloves) and the upload checklist (EXP-01 / EXP-02 / EXP-09)."""
from __future__ import annotations

import json

import pytest

from duoskin.imaging import recipes as R
from duoskin.imaging import shoes_painted as K
from duoskin.roblox import checklist as CL
from duoskin.roblox import limits_clothing as LC
from duoskin.roblox import template as T

ROLES = {"shoe_base", "shoe_sole", "shoe_accent", "legwear", "bracelet", "glove"}


def test_catalogue_lists_every_style_and_obeys_the_static_rules():
    assert K.list_shoe_styles() == sorted(K.SHOE_STYLES) and K.list_legwear() == sorted(K.LEGWEAR_KINDS) and K.list_arm_pieces() == sorted(K.ARM_PIECES)
    assert K.validate_catalogue() == []
    for sid in K.SHOE_STYLES:
        p = K.shoe_piece(sid)
        assert 446 <= p.top_row <= 465 and p.layers and all(lay.stage == "kit" for lay in p.layers)
        ids = [lay.id for lay in p.layers]
        assert all(i.startswith(f"shoes.{sid}.") for i in ids)
        assert any(lay.assert_rule == "shoe_top" for lay in p.layers)
    # low shoes stay in 460-482 (bible §6.2), boots and high-tops start higher
    assert K.shoe_piece("sneaker_low").top_row >= 460 and K.shoe_piece("boot").top_row <= 450


def test_every_layer_uses_known_colour_roles_and_regions():
    def roles(spec):
        if isinstance(spec, dict):
            return {spec["role"]} if "role" in spec else set()
        return {spec} if isinstance(spec, str) and not spec.startswith("#") else set()

    used = set()
    for sid in K.SHOE_STYLES:
        for lay in K.shoe_piece(sid).layers:
            used |= roles(lay.colour)
    for kind in K.LEGWEAR_KINDS:
        for lay in K.legwear_piece(kind).layers:
            used |= roles(lay.colour)
    for piece in K.ARM_PIECES:
        for lay in K.arm_piece(piece).layers:
            used |= roles(lay.colour)
    assert used <= ROLES, used - ROLES
    for piece in K.ARM_PIECES:
        for lay in K.arm_piece(piece).layers:
            if lay.assert_rule == "band":
                rows = [s["rect"][1:4:2] for s in lay.shapes if "rect" in s]
                for lo, hi in rows:
                    assert any(a <= lo and hi <= b for a, b in T.LIMB_BANDS), (piece, lay.id, lo, hi)


def test_kit_layers_selection_per_template():
    assert K.kit_layers("shirt", arm_extras=[]) == [] and K.kit_layers("pants") == []
    pants = K.kit_layers("pants", shoe_style="boot", legwear="socks_crew")
    assert [lay.id.split(".")[0] for lay in pants][0] == "legwear" and pants[-1].id.startswith("shoes.boot")        # legwear first, shoes last
    shirt = K.kit_layers("shirt", arm_extras=["bracelet_char_left", "gloves", "bracelet_char_left"])
    assert len({lay.id for lay in shirt}) == len(shirt)
    assert K.kit_layers("pants", shoe_style="none", legwear="bare") == [] and K.legwear_piece("bare") is None
    with pytest.raises(R.RecipeError):
        K.shoe_piece("moon_boot")
    with pytest.raises(R.RecipeError):
        K.arm_piece("hat")


def test_user_kit_dir_overrides_a_builtin_file(tmp_path):
    custom = {"version": 1, "styles": {"sneaker_low": {"title": "mine", "top_row": 450, "layers": [
        {"id": "u", "colour": "shoe_base", "label": 6, "coverage": "add", "shapes": [{"rect": [0, 450, 584, 482]}], "regions": ["?limb_[fblr]"],
         "assert_rule": "shoe_top"}]}}}
    (tmp_path / "shoes.json").write_text(json.dumps(custom), encoding="utf-8")
    p = K.shoe_piece("sneaker_low", [tmp_path])
    assert p.title == "mine" and len(p.layers) == 1 and K.shoe_piece("sneaker_low").title != "mine"


def test_check_rule_messages():
    assert K.check_rule("shoe_top", 462, 482) is None and "outside" in K.check_rule("shoe_top", 440, 482)
    assert K.check_rule("shoe_top", 466, 482) and K.check_rule("shoe_top", 446, 482) is None
    assert K.check_rule("band", 452, 465) is None and K.check_rule("band", 410, 425) and K.check_rule("band", 469, 482) is None
    assert K.check_rule("band", 462, 470) and K.check_rule("", 0, 999) is None


# ------------------------------------------------------------------------------------------------------------ checklist
ITEMS = [{"item_id": "a.shirt", "character": "A", "type": "Shirt"}, {"item_id": "a.pants", "character": "A", "type": "Pants"},
         {"item_id": "a.hair", "character": "A", "type": "Hair", "category": "Hair", "attachment": "HairAttachment"},
         {"item_id": "a.head", "character": "A", "type": "Head"}]


def test_item_type_table_drives_channel_and_fee_exp01():
    assert set(CL.ITEM_TYPES) == set(CL.ALL_TYPES)
    for t in CL.CLASSIC_TYPES:
        row = CL.item_type_row(t)
        assert row.channel == "creator_dashboard" and row.fee_robux == 80 and "not refunded" in row.fee_note and "Classics" in row.channel_text
        assert "ID verification" in row.requirements[0]
    for t in CL.ACCESSORY_TYPES:
        assert CL.item_type_row(t).channel == "studio" and "UGC Validation" in CL.item_type_row(t).channel_text
    assert CL.item_type_row("Head").channel == "studio" and CL.item_type_row("Body").channel == "studio"
    assert CL.item_type_row("Shirt").fee_robux == LC.UPLOAD_FEE_ROBUX
    with pytest.raises(CL.ChecklistError):
        CL.item_type_row("Spaceship")


def test_upload_lines_are_locked_until_the_studio_test_is_ticked_exp02():
    cl = CL.build_checklist(ITEMS)
    assert [i.type for i in cl.items] == ["Shirt", "Pants", "Hair", "Head"]
    assert CL.is_locked(cl, "a.shirt", "upload") and not CL.upload_ready(cl, "a.shirt")
    with pytest.raises(CL.ChecklistError):
        CL.tick(cl, "a.shirt", "upload")
    cl = CL.tick(cl, "a.shirt", "studio_test")
    assert CL.is_locked(cl, "a.shirt", "upload")                           # still needs the final confirmation (EXP-09)
    cl = CL.tick(cl, "a.shirt", "confirm_final")
    assert not CL.is_locked(cl, "a.shirt", "upload") and CL.upload_ready(cl, "a.shirt")
    cl = CL.tick(cl, "a.shirt", "upload")
    assert CL.upload_ready(cl, "a.pants") is False                          # other items are unaffected
    cl = CL.tick(cl, "a.shirt", "studio_test", False)                       # un-ticking the test re-locks and un-ticks the upload
    assert CL.is_locked(cl, "a.shirt", "upload")
    assert not any(s.ticked for s in next(i for i in cl.items if i.item_id == "a.shirt").steps if s.step_id == "upload")


def test_accessory_steps_need_the_importer_settings_first():
    cl = CL.build_checklist(ITEMS)
    hair = next(i for i in cl.items if i.item_id == "a.hair")
    assert [s.step_id for s in hair.steps] == ["importer_settings", "aft", "meshpart", "studio_test", "confirm_final", "upload"]
    assert CL.is_locked(cl, "a.hair", "studio_test")
    for sid in ("importer_settings", "aft", "meshpart", "studio_test", "confirm_final"):
        cl = CL.tick(cl, "a.hair", sid)
    assert CL.upload_ready(cl, "a.hair")
    assert "category Hair" in hair.notes[0] and "HairAttachment" in hair.notes[0]
    text = " ".join(s.text for s in hair.steps)
    assert "Scale Unit = Studs" in text and "World Forward = Front" in text and "Merge Meshes OFF" in text


def test_lineage_unknown_adds_a_confirmation_that_gates_the_upload():
    cl = CL.build_checklist(ITEMS, lineage_unknown_items=["a.head"])
    head = next(i for i in cl.items if i.item_id == "a.head")
    assert "lineage_confirm" in [s.step_id for s in head.steps] and head.steps[-1].requires[-1] == "lineage_confirm"
    cl = CL.tick(CL.tick(cl, "a.head", "studio_test"), "a.head", "confirm_final")
    assert CL.is_locked(cl, "a.head", "upload")
    assert not CL.is_locked(CL.tick(cl, "a.head", "lineage_confirm"), "a.head", "upload")
    assert "FACS" in head.steps[0].text and "Avatar Setup" in head.steps[0].text


def test_json_round_trip_tick_state_banners_and_context():
    cl = CL.build_checklist(ITEMS, banners=["Reference check was off"], creator_docs_commit="abc123", validator_defaults={"tris": 4000},
                            ticked={"a.pants": ["studio_test", "confirm_final"]})
    d = CL.to_json(cl)
    json.dumps(d)
    assert d["banners"] == ["Reference check was off"] and d["context"]["creator_docs_commit"] == "abc123"
    pants = next(i for i in d["items"] if i["item_id"] == "a.pants")
    assert [s["ticked"] for s in pants["steps"]] == [True, True, False] and [s["locked"] for s in pants["steps"]] == [False, False, False]
    back = CL.from_json(d)
    assert CL.to_json(back) == d and CL.upload_ready(back, "a.pants")
    with pytest.raises(CL.ChecklistError):
        CL.build_checklist(ITEMS, ticked={"a.pants": ["upload"]})
    with pytest.raises(CL.ChecklistError):
        CL.tick(cl, "nobody", "upload")
    with pytest.raises(CL.ChecklistError):
        CL.tick(cl, "a.pants", "nothing")
    with pytest.raises(CL.ChecklistError):
        CL.build_checklist([{"item_id": "x", "type": "Boat"}])


def test_html_is_escaped_and_marks_locked_boxes():
    cl = CL.build_checklist([{"item_id": "a<script>", "character": "<b>A</b>", "type": "Shirt"}], banners=["<img src=x onerror=alert(1)>"])
    page = CL.render_html(cl)
    assert "<script>" not in page and "&lt;script&gt;" in page and "&lt;b&gt;A&lt;/b&gt;" in page and "onerror" not in page.replace("&lt;img src=x onerror", "")
    assert page.count("disabled") == 1                                       # only the locked upload box
    assert "80 Robux" in page and "Creator Dashboard" in page

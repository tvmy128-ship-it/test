"""Roblox facts the app encodes, pinned to the official docs (review lens 5: Roblox correctness).

Ground truth is the Roblox creator-docs checkout of 2026-09-26 (commit ``0b817b5cd955a63936bebf7b90746e34488af079``) and the official
template PNGs in ``tests/fixtures``. The pinned tests below need nothing but this repository; each carries the doc path it comes from.
The ``docs_*`` tests re-read the markdown and compare, and are skipped unless ``DUOSKIN_CREATOR_DOCS`` points at a creator-docs checkout::

    DUOSKIN_CREATOR_DOCS=/path/to/creator-docs python -m pytest tests/mesh/test_roblox_facts.py

Where the docs are silent the value is UNVERIFIED and the tests pin that label, so nobody can quietly promote a guess to a "Roblox rule".
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from duoskin.pipeline import export
from duoskin.roblox import checklist as CL
from duoskin.roblox import fees, limits
from duoskin.roblox import limits_clothing as LC
from duoskin.roblox import template as T
from duoskin.roblox.mesh_validators import validate_accessory

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
DOCS = Path(os.environ.get("DUOSKIN_CREATOR_DOCS", "")) / "content" / "en-us"
needs_docs = pytest.mark.skipif(not (DOCS / "avatar" / "classic-clothing.md").is_file(), reason="set DUOSKIN_CREATOR_DOCS to a creator-docs checkout")


def _doc(rel: str) -> str:
    return (DOCS / rel).read_text(encoding="utf-8")


# =====================================================================================================================
# Rigid accessories: content/en-us/avatar/rigid-accessories/specifications.md
# =====================================================================================================================
CLASSIC_BOXES = {                      # "#### Classic" table (width X, height Y, depth Z in studs, centred on the attachment unless noted)
    "Hat": (3, 4, 3), "Hair": (3, 5, 3.5), "Face": (3, 2, 2), "Neck": (3, 3, 2), "Front": (3, 3, 3), "Back": (10, 7, 4.5), "Waist": (4, 3.5, 7),
}


def test_classic_boxes_rigid_accessory_specifications_md():
    # avatar/rigid-accessories/specifications.md, "Body scale > Classic"
    for t, size in CLASSIC_BOXES.items():
        assert limits.box_for(t).size == size, t
    assert limits.box_for("Shoulder", "NeckAttachment").size == (7, 3, 3)             # "Shoulder (attached to NeckAttachment)"
    for att in ("RightCollarAttachment", "LeftCollarAttachment", "RightShoulderAttachment", "LeftShoulderAttachment"):
        assert limits.box_for("Shoulder", att).size == (3, 3, 3)                         # "Shoulder (other)"


def test_not_centred_boxes_in_the_roblox_frame_and_the_file_frame():
    # specifications.md Classic: Hair "2 up, 3 down; 1.5 front, 2 behind"; Back "1.5 front, 3 behind"; Waist "1.5 up, 2 down".
    # Roblox's front is -Z, our files put the front at +Z, so only the Z offset changes sign.
    hair, back, waist = limits.box_for("Hair"), limits.box_for("Back"), limits.box_for("Waist")
    assert hair.offset_roblox == ((0, (2 - 3) / 2, (2 - 1.5) / 2))
    assert back.offset_roblox == (0, 0, (3 - 1.5) / 2)
    assert waist.offset_roblox == (0, (1.5 - 2) / 2, 0)
    assert hair.offset_file == (0, -0.5, -0.25) and back.offset_file == (0, 0, -0.75) and waist.offset_file == (0, -0.25, 0)
    lo, hi = hair.lo_hi()
    assert (lo[1], hi[1]) == (-3.0, 2.0) and (lo[2], hi[2]) == (-2.0, 1.5)             # 3 down / 2 up; 2 behind / 1.5 in front (file frame)


def test_attachment_names_rigid_accessory_specifications_md():
    # specifications.md "Attachment points": accessory type -> attachment names (there is no wrist or hand type)
    want = {"Hat": ["HatAttachment"], "Hair": ["HairAttachment"], "Back": ["BodyBackAttachment"], "Front": ["BodyFrontAttachment"],
            "Neck": ["NeckAttachment"], "Face": ["FaceFrontAttachment", "FaceCenterAttachment"],
            "Waist": ["WaistFrontAttachment", "WaistCenterAttachment", "WaistBackAttachment"],
            "Shoulder": ["RightShoulderAttachment", "RightCollarAttachment", "NeckAttachment", "LeftCollarAttachment", "LeftShoulderAttachment"]}
    for t, names in want.items():
        assert sorted(limits.attachments_for(t)) == sorted(names), t
    assert limits.load_limits()["no_wrist_attachment"] is True and limits.attachments_for("Wrist") == []


def test_triangle_and_texture_limits_rigid_accessory_specifications_md():
    b = limits.budgets()
    assert b["rigid_accessory_tris_roblox_max"] == 4000                                 # "Rigid accessories can't exceed 4k triangles"
    assert b["texture_px_fail"] == 2048                                                 # "cannot exceed 2048x2048 resolution" (Marketplace)
    assert b["export_tris_max"] < b["rigid_accessory_tris_roblox_max"]                  # our own headroom under Roblox's 4000 (DES)


def test_texture_alpha_must_be_255_validation_system_md():
    # marketplace/validation-system.md: "Texture is not fully opaque ... any pixel ... has an alpha value below 255" fails a rigid accessory.
    # (A neighbouring 1 stud box keeps every other check green.)
    from test_mesh_limits_validators import GOOD

    res = {r.check_id: r for r in validate_accessory({**GOOD, "tex_min_alpha": 254}, "Shoulder", "RightCollarAttachment")}
    assert not res["CHK-M06"].passed and "255" in res["CHK-M06"].evidence


# =====================================================================================================================
# What the docs do NOT give: UNVERIFIED must stay UNVERIFIED (marketplace/validation-system.md names these checks without numbers)
# =====================================================================================================================
@pytest.mark.parametrize("key", ["mesh.surface_area_max", "mesh.surface_area_warn", "mesh.coplanar_max_frac", "mesh.center_offset_max",
                                 "mesh.scale_min", "mesh.components_max", "mesh.sparse_cover_warn_fail"])
def test_numbers_the_docs_do_not_give_are_unverified_and_say_so_to_the_user(key):
    assert limits.is_unverified(key) and limits.status_of(key) == "UNV"
    assert "UNVERIFIED" in limits.describe(key, "<=")


def test_documented_numbers_are_not_labelled_unverified():
    assert not limits.is_unverified("mesh.tex_warn_hard") and limits.status_of("mesh.tex_warn_hard") == "DOC"
    assert "UNVERIFIED" not in limits.describe("mesh.tris_max", "<=")


def test_unverified_limits_are_labelled_in_the_check_texts_the_user_reads():
    from test_mesh_limits_validators import GOOD

    res = {r.check_id: r for r in validate_accessory({**GOOD, "surface_area": 99.0, "coplanar_intersections": 999, "centre_offset": 2.0,
                                                      "shells": 12, "micro_shells": 1}, "Hat", "HatAttachment")}
    for cid in ("CHK-M10", "CHK-M11", "CHK-M05"):
        assert not res[cid].passed
        assert "UNVERIFIED" in res[cid].threshold or "UNVERIFIED" in res[cid].evidence, cid


def test_limits_json_records_the_docs_commit_and_sources():
    d = limits.load_limits()
    assert d["docs_commit"].startswith(fees.DOCS_COMMIT)
    assert "content/en-us/avatar/rigid-accessories/specifications.md" in d["docs_sources"]["boxes_and_attachments"]


# =====================================================================================================================
# Classic clothing: content/en-us/avatar/classic-clothing.md and the official template PNGs
# =====================================================================================================================
def test_classic_template_region_sizes_classic_clothing_md():
    # classic-clothing.md table: 128x128 FRONT/BACK; 64x128 torso R/L and the sides of arms and legs; 128x64 UP/DOWN; 64x64 U/D of arms and legs
    assert (T.WIDTH, T.HEIGHT) == (585, 559) == LC.TEMPLATE_SIZE
    size = {k: (x1 - x0 + 1, y1 - y0 + 1) for k, (x0, y0, x1, y1) in T.REGIONS.items()}
    assert size["torso_f"] == size["torso_b"] == (128, 128)
    assert size["torso_r"] == size["torso_l"] == (64, 128)
    assert size["torso_u"] == size["torso_d"] == (128, 64)
    for limb in ("rlimb", "llimb"):
        assert size[f"{limb}_u"] == size[f"{limb}_d"] == (64, 64)
        assert all(size[f"{limb}_{f}"] == (64, 128) for f in "lbrf")
    assert len(T.REGIONS) == 18 and LC.BIT_DEPTH == 8 and LC.FILE_FORMAT == "PNG"       # the template "supports 8-bit alpha channels"
    assert LC.TSHIRT_SIZE_HINT == (512, 512)                                              # "T-shirts are a square image, such as 512x512"


@pytest.mark.parametrize("kind", ["Shirts", "Pants"])
def test_official_template_png_guide_rows_and_size(kind):
    # tests/fixtures/Template-{Shirts,Pants}-R15.png: 585x559 RGBA; dotted guide rows 170 (torso) and 407 / 446 (limbs: "maximum limits for
    # height of gloves and lower leg details on R15 only"). The 418.5 and 467 splits are NOT on the official PNG.
    im = Image.open(FIXTURES / f"Template-{kind}-R15.png")
    assert im.size == (585, 559) and im.mode == "RGBA"
    a = np.asarray(im.convert("RGB")).astype(int)

    def dotted_rows(x0: int, x1: int, y0: int, y1: int) -> list[int]:
        return [y for y in range(y0, y1 + 1) if int((a[y, x0:x1 + 1].min(axis=1) > 235).sum()) > 40]

    assert 170 in dotted_rows(*T.REGIONS["torso_f"][0:1], T.REGIONS["torso_f"][2], 150, 190)
    limb = T.REGIONS["rlimb_l"]
    rows = dotted_rows(limb[0], limb[2], limb[1], limb[3])
    assert rows == [407, 446] == list(T.DASHED_ROWS)
    assert 418 not in rows and 419 not in rows and 467 not in rows


def test_split_rows_418_and_467_and_hidden_rows_are_declared_unverified():
    text = " ".join(LC.UNVERIFIED_FACTS)
    assert "418.5" in text and "467" in text and "355-377" in text and "R6" in text
    assert LC.describe()["unverified"] == list(LC.UNVERIFIED_FACTS)
    notes = " ".join(T.DATA["_notes"])
    assert "UNVERIFIED" in notes and "418.5" in notes and "classic-clothing.md" in notes


# =====================================================================================================================
# Fees and requirements: content/en-us/marketplace/marketplace-fees-and-commissions.md and marketplace-policy.md
# =====================================================================================================================
def test_upload_fees_and_publishing_advances_marketplace_fees_md():
    # "Pay an upload fee of 80 Robux per submission. Items that use an emissive mask ... 500 Robux instead."
    assert fees.UPLOAD_FEE_ROBUX == 80 == LC.UPLOAD_FEE_ROBUX and fees.UPLOAD_FEE_EMISSIVE_ROBUX == 500
    # "Publishing advance" table, non-limited column (the 3D "Shirt" = 600 and "Pants" = 600 rows are layered clothing, not what we make)
    assert fees.PUBLISHING_ADVANCE_NON_LIMITED == {"Shirt": 10, "Pants": 10, "TShirt": 10, "Hat": 1500, "Face": 1500, "Hair": 1000, "Neck": 1000,
                                                   "Shoulder": 1000, "Front": 1000, "Back": 1000, "Waist": 1000, "Head": 1500, "Body": 2500}


def test_every_fee_text_carries_the_price_disclaimer():
    assert "check Roblox for current prices" in fees.PRICE_NOTE
    for t in ("Shirt", "Pants", "Accessory", "Hair", "Hat", "Head", "Body"):
        assert fees.fee_note(t).endswith(fees.PRICE_NOTE), t
    cl = CL.build_checklist([{"item_id": "a.shirt", "type": "Shirt"}, {"item_id": "a.hair", "type": "Hair"}, {"item_id": "a.head", "type": "Head"},
                             {"item_id": "a.body", "type": "Body"}])
    for it in cl.items:
        assert "check Roblox for current prices" in it.fee_note, it.item_id
        assert any("check Roblox for current prices" in r for r in it.requirements), it.item_id        # the publishing requirement line
        upload = next(s for s in it.steps if s.kind == "upload")
        assert "check Roblox for current prices" in upload.text, it.item_id
    assert "check Roblox for current prices" in " ".join(cl.notes)
    assert "check Roblox for current prices" in export.readme(type("P", (), {"name": "x"}), {}, [], [], False)


def test_publishing_requirements_marketplace_policy_md():
    # marketplace-policy.md#creator-requirements: ID verification (or a linked parental account) to upload; 2-step verification, Roblox Plus or
    # Premium 1000/2200 and a publishing advance to publish.
    cl = CL.build_checklist([{"item_id": "a.shirt", "type": "Shirt"}])
    req = " ".join(cl.items[0].requirements)
    assert "ID verification" in cl.items[0].requirements[0] and "linked parental account" in req
    assert "2-step verification" in req and "Roblox Plus or Premium 1000/2200" in req and "publishing advance" in req and "10 Robux" in req
    assert "not refunded" in cl.items[0].fee_note and "generally" in cl.items[0].fee_note                 # "In general, upload fees are not refunded"


def test_the_kit_says_upload_is_manual_and_assets_cannot_be_changed_afterwards():
    # cloud/guides/usage-assets.md lists Animation, Audio, Decal/Image, Mesh, Model and Video only; marketplace/publish-to-marketplace.md:
    # "You can't update or edit assets and thumbnails after uploading."
    notes = " ".join(CL.COMMON_NOTES)
    assert "never does" in notes and "Open Cloud Assets API" in notes and "does not list Shirt, Pants" in notes
    assert "cannot be changed after upload" in notes and "thumbnail" in notes
    assert "this app never uploads anything" in export.readme(type("P", (), {"name": "x"}), {}, [], [], False)


def test_studio_steps_follow_the_docs():
    cl = CL.build_checklist([{"item_id": "a.shirt", "type": "Shirt"}, {"item_id": "a.hair", "type": "Hair", "category": "Hair", "attachment": "HairAttachment"}])
    shirt = " ".join(s.text for s in cl.items[0].steps)
    # classic-clothing.md "Test classic clothing": Avatar tab > Character > Block Avatar rig; the property takes an image you uploaded
    assert "Block Avatar" in shirt and "Asset Manager" in shirt and "ShirtTemplate" in shirt and "Creator Dashboard > Avatar Items > Classics" in shirt
    hair = " ".join(s.text for s in cl.items[1].steps)
    # publish-to-marketplace.md "Upload an asset": validation starts when the asset type is chosen; Submit pays the fee
    assert "do NOT click Submit" in hair and "Save to Roblox" in hair and "Generate MeshPart Accessory" in hair
    # rigid-accessories/specifications.md "Marketplace requirements": Plastic, Transparency 0, VertexColor 1,1,1, no extra objects
    assert "Material Plastic" in hair and "Transparency 0" in hair and "VertexColor 1,1,1" in hair and "not a Roblox rule" in hair      # DoubleSided is ours
    assert "Rig Scale = Default (Classic)" in hair and "Scale Unit = Studs" in hair
    # accessory-fitting-tool.md: AFT body type Classic; attachments cannot be imported (rigid-accessories/specifications.md)
    assert "body type Classic" in hair and "does not support importing attachments" in hair


def test_head_and_body_facts_dynamic_heads_and_character_bodies():
    head = CL.item_type_row("Head")
    # dynamic-heads/specifications.md: at least 17 FACS poses; blink, mouth open, happy and sad are validated
    text = " ".join(s.text for s in head.steps)
    assert "17 required FACS poses" in text and "eyes closed, mouth open" in text and "happy and sad" in text and "UNVERIFIED" in text
    # character-bodies/specifications.md "Triangle budgets": DynamicHead 4000; dynamic-heads/specifications.md: outer cage with eye/mouth landmarks
    assert "4000 triangles" in " ".join(head.requirements) and "17 FACS poses" in " ".join(head.requirements)
    body = CL.item_type_row("Body")
    req = " ".join(body.requirements)
    # character-bodies/specifications.md: 15 mesh objects; head 4000, torso 1750, arms and legs 1248 each, total 10,742
    assert "15 separate meshes" in req and "head 4000" in req and "torso 1750" in req and "1248" in req and "10,742" in req
    assert 4000 + 1750 + 4 * 1248 == 10742
    # art/accessories/publish-eyebrows-eyelashes.md: only eyelashes, eyebrows and hair may be bundled with a body
    assert "hair, eyebrow and eyelash" in " ".join(s.text for s in body.steps)


# =====================================================================================================================
# Re-read the docs when a checkout is available
# =====================================================================================================================
@needs_docs
def test_docs_classic_box_table_matches_limits_json():
    md = _doc("avatar/rigid-accessories/specifications.md")
    classic = md[md.index("#### Classic"):md.index("### AvatarPartScaleType")]
    rows = re.findall(r"<tr>\s*<td>(.*?)</td>\s*<td>(.*?)</td>\s*<td>(.*?)</td>\s*<td>(.*?)</td>\s*</tr>", classic, re.DOTALL)
    seen = {}
    for name, x, y, z in rows:
        num = lambda c: float(re.match(r"\s*([\d.]+)", c).group(1))
        seen[re.sub(r"<.*?>", "", name).strip()] = (num(x), num(y), num(z), y, z)
    for t, size in CLASSIC_BOXES.items():
        assert seen[t][:3] == tuple(map(float, size)), t
    assert seen["Shoulder (attached to NeckAttachment)"][:3] == (7, 3, 3) and seen["Shoulder (other)"][:3] == (3, 3, 3)
    assert "Not centered: 2 up, 3 down" in seen["Hair"][3] and "Not centered: 1.5 front, 2 behind" in seen["Hair"][4]
    assert "Not centered: 1.5 front, 3 behind" in seen["Back"][4] and "Not centered: 1.5 up, 2 down" in seen["Waist"][3]
    assert "Rigid accessories can't exceed **4k** triangles" in md and "cannot exceed 2048x2048" in md


@needs_docs
def test_docs_fee_table_matches_fees_py():
    md = _doc("marketplace/marketplace-fees-and-commissions.md")
    assert "upload fee of **80 Robux** per submission" in md and "require an upload fee of **500 Robux**" in md
    first = md[md.index("## Publishing advance"):md.index("Publishing advance example scenario")]
    rows = {n.strip(): int(v) for n, v in re.findall(r"<td>([^<]+)</td>\s*<td>(\d+)</td>\s*<td>[\dN/A]+</td>", first)}
    names = {"Classic shirt": "Shirt", "Classic pants": "Pants", "Classic t-shirt": "TShirt"}
    for doc_name, ours in {**names, **{k: k for k in ("Hat", "Face", "Hair", "Neck", "Shoulder", "Front", "Back", "Waist", "Head", "Body")}}.items():
        assert rows[doc_name] == fees.PUBLISHING_ADVANCE_NON_LIMITED[ours], doc_name
    assert rows["Shirt"] == rows["Pants"] == 600          # the 3D rows we do not make


@needs_docs
def test_docs_policy_and_validation_facts():
    pol = _doc("marketplace/marketplace-policy.md")
    assert "2-step verification" in pol and "Roblox Plus or Premium 1000/2200" in pol and "government ID verification" in pol
    assert "Do not use any Roblox-created assets or official" in pol                   # the IP rule behind the banned terms and CHK-E09
    val = _doc("marketplace/validation-system.md")
    assert "alpha value below `255`" in val and "sparse geometry" in val and "is not centered" in val
    assert "Validation begins upon selection" in _doc("marketplace/publish-to-marketplace.md")
    assert "You can't update or edit assets and thumbnails after uploading" in _doc("marketplace/publish-to-marketplace.md")
    assert "at least the following 17" in _doc("avatar/dynamic-heads/specifications.md")
    assert "you can only bundle **eyelashes**, **eyebrows**, and **hair**" in _doc("art/accessories/publish-eyebrows-eyelashes.md")


@needs_docs
def test_docs_assets_api_lists_no_clothing_or_accessory_types():
    md = _doc("cloud/guides/usage-assets.md")
    table = md[md.index("## Supported asset types and limits"):md.index("## Security permissions")]
    for word in ("Shirt", "Pants", "Accessory", "Hat", "Hair", "Head", "Body"):
        assert word not in table, word
    for word in ("Animation", "Audio", "Decal, Image", "Mesh", "Model", "Video"):
        assert word in table, word
    cls = _doc("avatar/classic-clothing.md")
    assert "Upload Asset" in cls and "**80 Robux** per submission" in cls and "does not require any fees" in cls
    assert "select the image you uploaded to Roblox" in cls


@needs_docs
def test_docs_get_validation_rules_exists_for_the_t7_helper():
    yaml = (DOCS / "reference" / "engine" / "classes" / "AvatarCreationService.yaml").read_text(encoding="utf-8")
    assert "AvatarCreationService:GetValidationRules" in yaml and "AccessoryMaxTriangles" in yaml and '["MaxTextureSize"]' in yaml
    assert "GetValidationRules" in export.VALIDATION_RULES_LUAU and "AccessoryRules" in export.VALIDATION_RULES_LUAU
    assert "UNTESTED" in export.VALIDATION_RULES_LUAU and "UNTESTED" in export.PROPERTY_CHECK_LUAU and "UNTESTED" in export.WRAPPER_LUAU


def test_t7_and_property_scripts_are_labelled_untested_and_in_the_kit():
    assert "UNTESTED" in export.VALIDATION_RULES_LUAU and "UNTESTED" in export.PROPERTY_CHECK_LUAU and "UNTESTED" in export.WRAPPER_LUAU
    assert "GetValidationRules" in export.VALIDATION_RULES_LUAU and "AccessoryMaxTriangles" in export.VALIDATION_RULES_LUAU
    assert "Transparency" in export.PROPERTY_CHECK_LUAU and "Plastic" in export.PROPERTY_CHECK_LUAU      # rigid-accessories/specifications.md
    text = export.readme(type("P", (), {"name": "x"}), {}, [], [], False)
    assert "validation_rules.luau" in text and "property_check.luau" in text


def test_json_files_stay_valid():
    json.loads((Path(limits.__file__).with_name("limits.json")).read_text(encoding="utf-8"))

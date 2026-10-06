"""The built-in kit files owned by the clothing track: recipes, shoes, legwear, bracelets, fabrics, procedural folds, mannequin."""
from __future__ import annotations

import json
from pathlib import Path

from duoskin.imaging import folds as FO
from duoskin.imaging import recipes as R
from duoskin.roblox import template as T

KIT = Path(R.__file__).resolve().parent.parent / "builtin_kits"


def test_every_kit_file_is_valid_json_utf8_with_a_version():
    for name in ("shoes.json", "legwear.json", "bracelets.json", "fabrics.json", "folds_procedural.json", "mannequin_blocky.json"):
        data = json.loads((KIT / name).read_text(encoding="utf-8"))
        assert isinstance(data, dict) and (data.get("version") == 1 or data.get("schema")), name
    for p in sorted((KIT / "recipes").glob("*.json")):
        d = json.loads(p.read_text(encoding="utf-8"))
        assert d["recipe_id"] == p.stem and d["template"] in ("shirt", "pants") or "extends" in d, p.name


def test_every_recipe_names_a_procedural_fold_set_so_the_fallback_is_tuned():
    ids = set(FO.procedural_set_ids())
    for rid in R.list_recipe_ids():
        assert R.load_recipe(rid).fold_set in ids, rid


def test_mannequin_matches_the_template_geometry_in_studs():
    d = json.loads((KIT / "mannequin_blocky.json").read_text(encoding="utf-8"))
    assert d["units"] == "studs" and d["frame"] == "y_up_z_front" and d["meta"]["px_per_stud_template"] == 64
    parts = {p["name"]: p for p in d["parts"]}
    assert len(parts) == 15
    px = 64.0
    assert abs(parts["RightUpperArm"]["size"][1] - 63.5 / px) < 1e-6 and abs(parts["RightLowerArm"]["size"][1] - 48.5 / px) < 1e-6
    assert abs(parts["RightHand"]["size"][1] - 16 / px) < 1e-6
    assert parts["UpperTorso"]["size"][0] == 2.0 and parts["UpperTorso"]["size"][2] == 1.0
    assert parts["RightUpperArm"]["centre"][0] < 0 < parts["LeftUpperArm"]["centre"][0]           # the character's right is -X
    ut, lt = parts["UpperTorso"]["size"][1], parts["LowerTorso"]["size"][1]
    assert abs(ut * px - (T.UPPER_TORSO_ROWS[1] - T.UPPER_TORSO_ROWS[0] + 1)) < 1e-6 and abs(lt * px - 32) < 1e-6    # rows 74-169 and 170-201
    for att in ("HatAttachment", "HairAttachment", "NeckAttachment", "RightCollarAttachment", "LeftShoulderAttachment", "BodyFrontAttachment",
                "WaistBackAttachment", "FaceFrontAttachment"):
        assert att in d["attachments"], att
    assert d["attachments"]["RightCollarAttachment"][0] == -d["attachments"]["LeftCollarAttachment"][0]


def test_fabrics_json_ids_and_limits():
    d = json.loads((KIT / "fabrics.json").read_text(encoding="utf-8"))["fabrics"]
    assert len(d) >= 10
    for fid, spec in d.items():
        assert spec["amplitude_dL"] <= 6.0 and spec["size"] >= 64 and spec["px_per_repeat"] > 0 and spec["prompt_phrase"], fid
        assert spec["material_family"] in {"jersey", "twill", "denim", "fleece", "nylon", "knit", "canvas", "corduroy"}, fid

"""The library (APP_SPEC 10.1, 10.7.1): the Add kit validator, the copy with origin and licence, the manifest, the head-base refusal without Blender."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from PIL import Image

from duoskin.pipeline import kits, library


def hair_folder(tmp_path: Path, name: str = "my_curls", source: str = "hair_bob_03") -> Path:
    dst = tmp_path / name
    shutil.copytree(kits.DEMO_DIR / "hair" / source, dst)
    meta = json.loads((dst / "style.json").read_text())
    meta.pop("origin", None)
    meta["id"] = name
    (dst / "style.json").write_text(json.dumps(meta))
    return dst


def test_a_valid_hair_style_is_copied_with_its_origin_and_licence(rt, tmp_path):
    out = library.add_kit(rt, str(hair_folder(tmp_path)), "hair", "user_made", "user_made")
    assert out["added"] == "my_curls" and out["flags"]["hair_kit_empty"] is False
    meta = json.loads((rt.paths.kits_dir / "hair" / "my_curls" / "style.json").read_text())
    assert meta["origin"] == "user_made" and meta["license"] == "user_made"
    summary = library.summary(rt)
    assert "my_curls" in summary["hair"] and summary["labels"]["hair"] == "kit hair"


def test_a_kit_asset_without_an_allowed_origin_is_refused(rt, tmp_path):
    with pytest.raises(library.KitError, match="origin"):
        library.add_kit(rt, str(hair_folder(tmp_path)), "hair", "scraped_from_the_web", "n/a")
    with pytest.raises(library.KitError, match="license"):
        library.add_kit(rt, str(hair_folder(tmp_path, "x")), "hair", "user_made", "all rights reserved")


def test_a_second_kit_with_the_same_id_is_refused(rt, tmp_path):
    f = hair_folder(tmp_path)
    library.add_kit(rt, str(f), "hair", "user_made", "user_made")
    with pytest.raises(library.KitError, match="already exists"):
        library.add_kit(rt, str(f), "hair", "user_made", "user_made")


def test_the_hair_validator_names_what_to_fix(tmp_path):
    f = hair_folder(tmp_path)
    (f / "bands.png").unlink()
    with pytest.raises(library.KitError, match="bands.png"):
        library.validate_hair(f, module=False)
    Image.new("RGB", (96, 32), (10, 10, 10)).save(f / "bands.png")
    with pytest.raises(library.KitError, match="grey values"):
        library.validate_hair(f, module=False)
    g = hair_folder(tmp_path, "nomesh")
    (g / "mesh.glb").unlink()
    with pytest.raises(library.KitError, match="mesh.glb"):
        library.validate_hair(g, module=False)
    h = hair_folder(tmp_path, "nometa")
    meta = json.loads((h / "style.json").read_text())
    del meta["supported_adjustments"]
    (h / "style.json").write_text(json.dumps(meta))
    with pytest.raises(library.KitError, match="supported_adjustments"):
        library.validate_hair(h, module=False)


def test_a_style_over_the_triangle_budget_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(library, "HAIR_TRIS_MAX", 20)
    with pytest.raises(library.KitError, match="at most 20"):
        library.validate_hair(hair_folder(tmp_path), module=False)


def test_a_fabric_tile_must_be_square(rt, tmp_path):
    f = tmp_path / "wool"
    f.mkdir()
    Image.new("L", (64, 32), 128).save(f / "tile.png")
    with pytest.raises(library.KitError, match="square"):
        library.validate_fabric(f)
    Image.new("L", (64, 64), 128).save(f / "tile.png")
    assert library.validate_fabric(f)["size"] == 64
    out = library.add_kit(rt, str(f), "fabric", "user_made", "cc0")
    assert out["added"] == "wool" and (rt.paths.kits_dir / "fabrics" / "wool" / "tile.png").is_file()


def test_unknown_kinds_and_missing_folders_are_refused(rt, tmp_path):
    with pytest.raises(library.KitError, match="kind"):
        library.add_kit(rt, str(tmp_path), "sofa", "user_made", "n/a")
    with pytest.raises(library.KitError, match="does not exist"):
        library.add_kit(rt, str(tmp_path / "nope"), "hair", "user_made", "n/a")


def test_rebuild_manifest_reports_the_enums_and_the_schema(rt):
    out = library.rebuild_manifest(rt)
    assert out["schema_smoke_test"]["ok"] is True and out["manifest_sha"] and "hair_kit_empty" in out["flags"]

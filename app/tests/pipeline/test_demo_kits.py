"""The shipped demo kits (APP_SPEC §16) are exactly what the generator writes, and meet the kit contract of §10.7.1."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from duoskin.pipeline import demo_kits, kits


def _digest(root: Path) -> dict[str, str]:
    import hashlib

    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(root.rglob("*")) if p.is_file()}


def test_shipped_demo_kits_match_the_generator(tmp_path):
    demo_kits.build_demo_kits(tmp_path / "gen")
    assert _digest(tmp_path / "gen") == _digest(kits.DEMO_DIR)


def test_three_hair_styles_with_the_contract_files():
    styles = sorted(p.name for p in (kits.DEMO_DIR / "hair").iterdir() if p.is_dir() and p.name != "modules")
    assert styles == ["hair_bob_03", "hair_short_crop_01", "hair_spiky_05"]
    for sid in styles:
        folder = kits.DEMO_DIR / "hair" / sid
        meta = json.loads((folder / "style.json").read_text())
        assert meta["origin"] == "code_generated" and meta["license"] == "n/a" and meta["id"] == sid
        assert {"prompt_phrase", "length_class", "silhouette_class", "parting", "supported_adjustments", "attach_frame"} <= set(meta)
        assert meta["tris"] <= 3000
        assert sorted(int(v) for v in np.unique(np.asarray(Image.open(folder / "bands.png").convert("L")))) == [64, 128, 192]
        assert sorted(p.name for p in (folder / "views").iterdir()) == ["back.png", "front.png", "left.png", "right.png"]


def test_the_demo_kit_passes_the_add_kit_validator():
    from duoskin.pipeline import library

    for sid in ("hair_bob_03", "hair_short_crop_01", "hair_spiky_05"):
        assert library.validate_hair(kits.DEMO_DIR / "hair" / sid, module=False)["tris"] <= 3000


def test_demo_manifest_flags_are_reduced_modes_only(tmp_path):
    m = kits.build_manifest(tmp_path / "none", include_demo=True)
    assert set(m["hair"]) >= {"hair_bob_03", "hair_short_crop_01", "hair_spiky_05"}
    assert m["flags"]["hair_kit_empty"] is False and m["flags"]["head_base_present"] is False
    empty = kits.build_manifest(tmp_path / "none", include_demo=False)
    assert empty["flags"]["hair_kit_empty"] is True and empty["hair"] == {}


@pytest.mark.parametrize("origin", ["code_generated"])
def test_every_demo_asset_has_an_allowed_origin(origin):
    for meta in kits.DEMO_DIR.rglob("*.json"):
        data = json.loads(meta.read_text())
        if isinstance(data, dict) and "origin" in data:
            assert data["origin"] in kits.ORIGINS

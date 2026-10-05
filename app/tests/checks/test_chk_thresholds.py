"""checks/thresholds.py: the single threshold registry (FAILURE_MODES §4.1)."""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from duoskin.checks import thresholds as TH

PKG = Path(__file__).resolve().parents[2] / "duoskin"
MY_MODULES = ["imaging/checks.py", "imaging/face_canvas.py", "imaging/masks.py", "imaging/similarity.py", "imaging/svg.py", "imaging/ocr.py",
              "imaging/glyph.py", "imaging/files.py", "imaging/palette.py", "imaging/matte.py", "imaging/uvwarp.py", "imaging/guides.py",
              "imaging/slab_art.py", "checks/runner.py", "checks/policy.py"]


def test_table_shape():
    assert TH.THRESHOLDS_VERSION
    for name, entry in TH.T.items():
        assert len(entry) == 3, name
        value, status, fm_ids = entry
        assert status in TH.STATUSES, name
        assert isinstance(fm_ids, list) and fm_ids and all(isinstance(i, str) for i in fm_ids), name
        assert value is not None, name


def test_known_values_from_the_spec_table():
    assert TH.get("tpl.size_wh") == (585, 559)
    assert TH.get("img.clear_share_min") == 0.10
    assert TH.get("img.palette_de_max") == 12.0
    assert TH.get("img.palette_large_reject_de") == 15.0
    assert TH.get("face.line_skin_de_min") == 20.0
    assert TH.get("face.registry_phash_max") == 8
    assert TH.get("face.registry_dreamsim_min") == 0.15
    assert TH.get("duo.dreamsim_clone_min") == 0.30
    assert TH.get("con.clone_proxy_dreamsim_min") == 0.30
    assert TH.get("duo.degraded_spec_dist_min") == 0.40 and TH.get("duo.degraded_phash_clone_max") == 10
    assert TH.get("ocr.glyph_score_max") == 0.3 and TH.get("acc.slab_convexity_min") == 0.8
    assert TH.get("calib.demote_fire_rate") == 0.30 and TH.get("calib.percentile_min_duos") == 5
    assert TH.get("ladder.max_fixes_per_part") == 3
    assert TH.get("gate.max_soft_warnings") == 2
    assert TH.get("face.skin_tones") == 5
    assert TH.status_of("mesh.surface_area_max") == "DOC"


def test_removed_accessory_jaccard_key():
    with pytest.raises(TH.UnknownThreshold):
        TH.get("pln.accessory_cat_jaccard_max")


def test_unknown_name_raises_not_defaults():
    with pytest.raises(TH.UnknownThreshold):
        TH.get("img.no_such_key")
    with pytest.raises(KeyError):
        TH.describe("nope")


def test_describe_text():
    assert TH.describe("img.palette_de_max", "<=") == "<= 12.0 (img.palette_de_max, DES)"


def test_overrides_only_for_tunable_statuses():
    assert TH.is_tunable("img.palette_de_max") and not TH.is_tunable("mesh.surface_area_max") and not TH.is_tunable("gate.max_soft_warnings")
    with TH.overrides({"img.palette_de_max": 9.0}):
        assert TH.get("img.palette_de_max") == 9.0
        assert TH.default("img.palette_de_max") == 12.0
        assert "default 12.0" in TH.describe("img.palette_de_max", "<=")
    assert TH.get("img.palette_de_max") == 12.0
    with pytest.raises(ValueError), TH.overrides({"mesh.surface_area_max": 1.0}):
        pass
    with pytest.raises(ValueError), TH.overrides({"gate.max_soft_warnings": 5}):
        pass


def test_overrides_nest_and_restore():
    with TH.overrides({"img.margin_min": 0.1}):
        with TH.overrides({"img.halo_de_max": 3.0}):
            assert TH.get("img.margin_min") == 0.1 and TH.get("img.halo_de_max") == 3.0
        assert TH.get("img.halo_de_max") == 8.0
    assert TH.get("img.margin_min") == 0.06


def test_fm_ids_for_union():
    assert TH.fm_ids_for(["img.palette_de_max", "img.margin_min"]) == ["IMG-05", "DUO-07", "IMG-03", "ACC-03"]


def test_no_percent_or_target_ratios_in_the_registry():
    """Thresholds cut the bad tail only (APP_SPEC §3.1): no key may describe a desired look (a 'target')."""
    assert not [k for k in TH.T if "target" in k.split(".")[-1] and k not in ("hair.tris_target", "calib.target_labels")]


def test_removed_keys_stay_removed():
    """FAILURE_MODES v1.3 / APP_SPEC v1.3: the identical-field plan proxy is gone, and so are the Jaccard rule and the old clone-stage keys."""
    for k in TH.T:
        assert not k.startswith("pln.identical_fields"), k
    for gone in ("pln.accessory_cat_jaccard_max", "pln.identical_fields_max", "pln.identical_fields_same_club_extra", "clone.lower_edge",
                 "clone.concept_lower_edge", "clone.gate2_lower_edge", "img.glyph_score_max", "img.slab_solidity_min"):
        assert gone not in TH.T, gone


def test_every_fm_v13_key_is_present():
    """The FAILURE_MODES §4.1 table is the registry: parse it from the doc (when present) and compare names, values and statuses."""
    import re

    doc = PKG.parents[1] / "docs" / "FAILURE_MODES.md"
    if not doc.exists():
        pytest.skip("docs not present")
    block = doc.read_text(encoding="utf-8").split("### 4.1 Code-ready table", 1)[1].split("```python", 1)[1].split("```", 1)[0]
    ns: dict = {}
    exec(block, ns)  # noqa: S102 - the table is plain literals from our own docs
    assert ns["THRESHOLDS_VERSION"] == TH.THRESHOLDS_VERSION
    assert len(ns["T"]) >= 150
    for key, (value, status, ids) in ns["T"].items():
        assert key in TH.T, key
        assert TH.default(key) == value and TH.status_of(key) == status and TH.fm_ids_of(key) == ids, key
    assert not re.search(r"pln\.identical_fields", block)


def test_tv_is_the_reader_check_code_uses():
    assert TH.tv is TH.get and TH.tv("img.margin_min") == 0.06


def _threshold_keys_used(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    keys: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in ("get", "describe", "default", "status_of"):
            owner = node.func.value
            if isinstance(owner, ast.Name) and owner.id == "TH" and node.args and isinstance(node.args[0], ast.Constant):
                keys.add(str(node.args[0].value))
    return keys


@pytest.mark.parametrize("rel", MY_MODULES)
def test_every_threshold_key_read_by_code_exists(rel):
    """A typo in a threshold name must fail here, not silently at runtime inside a fail-closed wrapper."""
    for key in _threshold_keys_used(PKG / rel):
        assert key in TH.T, f"{rel} reads unknown threshold {key!r}"


def test_checks_json_threshold_keys_exist():
    import json

    data = json.loads((PKG / "data" / "checks.json").read_text(encoding="utf-8"))
    for row in data["checks"]:
        for k in row["threshold_keys"]:
            assert k in TH.T, (row["check_id"], k)
    assert data["thresholds_version"] == TH.THRESHOLDS_VERSION

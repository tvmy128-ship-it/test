"""The export gate's Roblox / IP safeguards (review lens 5): no catalogue or Roblox-made assets, no mock sources, honest provenance.

Roblox's rule is in the creator-docs (``content/en-us/marketplace/marketplace-policy.md``, General creation guidelines): "Do not use any
Roblox-created assets or official Roblox branding or iconography as part of your items."
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from duoskin.pipeline import export
from duoskin.prompts.catalog import load_banned
from duoskin.roblox import limits_clothing as LC


# ---------------------------------------------------------------------------------------------------- CHK-E09: allowed origins
def _k(asset, origin, sha="a" * 64):
    return {"kit_asset": asset, "origin": origin, "sha256": sha, "license": "n/a"}


def test_only_self_made_kit_assets_may_ship():
    ok = [_k("hair/long", "user_made"), _k("fabrics/denim", "code_generated"), _k("fabrics/knit", "app_generated")]
    assert export.kit_origin_problems(ok) == []
    assert export.SHIPPABLE_ORIGINS == ("user_made", "code_generated", "app_generated")


@pytest.mark.parametrize("origin", ["roblox_reference", "catalogue", "roblox_catalog", "downloaded", "", None, "unknown"])
def test_a_hair_style_or_fabric_with_any_other_origin_blocks_the_export(origin):
    for asset in ("hair/long", "fabrics/denim"):
        assert export.kit_origin_problems([_k(asset, origin)]), (asset, origin)


def test_a_roblox_reference_is_allowed_for_the_head_base_only_and_a_missing_sha_blocks():
    assert export.kit_origin_problems([_k("head_base/round/head.fbx", "roblox_reference")]) == []
    assert export.kit_origin_problems([_k("head_base/round/head.fbx", "catalogue")])
    bad = export.kit_origin_problems([_k("hair/long", "user_made", sha="")])
    assert bad and "no sha256" in bad[0]


# ---------------------------------------------------------------------------------------------------- the head base lineage (POL-07)
class _Kit:
    def __init__(self, base, present=True):
        self.flags = {"head_base_present": present}
        self._base = base

    def head_base_dir(self, eye_shape=None):
        return self._base


def test_head_base_lineage_defaults_to_a_roblox_reference_with_an_unknown_licence(tmp_path):
    d = tmp_path / "round"
    d.mkdir()
    (d / "head.fbx").write_bytes(b"fbx")
    spec = {"a": {"face": {"eye_shape": "round"}}, "b": {"face": {"eye_shape": "round"}}}
    lin = export.head_base_lineage(_Kit(d), spec)
    assert len(lin) == 1                                                                  # one variant, listed once for both characters
    assert lin[0]["kit_asset"] == "head_base/round/head.fbx" and lin[0]["origin"] == "roblox_reference" and lin[0]["license"] == "unknown"
    assert len(lin[0]["sha256"]) == 64
    assert export.kit_origin_problems(lin) == []                                         # allowed, but the checklist asks the user to confirm


def test_head_base_provenance_json_can_declare_the_users_own_work(tmp_path):
    d = tmp_path / "round"
    d.mkdir()
    (d / "head.fbx").write_bytes(b"fbx")
    (d / "provenance.json").write_text(json.dumps({"origin": "user_made", "license": "user_made"}), encoding="utf-8")
    lin = export.head_base_lineage(_Kit(d), {"a": {"face": {"eye_shape": "round"}}})
    assert lin[0]["origin"] == "user_made" and lin[0]["license"] == "user_made"


def test_no_head_base_means_no_head_base_lineage(tmp_path):
    assert export.head_base_lineage(_Kit(tmp_path, present=False), {"a": {"face": {"eye_shape": "round"}}}) == []


def test_an_unknown_head_base_licence_adds_the_manual_confirmation_line_to_the_head_item():
    from duoskin.roblox import checklist as CL

    cl = CL.build_checklist([{"item_id": "a.face", "type": "Head"}], lineage_unknown_items=["a.face"])
    assert "lineage_confirm" in [s.step_id for s in cl.items[0].steps]
    assert CL.is_locked(CL.tick(CL.tick(cl, "a.face", "studio_test"), "a.face", "confirm_final"), "a.face", "upload")


# ---------------------------------------------------------------------------------------------------- mock sources
def _asset(source="code", notes=(), stream="pipeline", inputs=()):
    return SimpleNamespace(first_provenance=SimpleNamespace(source=source, notes=list(notes), stream=stream, input_shas=list(inputs)))


class _Cas:
    def __init__(self, assets):
        self.assets = assets

    def find_asset(self, sha):
        return self.assets.get(sha)


def _rt(assets):
    return SimpleNamespace(cas=_Cas(assets))


def test_mock_sources_are_blocked_directly_and_through_their_ancestors():
    rt = _rt({"mock": _asset("mock"), "ok": _asset("openai"), "child": _asset("code", inputs=["mock"]),
              "grandchild": _asset("code", inputs=["child"]), "drill": _asset("openai", stream="drill"),
              "from_drill": _asset("code", inputs=["drill"]), "noted": _asset("code", notes=["mock_lineage"]),
              "from_noted": _asset("code", inputs=["noted"])})
    assert export.is_mock_asset(rt, "mock") == "source mock"
    assert export.is_mock_asset(rt, "ok") is None
    assert "mock" in export.is_mock_asset(rt, "child")                    # an ancestor that is a mock blocks even without a mock_lineage note
    assert "mock" in export.is_mock_asset(rt, "grandchild")
    assert export.is_mock_asset(rt, "drill") == "stream drill" and "drill" in export.is_mock_asset(rt, "from_drill")
    assert "mock" in export.is_mock_asset(rt, "noted") and "mock" in export.is_mock_asset(rt, "from_noted")
    assert export.is_mock_asset(rt, "absent") == "missing from the content store"


def test_the_ancestor_walk_stops_on_cycles_and_depth():
    rt = _rt({"a": _asset("code", inputs=["b"]), "b": _asset("code", inputs=["a"])})
    assert export.mock_ancestor(rt, "a") is None
    chain = {f"n{i}": _asset("code", inputs=[f"n{i + 1}"]) for i in range(12)}
    chain["n12"] = _asset("mock")
    assert export.mock_ancestor(_rt(chain), "n0", depth=4) is None and export.mock_ancestor(_rt(chain), "n0", depth=20)


def test_the_mock_flag_stays_a_test_only_switch():
    assert export.ALLOW_MOCK_FOR_TESTS is False


# ---------------------------------------------------------------------------------------------------- Roblox's own marks are banned words
def test_roblox_trademarks_and_catalogue_items_are_banned_words():
    banned = load_banned(extra_path="")
    for text in ("a roblox logo on the chest", "free robux", "roblox studio", "a robloxian hat", "korblox legs", "headless horseman look", "adopt me style",
                 "rthro proportions", "dominus crown", "bloxburg house", "royale high outfit", "roblox premium badge"):
        assert banned.hits(text, ["ip_platform"]), text
    assert not banned.hits("a plain red hat", ["ip_platform"])


def test_the_roblox_word_is_a_template_label_that_may_not_ship_on_a_texture():
    assert "ROBLOX" in LC.TEMPLATE_LABEL_WORDS and LC.find_label_words(["Roblox"]) == ["ROBLOX"]

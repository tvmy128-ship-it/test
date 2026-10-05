"""prompts/system_blocks.py: the cached system blocks of every Claude route (bible §8.1)."""
from __future__ import annotations

import json
import re

from duoskin.models import kitenums
from duoskin.prompts import system_blocks as SB
from duoskin.prompts.catalog import data_json


def test_block_order_is_context_rules_style_kits_then_profiles_then_role():
    blocks = SB.build_system("<role name=\"x\">text</role>", plan_loop=True)
    heads = [b["text"].split("\n", 1)[0] for b in blocks]
    assert heads[0].startswith("<duoskin_context>") and heads[1].startswith("<roblox_rules") and heads[2] == "<style_guide>"
    assert heads[3] == "<kit_inventory>" and heads[4] == "<structure_profiles>" and heads[5].startswith("<role")
    assert len(SB.build_system("<role>x</role>", plan_loop=False)) == 5


def test_only_the_role_block_carries_cache_control():
    blocks = SB.build_system("<role>x</role>", plan_loop=True, ttl_1h=True)
    assert blocks[-1]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    assert all("cache_control" not in b for b in blocks[:-1])
    assert SB.build_system("<role>x</role>")[-1]["cache_control"] == {"type": "ephemeral"}


def test_blocks_are_deterministic_and_have_no_timestamps_or_ids():
    a = SB.shared_blocks(plan_loop=True)
    b = SB.shared_blocks(plan_loop=True)
    assert a == b
    text = "\n".join(a)
    assert not re.search(r"\b20\d\d-\d\d-\d\d[T ]\d\d:\d\d", text) and not re.search(r"\b[0-9a-f]{8}-[0-9a-f]{4}-", text)
    assert SB.system_sha(SB.build_system("<role>x</role>")) == SB.system_sha(SB.build_system("<role>x</role>"))
    assert SB.system_sha(SB.build_system("<role>x</role>")) != SB.system_sha(SB.build_system("<role>y</role>"))


def test_the_style_guide_block_holds_the_parameters_and_both_verbatim_blocks():
    block = SB.style_guide_block()
    sg = data_json("style_guide.json")
    for name, text in sg["blocks"].items():
        assert f"{name}: {text}" in block
    params = json.loads(block.split("\n")[1])
    assert "blocks" not in params and params == {k: v for k, v in sg.items() if k != "blocks"}


def test_the_kit_inventory_block_follows_the_installed_inventory_and_sorts_keys(demo_kit_inventory):
    block = SB.kit_inventory_block()
    body = json.loads(block.split("\n", 1)[1].rsplit("\n", 1)[0])
    assert "hair_bob_03" in json.dumps(body)
    empty = kitenums.inventory_from_manifest({})
    assert SB.kit_inventory_block(empty) != block
    assert SB.kit_inventory_block(demo_kit_inventory) == block
    assert list(body) == sorted(body)


def test_the_structure_profiles_block_is_the_linters_file():
    block = SB.structure_profiles_block()
    assert json.loads(block.split("\n", 1)[1].rsplit("\n", 1)[0]) == data_json("structure_profiles.json")


def test_plan_loop_routes_are_listed():
    assert SB.PLAN_LOOP_ROUTES == ("L3", "L4", "L6", "L7")


def test_the_roblox_rules_block_states_the_limits_the_code_enforces():
    from duoskin.roblox import limits

    text = SB.ROBLOX_RULES
    assert "585x559" in text and "4000 triangles" in text and "2048" in text
    assert limits.box_for("Hat", "Hat").size == (3.0, 4.0, 3.0) and "hat 3x4x3" in text

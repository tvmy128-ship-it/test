"""Golden fixtures for the compositor output (APP_SPEC §17.3, CHK-B07): the layer-stack hash, the alpha channel and the label map of
every recipe with fixed inputs. A change here is a deliberate change of the garments: regenerate with

    DUOSKIN_REGEN_GOLDEN=1 python -m pytest tests/clothing/test_clo_golden.py

and review the diff of ``golden_compositor.json``. The stack hash covers the resolved layer descriptors and the identity of every
input (fabric tile pixels, fold-set pixels, prints, palette), so it also changes when a procedural fabric or fold is redrawn; the
alpha and label hashes only depend on integer rasterisation and counting, never on floating-point shading.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest
from clo_helpers import compose, composed, rgba

from duoskin.imaging import recipes as R

GOLDEN = Path(__file__).with_name("golden_compositor.json")
VARIANTS = {
    "tee@waist_tucked": ("tee", {"attrs": {"hem": "waist_tucked"}}),
    "tee@v_neck+yoke": ("tee", {"attrs": {"neckline": "v_neck", "block_layout": "yoke"}}),
    "jacket_zip@layered": ("jacket_zip", {"attrs": {"front": "layered"}}),
    "skirt_pleated@high_midi_socks": ("skirt_pleated", {"attrs": {"waist": "high", "leg": "midi"}, "legwear": "socks_knee", "shoe": "boot"}),
    "jeans_straight@loafer": ("jeans_straight", {"shoe": "loafer"}),
    "tee@gloves_bracelet": ("tee", {"extras": ["gloves", "bracelet_char_left"]}),
}


def digest(res) -> dict[str, str]:
    return {"layer_stack_hash": res.layer_stack_hash, "alpha_sha": hashlib.sha256(rgba(res.png)[..., 3].tobytes()).hexdigest(),
            "label_sha": hashlib.sha256(res.label_map.tobytes()).hexdigest()}


def current() -> dict[str, dict[str, str]]:
    out = {rid: digest(composed(rid)) for rid in sorted(R.list_recipe_ids())}
    for name, (rid, kw) in VARIANTS.items():
        out[name] = digest(compose(rid, run_checks=False, **kw))
    return out


def test_compositor_goldens():
    now = current()
    if os.environ.get("DUOSKIN_REGEN_GOLDEN") == "1":
        GOLDEN.write_text(json.dumps(now, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        pytest.skip("golden file regenerated")
    gold = json.loads(GOLDEN.read_text(encoding="utf-8"))
    assert set(gold) == set(now), (sorted(set(gold) ^ set(now)))
    stale = {k: [f for f in ("layer_stack_hash", "alpha_sha", "label_sha") if gold[k][f] != now[k][f]] for k in now if gold[k] != now[k]}
    assert not stale, f"golden mismatch (regenerate deliberately if the garments changed): {stale}"


def test_every_recipe_has_a_distinct_stack_hash_and_a_distinct_alpha_or_hash():
    now = current()
    assert len({v["layer_stack_hash"] for v in now.values()}) == len(now)

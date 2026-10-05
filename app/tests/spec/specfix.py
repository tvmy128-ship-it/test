"""Shared fixtures of track S (specs, plans, demo kit inventory). Test code only: nothing here is ever sent to a model.

The kit manifest is empty in production (every hair is ``hair_custom``); the demo inventory below adds a small human-written
hair kit so that the kit-hair rules (hair A != B, the pair IoU matrix, fringe defaults) can be tested.
"""
from __future__ import annotations

import json
from pathlib import Path

from duoskin.models import kitenums
from duoskin.models.spec import DuoSpec

FIXTURES = Path(__file__).resolve().parent / "fixtures"
SPEC_NAMES = ["spec_complement_gb", "spec_same_club_bb", "spec_mirror_gg", "spec_twins_bg", "spec_empty_bb", "spec_bg_min"]

DEMO_MANIFEST = {
    "hair": {
        "hair_short_crop_01": {"prompt_phrase": "short tousled crop with a tapered back", "length_class": "short", "silhouette_class": "crop",
                                "clump_k": 6, "parting": "left", "default_fringe": "fringe_a",
                                "supported_adjustments": ["volume", "fringe_length", "part_side"]},
        "hair_long_braids_02": {"prompt_phrase": "long braids with a centre part", "length_class": "long", "silhouette_class": "braids",
                                 "clump_k": 5, "parting": "centre", "default_fringe": "none", "symmetric": True,
                                 "supported_adjustments": ["back_length", "clump_size"]},
        "hair_bob_03": {"prompt_phrase": "chin-length bob with blunt ends", "length_class": "medium", "silhouette_class": "bob",
                         "clump_k": 5, "parting": "centre", "default_fringe": "fringe_b", "symmetric": True,
                         "supported_adjustments": ["fringe_length", "side_length"]},
        "hair_buns_04": {"prompt_phrase": "two high round buns", "length_class": "short", "silhouette_class": "buns", "clump_k": 4,
                          "parting": "centre", "default_fringe": "none", "symmetric": True, "supported_adjustments": ["volume"]},
        "hair_spiky_05": {"prompt_phrase": "tall swept-up spiky style", "length_class": "short", "silhouette_class": "spiky", "clump_k": 7,
                           "parting": "none", "default_fringe": "none", "supported_adjustments": ["volume", "clump_size"]},
        "hair_wavy_06": {"prompt_phrase": "long wavy hair past the shoulders", "length_class": "long", "silhouette_class": "waves", "clump_k": 6,
                          "parting": "centre", "default_fringe": "none", "supported_adjustments": ["back_length", "side_length"]},
        # the ids of the bible's Appendix D fixture
        "hair_spiky_crop_02": {"prompt_phrase": "short spiky crop", "length_class": "short", "silhouette_class": "crop", "clump_k": 6,
                                "parting": "left", "default_fringe": "fringe_a"},
        "hair_twin_braids_03": {"prompt_phrase": "long twin braids", "length_class": "long", "silhouette_class": "braids", "clump_k": 5,
                                 "parting": "centre", "default_fringe": "none"},
    },
    "hair_modules": {"fringe": {"fringe_a": {"prompt_phrase": "side-swept fringe"}, "fringe_b": {"prompt_phrase": "straight blunt fringe"}},
                     "back": {"back_a": {"prompt_phrase": "long tapered back"}, "back_b": {"prompt_phrase": "short tidy back"}}},
    "hair_pair_iou": {"hair_buns_04|hair_long_braids_02": {"front": 0.35, "side": 0.4},
                      "hair_short_crop_01|hair_spiky_05": {"front": 0.9, "side": 0.88},
                      "hair_bob_03|hair_spiky_05": {"front": 0.3, "side": 0.4}},
}


def demo_inventory() -> kitenums.KitInventory:
    return kitenums.inventory_from_manifest(DEMO_MANIFEST)


def load_dict(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def load_spec(name: str) -> DuoSpec:
    """A fixture spec validated against the demo inventory (with every spec rule on)."""
    with kitenums.use_inventory(demo_inventory()):
        return DuoSpec.model_validate(load_dict(name))


def all_specs() -> dict[str, DuoSpec]:
    return {n: load_spec(n) for n in SPEC_NAMES}

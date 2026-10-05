"""The Recraft rung of a sticker-slab badge (technique ``R2_badge`` of the badge ladder, ``pipeline/assetloop.py``) compiles ``R2.print`` with the
loop's ``accessory`` slot. The template used to declare only ``print`` and ``aspect``, so the rung raised "unknown input accessory" the moment
the first rung (I6) failed a Gate A check, and the whole PARTS job of the duo failed (found by clicking the app through, tests/e2e_ui)."""
from __future__ import annotations

import re

from duoskin.prompts import compiler


def test_the_badge_route_compiles_r2_print_from_the_accessory_and_its_colours(specs):
    spec = specs["spec_complement_gb"]
    acc = spec.a.accessories[0]
    cp = compiler.compile("R2.print", spec, "a", {"accessory": 0})
    assert "1)" in cp.text and cp.provider_fields["background"] in ("transparent", "opaque", "sentinel")
    assert " ".join(acc.description.split()[:3]) in cp.text, "the artwork is the accessory itself"
    assert not re.search(r"#[0-9A-Fa-f]{6}", cp.text)                              # colours are named, never hex codes
    assert cp.provider_fields["controls"]["colors"], "the Recraft colour controls carry the accessory's colours"


def test_the_print_route_is_unchanged(specs):
    spec = specs["spec_complement_gb"]
    r = compiler.compile("R2.print", spec, "a", {"print": "top.0"})
    assert spec.a.top.prints[0].motif.split()[0] in r.text
    assert r.sha256 != compiler.compile("R2.print", spec, "a", {"accessory": 0}).sha256

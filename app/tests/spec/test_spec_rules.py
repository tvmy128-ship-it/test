"""models/spec_rules.py: what the grammar cannot enforce (word caps, hex, palette ids, refs, counts, prints)."""
from __future__ import annotations

import copy

import pytest
import specfix
from pydantic import ValidationError

from duoskin.models import spec, spec_rules


@pytest.fixture()
def base():
    return specfix.load_dict("spec_complement_gb")


def codes(d):
    return [(p.code, p.path) for p in spec_rules.spec_problems(d)]


@pytest.mark.parametrize("name", specfix.SPEC_NAMES)
def test_valid_fixtures_have_no_problems(name):
    assert spec_rules.spec_problems(specfix.load_dict(name)) == []
    specfix.load_spec(name)


def test_word_cap_per_field(base):
    base["world"]["theme"] = "one two three four five six seven eight nine"
    base["a"]["hair"]["description"] = " ".join(["w"] * 13)
    base["a"]["dna"]["motif_object"] = "a b c d e f"
    found = codes(base)
    assert ("word_cap", "/world/theme") in found and ("word_cap", "/a/hair/description") in found
    assert ("word_cap", "/a/dna/motif_object") in found


def test_cap_boundary_passes(base):
    base["world"]["theme"] = "one two three four five six seven eight"          # exactly 8 words
    assert codes(base) == []


@pytest.mark.parametrize("bad", ["#12345", "#12345G", "123456", "red", "#1234567", ""])
def test_hex_pattern(base, bad):
    base["palette"][0]["hex"] = bad
    assert ("palette_hex", "/palette/0/hex") in codes(base)


def test_unique_palette_ids_and_reserved_none(base):
    base["palette"][1]["id"] = "p1"
    base["palette"][2]["id"] = "none"
    found = codes(base)
    assert ("palette_id_unique", "/palette/1/id") in found and ("palette_id", "/palette/2/id") in found


def test_palette_size_5_to_12(base):
    short = copy.deepcopy(base)
    short["palette"] = short["palette"][:4]
    assert ("palette_count", "/palette") in codes(short)
    long = copy.deepcopy(base)
    long["palette"] += [{"id": f"x{i}", "name": "extra", "hex": "#101010", "role": "accent"} for i in range(3)]
    assert ("palette_count", "/palette") in codes(long)


def test_dangling_and_none_refs(base):
    base["a"]["hair"]["colour_ref"] = "p99"
    base["a"]["top"]["base_ref"] = "none"                    # base colours may not be none
    base["a"]["top"]["trim_ref"] = "none"                    # trim may
    found = codes(base)
    assert ("ref_dangling", "/a/hair/colour_ref") in found
    assert ("ref_none", "/a/top/base_ref") in found
    assert not any(p == "/a/top/trim_ref" for _, p in found)


def test_anchor_and_contrast_counts(base):
    base["shared_anchors"] = base["shared_anchors"][:1]
    base["contrasts"] = base["contrasts"][:4]
    found = [c for c, _ in codes(base)]
    assert "anchor_count" in found and "contrast_count" in found
    base["shared_anchors"] = base["shared_anchors"] * 4
    assert any(c == "anchor_count" for c, _ in codes(base))


def test_prints_per_garment_and_colour_ref_counts(base):
    top = base["a"]["top"]
    top["prints"] = top["prints"] * 3
    base["a"]["bottom"]["prints"] = [copy.deepcopy(top["prints"][0]) for _ in range(2)]
    top["prints"][0]["colour_refs"] = []
    base["b"]["accessories"][0]["colour_refs"] = ["p1"] * 5
    found = codes(base)
    assert ("print_count", "/a/top/prints") in found and ("print_count", "/a/bottom/prints") in found
    assert ("colour_refs_count", "/a/top/prints/0/colour_refs") in found
    assert ("colour_refs_count", "/b/accessories/0/colour_refs") in found


def test_structure_note_rules(base):
    base["world"]["structure_note"] = "a note"
    assert ("structure_note", "/world/structure_note") in codes(base)
    base["world"]["pair_structure"] = "other"
    base["world"]["structure_note"] = ""
    assert ("structure_note", "/world/structure_note") in codes(base)
    base["world"]["structure_note"] = "two rivals on one team"
    assert codes(base) == []


def test_makeup_none_needs_empty_fields_and_arm_extras_unique(base):
    base["a"]["makeup"]["description"] = "freckles"
    base["a"]["top"]["arm_extras"] = ["gloves", "gloves"]
    found = codes(base)
    assert ("makeup_none", "/a/makeup") in found and ("arm_extras_unique", "/a/top/arm_extras") in found


def test_completeness_of_motifs_and_descriptions(base):
    base["a"]["top"]["prints"][0]["motif"] = ""
    base["b"]["accessories"][0]["description"] = " "
    found = codes(base)
    assert ("print_motif", "/a/top/prints/0/motif") in found and ("accessory_description", "/b/accessories/0/description") in found


def test_the_model_raises_with_structured_problems(base):
    base["a"]["hair"]["colour_ref"] = "p99"
    with pytest.raises(ValidationError) as ei:
        spec.DuoSpec.model_validate(base)
    probs = spec_rules.problems_from_error(ei.value)
    assert probs and probs[0].path == "/a/hair/colour_ref" and probs[0].rule == "refs"
    assert isinstance(ei.value.errors()[0]["ctx"]["error"], spec_rules.SpecRuleError)


def test_plan_set_problems_prefix_the_spec_index(base):
    base["a"]["hair"]["colour_ref"] = "p99"
    plan = {"specs": [base], "brief_constraints": [], "how_they_differ": "x " * 41}
    found = [(p.code, p.path) for p in spec_rules.plan_set_problems(plan)]
    assert ("word_cap", "/how_they_differ") in found and ("ref_dangling", "/specs/0/a/hair/colour_ref") in found


def test_iter_helpers_cover_every_free_text_and_ref_field(base):
    paths = {p for p, _, _ in spec_rules.iter_free_text(base)}
    assert {"/world/theme", "/world/story", "/a/dna/motif_object", "/b/accessories/0/description", "/a/top/prints/0/motif",
            "/a/bottom/shoes/motif", "/palette/0/name", "/shared_anchors/1/on_b", "/contrasts/0/a_value"} <= paths
    refs = {p for p, _, _ in spec_rules.iter_refs(base)}
    assert {"/a/face/iris_ref", "/b/hair/highlight_ref", "/a/top/prints/0/colour_refs/1", "/b/bottom/shoes/accent_ref",
            "/a/accessories/0/colour_refs/0"} <= refs

"""The DNA router test (APP_SPEC §3.2, bible §3.3, PRM-10): every template x the fixture specs x both characters.

* at most 2 DNA fields reach any prompt, and only from the routing row of the template;
* CHARACTER fields come from the asset's own character only (changing the other character's DNA never changes the prompt);
* at most 5 constraints (MUST lines);
* the router has no source for palette hexes, the colour plan, the story or the pair structure.
"""
from __future__ import annotations

import promptfix as P
import pytest

from duoskin.models.common import sha256_of
from duoskin.prompts import compiler, dna_router, registry, slots
from duoskin.prompts.limits import thr

TEMPLATES = P.image_ids()
SPEC_NAMES = ["spec_complement_gb", "spec_same_club_bb", "spec_mirror_gg", "spec_twins_bg", "spec_bg_min"]


def _compile_all(template_id, spec, char):
    """Compile every valid input variant; skip the combination when the spec has nothing for the template."""
    out = []
    ids = [template_id] + ([template_id + registry.BOOTSTRAP_SUFFIX] if registry.get(template_id).meta.bootstrap else [])
    for tid in ids:
        for inputs in P.input_variants(template_id, spec, char, all_flags=True):
            try:
                out.append((tid, inputs, compiler.compile(tid, spec, char, inputs)))
            except slots.PromptBuildError as exc:
                if "does not exist" in str(exc):
                    continue
                raise
    return out


@pytest.mark.parametrize("template_id", TEMPLATES)
@pytest.mark.parametrize("spec_name", SPEC_NAMES)
@pytest.mark.parametrize("char", ["a", "b"])
def test_router_limits_every_template_every_spec(specs, template_id, spec_name, char):
    spec = specs[spec_name]
    meta = registry.get(template_id).meta
    compiled = _compile_all(template_id, spec, char)
    allowed = set(dna_router.allowed_fields(template_id))
    for tid, inputs, cp in compiled:
        ctx = f"{tid} {inputs}"
        assert len(cp.dna_fields) <= thr("prm.dna_fields_max") == 2, ctx
        assert set(cp.dna_fields) <= allowed, ctx
        assert set(meta.dna_fields) == allowed, f"{template_id}: front matter dna_fields differ from the routing table"
        assert cp.must_lines <= thr("prm.must_max") == 5, ctx
        assert cp.character == char


@pytest.mark.parametrize("template_id", TEMPLATES)
@pytest.mark.parametrize("spec_name", SPEC_NAMES)
@pytest.mark.parametrize("char", ["a", "b"])
def test_own_character_only(specs, template_id, spec_name, char):
    """Change every CHARACTER DNA field of the other character: the prompt text and hash do not change."""
    spec = specs[spec_name]
    other_changed = P.with_other_dna_changed(spec, char)
    for tid, inputs, cp in _compile_all(template_id, spec, char):
        cp2 = compiler.compile(tid, other_changed, char, inputs)
        assert cp2.text == cp2.text and cp.text == cp2.text, f"{tid} {inputs}: the other character's DNA leaked"
        assert cp.sha256 == cp2.sha256


@pytest.mark.parametrize("template_id", [t for t in TEMPLATES if registry.get(t).meta.dna_fields])
def test_world_detail_level_is_shared_not_per_character(specs, template_id):
    """detail_level is WORLD: it reaches the prompt of A and of B alike, and only where the row lists it."""
    spec = specs["spec_complement_gb"]
    changed = P.with_world_changed(spec)
    for char in ("a", "b"):
        for tid, inputs, cp in _compile_all(template_id, spec, char):
            cp2 = compiler.compile(tid, changed, char, inputs)
            should_differ = "detail_level" in dna_router.allowed_fields(template_id) and (
                "detail_level" in cp.dna_fields or "detail_level" in cp2.dna_fields)
            assert (cp.text != cp2.text) == should_differ, f"{tid} {inputs}"


@pytest.mark.parametrize("template_id", [t for t in TEMPLATES if not registry.get(t).meta.dna_fields])
def test_dna_free_templates_ignore_all_dna(specs, template_id):
    spec = specs["spec_complement_gb"]
    assert dna_router.allowed_fields(template_id) == ()
    assert dna_router.route_dna(template_id, spec, "a") == []


@pytest.mark.parametrize("template_id", sorted(dna_router.ROUTING))
def test_route_rows_are_within_the_rules(specs, template_id):
    row = dna_router.ROUTING[template_id]
    assert len(row.fields) <= dna_router.MAX_DNA_FIELDS
    assert set(row.fields) <= set(dna_router.ROUTABLE)
    for part, fields in row.per_part:
        assert set(fields) <= set(row.fields) and len(fields) <= 2, (template_id, part)
    for char in ("a", "b"):
        for slot in dna_router.route_dna(template_id, specs["spec_complement_gb"], char, part=row.per_part[0][0] if row.per_part else None):
            if slot.scope == "character":
                assert slot.character == char and slot.source_path.startswith(f"/{char}/dna/")
            else:
                assert slot.character is None and slot.source_path == "/world/detail_level"


def test_every_routing_row_names_an_existing_template():
    for tid in dna_router.ROUTING:
        assert tid in registry.all_templates(), tid
    for tid, t in registry.all_templates().items():
        if t.meta.kind != "llm":
            assert set(t.meta.dna_fields) == set(dna_router.allowed_fields(tid)), tid


def test_character_field_needs_a_character(specs):
    with pytest.raises(ValueError):
        dna_router.route_dna("I1.concept_char", specs["spec_complement_gb"], None)


def test_part_rows_of_face_parts(specs):
    spec = specs["spec_complement_gb"]
    iris = [s.field for s in dna_router.route_dna("I3.face_part", spec, "a", part="iris")]
    brow = [s.field for s in dna_router.route_dna("I3.face_part", spec, "a", part="brow")]
    assert iris == ["shape_language", "detail_level"] and brow == ["shape_language"]
    assert [s.field for s in dna_router.route_dna("R1.face_part", spec, "a", part="iris")] == ["detail_level"]
    with pytest.raises(ValueError):
        dna_router.route_dna("I3.face_part", spec, "a", part="ear")


def test_joint_concept_routes_no_dna_at_all(specs):
    for spec in specs.values():
        assert dna_router.route_dna("I1j.concept_joint", spec, None) == []
        assert compiler.compile("I1j.concept_joint", spec, None).dna_fields == []


def test_router_never_sources_palette_story_plan_or_pair_structure(specs):
    spec = specs["spec_complement_gb"]
    for tid in dna_router.ROUTING:
        for char in ("a", "b"):
            for s in dna_router.route_dna(tid, spec, char, part=None):
                assert s.field in dna_router.ROUTABLE
                assert not any(h.hex.lower() in s.value.lower() for h in spec.palette)
                assert spec.world.story not in s.value


def test_a_hash_covers_the_text_and_is_stable(specs):
    spec = specs["spec_complement_gb"]
    h1 = compiler.compile("I1.concept_char", spec, "a")
    h2 = compiler.compile("I1.concept_char", spec, "a")
    assert h1.sha256 == h2.sha256 and h1.text == h2.text
    assert h1.sha256 != compiler.compile("I1.concept_char", spec, "b").sha256
    assert sha256_of(h1.text)  # hashable

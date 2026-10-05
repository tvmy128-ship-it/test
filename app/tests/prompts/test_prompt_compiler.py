"""prompts/compiler.py: template + spec + slots -> CompiledPrompt (APP_SPEC §10.0, bible §2): skeleton, determinism, degrade, errors."""
from __future__ import annotations

import dataclasses
import re

import promptfix as P
import pytest

from duoskin.models.spec import DuoSpec
from duoskin.prompts import compiler, registry, slots
from duoskin.prompts import template_lang as TL
from duoskin.prompts.catalog import default_ctx
from duoskin.prompts.limits import thr

LABELS = ("PURPOSE", "IMAGES", "SUBJECT", "MUST", "STYLE", "KEEP", "OUTPUT", "EXCLUDE")
SECTIONED = [t for t in P.image_ids(("image",)) if not registry.get(t).meta.flat]


def one(spec, tid="I2.print", char="a", **inputs):
    inputs = inputs or ({"print": "top.0"} if tid.startswith(("I2", "R2")) else {})
    return compiler.compile(tid, spec, char, inputs)


@pytest.mark.parametrize("tid", SECTIONED)
def test_sections_come_in_the_fixed_order_and_must_is_numbered(specs, tid):
    spec = specs["spec_complement_gb"]
    for inputs in P.input_variants(tid, spec, "a"):
        cp = compiler.compile(tid, spec, "a", inputs)
        labels = re.findall(r"^([A-Z]+):", cp.text, re.MULTILINE)
        idx = [LABELS.index(x) for x in labels]
        assert idx == sorted(idx) and {"PURPOSE", "SUBJECT", "MUST"} <= set(labels), (tid, labels)
        must = compiler.split_sections(cp.text)["MUST"].splitlines()
        assert [ln.split(".")[0] for ln in must] == [str(i) for i in range(1, len(must) + 1)], (tid, must)
        assert cp.must_lines == len(must) <= thr("prm.must_max")
        break


def test_compile_is_deterministic_and_the_hash_covers_everything_sent(specs):
    spec = specs["spec_complement_gb"]
    a, b = one(spec), one(spec)
    assert a == b and a.sha256 == b.sha256 and a.text == b.text
    assert one(spec, char="b", print="top.0").sha256 != a.sha256
    d = spec.model_dump(mode="json")
    d["a"]["top"]["prints"][0]["motif"] = "one simple fern shape"
    other = compiler.compile("I2.print", DuoSpec.model_validate(d, context={"skip_rules": True}), "a", {"print": "top.0"})
    assert other.sha256 != a.sha256 and "fern" in other.text


def test_the_hash_changes_with_the_template_version(specs, monkeypatch):
    spec = specs["spec_complement_gb"]
    before = one(spec)
    t = registry.get("I2.print")
    monkeypatch.setattr(t.meta, "version", t.meta.version + 1)
    after = one(spec)
    assert after.text == before.text and after.sha256 != before.sha256 and after.template_version == before.template_version + 1


def test_the_compiler_accepts_a_spec_dict(specs):
    spec = specs["spec_complement_gb"]
    assert compiler.compile("I1.concept_char", spec.model_dump(mode="json"), "a").text == compiler.compile("I1.concept_char", spec, "a").text


def test_the_house_style_block_is_verbatim_wherever_a_style_line_is_written(specs):
    """A template that names a style block writes the STYLE line verbatim (the bootstrap and first-draft variants), never a paraphrase."""
    ctx = default_ctx()
    for tid in P.image_ids(("image",)):
        meta = registry.get(tid).meta
        if meta.style_block == "none" or meta.flat:
            continue
        seen = 0
        for spec in specs.values():
            for char in ("a", "b"):
                for variant in [tid] + ([tid + "@s0"] if meta.bootstrap else []):
                    for inputs in P.input_variants(tid, spec, char, all_flags=True):
                        cp = compiler.compile(variant, spec, char, inputs)
                        if re.search(r"^STYLE:", cp.text, re.MULTILINE):
                            seen += 1
                            assert ctx.style_block(meta.style_block) in cp.text, variant
                        else:
                            assert ctx.style_block(meta.style_block) not in cp.text
        style_lines = [ln for ln in registry.get(tid).lines if ln.nodes and isinstance(ln.nodes[0], TL.Text) and ln.nodes[0].text.startswith("STYLE:")]
        conditional_on_bootstrap = any(isinstance(n, TL.Cond) for ln in style_lines for n in ln.nodes)
        if meta.bootstrap or not conditional_on_bootstrap:
            assert seen, f"{tid} names {meta.style_block} but never writes a STYLE line"
        else:
            assert not seen, f"{tid}: a template without an S0 variant writes its STYLE line only on bootstrap, so it must never write one"


def test_prompts_never_carry_hex_story_pair_words_or_colour_counts(specs):
    for name, spec in specs.items():
        for tid in ("I1.concept_char", "I1j.concept_joint", "I4.hair_front", "I2.print"):
            for char in ("a", "b") if tid != "I1j.concept_joint" else (None,):
                inputs = {"print": "top.0"} if tid == "I2.print" and spec.a.top.prints and spec.b.top.prints else None
                if tid == "I2.print" and inputs is None:
                    continue
                text = compiler.compile(tid, spec, char, inputs or {}).text
                assert not re.search(r"#[0-9A-Fa-f]{3,8}\b", text), (name, tid)
                assert spec.world.story.lower() not in text.lower()
                assert not re.search(r"\b(complement|same[_ ]club|mirror|seasonal|twins|mascot)\b", text, re.IGNORECASE), (name, tid, text)


def test_s0_bootstrap_lists_only_the_s0_images(specs):
    spec = specs["spec_complement_gb"]
    for base in [t for t in P.image_ids(("image",)) if registry.get(t).meta.bootstrap]:
        inputs = next(iter(P.input_variants(base, spec, "a")))
        full = compiler.compile(base, spec, "a", inputs)
        boot = compiler.compile(base + "@s0", spec, "a", inputs)
        assert boot.template_id == base + "@s0" and boot.sha256 != full.sha256
        assert len(boot.images) < len(full.images) and set(boot.images) <= set(full.images), base
        assert len(re.findall(r"Image \d+ =", boot.text)) == len(boot.images)


def test_a_template_without_a_bootstrap_variant_rejects_the_suffix(specs):
    with pytest.raises(slots.PromptBuildError, match="bootstrap"):
        compiler.compile("I7.fabric@s0", specs["spec_complement_gb"], None, {"fabric_id": "jersey_plain"})


def test_style_sheet_role_is_resolved_per_character(specs):
    spec = specs["spec_complement_gb"]
    assert "style_sheet_a" in one(spec, char="a", print="top.0").images
    assert "style_sheet_b" in one(spec, char="b", print="top.0").images


def test_recraft_flat_templates_have_no_labels_and_numbered_constraints(specs):
    spec = specs["spec_complement_gb"]
    for part in ("iris", "lash_upper", "brow", "mouth_closed", "mouth_open", "closed_lid_line"):
        cp = compiler.compile("R1.face_part", spec, "a", {"part": part})
        assert not re.search(r"^[A-Z]+:", cp.text, re.MULTILINE) and "1)" in cp.text and cp.must_lines <= 5, part
        assert cp.provider_fields["background"] == "sentinel" and cp.provider_fields["sentinel_hex"].startswith("#")
        assert "controls" in cp.provider_fields and not re.search(r"#[0-9A-Fa-f]{6}", cp.text), part
    r2 = compiler.compile("R2.print", spec, "a", {"print": "top.0"})
    assert "1)" in r2.text and r2.provider_fields["background"] in ("transparent", "opaque", "sentinel")


def test_tripo_text_prompts_are_at_most_three_sentences(specs):
    spec = specs["spec_complement_gb"]
    for view in ("back", "left", "right"):
        cp = compiler.compile("T2.edit_view", spec, "a", {"view": view, "fix_sentence": "Make the back flatter."})
        assert len([x for x in re.split(r"[.!?](?:\s|$)", cp.text) if x.strip()]) <= 3 and cp.must_lines == 0 and "text" in cp.text


def test_a_long_description_degrades_the_i1_prompt_but_keeps_it_within_budget(specs):
    d = specs["spec_complement_gb"].model_dump(mode="json")
    d["a"]["accessories"][0]["description"] = " ".join(["round"] * 15)
    d["a"]["top"]["prints"][0]["motif"] = " ".join(["leaf"] * 12)
    d["a"]["hair"]["description"] = " ".join(["curly"] * 12)
    spec = DuoSpec.model_validate(d, context={"skip_rules": True})
    cp = compiler.compile("I1.concept_char", spec, "a")
    meta = registry.get("I1.concept_char").meta
    s = compiler.split_sections(cp.text)
    assert len(cp.text) - len(s["STYLE"]) <= meta.budget.get("max_chars_excl_style", thr("prm.max_chars_excl_style"))
    assert len(cp.text) <= meta.budget.get("max_chars_total", thr("prm.max_chars_total"))


def test_every_prompt_of_every_fixture_stays_inside_its_budget(specs):
    for spec in specs.values():
        for tid in ("I1.concept_char", "I1j.concept_joint"):
            for char in (("a", "b") if tid == "I1.concept_char" else (None,)):
                cp = compiler.compile(tid, spec, char)
                meta = registry.get(tid).meta
                assert len(cp.text) <= meta.budget.get("max_chars_total", thr("prm.max_chars_total"))


# ---------------------------------------------------------------------------------------------- errors
def test_unknown_and_missing_inputs_are_errors(specs):
    spec = specs["spec_complement_gb"]
    with pytest.raises(slots.PromptBuildError, match="unknown input"):
        compiler.compile("I2.print", spec, "a", {"print": "top.0", "colour": "red"})
    with pytest.raises(slots.PromptBuildError, match="required"):
        compiler.compile("I3.face_part", spec, "a", {})
    with pytest.raises(slots.PromptBuildError, match="not one of"):
        compiler.compile("I3.face_part", spec, "a", {"part": "ear"})
    with pytest.raises(slots.PromptBuildError, match="must be a bool"):
        compiler.compile("I1.concept_char", spec, "a", {"mood": "yes"})
    with pytest.raises(slots.PromptBuildError, match="must be an int"):
        compiler.compile("I5.accessory_front", spec, "a", {"accessory": "0"})
    with pytest.raises(slots.PromptBuildError, match="must be a list"):
        compiler.compile("I11.repair", spec, "a", {"asset": "print", "form": "masked", "subject_sentence": "A leaf.", "edit": "fix it"})


def test_a_character_is_required_where_the_template_reads_one(specs):
    with pytest.raises(slots.PromptBuildError, match="character"):
        compiler.compile("I1.concept_char", specs["spec_complement_gb"], None)


def test_missing_print_or_accessory_is_a_build_error_not_a_crash(specs):
    spec = specs["spec_empty_bb"]
    with pytest.raises(slots.PromptBuildError, match="does not exist"):
        compiler.compile("I2.print", spec, "a", {"print": "top.0"})
    with pytest.raises(slots.PromptBuildError, match="does not exist"):
        compiler.compile("I5.accessory_front", spec, "a", {"accessory": 0})


def test_llm_templates_do_not_compile_here(specs):
    with pytest.raises(slots.PromptBuildError, match="LLM template"):
        compiler.compile("L3.planner", specs["spec_complement_gb"], None)


def test_model_written_slot_text_passes_the_free_text_lint_first(specs):
    d = specs["spec_complement_gb"].model_dump(mode="json")
    d["a"]["top"]["prints"][0]["motif"] = "a pikachu face"
    with pytest.raises(slots.FreeTextLintError) as ei:
        compiler.compile("I2.print", DuoSpec.model_validate(d, context={"skip_rules": True}), "a", {"print": "top.0"})
    assert "banned" in str(ei.value) and ei.value.path.endswith("/motif")
    d["a"]["dna"]["motif_object"] = "the letter A"
    with pytest.raises(slots.FreeTextLintError):
        compiler.compile("I1.concept_char", DuoSpec.model_validate(d, context={"skip_rules": True}), "a")


def test_a_fix_sentence_is_linted_and_user_text_never_becomes_a_slot(specs):
    spec = specs["spec_complement_gb"]
    for bad in ("Make it look like nike.", "Add the text hello.", "Make it 3 times larger.", "Do not draw the leaf."):
        with pytest.raises(slots.FreeTextLintError):
            compiler.compile("I2e.print_edit", spec, "a", {"fix_sentence": bad})
    ok = compiler.compile("I2e.print_edit", spec, "a", {"fix_sentence": "Make the leaf rounder."})
    assert "Make the leaf rounder." in ok.text


def test_a_slot_that_would_be_empty_in_a_required_place_is_an_error_not_none(specs):
    d = specs["spec_complement_gb"].model_dump(mode="json")
    d["a"]["top"]["prints"][0]["motif"] = ""
    with pytest.raises(slots.FreeTextLintError, match="empty"):
        compiler.compile("I2.print", DuoSpec.model_validate(d, context={"skip_rules": True}), "a", {"print": "top.0"})


def test_a_missing_phrase_is_a_loud_error(specs, monkeypatch):
    from duoskin.prompts import catalog

    ctx = default_ctx()
    broken = catalog.Phrases({k: v for k, v in ctx.phrases.data.items() if k != "item_noun"})
    bad = dataclasses.replace(ctx, phrases=broken)
    with pytest.raises(catalog.PhraseError):
        compiler.compile("I5.accessory_front", specs["spec_complement_gb"], "a", {"accessory": 0}, bad)


def test_a_dna_slot_cannot_be_filled_by_a_builder(specs, monkeypatch):
    def evil(a):
        return slots.Built(slots={"shape_language_line": "sneaky"})

    monkeypatch.setitem(slots.BUILDERS, "i2", (evil, 0))
    with pytest.raises(slots.PromptBuildError, match="only the router"):
        compiler.compile("I2.print", specs["spec_complement_gb"], "a", {"print": "top.0"})


def test_unknown_phrase_free_text_in_a_character_without_a_motif_drops_the_line(specs):
    spec = specs["spec_empty_bb"]
    cp = compiler.compile("I1.concept_char", spec, "a")
    assert "motif_object" not in cp.dna_fields and "Signature detail" not in cp.text
    assert not re.search(r"\bNone\b|\{|\}", cp.text)


def test_the_character_the_prompt_was_built_for_is_recorded(specs):
    assert one(specs["spec_complement_gb"], char="b", print="top.0").character == "b"
    assert compiler.compile("I1j.concept_joint", specs["spec_complement_gb"], None).character is None

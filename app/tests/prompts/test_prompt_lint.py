"""CHK-P01: the prompt lint PRM-01..PRM-12 as an ASSERT (FAILURE_MODES §5.3, bible §2.4). Every rule has a pass and a fail case."""
from __future__ import annotations

import dataclasses

import pytest

from duoskin.checks import policy
from duoskin.prompts import compiler, registry, slots
from duoskin.prompts.catalog import default_ctx, load_banned


@pytest.fixture()
def base(specs):
    spec = specs["spec_complement_gb"]
    cp = compiler.compile("I2.print", spec, "a", {"print": "top.0"})
    return spec, cp


def lint(spec, cp, tid=None, **kw):
    ctx = compiler.lint_ctx(tid or cp.template_id, spec, default_ctx(), **kw)
    return compiler.lint_prompt(cp, ctx)


def edit(cp, old, new, **kw):
    assert old in cp.text, old
    return dataclasses.replace(cp, text=cp.text.replace(old, new, 1), **kw)


def failing(results):
    return {(r.fm_ids[0], r.metric) for r in results if not r.passed}


def test_a_clean_prompt_passes_every_rule_and_the_check_is_an_assert(base):
    spec, cp = base
    res = lint(spec, cp)
    assert failing(res) == set() and len(res) >= 14
    assert all(r.check_id == "CHK-P01" and r.kind == policy.meta("CHK-P01").kind == "assert" for r in res)
    assert {r.fm_ids[0] for r in res} >= {"PRM-01", "PRM-02", "PRM-03", "PRM-04", "PRM-05", "PRM-06", "PRM-07", "PRM-09", "PRM-10", "PRM-12"}


def test_compile_raises_when_the_lint_fails(specs, monkeypatch):
    spec = specs["spec_complement_gb"]
    real = compiler.lint_prompt

    def bad(cp, ctx, banned=None):
        out = real(cp, ctx, banned)
        return [out[0].model_copy(update={"passed": False, "evidence": "forced"}), *out[1:]]

    monkeypatch.setattr(compiler, "lint_prompt", bad)
    with pytest.raises(compiler.PromptLintError) as ei:
        compiler.compile("I2.print", spec, "a", {"print": "top.0"})
    assert isinstance(ei.value, AssertionError) and ei.value.template_id == "I2.print" and "forced" in str(ei.value)
    assert compiler.compile("I2.print", spec, "a", {"print": "top.0"}, lint=False).text


def test_prm04_no_hex_codes(base):
    spec, cp = base
    assert ("PRM-04", "hex_in_prompt") in failing(lint(spec, edit(cp, "forest green", "#2F6B4F")))


@pytest.mark.parametrize("junk", ["{motif}", "None", "null", "undefined", "leaf,, shape", "leaf  shape", "}"])
def test_prm05_no_slot_leaks(base, junk):
    spec, cp = base
    assert ("PRM-05", "slot_leak") in failing(lint(spec, edit(cp, "one simple leaf shape", junk)))


def test_prm06_banned_vocabulary_anywhere_including_exclude(base):
    spec, cp = base
    assert any(m.startswith("banned_") for _, m in failing(lint(spec, edit(cp, "one simple leaf shape", "a nike swoosh"))))
    assert any(m.startswith("banned_") for _, m in failing(lint(spec, edit(cp, "watermark", "watermark, pikachu"))))
    assert not any(m.startswith("banned_") for _, m in failing(lint(spec, cp)))


def test_prm06_text_inviting_words_are_fine_in_the_fixed_lines_only(base):
    spec, cp = base
    assert "logos" in cp.text.split("EXCLUDE:")[1]                       # allowed where it is a negation
    assert ("PRM-02", "text_inviting_outside_OUTPUT_EXCLUDE") in failing(lint(spec, edit(cp, "one simple leaf shape", "a leaf with a logo")))


def test_prm10_story_pair_words_and_colour_counts_stay_out(base):
    spec, cp = base
    assert ("PRM-10", "story_in_prompt") in failing(lint(spec, edit(cp, "one simple leaf shape", spec.world.story)))
    assert ("PRM-10", "pair_structure_words") in failing(lint(spec, edit(cp, "one simple leaf shape", "a complement leaf")))
    assert ("PRM-10", "pair_structure_words") in failing(lint(spec, edit(cp, "one simple leaf shape", "a mirror leaf")))
    assert ("PRM-10", "colour_count_or_ratio") in failing(lint(spec, edit(cp, "forest green and near black", "three colours")))
    assert ("PRM-10", "colour_count_or_ratio") in failing(lint(spec, edit(cp, "forest green and near black", "60/30 split")))


def test_prm10_at_most_two_dna_fields(base):
    spec, cp = base
    assert ("PRM-10", "dna_fields") in failing(lint(spec, dataclasses.replace(cp, dna_fields=["shape_language", "detail_level", "motif_object"])))
    assert ("PRM-10", "dna_fields") not in failing(lint(spec, dataclasses.replace(cp, dna_fields=["shape_language", "detail_level"])))


@pytest.mark.parametrize("phrase", ["same as before", "as before", "like the last image", "try again", "once more", "the previous version",
                                    "unchanged from the last round"])
def test_prm12_prompts_are_stateless(base, phrase):
    spec, cp = base
    assert ("PRM-12", "stateless_reference") in failing(lint(spec, edit(cp, "one simple leaf shape", phrase)))


def test_prm01_must_lines_characters_and_exclude_nouns(base):
    spec, cp = base
    six = edit(cp, "5. Simple main shape", "5. Simple main shape")
    six = dataclasses.replace(six, text=six.text.replace("\nSTYLE:", "\n6. A sixth line.\nSTYLE:", 1))
    assert ("PRM-01", "must_lines") in failing(lint(spec, six))
    long_ = edit(cp, "one simple leaf shape", "one simple leaf shape " + "and a very long wandering description " * 40)
    assert ("PRM-01", "chars_excl_style") in failing(lint(spec, long_))
    many = edit(cp, "watermark", "watermark, " + ", ".join(f"thing{chr(97 + i)}" for i in range(12)))
    assert ("PRM-01", "exclude_nouns") in failing(lint(spec, many))
    twice = dataclasses.replace(cp, text=cp.text + "\nKEEP: a\nKEEP: b")
    assert ("PRM-01", "keep_exclude_one_line") in failing(lint(spec, twice))


def test_prm07_the_attached_images_equal_the_images_line(base):
    spec, cp = base
    assert ("PRM-07", "image_roles") in failing(lint(spec, dataclasses.replace(cp, images=["ref_crop"])))
    ctx = compiler.lint_ctx(cp.template_id, spec, default_ctx(), n_images=3)
    assert ("PRM-07", "image_roles") in failing(compiler.lint_prompt(cp, ctx))
    unnamed = dataclasses.replace(cp, text=cp.text.replace("Image 2 = ", "", 1))
    assert ("PRM-07", "image_roles") in failing(lint(spec, unnamed))
    assert ("PRM-07", "image_roles") not in failing(lint(spec, cp))


def test_prm09_the_house_style_block_is_verbatim(base):
    spec, cp = base
    paraphrased = edit(cp, "Clean cel-shaded cartoon game art", "Nice cartoon art")
    assert ("PRM-09", "house_style_verbatim") in failing(lint(spec, paraphrased))
    assert ("PRM-09", "house_style_verbatim") not in failing(lint(spec, cp))


def test_prm02_priming_words_stay_out_of_purpose_subject_and_must(base):
    spec, cp = base
    assert registry.priming_table()["I2.print"]
    word = min(registry.priming_table()["I2.print"])
    assert ("PRM-02", "priming_words_outside_EXCLUDE") in failing(lint(spec, edit(cp, "one simple leaf shape", f"a {word} leaf")))
    assert ("PRM-02", "priming_words_outside_EXCLUDE") not in failing(lint(spec, cp)), "priming words are allowed in EXCLUDE"


def test_prm03_a_transparent_asset_has_no_backdrop_words_and_says_so_in_output(base):
    spec, cp = base
    assert ("PRM-03", "backdrop_words") in failing(lint(spec, edit(cp, "one simple leaf shape", "a leaf on a white background")))
    assert ("PRM-03", "isolation_line_in_OUTPUT") in failing(lint(spec, edit(cp, "fully transparent background", "plain background")))
    opaque = compiler.compile("I1.concept_char", spec, "a")
    assert not [m for _, m in failing(lint(spec, opaque)) if m.startswith(("backdrop", "isolation"))]


def test_text_templates_get_the_reduced_rule_set(specs):
    spec = specs["spec_complement_gb"]
    cp = compiler.compile("T2.edit_view", spec, "a", {"view": "back", "fix_sentence": "Make the back flatter."})
    res = lint(spec, cp)
    assert failing(res) == set() and {m for _, m in [(r.fm_ids[0], r.metric) for r in res]} >= {"sentences", "chars_total"}
    assert "must_lines" not in {r.metric for r in res}
    long_ = dataclasses.replace(cp, text=cp.text + " One more. And another. And a last one.")
    assert ("PRM-01", "sentences") in failing(lint(spec, long_))


def test_a_lint_that_raises_fails_closed_through_the_runner(base):
    from duoskin.checks import runner

    spec, cp = base
    ctx = compiler.lint_ctx(cp.template_id, spec, default_ctx())
    out = runner.run_check("CHK-P01", cp.sha256, compiler.lint_prompt, cp=cp, ctx=None)
    assert not out.passed and not out.ran
    ok = runner.run_check("CHK-P01", cp.sha256, compiler.lint_prompt, cp=cp, ctx=ctx)
    assert ok.passed


def test_user_banned_terms_are_merged_into_the_lint(tmp_path, specs):
    extra = tmp_path / "banned_terms.extra.json"
    extra.write_text('["leaf"]', encoding="utf-8")
    spec = specs["spec_complement_gb"]
    ctx = dataclasses.replace(default_ctx(), banned=load_banned(extra_path=extra))
    with pytest.raises(slots.FreeTextLintError):
        compiler.compile("I2.print", spec, "a", {"print": "top.0"}, ctx)

"""prompts/template_lang.py: the tiny template language (bible §2.3 rules 1 and 5, PRM-05)."""
from __future__ import annotations

import pytest

from duoskin.prompts import template_lang as TL


def render(src, values=None, flags=None):
    return TL.render(TL.parse(src), values or {}, flags or {})


def test_slots_are_replaced_and_text_is_kept():
    assert render("A {colour} jacket.", {"colour": "teal"}) == "A teal jacket."


def test_a_missing_slot_is_a_template_bug_not_an_empty_string():
    with pytest.raises(TL.MissingSlot):
        render("A {colour} jacket.", {})
    with pytest.raises(TL.MissingSlot):
        render("A {colour} jacket.", {"colour": None})


def test_an_empty_required_slot_raises_and_an_optional_one_renders_nothing():
    with pytest.raises(TL.EmptySlot) as ei:
        render("A {colour} jacket.", {"colour": ""})
    assert ei.value.name == "colour"
    assert render("A{colour?} jacket.", {"colour": ""}) == "A jacket."


def test_an_optional_clause_drops_as_a_whole():
    src = "A jacket[[ with {trim} trim]]."
    assert render(src, {"trim": "yellow"}) == "A jacket with yellow trim."
    assert render(src, {"trim": ""}) == "A jacket."


def test_nested_clauses_drop_from_the_inside_out():
    src = "A[[ coat[[ with {trim}]]]]"
    assert render(src, {"trim": ""}) == "A coat"     # only the clause that holds the empty slot is dropped
    assert render(src, {"trim": "gold"}) == "A coat with gold"


def test_conditions_choose_by_flag_and_else_is_optional():
    src = "{{if mood}}happy{{else}}calm{{/if}} {{unless mood}}and quiet{{/unless}}"
    assert render(src, flags={"mood": True}) == "happy "
    assert render(src, flags={"mood": False}) == "calm and quiet"
    with pytest.raises(TL.MissingSlot):
        render(src, flags={})


@pytest.mark.parametrize("bad", ["{{else}}", "{{/if}}", "[[ never closed", "]]", "{{if x}}open", "a {b c} d", "a {{ b", "{Upper}"])
def test_malformed_templates_refuse_to_parse(bad):
    with pytest.raises(TL.TemplateSyntaxError):
        TL.parse(bad)


def test_slot_and_flag_inventory():
    nodes = TL.parse("{a} [[{b?} {{if f}}{c}{{/if}}]] {{unless g}}x{{/unless}}")
    assert TL.slots_used(nodes) == {"a", "b", "c"} and TL.flags_used(nodes) == {"f", "g"}
    assert TL.literal_text(TL.parse("x{{if f}}1{{else}}2{{/if}}y")) == "x12y"
    assert TL.literal_text(TL.parse("x{{if f}}1{{else}}2{{/if}}y"), {"f": True}) == "x1y"


def test_numbered_lines_with_an_empty_slot_drop_and_renumber():
    lines = TL.parse_lines("MUST:\n1. First {a}.\n2. Second {b}.\n3. Third {c}.")
    out = TL.render_lines(lines, {"a": "x", "b": "", "c": "z"}, {})
    assert out == "MUST:\n1. First x.\n2. Third z."


def test_a_numbered_line_may_use_a_bracket_delimiter_and_is_capitalised():
    out = TL.render_lines(TL.parse_lines("1) {a} one.\n2) {b} two."), {"a": "lower", "b": "also"}, {}, joiner=" ")
    assert out == "1) Lower one. 2) Also two."


def test_an_unnumbered_line_with_an_empty_required_slot_is_an_error():
    with pytest.raises(TL.EmptySlot):
        TL.render_lines(TL.parse_lines("OUTPUT: {x}"), {"x": ""}, {})


def test_a_label_with_nothing_after_it_vanishes_but_must_stays():
    lines = TL.parse_lines("KEEP:{{if k}} the light background{{/if}}\nMUST:\n1. One.")
    assert TL.render_lines(lines, {}, {"k": False}) == "MUST:\n1. One."
    assert TL.render_lines(lines, {}, {"k": True}).startswith("KEEP: the light background")


def test_double_spaces_are_tidied_and_rendering_is_deterministic():
    lines = TL.parse_lines("A  {x}   b.")
    a = TL.render_lines(lines, {"x": "y"}, {})
    assert a == "A y b." == TL.render_lines(lines, {"x": "y"}, {})

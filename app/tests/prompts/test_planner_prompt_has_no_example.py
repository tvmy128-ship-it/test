"""``test_planner_prompt_has_no_example`` (APP_SPEC §2 S12 / "Remove the single worked example from the planner prompt", bible §9.3).

The compiled L3 system prompt contains no JSON object with a ``palette`` key and none of the retired example words: a worked example
anchors every plan on its look. Everything the planner sees besides its role text (the style guide, the kit inventory, the structure
profiles) is checked too, because an example could hide in any cached block.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from duoskin.prompts import registry
from duoskin.prompts.llm import compile_llm

RETIRED_EXAMPLE_WORDS = ("night market", "lantern gold", "hyper", "deadpan")
PLANNER_INPUTS = {"brief_text": "two forest rangers", "structure_request": "auto", "must_include": "", "combo": "gb",
                  "reference_analysis": "none supplied", "taste_profile": "no taste profile yet", "recent_cards": "[]", "recently_used": "[]"}
DOCS = Path(__file__).resolve().parents[3] / "docs"


def json_objects(text: str):
    """Every JSON object (at any nesting depth) that parses inside ``text``."""
    dec = json.JSONDecoder()
    for m in re.finditer(r"\{", text):
        try:
            obj, _ = dec.raw_decode(text[m.start():])
        except ValueError:
            continue
        if isinstance(obj, dict):
            yield obj


def has_key(obj, key: str) -> bool:
    if isinstance(obj, dict):
        return key in obj or any(has_key(v, key) for v in obj.values())
    if isinstance(obj, list):
        return any(has_key(v, key) for v in obj)
    return False


def example_problems(text: str) -> list[str]:
    found = [f"JSON object with a palette key: {json.dumps(o)[:60]}" for o in json_objects(text) if has_key(o, "palette")]
    found += [f"palette key written out: {m.group(0)}" for m in re.finditer(r"""["']palette["']\s*:""", text)]
    found += [f"retired example word: {w}" for w in RETIRED_EXAMPLE_WORDS if re.search(rf"(?<![a-z]){re.escape(w)}", text, re.IGNORECASE)]
    return found


@pytest.mark.parametrize("replacement", [False, True])
def test_planner_prompt_has_no_example(replacement):
    extra = {"replacement": True, "wildcard_flag": "false", "dropped_reasons": "the hair kit repeats"} if replacement else {}
    p = compile_llm("L3.planner", {**PLANNER_INPUTS, **extra})
    system = "\n".join(b["text"] for b in p.system)
    assert example_problems(system) == [], "the planner prompt must not carry an example spec"
    assert example_problems(p.user_text) == []
    assert any(b["text"].startswith("<structure_profiles>") for b in p.system), "the checked prompt is the plan-loop one"
    assert example_problems(registry.get("L3.planner").role_text) == []


def test_the_planner_template_file_has_no_example_either():
    raw = (Path(registry.__file__).resolve().parent / "L3.planner.md").read_text(encoding="utf-8")
    assert example_problems(raw) == []
    assert "```" not in raw


def test_the_detector_would_catch_an_example():
    """The check is not vacuous: a prompt with a spec-shaped example or a retired word is flagged."""
    fixture = (Path(__file__).resolve().parents[1] / "spec" / "fixtures" / "spec_complement_gb.json").read_text(encoding="utf-8")
    assert example_problems("Example:\n" + fixture)
    assert example_problems('{"palette": [{"id": "p1"}]}')
    assert example_problems("A hyper-detailed night market with lantern gold trim.")
    assert example_problems("a deadpan expression")
    assert example_problems("Nothing to see here: a calm forest pair.") == []


def test_other_plan_loop_prompts_may_carry_specs_as_data_but_not_the_planner():
    """The critic gets a spec as data; that is not an example, and the detector must see the difference by route."""
    fixture = (Path(__file__).resolve().parents[1] / "spec" / "fixtures" / "spec_complement_gb.json").read_text(encoding="utf-8")
    critic = compile_llm("L4.critic", {"spec_id": "spc_1", "spec_json": fixture, "measured_facts": "{}", "brief_text": "two rangers"})
    assert example_problems(critic.user_text)
    assert example_problems("\n".join(b["text"] for b in critic.system)) == []


def test_the_bible_marks_the_example_as_removed():
    doc = DOCS / "PROMPT_BIBLE.md"
    if not doc.exists():
        pytest.skip("docs/ not present")
    assert "no example spec" in doc.read_text(encoding="utf-8")

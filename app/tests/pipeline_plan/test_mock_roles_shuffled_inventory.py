"""The mock planner stays lint-clean whatever order the kit inventory is shown in.

The real prompt lists the kit ids in a seeded, shuffled order (``order_seed``) and the mock reads them "like a lazy model": by position. With some
orders A and B got two tops of the same family and two bottoms of the same family ("tee" and "tee_long", "cargo_joggers" and "cargos"), CHK-G0-05
failed, the revision loop could not fix it, and about one duo in ten reached Gate 1 with two plans instead of three (found by clicking the app
through, tests/e2e_ui).
"""
from __future__ import annotations

import random

from test_mock_roles import LI, MR, PlanSet, inv  # noqa: F401  (``inv`` is the fixture)

from duoskin.prompts.llm import compile_llm
from duoskin.providers.mock.llm import RoleCall

INPUTS = {"brief_text": "two friends sharing a rainy-day picnic", "structure_request": "auto", "must_include": "none", "combo": "bg", "reference_analysis": "none",
          "taste_profile": "none", "recent_cards": "none", "recently_used": "none", "avoid": ""}


def planner_call(order_seed: int, seed: int) -> RoleCall:
    prompt = compile_llm("L3.planner", INPUTS, order_seed=order_seed)
    system = "\n".join(str(block.get("text", "")) for block in prompt.system)
    return RoleCall(route="L3_planner", out=PlanSet, schema={}, system_text=system, content_text=prompt.user_text, content=[], seed=seed, rng=random.Random(seed),
                    prompt_version=1)


def test_every_order_of_the_kit_inventory_gives_three_clean_plans(inv):  # noqa: F811
    unclean = []
    for k in range(150):
        out = MR.planner_builder(planner_call(order_seed=k, seed=1000 + k))
        b = LI.lint_candidates([(f"s{i}", s) for i, s in enumerate(out["specs"])], LI.lint_context(None), how_they_differ=out["how_they_differ"])
        if b.unclean() or not b.set_clean:
            unclean.append((k, str(b.unclean())[:200] if b.unclean() else "the set of three"))
    assert not unclean, f"{len(unclean)} of 150 inventory orders gave a plan that fails the rules: {unclean[:3]}"

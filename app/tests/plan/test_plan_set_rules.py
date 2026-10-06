"""C1 plan-set rules: exactly 3 specs, exactly 1 wildcard (always), the structure request, must-include lines, soft distribution."""
from __future__ import annotations

import planfix as F
import pytest

from duoskin.checks import plan_rules as P
from duoskin.checks import policy
from duoskin.models.spec import BriefConstraint, PlanSet, parse_plan_set


def _wild(d, on=True):
    d["is_wildcard"] = on
    return d


def mixed_auto():
    """Three different structures, the third is the wildcard (an open brief)."""
    return [F.spec_of(_wild(F.load("spec_complement_gb"), False)), F.spec_of(_wild(F.load("spec_same_club_bb"), False)),
            F.spec_of(_wild(F.load("spec_mirror_gg"), True))]


def same_structure(structure="complement"):
    a = _wild(F.load("spec_complement_gb"), False)
    b = _wild(F.load("spec_bg_min"), False)
    c = _wild(F.load("spec_complement_gb"), True)
    c["world"].update(palette_family="candy_bright", theme="neon night market runners")
    c["shared_anchors"] = [{**c["shared_anchors"][0], "kind": "trim"}]
    out = []
    for d in (a, b, c):
        d["world"]["pair_structure"] = structure
        out.append(F.spec_of(d))
    return out


def run(specs, **kw):
    ctx = kw.pop("ctx", {})
    return P.lint_plan_set(specs, P.PlanLintCtx(**ctx), **kw)


def get(rep, metric):
    return F.result(rep, metric)


def test_an_open_brief_with_three_structures_and_one_wildcard_passes_clean():
    rep = run(mixed_auto())
    assert rep.passed and rep.warnings == []
    assert get(rep, "plan_set_size").passed and get(rep, "wildcard_count").passed


@pytest.mark.parametrize("n", [0, 1, 2, 4])
def test_exactly_three_specs_is_hard(n):
    specs = (mixed_auto() * 2)[:n]
    rep = run(specs)
    r = get(rep, "plan_set_size")
    assert not r.passed and r.kind == "hard" and r.check_id == "CHK-G0-07" and not rep.passed


@pytest.mark.parametrize("flags", [(False, False, False), (True, True, False), (True, True, True)])
def test_exactly_one_wildcard_is_hard(flags):
    specs = mixed_auto()
    for s, f in zip(specs, flags, strict=True):
        s.is_wildcard = f
    rep = run(specs)
    r = get(rep, "wildcard_count")
    assert not r.passed and r.kind == "hard" and not rep.passed and r.value == sum(flags)


def test_the_wildcard_rule_holds_when_the_brief_fixes_the_structure():
    specs = same_structure()
    rep = run(specs, ctx={"structure_request": "complement"})
    assert rep.passed and get(rep, "structure_request").passed and get(rep, "wildcard_count").passed
    specs[2].is_wildcard = False
    rep = run(specs, ctx={"structure_request": "complement"})
    assert not get(rep, "wildcard_count").passed and not rep.passed


def test_an_explicit_structure_means_all_three_use_it_wildcard_included():
    specs = same_structure()
    specs[2].world.pair_structure = "mirror"                        # the wildcard strays
    rep = run(specs, ctx={"structure_request": "complement"})
    r = get(rep, "structure_request")
    assert not r.passed and r.kind == "hard" and r.check_id == "PLN-STR-01" and not rep.passed
    assert any(f.path == "/specs/2/world/pair_structure" for f in rep.findings if f.hard)


def test_the_request_is_case_insensitive_and_auto_is_the_default():
    assert get(run(same_structure(), ctx={"structure_request": " Complement "}), "structure_request").passed
    assert get(run(mixed_auto()), "structure_request").passed


def test_auto_allows_a_mix_and_a_two_plus_one_mix_is_only_soft():
    specs = mixed_auto()
    specs[1].world.pair_structure = "complement"                    # 2 + 1
    rep = run(specs)
    r = get(rep, "plan_set_distribution")
    assert rep.passed, "a 2+1 mix must never block"
    assert not r.passed and r.kind == "soft" and "structure mix" in r.evidence
    assert get(rep, "structure_request").passed


def test_three_of_one_structure_under_auto_is_also_soft_only():
    rep = run(same_structure())
    assert rep.passed and not get(rep, "plan_set_distribution").passed and get(rep, "plan_set_distribution").kind == "soft"


def test_two_plans_that_share_palette_family_and_anchor_kinds_warn():
    specs = mixed_auto()
    specs[1].world.palette_family = specs[0].world.palette_family
    specs[1].shared_anchors = list(specs[0].shared_anchors)
    rep = run(specs, ctx={"structure_request": "auto"})
    assert get(rep, "plan_set_distribution").passed, "under auto, different structures already make two plans different"
    specs[1].world.pair_structure = specs[0].world.pair_structure
    rep = run(specs, ctx={"structure_request": "auto"})
    r = get(rep, "plan_set_distribution")
    assert rep.passed and not r.passed and "differ in neither palette family nor anchor kinds nor structure" in r.evidence
    rep = run(specs, ctx={"structure_request": "complement"})
    assert "differ in neither palette family nor anchor kinds" in get(rep, "plan_set_distribution").evidence


def test_the_wildcard_must_depart_from_the_others_softly():
    specs = same_structure()
    w = specs[2]
    w.world.palette_family = specs[0].world.palette_family
    w.world.theme = specs[0].world.theme
    w.shared_anchors = list(specs[0].shared_anchors)
    rep = run(specs, ctx={"structure_request": "complement"})
    r = get(rep, "plan_set_distribution")
    assert not r.passed and "wildcard does not depart" in r.evidence and rep.passed


# ---------------------------------------------------------------------------------------------- must-include lines (C1 #20)
def test_must_include_lines_need_a_constraint_with_resolving_paths():
    specs = mixed_auto()
    ctx = {"must_include": ("a red scarf on A",)}
    rep = run(specs, ctx=ctx, brief_constraints=[])
    r = get(rep, "brief_constraints")
    assert not r.passed and r.kind == "hard" and "missing from brief_constraints" in r.evidence
    ok = [BriefConstraint(text="a red scarf on A", spec_paths=["/a/accessories/0"])]
    assert get(run(specs, ctx=ctx, brief_constraints=ok), "brief_constraints").passed


def test_a_path_that_does_not_resolve_in_every_spec_fails():
    specs = mixed_auto()
    ctx = {"must_include": ("a red scarf on A",)}
    bad = [BriefConstraint(text="a red scarf on A", spec_paths=["/b/accessories/5"])]
    r = get(run(specs, ctx=ctx, brief_constraints=bad), "brief_constraints")
    assert not r.passed and "does not resolve in spec 0" in r.evidence
    none = [BriefConstraint(text="a red scarf on A", spec_paths=[])]
    assert not get(run(specs, ctx=ctx, brief_constraints=none), "brief_constraints").passed


def test_no_must_include_lines_means_nothing_to_map():
    assert get(run(mixed_auto()), "brief_constraints").passed


def test_how_they_differ_gets_the_light_lint():
    specs = mixed_auto()
    assert get(run(specs, how_they_differ="Forest rangers, a robot garage and a skate park."), "how_they_differ").passed
    assert not get(run(specs, how_they_differ="All three are pikachu fans."), "how_they_differ").passed
    assert not get(run(specs, how_they_differ=" ".join(["word"] * 60)), "how_they_differ").passed


# ---------------------------------------------------------------------------------------------- the whole plan
def _plan_dict(specs):
    return {"specs": [s.model_dump(mode="json") for s in specs], "brief_constraints": [], "how_they_differ": "Three different worlds."}


def test_lint_plan_runs_set_and_every_spec_rule():
    rep = P.lint_plan(_plan_dict(mixed_auto()))
    assert rep.passed and len(rep.per_spec) == 3 and rep.set_report.passed
    assert len(rep.all_results) == len(rep.set_report.results) + sum(len(r.results) for r in rep.per_spec)


def test_one_bad_spec_fails_the_plan_and_names_its_reports():
    plan = _plan_dict(mixed_auto())
    plan["specs"][1]["a"]["top"]["base_ref"] = "p99"
    rep = P.lint_plan(plan)
    assert not rep.passed and rep.set_report.passed
    assert [r.passed for r in rep.per_spec] == [True, False, True]


def test_the_strict_parser_agrees_with_the_linter_on_a_good_plan():
    plan = parse_plan_set(_plan_dict(mixed_auto()))
    assert isinstance(plan, PlanSet) and P.lint_plan(plan).passed


def test_plan_set_rule_kinds_come_from_the_registry():
    rep = run(mixed_auto())
    for r in rep.results:
        assert r.kind == policy.meta(r.check_id).kind

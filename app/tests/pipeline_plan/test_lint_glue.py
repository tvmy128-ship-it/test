"""``pipeline/lint.py``: the C1 glue (bundle, findings for the reviser, the wildcard rule, the Gate 1 set shape)."""
from __future__ import annotations

import copy

import pytest
import specfix

from duoskin.checks.plan_rules import PlanLintCtx
from duoskin.models.spec import BriefConstraint
from duoskin.pipeline import lint as LI

pytestmark = pytest.mark.usefixtures("demo_inv")

TRIO = ("spec_complement_gb", "spec_same_club_bb", "spec_mirror_gg")


def trio(wildcard: int = 0) -> list[dict]:
    out = [specfix.load_dict(n) for n in TRIO]
    for i, s in enumerate(out):
        s["is_wildcard"] = i == wildcard
    return out


def bundle(specs, **kw):
    return LI.lint_candidates([(f"s{i}", s) for i, s in enumerate(specs)], PlanLintCtx(), **kw)


def test_the_fixture_plans_are_clean_as_a_set():
    b = bundle(trio())
    assert b.unclean() == [] and b.set_clean
    assert b.order == ["s0", "s1", "s2"]
    for sid in b.order:
        assert b.results(sid), "every spec carries its own results plus the set results"
        assert all(r.passed for r in b.results(sid) if r.kind in ("hard", "assert"))


def test_a_hard_failure_makes_only_that_spec_unclean_and_gives_a_finding_at_its_own_path():
    specs = trio()
    specs[1]["a"]["top"]["base_ref"] = "p99"                  # a palette colour that does not exist
    b = bundle(specs)
    assert b.unclean() == ["s1"]
    findings = LI.reviser_findings(b.hard_findings("s1"))
    assert findings and findings[0]["number"] == 1
    assert any(f["path"] == "/a/top/base_ref" for f in findings)
    text = LI.findings_text(findings)
    assert text.startswith("1. /a/top/base_ref:")
    assert "/a/top/base_ref" in LI.finding_paths(findings)


def test_two_wildcards_flag_the_one_that_departs_least_and_exactly_one_spec_changes():
    specs = trio(wildcard=0)
    specs[2]["is_wildcard"] = True
    b = bundle(specs)
    flagged = [s for s in b.order if any(f.path == "/is_wildcard" for f in b.hard_findings(s))]
    assert len(flagged) == 1
    want = LI.wildcard_targets([LI.parse_loose(s) for s in specs])
    assert list(want.values()) == [False] and b.order[next(iter(want))] == flagged[0]


def test_no_wildcard_flags_the_spec_that_departs_most():
    specs = trio()
    specs[0]["is_wildcard"] = False
    want = LI.wildcard_targets([LI.parse_loose(s) for s in specs])
    assert len(want) == 1 and list(want.values()) == [True]
    b = bundle(specs)
    assert not b.set_clean or b.unclean(), "a missing wildcard is a HARD set finding"


def test_wildcard_targets_is_empty_when_there_is_exactly_one():
    assert LI.wildcard_targets([LI.parse_loose(s) for s in trio()]) == {}


def test_a_structure_the_form_asked_for_is_a_hard_set_rule():
    ctx = PlanLintCtx(structure_request="mirror")
    b = LI.lint_candidates([(f"s{i}", s) for i, s in enumerate(trio())], ctx)
    assert b.unclean(), "the complement and same_club specs break a request for mirror"


def test_brief_constraints_must_resolve_in_every_spec():
    cons = [BriefConstraint(text="a teal bow", spec_paths=["/a/does/not/exist"])]
    b = bundle(trio(), brief_constraints=cons, how_they_differ="they differ")
    assert b.envelope_findings or b.unclean(), "a must-include line that points nowhere is a finding"
    ok = bundle(trio(), brief_constraints=[BriefConstraint(text="a bow", spec_paths=["/a/accessories/0"])], how_they_differ="they differ")
    assert ok.set_clean


def test_relaxed_forgets_the_wildcard_count_and_the_envelope():
    specs = trio()
    specs[0]["is_wildcard"] = False
    b = bundle(specs)
    r = b.relaxed()
    assert r.unclean() == [] and r.envelope_findings == []
    assert r.order == b.order and r.per_spec is b.per_spec


def test_critic_findings_keep_only_high_severity_and_continue_the_numbering():
    fixes = [{"path": "/a/hair/description", "problem": "too long", "severity": "high", "direction": "shorten it"},
             {"path": "/b/hair/description", "problem": "meh", "severity": "low", "direction": ""},
             {"path": "/a/top/neckline", "problem": "unclear", "severity": "medium", "direction": ""}]
    rows = LI.critic_findings(fixes, start=3)
    assert [r["number"] for r in rows] == [3] and rows[0]["rule"] == "critic" and "shorten it" in rows[0]["problem"]
    assert len(LI.critic_findings(fixes, start=1, min_severity="medium")) == 2


def test_gate1_set_shape_the_three_plan_case_and_the_notices():
    ok = LI.gate1_set_shape(trio())
    assert ok.ok and ok.notice == ""
    two = LI.gate1_set_shape(trio()[:2], wildcard_dropped=False, dropped_reasons=["a rule failed"])
    assert two.ok and "only 2 plans" in two.notice and "a rule failed" in two.notice
    specs = trio()
    specs[0]["is_wildcard"] = False
    nw = LI.gate1_set_shape(specs, wildcard_dropped=True)
    assert nw.ok and nw.notice == LI.WILDCARD_NOTICE


def test_gate1_set_check_is_an_assert_that_fails_only_without_front_and_back():
    good = LI.gate1_set_check(trio(), front_back_ready=True)
    assert good.check_id == "CHK-G1-07" and good.kind == "assert" and good.passed
    bad = LI.gate1_set_check(trio(), front_back_ready=False)
    assert not bad.passed and "front or back" in bad.evidence


def test_the_lint_module_has_no_numeric_literals_beyond_the_allowed_ones():
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(LI))
    bad = [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)) and not isinstance(n.value, bool)
           and n.value not in (0, 1, 2, -1)]
    assert not bad, bad


def test_parse_loose_accepts_a_spec_the_strict_parse_would_refuse():
    d = copy.deepcopy(trio()[0])
    d["a"]["top"]["base_ref"] = "p99"
    assert LI.parse_loose(d).a.top.base_ref == "p99"

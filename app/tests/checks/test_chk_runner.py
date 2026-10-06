"""checks/runner.py: fail-closed wrapper (test_fail_closed, APP_SPEC §3.1 / §17.2)."""
from __future__ import annotations

import pytest

from duoskin.checks import thresholds
from duoskin.checks.model import CheckResult, gate_verdict
from duoskin.checks.runner import (
    AssertCheckError,
    CheckOutcome,
    CheckUnavailable,
    applicability,
    build_result,
    fail_closed,
    raise_on_assert_failure,
    run_check,
    run_checks,
)


def test_exception_is_ran_false_passed_false():
    def boom(**_):
        raise RuntimeError("renderer crashed")

    r = run_check("A_PALETTE", "sha1", boom)
    assert r.ran is False and r.passed is False and r.status == "not_run"
    assert "renderer crashed" in r.evidence and r.subject_sha == "sha1"
    assert r.kind == "hard"
    assert gate_verdict([r]) == "fail"


@pytest.mark.parametrize("exc", [ImportError("no module named rapidocr"), ModuleNotFoundError("onnxruntime"), OSError("dll"), AssertionError("x"),
                                 ValueError("v"), KeyError("k"), ZeroDivisionError()])
def test_every_exception_type_fails_closed(exc):
    def raiser():
        raise exc

    r = run_check("A_OCR", "s", raiser)
    assert (r.ran, r.passed) == (False, False)


def test_keyboard_interrupt_is_not_swallowed():
    def stop():
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        run_check("A_OCR", "s", stop)


def test_check_unavailable_is_not_run():
    def f():
        raise CheckUnavailable("model file missing")

    r = run_check("A_CLONE", "s", f)
    assert not r.ran and "model file missing" in r.evidence


def test_garbage_output_fails_closed():
    assert run_check("A_ALPHA", "s", lambda: None).ran is False
    assert run_check("A_ALPHA", "s", lambda: 3.5).ran is False
    assert run_check("A_ALPHA", "s", lambda: {"nope": 1}).ran is False
    assert run_check("A_ALPHA", "s", list).ran is False


def test_outputs_bool_dict_outcome_and_result():
    assert run_check("A_MARGIN", "s", lambda: True).passed is True
    assert run_check("A_MARGIN", "s", lambda: False).passed is False
    r = run_check("A_MARGIN", "s", lambda: {"passed": True, "metric": "m", "value": 0.5, "evidence": "e"})
    assert r.passed and r.metric == "m" and r.value == 0.5
    r = run_check("A_MARGIN", "s", lambda: CheckOutcome(False, "m", 1.0, "t", "ev", "code_recrop"))
    assert not r.passed and r.fix_hint == "code_recrop" and r.ran
    inner = build_result("A_MARGIN", passed=False, metric="mm", evidence="x", fix_hint="code_recrop")
    r = run_check("A_MARGIN", "SHA", lambda: inner)
    assert r.subject_sha == "SHA" and r.metric == "mm" and not r.passed


def test_registry_decides_kind_and_versions():
    liar = CheckResult(check_id="whatever", kind="soft", passed=False, thresholds_version="old")
    r = run_check("A_OCR", "s", lambda: liar)
    assert r.check_id == "A_OCR" and r.kind == "hard" and r.thresholds_version == thresholds.THRESHOLDS_VERSION
    assert r.fm_ids == ["POL-04"]
    soft = run_check("A_SWATCH", "s", lambda: build_result("A_SWATCH", passed=False))
    assert soft.kind == "soft"
    unknown = run_check("NOT-IN-REGISTRY", "s", lambda: False)
    assert unknown.kind == "soft" and unknown.ran


def test_facts_are_filtered_to_the_signature():
    seen = {}

    def fn(a, b=2):
        seen.update(a=a, b=b)
        return True

    run_check("A_MARGIN", "s", fn, a=1, head_base_present=False, extra="dropped")
    assert seen == {"a": 1, "b": 2}

    def fn_kw(**kw):
        seen.clear()
        seen.update(kw)
        return True

    run_check("A_MARGIN", "s", fn_kw, x=1, y=2)
    assert seen == {"x": 1, "y": 2}


def test_not_applicable_when_head_base_flag_says_absent():
    called = []

    def fn():
        called.append(1)
        return False

    r = run_check("F_WARP_IOU", "s", fn, head_base_present=False)
    assert r.status == "not_applicable" and r.passed and r.ran and r.na_reason == "no_head_base" and not called
    assert gate_verdict([r]) == "pass"
    # with the kit present the check runs; a missing input fails closed
    r2 = run_check("F_WARP_IOU", "s", lambda: (_ for _ in ()).throw(KeyError("render")), head_base_present=True)
    assert r2.status == "not_run" and not r2.passed
    r3 = run_check("F_WARP_IOU", "s", lambda: True, head_base_present=True)
    assert r3.status == "passed" and r3.passed


def test_body_base_flag_and_flag_unknown():
    r = run_check("CHK-B10", "s", lambda: True, body_base_present=False)
    assert r.status == "not_applicable" and r.na_reason == "no_body_base"
    ran = []
    run_check("CHK-B10", "s", lambda: ran.append(1) or True)          # flag not supplied: the check itself decides (and fails closed)
    assert ran == [1]


def test_a_check_body_can_never_declare_itself_not_applicable():
    """APP_SPEC S26: only the runner sets N/A (from the registry's requires flags), so an exception in a kit-dependent check is a fail."""
    assert not hasattr(__import__("duoskin.checks.runner", fromlist=["x"]), "NotApplicable")

    def fn():
        raise RuntimeError("no_hair_kit")

    r = run_check("A_MARGIN", "s", fn)
    assert r.status == "not_run" and not r.passed
    r2 = run_check("F_WARP_IOU", "s", fn, head_base_present=True)           # kit present, check body crashed: a fail, never N/A
    assert r2.status == "not_run" and not r2.passed


def test_applicability_function_and_negative_requirements():
    from duoskin.checks import policy

    m = policy.meta("F_WARP_IOU")
    assert applicability(m, {"head_base_present": False}) == "no_head_base"
    assert applicability(m, {"head_base_present": True}) is None
    assert applicability(m, {}) is None                                      # an unsupplied flag never rules a check out
    assert applicability(policy.meta("CHK-B10"), {"body_base_present": False}) == "no_body_base"
    assert applicability(policy.meta("CHK-S09"), {"blender_present": False}) == "no_blender"
    a17 = policy.meta("CHK-A17")                                             # the 2D profile runs only WITHOUT a head base
    assert a17.requires == ["!head_base_present"]
    assert applicability(a17, {"head_base_present": False}) is None
    assert applicability(a17, {"head_base_present": True}) == "has_head_base"


def test_manifest_flags_mapping_fact():
    r = run_check("CHK-B10", "s", lambda: True, manifest_flags={"body_base_present": False, "head_base_present": True})
    assert r.status == "not_applicable" and r.na_reason == "no_body_base"
    r = run_check("CHK-A17", "s", lambda: True, manifest_flags={"head_base_present": True})
    assert r.status == "not_applicable" and r.na_reason == "has_head_base"
    r = run_check("CHK-A17", "s", lambda: True, manifest_flags={"head_base_present": False})
    assert r.status == "passed"


def test_list_results_are_aggregated_worst_case():
    ok = build_result("A_ALPHA", passed=True)
    bad = build_result("A_HALO", passed=False, evidence="halo")
    r = run_check("A_ALPHA", "s", lambda: [ok, bad])
    assert not r.passed and "halo" in r.evidence
    assert run_check("A_ALPHA", "s", lambda: [ok, ok]).passed
    nr = fail_closed("A_HALO", "x")
    assert not run_check("A_ALPHA", "s", lambda: [ok, nr]).ran


def test_evidence_is_kept_short():
    r = run_check("A_MARGIN", "s", lambda: CheckOutcome(True, evidence="x" * 5000))
    assert len(r.evidence) <= thresholds.get("runner.evidence_max")


def test_run_checks_each_fail_closed_independently():
    out = run_checks([("A_MARGIN", lambda: True, {}), ("A_OCR", lambda: 1 / 0, {}), ("A_ALPHA", lambda: False, {})], "sha")
    assert [r.passed for r in out] == [True, False, False]
    assert [r.ran for r in out] == [True, False, True]


def test_assert_failure_raises_but_content_failure_does_not():
    hard_fail = run_check("A_MARGIN", "s", lambda: False)
    raise_on_assert_failure([hard_fail])        # a failing HARD check is a content problem, not a bug
    assert_fail = run_check("A_INGEST", "s", lambda: False)
    assert assert_fail.kind == "assert"
    with pytest.raises(AssertCheckError):
        raise_on_assert_failure([assert_fail])
    crashed = run_check("A_INGEST", "s", lambda: 1 / 0)
    with pytest.raises(AssertCheckError):
        raise_on_assert_failure([crashed])


def test_demoted_check_reports_soft():
    from duoskin.checks import policy

    with policy.check_overrides({"A_PALETTE": "soft"}):
        r = run_check("A_PALETTE", "s", lambda: False)
    assert r.kind == "soft" and gate_verdict([r]) == "pass"
    assert run_check("A_PALETTE", "s", lambda: False).kind == "hard"

"""Fail-closed check runner (APP_SPEC §3.1 "Checks fail closed", FAILURE_MODES §0.3 rule 4).

``run_check(check_id, subject_sha, fn, **facts) -> CheckResult`` wraps one check:

* an exception, a missing optional dependency or garbage output gives ``ran=False, passed=False`` (never a pass);
* a check whose registry entry ``requires`` a kit (``head_base`` / ``body_base``) is not called at all when the manifest
  flag (``head_base_present=False`` / ``body_base_present=False`` in the facts) says the kit is absent: it returns
  ``status="not_applicable"`` through ``checks.model.not_applicable`` with the reason ``no_head_base`` / ``no_body_base``.
  That is different from ``ran=False``: it counts as passed and never blocks;
* ``kind``, ``fm_ids`` and ``thresholds_version`` always come from the registry and the threshold table, never from the
  check function, so a check cannot silently re-label itself HARD or SOFT.

The check function ``fn`` receives the facts it declares (extra facts such as ``head_base_present`` are dropped unless
``fn`` takes ``**kwargs``). It may return a ``CheckResult``, a ``CheckOutcome``, a ``bool``, a ``dict`` of outcome fields
or a list of ``CheckResult`` (aggregated, worst case wins). It may raise ``NotApplicable(reason)`` or
``CheckUnavailable(why)``.
"""
from __future__ import annotations

import inspect
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from duoskin.checks import policy, thresholds
from duoskin.checks.model import CheckResult, FixHint, not_applicable, not_run

MAX_EVIDENCE = 600


class NotApplicable(Exception):
    """Raised by a check function that finds, while running, that it does not apply (for example ``no_head_base``)."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class CheckUnavailable(Exception):
    """Raised when a check cannot run (a missing dependency, an unreadable input). The result is ``ran=False``."""


class AssertCheckError(AssertionError):
    """A failed ASSERT check is a bug, not a content problem (FAILURE_MODES §0.1)."""


@dataclass
class CheckOutcome:
    """The plain verdict of a check function; ``run_check`` turns it into a ``CheckResult``."""

    passed: bool
    metric: str = ""
    value: float | None = None
    threshold: str = ""
    evidence: str = ""
    fix_hint: FixHint = "none"
    ran: bool = True


def _short(text: str) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= MAX_EVIDENCE else text[: MAX_EVIDENCE - 3] + "..."


def build_result(check_id: str, *, passed: bool, subject_sha: str = "", metric: str = "", value: float | None = None,
                 threshold: str = "", evidence: str = "", fix_hint: FixHint = "none", ran: bool = True) -> CheckResult:
    """Build a ``CheckResult`` with kind, fm_ids and thresholds_version taken from the registry (used by every check)."""
    m = policy.meta(check_id)
    kind = policy.effective_kind(check_id, declared=m.kind)
    return CheckResult(check_id=check_id, fm_ids=list(m.fm_ids), subject_sha=subject_sha, kind=kind, passed=bool(passed) and ran,
                       metric=metric, value=None if value is None else float(value), threshold=threshold,
                       evidence=_short(evidence), ran=ran, fix_hint=fix_hint if not passed else "none",
                       thresholds_version=thresholds.THRESHOLDS_VERSION)


def fail_closed(check_id: str, why: str, subject_sha: str = "") -> CheckResult:
    """A check that could not run: ``ran=False, passed=False``."""
    m = policy.meta(check_id)
    kind = policy.effective_kind(check_id, declared=m.kind)
    return not_run(check_id, kind, _short(why), fm_ids=list(m.fm_ids), subject_sha=subject_sha,
                   thresholds_version=thresholds.THRESHOLDS_VERSION)


def not_applicable_result(check_id: str, reason: str, subject_sha: str = "") -> CheckResult:
    m = policy.meta(check_id)
    kind = policy.effective_kind(check_id, declared=m.kind)
    return not_applicable(check_id, kind, reason, fm_ids=list(m.fm_ids), subject_sha=subject_sha,
                          thresholds_version=thresholds.THRESHOLDS_VERSION)


def _accepted_facts(fn: Callable[..., Any], facts: dict[str, Any]) -> dict[str, Any]:
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return facts
    params = sig.parameters
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return facts
    return {k: v for k, v in facts.items() if k in params}


def _applicability(m: policy.CheckMeta, facts: dict[str, Any]) -> str | None:
    for req in m.requires:
        if facts.get(f"{req}_present") is False:
            return f"no_{req}"
    return None


def _aggregate(check_id: str, results: list[CheckResult], subject_sha: str) -> CheckResult:
    if not results:
        return fail_closed(check_id, "the check returned no results", subject_sha)
    ran = all(r.ran for r in results)
    passed = ran and all(r.passed for r in results)
    ev = "; ".join(f"{r.check_id}: {r.evidence}" for r in results if not r.passed or not r.ran) or "all parts passed"
    worst = next((r for r in results if not r.passed), results[0])
    return build_result(check_id, passed=passed, subject_sha=subject_sha, metric=worst.metric, value=worst.value,
                        threshold=worst.threshold, evidence=ev, fix_hint=worst.fix_hint, ran=ran)


def _normalise(check_id: str, subject_sha: str, out: Any) -> CheckResult:
    if isinstance(out, CheckResult):
        # the registry decides the kind; the function decides the verdict
        m = policy.meta(check_id)
        kind = policy.effective_kind(check_id, declared=m.kind)
        upd: dict[str, Any] = {
            "check_id": check_id, "kind": kind, "subject_sha": subject_sha or out.subject_sha,
            "fm_ids": out.fm_ids or list(m.fm_ids), "thresholds_version": thresholds.THRESHOLDS_VERSION,
            "evidence": _short(out.evidence),
        }
        return CheckResult.model_validate({**out.model_dump(), **upd})
    if isinstance(out, CheckOutcome):
        return build_result(check_id, passed=out.passed, subject_sha=subject_sha, metric=out.metric, value=out.value,
                            threshold=out.threshold, evidence=out.evidence, fix_hint=out.fix_hint, ran=out.ran)
    if isinstance(out, bool):
        return build_result(check_id, passed=out, subject_sha=subject_sha)
    if isinstance(out, dict):
        allowed = {"passed", "metric", "value", "threshold", "evidence", "fix_hint", "ran"}
        extra = set(out) - allowed
        if extra or "passed" not in out:
            return fail_closed(check_id, f"unusable check output (keys {sorted(out)})", subject_sha)
        return build_result(check_id, subject_sha=subject_sha, **out)
    if isinstance(out, (list, tuple)) and out and all(isinstance(r, CheckResult) for r in out):
        return _aggregate(check_id, list(out), subject_sha)
    return fail_closed(check_id, f"unusable check output of type {type(out).__name__}", subject_sha)


def run_check(check_id: str, subject_sha: str, fn: Callable[..., Any], **facts: Any) -> CheckResult:
    """Run one check, fail closed. See the module docstring for the contract."""
    try:
        m = policy.meta(check_id)
        why_na = _applicability(m, facts)
        if why_na:
            return not_applicable_result(check_id, why_na, subject_sha)
        out = fn(**_accepted_facts(fn, facts))
        return _normalise(check_id, subject_sha, out)
    except NotApplicable as e:
        return not_applicable_result(check_id, e.reason or "not_applicable", subject_sha)
    except CheckUnavailable as e:
        return fail_closed(check_id, f"unavailable: {e}", subject_sha)
    except (KeyboardInterrupt, SystemExit, GeneratorExit):
        raise
    except BaseException as e:  # noqa: BLE001 - fail closed on anything else, including AssertionError and ImportError
        return fail_closed(check_id, f"{type(e).__name__}: {e}", subject_sha)


def run_checks(specs: Iterable[tuple[str, Callable[..., Any], dict[str, Any]]], subject_sha: str = "") -> list[CheckResult]:
    """Run several ``(check_id, fn, facts)`` triples; each is fail-closed on its own."""
    return [run_check(cid, subject_sha, fn, **facts) for cid, fn, facts in specs]


def raise_on_assert_failure(results: Iterable[CheckResult]) -> None:
    """Raise ``AssertCheckError`` when an ASSERT check failed or did not run (a bug, not a content problem)."""
    for r in results:
        if r.kind == "assert" and not r.passed:
            raise AssertCheckError(f"{r.check_id}: {r.evidence or 'assert failed'}")
